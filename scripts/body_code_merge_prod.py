#!/usr/bin/env python3
"""Production-target-bound plan, backup, and scratch proof (Brief 031 §C).

This module PREPARES the production consolidation of the split body identities.
It has four modes:

  --plan           read-only. Build a production-bound plan: exact mutation
                   code hashes + live target identity + plan digest.
  --backup         read-only on production. Fresh public-application dump at
                   mode 0600 plus a production baseline envelope.
  --scratch-prove  restore the dump into an ISOLATED local PostgreSQL cluster,
                   verify counts/schema/integrity/target binding, then COMMIT
                   the exact merge against the restored copy and verify its
                   postconditions.
  --apply          the only mode that mutates production. Fail-closed: it
                   requires the exact reviewed digest and a valid production
                   backup receipt, recomputes the plan live, and refuses on any
                   drift, collision, or postcondition failure.

Production is only ever READ by --plan and --backup.  The stage2 baseline
helpers refuse non-development targets by design, so this module reuses their
tier-agnostic capture functions and supplies a production envelope instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

from body_code_merge_runtime import (  # noqa: E402
    DEV_TARGET,
    OPTIONAL_TABLES,
    PRODUCTION_TARGET,
    assert_target,
    build_plan,
    code_hashes,
    content_digest,
    execute_plan,
    merge_lock,
    schema_capabilities,
)
from ops.production_interlock_guard import require_production_interlock  # noqa: E402
from kg import stage2_artifacts, stage2_backup_verify  # noqa: E402

PG = Path("/opt/homebrew/opt/postgresql@18/bin")
OUT = Path("data/backups")
PLAN_DIR = Path("data/body-code-merge/prod")

SCRATCH_DB = "poliscopic_merge_scratch_prod"
SCRATCH_TARGET = {"tier": "development", "database": SCRATCH_DB}
PRODUCTION_TIER = "production"

BASELINE_KIND = "body-code-merge-prod-baseline"
RECEIPT_KIND = "body-code-merge-prod-backup-receipt"
APPLY_RECEIPT_KIND = "body-code-merge-prod-apply-receipt"

# Baseline counts.  The third field names the table a count depends on, or None
# for counts over always-present tables.  Counts over optional tables are
# captured only when the live schema has them, so the SAME code produces a
# valid baseline on development (both present) and production (both absent).
BASELINE_COUNTS = (
    ("meetings", "meetings", None, "SELECT COUNT(*) FROM meetings"),
    ("public_bodies", "public_bodies", None,
     "SELECT COUNT(*) FROM public_bodies"),
    ("supporting_documents", "supporting_documents", None,
     "SELECT COUNT(*) FROM supporting_documents"),
    ("agenda_items", "agenda_items", None, "SELECT COUNT(*) FROM agenda_items"),
    ("agenda_item_key_reservation", "agenda_item_key_reservation", None,
     "SELECT COUNT(*) FROM agenda_item_key_reservation"),
    ("meeting_events", "meeting_events", None,
     "SELECT COUNT(*) FROM meeting_events"),
    ("agenda_items_with_parent", "agenda_items", "parent_item_id",
     "SELECT COUNT(*) FROM agenda_items WHERE parent_item_id IS NOT NULL"),
    ("supporting_documents_linked", "supporting_documents", "agenda_item_db_id",
     "SELECT COUNT(*) FROM supporting_documents "
     "WHERE agenda_item_db_id IS NOT NULL"),
)


def capture_counts(engine, capabilities: dict) -> dict[str, int]:
    """Baseline counts, restricted to tables AND columns that actually exist."""
    tables = capabilities.get("optional_tables") or {}
    columns = capabilities.get("optional_columns") or {}
    out: dict[str, int] = {}
    with engine.connect() as connection:
        for key, table, column, sql in BASELINE_COUNTS:
            if table in OPTIONAL_TABLES and not tables.get(table):
                continue
            if column and not columns.get(f"{table}.{column}", True):
                continue
            out[key] = int(connection.execute(text(sql)).scalar() or 0)
    return out


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def pg_environment() -> dict[str, str]:
    """Environment for cluster commands.

    PostgreSQL 18 on macOS aborts at startup with "postmaster became
    multithreaded during startup" unless the locale is pinned explicitly, so
    LC_ALL is set for every cluster invocation.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def run(args: list[str], *, env: dict[str, str] | None = None,
        capture: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=capture,
                            env=env if env is not None else pg_environment())
    if result.returncode:
        # stderr is None when capture=False, so never assume it exists —
        # otherwise the real failure is masked by an AttributeError.
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"{Path(args[0]).name} failed (rc={result.returncode}): {detail}")
    return result


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def create_empty_scratch_database(port: int) -> None:
    """Create the named disposable DB with no pre-existing public schema."""
    connection = ["-h", "127.0.0.1", "-p", str(port), "-U", "poliscopic"]
    run([str(PG / "createdb"), *connection, SCRATCH_DB])
    run([str(PG / "psql"), "-v", "ON_ERROR_STOP=1", *connection,
         "-d", SCRATCH_DB, "-c", "DROP SCHEMA IF EXISTS public CASCADE"])


