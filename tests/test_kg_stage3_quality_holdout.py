"""Adversarial tests for the untouched full-document Stage 3 holdout contract."""

import hashlib

import pytest

from scripts.kg import stage3_quality_holdout as H
from scripts.kg.stage2_artifacts import write_immutable


def doc(document_id, *, source="onbase", body="phoenix-cc", method="pymupdf_layout",
        doc_type="Meeting Result", predicate="approved"):
    text = f"RESULTS\nItem 3 — {predicate.title()}\n"
    span = text.index(predicate.title())
    return {"document_id": document_id, "source": source, "body": body,
            "document_type": doc_type, "extraction_method": method,
            "source_version": f"pdf:{document_id}:v1", "untouched": True,
            "retained_text": text, "content_sha256": H.text_sha256(text),
            "predictions": [{"prediction_id": f"p-{document_id}", "predicate": predicate,
                             "span": {"coordinate_system": H.COORDINATE_SYSTEM,
                                      "start": span, "end": span + len(predicate),
                                      "sha256": H.text_sha256(text[span:span + len(predicate)])}}]}


def packet(tmp_path):
    docs = [doc(10), doc(11, source="legistar", predicate="continued"),
            doc(12, body="mesa-cc", method="tesseract_tsv", predicate="adopted")]
    inventory = {"kind": "inventory", "digest": "inv"}
    value = H.build_packet(docs, seed="holdout-seed", max_documents=3, excluded_ids={99},
                           development_population_digest="a" * 64, inventory_binding=inventory,
                           created_at="2026-09-20T00:00:00Z")
    return value


def test_selection_is_deterministic_and_covers_source_body_method_predicate(tmp_path):
    first = packet(tmp_path)
    docs = [doc(12, body="mesa-cc", method="tesseract_tsv", predicate="adopted"),
            doc(10), doc(11, source="legistar", predicate="continued")]
    second = H.build_packet(docs, seed="holdout-seed", max_documents=3, excluded_ids={99},
                            development_population_digest="a" * 64,
                            inventory_binding={"kind": "inventory", "digest": "inv"},
                            created_at="2026-09-20T00:00:00Z")
    assert first["digest"] == second["digest"]
    assert first["sampling"]["selected_cells"] == 3
    assert all(item["review"]["gold_actions"] is None for item in first["items"])


def test_development_document_overlap_is_fail_closed():
    with pytest.raises(H.HoldoutRefused, match="overlaps"):
        H.build_packet([doc(10)], seed="s", max_documents=1, excluded_ids={10},
                       development_population_digest="b" * 64,
                       inventory_binding={}, created_at="now")


def test_inventory_is_read_only_and_marks_overlaps(tmp_path):
    path = tmp_path / "123.pdf"
    path.write_bytes(b"cached PDF bytes")
    inventory = H.inventory_cached_sources(tmp_path, excluded_ids={123})
    assert inventory["accounting"] == {"files": 1, "pdf_files": 1, "overlapping": 1, "eligible_unseen": 0}
    assert inventory["files"][0]["development_overlap"] is True


def test_full_document_labels_measure_recall_fp_qualifier_item_and_temporal():
    value = H.build_packet([doc(10)], seed="s", max_documents=1, excluded_ids=set(),
                           development_population_digest="c" * 64, inventory_binding={}, created_at="now")
    reviews = {"10": {"coverage": "complete",
             "gold_actions": [{"action_id": "a1", "predicate": "approved", "outcome_base": "approved",
                                "qualifier": "with_stipulations", "qualifier_text": "with stipulations",
                                "span": {"start": 0, "end": 1, "sha256": H.text_sha256("R")},
                                "item_reference": "3", "temporal_attribution": "current_meeting"},
                               {"action_id": "a2", "predicate": "continued", "outcome_base": "continued",
                                "qualifier": None, "qualifier_text": None,
                                "span": {"start": 0, "end": 1, "sha256": H.text_sha256("R")},
                                "item_reference": "4", "temporal_attribution": "prior_meeting"}],
             "prediction_labels": [{"prediction_id": "p-10", "status": "tp", "matched_action_id": "a1",
                                    "qualifier": "lost", "item_association": "correct",
                                    "temporal_attribution": "correct"}]}}
    metrics = H.evaluate_labels(value, reviews)
    assert metrics["metrics"]["precision"]["value"] == 1.0
    assert metrics["metrics"]["recall"]["value"] == 0.5
    assert metrics["metrics"]["new_false_positives"] == 0
    assert metrics["metrics"]["misses"] == 1
    assert metrics["metrics"]["qualifier_retention"]["value"] == 0.0
    assert metrics["metrics"]["temporal_attribution"]["value"] == 1.0
    assert metrics["metrics"]["by_source_body_document_type_extraction_method_predicate"]


