#!/usr/bin/env python3
"""``stage2_s2_schema_signature.py`` — the canonical schema signature a plan binds.

A plan that does not bind the schema it was built against cannot detect a schema move.
This module computes one canonical digest over exactly the objects a Stage 2 plan reads
or writes: every column, primary key, unique/other index and foreign key of
``agenda_items``, ``supporting_documents``, ``meetings`` and
``agenda_item_key_reservation``.

It is read-only and lives in its OWN module so that adding this binding to a newer plan
contract never edits a module that an already-applied artifact has bound.
"""

from __future__ import annotations

from typing import Any

__all__ = ["TABLES", "canonical_sha256", "signature", "verify"]

#: Every table the Stage 2 plans read or write.
TABLES = ("agenda_items", "supporting_documents", "meetings",
          "agenda_item_key_reservation")


def canonical_sha256(payload: Any) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def signature(connection: Any) -> dict[str, Any]:
    """The canonical signature of the four Stage 2 tables.  Reads only."""
    from sqlalchemy import text

    body: dict[str, Any] = {}
    for table in TABLES:
        columns = [{"name": r["column_name"], "type": r["data_type"],
                    "nullable": r["is_nullable"] == "YES",
                    "default": (r["column_default"] or "")[:80]}
                   for r in connection.execute(text("""
                       SELECT column_name, data_type, is_nullable, column_default
                       FROM information_schema.columns WHERE table_name = :t
                       ORDER BY ordinal_position"""), {"t": table}).mappings()]
        primary = connection.execute(text("""
            SELECT a.attname FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.contype = 'p' AND c.conrelid = CAST(:t AS regclass)
            ORDER BY k.ord"""), {"t": table}).scalars().all()
        indexes = [{"name": r["indexname"], "definition": r["indexdef"]}
                   for r in connection.execute(text("""
                       SELECT indexname, indexdef FROM pg_indexes
                       WHERE tablename = :t ORDER BY indexname"""),
                       {"t": table}).mappings()]
        foreign = [{"name": r["conname"], "table": r["referred"],
                    "on_delete": r["del"], "on_update": r["upd"],
                    "validated": r["validated"]}
                   for r in connection.execute(text("""
                       SELECT c.conname, rt.relname AS referred,
                              c.confdeltype AS del, c.confupdtype AS upd,
                              c.convalidated AS validated
                       FROM pg_constraint c
                       JOIN pg_class t ON t.oid = c.conrelid
                       JOIN pg_class rt ON rt.oid = c.confrelid
                       WHERE c.contype = 'f' AND t.relname = :t"""),
                       {"t": table}).mappings()]
        body[table] = {"columns": columns, "primary_key": list(primary),
                       "indexes": indexes, "foreign_keys": foreign}
    return {**body, "digest": canonical_sha256(body)}


def verify(connection: Any, bound: dict[str, Any]) -> list[str]:
    """The live schema must be the one the plan bound, or nothing proves anything."""
    if not bound or not bound.get("digest"):
        return ["the plan binds no schema signature"]
    # Recompute over the BOUND body first: a tampered signature carrying an intact
    # ``digest`` field must not compare clean just because the field was left alone.
    body = {k: v for k, v in bound.items() if k != "digest"}
    if canonical_sha256(body) != bound["digest"]:
        return ["the bound schema signature does not match its own digest (tampered)"]
    live = signature(connection)
    if live["digest"] == bound["digest"]:
        return []
    problems = ["the live schema differs from the bound schema signature"]
    for table in TABLES:
        was, now = bound.get(table) or {}, live.get(table) or {}
        if was == now:
            continue
        for key in ("columns", "primary_key", "indexes", "foreign_keys"):
            if was.get(key) != now.get(key):
                problems.append(f"{table}.{key} drifted")
    return problems
