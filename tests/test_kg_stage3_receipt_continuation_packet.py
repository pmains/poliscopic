"""Focused tests for the non-applying continuation packet and batch schedule.

Three things are pinned here.

The first is a regression for a real reading error: the design packet's *top-level*
digest and its *internal binding* digests are different values, and confusing them
produced a wrong claim that the packet constant was stale.  Nothing may read a packet's
identity with a first-match pattern.

The second is the batch schedule's arithmetic: windows must tile the plan exactly from
the proven cursor, never overlap, never skip, and never count a held identity as a write.

The third is the refusal set: an authorization bound to a stale plan, a stale design
packet, or a different code revision must be refused rather than accepted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.kg import stage3_processing_receipt_apply_packet as authorization
from scripts.kg import stage3_processing_receipt_store_packet as design
from scripts.kg import stage3_receipt_continuation_propose as propose
from scripts.kg.stage2_artifacts import load_verified

DESIGN_PACKET = Path("data/kg-plans/kg-stage3-processing-receipt-store-packet-20260921T194940Z.json")
CURSOR = 6100


# --------------------------------------------------------------------------- #
# Regression: a packet's top-level digest is not one of its binding digests
# --------------------------------------------------------------------------- #


def test_design_packet_identity_is_its_top_level_digest_not_a_binding_digest():
    packet = load_verified(DESIGN_PACKET)
    top = packet["digest"]
    assert top == design.packet_digest(packet)
    nested = {value["digest"] for value in packet["bindings"].values()
              if isinstance(value, dict) and "digest" in value}
    nested |= {value["digest"] for value in packet["bindings"]["evidence"].values()}
    assert top not in nested, "top-level digest collides with a nested binding digest"


def test_every_binding_digest_differs_from_the_packet_digest():
    """The failure mode: a first-match read picks a binding and reports it as the packet."""
    packet = load_verified(DESIGN_PACKET)
    serialized = Path(DESIGN_PACKET).read_text()
    first_match = serialized.split('"digest"')[1].split('"')[1]
    assert first_match != packet["digest"], (
        "a first-match read of \"digest\" returns a binding digest, not the packet's; "
        "readers must load the artifact and take the top-level field")


def test_first_match_reading_is_unsafe_on_any_sorted_artifact():
    """A synthetic guard so the lesson is not tied to one file's key ordering."""
    artifact = {"bindings": {"evidence": {"digest": "b" * 64}}, "digest": "a" * 64}
    serialized = __import__("json").dumps(artifact, sort_keys=True)
    assert serialized.split('"digest"')[1].split('"')[1] == "b" * 64
    assert artifact["digest"] == "a" * 64


# --------------------------------------------------------------------------- #
# Batch schedule arithmetic
# --------------------------------------------------------------------------- #


def _plan(outcomes):
    rows = []
    for index, outcome in enumerate(outcomes, start=1):
        rows.append({"processing_identity": ["supporting_document", str(index), "c" * 64,
                                             "pymupdf", "sweep_docs", "sweep_docs/1.0"],
                     "outcome": outcome})
    return {"kind": "kg-stage3-processing-dry-plan", "digest": "d" * 64, "bound":
            {"order": "source_id_asc"}, "selected": rows}


def test_windows_tile_the_plan_exactly_from_the_cursor():
    plan = _plan(["replay"] * CURSOR + ["planned"] * 1200)
    schedule = propose.build_schedule(plan, cursor=CURSOR, batch_size=500)
    spans = [(b["start_offset"], b["end_offset"]) for b in schedule["batches"]]
    assert spans[0][0] == CURSOR
    assert spans[-1][1] == len(plan["selected"])
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert end == start, "windows must be contiguous with no gap or overlap"
    assert sum(b["rows"] for b in schedule["batches"]) == len(plan["selected"]) - CURSOR


def test_held_identities_are_never_counted_as_writes():
    plan = _plan(["replay"] * CURSOR + ["planned", "held", "held"] * 100)
    schedule = propose.build_schedule(plan, cursor=CURSOR, batch_size=500)
    assert schedule["totals"]["expected_writes"] == 100
    assert schedule["totals"]["expected_outcomes"] == {
        "held": 200, "planned": 100}
    for batch in schedule["batches"]:
        assert batch["expected_writes"] == batch["expected_outcomes"].get("planned", 0)
        assert batch["held_in_window"] == batch["expected_outcomes"].get("held", 0)


def test_schedule_declares_stop_on_first_failure_and_never_writes_held():
    plan = _plan(["planned"] * 10)
    schedule = propose.build_schedule(plan, cursor=0, batch_size=5)
    assert schedule["stop_on_first_failure"] is True
    assert schedule["held_identities_are_never_written"] is True
    assert schedule["cursor"] == 0


# --------------------------------------------------------------------------- #
# Refusals: stale plan, stale design packet, code drift
# --------------------------------------------------------------------------- #


def _boundings():
    plan = {"digest": authorization.CURRENT_PLAN_DIGEST, "target":
            {"tier": "development", "database": "poliscopic_dev"}}
    design_packet = {"digest": authorization.CURRENT_DESIGN_PACKET_DIGEST}
    packet = {"kind": authorization.KIND, "version": authorization.VERSION,
              "state": "authorized", "enabled": True,
              "plan_digest": plan["digest"], "design_packet_digest": design_packet["digest"],
              "target": dict(plan["target"]), "approver": "review",
              "writer_role": "poliscopic", "batch_size": 500}
    packet["digest"] = authorization.digest(packet)
    return plan, design_packet, packet


def test_a_packet_bound_to_a_stale_plan_is_refused():
    plan, design_packet, packet = _boundings()
    stale = dict(plan, digest="0" * 64)
    problems = authorization.validate(packet, plan=stale, design_packet=design_packet)
    assert any("current dry plan" in problem for problem in problems)


def test_a_packet_bound_to_a_stale_design_packet_is_refused():
    plan, design_packet, packet = _boundings()
    stale = dict(design_packet, digest="0" * 64)
    problems = authorization.validate(packet, plan=plan, design_packet=stale)
    assert any("current disabled packet" in problem for problem in problems)


def test_code_binding_drift_is_refused():
    plan, design_packet, packet = _boundings()
    problems = authorization.validate(packet, plan=plan, design_packet=design_packet,
                                      current_code_digest="f" * 64)
    assert any("code binding drift" in problem for problem in problems)


def test_a_non_development_target_is_refused():
    plan, design_packet, packet = _boundings()
    packet = dict(packet, target={"tier": "production", "database": "poliscopic"}, digest=None)
    packet["digest"] = authorization.digest({k: v for k, v in packet.items() if k != "digest"})
    problems = authorization.validate(packet, plan=plan, design_packet=design_packet)
    assert any("target" in problem for problem in problems)


def test_an_empty_approver_is_refused():
    plan, design_packet, packet = _boundings()
    packet = dict(packet, approver="   ")
    packet["digest"] = authorization.digest({k: v for k, v in packet.items() if k != "digest"})
    problems = authorization.validate(packet, plan=plan, design_packet=design_packet)
    assert any("approver" in problem for problem in problems)
