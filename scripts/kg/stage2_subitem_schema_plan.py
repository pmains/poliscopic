#!/usr/bin/env python3
"""``stage2_subitem_schema_plan.py`` — immutable plan for the containment schema.

One additive change to ``agenda_items``: a nullable ``parent_item_id`` self-reference with
the CHILD holding the pointer, a self-reference CHECK, a reverse lookup index, and a
deferrable ``ON DELETE RESTRICT`` foreign key.

Honest enforcement boundary, carried into the plan so nobody reads a CHECK as doing work it
cannot do: ONLY the self-reference rule is a CHECK constraint.  Same-meeting,
number-shortening and acyclicity are NOT CHECK constraints - a PostgreSQL CHECK may not
contain a subquery - so they are stated as transactional preconditions owned by the data
apply, not by this schema.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.schema_scope import current_schema_columns  # noqa: E402

__all__ = ["CHECK_NAME", "CODE_MODULES", "COLUMN", "FK_NAME", "INDEX_NAME", "PLAN_KIND",
           "build_plan", "code_hashes", "ddl", "schema_signature", "validate_plan"]

PLAN_KIND = "kg-stage2-subitem-schema-plan"
PLAN_VERSION = "kg-stage2-subitem-schema-plan/1.0"
TABLE = "agenda_items"
COLUMN = "parent_item_id"
CHECK_NAME = "ck_agenda_items_parent_item_id_self"
FK_NAME = "fk_agenda_items_parent_item_id"
INDEX_NAME = "ix_agenda_items_parent_item_id"

CODE_MODULES = (
    "scripts/kg/stage2_subitem_schema_plan.py",
    "scripts/kg/stage2_subitem_schema_apply.py",
    "scripts/kg/stage2_subitem_schema.py",
    "scripts/kg/stage2_artifacts.py",
)

TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")
PROTECTED_TABLES = ("agenda_items", "supporting_documents", "meetings")


def ddl() -> list[str]:
    return [
        f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} integer NULL",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_NAME} "
        f"CHECK ({COLUMN} IS NULL OR {COLUMN} <> id)",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {FK_NAME} FOREIGN KEY ({COLUMN}) "
        f"REFERENCES {TABLE}(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED",
        f"CREATE INDEX {INDEX_NAME} ON {TABLE} ({COLUMN})",
    ]


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def code_hashes(modules: Sequence[str] = CODE_MODULES) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in modules:
        p = REPO / rel
        if p.exists():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def schema_signature(connection: Any) -> dict[str, Any]:
    """The exact pre-schema facts, plus the column's absence."""
    from sqlalchemy import text

    cols = current_schema_columns(connection, TABLE)
    pk = connection.execute(text("""
        SELECT a.attname FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'p'
        AND c.conrelid = to_regclass(format('%I.%I', current_schema(), :t))
        ORDER BY k.ord"""), {"t": TABLE}).scalars().all()
    indexes = [r[0] for r in connection.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = :t ORDER BY indexname"),
        {"t": TABLE})]
    fks = [r[0] for r in connection.execute(text("""
        SELECT conname FROM pg_constraint WHERE contype = 'f'
        AND conrelid = to_regclass(format('%I.%I', current_schema(), :t))
        ORDER BY conname"""), {"t": TABLE}).scalars()]
    counts = {t: int(connection.execute(
        text(f"SELECT COUNT(*) FROM public.{t}")).scalar()) for t in PROTECTED_TABLES}
    body = {"columns": [dict(c) for c in cols], "primary_key": list(pk),
            "indexes": indexes, "foreign_keys": fks, "protected_row_counts": counts,
            "column_present": any(c["column_name"] == COLUMN for c in cols)}
    return {**body, "digest": canonical_sha256(body)}


