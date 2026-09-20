"""Isolated accounting and receipt tests for the Stage 1 producers.

Covers the dry/live/replay/unresolved/rollback matrices for graph_builder,
pattern_cascade and role_classifier at their proposal and receipt boundaries.
No ML model is loaded and no database is touched: the proposal/classification and
accounting functions are pure, and the receipts they produce are checked against
the canonical ``reconcile_receipts`` authority.

Behavioural coverage of the role_classifier *run function* is retained by
``tests/test_role_classifier_batching.py``, which drives ``run_role_classifier``
with the model artifacts patched.
"""

from __future__ import annotations

import pytest

from scripts.entities import graph_builder_runtime
from scripts.entities.graph_builder_models import SourceStats
from scripts.entities.pattern_cascade_persistence import pattern_cascade_accounting
from scripts.entities.phase_receipt import (
    ReceiptAccountingError,
    RowAccounting,
    build_phase_receipt,
)
from scripts.entities.role_classifier_proposals import (
    PROPOSAL_REPLAY_NOOP,
    PROPOSAL_UNRESOLVED,
    PROPOSAL_WOULD_UPDATE,
    classify_role_proposal,
    role_classifier_accounting,
)
from scripts.kg import registries as r
from scripts.kg.emission_receipts import reconcile_receipts


def reconciles(receipt: dict, producer: str) -> list[str]:
    """Canonical reconciliation of one sealed phase receipt."""
    return reconcile_receipts(
        [receipt],
        expected_producers=[producer],
        model_version=r.MODEL_VERSION,
        registry_snapshot=r.snapshot_sha256(),
    )


# ── phase_receipt: refuses dishonest accounting ─────────────────────────


def test_receipt_refuses_accounting_that_does_not_classify_every_proposal():
    with pytest.raises(ReceiptAccountingError) as error:
        build_phase_receipt("graph_builder", dry_run=True,
                            rows=RowAccounting(proposed=5, would_insert=1))
    assert "unreconciled" in str(error.value)


def test_receipt_refuses_dry_run_that_reports_mutations():
    with pytest.raises(ReceiptAccountingError) as error:
        build_phase_receipt(
            "graph_builder", dry_run=True,
            rows=RowAccounting(proposed=1, would_insert=1, committed=1),
        )
    assert "dry run reported mutations" in str(error.value)


def test_receipt_refuses_live_mutation_mismatch():
    with pytest.raises(ReceiptAccountingError) as error:
        build_phase_receipt(
            "graph_builder", dry_run=False,
            rows=RowAccounting(proposed=2, would_insert=2, committed=1),
        )
    assert "mutations" in str(error.value)


def test_receipt_refuses_undeclared_producer():
    with pytest.raises(ReceiptAccountingError) as error:
        build_phase_receipt("not_a_producer", dry_run=True)
    assert "no declared version" in str(error.value)


def test_sealed_receipt_reconciles_under_canonical_authority():
    receipt = build_phase_receipt(
        "graph_builder", dry_run=True,
        values=[("entity_type", "developer"), ("entity_type", "person")],
        rows=RowAccounting(proposed=3, would_insert=2, replay_noop=1),
    )
    assert reconciles(receipt, "graph_builder") == []
    assert receipt["producer_version"] == "graph_builder/1.0"
    assert receipt["model_version"] == r.MODEL_VERSION
    assert receipt["registry_snapshot"] == r.snapshot_sha256()


def test_failed_receipt_still_seals_and_is_refused_as_failed():
    receipt = build_phase_receipt(
        "graph_builder", dry_run=True, rows=RowAccounting(),
        failure="ValueError: boom",
    )
    assert receipt["failure"] == "ValueError: boom"
    assert reconciles(receipt, "graph_builder") != []


# ── graph_builder ───────────────────────────────────────────────────────


def test_graph_builder_dry_accounting_writes_nothing():
    stats = SourceStats(entities_attempted=3, entities_planned=3,
                        edges_attempted=2, edges_planned=2)
    accounting = stats.proposal_accounting(dry_run=True)
    assert accounting["committed"] == 0
    assert accounting["would_insert"] == 5
    assert accounting["replay_noop"] == 0
    assert accounting["unresolved"] == 0