def test_threshold_evaluator_requires_explicit_complete_slices_and_denominators():
    value = H.build_packet([doc(10)], seed="s", max_documents=1, excluded_ids=set(),
                           development_population_digest="d" * 64, inventory_binding={}, created_at="now")
    text = value["items"][0]["retained_text"]
    review = {"coverage": "complete", "gold_actions": [{
        "action_id": "a1", "predicate": "approved", "outcome_base": "approved",
        "qualifier": None, "qualifier_text": None,
        "span": {"start": 0, "end": 1, "sha256": H.text_sha256(text[:1])},
        "item_reference": "3", "temporal_attribution": "current_meeting"}],
        "prediction_labels": [{"prediction_id": "p-10", "status": "tp",
                               "matched_action_id": "a1", "qualifier": "not_applicable",
                               "item_association": "correct", "temporal_attribution": "correct"}]}
    evaluation = H.evaluate_labels(value, {"10": review})
    key = next(iter(evaluation["metrics"]["by_source_body_document_type_extraction_method_predicate"]))
    policy = {"version": "holdout-thresholds/1.0", "approved": True,
              "approved_by": "human", "approved_at": "2026-09-20T00:00:00Z",
              "minimum_denominators": {"precision": 1, "recall": 1},
              "by_slice": {key: {"precision": 1.0, "recall": 1.0}}}
    assert H.evaluate_thresholds(evaluation, policy)["status"] == "PASS"
    missing = dict(policy); missing["by_slice"] = {}
    with pytest.raises(H.HoldoutRefused, match="exactly match observed slices"):
        H.evaluate_thresholds(evaluation, missing)
    undersized = dict(policy); undersized["minimum_denominators"] = {"precision": 2, "recall": 1}
    with pytest.raises(H.HoldoutRefused, match="undersized precision"):
        H.evaluate_thresholds(evaluation, undersized)
    empty_evaluation = {"packet_digest": evaluation["packet_digest"], "documents": [],
                        "totals": {}, "metrics": {"by_source_body_document_type_extraction_method_predicate": {}}}
    empty_evaluation["evaluation_digest"] = H.canonical_sha256(empty_evaluation)
    with pytest.raises(H.HoldoutRefused, match="no observed slices"):
        H.evaluate_thresholds(empty_evaluation, policy)
    forged = dict(evaluation)
    forged["metrics"] = dict(evaluation["metrics"])
    forged["metrics"]["by_source_body_document_type_extraction_method_predicate"] = dict(
        evaluation["metrics"]["by_source_body_document_type_extraction_method_predicate"])
    forged["metrics"]["by_source_body_document_type_extraction_method_predicate"][key] = dict(
        forged["metrics"]["by_source_body_document_type_extraction_method_predicate"][key])
    forged["metrics"]["by_source_body_document_type_extraction_method_predicate"][key]["precision"] = {
        "numerator": 0, "denominator": 1, "value": 0.0, "defined": True}
    forged["evaluation_digest"] = H.canonical_sha256({k: v for k, v in forged.items() if k != "evaluation_digest"})
    with pytest.raises(H.HoldoutRefused, match="ratios do not match counts"):
        H.evaluate_thresholds(forged, policy)


def test_temporal_prediction_must_be_correctness_label_not_gold_class():
    value = H.build_packet([doc(10)], seed="s", max_documents=1, excluded_ids=set(),
                           development_population_digest="e" * 64, inventory_binding={}, created_at="now")
    with pytest.raises(H.HoldoutRefused, match="temporal_attribution label"):
        H.evaluate_labels(value, {"10": {"coverage": "complete", "gold_actions": [],
            "prediction_labels": [{"prediction_id": "p-10", "status": "fp",
                                    "matched_action_id": None, "qualifier": "not_applicable",
                                    "item_association": "not_applicable",
                                    "temporal_attribution": "current_meeting"}]}})


