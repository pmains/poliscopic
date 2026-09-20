from pathlib import Path

import pytest

from scripts.kg import stage3_b3_result_derived_packet as packet
from scripts.kg import stage3_b3_result_derived_apply as apply
from scripts.kg import stage3_b3_result_derived_plan as subject
from scripts.kg.stage2_artifacts import load_verified, write_immutable


def linkage(tmp_path: Path) -> Path:
    rows = [
        {"source_index": 0, "identity": [1, subject.RESULT, 0, 10], "outcome": "hold_result_target_missing", "evidence": {"token": "1", "text_sha256": subject.text_sha("one two"), "text_extraction_method": "fixture"}},
        {"source_index": 1, "identity": [1, subject.RESULT, 10, 20], "outcome": "hold_result_target_missing", "evidence": {"token": "2", "text_sha256": subject.text_sha("one two"), "text_extraction_method": "fixture"}},
        {"source_index": 2, "identity": [2, subject.RESULT, 0, 10], "outcome": "hold_result_target_missing", "evidence": {"token": "1", "text_sha256": subject.text_sha("repeat"), "text_extraction_method": "fixture"}},
        {"source_index": 3, "identity": [2, subject.RESULT, 10, 20], "outcome": "hold_result_target_missing", "evidence": {"token": "1", "text_sha256": subject.text_sha("repeat"), "text_extraction_method": "fixture"}},
        {"source_index": 4, "identity": [1, subject.EVENT, 2, 3], "outcome": "hold_no_item_evidence", "evidence": {}},
        {"source_index": 5, "identity": [2, subject.EVENT, 2, 3], "outcome": "hold_no_item_evidence", "evidence": {}},
        {"source_index": 6, "identity": [1, subject.EVENT, 30, 31], "outcome": "hold_no_item_evidence", "evidence": {}},
    ]
    body = {"kind": subject.LINKAGE_KIND, "mode": "dry-run", "applied": False, "write_path": "absent by design", "accounting": {"population": len(rows), "reconciles": True}, "dispositions": rows}
    body["digest"] = subject.sha({k: v for k, v in body.items() if k != "digest"})
    path = tmp_path / "linkage.json"; write_immutable(path, body); return path


def rows():
    return {"documents": [
        {"id": 1, "body": "x", "meeting_db_id": 10, "document_type": "Meeting Result", "text_content": "one two", "text_extraction_method": "fixture", "agenda_item_db_id": None},
        {"id": 2, "body": "x", "meeting_db_id": 11, "document_type": "Meeting Result", "text_content": "repeat", "text_extraction_method": "fixture", "agenda_item_db_id": None},
    ], "items": [], "events": [
        {"id": 40, "supporting_doc_id": 1, "agenda_item_id": None, "text_offset_start": 2, "text_offset_end": 3},
        {"id": 41, "supporting_doc_id": 2, "agenda_item_id": None, "text_offset_start": 2, "text_offset_end": 3},
        {"id": 42, "supporting_doc_id": 1, "agenda_item_id": None, "text_offset_start": 30, "text_offset_end": 31},
    ]}


def build(tmp_path):
    path = linkage(tmp_path)
    return subject.build_plan(linkage_path=path, rows=rows(), target={"tier": "test"}, created_at="now"), path


def test_unique_tokens_create_explicit_result_derived_containers_and_links(tmp_path):
    plan, path = build(tmp_path)
    assert plan["accounting"] == {"result_spans": 4, "result_linked": 2, "result_held": 2, "event_spans": 3, "event_linked": 1, "event_held": 2, "containers": 2, "reconciles": True, "data_operations_proposed": 0}
    assert all(x["source_kind"] == "result_derived" for x in plan["containers"])
    assert all(x["reservation_key"][0] == 10 for x in plan["containers"])
    assert [x["outcome"] for x in plan["event_span_dispositions"]] == ["would_link_event_span", "hold_duplicate_token_document", "hold_no_admitted_result_span"]
    assert subject.validate_plan(plan, linkage_path=path, rows=rows()) == []


def test_drift_existing_items_and_event_lineage_refuse_or_hold(tmp_path):
    path = linkage(tmp_path); changed = rows(); changed["documents"][0]["text_content"] = "changed"
    plan = subject.build_plan(linkage_path=path, rows=changed, target={"tier": "test"}, created_at="now")
    assert all(x["outcome"] == "hold_evidence_drift" for x in plan["result_span_dispositions"][:2])
    changed = rows(); changed["items"] = [{"id": 9, "meeting_db_id": 10, "agenda_item_number": "1", "agenda_item_id": "old"}]
    plan = subject.build_plan(linkage_path=path, rows=changed, target={"tier": "test"}, created_at="now")
    assert all(x["outcome"] == "hold_existing_or_invalid_container" for x in plan["result_span_dispositions"][:2])


def test_disabled_packet_is_exactly_bound_and_never_executes(tmp_path):
    plan, path = build(tmp_path); plan_path = tmp_path / "plan.json"; write_immutable(plan_path, plan)
    # The production packet intentionally admits only the reviewed live plan digest;
    # exercise its mechanics with a local copy after preserving the test plan content.
    original = packet.EXPECTED_PLAN_DIGEST; packet.EXPECTED_PLAN_DIGEST = plan["digest"]
    value = packet.build_packet(plan_path=plan_path, created_at="now")
    assert value["enabled"] is False
    assert packet.validate_packet(value, plan_path=plan_path) == []
    packet.EXPECTED_PLAN_DIGEST = original
    with pytest.raises(packet.WritePathNotImplemented): packet.execute_packet()
    assert len(packet.rollback_preflight(packet=value, receipt_verified=False, dependent_rows=1, source_drift=True)) == 3
    assert load_verified(plan_path)["digest"] == plan["digest"]


def test_offline_apply_gate_requires_post_receipts_backup_and_is_never_enabled(tmp_path):
    plan, path = build(tmp_path); plan_path = tmp_path / "plan.json"; write_immutable(plan_path, plan)
    original = packet.EXPECTED_PLAN_DIGEST; packet.EXPECTED_PLAN_DIGEST = plan["digest"]
    value = packet.build_packet(plan_path=plan_path, created_at="now")
    packet.EXPECTED_PLAN_DIGEST = original
    problems = apply.gate(packet=value, backup_binding=None, current_target=value["target"],
                          current_schema_sha256="a" * 64,
                          authorization_token=apply.AUTHORIZATION_TOKEN,
                          processing_receipts_complete=False)
    assert "processing-receipt mutation/replay is not complete" in problems
    assert "post-receipts restore-verified B3 backup is absent" in problems
    with pytest.raises(apply.ApplyRefused): apply.execute()
