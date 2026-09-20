"""Focused and adversarial tests for the version-aware processing receipt contract."""

from copy import deepcopy

from _kg_stage3_processing_fixtures import (
    LATER, NOW, receipt_for, resign_receipt, row)
from scripts.kg import stage3_processing_identity as AUTHORITY
from scripts.kg import stage3_processing_receipt as R


def tampered(**changes):
    """A receipt that is internally consistent apart from the requested change."""
    candidate = receipt_for(row())
    identity = candidate["processing_identity"]
    if "identity" in changes:
        identity[changes["identity"][0]] = changes["identity"][1]
        changes.pop("identity")
    candidate.update(changes)
    return resign_receipt(candidate)


def test_identity_authority_is_imported_not_reimplemented():
    assert R.processing_identity is AUTHORITY.processing_identity
    assert R.evidence_identity is AUTHORITY.evidence_identity
    assert R.content_sha256 is AUTHORITY.content_sha256
    assert R.acquisition_class is AUTHORITY.acquisition_class
    assert R.EXTRACTOR_VERSION == AUTHORITY.EXTRACTOR_VERSION
    for candidate in (row(), row(text_content=None, text_extraction_method=None), row(id=99)):
        assert list(R.processing_identity(candidate)) == \
            list(AUTHORITY.processing_identity(candidate))


def test_identity_authority_binding_is_verified_against_disk_every_time():
    binding = R.identity_authority_binding()
    assert binding["module"] == R.IDENTITY_AUTHORITY_MODULE
    assert R.identity_authority_problems(binding) == []
    for field in ("module", "sha256", "functions", "declared_extractor_version"):
        broken = dict(binding)
        broken[field] = "tampered"
        assert R.identity_authority_problems(broken), f"{field} drift was accepted"
    assert R.identity_authority_problems(None) == ["identity authority binding is missing"]


def test_identity_fields_and_store_key_are_stable():
    identity = R.processing_identity(row())
    assert len(identity) == len(R.IDENTITY_FIELDS) == 6
    assert R.identity_key(identity) == R.identity_key(list(identity))
    assert R.identity_key(identity) != R.identity_key(R.processing_identity(row(text_content="x")))
    assert identity != R.processing_identity(row(text_extraction_method="ocr_local"))
    assert identity != R.processing_identity(row(), extractor_version="sweep_docs/2.0")


def test_a_well_formed_receipt_validates_and_always_carries_its_canonical_digest():
    built = receipt_for(row())
    assert R.validate_receipt(built) == []
    assert built["digest"] == R.receipt_digest(built)
    assert R.validate_receipt(R.build_receipt(row(), status="failed",
                                              reason="parser_exception",
                                              recorded_at=NOW)) == []


def test_a_missing_canonical_digest_is_invalid_and_can_never_be_proof():
    unsigned = receipt_for(row())
    unsigned.pop("digest")
    assert "canonical digest is required and must be a lowercase sha256" in \
        R.validate_receipt(unsigned)
    assert R.fold_receipts([unsigned])["current"] == {}
    merged = R.merge_receipts([], [unsigned])
    assert merged["writes"] == 0 and merged["invalid_arrivals"] == 1
    for bad in ("", None, "nothex", "AB" * 32, "0" * 63):
        broken = receipt_for(row())
        broken["digest"] = bad
        assert R.validate_receipt(broken), f"digest {bad!r} was accepted"


def test_kind_version_and_producer_are_enforced_exactly():
    assert R.validate_receipt(tampered(kind="something-else"))
    assert R.validate_receipt(tampered(version="kg-processing-receipt/2.0"))
    assert R.validate_receipt(tampered(producer_version="kg-stage3-processing-receipt/9.9"))


def test_unsupported_source_kind_extractor_and_extractor_version_are_rejected():
    assert any("source_kind is not supported" in p for p in R.validate_receipt(
        tampered(source_kind="agenda_item", identity=(0, "agenda_item"))))
    assert any("extractor is not supported" in p for p in R.validate_receipt(
        tampered(extractor="other", identity=(4, "other"))))
    unsupported = tampered(extractor_version="sweep_docs/2.0", identity=(5, "sweep_docs/2.0"))
    assert any("extractor_version is not the declared version" in p
               for p in R.validate_receipt(unsupported))


def test_acquisition_block_shape_is_enforced():
    assert any("acquisition block is required" in p for p in R.validate_receipt(
        tampered(acquisition="recorded_scraper_acquisition")))
    assert any("acquisition.class is not registered" in p for p in R.validate_receipt(
        tampered(acquisition={"class": "made_up", "legacy_swept_at_present": False,
                              "does_not_prove_processing": True})))
    assert any("must be a boolean" in p for p in R.validate_receipt(
        tampered(acquisition={"class": "recorded_scraper_acquisition",
                              "legacy_swept_at_present": "yes",
                              "does_not_prove_processing": True})))
    assert any("must not claim to prove processing" in p for p in R.validate_receipt(
        tampered(acquisition={"class": "recorded_scraper_acquisition",
                              "legacy_swept_at_present": False,
                              "does_not_prove_processing": False})))


