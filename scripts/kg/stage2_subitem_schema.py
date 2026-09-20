#!/usr/bin/env python3
"""``stage2_subitem_schema.py`` — the schema-readiness contract for containment.

**Design only.**  Nothing here creates a column, an index or a constraint, and the
plan that references this contract has no write path.  ``agenda_items.parent_item_id``
does not exist in the database today.

The contract states what an additive storage for ``PART_OF`` must look like, so
that the eventual migration is a reviewed object rather than an improvised DDL.
It keeps ``PART_OF`` (item contains item) strictly separate from ``ATTACHED_TO``
(document attaches to item): the storage proposed here carries no document ids.

Two things the contract is careful about, because getting either wrong would make
the plan look stronger than it is:

* **the current schema is bound exactly.**  :func:`read_schema_signature` captures
  a *focused* signature of ``agenda_items`` — every column with its type,
  nullability and default, the primary key, the unique and non-unique indexes, and
  the foreign keys — plus the exact target identity, and the whole thing is
  digested.  The validator compares that against a supplied authoritative
  (locked/current) signature for **exact equality**, so a plan built against a
  different schema is refused rather than applied.
* **enforcement is described honestly.**  Only the self-reference rule is
  expressible as an ordinary PostgreSQL ``CHECK``.  The same-meeting and
  number-shortening rules are **not** ``CHECK`` constraints — a ``CHECK`` may not
  contain a subquery and cannot read another row — so they are recorded as
  transactional preconditions or trigger-enforced.  :data:`ENFORCEMENT` states
  this per rule so the claim cannot be read as stronger than it is.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

__all__ = [
    "ENFORCEMENT",
    "SCHEMA_KIND",
    "SCHEMA_VERSION",
    "TABLE",
    "build_contract",
    "canonical_sha256",
    "migration_signature",
    "read_schema_signature",
    "target_identity",
    "validate_contract",
]

SCHEMA_KIND = "kg-stage2-subitem-schema-readiness"
SCHEMA_VERSION = "kg-stage2-subitem-schema-readiness/2.0"

#: The one table this contract is about.
TABLE = "agenda_items"

#: The additive change this contract describes.  Nullable, so every existing row
#: is valid as-is and nothing has to be guessed during the migration.
COLUMN = {
    "table": TABLE,
    "name": "parent_item_id",
    "type": "INTEGER",
    "nullable": True,
    "default": None,
    "references": {"table": TABLE, "column": "id",
                   "on_delete": "RESTRICT", "deferrable": "INITIALLY DEFERRED"},
    "additive": True,
    "backfill_required": False,
}

#: Direction: the CHILD holds the pointer, so a self-reference is detectable and
#: a child has at most one parent.
DIRECTION = {
    "holder": "child",
    "points_at": "parent",
    "relation": "PART_OF",
    "cardinality": "many children to one parent",
    "attached_to_is_separate": True,
}

INDEX = {
    "name": "ix_agenda_items_parent_item_id",
    "columns": ["parent_item_id"],
    "unique": False,
    "reason": "reverse lookup of a parent's children",
}

#: Domain rules a later apply must enforce, and — per rule — the mechanism that
#: can actually enforce them.  Listed here so the migration and the plan cannot
#: disagree, and so no one reads a rule as if a CHECK were doing the work.
ENFORCEMENT = {
    "self_reference": {
        "rule": "parent_item_id IS NULL OR parent_item_id <> id",
        "mechanism": "ordinary CHECK constraint",
        "expressible_as_check": True,
        "why": "it reads only the row's own columns, which a CHECK constraint may do",
    },
    "same_meeting": {
        "rule": "the parent row must belong to the child's meeting",
        "mechanism": "transactional precondition or trigger",
        "expressible_as_check": False,
        "why": "a PostgreSQL CHECK constraint may not contain a subquery, so it "
               "cannot read agenda_items at parent_item_id; enforced as a locked "
               "re-read of the parent row inside the applying transaction, or by a "
               "trigger",
    },
    "number_shortening": {
        "rule": "the child's number must be strictly longer than its parent's",
        "mechanism": "transactional precondition or trigger",
        "expressible_as_check": False,
        "why": "the rule compares the child's agenda_item_number with the parent "
               "row's, and a CHECK constraint cannot read another row; enforced as a "
               "transactional precondition, or by a trigger",
    },
    "no_cycles": {
        "rule": "the parent chain is strictly shortening, so it cannot cycle",
        "mechanism": "transactional precondition",
        "expressible_as_check": False,
        "why": "it is a closure property over the whole parent chain; the shortening "
               "precondition already implies it, because a strictly shorter number "
               "can never return to its own ancestor",
    },
    "statement": "ONLY the self-reference rule is expressible as an ordinary "
                 "PostgreSQL CHECK constraint. The same-meeting, number-shortening "
                 "and acyclicity rules are NOT CHECK constraints: they are enforced "
                 "as transactional preconditions or by triggers.",
}

#: Domain rules a later apply must enforce, in prose order.
DOMAIN_RULES = (
    ENFORCEMENT["self_reference"]["rule"],
    ENFORCEMENT["same_meeting"]["rule"],
    ENFORCEMENT["number_shortening"]["rule"],
    ENFORCEMENT["no_cycles"]["rule"],
)

#: The target fields that identify exactly where the plan would act.
TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


def target_identity(target: Mapping[str, Any]) -> dict[str, Any]:
    """The exact target this contract describes, digested.

    Five fields rather than "the database we happened to connect to": the tier is
    part of the identity, so a plan built for development cannot be read as
    describing production.
    """
    body = {field: target.get(field) for field in TARGET_FIELDS}
    return {**body, "digest": canonical_sha256(body)}


def read_schema_signature(connection: Any, table: str = TABLE) -> dict[str, Any]:
    """A focused, canonical signature of one table as it actually is.

    Columns (name, type, nullability, default), the primary key, every index with
    its uniqueness, and every foreign key with its referential actions.  Digested,
    so two schemas can be compared for **exact equality** rather than by eyeball.
    """
    from sqlalchemy import inspect as _inspect

    inspector = _inspect(connection)
    columns = sorted(
        ({"name": str(c["name"]), "type": str(c["type"]),
          "nullable": bool(c.get("nullable")),
          "default": None if c.get("default") is None else str(c["default"])}
         for c in inspector.get_columns(table)),
        key=lambda c: c["name"])
    primary_key = sorted(
        str(n) for n in (inspector.get_pk_constraint(table) or {})
        .get("constrained_columns") or [])
    indexes = sorted(
        ({"name": str(i.get("name")), "unique": bool(i.get("unique")),
          "columns": [str(n) for n in (i.get("column_names") or [])]}
         for i in inspector.get_indexes(table) or []),
        key=lambda i: (i["name"] or "", i["columns"]))
    foreign_keys = sorted(
        ({"columns": [str(n) for n in (f.get("constrained_columns") or [])],
          "references_table": (None if f.get("referred_table") is None
                               else str(f["referred_table"])),
          "references_columns": [str(n) for n in (f.get("referred_columns") or [])],
          "on_delete": (f.get("options") or {}).get("ondelete"),
          "on_update": (f.get("options") or {}).get("onupdate")}
         for f in inspector.get_foreign_keys(table) or []),
        key=lambda f: (f["references_table"] or "", f["columns"]))
    body = {"table": table, "columns": columns, "primary_key": primary_key,
            "indexes": indexes, "foreign_keys": foreign_keys}
    return {**body, "digest": canonical_sha256(body)}


def migration_signature() -> str:
    """A stable signature over the proposed change, so a later apply can bind it."""
    body = {"column": COLUMN, "direction": DIRECTION, "index": INDEX,
            "domain_rules": list(DOMAIN_RULES), "enforcement": dict(ENFORCEMENT),
            "version": SCHEMA_VERSION}
    return canonical_sha256(body)


def build_contract(*, target: Mapping[str, Any],
                   schema_signature: Mapping[str, Any] | None = None,
                   column_present: bool = False,
                   agenda_items_schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Describe the readiness state of the containment storage."""
    return {
        "kind": SCHEMA_KIND,
        "version": SCHEMA_VERSION,
        "design_only": True,
        "implemented": False,
        "column": dict(COLUMN),
        "direction": dict(DIRECTION),
        "index": dict(INDEX),
        "domain_rules": list(DOMAIN_RULES),
        "enforcement": {k: (dict(v) if isinstance(v, Mapping) else v)
                        for k, v in ENFORCEMENT.items()},
        "migration_signature": migration_signature(),
        "target_identity": target_identity(target),
        "agenda_items_schema": (dict(agenda_items_schema)
                                if agenda_items_schema else None),
        "target_schema_signature": dict(schema_signature) if schema_signature else None,
        "observed": {"column_present": bool(column_present)},
        "prerequisites": {
            "backup": "a protected, restore-verified backup receipt bound into the plan",
            "apply": "one SERIALIZABLE transaction; the column is added and left NULL",
            "replay": "a second apply is a no-op: the column already exists, no backfill",
            "rollback": "drop the column and the index; all values were NULL so no data is lost",
            "ordering": "schema readiness precedes any containment apply; a plan may not "
                        "write parent_item_id before the column exists",
        },
        "separation": {
            "part_of": "item -> parent item, stored on agenda_items.parent_item_id",
            "attached_to": "document -> item, unchanged; no document column is added",
        },
    }


