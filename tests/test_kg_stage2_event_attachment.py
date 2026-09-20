#!/usr/bin/env python3
"""Event → agenda-item attachment: eligibility, accounting, plan."""

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
from scripts.kg import stage2_event_attachment as EA  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as B  # noqa: E402

_PLAN = _REPO / "data" / "kg-plans" / \
    "kg-stage2-event-attachment-plan-8121338e93ba1ee6.json"
_BASELINE = _REPO / "data" / "kg-plans" / \
    "kg-stage2-event-attachment-baseline-20260913T041500Z.json"


def _event(sd, outcome="approved", case=None, event_id=1):
    return {"id": event_id, "meeting_id": "m1", "supporting_doc_id": sd,
            "case_number": case, "outcome": outcome}


def _classify(event, *, doc_class="meeting_level_only", doc_item=None, case_ids=(),
              meeting=10, document=True):
    return EA.classify_event(
        event, document={"id": 1} if document else None,
        document_class=doc_class, document_item=doc_item,
        case_item_ids=case_ids, meeting_db_id=meeting if document else None)


# ── eligibility rules ──────────────────────────────────────────────────


def test_a_procedural_event_is_ineligible_whatever_else_is_true():
    """Even with perfect item evidence, being called to order cannot attach."""
    verdict = _classify(_event(1, outcome="called_to_order"), doc_class="attached_item_number",
                        doc_item=99)
    assert verdict["class"] == EA.CLASS_INELIGIBLE
    assert verdict["item"] is None


def test_a_source_supported_document_attaches():
    verdict = _classify(_event(1), doc_class="attached_item_number", doc_item=278759)
    assert verdict["class"] == EA.CLASS_DETERMINISTIC and verdict["item"] == 278759
    assert verdict["evidence"]["kind"] == "source_supported_document"


def test_an_exact_unique_case_number_attaches():
    verdict = _classify(_event(1, case="Z-119"), case_ids=[42])
    assert verdict["class"] == EA.CLASS_DETERMINISTIC and verdict["item"] == 42
    assert verdict["evidence"]["kind"] == "exact_case_number"


def test_a_case_number_matching_several_items_is_ambiguous():
    verdict = _classify(_event(1, case="Z-119"), case_ids=[42, 43])
    assert verdict["class"] == EA.CLASS_AMBIGUOUS
    assert "matches 2 items" in verdict["reason"]


def test_a_held_document_is_ambiguous_rather_than_meeting_level():
    verdict = _classify(_event(1), doc_class="held_ambiguous")
    assert verdict["class"] == EA.CLASS_AMBIGUOUS


def test_a_meeting_level_document_with_no_case_is_meeting_level():
    verdict = _classify(_event(1))
    assert verdict["class"] == EA.CLASS_MEETING_LEVEL


def test_an_unresolvable_document_is_missing_evidence():
    verdict = _classify(_event(1), document=False)
    assert verdict["class"] == EA.CLASS_MISSING and verdict["item"] is None


def test_a_case_number_matching_nothing_does_not_attach():
    verdict = _classify(_event(1, case="Z-119"), case_ids=[])
    assert verdict["class"] == EA.CLASS_MEETING_LEVEL
    assert "matches no item" in verdict["reason"]


def test_cooccurrence_is_never_evidence():
    """Two events beside an item in one document must not attach to it."""
    a = _classify(_event(1))
    b = _classify(_event(2, event_id=2))
    assert a["class"] == EA.CLASS_MEETING_LEVEL
    assert b["class"] == EA.CLASS_MEETING_LEVEL
    assert a["item"] is None and b["item"] is None


# ── accounting ─────────────────────────────────────────────────────────


def _audit(events):
    docs = {i: {"id": i, "meeting_db_id": 10, "body": "phoenix-cc"} for i in range(1, 100)}
    return EA.audit(
        events, documents=docs,
        document_classes={i: "meeting_level_only" for i in docs},
        document_items={}, meeting_db_id_by_event={int(e["id"]): 10 for e in events},
        case_items={}, body_by_event={int(e["id"]): "phoenix-cc" for e in events})


def test_every_event_is_classified_exactly_once():
    events = [_event(1, event_id=1), _event(1, event_id=2, outcome="called_to_order"),
              _event(1, event_id=3)]
    base = _audit(events)
    assert base["total"] == 3
    assert sum(base["counts"].values()) == 3
    assert base["population"]["reconciles"] is True
    assert len({r["event_id"] for r in base["rows"]}) == 3


def test_the_class_set_is_exactly_the_five_defined_classes():
    base = _audit([_event(1)])
    assert sorted(base["counts"]) == sorted(EA.CLASSES)


