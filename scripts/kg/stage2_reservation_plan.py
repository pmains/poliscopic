#!/usr/bin/env python3
"""``stage2_reservation_plan.py`` — the immutable schema plan for the reservation.

The plan is the reviewed object.  It binds, by digest:

* the **target** — dialect, host, port, database, tier;
* the **code** that would apply it, so it cannot be applied by a different revision;
* the **current schema** it was built against, so it cannot be applied to a schema
  that has moved;
* the **duplicate-key investigation** and the **collision-control contract** whose
  evidence and reasoning justify the table existing at all.

Everything is additive: one ``CREATE TABLE``.  No existing table, column, index or
row is touched, and there is no write path here — this module reads and describes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_reservation as reservation  # noqa: E402

__all__ = ["CODE_MODULES", "PLAN_KIND", "PLAN_VERSION", "build_plan",
           "schema_signature", "validate_plan"]

PLAN_KIND = "kg-stage2-reservation-schema-plan"
PLAN_VERSION = "kg-stage2-reservation-schema-plan/1.0"
DIGEST_FIELD = artifacts.DIGEST_FIELD

#: Every module that decides whether this plan may be applied, or would apply it.
CODE_MODULES = (
    "scripts/kg/stage2_reservation.py",
    "scripts/kg/stage2_reservation_plan.py",
    "scripts/kg/stage2_reservation_apply.py",
    "scripts/kg/stage2_s2_collision.py",
    "scripts/kg/stage2_s2_admission_tx.py",
    "scripts/kg/stage2_s2_apply_runner.py",
    "scripts/kg/stage2_s2_apply_target.py",
    "scripts/kg/stage2_artifacts.py",
)

TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def code_hashes(modules: Sequence[str] = CODE_MODULES) -> dict[str, str]:
    out: dict[str, str] = {}
    for relative in modules:
        path = REPO / relative
        if path.exists():
            out[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def schema_signature(connection: Any) -> dict[str, Any]:
    """What the plan was built against: the FK target, the key's exact type, and
    the reservation table's absence."""
    from sqlalchemy import text

    meeting_pk = connection.execute(text("""
        SELECT a.attname FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'p' AND c.conrelid = 'meetings'::regclass
        ORDER BY k.ord""")).scalars().all()
    item_key = connection.execute(text("""
        SELECT data_type, character_maximum_length, collation_name
        FROM information_schema.columns
        WHERE table_name = 'agenda_items' AND column_name = 'agenda_item_number'
    """)).mappings().first()
    body = {
        "meetings_primary_key": list(meeting_pk),
        "agenda_item_number": dict(item_key or {}),
        "reservation_table_present": reservation.read_reservation_signature(
            connection) is not None,
    }
    return {**body, "digest": canonical_sha256(body)}


def build_plan(connection: Any, *, target: Mapping[str, Any],
               investigation_path: str | Path, contract_path: str | Path,
               created_at: str) -> dict[str, Any]:
    """Assemble the plan over the live schema and the justifying artifacts."""
    investigation = artifacts.load_verified(investigation_path)
    contract = artifacts.load_verified(contract_path)
    signature = schema_signature(connection)
    plan = {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "created_at": created_at,
        "mode": "dry-run",
        "target": {field: target.get(field) for field in TARGET_FIELDS},
        "table": reservation.RESERVATION_TABLE,
        "ddl": reservation.reservation_ddl(),
        "columns": [dict(c) for c in reservation.COLUMNS],
        "primary_key": ["meeting_db_id", "agenda_item_number"],
        "foreign_key": {"columns": ["meeting_db_id"], "references_table": "meetings",
                        "references_columns": ["id"], "on_delete": "CASCADE",
                        "on_update": "NO ACTION", "validated": True},
        "additive_only": True,
        "touches_existing_tables": False,
        "bindings": {
            "code_hashes": code_hashes(),
            "schema_signature": signature,
            "duplicate_investigation": {
                "path": Path(investigation_path).name,
                "digest": artifacts.recorded_digest(investigation),
                "summary_digest": investigation.get("summary_digest"),
            },
            "collision_contract": {
                "path": Path(contract_path).name,
                "digest": artifacts.recorded_digest(contract),
                "contract_digest": contract.get("contract_digest"),
            },
        },
        "preconditions": {
            "target_is_development": True,
            "reservation_table_absent": signature["reservation_table_present"] is False,
            "protected_backup_verified": "a restore-verified development receipt",
            "investigation_reconciles": True,
            "no_existing_row_is_modified": True,
        },
        "postconditions": {
            "table_present": True,
            "primary_key": ["meeting_db_id", "agenda_item_number"],
            "foreign_key_validated": True,
            "agenda_items_row_count_unchanged": True,
            "supporting_documents_row_count_unchanged": True,
        },
        "rollback": {
            "statement": f"DROP TABLE {reservation.RESERVATION_TABLE}",
            "safe_because": "the table is created empty and is only written by a "
                            "later, separately approved apply",
        },
        "write_path": "absent by design",
        "applied": False,
    }
    plan["replay_digest"] = canonical_sha256(
        {k: v for k, v in plan.items()
         if k not in (DIGEST_FIELD, "replay_digest", "created_at")})
    plan[DIGEST_FIELD] = artifacts.compute_digest(plan)
    problems = validate_plan(plan)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return plan


def validate_plan(plan: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if plan.get("kind") != PLAN_KIND:
        problems.append(f"kind must be {PLAN_KIND!r}")
    if plan.get("version") != PLAN_VERSION:
        problems.append(f"version must be {PLAN_VERSION!r}")
    if plan.get("mode") != "dry-run":
        problems.append("the plan must be dry-run only")
    if plan.get("write_path") != "absent by design":
        problems.append("the plan must declare no write path")
    if plan.get("applied") is not False:
        problems.append("the plan must record applied=false")
    if plan.get("table") != reservation.RESERVATION_TABLE:
        problems.append(f"the table must be {reservation.RESERVATION_TABLE!r}")
    if list(plan.get("ddl") or ()) != reservation.reservation_ddl():
        problems.append("the DDL is not the declared reservation DDL")
    if list(plan.get("primary_key") or ()) != ["meeting_db_id", "agenda_item_number"]:
        problems.append("the primary key is not the exact natural key")
    if plan.get("touches_existing_tables") is not False:
        problems.append("the plan must not touch existing tables")
    if plan.get("additive_only") is not True:
        problems.append("the plan must be additive only")

    bindings = plan.get("bindings") or {}
    recorded = bindings.get("code_hashes") or {}
    for required in CODE_MODULES:
        if required not in recorded:
            problems.append(f"code hashes do not cover {required}")
    live = code_hashes(tuple(recorded))
    drifted = sorted(r for r, d in recorded.items() if live.get(r) != d)
    if drifted:
        problems.append(f"code has drifted for {drifted}")
    if not (bindings.get("schema_signature") or {}).get("digest"):
        problems.append("the plan binds no schema signature")
    for key in ("duplicate_investigation", "collision_contract"):
        entry = bindings.get(key) or {}
        if not entry.get("path") or not entry.get("digest"):
            problems.append(f"the plan does not bind the {key}")
    target = plan.get("target") or {}
    if not target.get("database"):
        problems.append("the plan binds no target database")
    if target.get("tier") != "development":
        problems.append("the plan target is not development")
    if not plan.get("replay_digest"):
        problems.append("replay_digest is missing")
    if plan.get(DIGEST_FIELD) != artifacts.compute_digest(plan):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems
