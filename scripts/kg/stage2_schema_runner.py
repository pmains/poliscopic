#!/usr/bin/env python3
"""``stage2_schema_runner.py`` — apply a Stage 2 parentage schema readiness plan.

**Do not execute without explicit authorization.**  This is the only module that
changes schema, and it is deliberately separate from the planner so the plan a
human reviews is not authored by the thing that executes it.

Contract
--------
* **Saved plan plus exact digest.**  Both the artifact's own digest and the
  caller digest must match before anything else is trusted.
* **Bound to the reviewed plan.**  Before any DDL the runner revalidates the
  readiness contract snapshot, the readiness digest and every bound module hash
  against the plan, and requires the live engine to be the *exact* target the
  plan recorded (dialect, host, database).  A different development database that
  happens to have an identical schema is refused.
* **Derived DDL, not trusted DDL.**  The operation list is recomputed from the
  bound declaration and the live observation and must equal the plan's
  operations exactly — reordered, extra, missing, or arbitrary SQL is refused.
* **Backup evidence, not backup shape.**  The protected restore receipt must
  reproduce the plan's baseline counts, including every required table, not
  merely pass structural validation.
* **Development only.**  Proven through the tier resolver and
  ``assert_development_target``, then bound to the plan's recorded target.
* **Refusals leave evidence.**  Every refusal raised after the plan is loaded is
  recorded as a write-once artifact, and no refusal occurs after DDL has begun.
* **One transaction.**  PostgreSQL DDL is transactional, so every operation runs
  in a single transaction with a bounded ``lock_timeout``; a lock that cannot be
  taken in time fails the run rather than queueing indefinitely.
* **Idempotent.**  An already-ready schema produces zero operations and a success
  receipt that says so; a second apply of the same plan is refused.
* **Rollback is described accurately.**  PostgreSQL DDL — including
  ``ADD CONSTRAINT`` and ``VALIDATE CONSTRAINT`` — is transactional, so a failure
  inside the apply transaction rolls every operation back.  After commit, the
  pre-plan shape is restored by dropping only the constraints, indexes and
  columns this plan introduced.  Nothing here is absolutely irreversible.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import text  # noqa: E402

from scripts.db import tier as tier_module  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_schema_readiness as readiness  # noqa: E402
from scripts.kg.phoenix_dr_adjudication import assert_development_target  # noqa: E402

__all__ = [
    "APPLY_DIALECTS",
    "PLAN_KIND",
    "RECEIPT_KIND",
    "DEFAULT_LOCK_TIMEOUT_MS",
    "SchemaRefused",
    "apply_plan",
    "engine_identity",
    "load_plan_for_digest",
    "main",
    "require_backup",
    "verify_operation_derivation",
    "verify_plan_binding",
    "verify_target_identity",
]

PLAN_KIND = "kg-stage2-schema-readiness-plan"
RECEIPT_KIND = "kg-stage2-schema-readiness-receipt"

#: Only PostgreSQL may alter schema here.
APPLY_DIALECTS = ("postgresql",)

#: Bound on waiting for a table lock, so a busy table fails rather than hangs.
DEFAULT_LOCK_TIMEOUT_MS = 5000


class SchemaRefused(RuntimeError):
    """The schema apply was refused; nothing was changed."""


def engine_identity(engine: Any) -> dict[str, Any]:
    """The live engine's own target identity.

    Never includes credentials: ``str(engine.url)`` renders the password masked,
    and only the parsed host/database are kept.
    """
    parts = urlsplit(str(engine.url))
    return {
        "dialect": engine.dialect.name,
        "host": parts.hostname,
        "database": (parts.path or "").lstrip("/"),
    }


def load_plan_for_digest(plan_path: str | Path, supplied_digest: str) -> dict[str, Any]:
    """Load a plan, requiring both its own digest and the supplied digest."""
    plan = artifacts.load_verified(plan_path)
    actual = artifacts.compute_digest(plan)
    if actual != supplied_digest:
        raise SchemaRefused(
            f"supplied digest {supplied_digest} does not match plan digest {actual}"
        )
    return plan


def verify_target_identity(plan: Mapping[str, Any], engine: Any) -> list[str]:
    """Refuse an engine that is not the exact target the plan recorded.

    Being a development-class database is not sufficient: pointing the reviewed
    plan at a *different* development database with an identical schema would
    otherwise apply DDL to a database nobody reviewed.
    """
    problems: list[str] = []
    live = engine_identity(engine)
    recorded = plan.get("target") or {}
    for field in ("dialect", "host", "database"):
        if recorded.get(field) != live[field]:
            problems.append(
                f"engine {field} is {live[field]!r} but the plan recorded "
                f"{recorded.get(field)!r}"
            )
    return problems


def verify_plan_binding(plan: Mapping[str, Any]) -> list[str]:
    """Revalidate the contract, readiness digest and every bound module hash."""
    problems: list[str] = []
    if plan.get("contract") != readiness.contract_snapshot():
        problems.append("readiness contract drifted since the plan was written")
    if plan.get("readiness_digest") != readiness.readiness_digest():
        problems.append("readiness digest drifted")
    live_hashes = readiness.code_hashes()
    bound_hashes = plan.get("code_hashes") or {}
    for module, digest in sorted(live_hashes.items()):
        if bound_hashes.get(module) != digest:
            problems.append(f"bound module drifted: {module}")
    return problems


def verify_operation_derivation(
    plan: Mapping[str, Any], observed: Mapping[str, Any]
) -> list[str]:
    """Require the plan's operations to be exactly what the declaration derives.

    Recomputing here is what stops an arbitrary SQL string from being smuggled
    into a correctly-hashed plan: order, kind and text must all match, and the
    declared ``expected_ddl`` must match the operations it summarises.
    """
    problems: list[str] = []
    derived = readiness.operations_for(observed)
    planned = list(plan.get("operations") or ())

    if [op.get("kind") for op in planned] != [op["kind"] for op in derived]:
        problems.append("planned operation kinds or order are not the derived operations")
    if [op.get("sql") for op in planned] != [op["sql"] for op in derived]:
        problems.append("planned SQL is not the SQL the declaration derives")
    if list(plan.get("expected_ddl") or ()) != [op.get("sql") for op in planned]:
        problems.append("expected_ddl does not match the planned operations")
    return problems


def require_backup(receipt_path: str | Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Require a protected receipt that reproduces the plan's baseline counts."""
    path = Path(receipt_path)
    if not path.exists():
        raise SchemaRefused(f"backup receipt not found: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SchemaRefused(f"backup receipt is not protected (mode {mode:o}): {path}")

    receipt = json.loads(path.read_text(encoding="utf-8"))
    baseline = plan.get("baseline") or {}
    expected = baseline.get("counts") or {}
    if not expected:
        raise SchemaRefused("plan carries no baseline count evidence to check against")

    restored = receipt.get("counts")
    if not isinstance(restored, Mapping):
        raise SchemaRefused("backup receipt carries no restored counts")
    missing = sorted(set(expected) - set(restored))
    if missing:
        raise SchemaRefused(f"backup receipt is missing required tables: {missing}")

    try:
        result = receipts.require_receipt(receipt, expected_counts=expected)
    except ValueError as exc:
        raise SchemaRefused(f"backup receipt refused: {exc}") from exc

    fingerprint = result.get("counts_fingerprint")
    if fingerprint != baseline.get("counts_fingerprint"):
        raise SchemaRefused(
            "backup receipt counts fingerprint does not match the plan baseline"
        )
    return {"path": str(path), "receipt": receipt, "counts_fingerprint": fingerprint}


def _write(out_dir: str | Path, name: str, payload: Mapping[str, Any]) -> tuple[Path, str]:
    """Write one immutable receipt artifact."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    return path, artifacts.write_immutable(path, payload)


def apply_plan(
    engine: Any,
    plan: Mapping[str, Any],
    *,
    supplied_digest: str,
    backup_receipt: str | Path,
    out_dir: str | Path,
    allow_unsupported_dialect: bool = False,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a reviewed schema readiness plan.

    ``allow_unsupported_dialect`` exists so isolated tests can exercise the
    mechanics on SQLite.  It relaxes only the dialect requirement; every
    identity, binding, derivation, backup-evidence, drift and postcondition check
    still applies.
    """
    if artifacts.compute_digest(plan) != supplied_digest:
        raise SchemaRefused("plan digest changed after loading")
    if plan.get("kind") != PLAN_KIND:
        raise SchemaRefused(f"unexpected plan kind {plan.get('kind')!r}")
    if plan.get("version") != readiness.READINESS_VERSION:
        raise SchemaRefused(f"unexpected plan version {plan.get('version')!r}")

    dialect = engine.dialect.name
    plan_id = str(plan.get("signature_digest"))[:16]
    terminal = Path(out_dir) / f"kg-stage2-schema-receipt-{plan_id}.json"
    if terminal.exists():
        raise SchemaRefused(f"plan {plan_id} already has a terminal receipt: {terminal}")

    live_before = readiness.observe(engine)
    receipt: dict[str, Any] = {
        "kind": RECEIPT_KIND,
        "signature_digest": plan.get("signature_digest"),
        "plan_digest": supplied_digest,
        "applied_at": (now or datetime.now(timezone.utc)).isoformat(),
        "operations_planned": len(plan.get("operations") or ()),
        "expected_ddl": list(plan.get("expected_ddl") or ()),
    }
    executed: list[str] = []

    try:
        # ---- preflight: every refusal below happens before any DDL ----------
        problems = (
            verify_target_identity(plan, engine)
            + verify_plan_binding(plan)
            + verify_operation_derivation(plan, live_before)
        )
        if problems:
            raise SchemaRefused("preflight refused: " + "; ".join(problems[:5]))

        if dialect not in APPLY_DIALECTS and not allow_unsupported_dialect:
            raise SchemaRefused(f"unsupported dialect for schema apply: {dialect!r}")
        target_info = assert_development_target(engine)
        receipt["engine_target"] = target_info

        blocking = list(plan.get("blocking_problems") or ())
        if blocking:
            raise SchemaRefused(
                "plan carries blocking drift that must be adjudicated first: "
                + "; ".join(blocking[:5])
            )
        if readiness.signature_digest(live_before) != plan.get("signature_digest"):
            raise SchemaRefused("live schema drifted from the signature the plan recorded")

        backup = require_backup(backup_receipt, plan)
        receipt["backup_receipt_path"] = backup["path"]
        receipt["backup_counts_fingerprint"] = backup["counts_fingerprint"]

        preimage_name = f"kg-stage2-schema-preimage-{plan_id}.json"
        _write(out_dir, preimage_name, {
            "kind": "kg-stage2-schema-preimage",
            "plan_digest": supplied_digest,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "observed_before": live_before,
            "expected_ddl": list(plan.get("expected_ddl") or ()),
            "rollback": dict(plan.get("rollback") or {}),
        })
        receipt["preimage_artifact"] = preimage_name

        operations = list(plan.get("operations") or ())
        if not operations:
            # Idempotent path: the schema is already ready.
            receipt["status"] = "success"
            receipt["operations_executed"] = 0
            receipt["idempotent_noop"] = True
            receipt["postconditions"] = {"ready": True}
            path, digest = _write(out_dir, terminal.name, receipt)
            receipt["receipt_path"] = str(path)
            receipt["receipt_digest"] = digest
            return receipt

        # ---- DDL: one transaction, bounded lock wait -----------------------
        with engine.begin() as connection:
            if dialect in APPLY_DIALECTS:
                connection.execute(
                    text(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'")
                )
            for operation in operations:
                connection.execute(text(operation["sql"]))
                executed.append(operation["sql"])
            receipt["operations_executed"] = len(executed)

            after = readiness.observe(connection)
            remaining = readiness.operations_for(after)
            blocking_after = readiness.blocking_problems(after)
            if blocking_after or remaining:
                raise SchemaRefused(
                    "postconditions failed: "
                    + "; ".join((blocking_after + [f"{len(remaining)} operation(s) remain"])[:5])
                )
            receipt["postconditions"] = {
                "ready": readiness.is_ready(after),
                "signature_digest_after": readiness.signature_digest(after),
            }
            receipt["status"] = "success"
    except Exception as exc:
        ddl_started = bool(executed)
        refused = isinstance(exc, SchemaRefused) and not ddl_started
        evidence = dict(receipt)
        evidence["status"] = "refused" if refused else "failed"
        evidence["refused_before_ddl"] = refused
        evidence["ddl_started"] = ddl_started
        evidence["operations_executed"] = len(executed)
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        suffix = "-refused" if refused else "-failure"
        try:
            _write(out_dir, f"kg-stage2-schema-receipt-{plan_id}{suffix}.json", evidence)
        except artifacts.ArtifactCollision:
            pass
        raise

    path, digest = _write(out_dir, terminal.name, receipt)
    receipt["receipt_path"] = str(path)
    receipt["receipt_digest"] = digest
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    """Apply a schema readiness plan.  Requires explicit authorization to run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--backup-receipt", required=True)
    parser.add_argument("--out-dir", default="data/kg-plans")
    args = parser.parse_args(argv)

    from scripts.db import config
    from db.core import get_engine

    tier_module.validate_tier_target(config.DB_TIER, config.DB_TARGET)
    if config.DB_TIER != tier_module.DEVELOPMENT:
        raise SchemaRefused(f"schema apply is development-only, got tier {config.DB_TIER!r}")

    plan = load_plan_for_digest(args.plan, args.digest)
    receipt = apply_plan(
        get_engine(), plan,
        supplied_digest=args.digest,
        backup_receipt=args.backup_receipt,
        out_dir=args.out_dir,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
