"""Adversarial tests for exact, read-only B3 span-to-item linkage planning."""

from pathlib import Path

from scripts.kg import stage3_b3_item_linkage_plan as subject


REAL_UPSTREAM = Path("data/kg-plans/kg-stage3-b3-held-span-disposition-20260915T021800Z.json")


def upstream():
    records = []
    identities = [(1, subject.EVENT_KIND, 0, 2), (2, subject.EVENT_KIND, 0, 2),
                  (3, subject.RESULT_KIND, 0, 11), (4, subject.RESULT_KIND, 0, 11),
                  (5, subject.EVENT_KIND, 0, 2), (6, subject.RESULT_KIND, 0, 11)]
    for index, identity in enumerate(identities):
        records.append({"source_index": index, "outcome": "meeting_linked",
                        "evidence_identity": {"span_identity": list(identity)}})
    return {"digest": "u" * 64, "dispositions": records}


def rows():
    return {
        "documents": [
            {"id": 1, "meeting_db_id": 10, "agenda_item_db_id": 100,
             "text_content": "abcdef", "text_extraction_method": "fixture"},
            {"id": 2, "meeting_db_id": 10, "agenda_item_db_id": None,
             "text_content": "abcdef", "text_extraction_method": "fixture"},
            {"id": 3, "meeting_db_id": 10, "agenda_item_db_id": None,
             "document_type": "Meeting Result", "text_content": "1. Adopted\n", "text_extraction_method": "fixture"},
            {"id": 4, "meeting_db_id": 10, "agenda_item_db_id": None,
             "document_type": "Meeting Result", "text_content": "1. Adopted\n", "text_extraction_method": "fixture"},
            {"id": 5, "meeting_db_id": 10, "agenda_item_db_id": 200,
             "text_content": "abcdef", "text_extraction_method": "fixture"},
            {"id": 6, "meeting_db_id": 10, "agenda_item_db_id": None,
             "document_type": "Meeting Result", "text_content": "1. Adopted\n", "text_extraction_method": "fixture"},
        ],
        "events": [
            {"id": 20, "supporting_doc_id": 2, "agenda_item_id": 101,
             "text_offset_start": 0, "text_offset_end": 2},
        ],
        "items": [
            {"id": 100, "meeting_db_id": 10, "agenda_item_number": "A"},
            {"id": 101, "meeting_db_id": 10, "agenda_item_number": "B"},
            {"id": 102, "meeting_db_id": 10, "agenda_item_number": "1"},
            {"id": 103, "meeting_db_id": 10, "agenda_item_number": "1"},
            {"id": 200, "meeting_db_id": 11, "agenda_item_number": "C"},
        ],
    }


def test_exact_routes_are_exclusive_and_cross_meeting_is_held():
    result = subject.classify(upstream=upstream(), rows=rows())["records"]
    assert [row["outcome"] for row in result] == [
        "would_link_document_item", "would_link_event_item", "hold_result_target_ambiguous",
        "hold_result_target_ambiguous", "hold_cross_meeting_target", "hold_result_target_ambiguous",
    ]
    assert result[0]["agenda_item_db_id"] == 100
    assert result[1]["agenda_item_db_id"] == 101
    assert "agenda_item_db_id" not in result[4]


def test_result_number_needs_one_exact_same_meeting_target():
    current = rows()
    current["items"] = [row for row in current["items"] if row["id"] != 103]
    records = subject.classify(upstream=upstream(), rows=current)["records"]
    assert records[2]["outcome"] == "would_link_result_number"
    assert records[2]["agenda_item_db_id"] == 102
    current["items"] = [row for row in current["items"] if row["id"] != 102]
    assert subject.classify(upstream=upstream(), rows=current)["records"][2]["outcome"] == "hold_result_target_missing"


def test_missing_lineage_and_span_drift_are_quarantined():
    current = rows()
    current["events"] = []
    current["documents"][2]["text_content"] = "not an item\n"
    records = subject.classify(upstream=upstream(), rows=current)["records"]
    assert records[1]["outcome"] == "hold_lineage_gap"
    assert records[2]["outcome"] == "hold_result_span_drift"


def test_source_text_is_required_and_offsets_cannot_escape_it():
    current = rows()
    current["documents"][0]["text_content"] = None
    current["documents"][1]["text_content"] = "x"
    records = subject.classify(upstream=upstream(), rows=current)["records"]
    assert records[0]["outcome"] == "hold_source_gap"
    assert records[1]["outcome"] == "hold_evidence_drift"


def test_real_governed_population_is_bound_and_empty_source_never_links():
    """A reconstructed plan must retain every real held identity, even on a source gap."""
    empty = {"documents": [], "events": [], "items": []}
    plan = subject.build_plan(
        upstream_path=REAL_UPSTREAM, rows=empty, target={"tier": "test"},
        created_at="2026-09-20T00:00:00Z")
    assert plan["accounting"]["population"] == 36_635
    assert plan["accounting"]["linked"] == 0
    assert plan["accounting"]["held"] == 36_635
    assert plan["accounting"]["by_outcome"]["hold_source_gap"] == 36_635
    assert subject.validate_plan(plan, upstream_path=REAL_UPSTREAM, rows=empty) == []


def test_governed_source_projection_is_exactly_bounded_to_held_documents():
    assert subject.governed_document_ids(upstream()) == [1, 2, 3, 4, 5, 6]
    assert "WHERE id IN :document_ids" in subject.DOCUMENTS_SQL
    assert "WHERE supporting_doc_id IN :document_ids" in subject.EVENTS_SQL
    assert "WHERE meeting_db_id IN :meeting_ids" in subject.ITEMS_SQL


def test_module_declares_no_apply_surface_or_database_writes():
    source = Path(subject.__file__).read_text(encoding="utf-8")
    assert not hasattr(subject, "apply")
    assert "INSERT INTO" not in source
    assert "UPDATE " not in source
    assert "DELETE FROM" not in source
    assert "DROP TABLE" not in source
