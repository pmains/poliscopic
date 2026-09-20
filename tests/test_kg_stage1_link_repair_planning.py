"""Tests for the Stage 1 extraction-link repair planning rules.

These cover the pure decision logic the planner uses, including the condition that
stopped the real plan: a correct later-pass target that is already held by a
*duplicate extraction* of the same evidence identity.  No database is touched.
"""

from __future__ import annotations

from scripts.entities.event_link_storage import confidence_increases, storage_confidence
from scripts.entities.stage1_link_repair_planning import (
    classify_participant_conflict,
    conflict_rule_fits,
    duplicate_proposed_targets,
    effective_confidence_updates,
    holder_conflicts,
    operations_are_executable,
    participant_conflicts,
    plan_preconditions_hold,
    resolve_conflict_max_confidence,
    shared_identity_holders,
    stale_baseline_keys,
    unique_later_pass_target,
)

LATER = "2026-08-25"
EARLIER = "2026-07-27"


def candidate(event_id: int, pass_date: str) -> dict:
    return {"event_id": event_id, "pass_date": pass_date}


# -- target selection ----------------------------------------------------

def test_selects_the_single_later_pass_candidate():
    pool = [candidate(4088, EARLIER), candidate(23632, LATER)]
    assert unique_later_pass_target(pool, LATER) == 23632


def test_ignores_earlier_pass_candidates():
    assert unique_later_pass_target([candidate(4088, EARLIER)], LATER) is None


def test_two_later_pass_candidates_is_ambiguous():
    pool = [candidate(10, LATER), candidate(11, LATER)]
    assert unique_later_pass_target(pool, LATER) is None


def test_no_candidates_is_ambiguous():
    assert unique_later_pass_target([], LATER) is None


# -- collision detection -------------------------------------------------

def test_duplicate_targets_are_detected():
    assert duplicate_proposed_targets([10, 11, 10, 10]) == [10]


def test_unique_targets_report_no_collision():
    assert duplicate_proposed_targets([10, 11, 12]) == []


def test_holder_conflict_is_a_non_moving_holder():
    """The real shape: 24195 wants 23632, which 43739 still holds."""
    proposed = {24195: 23632}
    holders = {23632: [43739]}
    assert holder_conflicts(proposed, holders, moving={24195}) == [43739]


def test_no_conflict_when_every_holder_is_repointed():
    """A pure rotation closes: each holder is itself moving away."""
    proposed = {24195: 23632, 43739: 30000}
    holders = {23632: [43739], 30000: [43739]}
    assert holder_conflicts(proposed, holders, moving=set(proposed)) == []


def test_holder_conflict_detects_shared_event():
    proposed = {1: 100, 2: 100}
    holders = {100: [3]}
    assert holder_conflicts(proposed, holders, moving={1, 2}) == [3]


# -- executability -------------------------------------------------------

def test_executable_for_a_closed_rotation():
    proposed = {24195: 23632, 43739: 30000}
    holders = {23632: [43739], 30000: [43739]}
    assert operations_are_executable(proposed, holders) is True


def test_not_executable_when_target_is_held_by_a_duplicate_extraction():
    """Repointing would leave two extractions sharing one event."""
    proposed = {24195: 23632}
    holders = {23632: [43739]}
    assert operations_are_executable(proposed, holders) is False


def test_not_executable_when_two_extractions_share_one_target():
    proposed = {24195: 23632, 24196: 23632}
    assert operations_are_executable(proposed, {23632: []}) is False


# -- duplicate-extraction identification --------------------------------

def test_shared_identity_holder_is_identified_as_a_duplicate():
    identity = ("doc-111310", "received and filed", 627, 645, None)
    identities = {43739: identity, 24195: identity}
    found = shared_identity_holders(23632, {23632: [43739]}, identities,
                                    identity, exclude=24195)
    assert found == [43739]


def test_unrelated_holder_is_not_a_duplicate():
    mine = ("doc-111310", "received and filed", 627, 645, None)
    theirs = ("doc-111310", "approved", 900, 920, None)
    identities = {99: theirs}
    assert shared_identity_holders(23632, {23632: [99]}, identities, mine,
                                   exclude=24195) == []


def test_real_stage1_shape_stops_the_plan():
    """End-to-end shape of the audited condition, using the real ids."""
    pool = [candidate(4088, EARLIER), candidate(23632, LATER)]
    target = unique_later_pass_target(pool, LATER)
    assert target == 23632
    proposed = {24195: target}
    holders = {23632: [43739]}
    assert operations_are_executable(proposed, holders) is False
    assert holder_conflicts(proposed, holders, moving={24195}) == [43739]


# -- participant conflict detection -------------------------------------

def test_identical_participant_sets_conflict_nowhere():
    shared = {(1, "presenter", 0.5), (2, "staff", 0.2)}
    assert participant_conflicts([shared, set(shared)]) == []