def test_empty_no_candidate_evaluation_cannot_reach_threshold_gate():
    empty_doc = doc(10)
    empty_doc["predictions"] = []
    value = H.build_packet([empty_doc], seed="s", max_documents=1, excluded_ids=set(),
                           development_population_digest="f" * 64, inventory_binding={}, created_at="now")
    with pytest.raises(H.HoldoutRefused, match="no predicate slices"):
        H.evaluate_labels(value, {"10": {"coverage": "complete", "gold_actions": [],
                                          "prediction_labels": []}})


def test_threshold_gate_rejects_malformed_timestamp_and_duplicate_prediction_labels():
    value = H.build_packet([doc(10)], seed="s", max_documents=1, excluded_ids=set(),
                           development_population_digest="g" * 64, inventory_binding={}, created_at="now")
    review = {"coverage": "complete", "gold_actions": [], "prediction_labels": [
        {"prediction_id": "p-10", "status": "fp", "matched_action_id": None,
         "qualifier": "not_applicable", "item_association": "not_applicable",
         "temporal_attribution": "not_applicable"},
        {"prediction_id": "p-10", "status": "fp", "matched_action_id": None,
         "qualifier": "not_applicable", "item_association": "not_applicable",
         "temporal_attribution": "not_applicable"}]}
    with pytest.raises(H.HoldoutRefused, match="duplicate prediction labels"):
        H.evaluate_labels(value, {"10": review})
    # A valid single-label review produces an evaluation whose threshold policy
    # must still carry an ISO-8601 timestamp.
    review["prediction_labels"] = review["prediction_labels"][:1]
    evaluation = H.evaluate_labels(value, {"10": review})
    key = next(iter(evaluation["metrics"]["by_source_body_document_type_extraction_method_predicate"]))
    policy = {"version": "holdout-thresholds/1.0", "approved": True,
              "approved_by": "human", "approved_at": "not-a-timestamp",
              "minimum_denominators": {"precision": 1, "recall": 1},
              "by_slice": {key: {"precision": 1.0, "recall": 1.0}}}
    with pytest.raises(H.HoldoutRefused, match="ISO-8601"):
        H.evaluate_thresholds(evaluation, policy)


def test_prediction_label_set_must_be_complete():
    value = H.build_packet([doc(10)], seed="s", max_documents=1, excluded_ids=set(),
                           development_population_digest="h" * 64, inventory_binding={}, created_at="now")
    review = {"coverage": "complete", "gold_actions": [], "prediction_labels": []}
    with pytest.raises(H.HoldoutRefused, match="prediction labels are not complete"):
        H.evaluate_labels(value, {"10": review})


def test_correction_proposal_is_evidence_only_and_immutable(tmp_path):
    # Build minimal digest-bound packet/label artifacts with the same case IDs.
    case_items = []
    for case_id in ("meeting_event_extraction:47369", "meeting_event_extraction:56289"):
        case_items.append({"case_id": case_id, "document": {"source_id": 7},
                           "candidate": {"predicate": "Approved"},
                           "evidence_record": {"start": 1, "end": 3},
                           "evidence": {"snippet": "x"}})
    packet_body = {"items": case_items}
    packet_body["digest"] = H.canonical_sha256({"items": case_items})
    packet_path = tmp_path / "packet.json"
    write_immutable(packet_path, packet_body)
    labels_body = {"packet_digest": packet_body["digest"], "labels": {
        case_id: {"decision": "accept", "notes": "review"} for case_id in
        ("meeting_event_extraction:47369", "meeting_event_extraction:56289")}}
    labels_path = tmp_path / "labels.json"
    write_immutable(labels_path, labels_body)
    proposal = H.build_correction_proposals(packet_path, labels_path, created_at="now")
    assert proposal["mode"] == "evidence-only"
    assert all(case["adjudication"] is None for case in proposal["cases"])
    assert proposal["digest"] == H.canonical_sha256({k: v for k, v in proposal.items() if k != "digest"})