def public_dump_command(url, dump_path: Path) -> list[str]:
    """A rollback-capable public application dump without FDW credentials.

    The production ``dev`` schema is an FDW mirror and its user mapping embeds
    a development password.  Dumping only ``public`` retains the application's
    schema and table data while excluding that mirror and database-global FDW
    objects.  The TOC is checked after dumping as a second fail-closed guard.
    """
    return [str(PG / "pg_dump"), "-Fc", "--no-owner", "--no-privileges",
            "--schema=public", "--exclude-schema=dev",
            "-h", str(url.host), "-p", str(url.port), "-U", str(url.username),
            "-d", str(url.database), "-f", str(dump_path)]


def assert_public_dump_toc(listing: str) -> None:
    """Refuse a dump whose TOC still carries FDW credentials or the dev schema."""
    forbidden = ("FOREIGN DATA WRAPPER", "FOREIGN SERVER", "USER MAPPING")
    found = [marker for marker in forbidden if marker in listing]
    if found:
        raise RuntimeError(f"refusing dump with FDW objects: {found}")
    if any(" SCHEMA - dev" in line or " dev " in line and "TABLE DATA" in line
           for line in listing.splitlines()):
        raise RuntimeError("refusing dump containing dev-schema objects")
    lines = listing.splitlines()
    if not any(" TABLE public" in line for line in lines):
        raise RuntimeError("refusing dump without public application table definitions")
    if not any("TABLE DATA public" in line for line in lines):
        raise RuntimeError("refusing dump without public application table data")


def production_engine():
    """Connect to production, refusing anything that is not the pinned target.

    Credentials resolve through the AUTHORITATIVE tier resolver so a swapped or
    misclassified URL is refused here rather than trusted (Brief 031E item 7).
    """
    from dotenv import load_dotenv

    load_dotenv()  # repo convention: credentials live in .env, never the shell
    raw = os.environ.get("PROD_DATABASE_URL")
    if not raw:
        raise SystemExit("refusing: PROD_DATABASE_URL is not set")
    from db.tier import PRODUCTION, TierError, resolve_role_url

    try:
        resolved = resolve_role_url(PRODUCTION, raw, label="body-code merge")
    except TierError as exc:
        raise SystemExit(f"refusing: tier resolver rejected the URL: {exc}")
    url_value = str(getattr(resolved, "url", "") or raw)
    engine = create_engine(url_value, future=True, pool_pre_ping=True)
    with engine.connect() as connection:
        assert_target(connection, PRODUCTION_TARGET)
    return engine


def capture_baseline(engine, *, created_at: str) -> dict:
    """Immutable pre-merge baseline of the production database."""
    from body_code_merge_runtime import schema_capabilities, target_identity

    with engine.connect() as connection:
        identity = assert_target(connection, PRODUCTION_TARGET)
        capabilities = schema_capabilities(connection)
    counts = capture_counts(engine, capabilities)
    signature = stage2_backup_verify.capture_schema_signature(engine)
    integrity = stage2_backup_verify.capture_integrity(engine)
    body = {"kind": BASELINE_KIND, "version": 1, "created_at": created_at,
            "tier": PRODUCTION_TIER, "target": identity,
            "capabilities": capabilities,
            "counts": counts, "counts_sha256": canonical_sha256(counts),
            "schema_signature": signature, "integrity": integrity}
    return {**body, "digest": canonical_sha256(body)}


