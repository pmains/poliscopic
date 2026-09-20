#!/usr/bin/env python3
"""``stage2_subitem_schema_apply.py`` — apply the containment schema plan, once.

Additive, transactional, exact-digest, backup-protected and replay-safe.  It refuses on
any target, schema, code, digest or backup drift, on a pre-existing column/index/constraint,
and on any protected-row change - and it never commits if a postcondition fails, because the
DDL and its validation share ONE transaction.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_subitem_schema_plan as planning  # noqa: E402

__all__ = ["ApplyRefused", "RECEIPT_KIND", "apply_plan", "protected_row_counts"]

RECEIPT_KIND = "kg-stage2-subitem-schema-receipt"
PREIMAGE_KIND = "kg-stage2-subitem-schema-preimage"


class ApplyRefused(RuntimeError):
    """The apply was refused; nothing was created."""


def protected_row_counts(connection: Any) -> dict[str, int]:
    return {t: int(connection.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar())
            for t in planning.PROTECTED_TABLES}


def _write(out_dir: str | Path, name: str, payload: Mapping[str, Any]):
    path = Path(out_dir) / name
    return path, artifacts.write_immutable(path, dict(payload))


def require_backup(receipt_path: str | Path, *, target: Mapping[str, Any]) -> dict:
    path = Path(receipt_path)
    if not path.exists():
        raise ApplyRefused(f"backup receipt {path.name!r} does not exist")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ApplyRefused("the backup receipt mode is not 0o600")
    receipt = json.loads(path.read_text())
    if dict(receipt.get("target") or {}).get("database") != target.get("database"):
        raise ApplyRefused("the backup was taken from a different database")
    if not receipts.validate_receipt(receipt, expected_counts=None)["valid"]:
        raise ApplyRefused("the backup receipt is invalid or stale")
    if not receipts.restore_verified(receipt):
        raise ApplyRefused("the backup receipt proves no verified restore")
    dump = Path(str(receipt.get("dump_path") or ""))
    if not dump.exists():
        raise ApplyRefused("the backup receipt names a missing dump")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    if digest != receipt.get("dump_sha256"):
        raise ApplyRefused("the dump hash does not match the receipt")
    return {"path": path.name, "canonical_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "dump_sha256": digest, "restore_proven": True}


def _existing_objects(connection: Any) -> list[str]:
    found: list[str] = []
    for kind, name in (("column", planning.COLUMN), ("index", planning.INDEX_NAME),
                       ("constraint", planning.CHECK_NAME),
                       ("constraint", planning.FK_NAME)):
        if kind == "column":
            hit = connection.execute(text(
                "SELECT COUNT(*) FROM information_schema.columns WHERE table_name=:t "
                "AND column_name=:c"), {"t": planning.TABLE, "c": name}).scalar()
        elif kind == "index":
            hit = connection.execute(text(
                "SELECT COUNT(*) FROM pg_indexes WHERE tablename=:t AND indexname=:i"),
                {"t": planning.TABLE, "i": name}).scalar()
        else:
            hit = connection.execute(text(
                "SELECT COUNT(*) FROM pg_constraint WHERE conname=:n"), {"n": name}).scalar()
        if hit:
            found.append(name)
    return found


def apply_plan(engine: Any, plan: Mapping[str, Any], *, supplied_digest: str,
               backup_receipt: str | Path, out_dir: str | Path) -> dict[str, Any]:
    if artifacts.compute_digest(plan) != supplied_digest:
        raise ApplyRefused("the plan digest changed after loading")
    problems = planning.validate_plan(plan)
    if problems:
        raise ApplyRefused("plan refused: " + "; ".join(problems[:4]))
    if engine.dialect.name != "postgresql":
        raise ApplyRefused(f"unsupported dialect: {engine.dialect.name!r}")
    identity = {"dialect": engine.url.get_backend_name(), "host": engine.url.host,
                "port": engine.url.port, "database": engine.url.database}
    for field in ("dialect", "host", "port", "database"):
        if (plan.get("target") or {}).get(field) != identity[field]:
            raise ApplyRefused(f"engine {field} disagrees with the plan")

    plan_id = supplied_digest[:16]
    terminal = Path(out_dir) / f"kg-stage2-subitem-schema-receipt-{plan_id}.json"
    if terminal.exists():
        with engine.connect() as connection:
            remaining = [o for o in _existing_objects(connection)]
        if len(remaining) != 4:
            raise ApplyRefused("a terminal receipt exists but the objects do not match it")
        receipt = {"kind": RECEIPT_KIND, "stage": "replay-no-op", "writes": 0,
                   "plan_digest": supplied_digest, "recorded_at":
                       datetime.now(timezone.utc).isoformat(), "replay": True}
        path, digest = _write(out_dir, f"kg-stage2-subitem-schema-replay-{plan_id}.json",
                              receipt)
        return {**receipt, "receipt_path": str(path), "receipt_digest": digest}

    backup = require_backup(backup_receipt, target=plan.get("target") or {})
    with engine.connect() as connection:
        live = planning.schema_signature(connection)
        before = protected_row_counts(connection)
    if live["digest"] != (plan["bindings"]["schema_signature"] or {}).get("digest"):
        raise ApplyRefused("the live schema is not the schema the plan recorded")
    preexisting = _existing_objects(engine.connect())
    if preexisting:
        raise ApplyRefused(f"objects already exist: {preexisting}")

    preimage, preimage_digest = _write(
        out_dir, f"kg-stage2-subitem-schema-preimage-{plan_id}.json", {
            "kind": PREIMAGE_KIND, "plan_digest": supplied_digest,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schema_signature": live, "protected_row_counts": before,
            "ddl": list(plan["ddl"]), "backup_receipt": backup})

    applied: list[str] = []
    with engine.begin() as connection:
        for statement in plan["ddl"]:
            connection.execute(text(statement))
            applied.append(statement)
        after = protected_row_counts(connection)
        if after != before:
            raise ApplyRefused(f"protected row counts changed: {before} -> {after}")
        objects = _existing_objects(connection)
        if len(objects) != 4:
            raise ApplyRefused(f"postconditions failed; present objects {objects}")
        non_null = int(connection.execute(text(
            f"SELECT COUNT(*) FROM {planning.TABLE} WHERE {planning.COLUMN} IS NOT NULL"
        )).scalar())
        if non_null != 0:
            raise ApplyRefused(f"{non_null} rows are non-NULL in a freshly added column")
        signature_after = planning.schema_signature(connection)

    receipt = {"kind": RECEIPT_KIND, "stage": "terminal", "writes": len(applied),
               "plan_digest": supplied_digest,
               "applied_at": datetime.now(timezone.utc).isoformat(),
               "engine_target": identity, "statements": applied,
               "backup_receipt": backup, "preimage_artifact": preimage.name,
               "preimage_digest": preimage_digest,
               "schema_signature_after_digest": signature_after["digest"],
               "protected_row_counts_before": before, "protected_row_counts_after": before,
               "postconditions": {"objects_present": 4, "all_existing_rows_null": True,
                                  "protected_unchanged": True},
               "replay": False}
    path, digest = _write(out_dir,
                          f"kg-stage2-subitem-schema-receipt-{plan_id}.json", receipt)
    return {**receipt, "receipt_path": str(path), "receipt_digest": digest}
