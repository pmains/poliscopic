"""Tests for orchestration-level ontology-emission receipt enforcement.

Covers the full refusal matrix, six-phase coverage drift, valid receipts, the
dry/live distinction, and backward compatibility of the orchestrator's result
keys.  The canonical validator is *not* re-implemented here: these tests exercise
the orchestration boundary that delegates to it.
"""

from __future__ import annotations

import pytest

from scripts.entities import detect_entities
from scripts.kg import orchestration_receipts as orch
from scripts.kg import producer_versions
from scripts.kg import registries as r
from scripts.kg.emission_models import STATE_SEALED
from scripts.kg.producer_coverage import PRODUCER_COVERAGE, producers_requiring_receipts

RECEIPT_PHASES = producers_requiring_receipts()
ANY_PHASE = RECEIPT_PHASES[0]


def valid_receipt(producer: str = ANY_PHASE, *, dry_run: bool = True) -> dict:
    """A sealed, fully reconciling receipt carrying the *declared* version."""
    return {
        "producer": producer,
        "producer_version": producer_versions.declared_producer_version(producer),
        "model_version": r.MODEL_VERSION,
        "registry_snapshot": r.snapshot_sha256(),
        "state": STATE_SEALED,
        "dry_run": dry_run,
        "failure": None,
        "values": {"attempted": 3, "accepted": 3, "rejected": 0,
                   "equation": "attempted == accepted + rejected", "reconciles": True},
        "rows": {"proposed": 4, "would_insert": 1, "would_update": 1,
                 "replay_noop": 2, "unresolved": 0, "committed": 0, "rolled_back": 0,
                 "classification_reconciles": True, "mutation_reconciles": None,
                 "reconciles": True},
        "observed": {},
        "rejections": [],
        "reclassified_conflicts": [],
        "derived_excluded": 0,
        "derived_exclusion_reasons": [],
    }


def wrap(receipt) -> dict:
    return {"validation_receipt": receipt}


def enforce(raw, *, phase: str = ANY_PHASE, dry_run: bool = True) -> dict:
    return orch.enforce_phase_receipt(phase, raw_result=raw, dry_run=dry_run)


# -- valid receipts ------------------------------------------------------

def test_valid_receipt_is_trusted():
    result = enforce(wrap(valid_receipt()))
    assert result["ok"] is True, result["reasons"]
    assert result["receipt"]["producer"] == ANY_PHASE


def test_single_receipt_in_a_sequence_is_accepted():
    result = enforce(wrap([valid_receipt()]))
    assert result["ok"] is True, result["reasons"]


def test_every_receipt_bearing_phase_is_covered():
    assert set(RECEIPT_PHASES) == set(PRODUCER_COVERAGE)
    for phase in RECEIPT_PHASES:
        assert enforce(wrap(valid_receipt(phase)), phase=phase)["ok"] is True


# -- refusal matrix ------------------------------------------------------

def test_missing_receipt_is_refused():
    result = enforce({})
    assert result["ok"] is False
    assert any("missing validation receipt" in reason for reason in result["reasons"])


def test_missing_result_payload_is_refused():
    assert enforce(None)["ok"] is False


def test_multiple_receipts_are_refused():
    result = enforce(wrap([valid_receipt(), valid_receipt()]))
    assert result["ok"] is False
    assert any("exactly one receipt" in reason for reason in result["reasons"])


def test_malformed_receipt_is_refused():
    result = enforce(wrap("not-an-object"))
    assert result["ok"] is False
    assert any("not an object" in reason for reason in result["reasons"])


@pytest.mark.parametrize("missing", ["producer", "producer_version", "model_version",
                                     "registry_snapshot", "state", "values", "rows"])
def test_receipt_missing_a_required_field_is_refused(missing):
    receipt = valid_receipt()
    receipt.pop(missing)
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("missing fields" in reason for reason in result["reasons"])


def test_empty_producer_version_is_refused():
    receipt = valid_receipt()
    receipt["producer_version"] = "  "
    assert enforce(wrap(receipt))["ok"] is False


def test_producer_mismatch_is_refused():
    result = enforce(wrap(valid_receipt(other_phase())), phase=ANY_PHASE)
    assert result["ok"] is False
    assert any("does not match phase" in reason for reason in result["reasons"])


def other_phase() -> str:
    return next(p for p in RECEIPT_PHASES if p != ANY_PHASE)


def test_producer_failure_is_refused():
    receipt = valid_receipt()
    receipt["failure"] = "producer exploded"
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("run failed" in reason for reason in result["reasons"])


def test_unsealed_receipt_is_refused():
    receipt = valid_receipt()
    receipt["state"] = "collecting"
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("not sealed" in reason for reason in result["reasons"])


def test_value_equation_disagreement_is_refused():
    receipt = valid_receipt()
    receipt["values"]["accepted"] = 2          # 3 != 2 + 0
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("value accounting does not reconcile" in reason
               for reason in result["reasons"])


def test_row_classification_disagreement_is_refused():
    receipt = valid_receipt()
    receipt["rows"]["replay_noop"] = 0         # 4 != 1 + 1 + 0 + 0
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("classification does not reconcile" in reason
               for reason in result["reasons"])


def test_committed_rows_during_dry_run_are_refused():
    receipt = valid_receipt()
    receipt["rows"]["committed"] = 2
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("dry run mutated rows" in reason for reason in result["reasons"])


def test_live_mutation_equation_is_enforced_when_not_dry():
    receipt = valid_receipt(dry_run=False)
    receipt["rows"]["committed"] = 1           # 2 mutations != 1 + 0
    result = enforce(wrap(receipt), dry_run=False)
    assert result["ok"] is False
    assert any("mutation does not reconcile" in reason for reason in result["reasons"])


def test_model_version_mismatch_is_refused():
    receipt = valid_receipt()
    receipt["model_version"] = "kg-model/0.0"
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("model version" in reason for reason in result["reasons"])


def test_registry_snapshot_mismatch_is_refused():
    receipt = valid_receipt()
    receipt["registry_snapshot"] = "0" * 64
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("registry snapshot" in reason for reason in result["reasons"])


def test_rejections_are_refused_unless_permitted():
    receipt = valid_receipt()
    receipt["values"] = {"attempted": 3, "accepted": 2, "rejected": 1}
    receipt["rejections"] = [{"reason": "unknown value"}]
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("rejected value" in reason for reason in result["reasons"])


def test_rejections_permitted_explicitly_are_accepted():
    receipt = valid_receipt()
    receipt["values"] = {"attempted": 3, "accepted": 2, "rejected": 1}
    receipt["rejections"] = [{"reason": "unknown value"}]
    result = orch.enforce_phase_receipt(ANY_PHASE, raw_result=wrap(receipt),
                                        dry_run=True, allowed_rejections=1)
    assert result["ok"] is True, result["reasons"]


def test_rejected_count_without_reasons_is_refused():
    receipt = valid_receipt()
    receipt["values"] = {"attempted": 2, "accepted": 1, "rejected": 1}
    receipt["rejections"] = []
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("silently discarded rejects" in reason for reason in result["reasons"])


def test_quarantined_observed_value_is_refused():
    receipt = valid_receipt()
    receipt["observed"] = {"entity_type": {"not_a_registered_type": 1}}
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("is unmapped" in reason or "is quarantined" in reason
               for reason in result["reasons"])


def test_uncovered_phase_is_refused():
    result = orch.enforce_phase_receipt("not_a_phase", raw_result=wrap(valid_receipt()),
                                        dry_run=True)
    assert result["ok"] is False
    assert any("no producer coverage entry" in reason for reason in result["reasons"])


def test_wrong_declared_version_is_refused():
    receipt = valid_receipt()
    receipt["producer_version"] = "not-the-declared-version"
    result = enforce(wrap(receipt))
    assert result["ok"] is False
    assert any("!= declared" in reason for reason in result["reasons"])


def test_declaring_no_version_fails_closed(monkeypatch):
    """A producer with no declaration cannot pass on a non-empty string alone."""
    monkeypatch.setattr(orch, "declared_producer_version", lambda _name: None)
    result = enforce(wrap(valid_receipt()))
    assert result["ok"] is False
    assert any("no declared version" in reason for reason in result["reasons"])


def test_version_registry_covers_every_receipt_bearing_producer():
    assert producer_versions.version_declaration_problems() == []
    missing = set(producers_requiring_receipts()) - set(
        producer_versions.PRODUCER_VERSIONS)
    assert missing == set()


def test_version_registry_flags_an_undeclared_producer(monkeypatch):
    monkeypatch.setattr(producer_versions, "PRODUCER_VERSIONS", {})
    problems = producer_versions.version_declaration_problems()
    assert any("declares no version" in problem for problem in problems)


def test_run_level_version_declaration_check_fails_closed(monkeypatch):
    monkeypatch.setattr(
        orch, "version_declaration_problems", lambda: ["producer x undeclared"])
    result = orch.enforce_run_receipts(entries(*RECEIPT_PHASES), dry_run=True)
    assert result["ok"] is False
    assert any("version declarations are incomplete" in reason
               for reason in result["reasons"])


# -- dry / live semantics ------------------------------------------------

