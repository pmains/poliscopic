"""Regression tests for layout-preserving document and event extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.docs import layout_extract as layout
from scripts.docs.extract import extract_document_safe
from scripts.entities.event_extract import extract_events_from_text
from scripts.kg.registries.evidence import evidence_class_for_extraction_method


def _result_pdf(path):
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((42, 100), "Heard")
    page.insert_text((160, 100), "1. Performance update")
    page.insert_text((42, 125), "Approved")
    page.insert_text((160, 125), "2. Assignment change")
    page.insert_text((42, 150), "Denied")
    page.insert_text((160, 150), "3. Appeal")
    document.save(path)
    document.close()


def test_native_extraction_preserves_rows_boxes_and_offsets(tmp_path, monkeypatch):
    pdf = tmp_path / "results.pdf"
    _result_pdf(pdf)

    extracted = layout.extract_native(pdf)
    assert extracted is not None
    text, artifact = extracted
    assert artifact["method"] == "pymupdf_layout"
    assert artifact["diagnostics"]["coordinate_system"] == "pdf_points_top_left"
    assert "Approved 2. Assignment change" in text

    approved_row = next(
        row for row in artifact["pages"][0]["rows"] if "Approved" in row["text"]
    )
    assert approved_row["bbox"] is not None
    assert text[approved_row["text_start"]:approved_row["text_end"]] == approved_row["text"]
    token = approved_row["tokens"][0]
    assert text[token["text_start"]:token["text_end"]] == "Approved"
    evidence = layout.evidence_for_span(
        artifact, token["text_start"], token["text_end"]
    )
    assert evidence["page"] == 1
    assert evidence["row_id"] == approved_row["row_id"]
    assert evidence["span_bbox"] == token["bbox"]

    monkeypatch.setattr(layout, "LAYOUT_DIR", tmp_path / "artifacts")
    path = layout.write_artifact(artifact)
    assert layout.write_artifact(artifact) == path  # identical replay
    assert json.loads(path.read_text())["retained_text_sha256"] == layout.sha256_text(text)
    assert layout.load_artifact_for_text(
        text, artifact["source_pdf_sha256"]
    )["source_pdf_sha256"] == artifact["source_pdf_sha256"]


def test_application_anchor_infers_bounded_result_region():
    page = {
        "page": 1, "height": 792,
        "rows": [
            {"bbox": [100, 100, 500, 112], "text": "# 5, 6 and 7 Denied 11. Application #:",
             "tokens": [
                 {"text": "#", "bbox": [100, 100, 108, 112], "text_start": 0, "text_end": 1},
                 {"text": "Denied", "bbox": [120, 100, 170, 112], "text_start": 2, "text_end": 8},
                 {"text": "11.", "bbox": [240, 100, 260, 112], "text_start": 9, "text_end": 12},
                 {"text": "Application", "bbox": [295, 100, 365, 112], "text_start": 13, "text_end": 24},
                 {"text": "#:", "bbox": [370, 100, 380, 112], "text_start": 25, "text_end": 27},
             ]},
            {"bbox": [125, 115, 550, 127], "text": "Filed Quarter Section Q12-28(G8)",
             "tokens": [
                 {"text": "Filed", "bbox": [125, 115, 155, 127], "text_start": 28, "text_end": 33},
                 {"text": "Quarter", "bbox": [295, 115, 340, 127], "text_start": 34, "text_end": 41},
             ]},
        ],
    }
    regions = layout._infer_result_regions(page)
    assert len(regions) == 1
    assert regions[0]["role"] == "result_candidate"
    assert regions[0]["item_number"] == "11"
    assert regions[0]["text"] == "# Denied 11. Filed"
    assert "Quarter" not in regions[0]["text"]


def test_identical_text_from_independent_pdfs_does_not_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "LAYOUT_DIR", tmp_path / "artifacts")
    text = "Approved"
    base = {
        "kind": "document-layout",
        "version": layout.LAYOUT_ARTIFACT_VERSION,
        "retained_text_sha256": layout.sha256_text(text),
        "pages": [],
    }
    first = dict(base, source_pdf_sha256="a" * 64)
    second = dict(base, source_pdf_sha256="b" * 64)
    assert layout.write_artifact(first) != layout.write_artifact(second)
    assert layout.load_artifact_for_text(text) is None
    assert layout.load_artifact_for_text(text, "a" * 64)["source_pdf_sha256"] == "a" * 64


def test_governed_cascade_prefers_native_geometry(tmp_path):
    pdf = tmp_path / "results.pdf"
    _result_pdf(pdf)
    text, method, artifact = extract_document_safe(pdf)
    assert method == "pymupdf_layout"
    assert artifact is not None
    assert artifact["retained_text_sha256"] == layout.sha256_text(text)


def test_layout_methods_are_registered_as_source_evidence():
    assert evidence_class_for_extraction_method("pymupdf_layout") == "source_pdf_text"
    assert evidence_class_for_extraction_method("pdftotext_layout") == "source_pdf_text"
    assert evidence_class_for_extraction_method("tesseract_tsv") == "source_ocr"


def test_real_multpage_fixture_retains_page_and_row_geometry():
    fixture = Path(__file__).parent / "fixtures" / "tempe" / "1687_summary.pdf"
    text, method, artifact = extract_document_safe(fixture)
    assert method == "pymupdf_layout"
    assert "LEGAL ACTION SUMMARY" in text
    assert len(artifact["pages"]) == 12
    assert all(page["rows"] for page in artifact["pages"])


def test_event_extraction_accepts_later_rows_and_compound_actions(tmp_path):
    pdf = tmp_path / "results.pdf"
    _result_pdf(pdf)
    text, artifact = layout.extract_native(pdf)

    events = extract_events_from_text(1, text, artifact)
    assert [event["action_verb"] for event in events] == ["Approved", "Denied"]
    for event in events:
        assert text[event["text_offset_start"]:event["text_offset_end"]] == event["action_verb"]

    compound = "Discussed and Approved"
    compound_events = extract_events_from_text(2, compound)
    assert [event["outcome"] for event in compound_events] == ["discussed", "approved"]
    assert len({event["compound_result_group_id"] for event in compound_events}) == 1
    assert [event["compound_result_member_index"] for event in compound_events] == [1, 2]
    assert all(event["compound_result_member_count"] == 2 for event in compound_events)
    assert all(
        event["compound_result_qualifier_context"] == compound
        for event in compound_events
    )
    assert all(
        (event["compound_result_span_start"], event["compound_result_span_end"])
        == (0, len(compound))
        for event in compound_events
    )

    assert extract_events_from_text(3, "Approved by unanimous vote")[0]["outcome"] == "approved"


@pytest.mark.parametrize(
    "text",
    [
        "Continued page 2 of 2",
        "Application ZA-1 (Continued from October 4, 2025)",
        "The hearing will proceed unless continued.",
        "Employees' Deferred Compensation Board",
        "The position was Introduced in 2023.",
        "This item was not discussed.",
        "Any approved above-ground service lines must be screened.",
        "Preliminary Review of Preliminary Site Plan",
        "AMENDED Meeting Minutes",
    ],
)
def test_high_confidence_non_results_are_rejected(text):
    assert extract_events_from_text(1, text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Additional federal funding has been received to install fiber on Happy Valley Road.",
        "In accordance with a request, received and filed with the City Clerk on September 15.",
        "Chairperson Dalton extended her appreciation of the aquatics team.",
        "The proposal shall reflect the adopted Algodon Comprehensive Sign Plan.",
        "shall reflect common ground elements with the\nadopted Algodon Comprehensive Sign Plan as determined by staff",
        "c. Deferred Retirement Option Plan",
        "(Approved RH/R1-10 PCD) (Ranch or Farm Residence District) to PUD",
        "PCD) (Ranch or Farm Residence,\nApproved Single-Family\nResidence District to PUD",
        "PCD) (Ranch or Farm Residence,\nApproved Multifamily\nResidence District to PUD",
        "Residence, Approved Intermediate Commercial, Planned",
        "In accordance with a request from the Mayor of the City of Phoenix, received and filed with the City\nClerk on September 15.",
    ],
)
def test_reviewed_semantic_non_results_are_rejected(text):
    assert extract_events_from_text(1, text) == []


@pytest.mark.parametrize(
    "text,outcome",
    [
        ("Funding received", "received"),
        ("Received and filed", "received"),
        ("Extended to December 1", "extended"),
        ("Adopted General Plan", "adopted"),
        ("Deferred to the next meeting", "deferred"),
        ("Approved by unanimous vote", "approved"),
    ],
)
def test_semantic_guards_preserve_real_results(text, outcome):
    events = extract_events_from_text(1, text)
    assert [event["outcome"] for event in events] == [outcome]


def test_negation_guard_does_not_cross_into_adjacent_result():
    events = extract_events_from_text(1, "Not discussed\nDiscussed")
    assert [event["outcome"] for event in events] == ["discussed"]


def test_explicit_no_action_survives_earlier_agenda_wording():
    text = "For discussion only. No action will take place at this meeting."
    assert [event["outcome"] for event in extract_events_from_text(1, text)] == [
        "no_action"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "The Council may discuss the proposed agreement.",
        "The application will be approved at a future meeting.",
        "The item was not approved.",
        "Executive Session: discussion or consultation regarding legal advice.",
        "Agenda: For discussion and possible action; the plan was discussed.",
        "The proposal was previously approved by the commission.",
        "The item was discussed at the prior meeting.",
        'The minutes state that the item was \"approved\".',
        "1. Zoning case description  2. Approved",
    ],
)
def test_non_current_result_contexts_fail_closed(text):
    assert extract_events_from_text(1, text) == []


@pytest.mark.parametrize(
    "text,outcomes",
    [
        ("Approved", ["approved"]),
        ("1. Approved by unanimous vote", ["approved"]),
        ("Continued to the October 8 meeting", ["continued"]),
        ("Executive Session concluded; No Action", ["no_action"]),
        ("Approved following public discussion", ["approved"]),
    ],
)
def test_context_classifier_preserves_current_results_and_offsets(text, outcomes):
    events = extract_events_from_text(1, text)
    assert [event["outcome"] for event in events] == outcomes
    for event in events:
        start, end = event["text_offset_start"], event["text_offset_end"]
        assert text[start:end] == event["action_verb"]


def test_compound_result_group_is_stable_and_preserves_member_offsets():
    text = "Denied as Filed / Approved Subject to Stipulations"
    first = extract_events_from_text(91, text)
    second = extract_events_from_text(91, text)
    assert [event["outcome"] for event in first] == [
        "denied", "approved_with_conditions",
    ]
    assert [event["compound_result_group_id"] for event in first] == [
        event["compound_result_group_id"] for event in second
    ]
    assert first[0]["compound_result_group_id"] == first[1]["compound_result_group_id"]
    for event in first:
        start, end = event["text_offset_start"], event["text_offset_end"]
        assert text[start:end] == event["action_verb"]


def test_compound_results_never_join_across_logical_rows_without_geometry():
    events = extract_events_from_text(92, "Discussed\nApproved")
    assert [event["outcome"] for event in events] == ["discussed", "approved"]
    assert all("compound_result_group_id" not in event for event in events)


def test_compound_group_requires_connector_only_text_between_actions():
    events = extract_events_from_text(94, "Discussed application and Approved plans")
    assert [event["outcome"] for event in events] == ["discussed", "approved"]
    assert all("compound_result_group_id" not in event for event in events)


def test_standalone_event_shape_remains_backward_compatible():
    event = extract_events_from_text(93, "Approved")[0]
    assert not any(key.startswith("compound_result_") for key in event)
