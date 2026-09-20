#!/usr/bin/env python3
"""``stage2_link_column.py`` — the canonical link-column contract.

Stage 2 needs one new link: a document must be able to point at a specific
``agenda_items`` row.  The existing ``agenda_item_id`` column cannot carry it — it is
a *source-system* key (values such as ``"0"``, ``"bos-20"``,
``"result-phoenix-cc-publicmeeting-…"``), not a database identity.  Substituting it
would conflate "the vendor's item label" with "the row this document belongs to",
which is exactly the confusion the plan's own classification had to undo.  So the
link is a new, additive, nullable column with a validated foreign key.

The contract, and why each part is what it is:

* **nullable** — the 65,510 documents already in the table have no row link, and the
  scraper must keep loading documents that have none;
* **``integer``** — the exact type of ``agenda_items.id``, so the foreign key is
  type-correct and an index over this column is comparable to the referenced key;
* **``ON DELETE SET NULL``** — deleting an agenda item *unlinks* its documents rather
  than deleting them or blocking the delete.  This is also what makes the
  receipt-owned rollback work: it deletes exactly the rows an apply created, and the
  documents survive with the link cleared;
* **``ON UPDATE NO ACTION``** — the referenced key is an identity and never changes;
* **one index** — justified by the foreign key's own access path.  PostgreSQL does not
  index a referencing column automatically, so without it every
  ``DELETE FROM agenda_items`` sequentially scans ``supporting_documents`` and every
  "which documents belong to this item" lookup does too.

This module reads and describes.  It has no write path; the DDL is applied by
:mod:`stage2_link_column_apply` from an approved plan.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "COLUMN_NAME",
    "COLUMN_TYPE",
    "CONSTRAINT_NAME",
    "INDEX_NAME",
    "LINK_TABLE",
    "ON_DELETE",
    "ON_UPDATE",
    "REFERENCES_COLUMN",
    "REFERENCES_TABLE",
    "ddl",
    "index_justification",
    "parity_contract",
    "read_signature",
    "verify_contract",
    "verify_parity",
]

LINK_TABLE = "supporting_documents"
COLUMN_NAME = "agenda_item_db_id"
COLUMN_TYPE = "integer"
NULLABLE = True

REFERENCES_TABLE = "agenda_items"
REFERENCES_COLUMN = "id"
ON_DELETE = "SET NULL"
ON_UPDATE = "CASCADE"

#: The ORM already declares this constraint by this exact name, so the live
#: schema must use it too: parity is a name-level contract, not a convention.
CONSTRAINT_NAME = f"{LINK_TABLE}_{COLUMN_NAME}_fkey"
INDEX_NAME = f"ix_{LINK_TABLE}_{COLUMN_NAME}"

#: ``confdeltype`` / ``confupdtype`` codes, so behaviour is checked by value rather
#: than by finding a substring in a printed definition.
_CONF_ACTION = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
                "n": "SET NULL", "d": "SET DEFAULT"}


def ddl() -> list[str]:
    """The complete additive change: one column, one validated FK, one index.

    Nothing else is touched.  The three statements are ordered so the constraint and
    the index can only exist on a column that exists.
    """
    return [
        f"ALTER TABLE {LINK_TABLE} ADD COLUMN {COLUMN_NAME} {COLUMN_TYPE} NULL",
        f"ALTER TABLE {LINK_TABLE} ADD CONSTRAINT {CONSTRAINT_NAME} "
        f"FOREIGN KEY ({COLUMN_NAME}) REFERENCES {REFERENCES_TABLE}({REFERENCES_COLUMN}) "
        f"ON DELETE {ON_DELETE} ON UPDATE {ON_UPDATE}",
        f"CREATE INDEX {INDEX_NAME} ON {LINK_TABLE} ({COLUMN_NAME})",
    ]


def index_justification() -> dict[str, Any]:
    """The access paths that make the index worth carrying."""
    return {
        "index": INDEX_NAME,
        "columns": [COLUMN_NAME],
        "unique": False,
        "access_paths": [
            f"DELETE FROM {REFERENCES_TABLE}: PostgreSQL does not index a referencing "
            f"column, so the {ON_DELETE} action would sequentially scan "
            f"{LINK_TABLE} (65,510 rows) for every deleted item",
            f"attachment lookup: SELECT … FROM {LINK_TABLE} WHERE {COLUMN_NAME} = ?",
            f"receipt verification: re-reading the documents an apply linked",
        ],
        "not_unique_because": "many documents may belong to one item",
    }


def parity_contract() -> dict[str, Any]:
    """What the ORM declares, so the live column cannot drift from the model.

    The model already carries a STAGED declaration of this column — the attribute
    exists before the database column does, wrapped in ``deferred`` so an ordinary
    query never selects it.  This reads that declaration back so the DDL, the plan
    and the model all name the same constraint.
    """
    from scripts.db import models as db_models

    table = db_models.SupportingDocument.__table__
    column = table.columns[COLUMN_NAME]
    constraint = None
    for foreign in table.foreign_key_constraints:
        if COLUMN_NAME in [c.name for c in foreign.columns]:
            constraint = foreign
    if constraint is None:
        raise ValueError("the model declares no foreign key for " + COLUMN_NAME)
    target = list(constraint.elements)[0].target_fullname
    return {
        "table": LINK_TABLE,
        "column": COLUMN_NAME,
        "constraint": constraint.name,
        "references": target,
        "on_delete": str(constraint.ondelete or "").upper(),
        "on_update": str(constraint.onupdate or "").upper(),
        "nullable": bool(column.nullable),
        "indexed": bool(column.index),
        "staged": "deferred" in repr(column.expression)
        if hasattr(column, "expression") else None,
    }


def verify_parity() -> list[str]:
    """The contract's names must be the model's names, or parity is a claim only."""
    problems: list[str] = []
    declared = parity_contract()
    if declared["constraint"] != CONSTRAINT_NAME:
        problems.append(f"the model names the constraint {declared['constraint']!r}, "
                        f"the contract {CONSTRAINT_NAME!r}")
    if declared["on_delete"] != ON_DELETE:
        problems.append(f"the model ON DELETE is {declared['on_delete']!r}, "
                        f"the contract {ON_DELETE!r}")
    if declared["on_update"] != ON_UPDATE:
        problems.append(f"the model ON UPDATE is {declared['on_update']!r}, "
                        f"the contract {ON_UPDATE!r}")
    if declared["references"] != f"{REFERENCES_TABLE}.{REFERENCES_COLUMN}":
        problems.append(f"the model references {declared['references']!r}")
    if declared["nullable"] is not True:
        problems.append("the model column must be nullable")
    return problems


def read_signature(connection: Any) -> dict[str, Any] | None:
    """The column, its foreign key and its index as they actually are.

    ``None`` when the column does not exist.  Reads only.
    """
    if connection.dialect.name != "postgresql":
        raise ValueError("the link column contract is defined for PostgreSQL only")
    from sqlalchemy import text

    column = connection.execute(text("""
        SELECT data_type, is_nullable, character_maximum_length
        FROM information_schema.columns
        WHERE table_name = :t AND column_name = :c"""),
        {"t": LINK_TABLE, "c": COLUMN_NAME}).mappings().first()
    if column is None:
        return None

    foreign = connection.execute(text("""
        SELECT c.conname, rt.relname AS referred_table, a.attname AS referred_column,
               c.confdeltype AS on_delete, c.confupdtype AS on_update,
               c.convalidated AS validated
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_class rt ON rt.oid = c.confrelid
        JOIN unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
        WHERE c.contype = 'f' AND t.relname = :t
          AND c.conrelid = CAST(:t AS regclass)"""), {"t": LINK_TABLE}).mappings().all()
    mine = [dict(f) for f in foreign if f["conname"] == CONSTRAINT_NAME]

    indexes = connection.execute(text("""
        SELECT indexname, indexdef FROM pg_indexes
        WHERE tablename = :t AND indexname = :i"""),
        {"t": LINK_TABLE, "i": INDEX_NAME}).mappings().all()
    return {
        "table": LINK_TABLE,
        "column": COLUMN_NAME,
        "data_type": column["data_type"],
        "nullable": column["is_nullable"] == "YES",
        "constraint": CONSTRAINT_NAME,
        "foreign_keys": mine,
        "on_delete": _CONF_ACTION.get(
            str(mine[0]["on_delete"])) if mine else None,
        "on_update": _CONF_ACTION.get(
            str(mine[0]["on_update"])) if mine else None,
        "indexes": [dict(i) for i in indexes],
        "present": True,
    }


def verify_contract(connection: Any) -> list[str]:
    """The link column must be exactly the contract, or it proves nothing."""
    signature = read_signature(connection)
    if signature is None:
        return [f"{LINK_TABLE}.{COLUMN_NAME} does not exist"]
    problems: list[str] = []
    if signature["data_type"] != COLUMN_TYPE:
        problems.append(f"the column type is {signature['data_type']!r}, "
                        f"not {COLUMN_TYPE!r}")
    if signature["nullable"] is not True:
        problems.append("the column must be nullable: existing documents have no link")
    if not signature["foreign_keys"]:
        problems.append(f"the foreign key {CONSTRAINT_NAME} is missing")
    else:
        foreign = signature["foreign_keys"][0]
        if foreign["referred_table"] != REFERENCES_TABLE:
            problems.append(f"the foreign key targets {foreign['referred_table']!r}")
        if foreign["referred_column"] != REFERENCES_COLUMN:
            problems.append(f"the foreign key targets {foreign['referred_column']!r}")
        if not foreign["validated"]:
            problems.append("the foreign key is not validated")
        if signature["on_delete"] != ON_DELETE:
            problems.append(f"ON DELETE is {signature['on_delete']!r}, not {ON_DELETE!r}")
        if signature["on_update"] != ON_UPDATE:
            problems.append(f"ON UPDATE is {signature['on_update']!r}, not {ON_UPDATE!r}")
    if not signature["indexes"]:
        problems.append(f"the index {INDEX_NAME} is missing")
    return problems