def test_same_entity_and_role_with_different_confidence_is_a_conflict():
    survivor = {(1, "presenter", 0.5)}
    retired = {(1, "presenter", 0.9)}
    assert participant_conflicts([survivor, retired]) == [(1, "presenter")]


def test_union_only_addition_is_not_a_conflict():
    """An entity present in one set only is preserved, not disputed."""
    survivor = {(1, "presenter", 0.5)}
    retired = {(1, "presenter", 0.5), (2, "staff", 0.2)}
    assert participant_conflicts([survivor, retired]) == []


def test_same_entity_different_role_is_not_a_conflict():
    survivor = {(1, "presenter", 0.5)}
    retired = {(1, "staff", 0.9)}
    assert participant_conflicts([survivor, retired]) == []


def test_conflicts_are_reported_sorted_and_complete():
    survivor = {(9, "staff", 0.1), (3, "presenter", 0.2)}
    retired = {(9, "staff", 0.4), (3, "presenter", 0.7)}
    assert participant_conflicts([survivor, retired]) == [(3, "presenter"), (9, "staff")]


# -- plan preconditions --------------------------------------------------

def test_preconditions_hold_when_population_reconciles_and_nothing_is_unhandled():
    proof = {
        "components_reconciles": True,
        "member_events_reconciles": True,
        "unhandled_safe_components": 0,
        "zero_effects_outside_components": True,
    }
    assert plan_preconditions_hold(proof) is True


def test_preconditions_fail_on_unhandled_safe_component():
    proof = {
        "components_reconciles": True,
        "member_events_reconciles": True,
        "unhandled_safe_components": 1,
        "zero_effects_outside_components": True,
    }
    assert plan_preconditions_hold(proof) is False


def test_preconditions_fail_when_population_does_not_reconcile():
    proof = {
        "components_reconciles": False,
        "member_events_reconciles": True,
        "unhandled_safe_components": 0,
        "zero_effects_outside_components": True,
    }
    assert plan_preconditions_hold(proof) is False


def test_preconditions_fail_on_effects_outside_components():
    proof = {
        "components_reconciles": True,
        "member_events_reconciles": True,
        "unhandled_safe_components": 0,
        "zero_effects_outside_components": False,
    }
    assert plan_preconditions_hold(proof) is False


# -- participant conflict classification ---------------------------------

def test_absent_provenance_blocks_automatic_adjudication():
    """Schemas without participant provenance can never auto-pick a confidence."""
    label, reason = classify_participant_conflict(event_level_basis_identical=True,
                                                  participant_provenance_recorded=False)
    assert label == "requires_individual_adjudication"
    assert "absent" in reason


def test_absent_provenance_blocks_even_when_basis_differs():
    label, _ = classify_participant_conflict(event_level_basis_identical=False,
                                             participant_provenance_recorded=False)
    assert label == "requires_individual_adjudication"


def test_recorded_identical_basis_recommends_maximum_confidence():
    label, reason = classify_participant_conflict(event_level_basis_identical=True,
                                                  participant_provenance_recorded=True)
    assert label == "proposed_retain_max_confidence"
    assert "identical" in reason


def test_recorded_but_differing_basis_requires_adjudication():
    label, reason = classify_participant_conflict(event_level_basis_identical=False,
                                                  participant_provenance_recorded=True)
    assert label == "requires_individual_adjudication"
    assert "differs" in reason


# -- stale baseline gate --------------------------------------------------

def test_matching_baselines_report_no_staleness():
    baseline = {"meeting_events": 39132, "public_bodies": 287}
    assert stale_baseline_keys(baseline, dict(baseline), dict(baseline)) == []


def test_plan_mismatch_is_stale():
    plan = {"public_bodies": 287}
    restored = {"public_bodies": 284}
    assert stale_baseline_keys(plan, restored, dict(restored)) == ["public_bodies"]


def test_development_mismatch_is_stale():
    plan = {"meeting_events": 39132}
    dev = {"meeting_events": 39132, "meetings_null_public_body": 1428}
    restored = {"meeting_events": 39132, "meetings_null_public_body": 1483}
    assert stale_baseline_keys(plan, restored, dev) == ["meetings_null_public_body"]


def test_schema_error_marks_the_dump_stale():
    """A column that does not exist proves the dump predates a migration."""
    plan = {"meeting_events": 39132}
    restored = {"meeting_events": 39132,
                "__schema_errors": {"extractions_quarantined": "UndefinedColumn"}}
    assert stale_baseline_keys(plan, restored, dict(plan)) == ["extractions_quarantined"]


def test_key_absent_from_the_plan_is_not_a_mismatch_when_it_agrees():
    """A count the plan never recorded must not masquerade as staleness."""
    plan = {"meeting_events": 39132}
    dev = {"meeting_events": 39132, "agenda_items": 116429}
    restored = {"meeting_events": 39132, "agenda_items": 116429}
    assert stale_baseline_keys(plan, restored, dev) == []


