"""Pure-layer tests for the stored-state snapshot and its comparison rules.

No database, no pipeline, no subprocess.  Everything here is in-memory.
"""

from __future__ import annotations

import pytest

from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.entities.event_normalize_planning import (
    ClassificationPlan,
    build_classification_plan,
)
from scripts.entities.event_normalize_snapshot import ExistingNormalizedEvent
from scripts.entities.event_normalize_storage import fetch_normalization_page

from _kg_event_normalize_sqlite import build_engine, seed


def candidate(**overrides) -> NormalizationCandidate:
    kwargs = dict(
        extraction_id=1,
        supporting_document_id=10,
        meeting_db_id=500,
        meeting_source_id="2024-03-05-CC",
        public_body_id="B-1",
        jurisdiction_id="J-1",
        action_verb="approved",
        content_hash="hash-a",
        extraction_method="pdftotext",
        confidence=0.9,
    )
    kwargs.update(overrides)
    return NormalizationCandidate.create(**kwargs)


def rich_candidate(**overrides) -> NormalizationCandidate:
    """A candidate with a populated span and case number."""
    kwargs = dict(span_start=100, span_end=140, case_number="CV2024-001")
    kwargs.update(overrides)
    return candidate(**kwargs)


def stored(c: NormalizationCandidate, **overrides) -> ExistingNormalizedEvent:
    """A stored-row snapshot that exactly matches ``c`` unless overridden."""
    kwargs = dict(
        extraction_id=c.extraction_id,
        event_id=900 + c.extraction_id,
        supporting_document_id=c.supporting_document_id,
        event_type=c.event_type,
        outcome_base=c.outcome.base,
        outcome_qualifier=c.outcome.qualifier,
        meeting_db_id=c.meeting_db_id,
        stored_meeting_source_id=c.meeting_source_id,
        canonical_meeting_source_id=c.meeting_source_id,
        supporting_document_meeting_db_id=c.meeting_db_id,
        supporting_document_meeting_source_id=c.meeting_source_id,
        meeting_context=c.meeting_identity,
        action_verb=c.action_verb,
        span_start=c.span_start,
        span_end=c.span_end,
        case_number=c.case_number,
    )
    kwargs.update(overrides)
    return ExistingNormalizedEvent(**kwargs)


def plan_for(*candidates, existing_events=None) -> ClassificationPlan:
    return build_classification_plan(candidates, existing_events=existing_events)


# -- meeting references -------------------------------------------------------


def test_stored_external_meeting_reference_match_is_a_replay():
    c = rich_candidate(meeting_source_id="2024-03-05-CC")
    snapshot = stored(c)

    assert snapshot.meeting_db_id == c.meeting_db_id
    assert snapshot.stored_meeting_source_id == c.meeting_source_id
    assert snapshot.canonical_meeting_source_id == c.meeting_source_id
    assert snapshot.meeting_context.digest == c.meeting_identity.digest

    plan = plan_for(c, existing_events=[snapshot])
    assert plan.total_replay_noop == 2
    assert plan.total_inconsistent == 0


def test_mismatched_stored_event_meeting_reference_is_named_separately():
    c = rich_candidate()
    plan = plan_for(c, existing_events=[stored(c, stored_meeting_source_id="STALE-1")])

    assert plan.total_inconsistent == 1
    fields = set(plan.inconsistent[0].differing_fields)
    assert fields == {"stored_meeting_source_id"}
    assert "canonical_meeting_source_id" not in fields
    assert "meeting_db_id" not in fields


def test_mismatched_canonical_meeting_source_reference_is_named_separately():
    c = rich_candidate()
    plan = plan_for(
        c, existing_events=[stored(c, canonical_meeting_source_id="CANON-OTHER")]
    )

    assert plan.total_inconsistent == 1
    fields = set(plan.inconsistent[0].differing_fields)
    assert fields == {"canonical_meeting_source_id"}
    assert "stored_meeting_source_id" not in fields


