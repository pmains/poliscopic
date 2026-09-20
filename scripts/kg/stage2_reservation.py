#!/usr/bin/env python3
"""``stage2_reservation.py`` — the additive exact-key reservation contract.

Stage 2 needs one guarantee: **two writers must not create the same new
``(meeting_db_id, agenda_item_number)``**.  The historical data cannot give that
guarantee — 4,673 excess rows sit on the key — so the guarantee is moved to a new,
additive table that describes only the keys an apply would *create*.

The table is deliberately narrow:

* ``(meeting_db_id, agenda_item_number)`` is the **primary key**, which is what
  makes a duplicate insert impossible rather than unlikely;
* ``plan_digest`` binds every reservation to the reviewed plan that made it, so a
  replay is identifiable and a foreign apply cannot reserve keys for itself;
* ``reserved_at`` and ``reserved_by`` are provenance: who reserved it and when.
  They are justified because the reservation outlives the transaction that made it
  and must be attributable during a later audit or rollback.

Two things it deliberately does NOT do:

* it carries **no foreign key to ``agenda_items``**.  A reservation is made before
  the item exists, so an FK there would be unsatisfiable.  The consequence is
  recorded rather than hidden: deleting an item does not remove its reservation, so
  a cleanup must clear reservations explicitly;
* it does **not** make the historical data unique, and does not claim to.

Concurrency is a combination, and this module says which part carries what:

* the **primary key** carries the invariant;
* ``pg_advisory_xact_lock`` on the key serializes writers so the occupancy read and
  the insert cannot interleave — it enforces nothing on its own;
* the transaction owner's **SERIALIZABLE + whole-unit retry** is the retry path.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ADVISORY_NAMESPACE",
    "COLUMNS",
    "DDL",
    "FK_NAME",
    "PK_NAME",
    "RESERVATION_TABLE",
    "RESERVATION_VERSION",
    "existing_reservations",
    "lock_key",
    "reservation_ddl",
    "reservation_key",
    "read_reservation_signature",
    "reserve_keys",
    "verify_reservation_contract",
]

RESERVATION_VERSION = "kg-stage2-reservation/1.0"
RESERVATION_TABLE = "agenda_item_key_reservation"
PK_NAME = f"{RESERVATION_TABLE}_pkey"
FK_NAME = f"{RESERVATION_TABLE}_meeting_fkey"

#: The advisory-lock namespace, so Stage 2 locks cannot collide with another
#: subsystem that happens to hash the same key text.
ADVISORY_NAMESPACE = "kg-stage2-natural-key"

#: The columns, with the exact type of its counterpart in ``agenda_items``.  The
#: number column is ``varchar(32)`` because ``agenda_items.agenda_item_number`` is:
#: a narrower reservation column could silently refuse a key the item would accept.
COLUMNS = (
    {"name": "meeting_db_id", "type": "integer", "nullable": False,
     "references": {"table": "meetings", "column": "id",
                    "on_delete": "CASCADE", "on_update": "NO ACTION"}},
    {"name": "agenda_item_number", "type": "character varying(32)", "nullable": False,
     "collation": "database default (matches agenda_items.agenda_item_number)"},
    {"name": "plan_digest", "type": "character varying(64)", "nullable": False},
    {"name": "reserved_at", "type": "timestamp with time zone", "nullable": False,
     "default": "now()"},
    {"name": "reserved_by", "type": "text", "nullable": False, "default": "''"},
)

#: No column-level collation is declared: the reservation must compare its key the
#: same way ``agenda_items`` does, and that column uses the database default.  A
#: different collation here would let two keys be equal for the item and distinct
#: for the reservation (or the reverse), which is exactly the gap this table exists
#: to close.
DDL = (
    f"CREATE TABLE {RESERVATION_TABLE} ("
    f"meeting_db_id integer NOT NULL, "
    f"agenda_item_number character varying(32) NOT NULL, "
    f"plan_digest character varying(64) NOT NULL, "
    f"reserved_at timestamp with time zone NOT NULL DEFAULT now(), "
    f"reserved_by text NOT NULL DEFAULT '', "
    f"CONSTRAINT {PK_NAME} PRIMARY KEY (meeting_db_id, agenda_item_number), "
    f"CONSTRAINT {FK_NAME} FOREIGN KEY (meeting_db_id) REFERENCES meetings(id) "
    f"ON DELETE CASCADE ON UPDATE NO ACTION"
    f")",
)


def reservation_ddl() -> list[str]:
    """The complete additive change: one table, nothing else touched."""
    return list(DDL)


def reservation_key(meeting_db_id: int, agenda_item_number: str) -> str:
    return f"{int(meeting_db_id)}|{agenda_item_number}"


def lock_key(connection: Any, meeting_db_id: int, agenda_item_number: str) -> None:
    """Take the per-key advisory lock for the rest of this transaction.

    ``pg_advisory_xact_lock`` releases at commit or rollback, so a crash cannot
    strand a lock.  A no-op on SQLite, which serializes writers itself.
    """
    if connection.dialect.name != "postgresql":
        return
    from sqlalchemy import text

    connection.execute(text(
        "SELECT pg_advisory_xact_lock(hashtext(:ns), hashtext(:key))"),
        {"ns": ADVISORY_NAMESPACE, "key": reservation_key(meeting_db_id,
                                                          agenda_item_number)})


def existing_reservations(connection: Any,
                          keys: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Read the reservations already recorded for the requested keys."""
    if not keys:
        return {}
    from sqlalchemy import text

    found: dict[str, Any] = {}
    for entry in keys:
        row = connection.execute(text(
            f"SELECT meeting_db_id, agenda_item_number, plan_digest, reserved_at, "
            f"reserved_by FROM {RESERVATION_TABLE} "
            f"WHERE meeting_db_id = :m AND agenda_item_number = :n"),
            {"m": int(entry["meeting_db_id"]),
             "n": str(entry["agenda_item_number"])}).mappings().first()
        if row is not None:
            found[reservation_key(row["meeting_db_id"], row["agenda_item_number"])] = \
                {k: row[k] for k in ("meeting_db_id", "agenda_item_number",
                                     "plan_digest", "reserved_at", "reserved_by")}
    return found


