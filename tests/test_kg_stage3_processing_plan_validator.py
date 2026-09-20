"""Adversarial tests for the public immutable processing-dry-plan validator."""

import hashlib
import json
from pathlib import Path

import pytest
from _kg_stage3_processing_fixtures import (
    LATER, NOW, STAMP, TARGET, build_plan, plan_inputs, receipt_for, receipt_set,
    resign_plan, row)
from scripts.kg import stage3_processing_backfill as B
from scripts.kg import stage3_processing_plan_inputs as P
from scripts.kg import stage3_processing_plan_validator as V
from scripts.kg import stage3_processing_receipt as R
from scripts.kg import stage3_source_eligibility as eligibility
from scripts.kg.stage2_artifacts import record_obsolete, write_immutable


def refuses(plan, *fragments):
    problems = V.validate_plan(plan, target=TARGET, hashes=V.code_hashes())
    assert problems, "a tampered plan was accepted"
    for fragment in fragments:
        assert any(fragment in problem for problem in problems), \
            f"no problem mentioned {fragment!r}: {problems}"
    return problems


def rehash(plan):
    """A tamperer who re-derives every derived value before re-signing."""
    plan["records_sha256"] = V.canonical_sha256(plan["records"])
    plan["accounting"] = V.accounting(plan["records"], bound=plan["bound"], folded={})
    return resign_plan(plan)


def test_a_built_plan_validates_clean(tmp_path):
    value = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    assert V.validate_plan(value, target=TARGET, hashes=V.code_hashes()) == []
    assert V.validate_plan("not a plan") == ["plan must be an object"]
    assert V.validate_plan({"not": "a plan"})


def test_digest_tampering_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["records"][0]["reason"] = "rewritten by hand"
    refuses(value, "plan digest does not match the plan body")
    rewound = build_plan([row()], tmp_path=tmp_path)
    rewound["digest"] = "0" * 64
    refuses(rewound, "plan digest does not match the plan body")


def test_code_hash_tampering_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["producer"]["code_hashes"][V.BUILDER_MODULE] = "0" * 64
    resign_plan(value)
    refuses(value, "producer code hashes do not match the code on disk")


def test_target_tampering_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["target"]["database"] = "poliscopic"
    resign_plan(value)
    refuses(value, "evidence target differs from the plan target",
            "plan target differs from the expected target")
    clean = build_plan([row()], tmp_path=tmp_path)
    assert "plan target differs from the expected target" in V.validate_plan(
        clean, target={"tier": "development", "database": "elsewhere"},
        hashes=V.code_hashes())


def test_identity_authority_tampering_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["identity_authority"]["sha256"] = "0" * 64
    resign_plan(value)
    refuses(value, "identity authority sha256 drift")


def test_unknown_and_missing_components_are_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["extra_component"] = {"path": "x"}
    resign_plan(value)
    refuses(value, "unregistered top-level components")
    trimmed = build_plan([row()], tmp_path=tmp_path)
    trimmed.pop("selection_binding")
    resign_plan(trimmed)
    refuses(trimmed, "missing plan components", "required component is missing")


def test_a_plan_claiming_work_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["applied"] = True
    value["writes_performed"] = 3
    resign_plan(value)
    refuses(value, "a dry plan must be unapplied", "must report zero writes")


def test_population_and_bound_tampering_is_refused(tmp_path):
    value = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    value["bound"]["population"] = 99
    resign_plan(value)
    refuses(value, "selected count is not what the bound implies",
            "plan population differs from the bound document population")
    skewed = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    skewed["bound"]["order"] = "insertion"
    resign_plan(skewed)
    refuses(skewed, "selection order is not the canonical order")


def test_accounting_tampering_is_refused(tmp_path):
    value = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    value["accounting"]["planned"] = 42
    resign_plan(value)
    refuses(value, "accounting planned does not match the records")
    optimistic = build_plan([row()], tmp_path=tmp_path)
    optimistic["accounting"]["current_proven"] = 7
    resign_plan(optimistic)
    refuses(optimistic, "accounting current_proven does not match the records")