def validate_baseline(baseline: dict | None) -> list[str]:
    problems: list[str] = []
    if not isinstance(baseline, dict):
        return ["the baseline must be a mapping"]
    if baseline.get("kind") != BASELINE_KIND:
        problems.append(f"kind must be {BASELINE_KIND!r}")
    if baseline.get("tier") != PRODUCTION_TIER:
        problems.append("the baseline target is not the production tier")
    target = baseline.get("target") or {}
    if target.get("database") != PRODUCTION_TARGET["database"]:
        problems.append("the baseline target is not the production database")
    if target.get("host") != PRODUCTION_TARGET["host"]:
        problems.append("the baseline target host is not the pinned production host")
    counts = baseline.get("counts") or {}
    if not counts:
        problems.append("the baseline records no counts")
    if baseline.get("counts_sha256") != canonical_sha256(counts):
        problems.append("the baseline counts fingerprint does not match its counts")
    signature = baseline.get("schema_signature") or {}
    if not signature.get("schema_sha256"):
        problems.append("the baseline records no schema signature")
    if not isinstance(baseline.get("integrity"), dict) or not baseline.get("integrity"):
        problems.append("the baseline records no integrity metrics")
    recorded = baseline.get("digest")
    if not recorded:
        problems.append("the baseline records no digest")
    else:
        body = {k: v for k, v in baseline.items() if k != "digest"}
        if recorded != canonical_sha256(body):
            problems.append("the recorded digest is not the baseline's canonical digest")
    return problems


def compare_restore(baseline: dict, restored: dict) -> list[str]:
    """Exact equality of counts, schema, and integrity between baseline and copy.

    Target tier is deliberately NOT compared: the scratch copy is a local,
    development-tier database by construction.  Target *binding* is enforced
    separately, against PRODUCTION_TARGET, before any write.
    """
    problems: list[str] = []
    b_counts, r_counts = baseline.get("counts") or {}, restored.get("counts") or {}
    for key, expected in sorted(b_counts.items()):
        actual = r_counts.get(key)
        if actual != expected:
            problems.append(f"restored count {key} = {actual} does not match baseline {expected}")
    for key in sorted(set(r_counts) - set(b_counts)):
        problems.append(f"the restored copy has an unexpected count {key!r}")
    b_sig = (baseline.get("schema_signature") or {}).get("schema_sha256")
    r_sig = (restored.get("schema_signature") or {}).get("schema_sha256")
    if b_sig != r_sig:
        problems.append("the restored schema signature does not match the baseline")
    b_items = ((baseline.get("schema_signature") or {}).get("agenda_items") or {})
    r_items = ((restored.get("schema_signature") or {}).get("agenda_items") or {})
    if b_items.get("digest") != r_items.get("digest"):
        problems.append("the restored agenda_items signature does not match the baseline")
    b_int, r_int = baseline.get("integrity") or {}, restored.get("integrity") or {}
    for key, expected in sorted(b_int.items()):
        if r_int.get(key) != expected:
            problems.append(
                f"restored integrity {key} = {r_int.get(key)} does not match baseline {expected}")
    return problems


MAX_ARTIFACT_AGE_HOURS = 24
IDENTITY_KEYS = ("database", "host", "port", "server_version", "dialect",
                 "driver", "cluster_identity")


def require_full_identity(identity: object, *, label: str) -> dict:
    """Return a complete live-target identity or refuse an underspecified one.

    Database/host alone are not a sufficient production binding: a same-named
    database can be restored on a different cluster or reached through a
    different driver.  ``cluster_identity`` may be blank when the production
    role cannot read ``pg_control_system()``, but the field must still be
    recorded and compared exactly.
    """
    if not isinstance(identity, dict):
        raise SystemExit(f"refusing: {label} records no full target identity")
    missing = [key for key in IDENTITY_KEYS if key not in identity]
    if missing:
        raise SystemExit(f"refusing: {label} target identity is missing {missing}")
    if (identity.get("database") != PRODUCTION_TARGET["database"] or
            identity.get("host") != PRODUCTION_TARGET["host"] or
            not isinstance(identity.get("port"), int) or identity["port"] <= 0 or
            not str(identity.get("server_version") or "") or
            not str(identity.get("dialect") or "") or
            not str(identity.get("driver") or "")):
        raise SystemExit(f"refusing: {label} is not a complete production identity")
    return {key: identity[key] for key in IDENTITY_KEYS}


def assert_same_identity(left: object, right: object, *, left_label: str,
                         right_label: str) -> dict:
    """Require the full, not partial, production identity to remain unchanged."""
    expected = require_full_identity(left, label=left_label)
    actual = require_full_identity(right, label=right_label)
    if actual != expected:
        raise SystemExit(
            f"refusing: {right_label} target identity differs from {left_label}")
    return expected


