#!/usr/bin/env python3
"""``stage2_link_column_plan.py`` — the immutable plan for the link column.

The plan is the reviewed object.  It binds, by digest:

* the **target** — dialect, host, port, database, tier;
* the **code** that would apply it, so it cannot be applied by another revision;
* the **current schema** it was built against — the exact type of the referenced key,
  the table's primary key, its index set, and the column's absence — so it cannot be
  applied to a schema that has moved;
* the **contract** whose reasoning justifies the column existing at all.

Everything is additive: one column, one validated foreign key, one index.  No
existing column, index or row is modified, and there is no write path here.
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
from scripts.kg import stage2_link_column as link  # noqa: E402

__all__ = ["CODE_MODULES", "PLAN_KIND", "PLAN_VERSION", "build_plan",
           "code_hashes", "schema_signature", "validate_plan"]

PLAN_KIND = "kg-stage2-link-column-schema-plan"
PLAN_VERSION = "kg-stage2-link-column-schema-plan/1.0"
DIGEST_FIELD = artifacts.DIGEST_FIELD

#: Every module that decides whether this plan may be applied, or would apply it.
CODE_MODULES = (
    "scripts/kg/stage2_link_column.py",
    "scripts/kg/stage2_link_column_plan.py",
    "scripts/kg/stage2_link_column_apply.py",
    "scripts/kg/stage2_s2_admission_tx.py",
    "scripts/kg/stage2_artifacts.py",
)

TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")

#: The tables whose rows this apply must not change.
PROTECTED_TABLES = ("agenda_items", "supporting_documents", "meetings")


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
    """What the plan was built against, as facts rather than as prose.

    A different referenced-key type, a different primary key, a different index set,
    or an already-present column all change the digest, so a stale plan cannot be
    applied to a moved schema.
    """
    from sqlalchemy import text

    referenced = connection.execute(text("""
        SELECT data_type, is_nullable FROM information_schema.columns
        WHERE table_name = :t AND column_name = :c"""),
        {"t": link.REFERENCES_TABLE, "c": link.REFERENCES_COLUMN}).mappings().first()
    primary = connection.execute(text("""
        SELECT a.attname FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'p' AND c.conrelid = CAST(:t AS regclass) ORDER BY k.ord"""),
        {"t": link.LINK_TABLE}).scalars().all()
    indexes = connection.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = :t ORDER BY indexname"),
        {"t": link.LINK_TABLE}).scalars().all()
    counts = {table: int(connection.execute(
        text(f"SELECT COUNT(*) FROM {table}")).scalar())
        for table in PROTECTED_TABLES}
    body = {
        "referenced_key": dict(referenced or {}),
        "link_table_primary_key": list(primary),
        "link_table_indexes": list(indexes),
        "link_column_present": link.read_signature(connection) is not None,
        "protected_row_counts": counts,
    }
    return {**body, "digest": canonical_sha256(body)}


def build_plan(connection: Any, *, target: Mapping[str, Any],
               created_at: str) -> dict[str, Any]:
    """Assemble the plan over the live schema."""
    signature = schema_signature(connection)
    plan = {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "created_at": created_at,
        "mode": "dry-run",
        "target": {field: target.get(field) for field in TARGET_FIELDS},
        "table": link.LINK_TABLE,
        "column": link.COLUMN_NAME,
        "ddl": link.ddl(),
        "column_spec": {"name": link.COLUMN_NAME, "type": link.COLUMN_TYPE,
                        "nullable": link.NULLABLE},
        "foreign_key": {"name": link.CONSTRAINT_NAME, "columns": [link.COLUMN_NAME],
                        "references_table": link.REFERENCES_TABLE,
                        "references_columns": [link.REFERENCES_COLUMN],
                        "on_delete": link.ON_DELETE, "on_update": link.ON_UPDATE,
                        "validated": True},
        "index": link.index_justification(),
        "no_legacy_substitution": {
            "rejected_column": "agenda_item_id",
            "why": "agenda_item_id is a source-system key, not a database identity; "
                   "using it would conflate the vendor's item label with the row the "
                   "document belongs to",
        },
        "additive_only": True,
        "touches_existing_columns": False,
        "bindings": {
            "code_hashes": code_hashes(),
            "schema_signature": signature,
            # The ORM's own declaration, so the live column cannot drift from the
            # model and the model cannot drift from the DDL.
            "parity": link.parity_contract(),
        },
        "preconditions": {
            "target_is_development": True,
            "link_column_absent": signature["link_column_present"] is False,
            "referenced_key_is_integer_identity": True,
            "protected_backup_verified": "a restore-verified development receipt",
            "no_existing_row_is_modified": True,
        },
        "postconditions": {
            "column_present": True,
            "column_nullable_integer": True,
            "foreign_key_validated": True,
            "on_delete_set_null": True,
            "index_present": True,
            "agenda_items_row_count_unchanged": True,
            "supporting_documents_row_count_unchanged": True,
            "meetings_row_count_unchanged": True,
        },
        "rollback": {
            "statements": [
                f"DROP INDEX {link.INDEX_NAME}",
                f"ALTER TABLE {link.LINK_TABLE} DROP CONSTRAINT {link.CONSTRAINT_NAME}",
                f"ALTER TABLE {link.LINK_TABLE} DROP COLUMN {link.COLUMN_NAME}",
            ],
            "safe_because": "the column is created empty and is only written by a "
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
    if plan.get("table") != link.LINK_TABLE:
        problems.append(f"the table must be {link.LINK_TABLE!r}")
    if plan.get("column") != link.COLUMN_NAME:
        problems.append(f"the column must be {link.COLUMN_NAME!r}")
    if list(plan.get("ddl") or ()) != link.ddl():
        problems.append("the DDL is not the declared link-column DDL")
    if (plan.get("column_spec") or {}).get("nullable") is not True:
        problems.append("the column must be nullable")
    if (plan.get("column_spec") or {}).get("type") != link.COLUMN_TYPE:
        problems.append(f"the column type must be {link.COLUMN_TYPE!r}")
    foreign = plan.get("foreign_key") or {}
    if foreign.get("on_delete") != link.ON_DELETE:
        problems.append(f"ON DELETE must be {link.ON_DELETE!r}")
    if foreign.get("validated") is not True:
        problems.append("the foreign key must be validated")
    if not (plan.get("index") or {}).get("access_paths"):
        problems.append("the index must carry its justification")
    if plan.get("additive_only") is not True:
        problems.append("the plan must be additive only")
    if plan.get("touches_existing_columns") is not False:
        problems.append("the plan must not touch existing columns")

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
    parity = bindings.get("parity") or {}
    if parity.get("constraint") != link.CONSTRAINT_NAME:
        problems.append("the plan does not bind the model's constraint name")
    if parity.get("on_delete") != link.ON_DELETE:
        problems.append("the plan's parity contract disagrees on ON DELETE")
    problems.extend(link.verify_parity())
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
