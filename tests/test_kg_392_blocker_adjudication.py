"""Tests for the read-only 392-blocker adjudication.

Isolated: a purpose-built in-memory SQLite database supplies only the columns the
adjudication SQL reads.  No development or production database is contacted and
no pipeline phase is run.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from scripts.entities.event_normalize_preflight import ReadOnlyViolation, guard_engine
from scripts.kg.unparented_blocker_adjudication import (
    CLASSIFICATIONS,
    classify_group,
    group_blocked,
    inventory_unparented,
    registry_codes,
    render_markdown,
    run_adjudication,
)

DDL = (
    "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, "
    "body_code TEXT, body_type TEXT, jurisdiction_id INTEGER)",
    "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT, meeting_id TEXT, "
    "meeting_title TEXT, meeting_date TEXT, source_system TEXT, public_body_id INTEGER, "
    "jurisdiction_id INTEGER)",
    "CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, meeting_db_id INTEGER, "
    "body TEXT, document_title TEXT, jurisdiction_id INTEGER)",
    "CREATE TABLE meeting_event_extractions (id INTEGER PRIMARY KEY, supporting_doc_id INTEGER, "
    "action_verb TEXT)",
)


def build_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for statement in DDL:
            conn.execute(text(statement))
        conn.execute(text(
            "INSERT INTO meetings VALUES (1,'phoenix-dr','M1','Design Review Committee',"
            "'2026-06-22','(null)',NULL,4)"
        ))
        conn.execute(text(
            "INSERT INTO meetings VALUES (2,'phoenix-dr','M2','Design Review Committee',"
            "'2026-05-18','(null)',NULL,4)"
        ))
        conn.execute(text(
            "INSERT INTO meetings VALUES (3,'__skip__','M3','Unknown',"
            "'2020-01-01','(null)',NULL,1)"
        ))
        conn.execute(text(
            "INSERT INTO meetings VALUES (4,'chandler-cc','M4','Council','2026-01-01','(null)',NULL,3)"
        ))
        for did, mid, body in ((1, 1, 'phoenix-dr'), (2, 2, 'phoenix-dr'), (3, 3, '__skip__')):
            conn.execute(text(
                "INSERT INTO supporting_documents VALUES (:d,:m,:b,'Design Review Committee',4)"
            ), {"d": did, "m": mid, "b": body})
        for xid, did in ((11, 1), (12, 1), (21, 2), (31, 3)):
            conn.execute(text(
                "INSERT INTO meeting_event_extractions VALUES (:x,:d,'approved')"
            ), {"x": xid, "d": did})
    return engine


# -- classification -----------------------------------------------------------


def test_unique_candidate_is_uniquely_repairable():
    klass, why, _ = classify_group("chandler-cc", [], [{"id": 9, "name": "Chandler CC"}])
    assert klass == "uniquely_repairable"
    assert "exactly one" in why


def test_multiple_candidates_are_ambiguous():
    klass, why, action = classify_group("x", [], [{"id": 1}, {"id": 2}])
    assert klass == "ambiguous"
    assert "not identity" in why
    assert action.startswith("quarantine")


def test_registry_only_code_is_uniquely_repairable():
    klass, why, _ = classify_group("phoenix-dr", ["phoenix_planning.py"], [])
    assert klass == "uniquely_repairable"
    assert "registry" in why


def test_no_evidence_is_unmatched():
    klass, _why, action = classify_group("mystery-body", [], [])
    assert klass == "unmatched"
    assert action.startswith("quarantine")


@pytest.mark.parametrize("code", ["", "__skip__", "skip", "none", "null"])
def test_sentinel_codes_are_structurally_exceptional(code):
    klass, why, _ = classify_group(code, ["some_file.py"], [{"id": 1}])
    assert klass == "structurally_exceptional"
    assert "sentinel" in why


def test_every_classification_is_from_the_allowed_set():
    for args in (
        ("a", [], [{"id": 1}]),
        ("b", [], [{"id": 1}, {"id": 2}]),
        ("c", [], []),
        ("__skip__", [], []),
    ):
        assert classify_group(*args)[0] in CLASSIFICATIONS


# -- registry evidence --------------------------------------------------------


def test_registry_codes_finds_scraper_body_codes():
    codes = registry_codes()
    assert "phoenix-dr" in codes
    assert any("phoenix" in name for name in codes["phoenix-dr"])
    assert "phoenix-dab" in codes
    assert "phoenix-ds" in codes


def test_registry_codes_are_not_sentinels():
    codes = registry_codes()
    assert "__skip__" not in codes


# -- grouping and reconciliation ---------------------------------------------


def test_groups_are_formed_by_source_system_and_body_code():
    engine = build_engine()
    with engine.connect() as conn:
        groups = group_blocked(conn)
    by_code = {g.body_code: g for g in groups}
    assert by_code["phoenix-dr"].meetings == 2
    assert by_code["phoenix-dr"].extractions == 3
    assert by_code["__skip__"].extractions == 1
    assert by_code["phoenix-dr"].classification == "uniquely_repairable"


def test_extraction_ids_are_exact_and_ordered():
    engine = build_engine()
    with engine.connect() as conn:
        groups = group_blocked(conn)
    phoenix = next(g for g in groups if g.body_code == "phoenix-dr")
    assert phoenix.extraction_ids == [11, 12, 21]
    assert phoenix.meeting_ids == [1, 2]


def test_adjudication_reconciles_and_counts_every_classification():
    engine = build_engine()
    with engine.connect() as conn:
        groups = group_blocked(conn)
    total = sum(g.extractions for g in groups)
    assert total == 4
    assert sum(
        g.extractions for g in groups if g.classification == "structurally_exceptional"
    ) == 1


def test_run_adjudication_reports_reconciliation():
    engine = build_engine()
    record = run_adjudication(engine, expect_blocked=4, expect_phoenix_extractions=3,
                              expect_phoenix_meetings=2)
    assert record["blocked_total"] == 4
    assert record["reconciles_to_392"] is True
    assert record["phoenix_dr"]["reconciles"] is True
    assert record["reconciles_no_overlap"] is True
    assert record["read_only"]["statement_audit"]["select_only"] is True


def test_inventory_splits_relevant_from_unrelated():
    engine = build_engine()
    with engine.connect() as conn:
        inventory = inventory_unparented(conn)
    assert inventory["unparented_total"] == 4
    assert inventory["relevant_to_blockers"] == 3      # meetings 1, 2, 3 have extractions
    assert inventory["unrelated_historical"] == 1      # meeting 4 has none
    assert (
        inventory["relevant_to_blockers"] + inventory["unrelated_historical"]
        == inventory["unparented_total"]
    )


def test_group_dict_is_json_serialisable_and_compact():
    engine = build_engine()
    with engine.connect() as conn:
        groups = group_blocked(conn)
    payload = json.dumps([g.as_dict() for g in groups])
    assert "extraction_ids" in payload
    assert "candidate_public_bodies" in payload


# -- read-only enforcement ----------------------------------------------------


def test_adjudication_refuses_mutation_through_the_guarded_engine():
    engine = build_engine()
    guard_engine(engine)
    with pytest.raises(ReadOnlyViolation):
        with engine.begin() as conn:
            conn.execute(text("UPDATE meetings SET public_body_id = 1"))


def test_adjudication_refuses_a_production_target():
    from scripts.entities.event_normalize_preflight import PreflightError

    class NoConnect:
        url = "postgresql://u:***@db.b.db.ondigitalocean.com:25060/poliscopic"
        dialect = type("D", (), {"name": "postgresql"})()

    with pytest.raises(PreflightError):
        run_adjudication(NoConnect())


# -- markdown packet ----------------------------------------------------------


def test_markdown_packet_contains_the_review_sections():
    engine = build_engine()
    record = run_adjudication(engine, expect_blocked=4, expect_phoenix_extractions=3,
                              expect_phoenix_meetings=2)
    markdown = render_markdown(record)
    for heading in (
        "# Unparented-meeting blocker adjudication",
        "## Classification totals",
        "## Groups",
        "## Phoenix Design Review",
        "## Unparented-meeting inventory",
        "## Rationale per group",
    ):
        assert heading in markdown
    assert "__skip__" in markdown
