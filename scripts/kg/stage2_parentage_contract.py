#!/usr/bin/env python3
"""``stage2_parentage_contract.py`` — sync/parity readiness for meeting parentage.

Stage 2 S1 writes ``meetings.public_body_id`` (and relies on ``meetings.jurisdiction_id``).
Two mechanisms decide whether that work is *carried* beyond development, and both
were previously silent about it:

* the dev→prod sync builds each table's payload from the **intersection** of the
  dev and prod column sets, so a column prod has not gained yet is dropped with
  no warning — parentage would simply never arrive;
* the schema-parity contract covered only the eight graph tables, so
  ``meetings`` was not compared at all and a dev/prod divergence was invisible.

This module is the single declaration of what parentage readiness *means*:
which columns must exist, with what type family and nullability, which foreign
keys they target, and which tables must be synced first.  Plan binding, sync
guarding and parity checking all read it, so they cannot disagree.

The functions here are pure given their inputs; nothing contacts a database
unless an engine is explicitly passed, and nothing writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "BOUND_MODULES",
    "PARENTAGE_COLUMNS",
    "PARENTAGE_TABLE",
    "REQUIRED_FK_ORDER",
    "ParentageColumn",
    "column_problems",
    "code_hashes",
    "contract_snapshot",
    "fk_order_problems",
    "readiness_digest",
    "readiness_problems",
    "sync_payload_columns",
    "sync_readiness",
    "type_family",
]

#: The table that carries parentage.
PARENTAGE_TABLE = "meetings"

#: Modules whose behaviour defines this contract; a plan binds their hashes.
BOUND_MODULES = (
    "scripts/kg/stage2_parentage_contract.py",
    "scripts/db/sync_prod.py",
    "scripts/entities/schema_parity.py",
)

#: Foreign keys the sync order must respect (dependant must come after target).
REQUIRED_FK_ORDER = (
    ("jurisdictions", PARENTAGE_TABLE),
    ("public_bodies", PARENTAGE_TABLE),
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ParentageColumn:
    """One column parentage depends on, with the shape it must have."""

    table: str
    column: str
    type_family: str
    nullable: bool
    fk_table: str | None = None
    fk_column: str | None = None

    def key(self) -> str:
        """``table.column`` dotted name."""
        return f"{self.table}.{self.column}"


#: Declared readiness: ``public_body_id`` is the S1 write target; the existing
#: ``jurisdiction_id`` must keep being carried alongside it.
PARENTAGE_COLUMNS: Mapping[str, tuple[ParentageColumn, ...]] = {
    PARENTAGE_TABLE: (
        ParentageColumn(PARENTAGE_TABLE, "public_body_id", "INTEGER", True,
                        "public_bodies", "id"),
        ParentageColumn(PARENTAGE_TABLE, "jurisdiction_id", "INTEGER", True,
                        "jurisdictions", "id"),
    ),
}


def type_family(sql_type: Any) -> str:
    """Normalize a dialect type string to a coarse family.

    The family is deliberately coarse: dev is PostgreSQL and prod is SQLite in
    older deployments, so exact type text is not comparable, but ``INTEGER``
    versus ``TEXT`` is.
    """
    text = str(sql_type).upper()
    if "INT" in text:
        return "INTEGER"
    if any(token in text for token in ("CHAR", "TEXT", "CLOB", "STRING", "UUID")):
        return "TEXT"
    if any(token in text for token in ("TIMESTAMP", "DATETIME", "DATE", "TIME")):
        return "TEMPORAL"
    if "BOOL" in text:
        return "BOOLEAN"
    if any(token in text for token in ("NUMERIC", "DECIMAL", "FLOAT", "REAL", "DOUBLE")):
        return "NUMERIC"
    return "OTHER"


def contracted_columns(table: str | None = None) -> tuple[ParentageColumn, ...]:
    """Every contracted column, optionally narrowed to one table."""
    if table is None:
        return tuple(c for cols in PARENTAGE_COLUMNS.values() for c in cols)
    return tuple(PARENTAGE_COLUMNS.get(table, ()))


def column_problems(signature: Mapping[str, Any]) -> list[str]:
    """Presence/type/nullability problems against a schema signature.

    ``signature`` is the shape produced by
    :func:`scripts.entities.schema_parity.schema_signature`, so the parity report
    and this contract examine the same structure.
    """
    problems: list[str] = []
    for column in contracted_columns():
        entry = signature.get(column.table) or {}
        if entry.get("missing"):
            problems.append(f"missing table: {column.table}")
            continue
        found = {c["name"]: c for c in entry.get("columns", [])}
        described = found.get(column.column)
        if described is None:
            problems.append(f"missing column: {column.key()}")
            continue
        actual_family = type_family(described.get("type"))
        if actual_family != column.type_family:
            problems.append(
                f"type drift: {column.key()} is {actual_family}, "
                f"expected {column.type_family}"
            )
        if bool(described.get("nullable")) != column.nullable:
            problems.append(
                f"nullability drift: {column.key()} nullable="
                f"{bool(described.get('nullable'))}, expected {column.nullable}"
            )
    return problems


def fk_problems(signature: Mapping[str, Any]) -> list[str]:
    """Declared foreign keys that are absent from the signature."""
    problems: list[str] = []
    for column in contracted_columns():
        if not column.fk_table:
            continue
        entry = signature.get(column.table) or {}
        declared = {
            (tuple(f[0]), f[1], tuple(f[2]))
            for f in entry.get("foreign_keys", [])
        }
        wanted = ((column.column,), column.fk_table, (column.fk_column,))
        if wanted not in declared:
            problems.append(
                f"missing foreign key: {column.key()} -> "
                f"{column.fk_table}.{column.fk_column}"
            )
    return problems


def sync_payload_columns(
    dev_columns: Iterable[str], prod_columns: Iterable[str], table: str | None = None
) -> list[str]:
    """Columns the sync would carry, mirroring its intersection rule.

    Contracted columns are always included in the reported intersection even if
    they are absent from one side, so the caller can see that they were dropped
    instead of the omission being invisible.
    """
    dev = {str(c) for c in dev_columns}
    prod = {str(c) for c in prod_columns}
    carried = dev & prod
    for column in contracted_columns(table):
        carried.add(column.column)
    return sorted(carried)


def sync_readiness(
    dev_columns: Iterable[str], prod_columns: Iterable[str], table: str = PARENTAGE_TABLE
) -> dict[str, Any]:
    """Compare contracted columns across the two sides of a sync."""
    dev = {str(c) for c in dev_columns}
    prod = {str(c) for c in prod_columns}
    contracted = contracted_columns(table)
    carried = sorted(
        c.column for c in contracted if c.column in dev and c.column in prod
    )
    return {
        "table": table,
        "contracted": [c.column for c in contracted],
        "carried": carried,
        "missing_in_prod": sorted(c.column for c in contracted if c.column not in prod),
        "missing_in_dev": sorted(c.column for c in contracted if c.column not in dev),
        "dropped_by_intersection": sorted(
            c.column for c in contracted if c.column in dev and c.column not in prod
        ),
    }


def readiness_problems(readiness: Mapping[str, Any]) -> list[str]:
    """Problems in a :func:`sync_readiness` result."""
    problems: list[str] = []
    table = readiness.get("table")
    for column in readiness.get("missing_in_prod", ()):
        problems.append(f"sync cannot carry {table}.{column}: missing on prod")
    for column in readiness.get("missing_in_dev", ()):
        problems.append(f"contracted column {table}.{column} missing on dev")
    for column in readiness.get("dropped_by_intersection", ()):
        problems.append(
            f"sync would silently drop {table}.{column} (present on dev, absent on prod)"
        )
    return problems


def fk_order_problems(order: Sequence[str]) -> list[str]:
    """Dependant tables that would be synced before their parent."""
    problems: list[str] = []
    position = {name: index for index, name in enumerate(order)}
    for parent, dependant in REQUIRED_FK_ORDER:
        if parent not in position or dependant not in position:
            problems.append(f"sync order is missing {parent} or {dependant}")
            continue
        if position[parent] > position[dependant]:
            problems.append(
                f"sync order puts {dependant} before {parent}, "
                f"so its parent key may not exist yet"
            )
    return problems


def contract_snapshot() -> dict[str, Any]:
    """A serialisable copy of the whole declaration."""
    return {
        "parentage_table": PARENTAGE_TABLE,
        "columns": {
            column.key(): {
                "table": column.table,
                "column": column.column,
                "type_family": column.type_family,
                "nullable": column.nullable,
                "fk_table": column.fk_table,
                "fk_column": column.fk_column,
            }
            for column in contracted_columns()
        },
        "required_fk_order": [list(pair) for pair in REQUIRED_FK_ORDER],
        "bound_modules": list(BOUND_MODULES),
    }


def code_hashes(root: Path | str | None = None) -> dict[str, str]:
    """SHA-256 of every module bound to this contract."""
    base = Path(root) if root is not None else _REPO_ROOT
    return {
        relative: hashlib.sha256((base / relative).read_bytes()).hexdigest()
        for relative in BOUND_MODULES
    }


def readiness_digest(
    snapshot: Mapping[str, Any] | None = None, hashes: Mapping[str, str] | None = None
) -> str:
    """Digest over the readiness declaration plus the bound module hashes."""
    payload = {
        "contract": dict(snapshot if snapshot is not None else contract_snapshot()),
        "code_hashes": dict(hashes if hashes is not None else code_hashes()),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