def test_canonical_digest_and_identity_agreement_are_enforced():
    assert any("canonical digest" in p for p in R.validate_receipt(
        {**receipt_for(row()), "reason": "rewritten"}))
    assert any("source_id disagrees" in p for p in R.validate_receipt(tampered(source_id=8)))
    assert any("content_sha256 disagrees" in p for p in R.validate_receipt(
        tampered(content_sha256="0" * 64)))
    assert any("must carry exactly six fields" in p for p in R.validate_receipt(
        tampered(processing_identity=[R.SOURCE_KINDS[0], 1])))
    assert any("content_sha256 must be 64 lowercase hex" in p for p in R.validate_receipt(
        tampered(content_sha256="DEADBEEF", identity=(2, "DEADBEEF"))))


def test_status_reason_and_time_are_enforced():
    assert any("status is not registered" in p for p in R.validate_receipt(tampered(status="maybe")))
    assert any("failed receipt requires a reason" in p for p in R.validate_receipt(
        tampered(status="failed", reason="")))
    assert any("recorded_at" in p for p in R.validate_receipt(tampered(recorded_at="not-a-time")))


def test_fold_is_ordered_fail_closed_and_reports_stored_status():
    key = R.identity_key(R.processing_identity(row()))
    failed = receipt_for(row(), status="failed", reason="boom")
    recovered = receipt_for(row(), status="success", recorded_at=LATER)
    folded = R.fold_receipts([failed])
    assert folded["current"][key]["status"] == "failed"
    assert folded["by_status"] == {"success": 0, "failed": 1}
    recovered_fold = R.fold_receipts([recovered, failed])
    assert recovered_fold["current"][key]["status"] == "success"
    assert recovered_fold["superseded"] == 1
    assert recovered_fold["by_status"] == {"success": 1, "failed": 0}
    tied = R.fold_receipts([receipt_for(row()), failed])
    assert tied["current"][key]["status"] == R.STATE_CONFLICT
    assert tied["by_status"] == {"success": 0, "failed": 0}
    assert R.fold_receipts([receipt_for(row()), receipt_for(row())])["replayed"] == 1


def test_invalid_receipts_never_become_state_and_taint_their_identity():
    key = R.identity_key(R.processing_identity(row()))
    folded = R.fold_receipts([receipt_for(row(), status="maybe")])
    assert folded["current"] == {}
    assert folded["invalid_identities"] == [key]
    assert folded["by_status"] == {"success": 0, "failed": 0}


def test_a_batch_yields_one_terminal_action_per_identity_whatever_the_order():
    success = receipt_for(row())
    failed = receipt_for(row(), status="failed", reason="boom", recorded_at=LATER)
    forward = R.merge_receipts([], [success, failed])
    reverse = R.merge_receipts([], [deepcopy(failed), deepcopy(success)])
    for merged in (forward, reverse):
        assert merged["writes"] == 1 and merged["identities"] == 1
        assert merged["max_writes_per_identity"] == 1 and merged["reconciles"] is True
        assert merged["counts"]["append"] == 1
    action = forward["actions"][0]
    assert action["terminal"] == "append"
    assert action["status"] == "failed" and action["recorded_at"] == LATER
    assert action["receipt"]["status"] == "failed"
    assert action["receipt"]["digest"] == action["digest"]
    assert [item["status"] for item in action["superseded_in_batch"]] == ["success"]
    assert action["consumed"] + len(action["superseded_in_batch"]) + action["duplicates"] == 2
    assert reverse["terminal"] == forward["terminal"]
    assert reverse["actions"][0]["digest"] == action["digest"]


def test_duplicate_arrivals_and_same_instant_disagreement():
    first = receipt_for(row())
    duplicate_batch = R.merge_receipts([], [first, deepcopy(first)])
    assert duplicate_batch["writes"] == 1 and duplicate_batch["duplicates"] == 1
    assert duplicate_batch["counts"]["append"] == 1 and duplicate_batch["reconciles"]
    conflicting = R.merge_receipts([], [receipt_for(row()),
                                        receipt_for(row(), status="failed", reason="boom")])
    assert conflicting["writes"] == 0 and conflicting["counts"]["conflict"] == 1
    action = conflicting["actions"][0]
    assert action["terminal"] == "conflict" and action["receipt"] is None
    assert action["consumed"] == 2 and conflicting["reconciles"]