def test_dry_receipt_is_refused_for_a_live_run():
    result = enforce(wrap(valid_receipt(dry_run=True)), dry_run=False)
    assert result["ok"] is False
    assert any("dry_run" in reason for reason in result["reasons"])


def test_live_receipt_is_refused_for_a_dry_run():
    result = enforce(wrap(valid_receipt(dry_run=False)), dry_run=True)
    assert result["ok"] is False
    assert any("dry_run" in reason for reason in result["reasons"])


# -- receipt / accounting agreement -------------------------------------

def test_accounting_disagreement_is_refused():
    receipt = valid_receipt()
    raw = wrap(receipt)
    raw["stats"] = {"rows_committed": 7}
    result = enforce(raw)
    assert result["ok"] is False
    assert any("receipt/accounting disagreement" in reason
               for reason in result["reasons"])


def test_agreeing_accounting_is_accepted():
    receipt = valid_receipt()
    raw = wrap(receipt)
    raw["stats"] = {"rows_committed": 0, "values_attempted": 3}
    assert enforce(raw)["ok"] is True


# -- six-phase coverage drift -------------------------------------------

def entries(*names, receipt_ok=True):
    return [{"name": n, "status": "ok", "receipt_ok": receipt_ok} for n in names]


def test_all_six_phases_present_passes():
    result = orch.enforce_run_receipts(entries(*RECEIPT_PHASES), dry_run=True)
    assert result["ok"] is True, result["reasons"]
    assert len(result["required_phases"]) == 6


def test_missing_phase_is_refused():
    result = orch.enforce_run_receipts(entries(*RECEIPT_PHASES[:-1]), dry_run=True)
    assert result["ok"] is False
    assert any("phases missing from the run" in reason for reason in result["reasons"])


def test_duplicate_phase_is_refused():
    result = orch.enforce_run_receipts(entries(*RECEIPT_PHASES, RECEIPT_PHASES[0]),
                                       dry_run=True)
    assert result["ok"] is False
    assert any("more than once" in reason for reason in result["reasons"])


def test_uncovered_phase_in_the_run_is_refused():
    result = orch.enforce_run_receipts(entries(*RECEIPT_PHASES, "mystery_phase"),
                                       dry_run=True)
    assert result["ok"] is False
    assert any("no coverage entry" in reason for reason in result["reasons"])


def test_a_refused_phase_receipt_fails_the_run():
    result = orch.enforce_run_receipts(entries(*RECEIPT_PHASES, receipt_ok=False),
                                       dry_run=True)
    assert result["ok"] is False
    assert any("phase receipts refused" in reason for reason in result["reasons"])


def test_skipped_phases_are_excluded_from_coverage():
    result = orch.enforce_run_receipts(
        entries(*RECEIPT_PHASES) + [{"name": "x", "status": "skipped"}], dry_run=True)
    assert result["ok"] is True


# -- orchestrator backward compatibility ---------------------------------

@pytest.fixture()
def stub_pipeline(monkeypatch):
    """Drive run_detection end-to-end with stub producers, no database."""
    monkeypatch.setattr(detect_entities, "_write_run_state", lambda state: None)
    monkeypatch.setattr(detect_entities, "_producer_metadata", lambda phase: {})

    def fake_resolve(phase):
        def run(engine, dry_run=False, force=False, verbose=False):
            return {"success": True, "entities_created": 0, "edges_created": 0,
                    **wrap(valid_receipt(phase["name"], dry_run=dry_run))}
        return run
    monkeypatch.setattr(detect_entities, "_resolve_phase_fn", fake_resolve)


def test_result_keys_remain_backward_compatible(stub_pipeline):
    result = detect_entities.run_detection(None, dry_run=True, force=True)
    for key in ("run_id", "phases", "phase_checks", "total_duration_s",
                "total_entities", "total_edges", "errors", "gate", "state_file"):
        assert key in result, f"historic result key {key!r} disappeared"
    assert "receipt_enforcement" in result
    assert result["receipt_enforcement"]["ok"] is True
    assert result["gate"]["failed"] is False
    assert len(result["phases"]) == 6


def test_orchestrator_gate_fails_when_a_receipt_is_missing(stub_pipeline, monkeypatch):
    def fake_resolve(phase):
        def run(engine, dry_run=False, force=False, verbose=False):
            return {"success": True, "entities_created": 0, "edges_created": 0}
        return run
    monkeypatch.setattr(detect_entities, "_resolve_phase_fn", fake_resolve)
    result = detect_entities.run_detection(None, dry_run=True, force=True)
    assert result["receipt_enforcement"]["ok"] is False
    assert result["gate"]["failed"] is True
    assert any(e.get("error") == "receipt enforcement failed" for e in result["errors"])
