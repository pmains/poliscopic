#!/usr/bin/env python3
"""``stage3_b2_alias_apply.py`` — the development-only alias schema/data apply runner.

DISABLED BY DESIGN.  ``ENABLED`` is False and the public entry points additionally require an
explicit authorization token, so this module cannot mutate a database until a human flips both.
The transactional core is implemented and exercised by ISOLATED tests, which is what "verified
but disabled" means here.

Fails closed on: target identity, tier, plan digests, writer-registry drift, provenance,
collisions, current-state drift, backup receipt, and any transaction or postcondition failure.
It never merges or deletes a source row; the only schema change is the additive alias table and
the only rollback is dropping that table.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_backup_verify as backup_verify  # noqa: E402
from scripts.kg import stage3_b2_identity as identity  # noqa: E402
from db.tier import PRODUCTION_HOST_MARKERS  # noqa: E402

__all__ = ["ALIAS_TABLE", "AUTHORIZATION_TOKEN", "ENABLED", "ApplyRefused",
           "apply_aliases", "replay_aliases", "rollback_aliases", "rollback_receipt"]

ENABLED = False
AUTHORIZATION_TOKEN = "stage3-b2-alias-development-apply"
ALIAS_TABLE = identity.ALIAS_TABLE
PRODUCER_VERSION = "kg-stage3-b2-alias-apply/1.0"
RECEIPT_KIND = "kg-stage3-b2-alias-apply-receipt"
PREIMAGE_KIND = "kg-stage3-b2-alias-apply-preimage"
ROLLBACK_KIND = "kg-stage3-b2-alias-rollback-receipt"
ALLOWED_TIERS = ("development",)
REFUSED_TARGET_MARKERS = PRODUCTION_HOST_MARKERS


class ApplyRefused(RuntimeError):
    """The apply was refused; nothing was created or modified."""


def canonical_sha256(payload: Any) -> str:
    return identity.canonical_sha256(payload)


def _write(out_dir: str | Path, name: str, payload: Mapping[str, Any]):
    path = Path(out_dir) / name
    return path, artifacts.write_immutable(path, dict(payload))


# --- gates ------------------------------------------------------------------

def check_target(engine: Any, *, tier: str) -> dict[str, Any]:
    url = engine.url
    host = str(url.host or "")
    if engine.dialect.name != "postgresql":
        raise ApplyRefused(f"unsupported dialect {engine.dialect.name!r}")
    if any(m in host for m in REFUSED_TARGET_MARKERS):
        raise ApplyRefused("production targets are refused structurally")
    if tier not in ALLOWED_TIERS:
        raise ApplyRefused(f"tier {tier!r} is not development")
    if "dev" not in str(url.database or ""):
        raise ApplyRefused(f"database {url.database!r} is not a development database")
    return {"dialect": engine.dialect.name, "host": host, "port": url.port,
            "database": url.database, "tier": tier}


def require_backup(receipt_path: str | Path, *, target: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(receipt_path)
    if not path.exists():
        raise ApplyRefused(f"backup receipt {path.name!r} does not exist")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ApplyRefused("the backup receipt mode is not 0o600")
    receipt = json.loads(path.read_text())
    if dict(receipt.get("target") or {}).get("database") != target.get("database"):
        raise ApplyRefused("the backup was taken from a different database")
    if not receipts.restore_verified(receipt):
        raise ApplyRefused("the backup receipt proves no verified restore")
    problems = backup_verify.validate_stage2_receipt(receipt)
    if problems:
        raise ApplyRefused("the backup receipt is not Stage-2 verified: "
                           + "; ".join(problems[:3]))
    dump = Path(str(receipt.get("dump_path") or ""))
    if not dump.exists():
        raise ApplyRefused("the backup receipt names a missing dump")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    if digest != receipt.get("dump_sha256"):
        raise ApplyRefused("the dump hash does not match the receipt")
    return {"path": path.name,
            "canonical_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "dump_sha256": digest, "restore_proven": True}


def check_plan_files(*, data_plan_path: str | Path, schema_plan_path: str | Path,
                     supplied_data_digest: str, supplied_schema_digest: str) -> dict[str, Any]:
    """The caller must name the EXACT artifacts; each file must be the live head whose own
    recorded digest equals the digest supplied.  This stops a different file being loaded."""
    out: dict[str, Any] = {}
    for label, path, supplied in (("data", Path(data_plan_path), supplied_data_digest),
                                  ("schema", Path(schema_plan_path), supplied_schema_digest)):
        if not path.exists():
            raise ApplyRefused(f"the {label} plan path {path.name!r} does not exist")
        if (path.parent / (path.name + ".obsolete.json")).exists():
            raise ApplyRefused(f"the {label} plan {path.name!r} is superseded")
        doc = artifacts.load_verified(path)
        if doc.get("digest") != supplied:
            raise ApplyRefused(f"the {label} plan file is not the supplied digest")
        out[label] = {"path": path.name, "digest": supplied}
    return out


def check_plan_bindings(data_plan: Mapping[str, Any], schema_plan: Mapping[str, Any], *,
                        supplied_data_digest: str,
                        supplied_schema_digest: str) -> dict[str, Any]:
    """Digest, binding, writer-registry, code and validator gates.  All must pass."""
    if data_plan.get("digest") != supplied_data_digest:
        raise ApplyRefused("the data plan digest changed after loading")
    if schema_plan.get("digest") != supplied_schema_digest:
        raise ApplyRefused("the schema plan digest changed after loading")
    for plan, label in ((data_plan, "data"), (schema_plan, "schema")):
        if plan.get("digest") != canonical_sha256(
                {k: v for k, v in plan.items() if k != "digest"}):
            raise ApplyRefused(f"the {label} plan digest is not canonical")
    if schema_plan.get("bindings", {}).get("data_plan_digest") != data_plan.get("digest"):
        raise ApplyRefused("the schema plan does not bind this data plan")
    producer = data_plan.get("producer") or {}
    if producer.get("namespace_registry") != identity.NAMESPACE_REGISTRY_VERSION:
        raise ApplyRefused("writer-registry drift: the plan names a different namespace "
                           "registry version")
    if producer.get("version") != identity.PRODUCER_VERSION:
        raise ApplyRefused("producer drift: the plan names a different identity producer")
    problems = identity.validate_data_plan(data_plan)
    if problems:
        raise ApplyRefused("the data plan is invalid: " + "; ".join(problems[:4]))
    sproblems = identity.validate_schema_plan(schema_plan)
    if sproblems:
        raise ApplyRefused("the schema plan is invalid: " + "; ".join(sproblems[:4]))
    if data_plan.get("no_merge") is not True or data_plan.get("no_row_deletion") is not True:
        raise ApplyRefused("the plan does not forbid merges and deletions")
    for plan, label in ((data_plan, "data"), (schema_plan, "schema")):
        bound = (plan.get("bindings") or {}).get("code_hashes")
        if not bound:
            raise ApplyRefused(f"the {label} plan binds no implementation code hashes")
        drift = identity.code_drift(bound)
        if drift:
            raise ApplyRefused(f"stale-code refusal: the {label} plan was built against "
                               f"different code {drift}")
    return {"data_digest": data_plan["digest"], "schema_digest": schema_plan["digest"],
            "operations": len(data_plan.get("operations") or []),
            "namespace_registry": producer.get("namespace_registry")}


def check_current_state(connection: Any, data_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Live drift + collision gate.  Nothing is written by this function."""
    problems = identity.validate_data_plan(data_plan, connection=connection)
    if problems:
        raise ApplyRefused("live drift: " + "; ".join(problems[:4]))
    present = int(connection.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' "
        "AND table_name=:t"), {"t": ALIAS_TABLE}).scalar() or 0)
    if present:
        raise ApplyRefused(f"{ALIAS_TABLE} already exists; this plan expects it absent")
    before = meetings_counts(connection)
    return {"table_absent": True, "meetings_before": before,
            "operations": len(data_plan["operations"])}


