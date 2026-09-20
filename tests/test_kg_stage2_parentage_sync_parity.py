"""Isolated tests for meeting-parentage sync and schema-parity readiness.

Two engines are built per test as in-memory SQLite databases standing in for the
development and production sides of a sync.  Nothing here contacts a real
database, runs a sync, or touches production.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.db import sync_prod  # noqa: E402
from scripts.entities.schema_parity import (  # noqa: E402
    contract_violations,
    schema_signature,
    signature_differences,
)
from scripts.kg import stage2_parentage_contract as pc  # noqa: E402


def _engine(*statements: str):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    return engine


PARENTAGE_TABLE_DDL = (
    "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE jurisdictions (id INTEGER PRIMARY KEY, name TEXT)",
)

WITH_PARENTAGE = (
    "CREATE TABLE meetings ("
    " id INTEGER PRIMARY KEY,"
    " body TEXT,"
    " public_body_id INTEGER REFERENCES public_bodies(id),"
    " jurisdiction_id INTEGER REFERENCES jurisdictions(id))"
)

WITHOUT_PARENTAGE = (
    "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT)"
)


def _meetings_problems(signature: dict) -> list[str]:
    return [p for p in contract_violations(signature) if "meetings" in p]


# ── sync: payload carries parentage ─────────────────────────────────────


def test_sync_payload_carries_the_parentage_columns():
    dev = _engine(*PARENTAGE_TABLE_DDL, WITH_PARENTAGE)
    prod = _engine(*PARENTAGE_TABLE_DDL, WITH_PARENTAGE)
    columns = sync_prod._column_intersection(dev, prod, "meetings")
    assert "public_body_id" in columns
    assert "jurisdiction_id" in columns


def test_sync_readiness_reports_every_contracted_column_carried():
    readiness = pc.sync_readiness(
        ["id", "body", "public_body_id", "jurisdiction_id"],
        ["id", "body", "public_body_id", "jurisdiction_id"],
    )
    assert readiness["carried"] == ["jurisdiction_id", "public_body_id"]
    assert pc.readiness_problems(readiness) == []


def test_sync_refuses_when_prod_lacks_the_parentage_column():
    """The defect: the intersection would drop the column without a word."""
    dev = _engine(*PARENTAGE_TABLE_DDL, WITH_PARENTAGE)
    prod = _engine(*PARENTAGE_TABLE_DDL, WITHOUT_PARENTAGE)
    with pytest.raises(RuntimeError) as exc:
        sync_prod._column_intersection(dev, prod, "meetings")
    assert "would not be carried" in str(exc.value)
    assert "public_body_id" in str(exc.value)


def test_sync_still_returns_a_plain_intersection_for_other_tables():
    """Non-parentage tables keep exactly the previous behaviour."""
    dev = _engine("CREATE TABLE cases (id INTEGER PRIMARY KEY, a TEXT, dev_only TEXT)")
    prod = _engine("CREATE TABLE cases (id INTEGER PRIMARY KEY, a TEXT, prod_only TEXT)")
    assert sync_prod._column_intersection(dev, prod, "cases") == ["a", "id"]


def test_sync_readiness_detects_a_silent_drop_and_a_missing_dev_column():
    dropped = pc.sync_readiness(["public_body_id", "jurisdiction_id"], [])
    problems = pc.readiness_problems(dropped)
    assert any("silently drop" in p for p in problems)
    assert any("missing on prod" in p for p in problems)

    absent_on_dev = pc.sync_readiness([], ["public_body_id", "jurisdiction_id"])
    assert any("missing on dev" in p for p in pc.readiness_problems(absent_on_dev))


def test_sync_payload_view_never_hides_a_dropped_contracted_column():
    carried = pc.sync_payload_columns({"id", "public_body_id"}, {"id"}, "meetings")
    assert "public_body_id" in carried  # visible, so the caller can judge it


# ── sync: foreign-key ordering and targets ──────────────────────────────


def test_parent_registry_is_synced_before_meetings():
    order = sync_prod.ALL_SYNC_TABLES
    assert order.index("public_bodies") < order.index("meetings")
    assert order.index("jurisdictions") < order.index("meetings")
    assert pc.fk_order_problems(order) == []


def test_fk_order_check_catches_a_reversed_order():
    problems = pc.fk_order_problems(["meetings", "public_bodies", "jurisdictions"])
    assert problems
    assert any("public_bodies" in p for p in problems)


def test_sync_table_set_still_contains_the_existing_tables():
    for table in ("meetings", "public_bodies", "jurisdictions", "agenda_items",
                  "supporting_documents", "entity_mentions", "meeting_events"):
        assert table in sync_prod.ALL_SYNC_TABLES


def test_declared_fk_targets_are_present_in_the_live_signature():
    engine = _engine(*PARENTAGE_TABLE_DDL, WITH_PARENTAGE)
    assert pc.fk_problems(schema_signature(engine)) == []


def test_missing_fk_target_is_reported():
    engine = _engine("CREATE TABLE public_bodies (id INTEGER PRIMARY KEY)",
                     "CREATE TABLE jurisdictions (id INTEGER PRIMARY KEY)",
                     "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
                     " public_body_id INTEGER, jurisdiction_id INTEGER)")
    problems = pc.fk_problems(schema_signature(engine))
    assert any("public_body_id" in p for p in problems)


# ── parity: contract failures for the parentage columns ─────────────────


def test_parity_passes_when_parentage_columns_are_correct():
    engine = _engine(*PARENTAGE_TABLE_DDL, WITH_PARENTAGE)
    assert _meetings_problems(schema_signature(engine)) == []


def test_parity_fails_on_a_missing_parentage_column():
    engine = _engine(*PARENTAGE_TABLE_DDL,
                     "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
                     " jurisdiction_id INTEGER REFERENCES jurisdictions(id))")
    problems = _meetings_problems(schema_signature(engine))
    assert any("missing column: meetings.public_body_id" in p for p in problems)


def test_parity_fails_on_a_missing_meetings_table():
    engine = _engine("CREATE TABLE public_bodies (id INTEGER PRIMARY KEY)")
    problems = _meetings_problems(schema_signature(engine))
    # meetings is now part of the graph parity surface
    assert any("meetings" in p for p in problems)


def test_parity_fails_on_type_drift():
    engine = _engine(*PARENTAGE_TABLE_DDL,
                     "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
                     " public_body_id TEXT REFERENCES public_bodies(id),"
                     " jurisdiction_id INTEGER REFERENCES jurisdictions(id))")
    problems = _meetings_problems(schema_signature(engine))
    assert any("type drift: meetings.public_body_id" in p for p in problems)


def test_parity_fails_on_nullability_drift():
    engine = _engine(*PARENTAGE_TABLE_DDL,
                     "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
                     " public_body_id INTEGER NOT NULL REFERENCES public_bodies(id),"
                     " jurisdiction_id INTEGER REFERENCES jurisdictions(id))")
    problems = _meetings_problems(schema_signature(engine))
    assert any("nullability drift: meetings.public_body_id" in p for p in problems)


def test_parity_fails_on_signature_drift_between_two_sides():
    left = _engine(*PARENTAGE_TABLE_DDL, WITH_PARENTAGE)
    right = _engine(*PARENTAGE_TABLE_DDL,
                    "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
                    " public_body_id INTEGER REFERENCES public_bodies(id))")
    differences = signature_differences(schema_signature(left), schema_signature(right))
    assert "meetings" in differences
    assert "columns" in differences["meetings"]


def test_meetings_is_part_of_the_parity_graph_surface():
    from scripts.entities.schema_parity import GRAPH_TABLES, REQUIRED_COLUMNS
    assert "meetings" in GRAPH_TABLES
    assert {"id", "public_body_id", "jurisdiction_id"} <= REQUIRED_COLUMNS["meetings"]


# ── contract declaration ────────────────────────────────────────────────


def test_readiness_digest_is_stable_and_depends_on_the_bound_modules():
    first = pc.readiness_digest()
    assert first == pc.readiness_digest()
    changed = dict(pc.code_hashes())
    changed[pc.BOUND_MODULES[0]] = "0" * 64
    assert pc.readiness_digest(hashes=changed) != first


def test_bound_modules_exist_and_are_hashed():
    hashes = pc.code_hashes()
    assert set(hashes) == set(pc.BOUND_MODULES)
    for digest in hashes.values():
        assert len(digest) == 64


def test_type_family_normalizes_dialect_spellings():
    assert pc.type_family("INTEGER") == "INTEGER"
    assert pc.type_family("character varying") == "TEXT"
    assert pc.type_family("VARCHAR(16)") == "TEXT"
    assert pc.type_family("TIMESTAMP WITH TIME ZONE") == "TEMPORAL"
