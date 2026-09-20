"""Write-contract tests: what may be written, and on what evidence.

Entirely pure -- no database, no transaction, no pipeline.  Plan refusal and
result reconciliation are decisions, so they are tested as decisions.
"""

from __future__ import annotations

import dataclasses

import pytest

from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.entities.event_normalize_planning import (
    ClassificationPlan, EventAssertion, ExtractionLinkAssertion,
    build_classification_plan,
)
from scripts.entities.event_normalize_snapshot import ExistingNormalizedEvent
from scripts.entities.event_normalize_write_contract import (
    PlanNotWritableError, ReconciliationError, WriteResult,
    stored_event_matches, validate_plan_for_write,
)
from scripts.entities.event_normalize_write_storage import StoredEventRow
from scripts.entities.event_normalize_writes import apply_classification_plan


def candidate(**overrides):
    kwargs = dict(
        extraction_id=1, supporting_document_id=1, meeting_db_id=500,
        meeting_source_id="M-1", public_body_id="B-1", jurisdiction_id="J-1",
        action_verb="approved", content_hash="h", extraction_method="pdftotext",
    )
    kwargs.update(overrides)
    return NormalizationCandidate.create(**kwargs)


def snapshot(c, **overrides):
    kwargs = dict(
        extraction_id=c.extraction_id, event_id=1,
        supporting_document_id=c.supporting_document_id, event_type=c.event_type,
        outcome_base=c.outcome.base, outcome_qualifier=c.outcome.qualifier,
        meeting_db_id=c.meeting_db_id,
        stored_meeting_source_id=c.meeting_source_id,
        canonical_meeting_source_id=c.meeting_source_id,
        supporting_document_meeting_db_id=c.meeting_db_id,
        supporting_document_meeting_source_id=c.meeting_source_id,
        meeting_context=c.meeting_identity, action_verb=c.action_verb,
        span_start=c.span_start, span_end=c.span_end, case_number=c.case_number,
    )
    kwargs.update(overrides)
    return ExistingNormalizedEvent(**kwargs)


def stored_row(**overrides):
    kwargs = dict(
        event_id=1, meeting_id="M-1", supporting_document_id=1, event_type_id=1,
        outcome="approved", action_verb="approved",
        span_start=None, span_end=None, case_number=None,
    )
    kwargs.update(overrides)
    return StoredEventRow(**kwargs)


def live_success() -> WriteResult:
    return WriteResult(
        events_planned=1, events_inserted=1, event_replay_noops=0,
        extraction_links_planned=1, extraction_links_updated=1,
        extraction_link_replay_noops=0, rows_committed=2,
        rows_rolled_back=0, dry_run=False,
    )


def live_failure(reason: str = "stale replay") -> WriteResult:
    return WriteResult(
        events_planned=1, events_inserted=0, event_replay_noops=0,
        extraction_links_planned=1, extraction_links_updated=0,
        extraction_link_replay_noops=0, rows_committed=0,
        rows_rolled_back=2, dry_run=False, failure_reason=reason,
        replay_verification_failures=1,
    )


# -- refusal ------------------------------------------------------------------


def test_inconsistent_plan_is_refused():
    c = candidate()
    plan = build_classification_plan(
        [c], existing_events=[snapshot(c, case_number="OTHER")]
    )
    assert plan.total_inconsistent == 1

    with pytest.raises(PlanNotWritableError, match="inconsistent"):
        validate_plan_for_write(plan)


@pytest.mark.parametrize("bucket", ["event", "link"])
def test_overlapping_assertions_are_refused(bucket):
    plan = build_classification_plan([candidate()])
    if bucket == "event":
        broken = dataclasses.replace(plan, event_replay_noops=plan.event_inserts)
    else:
        broken = dataclasses.replace(plan, link_replay_noops=plan.link_updates)

    with pytest.raises(PlanNotWritableError):
        validate_plan_for_write(broken)


def test_non_plan_is_refused():
    for not_a_plan in ({"a": 1}, None, [1, 2, 3]):
        with pytest.raises(PlanNotWritableError, match="ClassificationPlan"):
            validate_plan_for_write(not_a_plan)


# -- four-bucket coherence ----------------------------------------------------


def test_orphan_event_insert_is_refused():
    plan = build_classification_plan([candidate()])
    orphaned = dataclasses.replace(plan, link_updates=())

    with pytest.raises(PlanNotWritableError, match="referenced by no"):
        validate_plan_for_write(orphaned)


def test_orphan_event_replay_noop_is_refused():
    c = candidate()
    plan = build_classification_plan([c], existing_events=[snapshot(c)])
    assert len(plan.event_replay_noops) == 1 and len(plan.link_replay_noops) == 1

    orphaned = dataclasses.replace(plan, link_replay_noops=())

    with pytest.raises(PlanNotWritableError, match="referenced by no"):
        validate_plan_for_write(orphaned)


def test_orphan_link_replay_noop_is_refused():
    c = candidate()
    plan = build_classification_plan([c], existing_events=[snapshot(c)])
    orphaned = dataclasses.replace(plan, event_replay_noops=())

    with pytest.raises(PlanNotWritableError, match="backed by no EventAssertion"):
        validate_plan_for_write(orphaned)


def test_unresolved_link_target_is_refused():
    plan = build_classification_plan([candidate()])
    unresolved = dataclasses.replace(plan, event_inserts=())

    with pytest.raises(PlanNotWritableError, match="backed by no EventAssertion"):
        validate_plan_for_write(unresolved)