def validate_contract(contract: Mapping[str, Any], *,
                      authoritative_schema: Mapping[str, Any] | None = None,
                      authoritative_target: Mapping[str, Any] | None = None) -> list[str]:
    """Structural checks, plus exact equality against what the database says.

    When ``authoritative_schema`` is supplied — the locked/current signature read
    from the live connection — the bound signature must equal it **exactly**: same
    columns with the same types, nullability and defaults, the same primary key,
    the same indexes and the same foreign keys.  A plan that bound a schema the
    database does not have is refused, not applied.
    """
    problems: list[str] = []
    if contract.get("kind") != SCHEMA_KIND:
        problems.append(f"kind must be {SCHEMA_KIND!r}")
    if contract.get("version") != SCHEMA_VERSION:
        problems.append(f"version must be {SCHEMA_VERSION!r}")
    if contract.get("design_only") is not True or contract.get("implemented") is not False:
        problems.append("the schema contract must be design-only and unimplemented")
    if not contract.get("migration_signature"):
        problems.append("the schema contract carries no migration signature")
    if not (contract.get("prerequisites") or {}).get("rollback"):
        problems.append("the schema contract records no rollback prerequisite")

    bound_schema = contract.get("agenda_items_schema") or {}
    if not bound_schema:
        problems.append("the contract binds no agenda_items schema signature")
    else:
        body = {k: v for k, v in bound_schema.items() if k != "digest"}
        if bound_schema.get("digest") != canonical_sha256(body):
            problems.append("the bound agenda_items schema digest is not canonical")
        for field in ("columns", "primary_key", "indexes", "foreign_keys"):
            if field not in bound_schema:
                problems.append(f"the bound agenda_items schema omits {field!r}")

    identity = contract.get("target_identity") or {}
    if not identity.get("database"):
        problems.append("the contract binds no target identity")
    else:
        body = {k: v for k, v in identity.items() if k != "digest"}
        if identity.get("digest") != canonical_sha256(body):
            problems.append("the bound target identity digest is not canonical")

    enforcement = contract.get("enforcement") or {}
    for rule in ("self_reference", "same_meeting", "number_shortening", "no_cycles"):
        entry = enforcement.get(rule)
        if not isinstance(entry, Mapping):
            problems.append(f"the contract records no enforcement for {rule!r}")
            continue
        if rule != "self_reference" and entry.get("expressible_as_check") is not False:
            problems.append(
                f"{rule!r} must be recorded as NOT expressible as a PostgreSQL CHECK")
    if not enforcement.get("statement"):
        problems.append("the contract must state which rules are CHECK constraints")

    if authoritative_schema is not None:
        if dict(bound_schema) != dict(authoritative_schema):
            problems.append(
                "the bound agenda_items schema is not the authoritative current schema")
    if authoritative_target is not None:
        expected = target_identity(authoritative_target)
        if dict(identity) != expected:
            problems.append("the bound target identity is not the authoritative target")
    return problems
