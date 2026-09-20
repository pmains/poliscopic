"""Isolated tests for the ``phoenix-dr`` repair plan (no database, no network).

Every test runs against a throwaway in-memory SQLite database or a duck-typed
engine stub.  No PostgreSQL connection is opened and nothing is written to any
real database.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from scripts.kg import phoenix_dr_repair_plan as facade
from scripts.kg.phoenix_dr_adjudication import (
    BODY_CODE,
    PlanError,
    adjudicate,
    assert_development_target,
    collect_dispositions,
    fingerprint,
)
from scripts.kg.phoenix_dr_plan_body import (
    build_plan,
    content_hash_disposition,
    plan_digest,
    validate_plan,
)

DDL = (
    "CREATE TABLE jurisdictions (id INTEGER PRIMARY KEY, name VARCHAR, slug VARCHAR)",
    "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, jurisdiction_id INTEGER NOT NULL, "
    "name VARCHAR NOT NULL, slug VARCHAR NOT NULL, body_code VARCHAR, body_type VARCHAR, "
    "description TEXT)",
    "CREATE TABLE meetings (id INTEGER PRIMARY KEY, meeting_id VARCHAR, meeting_date VARCHAR, "
    "meeting_title VARCHAR, body VARCHAR, jurisdiction_id INTEGER, public_body_id INTEGER, "
    "source_url VARCHAR)",
    "CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, meeting_db_id INTEGER, "
    "body VARCHAR, document_title VARCHAR, document_url VARCHAR, text_extraction_method VARCHAR, "
    "content_hash VARCHAR, jurisdiction_id INTEGER, text_content TEXT)",
    "CREATE TABLE meeting_event_extractions (id INTEGER PRIMARY KEY, supporting_doc_id INTEGER)",
)


def build_engine(*, meetings=2, docs=2, parented=False, method="pdftotext", host="phoenix.gov"):
    """Seed a throwaway SQLite database shaped like the real target subset."""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for stmt in DDL:
            conn.execute(text(stmt))
        conn.execute(text("INSERT INTO jurisdictions VALUES (4, 'City of Phoenix', 'phoenix')"))
        conn.execute(text("INSERT INTO jurisdictions VALUES (1, 'Maricopa County', 'maricopa-county')"))
        for mid in range(1, meetings + 1):
            conn.execute(
                text(
                    "INSERT INTO meetings VALUES (:id, :ext, '2026-06-22', 'Design Review Committee', "
                    "'phoenix-dr', NULL, :pb, 'https://www.phoenix.gov/x.pdf')"
                ),
                {"id": mid, "ext": f"publicmeetings-{mid}", "pb": (10 if parented else None)},
            )
        for did in range(1, docs + 1):
            conn.execute(
                text(
                    "INSERT INTO supporting_documents VALUES (:id, :mid, 'phoenix-dr', "
                    "'Design Review Committee', :url, :method, NULL, 1, 'some analysed text')"
                ),
                {
                    "id": did,
                    "mid": did,
                    "url": f"https://www.{host}/content/x.pdf",
                    "method": method,
                },
            )
        conn.execute(text("INSERT INTO meeting_event_extractions VALUES (1, 1)"))
        conn.execute(
            text(
                "INSERT INTO meeting_event_extractions VALUES (2, 1)"
            )
        )
    return engine


MEETING = {
    "id": 1, "meeting_id": "m1", "meeting_title": "Design Review Committee",
    "body": BODY_CODE, "public_body_id": None,
}
DOC = {
    "id": 1, "meeting_db_id": 1, "body": BODY_CODE, "document_title": "Design Review Committee",
    "document_url": "https://www.phoenix.gov/x.pdf", "text_extraction_method": "pdftotext",
    "text_chars": 100,
}


class _StubDialect:
    def __init__(self, name):
        self.name = name


class _StubEngine:
    """Duck-typed engine: assert_development_target only reads url and dialect."""

    def __init__(self, url, dialect):
        self.url = url
        self.dialect = _StubDialect(dialect)


# -- adjudication -------------------------------------------------------------


def test_uniquely_supportive_row_is_included():
    included, reason = adjudicate(MEETING, DOC)
    assert included is True
    assert reason is None


@pytest.mark.parametrize(
    "mutate,expected_reason",
    [
        (lambda m, d: m.update(public_body_id=10), "already parented"),
        (lambda m, d: m.update(body="phoenix-other"), "stored body code is not the target body"),
        (lambda m, d: m.update(meeting_title="Planning Commission"), "title lacks the body signal"),
        (lambda m, d: d.update(body="phoenix-other"), "supporting document body code does not match"),
        (lambda m, d: d.update(meeting_db_id=99), "do not pair one-to-one"),
        (lambda m, d: d.update(document_title="Planning Commission"), "title lacks the body signal"),
        (lambda m, d: d.update(text_extraction_method="mystery"), "unsupported text extraction method"),
        (lambda m, d: d.update(text_chars=0), "no stored analysed text"),
        (lambda m, d: d.update(document_url="https://evil.example.com/x.pdf"), "not on the phoenix.gov host"),
    ],
)
def test_unsupportive_rows_are_excluded_with_a_reason(mutate, expected_reason):
    meeting, doc = dict(MEETING), dict(DOC)
    mutate(meeting, doc)
    included, reason = adjudicate(meeting, doc)
    assert included is False
    assert expected_reason in (reason or "")


def test_missing_document_is_excluded():
    included, reason = adjudicate(MEETING, None)
    assert included is False
    assert "no supporting document" in reason


# -- target safety ------------------------------------------------------------


def test_sqlite_is_accepted_as_the_isolated_test_tier():
    info = assert_development_target(create_engine("sqlite://"))
    assert info["tier"] == "test-isolated"


@pytest.mark.parametrize(
    "url,dialect",
    [
        # correct host class, wrong database name
        ("postgresql://u:p@db.example.com:5432/poliscopic", "postgresql"),
        # production host, even with a dev-looking database name
        ("postgresql://u:p@tenant.example.ondigitalocean.com:25060/poliscopic_dev", "postgresql"),
        # unsupported dialect
        ("mysql://u:p@host/db", "mysql"),
    ],
)
def test_unsafe_targets_are_refused(url, dialect):
    with pytest.raises(PlanError):
        assert_development_target(_StubEngine(url, dialect))


def test_correct_database_on_a_non_production_host_is_accepted():
    """The contract is: poliscopic_dev on a non-production host.  Not a host list."""
    info = assert_development_target(
        _StubEngine("postgresql://u:p@db.example.com:5432/poliscopic_dev", "postgresql")
    )
    assert info["tier"] == "development"
    assert info["database"] == "poliscopic_dev"


# -- plan construction --------------------------------------------------------


def test_plan_operations_and_reconciliation():
    plan = build_plan(build_engine())
    assert plan["reconciliation"]["included"] == 2
    assert plan["reconciliation"]["excluded"] == 0
    assert plan["reconciliation"]["accounts_for_every_candidate"] is True
    assert plan["operations_by_table"]["public_bodies"]["insert"] == 1
    assert plan["operations_by_table"]["meetings"]["update"] == 2
    assert plan["operations_by_table"]["supporting_documents"]["update"] == 0
    assert plan["safety"]["applied"] is False
    assert plan["safety"]["mutations_performed"] == 0


def test_plan_emits_exactly_one_meetings_update_resolved_by_body_code():
    plan = build_plan(build_engine())
    updates = [o for o in plan["operations"] if o["op"] == "UPDATE"]
    assert len(updates) == 1
    assert updates[0]["table"] == "meetings"
    assert BODY_CODE in updates[0]["set"]["public_body_id"]
    # the insert must not predict a surrogate key
    insert = [o for o in plan["operations"] if o["op"] == "INSERT"][0]
    assert "id" not in insert["values"]


def test_plan_records_fingerprints_for_every_included_row():
    plan = build_plan(build_engine())
    update = [o for o in plan["operations"] if o["op"] == "UPDATE"][0]
    assert len(update["fingerprints_before"]) == 2
    assert all(len(v) == 64 for v in update["fingerprints_before"].values())


def test_plan_postconditions_arithmetic():
    plan = build_plan(build_engine())
    expected = {p["id"]: p["expected"] for p in plan["expected_postconditions"]}
    before = plan["before_state"]
    assert expected["body_exactly_one"] == 1
    assert expected["meetings_parented_exactly"] == 2
    assert expected["no_target_still_unparented"] == 0
    assert expected["public_bodies_total"] == before["public_bodies_total"] + 1
    assert expected["meetings_null_public_body"] == before["meetings_null_public_body"] - 2
    assert expected["supporting_documents_unchanged"] == before["supporting_documents_total"]


def test_plan_refuses_when_the_body_already_exists():
    engine = build_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public_bodies VALUES (99, 4, 'Existing', 'phoenix-dr', 'phoenix-dr', "
                "'Committee', NULL)"
            )
        )
    with pytest.raises(PlanError):
        build_plan(engine)


def test_plan_refuses_when_nothing_is_supportive():
    engine = build_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE meetings SET body = 'phoenix-other'"))
    with pytest.raises(PlanError):
        build_plan(engine)


def test_ambiguous_document_pairing_is_excluded_not_guessed():
    engine = build_engine(meetings=1, docs=1)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO supporting_documents VALUES (2, 1, 'phoenix-dr', "
                "'Design Review Committee', 'https://www.phoenix.gov/y.pdf', 'pdftotext', NULL, 1, 't')"
            )
        )
    with engine.connect() as conn:
        dispositions = collect_dispositions(conn)
    assert len(dispositions) == 1
    assert dispositions[0].included is False
    assert "do not pair one-to-one" in dispositions[0].exclusion_reason


# -- content hash disposition -------------------------------------------------


def test_content_hash_disposition_emits_no_operation():
    disposition = content_hash_disposition()
    assert disposition["operation_included"] is False
    assert disposition["emitted_operations"] == 0
    assert disposition["invented_value"] is False
    assert disposition["canonical_rule_exists"] is False
    assert len(disposition["alternatives"]) >= 2


def test_no_supporting_documents_operation_appears_anywhere_in_the_plan():
    plan = build_plan(build_engine())
    assert not any(o["table"] == "supporting_documents" for o in plan["operations"])


# -- digest -------------------------------------------------------------------


def test_digest_is_stable_and_verifies():
    plan = build_plan(build_engine())
    assert plan_digest(plan) == plan["digest"]["value"]
    validate_plan(plan)


def test_digest_detects_tampering(tmp_path):
    plan = build_plan(build_engine())
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    ok, _ = facade.verify_plan_file(str(path))
    assert ok is True

    plan["expected_post_state"]["public_bodies_total"] = 999
    path.write_text(json.dumps(plan), encoding="utf-8")
    ok, recomputed = facade.verify_plan_file(str(path))
    assert ok is False
    assert recomputed != plan["digest"]["value"]


def test_validate_plan_rejects_inconsistent_reconciliation():
    plan = build_plan(build_engine())
    plan["reconciliation"]["included"] = 5
    with pytest.raises(PlanError):
        validate_plan(plan)


def test_validate_plan_rejects_a_supporting_documents_update():
    plan = build_plan(build_engine())
    plan["operations_by_table"]["supporting_documents"]["update"] = 1
    with pytest.raises(PlanError):
        validate_plan(plan)


def test_fingerprint_is_deterministic_and_order_insensitive():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
