#!/usr/bin/env python3
"""``stage2_link_column_apply.py`` — apply the link-column plan, once, atomically.

The apply is small on purpose: one column, one validated foreign key and one index,
in one transaction, after every precondition has been proved and before any data is
touched.  It has no code path for the correction, repair, reservation or containment
plans — those are different plans with their own receipts.

Order of decisions, all refusals before any DDL:

1. the plan is the exact one authorized — digest, kind, version, dry-run, no writes;
2. the code the plan binds is the code on disk;
3. the schema is still the schema the plan was built against;
4. the target is the exact development database the plan names;
5. a protected, restore-verified backup receipt exists and its evidence matches;
6. the column is absent — this apply only ever creates;
7. one transaction adds it, and the postconditions are checked **inside** it.

A replay — the column already present and matching the contract — is a bound no-op
with ``writes=0``, never a second apply.
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
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_link_column as link  # noqa: E402
from scripts.kg import stage2_link_column_plan as planning  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402

__all__ = ["LinkColumnApplyRefused", "RECEIPT_KIND", "apply_plan",
           "protected_row_counts"]

RECEIPT_KIND = "kg-stage2-link-column-schema-receipt"
PREIMAGE_KIND = "kg-stage2-link-column-schema-preimage"
APPLY_DIALECTS = ("postgresql",)

#: The tables whose rows this apply must not change.
PROTECTED_TABLES = planning.PROTECTED_TABLES


class LinkColumnApplyRefused(RuntimeError):
    """The apply was refused; nothing was created."""


def protected_row_counts(connection: Any) -> dict[str, int]:
    return {table: int(connection.execute(
        text(f"SELECT COUNT(*) FROM {table}")).scalar())
        for table in PROTECTED_TABLES}


def _fingerprint(counts: Mapping[str, int]) -> str:
    return planning.canonical_sha256(dict(sorted(counts.items())))


def _engine_identity(engine: Any) -> dict[str, Any]:
    url = engine.url
    return {"dialect": url.drivername.split("+")[0], "host": url.host,
            "port": url.port, "database": url.database}


def _write(out_dir: str | Path, name: str, payload: Mapping[str, Any]):
    path = Path(out_dir) / name
    digest = artifacts.write_immutable(path, dict(payload))
    return path, digest


def require_backup(receipt_path: str | Path, *, target: Mapping[str, Any]) -> dict:
    """A restore-verified, 0600 receipt whose dump still hashes to its record."""
    path = Path(receipt_path)
    if not path.exists():
        raise LinkColumnApplyRefused(f"backup receipt {path.name!r} does not exist")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise LinkColumnApplyRefused(f"the backup receipt mode is {oct(mode)}, not 0o600")
    receipt = json.loads(path.read_text())
    if dict(receipt.get("target") or {}).get("database") != target.get("database"):
        raise LinkColumnApplyRefused("the backup was taken from a different database")
    result = receipts.validate_receipt(receipt, expected_counts=None)
    if not result["valid"]:
        raise LinkColumnApplyRefused(
            f"backup receipt is invalid: {result['problems'][:3]}")
    if not receipts.restore_verified(receipt):
        raise LinkColumnApplyRefused("the backup receipt does not prove a verified restore")
    dump = Path(str(receipt.get("dump_path") or ""))
    if not dump.exists():
        raise LinkColumnApplyRefused("the backup receipt names a dump that is absent")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    if digest != receipt.get("dump_sha256"):
        raise LinkColumnApplyRefused("the dump hash does not match the receipt")
    return {"path": path.name,
            "receipt_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "dump_sha256": digest, "restore_proven": True}


def apply_plan(engine: Any, plan: Mapping[str, Any], *, supplied_digest: str,
               backup_receipt: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Apply the additive link column, once, in one transaction."""
    if artifacts.compute_digest(plan) != supplied_digest:
        raise LinkColumnApplyRefused("plan digest changed after loading")
    problems = planning.validate_plan(plan)
    if problems:
        raise LinkColumnApplyRefused("plan refused: " + "; ".join(problems[:5]))

    dialect = engine.dialect.name
    if dialect not in APPLY_DIALECTS:
        raise LinkColumnApplyRefused(f"unsupported dialect: {dialect!r}")
    identity = _engine_identity(engine)
    for field in ("dialect", "host", "port", "database"):
        if (plan.get("target") or {}).get(field) != identity[field]:
            raise LinkColumnApplyRefused(
                f"engine {field} is {identity[field]!r}, plan says "
                f"{(plan.get('target') or {}).get(field)!r}")

    plan_id = supplied_digest[:16]
    terminal = Path(out_dir) / f"kg-stage2-link-column-schema-receipt-{plan_id}.json"
    if terminal.exists():
        # A replay: prove the column still matches the contract, then record a
        # bound no-op.  A replay never issues DDL.
        with engine.connect() as connection:
            remaining = link.verify_contract(connection)
            signature = link.read_signature(connection)
        if signature is None or remaining:
            raise LinkColumnApplyRefused(
                "a terminal receipt exists but the column does not match it: "
                + "; ".join(remaining[:3]))
        receipt = {
            "kind": RECEIPT_KIND, "stage": "replay-no-op", "plan_digest": supplied_digest,
            "plan_id": plan_id, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "writes": 0, "existing_receipt": terminal.name,
            "column": signature["column"], "foreign_key": signature["constraint"],
            "replay": True,
        }
        path, digest = _write(out_dir,
                              f"kg-stage2-link-column-replay-{plan_id}.json", receipt)
        return {**receipt, "receipt_path": str(path), "receipt_digest": digest}

    backup = require_backup(backup_receipt, target=plan.get("target") or {})
    with engine.connect() as connection:
        live_signature = planning.schema_signature(connection)
        before = protected_row_counts(connection)

    bound = (plan["bindings"]["schema_signature"] or {}).get("digest")
    if live_signature["digest"] != bound:
        raise LinkColumnApplyRefused(
            "the live schema is not the schema the plan recorded")
    if live_signature["link_column_present"]:
        raise LinkColumnApplyRefused("the link column already exists; refusing")

    preimage, preimage_digest = _write(
        out_dir, f"kg-stage2-link-column-preimage-{plan_id}.json", {
            "kind": PREIMAGE_KIND, "plan_digest": supplied_digest,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "engine_target": identity, "schema_signature": live_signature,
            "protected_row_counts": before, "protected_fingerprint": _fingerprint(before),
            "ddl": list(plan["ddl"]), "rollback": dict(plan.get("rollback") or {}),
            "backup_receipt": backup,
        })

    applied: list[str] = []
    with engine.begin() as connection:
        for statement in plan["ddl"]:
            connection.execute(text(statement))
            applied.append(statement)
        remaining = link.verify_contract(connection)
        if remaining:
            raise LinkColumnApplyRefused(
                "postconditions failed: " + "; ".join(remaining))
        after = protected_row_counts(connection)
        if after != before:
            raise LinkColumnApplyRefused(
                f"protected row counts changed: {before} -> {after}")
        if _fingerprint(after) != _fingerprint(before):
            raise LinkColumnApplyRefused("protected fingerprint changed")
        signature_after = link.read_signature(connection)

    receipt = {
        "kind": RECEIPT_KIND, "stage": "terminal", "plan_digest": supplied_digest,
        "plan_id": plan_id, "applied_at": datetime.now(timezone.utc).isoformat(),
        "engine_target": identity, "statements": applied, "writes": len(applied),
        "backup_receipt": backup, "preimage_artifact": preimage.name,
        "preimage_digest": preimage_digest,
        "link_column_signature_after": signature_after,
        "protected_row_counts_before": before, "protected_row_counts_after": before,
        "postconditions": {
            "column_present": signature_after is not None,
            "column_type": (signature_after or {}).get("data_type"),
            "nullable": (signature_after or {}).get("nullable"),
            "foreign_key": (signature_after or {}).get("constraint"),
            "on_delete": (signature_after or {}).get("on_delete"),
            "indexes": [i["indexname"] for i in (signature_after or {}).get("indexes", [])],
            "protected_unchanged": True,
        },
        "replay": False,
    }
    path, digest = _write(
        out_dir, f"kg-stage2-link-column-schema-receipt-{plan_id}.json", receipt)
    return {**receipt, "receipt_path": str(path), "receipt_digest": digest}
