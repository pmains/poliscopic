#!/usr/bin/env python3
"""Regression tests for the sync propagation contract (Brief 037 §2, item 2).

The contract replaced an implementation observation — "a table is synced iff it
carries updated_at" — which was inferred from whichever schema happened to be
connected and therefore refused legitimate maintenance (a fixture with minimal
DDL, a table synced wholesale).

What must hold now:

  1. every table that can receive a body write declares HOW it propagates;
  2. an undeclared table fails closed rather than being written silently;
  3. a stamp is applied exactly when it is required *and* possible;
  4. the stamp-column requirement is asserted at the schema level, so a stamp
     skipped because a fixture lacks the column is never skipped against a real
     database.

Pure/unit level: no project database required.
"""

from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from db.sync_declarations import (  # noqa: E402
    ALL_SYNC_TABLES,
    EXCLUDED_TABLES,
    FULL_SYNC_TABLES,
    LOCAL_ONLY_TABLES,
    PROPAGATION,
    PROPAGATION_EXCLUDED,
    PROPAGATION_FULL_REFERENCE,
    PROPAGATION_INCREMENTAL,
    STAMP_REQUIRED_TABLES,
    body_write_sql,
    propagation_mode,
    stamp_required,
)
from ops.reference_integrity_gate import stamp_column_problems  # noqa: E402


# ── 1. every body-bearing table is declared ──────────────────────────────

# The body-bearing tables as observed in the development schema on 2026-09-18.
# Kept explicit so a *new* body column cannot appear without a declaration
# without this test failing.
BODY_BEARING = {
    "_ingest_failures",
    "_pattern_cascade_watermark",
    "agenda_item_votes",
    "agenda_items",
    "article_sources",
    "articles",
    "case_events",
    "dismissed_suggestions",
    "executive_session_participants",
    "meeting_attendance",
    "meeting_members",
    "meetings",
    "member_votes",
    "public_bodies",
    "pz_item_details",
    "scanned_agenda_text",
    "supporting_documents",
}


def test_every_body_bearing_table_has_a_declared_mode():
    undeclared = sorted(t for t in BODY_BEARING if t not in PROPAGATION)
    assert undeclared == [], (
        "these tables can hold a body code but declare no propagation mode: "
        f"{undeclared}"
    )


def test_declared_modes_are_partitioned_and_total():
    """incremental | full_reference | excluded, with no table in two classes."""
    for table, mode in PROPAGATION.items():
        assert mode in {
            PROPAGATION_INCREMENTAL,
            PROPAGATION_FULL_REFERENCE,
            PROPAGATION_EXCLUDED,
        }, f"{table} has an unknown mode {mode!r}"
    # membership must be consistent with the source declarations
    assert FULL_SYNC_TABLES <= set(ALL_SYNC_TABLES)
    assert ALL_SYNC_TABLES and not (set(ALL_SYNC_TABLES) & EXCLUDED_TABLES)
    assert LOCAL_ONLY_TABLES.isdisjoint(ALL_SYNC_TABLES)


def test_stamp_required_covers_incremental_classes_and_audited_tables():
    """DELIBERATELY revised in Stage B (owner decision 2).

    Stamping used to be equated with incremental transfer. That conflated two
    separate concepts:

      * transfer strategy    — how a table reaches production (incremental filter
                               vs full re-send)
      * mutation-audit stamping — whether a write must advance ``updated_at``

    A full-reference table is re-sent wholesale, yet its writes must STILL stamp so
    that audits — and any incremental consumer — observe the mutation. This is the
    defense in depth against the Brief 037 failure, where an in-place
    ``public_bodies.body_code`` rename skipped ``updated_at`` and became invisible.

    ``public_bodies`` is therefore full-reference AND stamp-required.
    """
    from db.sync_declarations import AUDIT_STAMP_TABLES, propagation_mode

    incremental = {t for t, m in PROPAGATION.items()
                   if m == PROPAGATION_INCREMENTAL}
    assert STAMP_REQUIRED_TABLES == incremental | AUDIT_STAMP_TABLES
    assert AUDIT_STAMP_TABLES, "audit stamping must not be empty"

    # the audit tables stamp even though they are not incremental
    assert propagation_mode("public_bodies") == "full_reference"
    for table in ("public_bodies", "meetings", "member_votes", "agenda_items"):
        assert stamp_required(table) is True, table
    for table in ("article_sources", "entity_types", "meeting_events"):
        assert stamp_required(table) is False, table


# ── 2. fail closed on an undeclared table ────────────────────────────────


def test_propagation_mode_fails_closed_on_undeclared_table():
    with pytest.raises(KeyError) as err:
        propagation_mode("a_table_nobody_declared")
    assert "no declared propagation mode" in str(err.value)


def test_body_write_sql_fails_closed_on_undeclared_table():
    with pytest.raises(KeyError):
        body_write_sql("a_table_nobody_declared", "body", has_stamp=True)


# ── 3. a stamp is applied iff required and possible ──────────────────────


def test_body_write_sql_stamps_an_incremental_table():
    sql = body_write_sql("public_bodies", "body_code", has_stamp=True)
    assert "updated_at = CURRENT_TIMESTAMP" in sql
    assert "body_code=:new" in sql
    assert "body_code=:old" in sql


def test_body_write_sql_omits_stamp_when_column_absent():
    """A minimal-DDL fixture is legitimate: no column, no stamp, no refusal."""
    sql = body_write_sql("meetings", "body", has_stamp=False)
    assert "updated_at" not in sql
    assert sql == 'UPDATE "meetings" SET body=:new WHERE body=:old'


def test_body_write_sql_never_stamps_a_full_reference_table():
    """entity_types is re-sent in full; a stamp would be meaningless."""
    sql = body_write_sql("entity_types", "body", has_stamp=True)
    assert "updated_at" not in sql


def test_body_write_sql_never_stamps_an_excluded_table():
    sql = body_write_sql("article_sources", "body", has_stamp=True)
    assert "updated_at" not in sql


def test_body_write_sql_quotes_the_identifier():
    sql = body_write_sql("member_votes", "body", has_stamp=True)
    assert sql.startswith('UPDATE "member_votes"')


# ── 4. the stamp-column requirement is asserted at the schema level ──────


def _engine(tables: dict[str, str]):
    """In-memory engine with the given DDL."""
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for name, columns in tables.items():
            conn.execute(text(f"CREATE TABLE {name} ({columns})"))
    return engine


def test_stamp_column_problems_flags_incremental_table_without_stamp():
    engine = _engine({"member_votes": "id INTEGER PRIMARY KEY, body TEXT"})
    problems = stamp_column_problems(engine)
    assert len(problems) == 1
    assert "member_votes" in problems[0]
    assert "cannot converge" in problems[0]


def test_stamp_column_problems_accepts_incremental_table_with_stamp():
    engine = _engine({
        "member_votes": "id INTEGER PRIMARY KEY, body TEXT, updated_at TIMESTAMP",
    })
    assert stamp_column_problems(engine) == []


def test_stamp_column_problems_ignores_excluded_and_full_reference():
    """Neither class needs a stamp, so neither may be reported."""
    engine = _engine({
        "article_sources": "id INTEGER PRIMARY KEY, body TEXT",
        "entity_types": "id INTEGER PRIMARY KEY, body TEXT",
    })
    assert stamp_column_problems(engine) == []


def test_stamp_column_problems_flags_undeclared_body_table():
    engine = _engine({"mystery_table": "id INTEGER PRIMARY KEY, body TEXT"})
    problems = stamp_column_problems(engine)
    assert len(problems) == 1
    assert "no declared propagation mode" in problems[0]
