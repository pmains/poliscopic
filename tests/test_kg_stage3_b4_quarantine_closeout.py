from copy import deepcopy

from scripts.kg import stage3_b4_quarantine_closeout as B4


def row(row_id):
    text = "before Approved after"
    return {
        "id": row_id, "supporting_doc_id": B4.SENTINEL_DOCUMENT_ID,
        "extractor": "pattern", "extractor_version": "v1", "action_verb": "Approved",
        "raw_text": text, "text_offset_start": 7, "text_offset_end": 15,
        "quarantine_reason": B4.SENTINEL_REASON, "quarantined_at": "when",
        "quarantined_by": B4.SENTINEL_ADJUDICATOR,
        "decision_id": B4.SENTINEL_DECISION_ID, "model_version": "kg-model/1.0",
        "body": "__skip__", "meeting_db_id": 15841, "document_title": "Annual Notice",
        "document_url": "https://example.test/notice.pdf",
        "text_extraction_method": "pdftotext", "text_content": text,
    }


def artifact():
    all_ids = sorted(B4.ADJUDICATED_SENTINEL_IDS)
    retired = all_ids[:9]
    rows = [row(i) for i in all_ids[9:]]
    return B4.build_closeout(
        rows=rows, retired_ids=retired, dedup_sources=["receipt", "rollback"],
        created_at="now", target={"tier": "development"}, hashes={"code": "hash"})


def test_exact_18_equals_9_retired_plus_9_surviving():
    value = artifact()
    assert value["accounting"] == {
        "adjudicated": 18, "retired_by_dedup": 9,
        "surviving_quarantined": 9, "reconciles": True}
    assert value["verdict"] == "CLOSED_GOVERNED_EXCEPTION"
    assert value["mutations_proposed"] == 0


def test_rows_bind_offsets_content_authority_and_lineage():
    item = artifact()["rows"][0]
    assert item["offsets_valid"] is True
    assert item["span_sha256"]
    assert item["document_text_sha256"]
    assert item["supporting_doc_id"] == 112947
    assert item["quarantine"]["decision_id"] == B4.SENTINEL_DECISION_ID


def test_missing_survivor_refuses():
    all_ids = sorted(B4.ADJUDICATED_SENTINEL_IDS)
    value = B4.build_closeout(
        rows=[row(i) for i in all_ids[9:-1]], retired_ids=all_ids[:9],
        dedup_sources=[], created_at="now", target={}, hashes={})
    assert value["verdict"] == "REFUSED"


def test_wrong_authority_and_invalid_offsets_refuse():
    all_ids = sorted(B4.ADJUDICATED_SENTINEL_IDS)
    rows = [row(i) for i in all_ids[9:]]
    rows[0]["quarantined_by"] = "someone else"
    rows[1]["text_offset_end"] = 999
    value = B4.build_closeout(
        rows=rows, retired_ids=all_ids[:9], dedup_sources=[],
        created_at="now", target={}, hashes={})
    assert len(value["problems"]) == 2


def test_artifact_digest_and_tamper_detection():
    value = artifact()
    assert value["digest"] == B4.canonical_sha256(
        {k: v for k, v in value.items() if k != "digest"})
    changed = deepcopy(value)
    changed["survivor_ids"][0] = -1
    assert changed != value


def test_no_apply_or_write_surface():
    assert not hasattr(B4, "apply")
    assert not hasattr(B4, "update")
    assert not hasattr(B4, "delete")
