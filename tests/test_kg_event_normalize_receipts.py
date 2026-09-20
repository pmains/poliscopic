"""Receipt accounting for the normalizer: two units, both reconciling.

Pure decisions plus one sealed-receipt check -- no database, no pipeline.
"""

from __future__ import annotations

import pytest

from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.entities.event_normalize_planning import build_classification_plan
from scripts.entities.event_normalize_receipts import (
    PRODUCER, ReceiptError, check_receipt, classify_page, new_validator,
    page_counts, propose_bundle_values, receipt_problems,
)
from scripts.entities.event_normalize_work_items import NormalizationWorkItem


def candidate(**overrides):
    kwargs = dict(
        extraction_id=1, supporting_document_id=1, meeting_db_id=1,
        meeting_source_id="2024-03-05-CC", public_body_id=1, jurisdiction_id=1,
        action_verb="approved", content_hash="h", extraction_method="pdftotext",
    )
    kwargs.update(overrides)
    return NormalizationCandidate.create(**kwargs)


def test_page_counts_match_the_plan_total():
    plan = build_classification_plan([candidate(), candidate(extraction_id=2)])

    counts = page_counts(plan)

    assert counts == {
        "would_insert": 2, "would_update": 2, "replay_noop": 0, "unresolved": 0,
    }
    assert sum(counts.values()) == plan.total_proposed


def test_classify_page_records_each_row_class_once():
    plan = build_classification_plan([candidate()])
    validator = new_validator("test/1.0", dry_run=True)

    validator.start_batch()
    validator.complete_validation()
    counts = classify_page(validator, plan)
    receipt = validator.seal()

    assert counts["would_insert"] == 1
    assert receipt.rows_proposed == 2
    assert receipt.rows_would_insert == 1
    assert receipt.rows_would_update == 1
    assert receipt.rows_replay_noop == 0
    assert receipt.rows_unresolved == 0
    assert receipt.classification_reconciles is True
    assert receipt.producer == PRODUCER


def test_propose_bundle_values_attempts_every_value_without_refusal():
    validator = new_validator("test/1.0", dry_run=True)
    validator.start_batch()

    propose_bundle_values(validator, [NormalizationWorkItem(candidate=candidate())])

    receipt = validator.receipt
    # event_type, outcome, assertion_class, model_version, evidence_class
    assert receipt.values_attempted == 5
    assert receipt.values_rejected == 0
    assert receipt.values_accepted == 5
    assert receipt.values_reconcile is True


def test_check_receipt_accepts_a_reconciling_receipt():
    validator = new_validator("test/1.0", dry_run=True)
    validator.start_batch()
    validator.complete_validation()
    validator.classify_rows(would_insert=1)
    receipt = validator.seal()

    check_receipt(receipt)
    assert receipt_problems(receipt) == ()


def test_check_receipt_rejects_an_unreconciled_receipt():
    validator = new_validator("test/1.0", dry_run=True)
    validator.start_batch()
    validator.complete_validation()
    validator.classify_rows(would_insert=1)
    receipt = validator.seal()

    receipt.rows_unresolved += 3
    assert receipt_problems(receipt)
    with pytest.raises(ReceiptError):
        check_receipt(receipt)