def test_record_tampering_is_refused(tmp_path):
    value = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    value["records"][0]["reason"] = "rewritten by hand"
    resign_plan(value)
    refuses(value, "records digest does not match the records")

    rehashed = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    rehashed["records"][0]["would_process"] = False
    rehash(rehashed)
    refuses(rehashed, "a planned document is not marked would_process")

    reordered = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    reordered["records"] = list(reversed(reordered["records"]))
    rehash(reordered)
    refuses(reordered, "records are not in canonical source_id order")

    duplicated = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    duplicated["records"] = duplicated["records"] + [dict(duplicated["records"][0])]
    rehash(duplicated)
    refuses(duplicated, "a document was reconciled more than once",
            "selected count does not match the record count")


def test_a_failure_marked_processed_is_refused_even_when_re_derived(tmp_path):
    value = build_plan([row()], [receipt_for(row(), status="failed", reason="boom")],
                       tmp_path=tmp_path)
    record = value["records"][0]
    assert record["outcome"] == "failure" and record["marked_processed"] is False
    record["marked_processed"] = True
    rehash(value)
    refuses(value, "a failed or held document is marked processed")


def test_a_planned_row_claiming_processing_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    assert value["records"][0]["outcome"] == "planned"
    value["records"][0]["processing_proven"] = True
    rehash(value)
    refuses(value, "processing is claimed without an exact receipt")


def test_a_re_signed_planned_to_replay_tamper_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    record = value["records"][0]
    assert record["outcome"] == "planned" and record["receipt_digest"] is None
    record.update(outcome="replay", would_process=False, marked_processed=True,
                  processing_proven=True, receipt_status="success",
                  receipt_digest=hashlib.sha256(b"invented").hexdigest())
    rehash(value)
    assert value["accounting"]["replay"] == 1 and value["accounting"]["planned"] == 0
    refuses(value, "contradicts the bound selection entry")


def test_an_invented_receipt_digest_is_refused(tmp_path):
    stored = receipt_for(row(), recorded_at=LATER)
    value = build_plan([row()], [stored], tmp_path=tmp_path)
    record = value["records"][0]
    assert record["outcome"] == "replay" and record["receipt_digest"] == R.receipt_digest(stored)
    record["receipt_digest"] = hashlib.sha256(b"invented").hexdigest()
    rehash(value)
    refuses(value, "receipt digest is not the bound receipt's digest")


def test_receipt_binding_mismatches_are_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["receipts_binding"] = {"source": "none", "count": 3}
    resign_plan(value)
    refuses(value, "claims a receipt set but binds no artifact")
    empty = build_plan([row()], tmp_path=tmp_path)
    empty["receipts_binding"] = {"source": "none", "count": 0, "path": "somewhere.json"}
    resign_plan(empty)
    refuses(empty, "claims a receipt set but binds no artifact")

    bound = build_plan([row()], [receipt_for(row(), recorded_at=LATER)], tmp_path=tmp_path)
    bound["receipts_binding"] = {**bound["receipts_binding"], "count": 9}
    resign_plan(bound)
    refuses(bound, "receipt set count differs from the bound artifact")
    drifted = build_plan([row()], [receipt_for(row(), recorded_at=LATER)], tmp_path=tmp_path)
    drifted["receipts_binding"] = {**drifted["receipts_binding"], "sha256": "0" * 64}
    resign_plan(drifted)
    refuses(drifted, "receipt set sha256 differs from the bound artifact")


def test_receipt_fold_tampering_is_refused(tmp_path):
    value = build_plan([row()], [receipt_for(row(), recorded_at=LATER)], tmp_path=tmp_path)
    value["receipt_fold"]["valid_receipts"] = 0
    resign_plan(value)
    refuses(value, "receipt fold valid_receipts does not match the bound receipt set")
    invented = build_plan([row()], tmp_path=tmp_path)
    invented["receipt_fold"]["identities"] = 4
    resign_plan(invented)
    refuses(invented, "receipt fold identities does not match the bound receipt set")


def test_selected_identity_substitution_is_refused(tmp_path):
    value = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    value["records"][0]["processing_identity"][2] = "a" * 64
    value["records_sha256"] = V.canonical_sha256(value["records"])
    resign_plan(value)
    refuses(value, "selected snapshot identity digest does not match the plan records")

    resigned = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    resigned["records"][0]["processing_identity"][2] = "b" * 64
    resigned["selection_binding"] = {**resigned["selection_binding"],
                                     "identity_sha256": P.selection_identity_sha256(
                                         [item["processing_identity"]
                                          for item in resigned["records"]])}
    resigned["records_sha256"] = V.canonical_sha256(resigned["records"])
    resign_plan(resigned)
    refuses(resigned, "selection identity_sha256 differs from the bound artifact")


def test_selection_binding_drift_is_refused(tmp_path):
    value = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    path = Path(value["selection_binding"]["path"])
    path.write_text(path.read_text(encoding="utf-8").replace("source_id_asc", "shuffled"),
                    encoding="utf-8")
    refuses(value, "selection artifact failed verification")
    missing = build_plan([row()], tmp_path=tmp_path)
    missing["selection_binding"] = {**missing["selection_binding"], "digest": "0" * 64}
    resign_plan(missing)
    refuses(missing, "selection artifact digest differs from the recorded binding")


def test_obsolete_and_drifted_evidence_is_refused(tmp_path):
    inputs = plan_inputs([row()], None, tmp_path=tmp_path)
    value = B.build_dry_plan(created_at=STAMP, **inputs)
    assert V.validate_plan(value, target=TARGET, hashes=V.code_hashes()) == []
    target_path = Path(inputs["evidence"]["source_eligibility"]["path"])
    record_obsolete(target_path.parent, target_path, "superseded")
    refuses(value, "artifact is obsolete")

    drifted = tmp_path / "drifted"
    drifted.mkdir()
    fresh_inputs = plan_inputs([row()], None, tmp_path=drifted)
    fresh = B.build_dry_plan(created_at=STAMP, **fresh_inputs)
    assert V.validate_plan(fresh, target=TARGET, hashes=V.code_hashes()) == []
    path = Path(fresh_inputs["evidence"]["source_eligibility"]["path"])
    path.write_text(path.read_text(encoding="utf-8").replace('"test"', '"rewritten"'),
                    encoding="utf-8")
    refuses(fresh, "artifact failed verification")


def test_extractor_and_identity_field_tampering_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["extractor_version"] = "sweep_docs/2.0"
    value["identity_fields"] = ["source_kind", "source_id"]
    resign_plan(value)
    refuses(value, "identity fields are not the canonical processing identity",
            "plan extractor/version is not the current declared extractor")


def test_approval_boundary_tampering_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    value["approval_boundary"]["mutations_proposed"] = 5
    value["approval_boundary"]["not_authorized"] = []
    resign_plan(value)
    refuses(value, "a dry plan may not propose mutations",
            "approval boundary must name what is not authorized")


def test_eligibility_is_rederived_from_bound_source_fields(tmp_path):
    rows = [row(id=3343, text_content="body", text_extraction_method="unregistered_method"),
            row(id=1)]
    value = build_plan(rows, tmp_path=tmp_path)
    held = [item for item in value["records"] if item["source_id"] == 3343][0]
    assert held["outcome"] == "held" and held["reason"] == "not_eligible:unsupported"
    assert V.validate_plan(value, target=TARGET, hashes=V.code_hashes()) == []
    held.update(outcome="planned", reason="no_exact_identity_receipt_would_process",
                would_process=True, marked_processed=False, processing_proven=False)
    rehash(value)
    assert value["accounting"]["planned"] == 2 and value["accounting"]["held"] == 0
    refuses(value, "contradicts the bound selection entry")


def test_a_rewritten_snapshot_entry_cannot_promote_a_held_row(tmp_path):
    """Rewriting the bound source fields alone is caught before any promotion.

    Note the honest boundary: an attacker who rewrites the whole bound snapshot *and*
    re-derives every digest produces a self-consistent plan for different inputs.  What
    the binding guarantees is that the classification inputs are the snapshot's, not the
    record's assertion - see the re-derivation test above - and that the snapshot is a
    write-once artifact whose digest the plan records.
    """
    rows = [row(id=3343, text_content="body", text_extraction_method="unregistered_method")]
    value = build_plan(rows, tmp_path=tmp_path)
    binding = value["selection_binding"]
    source = Path(binding["path"])
    artifact = json.loads(source.read_text(encoding="utf-8"))
    artifact.pop("digest")
    artifact["entries"][0]["extraction_method"] = "pymupdf"
    artifact["entries_sha256"] = P.eligibility_sha256(artifact["entries"])
    rewritten = source.parent / "selection-rewritten.json"
    digest = write_immutable(rewritten, artifact)
    value["selection_binding"] = {**binding, "path": str(rewritten), "digest": digest}
    resign_plan(value)
    refuses(value, "bound selection entry")


def test_snapshot_entry_tampering_is_refused(tmp_path):
    value = build_plan([row(id=1), row(id=2)], tmp_path=tmp_path)
    path = Path(value["selection_binding"]["path"])
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact.pop("digest")
    artifact["entries"][0]["content_sha256"] = "c" * 64
    rewritten = path.parent / "selection-tampered.json"
    digest = write_immutable(rewritten, artifact)
    value["selection_binding"] = {**value["selection_binding"], "path": str(rewritten),
                                  "digest": digest}
    resign_plan(value)
    refuses(value, "classification entries do not match their digest")


def test_reconstructed_classifier_input_matches_the_canonical_classifier():
    cases = [
        row(id=1, text_content="real body", text_extraction_method="pymupdf"),
        row(id=2, text_content="   \n\t ", text_extraction_method="pymupdf"),
        row(id=3, text_content="   \n", text_extraction_method="ocr_local"),
        row(id=4, text_content="\t", text_extraction_method="pdftotext"),
        row(id=5, text_content=None, text_extraction_method="pymupdf"),
        row(id=6, text_content="", text_extraction_method="pymupdf"),
        row(id=7, text_content="   ", text_extraction_method="extraction_failed"),
        row(id=8, text_content="   ", text_extraction_method="quarantine:oversized:12"),
        row(id=9, text_content="", text_extraction_method=None),
    ]
    for case in cases:
        entry = P.classification_entry(case)
        assert eligibility.classify_document(P.classification_row(entry)) == \
            eligibility.classify_document(case), f"case {case['id']} diverged"
        assert entry["text_present"] is bool(str(case.get("text_content") or "").strip())


def test_whitespace_only_text_with_supported_methods_stays_unsupported_offline(tmp_path):
    rows = [row(id=1, text_content="   \n ", text_extraction_method="pymupdf"),
            row(id=2, text_content="\t\n", text_extraction_method="ocr_local"),
            row(id=3, text_content=" \n ", text_extraction_method="pdftotext")]
    for item in rows:
        entry = P.classification_entry(item)
        assert entry["text_present"] is False
        assert P.classification_row(entry)["text_content"] is None
        assert eligibility.classify_document(P.classification_row(entry))[0] == "unsupported"
    value = build_plan(rows, tmp_path=tmp_path)
    assert [record["outcome"] for record in value["records"]] == ["held"] * 3
    assert all(record["reason"] == "not_eligible:unsupported" for record in value["records"])
    assert value["accounting"]["planned"] == 0
    assert value["accounting"]["would_process"] == 0
    assert value["accounting"]["marked_processed"] == 0
    assert V.validate_plan(value, target=TARGET, hashes=V.code_hashes()) == []
    value["records"][0].update(outcome="planned",
                               reason="no_exact_identity_receipt_would_process",
                               would_process=True, marked_processed=False,
                               processing_proven=False)
    rehash(value)
    refuses(value, "contradicts the bound selection entry")


def test_a_missing_text_present_flag_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    binding = value["selection_binding"]
    source = Path(binding["path"])
    artifact = json.loads(source.read_text(encoding="utf-8"))
    artifact.pop("digest")
    artifact["entries"][0].pop("text_present")
    artifact["entries_sha256"] = P.eligibility_sha256(artifact["entries"])
    rewritten = source.parent / "selection-no-flag.json"
    digest = write_immutable(rewritten, artifact)
    value["selection_binding"] = {**binding, "path": str(rewritten), "digest": digest}
    resign_plan(value)
    refuses(value, "no canonical text_present flag")


def test_a_snapshot_claiming_text_without_the_digest_to_back_it_is_refused(tmp_path):
    value = build_plan([row()], tmp_path=tmp_path)
    binding = value["selection_binding"]
    source = Path(binding["path"])
    artifact = json.loads(source.read_text(encoding="utf-8"))
    artifact.pop("digest")
    artifact["entries"][0]["content_sha256"] = P.EMPTY_TEXT_SHA256
    artifact["entries_sha256"] = P.eligibility_sha256(artifact["entries"])
    rewritten = source.parent / "selection-empty-text.json"
    digest = write_immutable(rewritten, artifact)
    value["selection_binding"] = {**binding, "path": str(rewritten), "digest": digest}
    resign_plan(value)
    refuses(value, "claim text the retained digest does not contain")


def test_a_bound_set_with_an_unkeyable_invalid_receipt_refuses_every_claim(tmp_path):
    unkeyable = {"kind": R.RECEIPT_KIND, "version": R.RECEIPT_VERSION,
                 "producer_version": R.PRODUCER_VERSION, "processing_identity": None,
                 "status": "success", "reason": "", "recorded_at": NOW}
    missing_identity = {"kind": R.RECEIPT_KIND, "status": "success", "recorded_at": NOW}
    binding = receipt_set(tmp_path, [unkeyable])
    assert R.validate_receipt(unkeyable) and R._safe_key(unkeyable) is None
    _folded, problems = P.resolve_receipt_binding(binding)
    assert any("unkeyable invalid receipt" in problem for problem in problems)

    value = build_plan([row()], tmp_path=tmp_path)
    assert value["accounting"]["marked_processed"] == 0
    assert value["accounting"]["current_proven"] == 0
    value["receipts_binding"] = binding
    resign_plan(value)
    refuses(value, "unkeyable invalid receipt")
    assert value["accounting"]["current_proven"] == 0

    with pytest.raises(ValueError, match="unkeyable invalid receipt"):
        build_plan([row()], [unkeyable], tmp_path=tmp_path)
    with pytest.raises(ValueError, match="unkeyable invalid receipt"):
        build_plan([row()], [missing_identity], tmp_path=tmp_path)


def test_a_keyable_invalid_receipt_taints_only_its_own_identity(tmp_path):
    invalid_status = receipt_for(row(id=1), status="maybe")
    assert R.validate_receipt(invalid_status)
    assert R._safe_key(invalid_status) is not None
    value = build_plan([row(id=1), row(id=2)], [invalid_status], tmp_path=tmp_path)
    records = {record["source_id"]: record for record in value["records"]}
    assert records[1]["outcome"] == "held"
    assert records[1]["reason"] == "receipt_invalid_fail_closed"
    assert records[1]["marked_processed"] is False
    assert records[2]["outcome"] == "planned"
    assert value["accounting"]["planned"] == 1 and value["accounting"]["held"] == 1
    assert value["accounting"]["current_proven"] == 0
    assert V.validate_plan(value, target=TARGET, hashes=V.code_hashes()) == []
    records[1].update(outcome="planned", reason="no_exact_identity_receipt_would_process",
                      would_process=True)
    rehash(value)
    refuses(value, "contradicts the bound selection entry")


def test_the_validator_has_no_weakening_switch():
    import inspect

    signature = inspect.signature(V.validate_plan)
    assert set(signature.parameters) == {"plan", "target", "hashes", "repo"}
    with pytest.raises(TypeError):
        V.validate_plan({}, check_evidence=False)
