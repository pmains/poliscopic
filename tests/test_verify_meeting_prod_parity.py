from scripts.ops.verify_meeting_prod_parity import compare


def meeting(updated="2026-10-06T12:00:00+00:00", title="Council"):
    return {
        "meeting_date": "2026-10-06", "meeting_type": "Regular",
        "meeting_title": title, "source_url": "https://example.test/1",
        "sync_status": "complete", "item_count_expected": 2,
        "item_count_actual": 2, "supporting_doc_count": 1,
        "items_extracted": True, "supporting_docs_extracted": True,
        "minutes_url": None, "updated_at": updated,
    }


def test_parity_succeeds_with_extra_production_rows():
    dev = {("city-cc", "1"): meeting()}
    prod = {**dev, ("old-cc", "9"): meeting()}
    assert compare(dev, prod)["status"] == "succeeded"


def test_parity_fails_for_missing_or_stale_meetings():
    dev = {("city-cc", "1"): meeting(), ("city-cc", "2"): meeting()}
    prod = {("city-cc", "1"): meeting(updated="2026-10-05T12:00:00+00:00")}
    result = compare(dev, prod)
    assert result["status"] == "failed"
    assert result["missing_count"] == 1
    assert result["mismatch_count"] == 1
    assert result["mismatch_examples"][0]["fields"] == ["updated_at"]
