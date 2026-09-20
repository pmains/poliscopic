#!/usr/bin/env python3
"""``stage2_schema_readiness.py`` — schema readiness for meeting parentage.

Stage 2 S1 writes ``meetings.public_body_id``.  Writing a parent that the schema
does not protect — no foreign key, no index, or a drifted column type — makes the
graph quietly unreliable, and a dev/prod schema divergence makes it silently
divergent.  This module is the single declaration of what "parentage schema is
ready" means, observes the live schema, and produces an idempotent, digest-bound
DDL plan.

Design rules
------------
* **Declare, then observe.**  Required columns, indexes and foreign keys are data.
  The planner compares them to the live schema and emits only the operations that
  are actually missing.
* **Idempotent.**  Running the planner against an already-ready schema yields zero
  operations.  There is no "nothing to do" ambiguity: the plan states it.
* **Drift is not silently fixed.**  A column that exists with the wrong type or
  nullability is a *blocking* problem.  Re-typing a populated table takes an
  ``ACCESS EXCLUSIVE`` lock and can rewrite it, so it is refused rather than
  performed unattended.
* **Constraints are installed the way this repository already does it.**  The
  established authority ``scripts/db/kg_integrity_schema.py`` adds foreign keys
  ``NOT VALID`` and then validates them; the same two-step is used here, and
  validation is refused when dangling values exist.
* **Development first.**  A production template is produced from the same
  declaration, but its live signature must be captured under separate approval.

Nothing here writes to a database; only the runner does, and it is a separate
module.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

__all__ = [
    "BOUND_MODULES",
    "COLUMN_SPECS",
    "FK_SPECS",
    "INDEX_SPECS",
    "READINESS_VERSION",
    "TABLE",
    "bind_section",
    "blocking_problems",
    "code_hashes",
    "contract_snapshot",
    "is_ready",
    "observe",
    "reader",
    "operations_for",
    "readiness_digest",
    "signature_digest",
]

TABLE = "meetings"
READINESS_VERSION = "kg-stage2-schema-readiness/1.0"

#: Modules whose behaviour defines this readiness contract.
BOUND_MODULES = (
    "scripts/kg/stage2_schema_readiness.py",
    "scripts/kg/stage2_schema_plan.py",
    "scripts/kg/stage2_schema_runner.py",
    "scripts/kg/stage2_parentage_contract.py",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ColumnSpec:
    """A required column and the shape it must have."""

    column: str
    type_family: str
    nullable: bool


@dataclass(frozen=True)
class IndexSpec:
    """A required index, identified by column set rather than by name."""

    columns: tuple[str, ...]
    unique: bool
    preferred_name: str


@dataclass(frozen=True)
class ForeignKeySpec:
    """A required foreign key, identified by shape rather than by name."""

    column: str
    ref_table: str
    ref_column: str
    preferred_name: str


#: ``public_body_id`` is the S1 write target; ``jurisdiction_id`` is carried with
#: it and must be protected the same way.
COLUMN_SPECS: tuple[ColumnSpec, ...] = (
    ColumnSpec("public_body_id", "INTEGER", True),
    ColumnSpec("jurisdiction_id", "INTEGER", True),
)

INDEX_SPECS: tuple[IndexSpec, ...] = (
    IndexSpec(("public_body_id",), False, "ix_meetings_public_body_id"),
    IndexSpec(("jurisdiction_id",), False, "ix_meetings_jurisdiction_id"),
)

FK_SPECS: tuple[ForeignKeySpec, ...] = (
    ForeignKeySpec("public_body_id", "public_bodies", "id", "meetings_public_body_id_fkey"),
    ForeignKeySpec("jurisdiction_id", "jurisdictions", "id", "meetings_jurisdiction_id_fkey"),
)


def type_family(sql_type: Any) -> str:
    """Coarse type family, so dialect spellings remain comparable."""
    text_value = str(sql_type).upper()
    if "INT" in text_value:
        return "INTEGER"
    if any(t in text_value for t in ("CHAR", "TEXT", "CLOB", "STRING", "UUID")):
        return "TEXT"
    if any(t in text_value for t in ("TIMESTAMP", "DATETIME", "DATE", "TIME")):
        return "TEMPORAL"
    if "BOOL" in text_value:
        return "BOOLEAN"
    if any(t in text_value for t in ("NUMERIC", "DECIMAL", "FLOAT", "REAL", "DOUBLE")):
        return "NUMERIC"
    return "OTHER"


def _equivalence(spec: IndexSpec) -> tuple[bool, tuple[str, ...]]:
    return (spec.unique, tuple(spec.columns))


def _pg_constraint_state(
    connection: Any, table: str, column: str, ref_table: str, ref_column: str
) -> tuple[bool, bool]:
    """Return ``(exists, validated)`` for a foreign key on one column.

    Identified by shape, not by name, so a constraint added under a different
    name still counts as satisfying the requirement.
    """
    row = connection.execute(
        text(
            "SELECT c.conname, c.convalidated FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:tbl AS regclass) AND c.contype = 'f' "
            "AND c.conkey = CAST(ARRAY[("
            "  SELECT a.attnum FROM pg_attribute a "
            "  WHERE a.attrelid = CAST(:tbl AS regclass) AND a.attname = :col"
            ")] AS smallint[]) "
            "AND c.confrelid = CAST(:ref AS regclass)"
        ),
        {"tbl": table, "col": column, "ref": ref_table},
    ).fetchone()
    if row is None:
        return False, False
    return True, bool(row[1])


def _inspector(engine: Any) -> Any:
    return sa_inspect(engine)


def reader(target: Any) -> Any:
    """Yield something that can execute, without closing a caller's connection.

    ``observe`` must work both against an engine (a plan-time read) and against
    a connection (an in-transaction postcondition check), so it never assumes
    it owns the resource.
    """
    return nullcontext(target) if hasattr(target, "execute") else target.connect()


def observe(engine: Any) -> dict[str, Any]:
    """Read the live parentage schema.  One read-only pass, no writes."""
    inspector = _inspector(engine)
    live_tables = set(inspector.get_table_names())
    dialect = getattr(getattr(engine, "dialect", None), "name", None) or "unknown"

    columns: dict[str, Any] = {}
    live_columns = {c["name"]: c for c in inspector.get_columns(TABLE)}
    for spec in COLUMN_SPECS:
        found = live_columns.get(spec.column)
        columns[spec.column] = {
            "present": found is not None,
            "type_family": type_family(found["type"]) if found else None,
            "nullable": bool(found["nullable"]) if found else None,
            "expected_type_family": spec.type_family,
            "expected_nullable": spec.nullable,
        }

    live_indexes = [
        {"name": i.get("name"), "unique": bool(i.get("unique")),
         "columns": tuple(i.get("column_names") or ())}
        for i in inspector.get_indexes(TABLE)
    ]
    indexes: dict[str, Any] = {}
    for spec in INDEX_SPECS:
        matching = [i for i in live_indexes if _equivalence(spec) == (i["unique"], i["columns"])]
        indexes[",".join(spec.columns)] = {
            "satisfied": bool(matching),
            "matching_names": sorted(str(i["name"]) for i in matching),
            "preferred_name": spec.preferred_name,
            "name_matches_preferred": any(i["name"] == spec.preferred_name for i in matching),
        }

    foreign_keys: dict[str, Any] = {}
    dangling: dict[str, int] = {}
    present_columns = {
        name: bool(state.get("present")) for name, state in columns.items()
    }
    with reader(engine) as connection:
        for spec in FK_SPECS:
            if dialect == "postgresql":
                exists, validated = _pg_constraint_state(
                    connection, TABLE, spec.column, spec.ref_table, spec.ref_column
                )
            else:
                declared = [
                    f for f in inspector.get_foreign_keys(TABLE)
                    if tuple(f.get("constrained_columns") or ()) == (spec.column,)
                    and f.get("referred_table") == spec.ref_table
                ]
                exists, validated = bool(declared), bool(declared)
            foreign_keys[spec.column] = {
                "exists": exists,
                "validated": validated,
                # A missing parent table is a blocking condition, not a crash:
                # the constraint cannot be installed or validated at all.
                "reference_missing": spec.ref_table not in live_tables,
                "ref_table": spec.ref_table,
                "ref_column": spec.ref_column,
                "preferred_name": spec.preferred_name,
            }

        for spec in FK_SPECS:
            if not present_columns.get(spec.column):
                # No column means no values, so there is nothing to dangle.
                dangling[spec.column] = 0
                continue
            if (foreign_keys.get(spec.column) or {}).get("reference_missing"):
                # Cannot probe a parent table that does not exist; the missing
                # reference is reported as a blocking problem instead.
                dangling[spec.column] = 0
                continue
            dangling[spec.column] = int(
                connection.execute(
                    text(
                        f"SELECT COUNT(*) FROM {TABLE} t WHERE t.{spec.column} IS NOT NULL "
                        f"AND NOT EXISTS (SELECT 1 FROM {spec.ref_table} r "
                        f"WHERE r.{spec.ref_column} = t.{spec.column})"
                    )
                ).scalar()
                or 0
            )

    redundant = {}
    for spec in INDEX_SPECS:
        names = [i["name"] for i in live_indexes
                 if (i["unique"], i["columns"]) == _equivalence(spec)]
        if len(names) > 1:
            redundant[",".join(spec.columns)] = sorted(str(n) for n in names)

    return {
        "dialect": dialect,
        "table": TABLE,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "dangling": dangling,
        "redundant_indexes": redundant,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def blocking_problems(observed: Mapping[str, Any]) -> list[str]:
    """Conditions the planner must not fix unattended."""
    problems: list[str] = []
    for column, state in (observed.get("columns") or {}).items():
        if not state.get("present"):
            continue  # absent is handled by an ADD COLUMN, not a refusal
        if state.get("type_family") != state.get("expected_type_family"):
            problems.append(
                f"{TABLE}.{column} is {state.get('type_family')}, expected "
                f"{state.get('expected_type_family')}: re-typing needs adjudication"
            )
        if bool(state.get("nullable")) != bool(state.get("expected_nullable")):
            problems.append(
                f"{TABLE}.{column} nullability is {bool(state.get('nullable'))}, "
                f"expected {bool(state.get('expected_nullable'))}"
            )
    for column, state in (observed.get("foreign_keys") or {}).items():
        if state.get("reference_missing"):
            problems.append(
                f"{TABLE}.{column} references {state.get('ref_table')}, which does "
                "not exist on this target"
            )
    for column, count in (observed.get("dangling") or {}).items():
        if int(count) > 0:
            problems.append(
                f"{TABLE}.{column} has {count} dangling value(s); a foreign key "
                "cannot be validated until they are resolved"
            )
    return problems


def operations_for(observed: Mapping[str, Any]) -> list[dict[str, str]]:
    """The ordered, idempotent DDL operations this schema still needs.

    Ordering matters: columns must exist before indexes or constraints reference
    them, and a constraint is added ``NOT VALID`` before validation so the
    validating scan is a separate, deliberate step.
    """
    operations: list[dict[str, str]] = []

    for spec in COLUMN_SPECS:
        state = (observed["columns"] or {}).get(spec.column) or {}
        if not state.get("present"):
            operations.append({
                "kind": "add_column",
                "target": f"{TABLE}.{spec.column}",
                "sql": f"ALTER TABLE {TABLE} ADD COLUMN {spec.column} {spec.type_family}",
                "rationale": "column absent; nullable and defaultless so no rewrite occurs",
            })

    for spec in INDEX_SPECS:
        state = (observed["indexes"] or {}).get(",".join(spec.columns)) or {}
        if not state.get("satisfied"):
            columns = ", ".join(spec.columns)
            operations.append({
                "kind": "create_index",
                "target": f"{TABLE}({columns})",
                "sql": f"CREATE INDEX IF NOT EXISTS {spec.preferred_name} "
                       f"ON {TABLE}({columns})",
                "rationale": "no index with this column set exists",
            })

    for spec in FK_SPECS:
        state = (observed["foreign_keys"] or {}).get(spec.column) or {}
        if not state.get("exists"):
            operations.append({
                "kind": "add_fk_not_valid",
                "target": f"{TABLE}.{spec.column} -> {spec.ref_table}.{spec.ref_column}",
                "sql": f"ALTER TABLE {TABLE} ADD CONSTRAINT {spec.preferred_name} "
                       f"FOREIGN KEY ({spec.column}) REFERENCES "
                       f"{spec.ref_table}({spec.ref_column}) NOT VALID",
                "rationale": "added NOT VALID so the validating scan is a separate step",
            })
            if int((observed.get("dangling") or {}).get(spec.column, 0)) == 0:
                operations.append({
                    "kind": "validate_fk",
                    "target": f"{TABLE}.{spec.column}",
                    "sql": f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {spec.preferred_name}",
                    "rationale": "no dangling values, so validation will succeed",
                })
        elif not state.get("validated"):
            if int((observed.get("dangling") or {}).get(spec.column, 0)) == 0:
                operations.append({
                    "kind": "validate_fk",
                    "target": f"{TABLE}.{spec.column}",
                    "sql": f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT "
                           f"{state.get('preferred_name')}",
                    "rationale": "constraint exists unvalidated; no dangling values remain",
                })
    return operations


def is_ready(observed: Mapping[str, Any]) -> bool:
    """Whether the parentage schema is fully in place and enforced."""
    if blocking_problems(observed):
        return False
    return not operations_for(observed)


def signature_digest(observed: Mapping[str, Any]) -> str:
    """Stable digest of the observed schema state."""
    payload = {
        "table": observed.get("table"),
        "columns": observed.get("columns"),
        "indexes": observed.get("indexes"),
        "foreign_keys": observed.get("foreign_keys"),
        "dangling": observed.get("dangling"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def contract_snapshot() -> dict[str, Any]:
    """The declared readiness contract, serialised."""
    return {
        "version": READINESS_VERSION,
        "table": TABLE,
        "columns": [{"column": c.column, "type_family": c.type_family,
                     "nullable": c.nullable} for c in COLUMN_SPECS],
        "indexes": [{"columns": list(i.columns), "unique": i.unique,
                     "preferred_name": i.preferred_name} for i in INDEX_SPECS],
        "foreign_keys": [{"column": f.column, "ref_table": f.ref_table,
                          "ref_column": f.ref_column,
                          "preferred_name": f.preferred_name} for f in FK_SPECS],
        "constraint_installation": "ADD CONSTRAINT ... NOT VALID, then VALIDATE",
        "bound_modules": list(BOUND_MODULES),
    }


def code_hashes(root: Path | str | None = None) -> dict[str, str]:
    """SHA-256 of every module bound to this readiness contract."""
    base = Path(root) if root is not None else _REPO_ROOT
    return {
        relative: hashlib.sha256((base / relative).read_bytes()).hexdigest()
        for relative in BOUND_MODULES
    }


def readiness_digest(
    snapshot: Mapping[str, Any] | None = None, hashes: Mapping[str, str] | None = None
) -> str:
    """Digest over the readiness declaration and the bound module hashes."""
    payload = {
        "contract": dict(snapshot if snapshot is not None else contract_snapshot()),
        "code_hashes": dict(hashes if hashes is not None else code_hashes()),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bind_section(engine: Any) -> dict[str, Any]:
    """The section embedded in a data plan to bind schema readiness.

    A data plan must not be applicable unless the schema that protects its
    writes is in place, so the plan records the readiness declaration, the
    module hashes behind it, and the digest of both.
    """
    observed = observe(engine)
    return {
        "contract": contract_snapshot(),
        "code_hashes": code_hashes(),
        "readiness_digest": readiness_digest(),
        "ready": is_ready(observed),
        "signature_digest": signature_digest(observed),
    }
