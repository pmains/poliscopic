"""Read-side storage adapter tests against isolated SQLite.

No dev database, no production database, no pipeline, and no writes to the civic
tables.  Each test builds a throwaway in-memory SQLite database (see the sibling
``_kg_event_normalize_sqlite`` helper) and lets it die.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.entities.event_normalize_planning import build_classification_plan
from scripts.entities.event_normalize_snapshot import ExistingNormalizedEvent
from scripts.entities.event_normalize_storage import (
    REASON_INVALID_ACTION_VERB, REASON_INVALID_EVENT_TYPE,
    REASON_INVALID_OUTCOME, REASON_MALFORMED_OFFSETS,
    REASON_MISSING_EXTRACTOR, REASON_MISSING_JURISDICTION,
    REASON_MISSING_LINKED_EVENT, REASON_MISSING_MEETING,
    REASON_MISSING_PUBLIC_BODY, REASON_MISSING_SUPPORTING_DOCUMENT,
    REASON_MISSING_TEXT, REASON_UNSUPPORTED_METHOD,
    NormalizationPage, NormalizationWorkItem, PageAccountingError,
    WorkItemError, fetch_normalization_page,
)

from _kg_event_normalize_sqlite import (
    DEFAULT_MEETING_SOURCE, add_extraction, build_engine, seed,
)

_MODULES = (
    "event_normalize_storage.py",
    "event_normalize_query.py",
)
ENTITIES_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "entities"


@pytest.fixture()
def engine():
    return build_engine()


def make_candidate(**overrides):
    kwargs = dict(
        extraction_id=1, supporting_document_id=1, meeting_db_id=500,
        meeting_source_id="M-1", public_body_id="B-1", jurisdiction_id="J-1",
        action_verb="approved", content_hash="h", extraction_method="pdftotext",
    )
    kwargs.update(overrides)
    return NormalizationCandidate.create(**kwargs)


def make_snapshot(extraction_id=1, **overrides):
    kwargs = dict(
        extraction_id=extraction_id, event_id=1, supporting_document_id=1,
        event_type="approval", outcome_base="approved", meeting_db_id=500,
        stored_meeting_source_id="M-1", canonical_meeting_source_id="M-1",
        supporting_document_meeting_db_id=500,
        supporting_document_meeting_source_id="M-1", action_verb="approved",
    )
    kwargs.update(overrides)
    return ExistingNormalizedEvent(**kwargs)


# -- modes --------------------------------------------------------------------


def test_unlinked_normal_mode_row_yields_a_candidate(engine):
    ids = seed(engine)
    page = fetch_normalization_page(engine)

    assert page.examined == 1
    assert page.failures == ()
    assert dict(page.existing_events) == {}
    assert len(page.work_items) == 1

    item = page.work_items[0]
    assert item.existing_event is None
    assert item.is_linked is False
    c = item.candidate
    assert c.extraction_id == ids["xid"]
    assert c.supporting_document_id == ids["did"]
    assert c.meeting_db_id == ids["mid"]
    assert c.meeting_source_id == DEFAULT_MEETING_SOURCE
    assert c.event_type == "approval"
    assert c.outcome.base == "approved"
    assert page.last_extraction_id == ids["xid"]
    assert page.has_more is False


def test_linked_row_is_excluded_in_normal_mode(engine):
    seed(engine, xid=1)
    seed(engine, xid=2, linked=True, did=2, mid=2, bid=2, jid=2, tid=2)

    page = fetch_normalization_page(engine)
    assert page.examined == 1
    assert [i.extraction_id for i in page.work_items] == [1]
    assert dict(page.existing_events) == {}


def test_force_mode_includes_linked_rows_as_work_items(engine):
    seed(engine, xid=1)
    seed(engine, xid=2, linked=True, did=2, mid=2, bid=2, jid=2, tid=2)

    page = fetch_normalization_page(engine, force=True)

    assert page.examined == 2
    assert [i.extraction_id for i in page.work_items] == [1, 2]
    assert page.failures == ()
    linked = page.work_items[1]
    assert linked.existing_event is not None
    assert linked.existing_event.event_id == 1


# -- work items ---------------------------------------------------------------


def test_unlinked_row_yields_one_work_item_without_a_snapshot(engine):
    seed(engine)
    page = fetch_normalization_page(engine)

    assert len(page.work_items) == 1
    assert page.work_items[0].existing_event is None
    assert page.work_items[0].candidate.extraction_id == 1


def test_linked_row_yields_one_work_item_with_both_sides(engine):
    seed(engine, linked=True)
    page = fetch_normalization_page(engine, force=True)

    assert len(page.work_items) == 1
    item = page.work_items[0]
    assert item.candidate is not None
    assert item.existing_event is not None
    assert item.candidate.extraction_id == item.existing_event.extraction_id == 1


def test_work_item_rejects_mismatched_extraction_ids():
    with pytest.raises(WorkItemError, match="same extraction row"):
        NormalizationWorkItem(
            candidate=make_candidate(extraction_id=1),
            existing_event=make_snapshot(extraction_id=2),
        )

    matched = NormalizationWorkItem(
        candidate=make_candidate(extraction_id=3),
        existing_event=make_snapshot(extraction_id=3),
    )
    assert matched.extraction_id == 3
    assert matched.is_linked is True


def test_unchanged_linked_work_item_plans_two_replay_noops(engine):
    seed(engine, linked=True)
    item = fetch_normalization_page(engine, force=True).work_items[0]

    plan = build_classification_plan(
        [item.candidate], existing_events=[item.existing_event]
    )

    assert plan.total_replay_noop == 2
    assert plan.total_would_insert == 0
    assert plan.total_would_update == 0
    assert plan.total_inconsistent == 0
    assert plan.is_consistent is True


def test_changed_stored_semantics_produce_an_inconsistent_plan(engine):
    seed(engine, linked=True, outcome="denied")
    item = fetch_normalization_page(engine, force=True).work_items[0]

    assert item.candidate.outcome.base == "approved"
    assert item.existing_event.outcome_base == "denied"

    plan = build_classification_plan(
        [item.candidate], existing_events=[item.existing_event]
    )

    assert plan.total_inconsistent == 1
    assert plan.total_replay_noop == 0
    assert plan.is_consistent is False
    assert set(plan.inconsistent[0].differing_fields) == {"outcome_base"}


def test_linked_row_with_unusable_evidence_fails_closed(engine):
    """Stored state must not bypass current-evidence validation."""
    seed(engine, linked=True, method="bogus_method")
    page = fetch_normalization_page(engine, force=True)

    assert page.examined == 1
    assert page.work_items == ()
    assert dict(page.existing_events) == {}
    assert len(page.failures) == 1
    assert page.failures[0].reason == REASON_UNSUPPORTED_METHOD


def test_compatibility_accessors_derive_from_work_items(engine):
    seed(engine, xid=1)
    seed(engine, xid=2, linked=True, did=2, mid=2, bid=2, jid=2, tid=2)
    page = fetch_normalization_page(engine, force=True)

    assert page.candidates == tuple(i.candidate for i in page.work_items)
    assert dict(page.existing_events) == {
        i.extraction_id: i.existing_event
        for i in page.work_items
        if i.existing_event is not None
    }


# -- typed read failures ------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "kw", "reason"),
    (
        ("document", {"extraction_doc_id": 999},
         REASON_MISSING_SUPPORTING_DOCUMENT),
        ("meeting", {"doc_meeting_db_id": 999}, REASON_MISSING_MEETING),
        ("body", {"meeting_body_id": 999}, REASON_MISSING_PUBLIC_BODY),
        ("jurisdiction", {"body_jid": 999}, REASON_MISSING_JURISDICTION),
    ),
)
def test_broken_civic_join_is_a_typed_failure(engine, label, kw, reason):
    seed(engine, **kw)
    page = fetch_normalization_page(engine)

    assert page.examined == 1, label
    assert page.work_items == ()
    assert dict(page.existing_events) == {}
    assert len(page.failures) == 1
    assert page.failures[0].extraction_id == 1
    assert page.failures[0].reason == reason, label
    assert page.failures[0].detail


@pytest.mark.parametrize(
    ("label", "kw", "reason", "force"),
    (
        ("missing text", {"text_content": "   "}, REASON_MISSING_TEXT, False),
        ("unsupported method", {"method": "bogus_method"},
         REASON_UNSUPPORTED_METHOD, False),
        ("malformed offsets", {"span_start": 10, "span_end": None},
         REASON_MALFORMED_OFFSETS, False),
        ("unmappable verb", {"action_verb": "not_a_real_verb"},
         REASON_INVALID_ACTION_VERB, False),
        ("empty extractor", {"extractor": ""}, REASON_MISSING_EXTRACTOR, False),
        ("dangling event", {"linked": True, "insert_event": False},
         REASON_MISSING_LINKED_EVENT, True),
        ("unregistered type", {"linked": True, "slug": "not.a.registered.slug"},
         REASON_INVALID_EVENT_TYPE, True),
        ("unmappable outcome", {"linked": True, "outcome": "sanctioned"},
         REASON_INVALID_OUTCOME, True),
    ),
)
def test_unusable_rows_are_typed_failures(engine, label, kw, reason, force):
    seed(engine, **kw)
    page = fetch_normalization_page(engine, force=force)

    assert page.examined == 1, label
    assert page.work_items == (), label
    assert len(page.failures) == 1, label
    assert page.failures[0].reason == reason, label
    assert page.failures[0].detail, label


# -- pagination ---------------------------------------------------------------


def test_deterministic_pagination_beyond_one_page(engine):
    seed(engine, xid=1)
    for xid in (2, 3, 4, 5):
        add_extraction(engine, xid=xid)

    seen, cursor, pages = [], None, 0
    while True:
        page = fetch_normalization_page(engine, limit=2, after_extraction_id=cursor)
        pages += 1
        seen.extend(i.extraction_id for i in page.work_items)
        if not page.has_more:
            break
        cursor = page.last_extraction_id
        assert pages < 10, "pagination did not terminate"

    assert pages == 3
    assert seen == [1, 2, 3, 4, 5]


def test_the_first_page_is_never_repeated(engine):
    seed(engine, xid=1)
    for xid in (2, 3, 4, 5):
        add_extraction(engine, xid=xid)

    first = fetch_normalization_page(engine, limit=2)
    second = fetch_normalization_page(
        engine, limit=2, after_extraction_id=first.last_extraction_id
    )

    assert [i.extraction_id for i in first.work_items] == [1, 2]
    assert [i.extraction_id for i in second.work_items] == [3, 4]
    assert {i.extraction_id for i in first.work_items}.isdisjoint(
        {i.extraction_id for i in second.work_items}
    )


def test_limit_is_honoured_exactly(engine):
    seed(engine, xid=1)
    for xid in (2, 3, 4, 5):
        add_extraction(engine, xid=xid)

    page = fetch_normalization_page(engine, limit=2)
    assert page.examined == 2
    assert len(page.work_items) == 2
    assert page.has_more is True

    exact = fetch_normalization_page(engine, limit=5)
    assert exact.examined == 5
    assert exact.has_more is True

    beyond = fetch_normalization_page(engine, limit=10)
    assert beyond.examined == 5
    assert beyond.has_more is False


def test_empty_page_reports_no_cursor(engine):
    page = fetch_normalization_page(engine)

    assert page.examined == 0
    assert page.work_items == ()
    assert dict(page.existing_events) == {}
    assert page.failures == ()
    assert page.last_extraction_id is None
    assert page.has_more is False


def test_invalid_limit_is_rejected(engine):
    for bad in (0, -1, True, "5"):
        with pytest.raises(ValueError):
            fetch_normalization_page(engine, limit=bad)


# -- accounting ---------------------------------------------------------------


def test_every_fetched_row_is_accounted_for_exactly_once(engine):
    seed(engine, xid=1)
    seed(engine, xid=2, did=2, mid=2, method="bogus_method")
    seed(engine, xid=3, did=3, mid=3, linked=True)
    seed(engine, xid=4, did=4, mid=4, span_start=5, span_end=None)

    page = fetch_normalization_page(engine, force=True)

    assert page.examined == 4
    assert page.examined == len(page.work_items) + len(page.failures)
    assert [i.extraction_id for i in page.work_items] == [1, 3]
    assert [i.extraction_id for i in page.failures] == [2, 4]
    assert page.work_items[1].existing_event is not None
    assert page.last_extraction_id == 4

    work_ids = {i.extraction_id for i in page.work_items}
    failure_ids = {f.extraction_id for f in page.failures}
    assert work_ids.isdisjoint(failure_ids)
    assert len(work_ids | failure_ids) == page.examined

    page.check_accounting()


def test_accounting_rejects_an_unaccounted_row():
    item = NormalizationWorkItem(candidate=make_candidate(extraction_id=7))
    broken = NormalizationPage(
        work_items=(item,), failures=(), examined=2,
        last_extraction_id=7, has_more=False,
    )
    with pytest.raises(PageAccountingError, match="examined"):
        broken.check_accounting()


def test_accounting_rejects_a_double_counted_row():
    from scripts.entities.event_normalize_storage import ReadFailure

    item = NormalizationWorkItem(candidate=make_candidate(extraction_id=7))
    single = NormalizationPage(
        work_items=(item,), failures=(), examined=1,
        last_extraction_id=7, has_more=False,
    )
    single.check_accounting()  # a single row accounted once is fine

    doubled = NormalizationPage(
        work_items=(item,),
        failures=(ReadFailure(extraction_id=7, reason="x"),),
        examined=2, last_extraction_id=7, has_more=False,
    )
    with pytest.raises(PageAccountingError, match="both a work item and a failure"):
        doubled.check_accounting()


# -- source scan --------------------------------------------------------------


@pytest.mark.parametrize("name", _MODULES)
def test_modules_issue_no_write_statements(name):
    lowered = (ENTITIES_DIR / name).read_text(encoding="utf-8").lower()

    for verb in ("insert", "update", "delete", "drop", "truncate", "alter"):
        assert not re.search(rf"\b{verb}\b", lowered), verb
    assert not re.search(r"\bcreate\s+(table|index|view|unique)\b", lowered)


def test_the_page_query_is_a_select():
    lowered = (
        ENTITIES_DIR / "event_normalize_query.py"
    ).read_text(encoding="utf-8").lower()

    assert "select" in lowered
    assert "from meeting_event_extractions" in lowered