def test_the_baseline_slices_reconcile_by_body():
    base = _audit([_event(1, event_id=1), _event(1, event_id=2)])
    for body, slice_counts in base["by_body"].items():
        assert sum(slice_counts.values()) > 0
    assert sum(sum(v.values()) for v in base["by_body"].values()) == base["total"]


def test_every_non_deterministic_class_carries_a_reason():
    base = _audit([_event(1, event_id=1, outcome="called_to_order"), _event(1, event_id=2)])
    for row in base["rows"]:
        if row["class"] != EA.CLASS_DETERMINISTIC:
            assert row["reason"]


# ── the plan ───────────────────────────────────────────────────────────


def _plan(events, decisions=None):
    base = _audit(events)
    base["rows_sha256"] = B.canonical_sha256(base["rows"])
    return EA.build_plan(base, decisions=decisions if decisions is not None else _five(),
                         created_at="t", target={"database": "poliscopic_dev"}), base


def _five():
    return [{"document_id": d, "decision_id": f"d{d}", "decision": "approve",
             "adjudicator": "Peter Mains", "decided_at": "t", "digest": "e" * 64,
             "path": "p", "document_role": "r", "proposal": {"path": "x", "digest": "y"},
             "candidate": {"agenda_item_db_id": 1}, "human_stated_item": "4.I"}
            for d in (107915, 107916, 107917, 107938, 107939)]


def test_the_plan_has_no_write_path_and_is_dry():
    plan, _ = _plan([_event(1)])
    assert plan["mode"] == "dry-run"
    assert plan["write_path"] == "absent by design"
    assert plan["applied"] is False and plan["promoted"] is False


def test_only_deterministic_events_become_operations():
    events = [_event(1, event_id=1), _event(1, event_id=2, outcome="called_to_order")]
    plan, base = _plan(events)
    assert plan["counts"][EA.CLASS_DETERMINISTIC] == 0
    assert plan["operations"] == []
    assert plan["accounting"]["unattached"] == 2


def test_the_operations_equal_the_deterministic_population():
    plan, base = _plan([_event(1)])
    assert plan["accounting"]["set_equality"] is True
    assert plan["accounting"]["baseline_population_sha256"] == \
        plan["accounting"]["classified_event_ids_sha256"]
    assert EA.validate_plan(plan, baseline=base) == []


def test_a_tampered_population_digest_is_refused():
    plan, _ = _plan([_event(1)])
    plan["accounting"]["classified_event_ids_sha256"] = "0" * 64
    assert any("does not match the baseline" in p for p in EA.validate_plan(plan))


def test_a_class_count_that_does_not_reconcile_is_refused():
    plan, _ = _plan([_event(1)])
    plan["counts"][EA.CLASS_MEETING_LEVEL] += 1
    assert any("do not reconcile" in p for p in EA.validate_plan(plan))


def test_the_plan_carries_one_canonical_digest_and_a_separate_replay_digest():
    plan, _ = _plan([_event(1)])
    assert plan["digest"] == artifacts.compute_digest(plan)
    assert plan["replay_digest"] != plan["digest"]
    tampered = copy.deepcopy(plan)
    tampered["counts"][EA.CLASS_MEETING_LEVEL] = 0
    assert tampered["digest"] != artifacts.compute_digest(tampered)


def test_the_plan_binds_lineage_decisions_and_target():
    plan, _ = _plan([_event(1)])
    assert plan["bindings"]["lineage"]["plan"]["digest"]
    assert plan["bindings"]["target"]["database"] == "poliscopic_dev"
    assert len(plan["bindings"]["decisions"]) == 5


def test_rollback_is_receipt_owned_and_replay_claims_no_stability():
    plan, _ = _plan([_event(1)])
    assert "does not own" in plan["rollback"]["never"]
    assert plan["rollback"]["restore"] == "exact preimage values, never recomputed ones"
    assert plan["replay"]["no_stability_claim"] is True


def test_the_recorded_plan_loads_verified_and_declares_zero_operations():
    plan = artifacts.load_verified(_PLAN)
    assert EA.validate_plan(plan) == []
    assert plan["write_path"] == "absent by design"
    assert plan["counts"][EA.CLASS_DETERMINISTIC] == 0
    assert plan["population"]["count"] == 19588


def test_the_recorded_baseline_reconciles_and_is_mutually_exclusive():
    base = artifacts.load_verified(_BASELINE)
    assert base["total"] == 19588
    assert sum(base["counts"].values()) == 19588
    assert base["counts"] == {"deterministic_link": 0, "meeting_level": 18960,
                              "ambiguous": 0, "missing_evidence": 0, "ineligible": 628}
    assert base["population"]["reconciles"] is True
