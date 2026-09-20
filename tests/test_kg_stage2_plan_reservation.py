#!/usr/bin/env python3
"""Adversarial tests for the regenerated correction/repair plans.

Each safety property gets an attack: stale code, a stale schema signature, a stale
current-state binding, a missing or duplicated reservation operation, a reservation
not bound to the plan digest, a held key carrying a digest, an occupied key treated
as free, the repair plan claiming to be applyable before correction, and a
transaction model that skips a step or drops the rollback/replay contract.
"""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_label_correction as LC  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as B  # noqa: E402
from scripts.kg import stage2_s2_repair_plan as RP  # noqa: E402
from scripts.kg import stage2_s2_reservation_binding as RB  # noqa: E402
from tests.kg_stage2_test_fixtures import code_current_copy  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


def correction():
    return code_current_copy(artifacts.load_verified(
        _live("kg-stage2-s2-label-correction-plan-*.json")))


def repair():
    return code_current_copy(artifacts.load_verified(
        _live("kg-stage2-s2-repair-plan-*.json")))


def recorded_correction():
    """The immutable artifact, for assertions about historical ancestry."""
    return artifacts.load_verified(
        _live("kg-stage2-s2-label-correction-plan-*.json"))


# ══ both plans canonically validate ════════════════════════════════════

def test_a_current_code_copy_of_the_recorded_correction_plan_validates():
    assert LC.validate_plan(correction()) == []


def test_a_current_code_copy_of_the_recorded_repair_plan_validates():
    assert RP.validate_plan(repair()) == []


def test_both_plans_bind_the_complete_settled_code_set():
    for plan in (correction(), repair()):
        recorded = set(plan["bindings"]["code_hashes"])
        # The v2 repair contract binds a SUPERSET (it adds the schema-signature modules);
        # the applied correction keeps exactly the v1 set it was built with.
        assert set(B.CODE_MODULES) <= recorded
        assert set(B.EXECUTION_MODULES) <= recorded


def test_both_plans_bind_the_reservation_modules():
    for plan in (correction(), repair()):
        recorded = set(plan["bindings"]["code_hashes"])
        for required in ("scripts/kg/stage2_reservation.py",
                         "scripts/kg/stage2_s2_reservation_binding.py",
                         "scripts/kg/stage2_s2_collision.py"):
            assert required in recorded, required


def test_both_plans_bind_the_backup_and_the_reservation_receipt():
    for plan in (correction(), repair()):
        bindings = plan["bindings"]
        assert bindings["backup"]["canonical_digest"]
        receipt = bindings["reservation_receipt"]
        assert receipt["path"] and receipt["digest"]
        assert len(receipt["digest"]) == 64


# ══ the governed contract replaced the impossible index ════════════════

def test_both_plans_bind_the_governed_reservation_contract():
    for plan in (correction(), repair()):
        control = plan["bindings"]["collision_control"]
        assert control["mode"] == "exact_key_reservation"
        assert control["table"] == "agenda_item_key_reservation"
        assert control["primary_key"] == ["meeting_db_id", "agenda_item_number"]
        assert control["historical_key_is_unique"] is False
        assert control["advisory_locking"] is True
        assert len(control["signature_digest"]) == 64


def test_a_plan_without_the_governed_control_is_refused():
    data = correction()
    data["bindings"]["collision_control"] = None
    assert any("collision control" in p for p in LC.validate_plan(data))


def test_a_plan_claiming_the_historical_key_is_unique_is_refused():
    data = correction()
    data["bindings"]["collision_control"]["historical_key_is_unique"] = True
    assert LC.validate_plan(data)


def test_a_plan_without_a_reservation_receipt_is_refused():
    data = correction()
    data["bindings"]["reservation_receipt"] = {}
    assert any("reservation schema receipt" in p for p in LC.validate_plan(data))


# ══ every proposed key has exactly one reservation operation ═══════════

def test_every_proposed_key_has_exactly_one_operation():
    for plan, role, validator in ((correction(), "correction", LC.validate_plan),
                                  (repair(), "repair", RP.validate_plan)):
        binding = plan["reservation_operations"]
        proposed = RB.proposed_keys(plan, role=role)
        assert len(binding["operations"]) == len(proposed)
        assert len({o["key"] for o in binding["operations"]}) == len(proposed)
        assert validator(plan) == []


def test_the_outcome_counts_reconcile():
    for plan in (correction(), repair()):
        binding = plan["reservation_operations"]
        counts = binding["counts"]
        assert sum(counts.values()) == len(binding["operations"])
        for outcome in RB.OUTCOMES:
            assert counts[outcome] == sum(
                1 for o in binding["operations"] if o["outcome"] == outcome)