def test_mismatched_supporting_document_meeting_db_link_is_named_separately():
    c = rich_candidate()
    plan = plan_for(
        c, existing_events=[stored(c, supporting_document_meeting_db_id=999)]
    )

    assert plan.total_inconsistent == 1
    fields = set(plan.inconsistent[0].differing_fields)
    assert fields == {"supporting_document_meeting_db_id"}
    # The event's own meeting reference still agrees and must not be blamed.
    assert "meeting_db_id" not in fields


def test_mismatched_supporting_document_external_reference_is_named_separately():
    c = rich_candidate()
    plan = plan_for(
        c,
        existing_events=[
            stored(c, supporting_document_meeting_source_id="SD-OTHER")
        ],
    )

    assert plan.total_inconsistent == 1
    fields = set(plan.inconsistent[0].differing_fields)
    assert fields == {"supporting_document_meeting_source_id"}
    assert "stored_meeting_source_id" not in fields


def test_meeting_db_id_mismatch_is_named_separately():
    c = rich_candidate(meeting_db_id=500)
    plan = plan_for(c, existing_events=[stored(c, meeting_db_id=501)])

    assert plan.total_inconsistent == 1
    assert set(plan.inconsistent[0].differing_fields) == {"meeting_db_id"}


def test_meeting_reference_mismatches_are_never_collapsed_into_one_verdict():
    c = rich_candidate(meeting_source_id="SRC-1")
    plan = plan_for(
        c,
        existing_events=[
            stored(
                c,
                stored_meeting_source_id="STORED-OTHER",
                canonical_meeting_source_id="CANON-OTHER",
            )
        ],
    )

    fields = set(plan.inconsistent[0].differing_fields)
    assert fields == {"stored_meeting_source_id", "canonical_meeting_source_id"}
    assert len(plan.inconsistent) == 1


def test_same_external_meeting_id_under_different_bodies_does_not_collide():
    a = candidate(
        meeting_db_id=500, meeting_source_id="2024-03-05-CC", public_body_id="B-1"
    )
    b = candidate(
        meeting_db_id=501, meeting_source_id="2024-03-05-CC", public_body_id="B-2"
    )

    assert a.meeting_source_id == b.meeting_source_id
    assert a.meeting_identity.digest != b.meeting_identity.digest

    plan = plan_for(b, existing_events=[stored(a)])
    assert plan.total_inconsistent == 1
    fields = set(plan.inconsistent[0].differing_fields)
    assert "meeting_context" in fields
    assert "meeting_db_id" in fields
    assert "supporting_document_meeting_db_id" in fields
    # The external reference genuinely agrees; it must not be reported as wrong.
    assert "canonical_meeting_source_id" not in fields
    assert "stored_meeting_source_id" not in fields
    assert "supporting_document_meeting_source_id" not in fields


# -- exact match and per-field mismatch ---------------------------------------


def test_exact_stored_match_is_two_replay_noops():
    c = rich_candidate()
    plan = plan_for(c, existing_events=[stored(c)])

    assert plan.total_proposed == 2
    assert plan.total_would_insert == 0
    assert plan.total_would_update == 0
    assert plan.total_replay_noop == 2
    assert plan.total_inconsistent == 0
    assert plan.is_consistent is True
    assert len(plan.event_replay_noops) == 1
    assert len(plan.link_replay_noops) == 1
    plan.check_invariants()


def test_replay_reconstructs_the_identity_from_the_candidate():
    """The stored snapshot is compared, never trusted as an identity digest."""
    from scripts.entities.event_normalize_planning import EventAssertion

    c = rich_candidate()
    snapshot = stored(c, event_id=4242)
    plan = plan_for(c, existing_events=[snapshot])

    expected = EventAssertion.from_candidate(c).identity.digest
    assert plan.event_replay_noops[0].identity.digest == expected
    assert plan.link_replay_noops[0].expected_event.digest == expected
    assert plan.link_replay_noops[0].extraction_id == c.extraction_id
    assert snapshot.event_id == 4242


