"""Tests for the read-only layout extraction diagnostic benchmark."""

import subprocess
import sys
from pathlib import Path

from scripts.docs.benchmark_layout_sample import (
    _canonical_outcome,
    _classify_match,
    _context_score,
    _local_context,
    _outcome_oracle_digest,
    apply_label_corrections,
    select_full_packet,
    select_replay,
    select_sample,
)
from scripts.kg import stage3_review_label_corrections as corrections

REPO = Path(__file__).resolve().parents[1]


def _case(index, decision, predicate, method="pdftotext"):
    case_id = f"meeting_event_extraction:{index}"
    item = {
        "case_id": case_id,
        "candidate": {"predicate": predicate},
        "document": {"source_id": index},
        "evidence": {"start": 0, "end": len(predicate)},
        "projection": {"dimensions": {
            "body": "phoenix-test", "extraction_method": method,
            "platform_or_source": f"https://example.test/{index}.pdf",
        }},
    }
    label = {"decision": decision, "notes": ""}
    source = {"case_id": case_id, "retained_text": predicate}
    return item, label, source


def test_context_score_handles_reordered_table_words():
    assert _context_score(
        "Approved 3. Application ZA-296-26-4",
        "3 Application ZA 296 26 4 Approved",
    ) > 0.7
    assert _context_score("Preliminary review parcel", "Deferred compensation") < 0.2


def test_local_context_is_bounded_to_adjacent_lines():
    text = "header\nApproved 3. Assignment\nnext detail\nfar away"
    start = text.index("Approved")
    assert _local_context(text, start, start + 8) == "header Approved 3. Assignment next detail"


def test_canonical_outcome_uses_frozen_oracle():
    assert _canonical_outcome("Approved with stipulations") == "approved_with_conditions"
    assert _canonical_outcome("Preliminary Review") == "discussed"
    assert _canonical_outcome("For discussion") == "discussed"
    assert len(_outcome_oracle_digest()) == 64


def test_matcher_does_not_call_unaligned_emitted_outcome_a_miss():
    assert _classify_match("accept", False, True) == ("alignment_unresolved", None)
    assert _classify_match("accept", False, False) == ("missed", False)
    assert _classify_match("reject", False, True) == ("suppressed", True)


def test_correction_overlay_preserves_base_and_changes_effective_label(tmp_path):
    base = {"labels": {"case": {"decision": "accept", "extraction": "tp", "notes": "old"}}}
    base_path = tmp_path / "labels.json"
    base_path.write_text(__import__("json").dumps(base))
    document = {
        "kind": "kg-stage3-review-label-corrections",
        "base_labels_sha256": corrections.file_sha256(base_path),
        "corrections": [{
            "case_id": "case",
            "before": {"decision": "accept", "extraction": "tp", "notes": "old"},
            "after": {"decision": "reject", "extraction": "fp", "notes": "new"},
            "reason": "test", "evidence": {"source": "fixture"},
        }],
    }
    assert corrections.validate_corrections(document, base_path) == []
    effective = apply_label_corrections(base, document, base_path=base_path)
    assert base["labels"]["case"]["decision"] == "accept"
    assert effective["labels"]["case"]["decision"] == "reject"


def test_selection_is_balanced_unique_and_includes_ocr_documents():
    rows = []
    labels = {}
    sources = []
    for index in range(1, 13):
        decision = "reject" if index <= 6 else "accept"
        method = "ocr_local" if index in {7, 8} else "pdftotext"
        item, label, source = _case(index, decision, f"Predicate {index % 3}", method)
        rows.append(item)
        labels[item["case_id"]] = label
        sources.append(source)
    selected = select_sample(
        {"items": rows}, {"labels": labels}, {"candidate_cases": sources}, 8
    )
    assert len(selected) == 8
    assert len({row["source_id"] for row in selected}) == 8
    assert sum(row["decision"] == "accept" for row in selected) == 4
    assert sum(row["decision"] == "reject" for row in selected) == 4
    assert {7, 8} <= {row["source_id"] for row in selected}

    replayed = select_replay(
        {"items": rows}, {"labels": labels}, {"candidate_cases": sources},
        [row["case_id"] for row in reversed(selected)],
    )
    assert [row["case_id"] for row in replayed] == [
        row["case_id"] for row in reversed(selected)
    ]


def test_full_packet_keeps_multiple_reviewed_cases_from_one_document():
    first, first_label, first_source = _case(1, "accept", "Approved")
    second, second_label, second_source = _case(2, "reject", "Discussed")
    second["document"]["source_id"] = first["document"]["source_id"]
    selected = select_full_packet(
        {"items": [first, second]},
        {"labels": {first["case_id"]: first_label, second["case_id"]: second_label}},
        {"candidate_cases": [first_source, second_source]},
    )
    assert [row["case_id"] for row in selected] == [first["case_id"], second["case_id"]]
    assert len({row["source_id"] for row in selected}) == 1


def test_standalone_cli_imports_workspace_modules():
    result = subprocess.run(
        [sys.executable, "scripts/docs/benchmark_layout_sample.py", "--help"],
        cwd=REPO, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--sample-size" in result.stdout
    assert "--full-packet" in result.stdout