def meetings_counts(connection: Any) -> dict[str, int]:
    out = {}
    for t in ("meetings", "agenda_items", "supporting_documents"):
        out[t] = int(connection.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0)
    return out


# --- transactional core (exercised by isolated tests) -----------------------

def sqlite_ddl() -> list[str]:
    """Test-only SQLite translation with the SAME semantics as the plan's DDL.

    Written explicitly rather than by string substitution: SQLite cannot ADD CONSTRAINT, and
    a naive replacement turns "varchar(64)" into "varTEXT" because it contains "char(64)".
    """
    return [
        f"CREATE TABLE {ALIAS_TABLE} ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "canonical_meeting_id INTEGER NOT NULL REFERENCES meetings(id) "
        "ON DELETE RESTRICT ON UPDATE CASCADE, "
        "source_meeting_id INTEGER NULL REFERENCES meetings(id) "
        "ON DELETE RESTRICT ON UPDATE CASCADE, "
        "source_system TEXT NOT NULL, body TEXT NOT NULL, external_id TEXT NOT NULL, "
        "rule TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, justification TEXT NULL, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))",
        f"CREATE UNIQUE INDEX uq_meeting_source_alias ON {ALIAS_TABLE} "
        "(source_system, body, external_id)",
        f"CREATE INDEX ix_meeting_source_aliases_canonical ON {ALIAS_TABLE} "
        "(canonical_meeting_id)",
        f"CREATE INDEX ix_meeting_source_aliases_source_meeting ON {ALIAS_TABLE} "
        "(source_meeting_id)",
    ]