def execution_plan(artifact: dict) -> dict:
    """Recover the runtime plan from its immutable artifact envelope.

    ``stage2_artifacts`` reserves ``digest`` for the envelope's self-signature.
    The runtime plan digest therefore lives in ``plan_digest`` and is restored
    only in memory for plan-content and live-plan comparison.
    """
    runtime_digest = artifact.get("plan_digest")
    if not isinstance(runtime_digest, str) or len(runtime_digest) != 64:
        raise SystemExit("refusing: plan artifact records no runtime plan digest")
    plan = dict(artifact)
    plan.pop("plan_digest", None)
    plan.pop("artifact_created_at", None)
    plan["digest"] = runtime_digest
    return plan


def plan_artifact_binding(path: Path, artifact: dict) -> dict:
    """The exact immutable plan proof every subsequent artifact must carry."""
    plan = execution_plan(artifact)
    identity = require_full_identity(plan.get("target_identity"), label="plan")
    return {
        "path": str(Path(path).resolve()),
        "artifact_digest": artifact.get("digest"),
        "plan_digest": plan["digest"],
        "content_digest": content_digest(plan),
        "target_identity": identity,
    }


def _runbook_binding(dump: Path, *, plan_digest: str) -> dict:
    """Bind the restore runbook into the receipt chain (Brief 031E item 8).

    The runbook filename shares the dump's tag, so it is derived rather than
    passed, and its digest is recorded so a later edit is detectable.
    """
    tag = Path(dump).name.removeprefix("poliscopic-body-code-merge-") \
        .removesuffix(".dump")
    path = OUT / f"body-code-merge-prod-restore-runbook-{tag}.md"
    return {"path": str(path.resolve()),
            "sha256": sha256_file(path) if path.is_file() else "",
            "dump_sha256": sha256_file(dump) if dump.is_file() else "",
            "plan_digest": plan_digest,
            "present": path.is_file()}


def verify_runbook_binding(binding: object, *, dump_path: Path,
                           dump_sha256: str, plan_digest: str) -> dict:
    """Verify the runbook itself and its binding to this dump and plan."""
    if not isinstance(binding, dict) or not binding.get("present"):
        raise SystemExit("refusing: backup has no restore runbook proof")
    path = Path(binding.get("path") or "")
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit("refusing: restore runbook is absent or not mode 0600")
    actual = sha256_file(path)
    if binding.get("sha256") != actual:
        raise SystemExit("refusing: restore runbook digest mismatch")
    if binding.get("dump_sha256") != dump_sha256 or binding.get("plan_digest") != plan_digest:
        raise SystemExit("refusing: restore runbook is bound to another dump or plan")
    text = path.read_text(encoding="utf-8")
    if (str(dump_path) not in text or dump_sha256 not in text or
            plan_digest not in text):
        raise SystemExit("refusing: restore runbook content is not bound to its proof")
    return {"path": str(path.resolve()), "sha256": actual,
            "dump_sha256": dump_sha256, "plan_digest": plan_digest,
            "present": True}