def test_stored_and_incoming_sequences_decide_one_terminal_action():
    stored = receipt_for(row())
    newer = receipt_for(row(), status="failed", reason="boom", recorded_at=LATER)
    assert R.merge_receipts([stored], [newer])["terminal"][next(iter(
        R.merge_receipts([stored], [newer])["terminal"]))] == "append"
    replay = R.merge_receipts([stored], [deepcopy(stored)])
    assert replay["writes"] == 0 and replay["counts"]["replay"] == 1
    older = R.merge_receipts([newer], [stored])
    assert older["writes"] == 0 and older["counts"]["refuse"] == 1
    assert older["actions"][0]["reason"] == "incoming_receipt_is_older_than_stored"
    same_instant = R.merge_receipts([stored], [receipt_for(row(), status="failed",
                                                          reason="boom")])
    assert same_instant["writes"] == 0 and same_instant["counts"]["conflict"] == 1
    assert same_instant["reconciles"] and same_instant["max_writes_per_identity"] == 0


def test_tainted_or_unkeyable_stored_receipts_refuse_the_identity():
    tainted = R.merge_receipts([receipt_for(row(), status="maybe")],
                               [receipt_for(row(), recorded_at=LATER)])
    assert tainted["writes"] == 0 and tainted["counts"]["refuse"] == 1
    assert tainted["actions"][0]["reason"] == "stored_receipt_invalid_for_identity"
    assert tainted["reconciles"]
    unkeyable = R.merge_receipts([{"kind": R.RECEIPT_KIND, "processing_identity": None,
                                   "status": "maybe"}], [receipt_for(row())])
    assert unkeyable["writes"] == 0
    assert unkeyable["actions"][0]["reason"] == "unkeyable_invalid_stored_receipt"


def test_arrivals_reconcile_across_many_identities():
    merged = R.merge_receipts([receipt_for(row(id=1))],
                              [receipt_for(row(id=2)), receipt_for(row(id=3)),
                               receipt_for(row(id=3), recorded_at=LATER),
                               receipt_for(row(id=1), recorded_at=LATER),
                               {"kind": R.RECEIPT_KIND, "processing_identity": None}])
    assert merged["arrivals"] == 5 and merged["identities"] == 3
    assert merged["invalid_arrivals"] == 1 and merged["unkeyable_invalid_arrivals"] == 1
    # id 1 is newer than its stored receipt, id 2 is new, id 3's later arrival wins.
    assert merged["writes"] == merged["writes_by_identity"] == 3
    assert merged["superseded_in_batch"] == 1 and merged["duplicates"] == 0
    assert merged["reconciles"] is True and merged["max_writes_per_identity"] == 1


def test_stored_history_is_folded_first_and_refuses_unresolved_conflicts():
    first = receipt_for(row())
    conflicted_history = [first, receipt_for(row(), status="failed", reason="boom")]
    key = R.identity_key(R.processing_identity(row()))
    assert R.fold_receipts(conflicted_history)["current"][key]["status"] == R.STATE_CONFLICT
    for arrival in (receipt_for(row(), recorded_at=LATER), deepcopy(first)):
        merged = R.merge_receipts(conflicted_history, [arrival])
        assert merged["writes"] == 0
        assert merged["counts"]["append"] == 0
        assert merged["actions"][0]["reason"] == \
            "stored_receipt_identity_conflict_requires_resolution"
        assert merged["reconciles"] is True and merged["max_writes_per_identity"] == 0


def test_a_malformed_arrival_taints_its_identity_for_the_whole_batch():
    good = receipt_for(row())
    malformed = receipt_for(row())
    malformed.pop("digest")
    assert R.validate_receipt(malformed), "the malformed arrival must actually be invalid"
    for batch in ([good, malformed], [malformed, good]):
        merged = R.merge_receipts([], batch)
        action = merged["actions"][0]
        assert merged["writes"] == 0 and merged["appends"] == []
        assert merged["counts"]["append"] == 0 and merged["counts"]["refuse"] == 1
        assert action["terminal"] == "refuse"
        assert action["reason"] == "malformed_arrival_for_identity"
        assert action["consumed"] == 0
        assert [item["reason"] for item in action["superseded_in_batch"]] == \
            ["superseded_by_malformed_arrival"]
        assert merged["invalid_arrivals"] == 1 and merged["reconciles"] is True
    only_invalid = R.merge_receipts([], [malformed])
    assert only_invalid["actions"][0]["terminal"] == "invalid"
    assert only_invalid["writes"] == 0 and only_invalid["reconciles"] is True


def test_no_apply_or_write_surface_exists():
    for forbidden in ("apply", "update", "delete", "write_immutable", "get_engine"):
        assert not hasattr(R, forbidden), f"{forbidden} must not exist on the contract module"