def test_supplied_stored_event_id_cannot_back_a_link():
    """A stored id is resolution evidence; it is never the assertion payload."""
    plan = build_classification_plan([candidate()])
    digest = plan.link_updates[0].expected_event.digest
    bare = dataclasses.replace(plan, event_inserts=())

    with pytest.raises(PlanNotWritableError, match="backed by no EventAssertion"):
        apply_classification_plan(None, bare, stored_event_ids={digest: 5})


def test_cross_wired_insert_and_replay_pair_is_refused():
    plan = build_classification_plan([candidate()])
    cross_wired = dataclasses.replace(
        plan, link_updates=(), link_replay_noops=plan.link_updates
    )

    with pytest.raises(PlanNotWritableError, match="pairs with the event insert"):
        validate_plan_for_write(cross_wired)


def test_two_links_claiming_one_event_identity_are_refused():
    plan = build_classification_plan([candidate()])
    stray = dataclasses.replace(plan.link_updates[0], extraction_id=777)
    broken = dataclasses.replace(plan, link_updates=plan.link_updates + (stray,))

    with pytest.raises(PlanNotWritableError, match="does not permit two"):
        validate_plan_for_write(broken)


def test_link_from_a_foreign_extraction_is_refused():
    c = candidate(extraction_id=1)
    event = EventAssertion.from_candidate(c)
    link = ExtractionLinkAssertion(
        extraction_id=2,
        extraction_identity=c.extraction_identity,
        expected_event=event.identity,
    )
    plan = ClassificationPlan(event_inserts=(event,), link_updates=(link,))

    with pytest.raises(PlanNotWritableError, match="was built from extraction row"):
        validate_plan_for_write(plan)


def test_a_replay_pair_is_accepted():
    c = candidate()
    plan = build_classification_plan([c], existing_events=[snapshot(c)])

    validate_plan_for_write(plan)


# -- semantic comparison ------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("meeting_id", "OTHER-MEETING"),
        ("supporting_document_id", 2),
        ("span_start", 5),
        ("span_end", 9),
        ("case_number", "CV-1"),
        ("action_verb", "denied"),
        ("outcome", "denied"),
        ("outcome", "sanctioned"),
    ),
)
def test_stored_event_matches_requires_exact_semantics(field, value):
    payload = EventAssertion.from_candidate(candidate()).payload

    assert stored_event_matches(stored_row(), payload) is True
    assert stored_event_matches(stored_row(**{field: value}), payload) is False


def test_stored_event_matches_accepts_a_qualified_outcome():
    c = candidate(action_verb="approved_with_conditions")
    payload = EventAssertion.from_candidate(c).payload
    matching = stored_row(
        outcome="approved_with_conditions", action_verb=payload.action_verb
    )

    assert stored_event_matches(matching, payload) is True
    # The base alone is not the same stored outcome.
    assert stored_event_matches(
        dataclasses.replace(matching, outcome="approved"), payload
    ) is False


# -- reconciliation -----------------------------------------------------------


def test_reconciliation_equations_hold_for_each_mode():
    plan = build_classification_plan([candidate()])

    dry = WriteResult(
        events_planned=1, events_inserted=0, event_replay_noops=0,
        extraction_links_planned=1, extraction_links_updated=0,
        extraction_link_replay_noops=0, rows_committed=0,
        rows_rolled_back=0, dry_run=True,
    )
    dry.check_reconciliation(plan)
    assert dry.would_mutate == 2

    live = live_success()
    live.check_reconciliation(plan)
    assert live.rows_reclassified_as_replay == 0
    assert live.succeeded is True

    replayed = dataclasses.replace(
        live, events_inserted=0, event_replay_noops=1,
        extraction_links_updated=0, extraction_link_replay_noops=1,
        rows_committed=0,
    )
    replayed.check_reconciliation(plan)
    assert replayed.rows_reclassified_as_replay == 2

    failed = live_failure()
    failed.check_reconciliation(plan)
    assert failed.succeeded is False
    assert failed.replay_verification_failures == 1


def test_reconciliation_rejects_a_failed_run_claiming_replays():
    """A rolled-back run completed no replay, and must not say otherwise."""
    plan = build_classification_plan([candidate()])
    corrupted = dataclasses.replace(live_failure(), event_replay_noops=1)

    with pytest.raises(ReconciliationError, match="replay no-ops"):
        corrupted.check_reconciliation(plan)


def test_reconciliation_rejects_a_failed_run_claiming_a_commit():
    plan = build_classification_plan([candidate()])
    corrupted = dataclasses.replace(
        live_failure(), rows_committed=1, events_inserted=1
    )

    with pytest.raises(ReconciliationError, match="committed row"):
        corrupted.check_reconciliation(plan)


@pytest.mark.parametrize(
    "corruption",
    (
        {"rows_committed": 99},
        {"rows_rolled_back": 5},
        {"events_inserted": 7},
        {"events_planned": 4},
        {"extraction_links_updated": 3},
        {"extraction_links_planned": 9},
        {"event_replay_noops": 4},
        {"replay_verification_failures": 2},
    ),
)
def test_reconciliation_rejects_corrupted_results(corruption):
    plan = build_classification_plan([candidate()])
    corrupted = dataclasses.replace(live_success(), **corruption)

    with pytest.raises(ReconciliationError):
        corrupted.check_reconciliation(plan)


def test_reconciliation_rejects_a_dry_run_that_wrote():
    plan = build_classification_plan([candidate()])
    corrupted = dataclasses.replace(
        live_success(), dry_run=True, rows_rolled_back=0
    )

    with pytest.raises(ReconciliationError):
        corrupted.check_reconciliation(plan)