@pytest.mark.parametrize(
    ("field", "override"),
    (
        ("supporting_document_id", {"supporting_document_id": 999}),
        ("event_type", {"event_type": "discussion"}),
        ("outcome_base", {"outcome_base": "denied"}),
        ("outcome_qualifier", {"outcome_qualifier": "with_conditions"}),
        ("meeting_db_id", {"meeting_db_id": 501}),
        ("stored_meeting_source_id", {"stored_meeting_source_id": "OTHER-1"}),
        ("canonical_meeting_source_id", {"canonical_meeting_source_id": "OTHER-2"}),
        ("supporting_document_meeting_db_id", {
            "supporting_document_meeting_db_id": 999
        }),
        ("supporting_document_meeting_source_id", {
            "supporting_document_meeting_source_id": "OTHER-3"
        }),
        ("action_verb", {"action_verb": "denied"}),
        ("span", {"span_start": 5, "span_end": 10}),
        ("case_number", {"case_number": "OTHER-1"}),
    ),
)
def test_each_semantic_field_mismatch_fails_closed(field, override):
    c = rich_candidate()
    plan = plan_for(c, existing_events=[stored(c, **override)])

    assert plan.total_inconsistent == 1, field
    assert plan.total_replay_noop == 0, field
    assert plan.total_would_insert == 0, field
    assert plan.total_would_update == 0, field
    assert plan.is_consistent is False
    assert field in plan.inconsistent[0].differing_fields, field
    plan.check_invariants()


# -- historical outcomes ------------------------------------------------------


def test_historical_qualified_outcome_canonicalizes_to_the_same_pair():
    c = candidate(action_verb="denied_without_prejudice")
    assert (c.outcome.base, c.outcome.qualifier) == ("denied", "without_prejudice")

    plan = plan_for(
        c,
        existing_events=[
            stored(c, outcome_base="denied", outcome_qualifier="without_prejudice")
        ],
    )
    assert plan.total_replay_noop == 2
    assert plan.total_inconsistent == 0


def test_stored_dotted_slug_normalizes_to_the_canonical_leaf():
    c = candidate(action_verb="denied")
    plan = plan_for(c, existing_events=[stored(c, event_type="decision.denial")])

    assert plan.total_replay_noop == 2
    assert plan.total_inconsistent == 0


def test_historical_unqualified_outcome_matches_an_unqualified_candidate():
    c = candidate(action_verb="approved")
    plan = plan_for(
        c, existing_events=[stored(c, outcome_base="approved", outcome_qualifier=None)]
    )
    assert plan.total_replay_noop == 2


def test_incompatible_outcome_mismatch_is_reported_on_the_base():
    c = candidate(action_verb="approved")
    plan = plan_for(c, existing_events=[stored(c, outcome_base="denied")])

    assert plan.total_inconsistent == 1
    assert "outcome_base" in plan.inconsistent[0].differing_fields


def test_unmappable_stored_outcome_fails_closed_on_both_outcome_fields():
    c = candidate(action_verb="approved")
    snapshot = stored(c, outcome_base="sanctioned", outcome_qualifier=None)

    assert snapshot.canonical_stored_outcome() is None
    plan = plan_for(c, existing_events=[snapshot])

    assert plan.total_inconsistent == 1
    fields = set(plan.inconsistent[0].differing_fields)
    assert {"outcome_base", "outcome_qualifier"} <= fields


# -- case number and offsets --------------------------------------------------


def test_present_versus_absent_case_number_mismatches():
    c = candidate(case_number=None)
    plan = plan_for(c, existing_events=[stored(c, case_number="CV2024-001")])

    assert plan.total_inconsistent == 1
    assert "case_number" in plan.inconsistent[0].differing_fields


def test_offset_mismatch_fails_closed():
    c = rich_candidate(span_start=100, span_end=140)

    shifted = plan_for(c, existing_events=[stored(c, span_start=100, span_end=141)])
    assert shifted.total_inconsistent == 1
    assert "span" in shifted.inconsistent[0].differing_fields

    gained = plan_for(c, existing_events=[stored(c, span_start=5, span_end=10)])
    assert "span" in gained.inconsistent[0].differing_fields

    lost = plan_for(c, existing_events=[stored(c, span_start=None, span_end=None)])
    assert "span" in lost.inconsistent[0].differing_fields