def build_plan(connection: Any, *, target: Mapping[str, Any], created_at: str,
               safety_modules: Sequence[str] | None = None) -> dict[str, Any]:
    signature = schema_signature(connection)
    modules = tuple(safety_modules or ()) + CODE_MODULES
    plan = {
        "kind": PLAN_KIND, "version": PLAN_VERSION, "created_at": created_at,
        "mode": "dry-run", "applied": False,
        "target": {f: target.get(f) for f in TARGET_FIELDS},
        "table": TABLE, "column": COLUMN, "ddl": ddl(),
        "column_spec": {"name": COLUMN, "type": "integer", "nullable": True},
        "check": {"name": CHECK_NAME,
                  "expression": f"{COLUMN} IS NULL OR {COLUMN} <> id"},
        "foreign_key": {"name": FK_NAME, "references": f"{TABLE}(id)",
                        "on_delete": "RESTRICT", "deferrable": "INITIALLY DEFERRED"},
        "index": {"name": INDEX_NAME, "columns": [COLUMN], "unique": False},
        "enforcement": {
            "check_enforced": ["self_reference"],
            "transactional_only": ["same_meeting", "number_shortening", "no_cycles"],
            "statement": "ONLY the self-reference rule is a CHECK constraint; the "
                         "same-meeting, number-shortening and acyclicity rules are "
                         "enforced as transactional preconditions in the data apply.",
        },
        "direction": {"holder": "child", "points_at": "parent", "relation": "PART_OF"},
        "additive_only": True,
        "touches_existing_columns": False,
        "bindings": {"code_hashes": code_hashes(modules),
                     "schema_signature": signature},
        "preconditions": {"target_is_development": True,
                          "column_absent": signature["column_present"] is False,
                          "protected_backup_verified": "restore-verified receipt"},
        "postconditions": {"column_present": True, "check_present": True,
                           "foreign_key_present": True, "index_present": True,
                           "all_existing_rows_null": True,
                           "protected_row_counts_unchanged": True},
        "rollback": {"statements": [f"DROP INDEX {INDEX_NAME}",
                                    f"ALTER TABLE {TABLE} DROP CONSTRAINT {FK_NAME}",
                                    f"ALTER TABLE {TABLE} DROP CONSTRAINT {CHECK_NAME}",
                                    f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"]},
        "write_path": "absent by design",
    }
    plan["replay_digest"] = canonical_sha256(
        {k: v for k, v in plan.items() if k not in (artifacts.DIGEST_FIELD,
                                                    "replay_digest", "created_at")})
    plan[artifacts.DIGEST_FIELD] = artifacts.compute_digest(plan)
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
    if plan.get("mode") != "dry-run" or plan.get("applied") is not False:
        problems.append("the plan must be dry-run and unapplied")
    if list(plan.get("ddl") or ()) != ddl():
        problems.append("the DDL is not the declared containment schema DDL")
    if plan.get("additive_only") is not True or plan.get("touches_existing_columns") is not False:
        problems.append("the change must be additive and must not touch existing columns")
    if (plan.get("index") or {}).get("unique") is not False:
        problems.append("the reverse index must not be unique")
    if (plan.get("foreign_key") or {}).get("on_delete") != "RESTRICT":
        problems.append("ON DELETE must be RESTRICT")
    enf = plan.get("enforcement") or {}
    if enf.get("transactional_only") != ["same_meeting", "number_shortening", "no_cycles"]:
        problems.append("the enforcement boundary must name the non-CHECK rules")
    recorded = (plan.get("bindings") or {}).get("code_hashes") or {}
    live = code_hashes(tuple(recorded))
    drift = sorted(r for r, d in recorded.items() if live.get(r) != d)
    if drift:
        problems.append(f"code has drifted for {drift}")
    if not (plan.get("bindings") or {}).get("schema_signature", {}).get("digest"):
        problems.append("the plan binds no schema signature")
    if (plan.get("target") or {}).get("tier") != "development":
        problems.append("the plan target is not development")
    if plan.get(artifacts.DIGEST_FIELD) != artifacts.compute_digest(plan):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems
