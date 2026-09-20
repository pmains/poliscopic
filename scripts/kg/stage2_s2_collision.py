#!/usr/bin/env python3
"""``stage2_s2_collision.py`` — absent-key collision serialization.

``SELECT ... FOR UPDATE`` locks rows that exist.  The collision the Stage 2 plans
can cause is two writers inserting the **same absent key**,
``agenda_items(meeting_db_id, agenda_item_number)`` — and no row lock can stop a
row that is not there yet.  The enforceable answer is a unique index on the
natural key plus an isolation strategy that makes a duplicate insert fail rather
than race.

A unique index only *enforces* if it is genuinely immediate and genuinely on the
natural key, so the PostgreSQL proof checks every attribute that could hollow it
out:

* ``indisunique`` — it is a unique index at all;
* ``indisvalid`` / ``indisready`` — it is built and usable (an index still being
  built enforces nothing);
* ``indimmediate`` — uniqueness is enforced **at statement time**.  A deferred
  unique constraint reports ``indimmediate = false`` and would let two colliding
  inserts into the same transaction;
* ``indpred IS NULL`` — it is not partial, so it covers every row;
* ``indexprs IS NULL`` — it indexes columns, not expressions;
* ``indnatts == indnkeyatts`` — no ``INCLUDE`` columns padding the attribute list;
* exactly the two natural-key columns, resolved through ``pg_attribute`` by the
  indexed **relation OID**, never by a name lookup that could land on a different
  schema's relation with a colliding index name.
"""

from __future__ import annotations

from typing import Any

__all__ = ["NATURAL_KEY", "CollisionRefused", "collision_contract",
           "governed_contract", "index_proof_requirements", "verify_collision_control",
           "verify_unique_index"]

NATURAL_KEY = ("meeting_db_id", "agenda_item_number")


class CollisionRefused(RuntimeError):
    """The natural key cannot be protected; the plan may not proceed."""


#: Every attribute the PostgreSQL proof demands, and why it matters.  Published so
#: a test can assert the proof is complete rather than assuming it.
PG_PROOF_REQUIREMENTS = {
    "indisunique": "must be a unique index",
    "indisvalid": "a half-built index enforces nothing",
    "indisready": "an index not ready for inserts enforces nothing",
    "indimmediate": "a deferred unique constraint would admit two colliding inserts",
    "not_partial": "a partial index leaves rows unprotected",
    "not_expression": "an expression index does not key the natural-key columns",
    "no_include": "INCLUDE columns would pad indnatts past indnkeyatts",
    "exact_attrs": "the key must be exactly the natural-key columns, by relation OID",
}

_PG_INDEX_QUERY = """
    SELECT x.indexrelid::regclass::text AS index_name,
           x.indrelid AS table_oid,
           x.indisunique, x.indisvalid, x.indisready, x.indimmediate,
           x.indpred IS NOT NULL AS is_partial,
           x.indexprs IS NOT NULL AS is_expression,
           x.indnatts, x.indnkeyatts,
           x.indkey::int[] AS attnums
    FROM pg_index x
    JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE t.relname = :t AND n.nspname = ANY (current_schemas(false))
"""


def index_proof_requirements(dialect: str) -> dict[str, str]:
    """What a usable proof demands on this dialect, so it can be asserted."""
    if dialect == "postgresql":
        return dict(PG_PROOF_REQUIREMENTS)
    if dialect == "sqlite":
        return {"unique": "the index must be declared UNIQUE",
                "exact_attrs": "the indexed columns must be exactly the natural key"}
    return {}


def _pg_key_names(connection: Any, table_oid: int, attnums: list[int]) -> list[str]:
    """Resolve attribute numbers to column names through the relation OID.

    Deliberately keyed on ``attrelid = <indexed relation OID>``.  Looking the
    columns up by index *name* can resolve a same-named relation in another
    schema, which is exactly how an unrelated index gets mistaken for this one.
    """
    from sqlalchemy import text as _text

    names: list[str] = []
    for attnum in attnums:
        name = connection.execute(_text(
            "SELECT attname FROM pg_attribute WHERE attrelid = :oid AND attnum = :a"),
            {"oid": int(table_oid), "a": int(attnum)}).scalar()
        names.append(str(name) if name is not None else "")
    return names


def _verify_pg(connection: Any, table: str) -> dict[str, Any]:
    from sqlalchemy import text as _text

    wanted = sorted(NATURAL_KEY)
    for candidate in connection.execute(_text(_PG_INDEX_QUERY), {"t": table}).mappings():
        if not candidate["indisunique"]:
            continue
        if not candidate["indisvalid"] or not candidate["indisready"]:
            continue
        if not candidate["indimmediate"]:
            continue          # deferred uniqueness is not enforcement
        if candidate["is_partial"] or candidate["is_expression"]:
            continue
        if candidate["indnatts"] != candidate["indnkeyatts"]:
            continue          # INCLUDE columns
        key_attnums = list(candidate["attnums"])[:candidate["indnkeyatts"]]
        if len(key_attnums) != len(wanted):
            continue
        names = _pg_key_names(connection, candidate["table_oid"], key_attnums)
        if sorted(n for n in names if n) != wanted:
            continue
        return {"dialect": "postgresql", "index": str(candidate["index_name"]),
                "valid": True, "ready": True, "immediate": True, "partial": False,
                "expression": False, "include_columns": False,
                "key_attrs": wanted, "requirements": dict(PG_PROOF_REQUIREMENTS)}
    raise CollisionRefused(
        f"no valid, ready, immediate, non-partial, non-expression unique index with "
        f"exactly {wanted} and no INCLUDE columns on {table}: absent-key collisions "
        f"cannot be serialized")


def _verify_sqlite(connection: Any, table: str) -> dict[str, Any]:
    from sqlalchemy import text as _text

    for row in connection.execute(_text(f"PRAGMA index_list('{table}')")).mappings():
        if not row["unique"]:
            continue
        cols = [r["name"] for r in connection.execute(
            _text(f"PRAGMA index_info('{row['name']}')")).mappings()]
        if sorted(cols) == sorted(NATURAL_KEY):
            return {"dialect": "sqlite", "index": str(row["name"]),
                    "immediate": True,
                    "definition": f"UNIQUE({', '.join(cols)})"}
    raise CollisionRefused(
        f"no unique index on {table}(meeting_db_id, agenda_item_number)")


def verify_unique_index(connection: Any, table: str = "agenda_items") -> dict[str, Any]:
    """Refuse unless an index that genuinely enforces the natural key exists."""
    dialect = connection.dialect.name
    if dialect == "postgresql":
        return _verify_pg(connection, table)
    if dialect == "sqlite":
        return _verify_sqlite(connection, table)
    raise CollisionRefused(f"unsupported dialect {dialect!r} for collision serialization")


#: How a GOVERNED writer's collision control is proved.  Either the historical
#: key is genuinely unique (a future cleanup could achieve that), or the governed
#: writer uses the additive exact-key reservation.  Nothing else qualifies.
CONTROL_MODES = ("global_unique_index", "exact_key_reservation")


def governed_contract(dialect: str) -> dict[str, Any]:
    """What a governed writer must have to be allowed to create a new key."""
    return {
        "modes": list(CONTROL_MODES),
        "invariant_carrier": "the reservation primary key",
        "supporting": ["pg_advisory_xact_lock per key",
                       "SERIALIZABLE whole-unit retry"],
        "scope": "GOVERNED WRITERS ONLY: the reservation constrains the apply path. "
                 "It does not constrain a client that bypasses it, and it makes no "
                 "claim to make the historical data unique.",
        "non_guarantees": [
            "a client that writes agenda_items directly is not constrained",
            "the historical duplicate rows are unchanged by this contract",
        ],
    }


def verify_collision_control(connection: Any, table: str = "agenda_items",
                             reservation_table: str = "agenda_item_key_reservation"
                             ) -> dict[str, Any]:
    """Prove the writer is governed: a unique key, or the reservation, or refuse.

    The historical requirement — a global unique index on the natural key — is
    **impossible** on this data.  It is not weakened; it is *replaced for governed
    writers* by a contract that is provable, and the replacement is verified as
    strictly as the original was: exact primary key columns in order, exact column
    types and lengths, no nullability, and a validated foreign key with the exact
    delete behaviour.
    """
    from scripts.kg import stage2_reservation as reservation

    if reservation_table != reservation.RESERVATION_TABLE:
        raise CollisionRefused(
            f"the reservation table must be {reservation.RESERVATION_TABLE!r}")

    # Mode 1: a genuine global unique index (the historical requirement).
    try:
        found = verify_unique_index(connection, table)
        return {**found, "mode": "global_unique_index",
                "scope": "the historical key is genuinely unique"}
    except CollisionRefused as unique_refusal:
        unique_problem = str(unique_refusal)

    # Mode 2: the additive exact-key reservation.
    problems = reservation.verify_reservation_contract(connection)
    if problems:
        raise CollisionRefused(
            "no governed collision control: the natural key has no unique index "
            f"({unique_problem}), and the reservation contract is not satisfied: "
            + "; ".join(problems[:4]))
    return {
        "dialect": connection.dialect.name,
        "mode": "exact_key_reservation",
        "table": reservation.RESERVATION_TABLE,
        "primary_key": ["meeting_db_id", "agenda_item_number"],
        "invariant_carrier": "primary key",
        "advisory_locking": connection.dialect.name == "postgresql",
        "scope": "governed writers only",
        "historical_key_is_unique": False,
    }


def collision_contract(dialect: str) -> dict[str, Any]:
    """How absent-key collisions are made impossible rather than unlikely."""
    if dialect == "postgresql":
        strategy = "SERIALIZABLE transaction; on serialization failure retry the " \
                   "entire unit a bounded number of times, then refuse"
    elif dialect == "sqlite":
        strategy = "unique index with an immediate (non-deferred) transaction"
    else:
        strategy = ""
    return {
        "requires_unique_index": True,
        "natural_key": list(NATURAL_KEY),
        "strategy": strategy,
        "proof_requirements": index_proof_requirements(dialect),
        "row_locks_are_insufficient": "SELECT ... FOR UPDATE cannot lock a row that "
                                      "does not exist yet, so it cannot serialize two "
                                      "writers inserting the same absent key",
        "on_conflict": "refuse; never overwrite an existing item",
    }
