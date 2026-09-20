"""Pure-layer tests for assertion planning, accounting, and invariants.

No database, no pipeline, no subprocess.  Everything here is in-memory.
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib

import pytest

from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.entities.event_normalize_planning import (
    ClassificationPlan,
    EventAssertion,
    ExtractionLinkAssertion,
    InconsistentAssertion,
    PlanInvariantError,
    build_classification_plan,
)
from scripts.entities.event_normalize_snapshot import ExistingNormalizedEvent
from scripts.entities.event_normalize_work_items import (
    NormalizationWorkItem,
    WorkItemError,
    build_plan_from_work_items,
)

ENTITIES_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "entities"


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


# -- occurrences and identities ----------------------------------------------


def test_distinct_extractions_produce_distinct_event_identities():
    first = candidate(extraction_id=1)
    second = candidate(extraction_id=2)

    assert first.occurrence != second.occurrence
    assert (
        EventAssertion.from_candidate(first).identity.digest
        != EventAssertion.from_candidate(second).identity.digest
    )


def test_two_verbs_in_one_meeting_are_distinct_events():
    approved = candidate(extraction_id=1, action_verb="approved")
    denied = candidate(extraction_id=2, action_verb="denied")
    assert (
        EventAssertion.from_candidate(approved).identity.digest
        != EventAssertion.from_candidate(denied).identity.digest
    )


def test_unchanged_replay_reproduces_the_same_identities():
    first = candidate()
    second = candidate()

    assert first.occurrence == second.occurrence
    assert first.evidence_identity.digest == second.evidence_identity.digest
    assert first.extraction_identity.digest == second.extraction_identity.digest
    assert (
        EventAssertion.from_candidate(first).identity.digest
        == EventAssertion.from_candidate(second).identity.digest
    )


# -- accounting ---------------------------------------------------------------


def test_new_unlinked_extraction_is_one_insert_and_one_update():
    plan = plan_for(candidate())

    assert plan.total_proposed == 2
    assert plan.total_would_insert == 1
    assert plan.total_would_update == 1
    assert plan.total_replay_noop == 0
    assert plan.total_inconsistent == 0
    assert len(plan.event_inserts) == 1
    assert len(plan.link_updates) == 1
    plan.check_invariants()


def test_plan_arithmetic_invariants_hold():
    a = rich_candidate(extraction_id=1)
    b = rich_candidate(extraction_id=2, action_verb="denied")
    c = rich_candidate(extraction_id=3, action_verb="continued")
    plan = plan_for(a, b, c, existing_events=[stored(a)])

    plan.check_invariants()
    assert plan.total_proposed == 6
    assert plan.total_proposed == (
        plan.total_would_insert
        + plan.total_would_update
        + plan.total_replay_noop
        + plan.total_inconsistent
    )
    assert plan.total_would_insert == 2
    assert plan.total_would_update == 2
    assert plan.total_replay_noop == 2


def test_empty_input_produces_an_empty_consistent_plan():
    plan = build_classification_plan([])
    assert plan.total_proposed == 0
    assert plan.is_consistent is True
    plan.check_invariants()


def test_mixed_plan_accounts_exactly():
    new = rich_candidate(extraction_id=1)
    replay = rich_candidate(extraction_id=2)
    bad = rich_candidate(extraction_id=3)
    plan = plan_for(
        new,
        replay,
        bad,
        existing_events=[stored(replay), stored(bad, case_number="OTHER-1")],
    )

    assert plan.total_proposed == 5
    assert (plan.total_would_insert, plan.total_would_update) == (1, 1)
    assert plan.total_replay_noop == 2
    assert plan.total_inconsistent == 1
    plan.check_invariants()


# -- inconsistency diagnostics ------------------------------------------------


def test_inconsistent_assertion_carries_full_diagnostics():
    c = rich_candidate()
    snapshot = stored(c, case_number="OTHER-1", span_start=5, span_end=10)
    plan = plan_for(c, existing_events=[snapshot])

    bad = plan.inconsistent[0]
    assert bad.extraction_id == c.extraction_id
    assert bad.linked_event_id == snapshot.event_id
    assert set(bad.differing_fields) == {"case_number", "span"}
    assert dict(bad.expected_values)["case_number"] == "CV2024-001"
    assert dict(bad.observed_values)["case_number"] == "OTHER-1"
    assert str(snapshot.event_id) in bad.reason
    assert "case_number" in bad.reason and "span" in bad.reason
    assert bad.expected_event.digest == EventAssertion.from_candidate(c).identity.digest


def test_inconsistency_diagnostics_name_the_exact_meeting_field():
    c = rich_candidate()
    snapshot = stored(c, stored_meeting_source_id="STALE-REF")
    plan = plan_for(c, existing_events=[snapshot])

    bad = plan.inconsistent[0]
    assert bad.differing_fields == ("stored_meeting_source_id",)
    assert dict(bad.expected_values)["stored_meeting_source_id"] == c.meeting_source_id
    assert dict(bad.observed_values)["stored_meeting_source_id"] == "STALE-REF"
    assert "stored_meeting_source_id" in bad.reason
    assert bad.linked_event_id == snapshot.event_id


# -- duplicate candidates -----------------------------------------------------


def test_duplicate_extraction_id_with_identical_payload_collapses():
    c = candidate()
    plan = plan_for(c, c, c)

    assert plan.total_proposed == 2
    assert plan.total_would_insert == 1
    assert plan.total_would_update == 1
    plan.check_invariants()

    assert plan_for(c, c, existing_events=[stored(c)]).total_replay_noop == 2


def test_duplicate_extraction_id_with_conflicting_payload_fails_closed():
    first = candidate(action_verb="approved")
    second = candidate(action_verb="denied")

    with pytest.raises(PlanInvariantError, match="conflicting"):
        plan_for(first, second)


def test_duplicate_extraction_id_with_changed_content_hash_fails_closed():
    first = candidate(content_hash="hash-a")
    second = candidate(content_hash="hash-b")

    with pytest.raises(PlanInvariantError, match="conflicting"):
        plan_for(first, second)


def test_duplicate_stored_snapshot_fails_closed():
    c = candidate()
    with pytest.raises(PlanInvariantError, match="duplicate stored snapshot"):
        plan_for(c, existing_events=[stored(c), stored(c)])


# -- ordering -----------------------------------------------------------------


def test_input_order_does_not_affect_the_plan():
    a = rich_candidate(extraction_id=1)
    b = rich_candidate(extraction_id=2, action_verb="denied")
    snapshots = [stored(a)]

    forward = plan_for(a, b, existing_events=snapshots)
    backward = plan_for(b, a, existing_events=snapshots)

    assert forward == backward
    assert forward.total_proposed == 4
    assert forward.total_replay_noop == 2
    assert forward.total_would_insert == 1
    assert forward.total_would_update == 1


# -- invariant detection ------------------------------------------------------


def test_assertion_in_two_buckets_is_detected():
    assertion = EventAssertion.from_candidate(candidate())
    broken = ClassificationPlan(
        event_inserts=(assertion,), event_replay_noops=(assertion,),
    )
    with pytest.raises(PlanInvariantError, match="classified twice"):
        broken.check_invariants()


def test_inconsistent_assertion_may_not_also_be_planned():
    c = candidate()
    link = ExtractionLinkAssertion.from_candidate(c)
    broken = ClassificationPlan(
        link_updates=(link,),
        inconsistent=(InconsistentAssertion(
            extraction_id=c.extraction_id,
            extraction_identity=link.extraction_identity,
            linked_event_id=1,
            expected_event=link.expected_event,
            differing_fields=("case_number",),
            expected_values=(("case_number", None),),
            observed_values=(("case_number", "X"),),
            reason="test",
        ),),
    )
    with pytest.raises(PlanInvariantError, match="inconsistent assertions also present"):
        broken.check_invariants()


def test_one_identity_with_two_payloads_is_detected():
    """Same identity, different payload -- must not silently deduplicate."""
    c = candidate()
    base = EventAssertion.from_candidate(c)

    mutated_payload = dataclasses.replace(base.payload, case_number="OTHER-1")
    assert mutated_payload.signature != base.payload.signature
    other = dataclasses.replace(base, payload=mutated_payload)
    assert other.identity.digest == base.identity.digest
    assert other.key != base.key

    broken = ClassificationPlan(event_inserts=(base, other))
    with pytest.raises(PlanInvariantError, match="conflicting payloads"):
        broken.check_invariants()


def test_event_assertion_exposes_its_semantic_payload():
    c = rich_candidate()
    assertion = EventAssertion.from_candidate(c)

    assert assertion.payload.event_type == c.event_type
    assert assertion.payload.outcome_base == c.outcome.base
    assert assertion.payload.outcome_qualifier == c.outcome.qualifier
    assert assertion.payload.meeting_db_id == c.meeting_db_id
    assert assertion.payload.meeting_source_id == c.meeting_source_id
    assert assertion.payload.occurrence == c.occurrence
    assert assertion.key == (assertion.identity.digest, assertion.payload.signature)


# -- API surface and purity ---------------------------------------------------


def test_no_opaque_digest_only_replay_api_remains():
    params = inspect.signature(build_classification_plan).parameters
    assert list(params) == ["candidates", "existing_events"]
    assert "existing_links" not in params

    source = (ENTITIES_DIR / "event_normalize_planning.py").read_text(encoding="utf-8")
    assert "existing_links" not in source

    c = candidate()
    with pytest.raises((TypeError, AttributeError, PlanInvariantError)):
        build_classification_plan([c], existing_events={c.extraction_id: "digest"})


@pytest.mark.parametrize(
    "name",
    (
        "event_normalize_models.py",
        "event_normalize_snapshot.py",
        "event_normalize_planning.py",
        "event_normalize_work_items.py",
    ),
)
def test_layer_is_pure(name):
    source = (ENTITIES_DIR / name).read_text(encoding="utf-8")
    for forbidden in (
        "import sqlalchemy", "from sqlalchemy",
        "import psycopg", "from psycopg",
        "import subprocess", "from subprocess",
        "create_engine(", "cursor(", "execute(",
        "EmissionValidator(", "ValidationReceipt(", "classify_rows(",
    ):
        assert forbidden not in source, f"{name} references {forbidden}"


# -- planning bridge ----------------------------------------------------------


def unlinked(c) -> NormalizationWorkItem:
    return NormalizationWorkItem(candidate=c)


def linked(c, **overrides) -> NormalizationWorkItem:
    return NormalizationWorkItem(candidate=c, existing_event=stored(c, **overrides))


def test_bridge_plans_mixed_linked_and_unlinked_items():
    new = rich_candidate(extraction_id=1)
    unchanged = rich_candidate(extraction_id=2)
    changed = rich_candidate(extraction_id=3)

    plan = build_plan_from_work_items([
        unlinked(new),
        linked(unchanged),
        linked(changed, case_number="OTHER-1"),
    ])

    assert plan.total_proposed == 5
    assert plan.total_would_insert == 1
    assert plan.total_would_update == 1
    assert plan.total_replay_noop == 2
    assert plan.total_inconsistent == 1
    plan.check_invariants()


def test_bridge_unchanged_linked_item_is_two_replay_noops():
    plan = build_plan_from_work_items([linked(rich_candidate())])

    assert plan.total_replay_noop == 2
    assert plan.total_would_insert == 0
    assert plan.total_would_update == 0
    assert plan.total_inconsistent == 0
    assert plan.is_consistent is True


def test_bridge_changed_linked_item_is_inconsistent():
    c = rich_candidate()
    plan = build_plan_from_work_items([linked(c, case_number="OTHER-1")])

    assert plan.total_inconsistent == 1
    assert plan.is_consistent is False
    assert set(plan.inconsistent[0].differing_fields) == {"case_number"}
    assert plan.inconsistent[0].linked_event_id is not None


def test_bridge_unlinked_item_inserts_and_links():
    c = candidate()
    plan = build_plan_from_work_items([unlinked(c)])

    assert plan.total_would_insert == 1
    assert plan.total_would_update == 1
    assert plan.total_replay_noop == 0
    assert plan.total_inconsistent == 0
    assert plan.link_updates[0].expected_event.digest == (
        EventAssertion.from_candidate(c).identity.digest
    )


def test_bridge_rejects_duplicate_extraction_ids():
    c = candidate(extraction_id=4)
    with pytest.raises(WorkItemError, match="duplicate work item"):
        build_plan_from_work_items([unlinked(c), linked(c)])


def test_bridge_input_order_does_not_change_the_plan():
    a = rich_candidate(extraction_id=1)
    b = rich_candidate(extraction_id=2, action_verb="denied")
    c = rich_candidate(extraction_id=3)

    forward = build_plan_from_work_items(
        [unlinked(a), linked(b), linked(c, case_number="X")]
    )
    backward = build_plan_from_work_items(
        [linked(c, case_number="X"), linked(b), unlinked(a)]
    )

    assert forward == backward
    assert forward.total_would_insert == 1
    assert forward.total_would_update == 1
    assert forward.total_replay_noop == 2
    assert forward.total_inconsistent == 1


def test_bridge_cannot_omit_a_candidate_or_a_snapshot():
    new = rich_candidate(extraction_id=1)
    replay = rich_candidate(extraction_id=2)
    bad = rich_candidate(extraction_id=3)
    items = [unlinked(new), linked(replay), linked(bad, case_number="OTHER-1")]

    plan = build_plan_from_work_items(items)

    # Every candidate reaches the planner, and so does every stored snapshot.
    planned_events = {
        a.identity.digest
        for a in (*plan.event_inserts, *plan.event_replay_noops)
    }
    assert planned_events == {
        EventAssertion.from_candidate(new).identity.digest,
        EventAssertion.from_candidate(replay).identity.digest,
    }
    assert [i.extraction_id for i in plan.link_updates] == [1]
    assert [i.extraction_id for i in plan.link_replay_noops] == [2]
    assert [i.extraction_id for i in plan.inconsistent] == [3]
    assert plan.inconsistent[0].linked_event_id is not None

    # The bridge is exactly hand-separation, through the same planner.
    direct = build_classification_plan(
        [new, replay, bad],
        existing_events=[stored(replay), stored(bad, case_number="OTHER-1")],
    )
    assert plan == direct
