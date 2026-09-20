"""Focused and adversarial tests for the bounded DRY processing backfill."""

from pathlib import Path

import pytest
from _kg_stage3_processing_fixtures import (
    LATER, TARGET, build_plan, evidence, plan_inputs, receipt_for, row)
from scripts.kg import stage3_processing_backfill as B
from scripts.kg import stage3_processing_plan_inputs as P
from scripts.kg import stage3_processing_plan_validator as V
from scripts.kg import stage3_processing_receipt as R
from scripts.kg.stage2_artifacts import record_obsolete, write_immutable


def test_selection_is_deterministic_bounded_and_ordered():
    rows = [row(id=3), row(id=1), row(id=2)]
    bounded = B.select(rows, limit=2)
    assert [item["id"] for item in bounded["rows"]] == [1, 2]
    assert bounded["bound"] == {"order": "source_id_asc", "offset": 0, "limit": 2,
                                "population": 3, "selected": 2}
    assert [item["id"] for item in B.select(rows, offset=1, limit=1)["rows"]] == [2]
    assert [item["id"] for item in B.select(rows)["rows"]] == [1, 2, 3]
    assert B.select([], limit=5)["bound"]["selected"] == 0


def test_ambiguous_selection_fails_closed():
    with pytest.raises(ValueError):
        B.select([row(id=1), row(id=1)])
    with pytest.raises(ValueError):
        B.select([row(id=1)], limit=-1)


def test_documents_without_a_receipt_are_planned_never_processed():
    record = B.reconcile([row()], R.fold_receipts([]))[0]
    assert record["outcome"] == "planned"
    assert record["outcome"] != "success"
    assert record["reason"] == "no_exact_identity_receipt_would_process"
    assert record["would_process"] is True
    assert record["marked_processed"] is False
    assert record["processing_proven"] is False
    assert record["retry_eligible"] is False
    assert record["receipt_status"] is None and record["receipt_digest"] is None


def test_legacy_swept_at_and_acquisition_evidence_are_not_processing_proof():
    record = B.reconcile([row(swept_at="2026-09-13T00:00:00+00:00")], R.fold_receipts([]))[0]
    assert record["outcome"] == "planned"
    assert record["legacy_swept_at_present"] is True
    assert record["acquisition_provenance"] == "recorded_scraper_acquisition"
    assert record["processing_proven"] is False


def test_ineligible_and_stale_documents_are_held_not_processed():
    rows = [row(id=1, text_content=None, text_extraction_method=None),
            row(id=2, text_content="body", text_extraction_method="unregistered_method"),
            row(id=3, scraped_at=row()["text_extracted_at"], text_extracted_at=row()["scraped_at"])]
    records = {item["source_id"]: item for item in B.reconcile(rows, R.fold_receipts([]))}
    assert records[1]["reason"] == "not_eligible:missing_text"
    assert records[2]["reason"] == "not_eligible:unsupported"
    assert records[3]["reason"] == "source_stale_pending_text_refresh"
    assert all(item["outcome"] == "held" and not item["marked_processed"]
               for item in records.values())


def test_a_stored_failure_stays_unprocessed_and_retry_eligible():
    stored = receipt_for(row(), status="failed", reason="parser_exception")
    record = B.reconcile([row()], R.fold_receipts([stored]))[0]
    assert record["outcome"] == "failure"
    assert record["reason"] == "parser_exception"
    assert record["marked_processed"] is False
    assert record["processing_proven"] is False
    assert record["would_process"] is False
    assert record["retry_eligible"] is True
    assert record["receipt_status"] == "failed"
    assert record["receipt_digest"] == R.receipt_digest(stored)


def test_only_a_stored_success_receipt_proves_processing():
    record = B.reconcile([row()], R.fold_receipts([receipt_for(row())]))[0]
    assert record["outcome"] == "replay"
    assert record["marked_processed"] is True
    assert record["processing_proven"] is True
    assert record["would_process"] is False
    assert record["retry_eligible"] is False


def test_identity_drift_invalidates_a_receipt():
    drifted = receipt_for(row(text_content="an older retained body"))
    record = B.reconcile([row()], R.fold_receipts([drifted]))[0]
    assert record["outcome"] == "planned"
    assert record["processing_proven"] is False


def test_conflicting_invalid_or_unregistered_receipts_hold_the_document():
    conflict = [receipt_for(row()), receipt_for(row(), status="failed", reason="boom")]
    assert B.reconcile([row()], R.fold_receipts(conflict))[0]["reason"] == \
        "receipt_identity_conflict"
    invalid = B.reconcile([row()], R.fold_receipts([receipt_for(row(), status="maybe")]))[0]
    assert invalid["reason"] == "receipt_invalid_fail_closed"
    folded = {"current": {R.identity_key(list(R.processing_identity(row()))):
                          {"status": "unknown", "reason": "x"}},
              "invalid_identities": []}
    assert B.reconcile([row()], folded)[0]["reason"] == "receipt_status_unregistered"