def reserve_keys(connection: Any, keys: Iterable[Mapping[str, Any]], *,
                 plan_digest: str, reserved_by: str) -> dict[str, Any]:
    """Reserve the keys for this plan.  The caller owns the transaction.

    Every key is locked before it is inserted, so two writers on the same key are
    serialized here and then decided by the primary key.  An existing reservation is
    **not** overwritten: it is returned as held, because a key already reserved by a
    different plan is a decision for a person.
    """
    from sqlalchemy import text

    if not plan_digest:
        raise ValueError("a reservation must be bound to a plan digest")
    if not str(reserved_by or "").strip():
        raise ValueError("a reservation must name who authorized it")
    entries = sorted(keys, key=lambda e: reservation_key(e["meeting_db_id"],
                                                         e["agenda_item_number"]))
    for entry in entries:
        lock_key(connection, int(entry["meeting_db_id"]),
                 str(entry["agenda_item_number"]))
    already = existing_reservations(connection, entries)
    inserted: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for entry in entries:
        key = reservation_key(entry["meeting_db_id"], entry["agenda_item_number"])
        record = {**{k: entry[k] for k in ("meeting_db_id", "agenda_item_number")},
                  "key": key}
        if key in already:
            held.append({**record, "reason": "already reserved",
                         "by_plan": already[key]["plan_digest"]})
            continue
        connection.execute(text(
            f"INSERT INTO {RESERVATION_TABLE} (meeting_db_id, agenda_item_number, "
            f"plan_digest, reserved_by) VALUES (:m, :n, :d, :b)"),
            {"m": int(entry["meeting_db_id"]),
             "n": str(entry["agenda_item_number"]),
             "d": plan_digest, "b": reserved_by})
        inserted.append({**record, "plan_digest": plan_digest})
    return {"plan_digest": plan_digest, "inserted": inserted, "held": held,
            "invariant_carrier": "primary key on (meeting_db_id, agenda_item_number)"}