def ensure_schema(connection: Any, schema_plan: Mapping[str, Any], *,
                  dialect: str = "postgresql") -> list[str]:
    statements = sqlite_ddl() if dialect == "sqlite" else list(schema_plan["ddl"])
    applied = []
    for statement in statements:
        connection.execute(text(statement))
        applied.append(statement)
    return applied


def insert_aliases(connection: Any, data_plan: Mapping[str, Any], *,
                   dialect: str = "postgresql") -> int:
    written = 0
    for op in data_plan["operations"]:
        statement = (
            f"INSERT INTO {ALIAS_TABLE} (canonical_meeting_id, source_meeting_id, "
            "source_system, body, external_id, rule, evidence_sha256, justification) "
            "VALUES (:c, :s, :sys, :body, :ext, :rule, :ev, :just)")
        if dialect == "postgresql":
            statement += " ON CONFLICT DO NOTHING"
        else:
            # SQLite: refuse on conflict rather than silently ignoring, so a duplicate
            # identity surfaces as a refusal instead of a no-op.
            statement = statement.replace("INSERT INTO", "INSERT OR ROLLBACK INTO", 1)
        params = {"c": int(op["canonical_meeting_db_id"]),
                  "s": int(op["source_meeting_db_id"]),
                  "sys": op["source_system"], "body": op["body"],
                  "ext": op["external_id"],
                  "rule": ",".join(op["rule"]) if isinstance(op["rule"], list) else op["rule"],
                  "ev": op["evidence_sha256"], "just": op.get("justification")}
        try:
            result = connection.execute(text(statement), params)
            written += int(result.rowcount or 0)
        except Exception as exc:  # noqa: BLE001 - a conflict must refuse, never merge
            raise ApplyRefused(f"insert refused for {op['external_id']}: {exc}") from exc
    return written


def postconditions(connection: Any, data_plan: Mapping[str, Any], *,
                   before: Mapping[str, int]) -> dict[str, Any]:
    expected = len(data_plan["operations"])
    count = int(connection.execute(text(f"SELECT COUNT(*) FROM {ALIAS_TABLE}")).scalar() or 0)
    if count != expected:
        raise ApplyRefused(f"alias count {count} != expected {expected}")
    after = meetings_counts(connection)
    if after != dict(before):
        raise ApplyRefused(f"protected row counts changed: {before} -> {after}")
    cross = int(connection.execute(text(
        f"SELECT COUNT(*) FROM {ALIAS_TABLE} WHERE canonical_meeting_id = source_meeting_id"
    )).scalar() or 0)
    if cross:
        raise ApplyRefused("a self-referential alias was inserted")
    return {"aliases": count, "protected_unchanged": True, "self_references": 0}