def test_matching_offsets_and_both_absent_match():
    c = rich_candidate(span_start=100, span_end=140)
    assert plan_for(c, existing_events=[stored(c)]).total_replay_noop == 2

    absent = candidate(span_start=None, span_end=None)
    assert plan_for(absent, existing_events=[stored(absent)]).total_replay_noop == 2


# -- source document and meeting context --------------------------------------


def test_source_document_mismatch_fails_closed():
    c = rich_candidate(supporting_document_id=10)
    plan = plan_for(c, existing_events=[stored(c, supporting_document_id=11)])

    assert plan.total_inconsistent == 1
    assert "supporting_document_id" in plan.inconsistent[0].differing_fields


def test_meeting_context_mismatch_fails_closed():
    c = rich_candidate(meeting_db_id=500)
    other_meeting = candidate(meeting_db_id=777).meeting_identity

    plan = plan_for(c, existing_events=[stored(c, meeting_context=other_meeting)])

    assert plan.total_inconsistent == 1
    assert "meeting_context" in plan.inconsistent[0].differing_fields


# -- snapshot model surface ---------------------------------------------------


def test_snapshot_model_exposes_the_stored_fields():
    c = rich_candidate()
    snapshot = stored(c, event_id=77)

    assert snapshot.extraction_id == c.extraction_id
    assert snapshot.event_id == 77
    assert snapshot.supporting_document_id == c.supporting_document_id
    assert snapshot.event_type == c.event_type
    assert snapshot.outcome_base == c.outcome.base
    assert snapshot.outcome_qualifier == c.outcome.qualifier
    assert snapshot.meeting_db_id == c.meeting_db_id
    assert snapshot.stored_meeting_source_id == c.meeting_source_id
    assert snapshot.canonical_meeting_source_id == c.meeting_source_id
    assert snapshot.supporting_document_meeting_db_id == c.meeting_db_id
    assert snapshot.supporting_document_meeting_source_id == c.meeting_source_id
    assert snapshot.action_verb == c.action_verb
    assert (snapshot.span_start, snapshot.span_end) == (100, 140)
    assert snapshot.case_number == "CV2024-001"
    assert snapshot.compare_to(c) == ()


def test_snapshot_requires_the_supporting_document_linkage():
    from scripts.entities.event_normalize_models import CandidateError

    c = rich_candidate()
    for missing in (
        "supporting_document_meeting_db_id",
        "supporting_document_meeting_source_id",
        "meeting_db_id",
        "stored_meeting_source_id",
        "canonical_meeting_source_id",
    ):
        with pytest.raises(CandidateError):
            stored(c, **{missing: None})


def test_ambiguous_stored_span_is_rejected_at_construction():
    from scripts.entities.event_normalize_models import CandidateError

    c = rich_candidate()
    with pytest.raises(CandidateError):
        stored(c, span_start=10, span_end=None)
    with pytest.raises(CandidateError):
        stored(c, span_start=40, span_end=10)


# -- snapshot fields read from a stored row ----------------------------------


@pytest.fixture()
def engine():
    return build_engine()


def test_each_meeting_reference_comes_from_its_own_column(engine):
    seed(
        engine,
        linked=True,
        meeting_source="CANONICAL-REF",
        doc_meeting_source="SD-REF",
        event_meeting_source="STORED-REF",
    )
    page = fetch_normalization_page(engine, force=True)
    snapshot = page.work_items[0].existing_event

    assert snapshot.meeting_db_id == 1
    assert snapshot.canonical_meeting_source_id == "CANONICAL-REF"
    assert snapshot.supporting_document_meeting_source_id == "SD-REF"
    assert snapshot.stored_meeting_source_id == "STORED-REF"
    assert snapshot.supporting_document_meeting_db_id == 1


def test_qualified_stored_outcome_is_reconstructed(engine):
    seed(engine, linked=True, outcome="approved_with_conditions")
    page = fetch_normalization_page(engine, force=True)
    snapshot = page.work_items[0].existing_event

    assert snapshot.outcome_base == "approved"
    assert snapshot.outcome_qualifier == "with_conditions"
