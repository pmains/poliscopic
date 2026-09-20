from datetime import datetime, timezone

from scripts.kg import stage3_source_eligibility as E


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


def doc(**changes):
    row = {"id": 1, "body": "x", "document_type": "Agenda",
           "document_url": "https://civic.example/a.pdf", "text_content": "text",
           "text_extraction_method": "pymupdf", "text_extracted_at": NOW,
           "scraped_at": OLD, "content_hash": "source", "meeting_db_id": 1,
           "agenda_item_db_id": None}
    row.update(changes)
    return row


def test_document_dispositions_are_mutually_exclusive():
    cases = [
        (doc(), "eligible_current"),
        (doc(scraped_at=NOW, text_extracted_at=OLD), "eligible_stale"),
        (doc(text_content=None, text_extraction_method=None), "missing_text"),
        (doc(text_content=None, text_extraction_method="extraction_failed"), "unreadable"),
        (doc(text_content=None, text_extraction_method="quarantine:oversized:9"), "unsupported"),
        (doc(text_extraction_method="mystery"), "unsupported"),
    ]
    assert [E.classify_document(row)[0] for row, _ in cases] == [want for _, want in cases]


def test_metadata_update_is_not_staleness_signal():
    row = doc(updated_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert E.classify_document(row)[0] == "eligible_current"


def test_agenda_item_text_rule():
    assert E.classify_item({"agenda_item_text": "body"})[0] == "eligible_current"
    assert E.classify_item({"agenda_item_text": " "})[0] == "missing_text"


def test_population_reconciles_and_exceptions_are_exact():
    rows = {
        "documents": [doc(), doc(id=2, text_content=None,
                                  text_extraction_method="download_failed")],
        "agenda_items": [
            {"id": 3, "body": "x", "source_body": "x", "agenda_item_text": "item",
             "meeting_db_id": 1, "lifecycle_status": "unknown"},
            {"id": 4, "body": "x", "source_body": "x", "agenda_item_text": None,
             "meeting_db_id": 1, "lifecycle_status": "unknown"},
        ],
    }
    value = E.build_baseline(
        rows=rows, created_at="now", target={"tier": "development"},
        schema={"sha256": "s"}, hashes={"code": "h"})
    assert value["accounting"]["discovered"] == 4
    assert value["accounting"]["classified"] == 4
    assert value["accounting"]["reconciles"] is True
    assert value["exception_count"] == 2
    assert {(x["source_kind"], x["id"]) for x in value["exceptions"]} == {
        ("supporting_document", 2), ("agenda_item", 4)}


def test_no_apply_or_write_surface():
    assert not hasattr(E, "apply")
    assert not hasattr(E, "update")
    assert not hasattr(E, "delete")