def test_every_proposed_key_is_free_and_reserved():
    for plan in (correction(), repair()):
        counts = plan["reservation_operations"]["counts"]
        assert counts["hold_occupied"] == 0, "an occupied key must not be proposed"
        assert counts["hold_reserved"] == 0
        assert counts["reserve"] > 0


def test_a_dropped_reservation_operation_is_refused():
    for data, validator in ((correction(), LC.validate_plan), (repair(), RP.validate_plan)):
        trimmed = copy.deepcopy(data)
        trimmed["reservation_operations"]["operations"] = \
            trimmed["reservation_operations"]["operations"][:-1]
        assert any("do not cover" in p for p in validator(trimmed))


def test_a_duplicated_reservation_operation_is_refused():
    data = correction()
    operations = data["reservation_operations"]["operations"]
    data["reservation_operations"]["operations"] = operations + [operations[0]]
    assert any("more than one reservation operation" in p
               for p in LC.validate_plan(data))


def test_a_reservation_not_bound_to_the_plan_digest_is_refused():
    data = correction()
    for operation in data["reservation_operations"]["operations"]:
        if operation["outcome"] == "reserve":
            operation["plan_digest"] = "0" * 64
            break
    assert any("not bound to the plan digest" in p for p in LC.validate_plan(data))


def test_a_held_key_carrying_a_plan_digest_is_refused():
    data = correction()
    operation = data["reservation_operations"]["operations"][0]
    operation["outcome"] = "hold_reserved"
    operation["plan_digest"] = data["reservation_operations"]["plan_digest"]
    assert any("held key carries a plan digest" in p for p in LC.validate_plan(data))


def test_an_unregistered_outcome_is_refused():
    data = correction()
    data["reservation_operations"]["operations"][0]["outcome"] = "maybe"
    assert any("unregistered outcome" in p for p in LC.validate_plan(data))


def test_a_forged_reserve_key_digest_is_refused():
    data = correction()
    data["reservation_operations"]["reserve_key_digest"] = "0" * 64
    assert any("reserve-key digest" in p for p in LC.validate_plan(data))


def test_an_occupied_key_is_held_rather_than_reserved():
    """The rule, exercised directly: occupancy becomes a hold, never an overwrite."""
    plan = correction()
    keys = RB.proposed_keys(plan, role="correction")
    occupied = [RB.reservation.reservation_key(keys[0]["meeting_db_id"],
                                               keys[0]["agenda_item_number"])]
    binding = RB.reservation_operations(plan, role="correction",
                                        plan_digest="d" * 64,
                                        live_keys=occupied, reserved_keys=[])
    held = [o for o in binding["operations"] if o["outcome"] == "hold_occupied"]
    assert len(held) == 1 and held[0]["plan_digest"] is None
    assert held[0]["reason"]


def test_an_already_reserved_key_is_held_rather_than_reserved():
    plan = correction()
    keys = RB.proposed_keys(plan, role="correction")
    reserved = [RB.reservation.reservation_key(keys[0]["meeting_db_id"],
                                               keys[0]["agenda_item_number"])]
    binding = RB.reservation_operations(plan, role="correction",
                                        plan_digest="d" * 64,
                                        live_keys=[], reserved_keys=reserved)
    held = [o for o in binding["operations"] if o["outcome"] == "hold_reserved"]
    assert len(held) == 1 and held[0]["plan_digest"] is None


def test_operations_are_derived_from_the_plan_only():
    """A key the plan does not propose cannot appear in its operations."""
    for data, role in ((correction(), "correction"), (repair(), "repair")):
        proposed = {RB.reservation.reservation_key(e["meeting_db_id"],
                                                   e["agenda_item_number"])
                    for e in RB.proposed_keys(data, role=role)}
        bound = {o["key"] for o in data["reservation_operations"]["operations"]}
        assert bound == proposed
        assert data["reservation_operations"]["derived_from_the_plan_only"] is True


# ══ the transaction model is explicit and ordered ══════════════════════

def test_both_plans_bind_the_ordered_transaction_model():
    for plan in (correction(), repair()):
        steps = [s["name"] for s in plan["transaction_model"]["steps"]]
        assert steps == [s["name"] for s in RB.TRANSACTION_STEPS]
        assert steps[0] == "advisory_lock"
        assert steps.index("insert_reservation") < steps.index("typed_row_ops")
        assert steps.index("postconditions") < steps.index("receipt")


