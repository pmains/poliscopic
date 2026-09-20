from copy import deepcopy
from datetime import datetime, timezone

from scripts.kg import stage3_processing_identity as P


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(**changes):
    value = {"id": 1, "body": "x", "document_type": "Agenda",
             "document_url": "https://example.test/a", "text_content": "body",
             "text_extraction_method": "pymupdf", "text_extracted_at": NOW,
             "scraped_at": OLD, "content_hash": "raw", "meeting_db_id": 1,
             "agenda_item_db_id": None, "swept_at": NOW}
    value.update(changes)
    return value


def test_identity_changes_with_text_method_extractor_or_version():
    base = P.processing_identity(row())
    assert base != P.processing_identity(row(text_content="changed"))
    assert base != P.processing_identity(row(text_extraction_method="ocr_local"))
    assert base != P.processing_identity(row(), extractor="other")
    assert base != P.processing_identity(row(), extractor_version="2")


def test_legacy_swept_at_never_proves_current_processing():
    assert P.classify(row(swept_at=NOW)) == (
        "unproven_current", "no_versioned_processing_receipt")


def test_acquisition_provenance_is_independent_of_sweep_receipt():
    assert P.acquisition_class(row(swept_at=None)) == "recorded_scraper_acquisition"
    assert P.acquisition_class(row(document_url=None, scraped_at=None)) == "recorded_text_pipeline"
    assert P.acquisition_class(row(document_url=None, scraped_at=None,
                                   text_extraction_method=None,
                                   text_extracted_at=None)) == "provenance_unrecorded"


def test_exact_success_and_failure_receipts_are_distinct():
    identity = list(P.processing_identity(row()))
    assert P.classify(row(), {"processing_identity": identity, "status": "success"})[0] == "current_proven"
    assert P.classify(row(), {"processing_identity": identity, "status": "failed",
                              "reason": "parser"}) == ("failed_current", "parser")


def test_stale_or_drifted_versions_do_not_pass():
    stale = row(scraped_at=NOW, text_extracted_at=OLD)
    assert P.classify(stale)[0] == "source_stale"
    receipt = {"processing_identity": list(P.processing_identity(row())), "status": "success"}
    assert P.classify(row(text_content="changed"), receipt)[0] == "unproven_current"


def test_baseline_reconciles_and_treats_swept_as_hint():
    rows = [row(), row(id=2, swept_at=None),
            row(id=3, text_content=None, text_extraction_method="failed")]
    value = P.build_baseline(
        rows=rows, created_at="now", target={"tier": "development"},
        eligibility_binding={"path": "e", "digest": "d"})
    assert value["accounting"]["documents"] == 3
    assert value["accounting"]["reconciles"] is True
    assert value["accounting"]["legacy_swept_at_present"] == 2
    assert value["accounting"]["legacy_swept_at_present_on_eligible"] == 1
    assert value["accounting"]["requires_processing_proof"] == 2
    assert value["accounting"]["acquisition_reconciles"] is True
    assert value["mutations_proposed"] == 0


def test_no_apply_or_write_surface():
    assert not hasattr(P, "apply")
    assert not hasattr(P, "update")
    assert not hasattr(P, "delete")


def test_validate_current_rebuilds_and_rejects_drift():
    rows = [row()]
    target = {"tier": "development"}
    binding = {"path": "e", "digest": "d"}
    value = P.build_baseline(rows=rows, created_at="now", target=target,
                             eligibility_binding=binding)
    assert P.validate_current(value, rows=rows, target=target,
                              eligibility_binding=binding) == []
    changed = deepcopy(rows)
    changed[0]["text_content"] = "drift"
    assert P.validate_current(value, rows=changed, target=target,
                              eligibility_binding=binding)