def test_graph_builder_live_accounting_commits_what_it_inserted():
    stats = SourceStats(entities_attempted=3, entities_inserted=3,
                        entities_planned=3, edges_attempted=2,
                        edges_inserted=2, edges_planned=2)
    accounting = stats.proposal_accounting(dry_run=False)
    assert accounting["committed"] == 5
    assert RowAccounting(**accounting).problems(dry_run=False) == []


def test_graph_builder_replay_and_unresolved_are_distinct():
    stats = SourceStats(
        entities_attempted=4, entities_planned=2,          # 2 entity replays
        edges_attempted=3, edges_planned=1,
        edge_replay_collisions=1, edges_unresolved_endpoint=1,
        mentions_attempted=2, mentions_planned=1,
        mention_replay_collisions=1,
    )
    accounting = stats.proposal_accounting(dry_run=True)
    assert accounting["replay_noop"] == 2 + 1 + 1
    assert accounting["unresolved"] == 1
    assert RowAccounting(**accounting).problems(dry_run=True) == []


def test_graph_builder_planned_unwritable_counts_as_unresolved_not_insert():
    """A planned edge with no id yet is never written, so it is not an insert.

    In a live run the writable edge is the only row written, which is why
    ``edges_inserted`` is 1 here: the accounting is only consistent when the
    committed count matches exactly what was written.
    """
    stats = SourceStats(edges_attempted=2, edges_planned=2,
                        edges_planned_unwritable=1, edges_inserted=1)
    accounting = stats.proposal_accounting(dry_run=False)
    assert accounting["would_insert"] == 1
    assert accounting["unresolved"] == 1
    assert RowAccounting(**accounting).problems(dry_run=False) == []


def test_graph_builder_dry_run_never_claims_the_unwritable_row():
    stats = SourceStats(edges_attempted=2, edges_planned=2,
                        edges_planned_unwritable=1)
    accounting = stats.proposal_accounting(dry_run=True)
    assert accounting["would_insert"] == 1
    assert accounting["unresolved"] == 1
    assert accounting["committed"] == 0
    assert RowAccounting(**accounting).problems(dry_run=True) == []


def test_graph_builder_result_carries_one_reconciled_receipt():
    stats = SourceStats(entities_attempted=2, entities_planned=2,
                        emitted_values=[("entity_type", "developer")])
    result = graph_builder_runtime._result(stats, source_count=1, skipped=0,
                                           dry_run=True)
    receipt = result["validation_receipt"]
    assert reconciles(receipt, "graph_builder") == []
    assert result["entities_created"] == 0
    assert result["dry_run"] is True


def test_graph_builder_receipt_records_rejection_and_orchestration_refuses_it():
    """A prohibited entity type is recorded as a rejection, then refused.

    Reconciliation covers the receipt's equations; refusing a rejected value is
    the orchestration boundary's job, and it fails closed there.
    """
    from scripts.kg import orchestration_receipts as orch

    stats = SourceStats(entities_attempted=1, entities_planned=1,
                        emitted_values=[("entity_type", "recommendation")])
    result = graph_builder_runtime._result(stats, source_count=1, skipped=0,
                                           dry_run=True)
    receipt = result["validation_receipt"]
    assert receipt["values"]["rejected"] == 1
    assert receipt["rejections"], "the refused value must be retained"

    enforcement = orch.enforce_phase_receipt(
        "graph_builder", raw_result=result, dry_run=True)
    assert enforcement["ok"] is False
    assert any("rejected value" in reason for reason in enforcement["reasons"])


# ── pattern_cascade ─────────────────────────────────────────────────────


def _pattern_total(**overrides):
    total = {"entities": 2, "mentions_planned": 5, "edges_planned": 3,
             "entity_replay_collisions": 1, "mention_replay_collisions": 1,
             "mentions_unresolved_entity": 1, "edge_replay_collisions": 1,
             "edges_unresolved_endpoint": 0}
    total.update(overrides)
    return total


def test_pattern_cascade_dry_accounting_classifies_every_proposal():
    accounting = pattern_cascade_accounting(_pattern_total(), dry_run=True)
    assert accounting.proposed == 11
    assert accounting.would_update == 0
    assert accounting.would_insert == 7
    assert accounting.replay_noop == 3
    assert accounting.unresolved == 1
    assert accounting.problems(dry_run=True) == []


def test_pattern_cascade_live_accounting_commits_every_insert():
    accounting = pattern_cascade_accounting(_pattern_total(), dry_run=False)
    assert accounting.committed == accounting.would_insert
    assert accounting.problems(dry_run=False) == []


def test_pattern_cascade_unresolved_endpoints_are_not_inserts():
    accounting = pattern_cascade_accounting(
        _pattern_total(edges_unresolved_endpoint=2), dry_run=True)
    assert accounting.would_insert == 5
    assert accounting.unresolved == 3
    assert accounting.problems(dry_run=True) == []


def test_pattern_cascade_emits_one_reconciled_receipt():
    receipt = build_phase_receipt(
        "pattern_cascade", dry_run=True,
        values=[("entity_type", "person"), ("role", "applicant"),
                ("relationship", "REPRESENTS")],
        rows=pattern_cascade_accounting(_pattern_total(), dry_run=True),
    )
    assert reconciles(receipt, "pattern_cascade") == []


# ── role_classifier: proposal classification ────────────────────────────


def classify(**kwargs):
    defaults = dict(mention_id=1, predicted_role="applicant", current_role="",
                    confidence=0.9, threshold=0.5)
    defaults.update(kwargs)
    return classify_role_proposal(**defaults)


def test_changed_valid_role_is_a_would_update_including_in_dry_mode():
    assert classify(current_role="", confidence=0.9) == PROPOSAL_WOULD_UPDATE


def test_unchanged_proposed_role_is_a_replay_noop():
    assert classify(predicted_role="applicant",
                    current_role="applicant") == PROPOSAL_REPLAY_NOOP


def test_below_threshold_prediction_is_unresolved():
    assert classify(current_role="", confidence=0.2) == PROPOSAL_UNRESOLVED


def test_non_canonical_role_is_unresolved_not_emitted():
    assert classify(predicted_role="known_org") == PROPOSAL_UNRESOLVED


def test_blank_or_missing_target_is_unresolved():
    assert classify(predicted_role="   ") == PROPOSAL_UNRESOLVED
    assert classify(mention_id=None) == PROPOSAL_UNRESOLVED


def test_quarantined_role_is_unresolved():
    assert classify(predicted_role="case_number") == PROPOSAL_UNRESOLVED


# ── role_classifier: accounting ─────────────────────────────────────────


def test_role_classifier_dry_accounting_writes_nothing():
    accounting = role_classifier_accounting(
        {"would_update": 3, "replay_noop": 2, "unresolved": 1},
        committed=0, dry_run=True,
    )
    assert accounting.proposed == 6
    assert accounting.would_update == 3   # counted in dry mode too
    assert accounting.committed == 0
    assert accounting.rolled_back == 0
    assert accounting.problems(dry_run=True) == []


def test_role_classifier_live_full_commit_reconciles():
    accounting = role_classifier_accounting(
        {"would_update": 3, "replay_noop": 2, "unresolved": 1},
        committed=3, dry_run=False,
    )
    assert accounting.rolled_back == 0
    assert accounting.problems(dry_run=False) == []


def test_role_classifier_partial_write_is_rolled_back():
    accounting = role_classifier_accounting(
        {"would_update": 4, "replay_noop": 0, "unresolved": 0},
        committed=1, dry_run=False,
    )
    assert accounting.rolled_back == 3
    assert accounting.problems(dry_run=False) == []


def test_empty_role_classifier_run_seals_an_honest_zero_receipt():
    receipt = build_phase_receipt(
        "role_classifier", dry_run=True,
        rows=role_classifier_accounting({}, committed=0, dry_run=True),
    )
    assert reconciles(receipt, "role_classifier") == []
    assert receipt["rows"]["proposed"] == 0


def test_role_classifier_receipt_validates_each_emitted_role():
    receipt = build_phase_receipt(
        "role_classifier", dry_run=True,
        values=[("role", "applicant"), ("role", "attorney")],
        rows=role_classifier_accounting(
            {"would_update": 1, "replay_noop": 1, "unresolved": 0},
            committed=0, dry_run=True),
    )
    assert reconciles(receipt, "role_classifier") == []
    assert receipt["values"]["attempted"] == 2
