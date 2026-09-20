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
                                "span": {}, "item_reference": "3", "temporal_attribution": "current_meeting"},
                               {"action_id": "a2", "predicate": "continued", "outcome_base": "continued",
                                "qualifier": None, "qualifier_text": None, "span": {},
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


def test_prediction_label_set_must_be_complete():
    value = packet(None)
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