def test_accounting_names_planned_rows_as_planned_and_never_as_success(tmp_path):
    value = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    account = value["accounting"]
    assert account["planned"] == 2 and account["would_process"] == 2
    assert account["replay"] == 0 and account["current_proven"] == 0
    assert account["processing_proven"] == 0 and account["marked_processed"] == 0
    assert account["stored_receipts"] == {"success": 0, "failed": 0}
    assert "success" not in account
    assert account["reconciles"] is True and account["selected"] == 2


def test_the_plan_reconciles_every_selected_document_exactly_once(tmp_path):
    rows = [row(id=1), row(id=2), row(id=3, text_content=None,
             text_extraction_method=None), row(id=4)]
    receipts = [receipt_for(row(id=2)),
                receipt_for(row(id=4), status="failed", reason="parser_exception")]
    value = build_plan(rows, receipts, tmp_path=tmp_path, limit=3)
    account = value["accounting"]
    assert account["population"] == 4 and account["selected"] == 3
    assert (account["planned"], account["replay"], account["failure"], account["held"]) == \
        (1, 1, 0, 1)
    assert len(value["records"]) == 3
    identities = [R.identity_key(item["processing_identity"]) for item in value["records"]]
    assert len(set(identities)) == len(identities)
    assert account["marked_processed"] == 1
    assert account["marked_processed_among_failure_or_held"] == 0
    assert account["acquisition_reconciles"] is True