# -- pragmatic maximum-confidence rule ------------------------------------

def test_max_confidence_retains_the_larger_score():
    assert resolve_conflict_max_confidence([0.13333334, 0.15]) == storage_confidence(0.15)


def test_max_confidence_is_order_independent():
    assert resolve_conflict_max_confidence([0.15, 0.13333334]) == storage_confidence(0.15)


def test_max_confidence_ignores_absent_values():
    assert resolve_conflict_max_confidence([None, 0.2, None]) == storage_confidence(0.2)


def test_max_confidence_returns_none_without_values():
    assert resolve_conflict_max_confidence([]) is None
    assert resolve_conflict_max_confidence([None]) is None


def test_conflict_rule_fits_when_key_present_in_every_member():
    layers = [{1: {"presenter": 0.13}}, {1: {"presenter": 0.15}}]
    assert conflict_rule_fits([(1, "presenter")], layers) is True


def test_conflict_rule_does_not_fit_when_a_role_is_missing():
    """A member lacking the (entity, role) keeps an exception."""
    layers = [{1: {"presenter": 0.13}}, {1: {"staff": 0.5}}]
    assert conflict_rule_fits([(1, "presenter")], layers) is False


def test_conflict_rule_does_not_fit_without_conflicts():
    assert conflict_rule_fits([], [{1: {"presenter": 0.1}}]) is False


def test_conflict_rule_fits_a_triple_when_all_three_carry_the_role():
    layers = [{2: {"staff": 0.1}}, {2: {"staff": 0.2}}, {2: {"staff": 0.3}}]
    assert conflict_rule_fits([(2, "staff")], layers) is True


# -- canonical float4 semantics (reuses the participant storage abstraction) ----

def test_float8_to_float4_rounding_is_the_storage_value():
    """0.1 is not representable in float4; the stored value is the rounded one."""
    assert storage_confidence(0.1) == 0.10000000149011612
    assert storage_confidence(0.1) != 0.1


def test_equality_after_storage_is_not_an_increase():
    stored = storage_confidence(0.15)
    assert confidence_increases(stored, stored) is False


def test_strictly_greater_is_an_increase():
    assert confidence_increases(storage_confidence(0.2), storage_confidence(0.15)) is True


def test_smaller_value_is_not_an_increase():
    assert confidence_increases(storage_confidence(0.1), storage_confidence(0.15)) is False


def test_max_selection_is_float4_normalized():
    """The retained score is the value the database would actually store."""
    result = resolve_conflict_max_confidence([1 / 3, 0.3])
    assert result == storage_confidence(1 / 3)


def test_effective_updates_drop_unchanged_stored_values():
    """An already-stored value is a replay, so it is not a row touch."""
    stored = storage_confidence(0.15)
    resolutions = [
        {"survivor_id": 10, "entity_id": 1, "role_in_event": "presenter",
         "retained_confidence": stored},
        {"survivor_id": 11, "entity_id": 2, "role_in_event": "staff",
         "retained_confidence": storage_confidence(0.3)},
    ]
    current = {(10, 1, "presenter"): stored, (11, 2, "staff"): storage_confidence(0.2)}
    effective = effective_confidence_updates(resolutions, current)
    assert [r["survivor_id"] for r in effective] == [11]


def test_effective_updates_are_empty_when_nothing_improves():
    stored = storage_confidence(0.15)
    resolutions = [{"survivor_id": 10, "entity_id": 1, "role_in_event": "presenter",
                    "retained_confidence": stored}]
    assert effective_confidence_updates(
        resolutions, {(10, 1, "presenter"): stored}) == []


def test_effective_updates_skip_rows_with_no_current_value():
    resolutions = [{"survivor_id": 10, "entity_id": 1, "role_in_event": "presenter",
                    "retained_confidence": storage_confidence(0.5)}]
    assert effective_confidence_updates(resolutions, {}) == []


def test_planned_and_executed_counts_share_one_predicate():
    """The exact invariant the accounting discrepancy violated.

    The plan and the executor both call ``effective_confidence_updates`` with the
    same current values, so the predicted count equals the executed count even when
    the candidates are float8 values that round into equal float4 values.
    """
    stored_a = storage_confidence(0.15)
    stored_b = storage_confidence(0.10909091)
    resolutions = [
        {"survivor_id": 1, "entity_id": 1, "role_in_event": "presenter",
         "retained_confidence": stored_a},
        {"survivor_id": 2, "entity_id": 2, "role_in_event": "presenter",
         "retained_confidence": stored_b},
    ]
    current = {(1, 1, "presenter"): stored_a, (2, 2, "presenter"): stored_b}
    planned = effective_confidence_updates(resolutions, current)
    executed = effective_confidence_updates(resolutions, dict(current))
    assert len(planned) == len(executed) == 0