def read_reservation_signature(connection: Any) -> dict[str, Any] | None:
    """The table as it actually is, or ``None`` when it does not exist."""
    from sqlalchemy import text

    exists = connection.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :t"),
        {"t": RESERVATION_TABLE}).scalar()
    if not exists:
        return None
    columns = [{"name": r["column_name"], "type": r["data_type"],
                "length": r["character_maximum_length"],
                "nullable": r["is_nullable"] == "YES",
                "collation": r["collation_name"]}
               for r in connection.execute(text(
                   "SELECT column_name, data_type, character_maximum_length, "
                   "is_nullable, collation_name FROM information_schema.columns "
                   "WHERE table_name = :t ORDER BY ordinal_position"),
                   {"t": RESERVATION_TABLE}).mappings()]
    primary = connection.execute(text("""
        SELECT a.attname FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'p' AND c.conrelid = to_regclass(:t) ORDER BY k.ord"""),
        {"t": RESERVATION_TABLE}).scalars().all()
    foreign = [{"columns": ["meeting_db_id"], "references_table": r["referred"],
                "references_columns": [r["col"]],
                "on_delete": r["del"], "on_update": r["upd"],
                "validated": r["validated"]}
               for r in connection.execute(text("""
                   SELECT rt.relname AS referred, a.attname AS col,
                          c.confdeltype AS del, c.confupdtype AS upd,
                          c.convalidated AS validated
                   FROM pg_constraint c
                   JOIN pg_class t ON t.oid = c.conrelid
                   JOIN pg_class rt ON rt.oid = c.confrelid
                   JOIN unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) ON true
                   JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
                   WHERE c.contype = 'f' AND t.relname = :t
                     AND t.relnamespace = (SELECT oid FROM pg_namespace
                                           WHERE nspname = ANY (current_schemas(false)))
                   """),
                   {"t": RESERVATION_TABLE}).mappings()]
    return {"table": RESERVATION_TABLE, "columns": columns,
            "primary_key": list(primary), "foreign_keys": foreign}


#: ``confdeltype`` codes, so the delete behaviour is checked by value and not by
#: the presence of "CASCADE" somewhere in a string.
_CONF_DELETE = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
                "n": "SET NULL", "d": "SET DEFAULT"}
_CONF_UPDATE = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
                "n": "SET NULL", "d": "SET DEFAULT"}


def verify_reservation_contract(connection: Any) -> list[str]:
    """The reservation table must be exactly the contract, or it proves nothing."""
    from sqlalchemy import text

    signature = read_reservation_signature(connection)
    if signature is None:
        return [f"{RESERVATION_TABLE} does not exist"]
    problems: list[str] = []
    if signature["primary_key"] != ["meeting_db_id", "agenda_item_number"]:
        problems.append(f"the primary key is {signature['primary_key']}, not "
                        f"['meeting_db_id', 'agenda_item_number']")
    by_name = {c["name"]: c for c in signature["columns"]}
    expected = {"meeting_db_id": ("integer", None),
                "agenda_item_number": ("character varying", 32),
                "plan_digest": ("character varying", 64)}
    for name, (data_type, length) in expected.items():
        column = by_name.get(name)
        if column is None:
            problems.append(f"column {name!r} is missing")
            continue
        if column["type"] != data_type:
            problems.append(f"{name}: type {column['type']!r} is not {data_type!r}")
        if length is not None and column["length"] != length:
            problems.append(f"{name}: length {column['length']} is not {length}")
        if column["nullable"]:
            problems.append(f"{name} is nullable but must not be")
    for name in ("reserved_at", "reserved_by"):
        column = by_name.get(name)
        if column is None:
            problems.append(f"column {name!r} is missing")
        elif column["nullable"]:
            problems.append(f"{name} is nullable but must not be")
    # The key column must compare the way agenda_items compares it.
    key_column = by_name.get("agenda_item_number")
    if key_column is not None:
        item_collation = connection.execute(text(
            "SELECT collation_name FROM information_schema.columns "
            "WHERE table_name = 'agenda_items' AND column_name = 'agenda_item_number'"
        )).scalar()
        if key_column["collation"] != item_collation:
            problems.append(
                f"the key collation {key_column['collation']!r} is not the agenda_items "
                f"collation {item_collation!r}")
    if not signature["foreign_keys"]:
        problems.append("the meeting foreign key is missing")
    for foreign in signature["foreign_keys"]:
        if foreign["references_table"] != "meetings":
            problems.append(f"the foreign key targets {foreign['references_table']!r}")
        if foreign["columns"] != ["meeting_db_id"]:
            problems.append(f"the foreign key covers {foreign['columns']}")
        if not foreign["validated"]:
            problems.append("the foreign key is not validated")
        if _CONF_DELETE.get(str(foreign["on_delete"])) != "CASCADE":
            problems.append(f"the foreign key ON DELETE is {foreign['on_delete']!r}")
    return problems
