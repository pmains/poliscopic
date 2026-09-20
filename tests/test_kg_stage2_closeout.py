#!/usr/bin/env python3
"""Adversarial exact-accounting tests for the Stage 2 closeout baseline.

The ledger is only useful if its arithmetic is checkable, so these tests attack the
arithmetic: a widened denominator, a forged rate, a dropped criterion, a tampered
exception count, a vacuous gate pass.  Every attack must be refused.
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
from scripts.kg import stage2_closeout as closeout  # noqa: E402
from scripts.kg import stage2_quality_gate as G  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


_LEDGER = _live("kg-stage2-closeout-ledger-*.json")
_GATE = _live("kg-stage2-quality-gate-dry-*.json")
_SCHEMA = _live("kg-stage2-closeout-receipt-schema-*.json")


def ledger():
    return copy.deepcopy(artifacts.load_verified(_LEDGER))


def gate():
    return copy.deepcopy(artifacts.load_verified(_GATE))


# ── the recorded artifacts validate ────────────────────────────────────

def test_the_recorded_ledger_validates():
    assert closeout.validate_ledger(ledger()) == []


def test_the_recorded_dry_gate_validates():
    assert G.validate_gate(gate()) == []


def test_the_receipt_schema_is_declared_and_has_no_write_path():
    schema = artifacts.load_verified(_SCHEMA)
    assert schema["kind"] == G.RECEIPT_KIND
    assert schema["required"] == list(G.RECEIPT_FIELDS)
    assert schema["write_path"] == "absent by design"
    assert "O_CREAT|O_EXCL" in schema["immutability"]
    assert set(schema["commit_statuses"]) == set(G.COMMIT_STATUSES)


# ── every criterion is complete ────────────────────────────────────────

def test_every_criterion_carries_the_required_fields():
    for row in ledger()["criteria"]:
        for field in ("id", "source", "statement", "status", "count", "evidence",
                      "owner", "pass_condition"):
            assert row.get(field) not in (None, "", []), (row.get("id"), field)
        assert row["status"] in closeout.STATUSES
        assert isinstance(row["dependency"], list)
        for item in row["evidence"]:
            assert item["path"]


def test_every_exit_criterion_and_work_item_is_present():
    ids = {row["id"] for row in ledger()["criteria"]}
    for expected in ("EC-1", "EC-2", "EC-3",
                     "W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W9", "W10"):
        assert expected in ids, expected


def test_a_dropped_criterion_is_refused():
    data = ledger()
    data["criteria"] = [r for r in data["criteria"] if r["id"] != "EC-3"]
    assert closeout.validate_ledger(data)


def test_a_duplicated_criterion_is_refused():
    data = ledger()
    data["criteria"] = data["criteria"] + [data["criteria"][0]]
    assert closeout.validate_ledger(data)


def test_a_criterion_with_a_denominator_but_no_rate_is_refused():
    data = ledger()
    data["criteria"][2]["rate"] = None
    assert closeout.validate_ledger(data)


def test_a_criterion_without_a_pass_condition_is_refused():
    data = ledger()
    data["criteria"][0]["pass_condition"] = ""
    assert closeout.validate_ledger(data)


def test_an_unregistered_status_is_refused():
    data = ledger()
    data["criteria"][0]["status"] = "mostly-done"
    assert closeout.validate_ledger(data)


# ── the eligible denominator is explicit and recomputable ──────────────

def test_the_eligible_denominator_is_the_explicit_exclusion_rule():
    counts = ledger()["counts"]
    expected = (counts["total_documents"] - counts["meeting_level_only"]
                - counts["unassigned_placeholder"])
    assert counts["eligible_documents"] == expected == 59307
    assert expected == 65510 - 4196 - 2007


def test_the_denominator_excludes_exactly_the_two_ineligible_classes():
    assert set(G.INELIGIBLE_FOR_ITEM_LINK) == {"meeting_level_only",
                                               "unassigned_placeholder"}
    counts = {"total_documents": 100, "meeting_level_only": 30,
              "unassigned_placeholder": 20, "deterministic_links": 50}
    assert G.eligible_documents(counts) == 50


def test_a_widened_denominator_is_refused():
    """Counting meeting-level documents as eligible would raise the rate."""
    data = ledger()
    changed = copy.deepcopy(data)
    changed["counts"]["eligible_documents"] = 65510
    assert any("eligible denominator" in p
               for p in closeout.validate_ledger(changed))


def test_a_shrunk_denominator_is_refused():
    changed = ledger()
    changed["counts"]["eligible_documents"] = 1000
    assert any("eligible denominator" in p
               for p in closeout.validate_ledger(changed))


def test_a_zero_denominator_is_refused():
    changed = ledger()
    changed["counts"]["eligible_documents"] = 0
    assert any("denominator" in p for p in closeout.validate_ledger(changed))


def test_more_links_than_eligible_is_refused():
    changed = ledger()
    changed["counts"]["deterministic_links"] = 999999
    assert any("exceeds the eligible population" in p
               for p in closeout.validate_ledger(changed))


def test_the_rate_is_the_quotient_it_claims_to_be():
    data = ledger()
    row = next(r for r in data["criteria"] if r["id"] == "EC-3")
    assert row["count"] == 59104 and row["denominator"] == 59307
    assert row["rate"] == pytest.approx(59104 / 59307)
    assert row["rate"] >= 0.995


def test_a_zero_denominator_gate_criterion_fails_rather_than_passing():
    result = G.evaluate_gate({"deterministic_links": 10, "eligible_documents": 0,
                              "orphan_count": 0, "cycle_count": 0})
    criterion = next(c for c in result["criteria"] if c["id"] == "G3-deterministic-item-links")
    assert criterion["passed"] is False
    assert "denominator" in criterion["failure"]


# ── exceptions are explained, failures are zero ────────────────────────

def test_every_exception_carries_a_count_and_a_reason():
    for name, entry in ledger()["exceptions"].items():
        assert entry["count"] is not None and entry["count"] >= 0, name
        assert entry["reason"], name
        assert entry["acceptable"] is True, name


def test_the_explained_population_reconciles_with_the_document_counts():
    counts = ledger()["counts"]
    exceptions = ledger()["exceptions"]
    held = (exceptions["meeting_level_only"]["count"]
            + exceptions["unassigned_placeholder"]["count"]
            + exceptions["gap_missing_target"]["count"])
    assert counts["deterministic_links"] + held == counts["total_documents"]


def test_a_tampered_exception_count_is_refused():
    data = ledger()
    data["exceptions"]["collision_held"]["count"] = None
    assert closeout.validate_ledger(data)


def test_a_disowned_exception_is_refused():
    data = ledger()
    data["exceptions"]["gap_missing_target"]["acceptable"] = False
    assert closeout.validate_ledger(data)


def test_every_failure_counter_is_zero_at_baseline():
    failures = ledger()["failures"]
    assert failures["orphans"] == 0
    assert failures["cycles"] == 0
    assert failures["unexplained_remainder"] == 0
    assert failures["schema_drift"] == 0


@pytest.mark.parametrize("field", ["orphans", "cycles", "unexplained_remainder"])
def test_a_nonzero_failure_is_refused(field):
    data = ledger()
    data["failures"][field] = 1
    assert any(field in p for p in closeout.validate_ledger(data))


def test_a_nonempty_failure_list_is_refused():
    data = ledger()
    data["failures"]["schema_drift"] = ["something drifted"]
    assert closeout.validate_ledger(data)


# ── the projections are exact ──────────────────────────────────────────

def test_the_projection_is_a_union_without_double_counting():
    projected = ledger()["projected"]
    assert projected["repair_and_correction_overlap"] >= 0
    assert projected["resolved_gap_documents"] == 129
    assert projected["resolved_gap_documents"] <= projected["repair_population"]
    assert projected["union_within_repair_population"] is True
    assert projected["correction_population_within_repair"] is True


def test_the_projected_rate_is_computed_on_the_same_denominator():
    projected = ledger()["projected"]
    counts = ledger()["counts"]
    assert projected["eligible_documents"] == counts["eligible_documents"]
    assert projected["projected_deterministic_links"] == \
        counts["deterministic_links"] + projected["resolved_gap_documents"]
    assert projected["projected_rate"] == pytest.approx(
        projected["projected_deterministic_links"] / projected["eligible_documents"])
    assert projected["projected_rate"] >= 0.995


def test_the_projection_never_exceeds_the_eligible_population():
    projected = ledger()["projected"]
    assert projected["projected_deterministic_links"] <= projected["eligible_documents"]


# ── the gate recomputes rather than being believed ─────────────────────

def test_a_forged_gate_result_is_refused():
    data = gate()
    criterion = next(c for c in data["criteria"] if c["id"] == "G6-schema-readiness")
    criterion["passed"] = True
    assert any("recomputed" in p for p in G.validate_gate(data))


def test_a_forged_closure_flag_is_refused():
    data = gate()
    data["closed"] = True
    assert any("recomputed" in p or "closure" in p for p in G.validate_gate(data))


def test_a_dropped_gate_criterion_is_refused():
    data = gate()
    data["criteria"] = data["criteria"][:-1]
    assert any("full declared set" in p for p in G.validate_gate(data))


def test_the_dry_gate_does_not_report_vacuous_passes():
    data = gate()
    assert data["applied"] is False
    assert set(data["pending_post_apply"]) == {"G7-replay-idempotence", "G8-parity"}
    for criterion in data["criteria"]:
        if criterion["id"] in ("G7-replay-idempotence", "G8-parity"):
            assert criterion["passed"] is False
            assert criterion["status"] == "pending-post-apply"


def test_a_dry_gate_with_no_pending_criteria_is_refused():
    data = gate()
    data["pending_post_apply"] = []
    assert G.validate_gate(data)


def test_the_gate_is_open_at_baseline_and_names_the_blockers():
    data = gate()
    assert data["closed"] is False
    assert data["failed"] == ["G6-schema-readiness", "G7-replay-idempotence",
                              "G8-parity"]


def test_a_full_gate_closes_only_when_every_criterion_passes():
    observed = {
        "applied": True,
        "containers_with_a_canonical_parent": 132305,
        "containers_eligible_for_a_parent": 132305,
        "orphan_count": 0, "cycle_count": 0,
        "deterministic_links": 59307, "eligible_documents": 59307,
        "unexplained_remainder": 0,
        "drifted_protected_counts": [], "drifted_integrity_metrics": [],
        "columns_present": ["supporting_documents.agenda_item_db_id"],
        "columns_validated": ["supporting_documents.agenda_item_db_id"],
        "replay_writes": 0, "parity_differences": [],
    }
    result = G.evaluate_gate(observed)
    assert result["closed"] is True and result["failed"] == []
    assert G.validate_gate(result) == []


def test_the_gate_never_declares_a_write_path():
    assert gate()["write_path"] == "absent by design"
    assert ledger()["write_path"] == "absent by design"


# ── the receipt schema and receipt validation ─────────────────────────

def _closed_gate():
    return G.evaluate_gate({
        "applied": True,
        "containers_with_a_canonical_parent": 132305,
        "containers_eligible_for_a_parent": 132305,
        "orphan_count": 0, "cycle_count": 0,
        "deterministic_links": 59307, "eligible_documents": 59307,
        "unexplained_remainder": 0,
        "drifted_protected_counts": [], "drifted_integrity_metrics": [],
        "columns_present": ["supporting_documents.agenda_item_db_id"],
        "columns_validated": ["supporting_documents.agenda_item_db_id"],
        "replay_writes": 0, "parity_differences": [],
    })


def _receipt_kwargs(**over):
    base = dict(
        created_at="2026-09-12T23:00:00+00:00",
        target={"dialect": "postgresql", "host": "h", "port": 5432,
                "database": "poliscopic_dev", "tier": "development"},
        plan_digests=[{"path": "plan.json", "digest": "d" * 64,
                       "replay_digest": "r" * 64}],
        gate=_closed_gate(),
        counts={"meetings": 15901, "agenda_items": 116622, "documents": 65510},
        denominators={"eligible_documents": 59307,
                      "excluded": {"meeting_level_only": 4196,
                                   "unassigned_placeholder": 2007}},
        exceptions={"collision_held": {"count": 7797, "reason": "ambiguous"}},
        approvals=[{"by": "Peter Mains", "at": "2026-09-12T22:00:00+00:00",
                    "scope": "schema readiness apply"}],
        commit_status="committed")
    base.update(over)
    return base


def test_a_conforming_receipt_validates():
    receipt = G.build_receipt(**_receipt_kwargs())
    assert G.validate_receipt(receipt) == []
    assert len(receipt["receipt_digest"]) == 64


def test_a_receipt_without_a_closed_gate_is_refused():
    with pytest.raises(ValueError) as exc:
        G.build_receipt(**_receipt_kwargs(gate=gate()))
    assert "closed gate" in str(exc.value)


def test_a_receipt_without_approvals_is_refused():
    with pytest.raises(ValueError) as exc:
        G.build_receipt(**_receipt_kwargs(approvals=[]))
    assert "approved" in str(exc.value)


def test_a_receipt_without_plans_is_refused():
    with pytest.raises(ValueError) as exc:
        G.build_receipt(**_receipt_kwargs(plan_digests=[]))
    assert "plans" in str(exc.value)


def test_a_receipt_with_an_unregistered_commit_status_is_refused():
    with pytest.raises(ValueError):
        G.build_receipt(**_receipt_kwargs(commit_status="maybe"))


def test_a_receipt_missing_a_required_field_is_refused():
    receipt = G.build_receipt(**_receipt_kwargs())
    receipt["denominators"] = None
    assert any("denominators" in p for p in G.validate_receipt(receipt))


def test_a_receipt_with_no_eligible_denominator_is_refused():
    receipt = G.build_receipt(**_receipt_kwargs())
    receipt["denominators"] = {"excluded": {}}
    assert any("eligible document denominator" in p for p in G.validate_receipt(receipt))


def test_a_receipt_with_a_tampered_digest_is_refused():
    receipt = G.build_receipt(**_receipt_kwargs())
    receipt["counts"]["documents"] = 1
    assert any("canonical digest" in p for p in G.validate_receipt(receipt))


def test_a_receipt_naming_an_unclosed_gate_is_refused():
    receipt = G.build_receipt(**_receipt_kwargs())
    receipt["gate"] = gate()
    assert any("open gate" in p for p in G.validate_receipt(receipt))


def test_a_receipt_plan_entry_without_a_replay_digest_is_refused():
    receipt = G.build_receipt(**_receipt_kwargs())
    receipt["plan_digests"] = [{"path": "p.json", "digest": "d" * 64}]
    assert any("replay_digest" in p for p in G.validate_receipt(receipt))


# ── the ordered apply sequence and its approval boundaries ────────────

def test_the_sequence_is_ordered_and_has_approval_boundaries():
    sequence = ledger()["sequence"]
    steps = [s["step"] for s in sequence]
    assert steps == sorted(steps) == list(range(1, len(steps) + 1))
    for step in sequence:
        assert isinstance(step["requires_peter_approval"], bool)
        assert step["action"] and step["detail"] and step["note"]


def test_the_sequence_names_the_regeneration_step_as_blocking():
    """Each plan binds the current-state digest, so an apply invalidates the other."""
    sequence = {s["action"]: s for s in ledger()["sequence"]}
    regeneration = sequence["regenerate-and-review-repair-plan"]
    assert regeneration["blocking"] is True
    assert regeneration["requires_peter_approval"] is True
    assert "current-state digest" in regeneration["note"]


def test_the_sequence_puts_schema_before_data_and_parity_last():
    order = [s["action"] for s in ledger()["sequence"]]
    assert order.index("apply-schema-readiness") < order.index("apply-correction-plan")
    assert order.index("apply-correction-plan") < order.index("apply-repair-plan")
    assert order.index("apply-repair-plan") < order.index("apply-containment-plan")
    assert order[-1] == "parity-and-sync"
    assert order.index("replay-verification") < order.index("parity-and-sync")


def test_every_apply_step_requires_approval():
    for step in ledger()["sequence"]:
        if step["action"].startswith("apply-") or step["action"] == "parity-and-sync":
            assert step["requires_peter_approval"] is True, step["action"]


def test_a_sequence_with_no_approval_boundary_is_refused():
    data = ledger()
    for step in data["sequence"]:
        step["requires_peter_approval"] = False
    assert any("approval" in p for p in closeout.validate_ledger(data))


def test_an_unordered_sequence_is_refused():
    data = ledger()
    data["sequence"] = list(reversed(data["sequence"]))
    assert any("ordered" in p for p in closeout.validate_ledger(data))


# ── the ledger never claims to have applied anything ──────────────────

def test_the_ledger_records_no_application():
    data = ledger()
    assert data["applied"] is False
    assert data["schema_readiness"]["column_present"] is False
    assert data["schema_readiness"]["parent_item_id_present"] is False


def test_a_ledger_claiming_it_applied_is_refused():
    data = ledger()
    data["applied"] = True
    assert any("applied=false" in p for p in closeout.validate_ledger(data))


def test_a_ledger_with_a_tampered_digest_is_refused():
    data = ledger()
    data["counts"]["documents"] = 1
    assert any("canonical digest" in p for p in closeout.validate_ledger(data))