def _apply_in_transaction(connection: Any, data_plan: Mapping[str, Any],
                          schema_plan: Mapping[str, Any], *, dialect: str,
                          dry_run: bool = False) -> dict[str, Any]:
    """The whole apply in ONE transaction.  Rolls back when ``dry_run``."""
    before = meetings_counts(connection)
    applied = ensure_schema(connection, schema_plan, dialect=dialect)
    written = insert_aliases(connection, data_plan, dialect=dialect)
    checks = postconditions(connection, data_plan, before=before)
    return {"schema_statements": len(applied), "writes": written, "postconditions": checks,
            "dry_run": dry_run}


def apply_aliases(engine: Any, *, data_plan: Mapping[str, Any],
                  schema_plan: Mapping[str, Any], supplied_data_digest: str,
                  supplied_schema_digest: str, data_plan_path: str | Path,
                  schema_plan_path: str | Path, backup_receipt: str | Path,
                  out_dir: str | Path, approver: str, tier: str = "development",
                  authorization: str | None = None) -> dict[str, Any]:
    """The public apply.  Refuses unless the module is enabled AND authorization matches."""
    if not ENABLED:
        raise ApplyRefused("the alias apply is DISABLED by design (ENABLED is False)")
    if authorization != AUTHORIZATION_TOKEN:
        raise ApplyRefused("explicit authorization is required for a development apply")
    if not approver:
        raise ApplyRefused("a named human approver is required")
    target = check_target(engine, tier=tier)
    backup = require_backup(backup_receipt, target=target)
    files = check_plan_files(data_plan_path=data_plan_path, schema_plan_path=schema_plan_path,
                            supplied_data_digest=supplied_data_digest,
                            supplied_schema_digest=supplied_schema_digest)
    bindings = check_plan_bindings(data_plan, schema_plan,
                                   supplied_data_digest=supplied_data_digest,
                                   supplied_schema_digest=supplied_schema_digest)
    plan_id = supplied_data_digest[:16]
    terminal = Path(out_dir) / f"kg-stage3-b2-alias-apply-receipt-{plan_id}.json"
    with engine.connect() as connection:
        state = check_current_state(connection, data_plan)
        if terminal.exists():
            count = int(connection.execute(text(
                f"SELECT COUNT(*) FROM {ALIAS_TABLE}")).scalar() or 0)
            if count != len(data_plan["operations"]):
                raise ApplyRefused("a terminal receipt exists but the alias count disagrees")
    if terminal.exists():
        out = {"kind": RECEIPT_KIND, "stage": "replay-no-op", "writes": 0,
               "plan_digest": supplied_data_digest, "replay": True}
        path, digest = _write(out_dir, f"kg-stage3-b2-alias-replay-{plan_id}.json", out)
        return {**out, "receipt_path": str(path), "receipt_digest": digest}
    preimage, preimage_digest = _write(
        out_dir, f"kg-stage3-b2-alias-apply-preimage-{plan_id}.json", {
            "kind": PREIMAGE_KIND, "plan_digest": supplied_data_digest,
            "schema_digest": supplied_schema_digest, "target": target,
            "approver": approver, "backup_receipt": backup, "bindings": bindings,
            "state": state, "operations": data_plan["operations"]})
    connection = engine.connect().execution_options(isolation_level="SERIALIZABLE")
    try:
        with connection.begin():
            connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 0x5A2D_0002})
            # LOCKED rechecks: nothing may drift between the gates and the writes.
            drift = identity.validate_data_plan(data_plan, connection=connection)
            if drift:
                raise ApplyRefused("locked recheck failed: " + "; ".join(drift[:4]))
            if meetings_counts(connection) != state["meetings_before"]:
                raise ApplyRefused("locked recheck: protected row counts moved")
            result = _apply_in_transaction(connection, data_plan, schema_plan,
                                           dialect="postgresql")
            if result["schema_statements"] != len(schema_plan["ddl"]):
                raise ApplyRefused("locked recheck: not every schema statement was applied")
            if result["writes"] != len(data_plan["operations"]):
                raise ApplyRefused(f"locked recheck: wrote {result['writes']} of "
                                   f"{len(data_plan['operations'])} aliases")
            tables = int(connection.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' "
                "AND table_name=:t"), {"t": ALIAS_TABLE}).scalar() or 0)
            if tables != 1:
                raise ApplyRefused("locked recheck: exactly one table must be created")
    except ApplyRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure must roll back and refuse
        raise ApplyRefused(f"transaction failed and was rolled back: {exc}") from exc
    finally:
        connection.close()
    receipt = {"kind": RECEIPT_KIND, "stage": "terminal", "writes": result["writes"],
               "plan_digest": supplied_data_digest, "schema_digest": supplied_schema_digest,
               "approver": approver, "backup_receipt": backup,
               "preimage_artifact": preimage.name, "preimage_digest": preimage_digest,
               "schema_statements": result["schema_statements"],
               "postconditions": result["postconditions"],
               "applied_at": datetime.now(timezone.utc).isoformat(),
               "replay": False}
    path, digest = _write(out_dir,
                          f"kg-stage3-b2-alias-apply-receipt-{plan_id}.json", receipt)
    return {**receipt, "receipt_path": str(path), "receipt_digest": digest}