def test_the_plan_binds_its_inputs_and_writes_nothing(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    assert value["mode"] == "dry-run" and value["applied"] is False
    assert value["writes_performed"] == 0 and value["processing_performed"] is False
    assert value["write_path"] == "absent by design"
    assert set(value["evidence_binding"]) == set(V.EVIDENCE_COMPONENTS)
    assert value["current_state_validation"]["verified"] is True
    assert set(value["selection_binding"]) == set(P.SELECTION_BINDING_KEYS)
    assert value["selection_binding"]["identity_sha256"] == P.selection_identity_sha256(
        [record["processing_identity"] for record in value["records"]])
    assert value["receipts_binding"] == {"source": "none", "count": 0}
    assert value["identity_authority"] == R.identity_authority_binding()
    assert value["approval_boundary"]["mutations_proposed"] == 0
    assert V.validate_plan(value, target=TARGET, hashes=V.code_hashes()) == []
    for forbidden in ("apply", "update", "delete", "execute"):
        assert not hasattr(B, forbidden), f"{forbidden} must not exist on the backfill module"


def test_the_plan_refuses_to_build_without_bound_inputs(tmp_path):
    inputs = plan_inputs([row()], None, tmp_path=tmp_path)
    for override in ({"evidence": None},
                     {"current_state_validation": None},
                     {"selection_binding": None}):
        with pytest.raises(ValueError):
            B.build_dry_plan(created_at="s", **{**inputs, **override})
    unverified = dict(inputs["current_state_validation"])
    unverified["verified"] = False
    with pytest.raises(ValueError):
        B.build_dry_plan(created_at="s", **{**inputs, "current_state_validation": unverified})
    shrunk = plan_inputs([row()], None, tmp_path=tmp_path, population=1)
    with pytest.raises(ValueError):
        B.build_dry_plan(created_at="s", **{**shrunk, "rows": [row(id=1), row(id=2)]})


def test_the_plan_refuses_a_selection_binding_that_does_not_match_the_records(tmp_path):
    inputs = plan_inputs([row(id=1), row(id=2)], None, tmp_path=tmp_path)
    substituted = [{**item, "identity_sha256": "0" * 64}
                   for item in [inputs["selection_binding"]]][0]
    with pytest.raises(ValueError, match="selection binding does not match"):
        B.build_dry_plan(created_at="s", **{**inputs, "selection_binding": substituted})


def test_the_plan_is_a_pure_function_of_its_inputs_and_replay_stable(tmp_path):
    rows = [row(id=1), row(id=2)]
    inputs = plan_inputs(rows, None, tmp_path=tmp_path)
    first = B.build_dry_plan(created_at="2026-09-14T12:00:00Z", **inputs)
    assert first["digest"] == B.build_dry_plan(created_at="2026-09-14T12:00:00Z", **inputs)["digest"]
    replay = B.replay_plan(first, **inputs)
    assert replay["identical"] is True and replay["replay_safe"] is True
    assert replay["writes_performed"] == 0 and replay["processing_performed"] is False
    changed = B.replay_plan(first, **plan_inputs(
        [row(id=1, text_content="drifted"), row(id=2)], None, tmp_path=tmp_path))
    assert changed["identical"] is False


def test_a_plan_that_would_mark_a_failure_processed_refuses_to_build(tmp_path, monkeypatch):
    original = B.build_record

    def tampered(source, *, outcome, reason, state):
        record = original(source, outcome=outcome, reason=reason, state=state)
        if record["outcome"] == "failure":
            record["marked_processed"] = True
        return record

    monkeypatch.setattr(B, "build_record", tampered)
    with pytest.raises(ValueError):
        build_plan([row()], [receipt_for(row(), status="failed", reason="boom")],
                   tmp_path=tmp_path)


def test_evidence_loading_refuses_obsolete_foreign_and_tampered_artifacts(tmp_path):
    binding, _ = evidence(tmp_path)
    paths = (binding["source_eligibility"]["path"],
             binding["processing_identity"]["path"])
    assert B.load_evidence_artifacts(
        eligibility_path=paths[0], processing_identity_path=paths[1], target=TARGET)
    record_obsolete(Path(paths[0]).parent, paths[0], "superseded by a corrected plan")
    with pytest.raises(RuntimeError, match="obsolete"):
        B.load_evidence_artifacts(eligibility_path=paths[0],
                                  processing_identity_path=paths[1], target=TARGET)

    other = tmp_path / "other"
    other.mkdir()
    foreign, _ = evidence(other, target={"tier": "development", "database": "somewhere_else"})
    with pytest.raises(RuntimeError, match="another target"):
        B.load_evidence_artifacts(
            eligibility_path=foreign["source_eligibility"]["path"],
            processing_identity_path=foreign["processing_identity"]["path"], target=TARGET)

    (other / "kinds").mkdir()
    mislabelled, _ = evidence(other / "kinds", kinds={
        "source_eligibility": "not-the-eligibility-kind",
        "processing_identity": V.EVIDENCE_COMPONENTS["processing_identity"]})
    with pytest.raises(RuntimeError, match="has kind"):
        B.load_evidence_artifacts(
            eligibility_path=mislabelled["source_eligibility"]["path"],
            processing_identity_path=mislabelled["processing_identity"]["path"], target=TARGET)


def test_evidence_loading_refuses_a_broken_artifact_chain(tmp_path):
    chain = tmp_path / "chain"
    chain.mkdir()
    eligibility_path = chain / "eligibility.json"
    write_immutable(eligibility_path, {
        "kind": V.EVIDENCE_COMPONENTS["source_eligibility"], "target": TARGET})
    processing_path = chain / "processing.json"
    write_immutable(processing_path, {
        "kind": V.EVIDENCE_COMPONENTS["processing_identity"], "target": TARGET,
        "eligibility_binding": {"digest": "0" * 64}})
    with pytest.raises(RuntimeError, match="does not bind this eligibility artifact"):
        B.load_evidence_artifacts(eligibility_path=eligibility_path,
                                  processing_identity_path=processing_path, target=TARGET)


def test_evidence_loading_refuses_tampered_bytes(tmp_path):
    binding, _ = evidence(tmp_path)
    path = Path(binding["source_eligibility"]["path"])
    path.write_text(path.read_text(encoding="utf-8").replace('"test"', '"rewritten"'),
                    encoding="utf-8")
    with pytest.raises(Exception):
        B.load_evidence_artifacts(
            eligibility_path=binding["source_eligibility"]["path"],
            processing_identity_path=binding["processing_identity"]["path"], target=TARGET)


def test_receipt_sets_are_bound_canonically_or_not_at_all(tmp_path):
    assert build_plan([row()], tmp_path=tmp_path)["receipts_binding"] == {
        "source": "none", "count": 0}
    bound = build_plan([row()], [receipt_for(row(), recorded_at=LATER)], tmp_path=tmp_path)
    assert bound["receipts_binding"]["count"] == 1
    assert bound["receipt_fold"]["valid_receipts"] == 1
    assert bound["accounting"]["stored_receipts"] == {"success": 1, "failed": 0}
    assert bound["accounting"]["replay"] == 1
    assert V.validate_plan(bound, target=TARGET, hashes=V.code_hashes()) == []
    plain = tmp_path / "plain.json"
    write_immutable(plain, {"receipts": [dict(receipt_for(row()))]})
    with pytest.raises(ValueError, match="kind"):
        B.load_receipts(plain)
    with pytest.raises(ValueError, match="absent"):
        B.load_receipts(tmp_path / "missing.json")