def write_immutable(path: Path, payload: dict) -> str:
    """Write through the repository's verified immutable-artifact helper.

    The helper recomputes and records the digest itself, so an artifact can
    never claim a digest that disagrees with its content (Brief 031D item 1).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return stage2_artifacts.write_immutable(path, payload)


def verify_artifact(path: Path, *, label: str) -> dict:
    """Load an artifact and refuse it unless its digest verifies."""
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"refusing: {label} artifact not found: {path}")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit(f"refusing: {label} artifact mode is not 0600")
    try:
        return stage2_artifacts.load_verified(path)
    except Exception as exc:
        raise SystemExit(
            f"refusing: {label} artifact failed verification: {exc}")


def assert_fresh(artifact: dict, *, label: str,
                 created_key: str = "created_at") -> None:
    """Refuse a stale artifact (Brief 031D item 1, freshness binding)."""
    raw = artifact.get(created_key)
    if not raw:
        raise SystemExit(f"refusing: {label} records no {created_key}")
    try:
        created = datetime.fromisoformat(str(raw))
    except ValueError:
        raise SystemExit(f"refusing: {label} has an unparseable {created_key}")
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age > MAX_ARTIFACT_AGE_HOURS:
        raise SystemExit(
            f"refusing: {label} is {age:.1f}h old "
            f"(limit {MAX_ARTIFACT_AGE_HOURS}h) — regenerate it")


def assert_plan_binding(artifact: dict, digest_value: str) -> dict:
    """Refuse unless this is the exact reviewed plan (Brief 031D item 1)."""
    plan = execution_plan(artifact)
    if plan.get("digest") != digest_value:
        raise SystemExit(
            "refusing: supplied digest does not match the plan artifact")
    if plan.get("code_hashes") != code_hashes():
        raise SystemExit(
            "refusing: plan was bound to different mutation code")
    if plan.get("tier") != PRODUCTION_TIER:
        raise SystemExit("refusing: plan artifact is not a production plan")
    require_full_identity(plan.get("target_identity"), label="plan")
    return plan


def write_restore_runbook(path: Path, *, dump_path: str, dump_sha256: str,
                          plan_digest: str) -> None:
    """Post-commit restore runbook using the verified dump (item 8)."""
    body = (
        "# Body-code merge — restore runbook\n\n"
        "Use only if the committed merge must be reversed.  Production was\n"
        "backed up immediately before the merge; restore from that dump.\n\n"
        f"- dump: `{dump_path}`\n"
        f"- dump SHA-256: `{dump_sha256}`\n"
        f"- plan digest: `{plan_digest}`\n\n"
        "## Steps\n\n"
        "1. Verify the dump digest above before touching anything.\n"
        "2. Take the application offline or quiesce writers; the merge runs\n"
        "   under the sync advisory lock, so a restore must hold it too.\n"
        "3. `pg_restore --clean --if-exists --no-owner --no-privileges -d <db> <dump>`\n"
        "4. Re-run the postcondition checks (counts, orphans, event source ids).\n"
        "5. Restart the application and re-run the sync validity check.\n"
        "6. Record the restore as a new receipt; never edit the original.\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)


def require_backup(receipt_path: Path, plan_artifact: dict,
                   plan_path: Path) -> dict:
    """Fail closed unless a restore-verified PRODUCTION backup matches the plan.

    Loaded through the verified immutable-artifact helper, so a tampered
    receipt is refused cryptographically rather than by field inspection
    (Brief 031E item 9).
    """
    path = Path(receipt_path).resolve()
    receipt = verify_artifact(path, label="backup receipt")
    plan = execution_plan(plan_artifact)
    expected_plan = plan_artifact_binding(plan_path, plan_artifact)
    if receipt.get("kind") != RECEIPT_KIND:
        raise SystemExit("refusing: not a production backup receipt")
    if receipt.get("problems"):
        raise SystemExit(f"refusing: backup receipt recorded problems: {receipt['problems']}")
    comparisons = receipt.get("comparisons")
    if (not isinstance(comparisons, dict) or not comparisons or
            any(value is not True for value in comparisons.values())):
        raise SystemExit(f"refusing: backup comparisons did not all pass: {comparisons}")
    assert_same_identity(plan.get("target_identity"), receipt.get("target"),
                         left_label="plan", right_label="backup receipt")
    if receipt.get("tier") != PRODUCTION_TIER:
        raise SystemExit("refusing: backup receipt is not a production receipt")
    dump_path = Path(receipt.get("dump_path") or "")
    if not dump_path.is_file():
        raise SystemExit("refusing: receipt dump is absent")
    if stat.S_IMODE(dump_path.stat().st_mode) != 0o600:
        raise SystemExit("refusing: receipt dump mode is not 0600")
    actual = sha256_file(dump_path)
    if actual != receipt.get("dump_sha256"):
        raise SystemExit("refusing: backup dump digest mismatch")
    if receipt.get("plan_artifact") != expected_plan:
        raise SystemExit("refusing: backup receipt was proven against another plan artifact")
    # Keep the legacy field too, but make it agree with the exact artifact chain.
    if receipt.get("plan_content_digest") != expected_plan["content_digest"]:
        raise SystemExit("refusing: backup receipt was proven against different plan content")
    runbook = verify_runbook_binding(
        receipt.get("restore_runbook"), dump_path=dump_path,
        dump_sha256=actual, plan_digest=plan["digest"])
    return {"receipt": str(path), "receipt_digest": receipt["digest"],
            "dump_path": str(dump_path), "dump_sha256": actual,
            "plan_artifact": expected_plan, "restore_runbook": runbook}


def mode_plan() -> int:
    engine = production_engine()
    with engine.connect() as connection:
        plan = build_plan(connection, PRODUCTION_TARGET)
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    path = PLAN_DIR / f"body-code-merge-prod-plan-{stamp()}.json"
    # The runtime plan digest and the immutable envelope digest are distinct.
    # Do not let the latter silently replace the former (both are needed later).
    artifact = dict(plan)
    artifact["plan_digest"] = artifact.pop("digest")
    artifact["artifact_created_at"] = now()
    write_immutable(path, artifact)
    reviewed = verify_artifact(path, label="plan")
    reviewed_plan = execution_plan(reviewed)
    if reviewed_plan["digest"] != plan["digest"]:
        raise SystemExit("refusing: immutable plan round-trip changed its digest")
    print(json.dumps({
        "status": "planned", "plan": str(path.resolve()),
        "digest": reviewed_plan["digest"], "artifact_digest": reviewed["digest"],
        "content_digest": content_digest(reviewed_plan),
        "tier": plan["tier"], "target": plan["target"],
        "code_hashes": plan["code_hashes"],
        "merges": [{"old": m["old"], "new": m["new"],
                    "overlap_meetings": len(m["meeting_map"]),
                    "deduplicated_items": len(m["item_map"])}
                   for m in plan["merges"]],
    }, sort_keys=True))
    return 0


def mode_backup(plan_path: Path, digest_value: str) -> int:
    reviewed_artifact = verify_artifact(Path(plan_path), label="plan")
    reviewed = assert_plan_binding(reviewed_artifact, digest_value)
    assert_fresh(reviewed_artifact, label="plan", created_key="artifact_created_at")
    plan_proof = plan_artifact_binding(plan_path, reviewed_artifact)
    engine = production_engine()
    OUT.mkdir(parents=True, exist_ok=True)
    created = now()
    baseline = capture_baseline(engine, created_at=created)
    assert_same_identity(reviewed.get("target_identity"), baseline.get("target"),
                         left_label="plan", right_label="backup baseline")
    baseline["plan_artifact"] = plan_proof
    # ``capture_baseline`` predates the plan binding.  Recompute its canonical
    # body digest before wrapping it in the immutable artifact envelope.
    baseline["digest"] = canonical_sha256(
        {key: value for key, value in baseline.items() if key != "digest"})
    problems = validate_baseline(baseline)
    if problems:
        raise SystemExit(f"refusing: baseline invalid: {problems}")
    tag = stamp()
    baseline_path = OUT / f"body-code-merge-prod-baseline-{tag}.json"
    stage2_artifacts.write_immutable(baseline_path, baseline)
    dump_path = (OUT / f"poliscopic-body-code-merge-{tag}.dump").resolve()
    env = pg_environment()
    url = engine.url
    if url.password:
        env["PGPASSWORD"] = url.password
    run(public_dump_command(url, dump_path), env=env)
    assert_public_dump_toc(run([str(PG / "pg_restore"), "--list", str(dump_path)]).stdout)
    os.chmod(dump_path, 0o600)
    runbook_path = OUT / f"body-code-merge-prod-restore-runbook-{tag}.md"
    write_restore_runbook(runbook_path, dump_path=str(dump_path),
                          dump_sha256=sha256_file(dump_path),
                          plan_digest=reviewed["digest"])
    runbook = verify_runbook_binding(
        _runbook_binding(dump_path, plan_digest=reviewed["digest"]),
        dump_path=dump_path, dump_sha256=sha256_file(dump_path),
        plan_digest=reviewed["digest"])
    payload = {"status": "dumped", "dump_path": str(dump_path),
               "dump_sha256": sha256_file(dump_path),
               "dump_mode": oct(stat.S_IMODE(dump_path.stat().st_mode)),
               "baseline_path": str(baseline_path.resolve()),
               "baseline_digest": baseline["digest"],
               "plan_artifact": plan_proof,
               "restore_runbook": runbook,
               "counts": baseline["counts"]}
    print(json.dumps(payload, sort_keys=True))
    return 0


def mode_scratch_prove(dump: Path, baseline_path: Path, plan_path: Path,
                       digest_value: str) -> int:
    # The exact reviewed plan and digest are REQUIRED (Brief 031D item 1): the
    # scratch proof must rehearse the reviewed plan, not one it invents.
    reviewed_artifact = verify_artifact(Path(plan_path), label="plan")
    reviewed = assert_plan_binding(reviewed_artifact, digest_value)
    assert_fresh(reviewed_artifact, label="plan", created_key="artifact_created_at")
    plan_proof = plan_artifact_binding(plan_path, reviewed_artifact)
    baseline = verify_artifact(Path(baseline_path), label="baseline")
    problems = validate_baseline(baseline)
    if problems:
        raise SystemExit(f"refusing: baseline invalid: {problems}")
    assert_same_identity(reviewed.get("target_identity"), baseline.get("target"),
                         left_label="plan", right_label="baseline")
    if baseline.get("plan_artifact") != plan_proof:
        raise SystemExit("refusing: baseline was captured for another plan artifact")
    dump = Path(dump).resolve()
    if not dump.is_file():
        raise SystemExit(f"refusing: dump not found: {dump}")
    dump_sha = sha256_file(dump)

    port = free_port()
    comparisons = {k: True for k in (
        "dump_restored", "counts_match", "schema_match", "integrity_match",
        "target_binding_enforced", "content_digest_match")}
    scratch_merge: dict = {}
    stopped = False
    with tempfile.TemporaryDirectory(prefix="poliscopic-body-merge-prod-") as temp:
        data = Path(temp) / "cluster"
        run([str(PG / "initdb"), "-A", "trust", "-U", "poliscopic",
             "-D", str(data), "--encoding=UTF8", "--locale=C"])
        run([str(PG / "pg_ctl"), "-D", str(data),
             "-l", str(Path(temp) / "postgres.log"),
             "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"], capture=False)
        try:
            create_empty_scratch_database(port)
            # A freshly created PostgreSQL database already contains `public`,
            # while the public-only production dump deliberately contains its
            # CREATE SCHEMA statement.  Remove only the disposable scratch
            # database's schema so pg_restore remains strict: any subsequent
            # restore error still aborts the proof instead of being ignored.
            run([str(PG / "pg_restore"), "--no-owner", "--no-privileges",
                 "-h", "127.0.0.1", "-p", str(port), "-U", "poliscopic",
                 "-d", SCRATCH_DB, str(dump)])
            restored_url = URL.create("postgresql", username="poliscopic",
                                      host="127.0.0.1", port=port,
                                      database=SCRATCH_DB)
            engine = create_engine(restored_url, future=True)
            with engine.connect() as capability_connection:
                restored_capabilities = schema_capabilities(capability_connection)
            restored = {
                "counts": capture_counts(engine, restored_capabilities),
                "schema_signature": stage2_backup_verify.capture_schema_signature(engine),
                "integrity": stage2_backup_verify.capture_integrity(engine),
            }
            problems = compare_restore(baseline, restored)
            if problems:
                # Leave the raw evidence on disk before refusing: a digest-only
                # failure is not diagnosable, and this comparison decides whether
                # production can be merged at all.
                evidence_path = (PLAN_DIR /
                                 f"scratch-compare-failure-{stamp()}.json")
                write_immutable(evidence_path, {
                    "problems": problems,
                    "baseline_schema_signature": baseline.get("schema_signature"),
                    "restored_schema_signature": restored.get("schema_signature"),
                    "baseline_counts": baseline.get("counts"),
                    "restored_counts": restored.get("counts"),
                    "baseline_integrity": baseline.get("integrity"),
                    "restored_integrity": restored.get("integrity"),
                })
                raise RuntimeError(
                    f"restored backup differs from baseline: {problems} "
                    f"(evidence: {evidence_path})")

            # Commit the exact merge against the restored production copy.
            with merge_lock(engine) as connection:
                # A development-tier target that is NOT production can bind here;
                # the production guard still fires for the pinned production host.
                assert_target(connection, SCRATCH_TARGET)
                scratch_plan = build_plan(connection, SCRATCH_TARGET)
                # The scratch plan must carry the SAME content as the reviewed
                # production plan; only location fields may differ.
                if content_digest(scratch_plan) != content_digest(reviewed):
                    raise RuntimeError(
                        "refusing: scratch plan content differs from the "
                        "reviewed production plan")
                stats = execute_plan(connection, scratch_plan)
            scratch_merge = {
                "committed": True,
                "plan_digest": scratch_plan["digest"],
                "content_digest": content_digest(scratch_plan),
                "code_hashes": scratch_plan["code_hashes"],
                "post_counts": stats,
                "merges": [{"old": m["old"], "new": m["new"],
                            "overlap_meetings": len(m["meeting_map"]),
                            "deduplicated_items": len(m["item_map"])}
                           for m in scratch_plan["merges"]],
            }
            engine.dispose()
        finally:
            run([str(PG / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                capture=False)
            stopped = True

    receipt = {
        "kind": RECEIPT_KIND, "version": 1, "created_at": now(),
        "tier": PRODUCTION_TIER,
        "target": require_full_identity(baseline.get("target"), label="baseline"),
        "plan_artifact": plan_proof,
        "restore_runbook": verify_runbook_binding(
            _runbook_binding(dump, plan_digest=reviewed["digest"]),
            dump_path=dump, dump_sha256=dump_sha, plan_digest=reviewed["digest"]),
        "baseline_path": str(Path(baseline_path).resolve()),
        "baseline_digest": baseline.get("digest"),
        "dump_path": str(dump), "dump_sha256": dump_sha,
        "dump_mode": oct(stat.S_IMODE(dump.stat().st_mode)),
        "code_hashes": code_hashes(),
        "plan_content_digest": scratch_merge.get("content_digest"),
        "comparisons": comparisons, "problems": [],
        "scratch_merge_apply": scratch_merge,
        "teardown": {"server_stopped": stopped, "temp_dir_removed": True},
    }
    problem_list = validate_baseline(baseline)
    if problem_list:
        raise SystemExit(f"refusing: {problem_list}")
    receipt_path = OUT / f"body-code-merge-prod-backup-receipt-{stamp()}.json"
    write_immutable(receipt_path, receipt)
    print(json.dumps({
        "status": "scratch_proven", "receipt": str(receipt_path.resolve()),
        "dump_sha256": dump_sha,
        "plan_content_digest": scratch_merge.get("content_digest"),
        "scratch_plan_digest": scratch_merge.get("plan_digest"),
        "post_counts": scratch_merge.get("post_counts"),
        "merges": scratch_merge.get("merges"),
    }, sort_keys=True))
    return 0


def mode_apply(digest_value: str, receipt_value: str,
               plan_path: Path) -> int:
    """Mutate production.  Fail-closed; one transaction; receipt only after commit."""
    reviewed_artifact = verify_artifact(Path(plan_path), label="plan")
    reviewed = assert_plan_binding(reviewed_artifact, digest_value)
    assert_fresh(reviewed_artifact, label="plan", created_key="artifact_created_at")
    engine = production_engine()
    with merge_lock(engine) as connection:
        plan = build_plan(connection, PRODUCTION_TARGET)
        if plan["digest"] != digest_value:
            raise SystemExit("refusing: exact live plan digest does not match")
        if content_digest(plan) != content_digest(reviewed):
            raise SystemExit("refusing: live plan content differs from reviewed plan")
        assert_same_identity(reviewed.get("target_identity"), plan.get("target_identity"),
                             left_label="reviewed plan", right_label="live plan")
        backup = require_backup(Path(receipt_value), reviewed_artifact, plan_path)
        stats = execute_plan(connection, plan)
        receipt = {"kind": APPLY_RECEIPT_KIND, "version": 1, "created_at": now(),
                   "status": "success", "plan_digest": plan["digest"],
                   "content_digest": content_digest(plan),
                   "tier": plan["tier"], "target": PRODUCTION_TARGET,
                   "target_identity": plan["target_identity"],
                   "plan_artifact": plan_artifact_binding(plan_path, reviewed_artifact),
                   "capabilities": plan.get("capabilities"),
                   "merges": [{"old": merge["old"], "new": merge["new"]}
                              for merge in plan["merges"]],
                   "backup": backup, "restore_runbook": backup["restore_runbook"],
                   "post_counts": stats}
    path = PLAN_DIR / f"body-code-merge-prod-receipt-{plan['digest'][:16]}.json"
    write_immutable(path, receipt)
    print(json.dumps({**receipt, "receipt": str(path.resolve())}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--scratch-prove", dest="scratch_prove", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dump", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--plan-artifact", dest="plan_artifact", type=Path,
                        help="the reviewed production plan (required to prove/apply)")
    parser.add_argument("--digest")
    parser.add_argument("--backup-receipt")
    args = parser.parse_args()
    chosen = [args.plan, args.backup, args.scratch_prove, args.apply]
    if sum(1 for c in chosen if c) != 1:
        parser.error("choose exactly one of --plan, --backup, --scratch-prove, --apply")

    if args.plan:
        require_production_interlock("OP-STATUS", "scripts/body_code_merge_prod.py")
        return mode_plan()
    if args.backup:
        if not args.plan_artifact or not args.digest:
            parser.error("--backup requires --plan-artifact and --digest")
        require_production_interlock("OP-STATUS", "scripts/body_code_merge_prod.py")
        return mode_backup(args.plan_artifact, args.digest)
    if args.scratch_prove:
        if not args.dump or not args.baseline:
            parser.error("--scratch-prove requires --dump and --baseline")
        if not args.plan_artifact or not args.digest:
            parser.error(
                "--scratch-prove requires --plan-artifact and --digest "
                "(the reviewed plan must be proven, not invented)")
        return mode_scratch_prove(args.dump, args.baseline, args.plan_artifact,
                                  args.digest)
    require_production_interlock("OP-REPAIR", "scripts/body_code_merge_prod.py")
    if not args.digest or not args.backup_receipt or not args.plan_artifact:
        parser.error(
            "--apply requires --digest, --backup-receipt and --plan-artifact")
    return mode_apply(args.digest, args.backup_receipt, args.plan_artifact)


if __name__ == "__main__":
    raise SystemExit(main())