def test_the_transaction_model_states_its_contracts():
    for plan in (correction(), repair()):
        model = plan["transaction_model"]
        assert model["one_transaction"] is True
        assert model["invariant_carrier"] == "the reservation primary key"
        for field in ("conflict_rule", "receipt_ownership", "replay_rule",
                      "rollback_rule"):
            assert model.get(field)
        assert model["write_path"] == "absent by design"


def test_a_model_with_a_dropped_step_is_refused():
    for data, validator in ((correction(), LC.validate_plan), (repair(), RP.validate_plan)):
        broken = copy.deepcopy(data)
        broken["transaction_model"]["steps"] = broken["transaction_model"]["steps"][:-1]
        assert validator(broken)


def test_a_model_without_a_rollback_rule_is_refused():
    data = correction()
    data["transaction_model"]["rollback_rule"] = ""
    assert LC.validate_plan(data)


def test_a_model_that_is_not_one_transaction_is_refused():
    data = correction()
    data["transaction_model"]["one_transaction"] = False
    assert LC.validate_plan(data)


# ══ ordering: repair depends on the correction postimage ══════════════

def test_the_repair_plan_depends_on_the_exact_correction_plan():
    dependency = repair()["dependency"]
    assert dependency["depends_on"] == "correction"
    named = dependency["correction_plan"]
    assert named["digest"], "the dependency must name the correction plan digest"
    assert named["path"].endswith(".json")
    assert named["replay_digest"]


def test_the_repair_plan_does_not_claim_to_be_applyable_now():
    dependency = repair()["dependency"]
    assert dependency["currently_applyable"] is False
    assert dependency["why_not_applyable_now"]
    assert "correction" in dependency["why_not_applyable_now"]


def test_the_repair_plan_forbids_reusing_its_digest_after_correction():
    dependency = repair()["dependency"]
    assert "NEVER reuse" in dependency["reuse_rule"]
    assert "regenerate" in dependency["reuse_rule"].lower()
    assert dependency["state_binding"]


def test_the_repair_plan_carries_a_regeneration_recipe_not_an_invented_digest():
    recipe = repair()["dependency"]["regeneration_recipe"]
    assert recipe["command"]
    assert recipe["inputs"] and recipe["invariants"]
    assert "invent a digest" in recipe["may_not"]


def test_the_dependency_names_the_plan_that_is_actually_current():
    named = repair()["dependency"]["correction_plan"]
    assert named["digest"] == artifacts.recorded_digest(recorded_correction())


def test_the_two_plans_propose_disjoint_keys():
    """Disjointness is the ordering's safety property, and it is measured."""
    left = {o["key"] for o in correction()["reservation_operations"]["operations"]
            if o["outcome"] == "reserve"}
    right = {o["key"] for o in repair()["reservation_operations"]["operations"]
             if o["outcome"] == "reserve"}
    assert left and right
    assert not (left & right)


# ══ stale code / schema / state ════════════════════════════════════════

def test_stale_code_hashes_are_refused():
    for data, validator in ((correction(), LC.validate_plan), (repair(), RP.validate_plan)):
        broken = copy.deepcopy(data)
        for relative in broken["bindings"]["code_hashes"]:
            broken["bindings"]["code_hashes"][relative] = "0" * 64
        assert any("code has drifted" in p for p in validator(broken))


def test_a_missing_code_module_is_refused():
    data = correction()
    data["bindings"]["code_hashes"].pop("scripts/kg/stage2_s2_collision.py")
    assert any("do not cover" in p for p in LC.validate_plan(data))


def test_a_stale_schema_signature_is_refused():
    data = correction()
    data["bindings"]["schema_signature"] = {}
    control = data["bindings"]["collision_control"]
    control["signature_digest"] = ""
    assert LC.validate_plan(data)


def test_a_stale_current_state_binding_is_refused():
    data = correction()
    data["bindings"]["current_state_sha256"] = "0" * 64
    assert LC.validate_plan(data)


def test_the_applied_correction_keeps_its_historical_state_digest():
    """The two digests must DIFFER once the correction has been applied.

    The correction artifact is an immutable record of the pre-state it was reviewed
    against; the repair plan is bound to the post-correction live state.  Requiring them
    to be equal would force the repair plan to reason about a world the correction has
    already changed - which is precisely the stale-state defect this guards against.
    """
    applied = correction()["bindings"]["current_state_sha256"]
    current = repair()["bindings"]["current_state_sha256"]
    assert applied and current
    assert applied != current


def test_the_plans_record_no_write_path():
    for plan in (correction(), repair()):
        assert plan["mode"] == "dry-run"
        assert plan["applied"] is False and plan["promoted"] is False
        assert plan["transaction_model"]["write_path"] == "absent by design"