def replay_aliases(engine: Any, *, data_plan: Mapping[str, Any],
                   supplied_data_digest: str | None = None) -> dict[str, Any]:
    """Inspect the alias table without writing.  A replay is a strict no-op."""
    if not ENABLED:
        raise ApplyRefused("the alias apply is DISABLED by design (ENABLED is False)")
    with engine.connect() as connection:
        present = int(connection.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' "
            "AND table_name=:t"), {"t": ALIAS_TABLE}).scalar() or 0)
        if not present:
            return {"stage": "replay-no-op", "writes": 0, "table_present": False}
        count = int(connection.execute(text(f"SELECT COUNT(*) FROM {ALIAS_TABLE}")).scalar() or 0)
    return {"stage": "replay-no-op", "writes": 0, "table_present": True, "aliases": count}


def rollback_receipt(*, data_plan: Mapping[str, Any], approver: str,
                     out_dir: str | Path,
                     reason: str) -> dict[str, Any]:
    """The rollback record.  The rollback itself is a single DROP of the additive table."""
    plan_id = str(data_plan.get("digest"))[:16]
    body = {"kind": ROLLBACK_KIND, "stage": "rollback-planned",
            "plan_digest": data_plan.get("digest"), "approver": approver,
            "reason": reason, "statements": [f"DROP TABLE {ALIAS_TABLE}"],
            "source_rows_touched": 0, "source_rows_deleted": 0,
            "planned_at": datetime.now(timezone.utc).isoformat()}
    path, digest = _write(out_dir, f"kg-stage3-b2-alias-rollback-{plan_id}.json", body)
    return {**body, "receipt_path": str(path), "receipt_digest": digest}


def rollback_aliases(engine: Any, *, out_dir: str | Path, approver: str,
                     reason: str, authorization: str | None = None) -> dict[str, Any]:
    """Execute the rollback.  Disabled with the same gate as the apply."""
    if not ENABLED:
        raise ApplyRefused("the alias apply is DISABLED by design (ENABLED is False)")
    if authorization != AUTHORIZATION_TOKEN:
        raise ApplyRefused("explicit authorization is required for a rollback")
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE {ALIAS_TABLE}"))
    return {"stage": "rolled-back", "approver": approver, "reason": reason,
            "statements": [f"DROP TABLE {ALIAS_TABLE}"]}
