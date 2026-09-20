from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from scripts.kg import stage3_b3_baseline as subject
from scripts.kg.stage2_artifacts import load_verified


def rows():
    long_text = "x" * 9000 + "\n2. Budget adopted\nbody"
    return {
        "documents": [
            {"id": 10, "meeting_db_id": 5, "document_type": "Agenda",
             "text_content": "0123456789", "text_extraction_method": "pymupdf",
             "agenda_item_db_id": 90},
            {"id": 11, "meeting_db_id": 5, "document_type": "Meeting Result",
             "text_content": long_text, "text_extraction_method": "pdftotext",
             "agenda_item_db_id": None},
        ],
        "events": [
            {"id": 7, "supporting_doc_id": 10, "text_offset_start": 2,
             "text_offset_end": 8},
            {"id": 8, "supporting_doc_id": 10, "text_offset_start": None,
             "text_offset_end": None},
        ],
        "extractions": [
            {"meeting_event_id": 7, "extractor": "pattern", "extractor_version": "1"},
            {"meeting_event_id": 8, "extractor": "pattern", "extractor_version": "1"},
        ],
        "items": [{"id": 91, "meeting_db_id": 5, "agenda_item_number": "2"}],
    }


def build():
    return subject.build_baseline(
        rows=rows(), created_at="2026-09-14T00:00:00Z",
        target={"tier": "test-isolated", "database": ":memory:"},
        schema={"sha256": "schema"}, hashes={"x.py": "abc"})


def test_exact_accounting_and_full_text_beyond_8000():
    baseline, plan = build()
    assert baseline["coverage"]["proposed"] == 3
    assert baseline["coverage"]["accepted"] == 2
    assert baseline["coverage"]["held"] == 1
    assert baseline["coverage"]["reconciles"] is True
    assert baseline["full_text_proof"]["query_uses_character_limit"] is False
    assert baseline["full_text_proof"]["configured_character_limit"] is None
    assert baseline["full_text_proof"]["maximum_document_characters"] > 8000
    assert baseline["full_text_proof"]["spans_ending_after_8000"] == 1
    assert plan["accounting"]["proposed"] == 3
    assert plan["accounting"]["would_insert"] == 2


def test_invalid_source_offsets_are_held_not_crashed():
    baseline, _ = build()
    assert baseline["holds"] == [{
        "index": 1, "disposition": "hold_invalid_offsets",
    }]


def test_input_change_changes_binding():
    baseline, _ = build()
    changed = rows()
    changed["documents"][0]["text_content"] = "changed"
    other, _ = subject.build_baseline(
        rows=changed, created_at="2026-09-14T00:00:00Z",
        target={"tier": "test-isolated"}, schema={"sha256": "schema"},
        hashes={"x.py": "abc"})
    assert (baseline["inputs"]["document_projection_sha256"] !=
            other["inputs"]["document_projection_sha256"])


def test_sql_has_no_limit_or_substring():
    normalized = " ".join(subject.DOCUMENTS_SQL.lower().split())
    assert " limit " not in normalized
    assert "left(" not in normalized
    assert "substring" not in normalized
    assert "8000" not in normalized


def sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE supporting_documents (
            id INTEGER PRIMARY KEY, meeting_db_id INTEGER, document_type TEXT,
            text_content TEXT, text_extraction_method TEXT, agenda_item_db_id INTEGER)"""))
        connection.execute(text("""CREATE TABLE meeting_events (
            id INTEGER PRIMARY KEY, supporting_doc_id INTEGER,
            text_offset_start INTEGER, text_offset_end INTEGER)"""))
        connection.execute(text("""CREATE TABLE meeting_event_extractions (
            id INTEGER PRIMARY KEY, meeting_event_id INTEGER, extractor TEXT,
            extractor_version TEXT)"""))
        connection.execute(text("""CREATE TABLE agenda_items (
            id INTEGER PRIMARY KEY, meeting_db_id INTEGER, agenda_item_number TEXT)"""))
        connection.execute(text("""INSERT INTO supporting_documents VALUES
            (10,5,'Agenda','0123456789','pymupdf',90),
            (11,5,'Meeting Result','\n2. Budget adopted\nbody','pdftotext',NULL)"""))
        connection.execute(text("INSERT INTO meeting_events VALUES (7,10,2,8)"))
        connection.execute(text(
            "INSERT INTO meeting_event_extractions VALUES (1,7,'pattern','1')"))
        connection.execute(text("INSERT INTO agenda_items VALUES (91,5,'2')"))
    return engine


def test_capture_and_run_are_read_only_and_immutable(tmp_path: Path):
    engine = sqlite_engine()
    result = subject.run(engine, out_dir=tmp_path, created_at="20260914T000000Z")
    assert result["status"] == "success"
    assert result["read_only_audit"]["select_only"] is True
    baseline = load_verified(result["baseline"])
    plan = load_verified(result["plan"])
    assert baseline["coverage"]["proposed"] == 2
    assert plan["baseline_artifact"]["digest"] == baseline["digest"]
    assert (Path(result["baseline"]).stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        Path(result["baseline"]).open("x")


def test_schema_and_code_are_bound(tmp_path: Path):
    engine = sqlite_engine()
    result = subject.run(engine, out_dir=tmp_path, created_at="20260914T000001Z")
    artifact = json.loads(Path(result["baseline"]).read_text())
    assert set(artifact["schema"]["tables"]) == {
        "supporting_documents", "meeting_events",
        "meeting_event_extractions", "agenda_items"}
    assert set(artifact["code_hashes"]) == set(subject.CODE_MODULES)


def test_current_validator_refuses_drift_and_cross_pair(tmp_path: Path):
    engine = sqlite_engine()
    first = subject.run(engine, out_dir=tmp_path, created_at="20260914T000002Z")
    second = subject.run(engine, out_dir=tmp_path, created_at="20260914T000003Z")
    with engine.connect() as connection:
        current = subject.capture_rows(connection)
    schema = subject.schema_binding(engine)
    hashes = subject.code_hashes()
    assert subject.validate_current(
        baseline_path=Path(first["baseline"]), plan_path=Path(first["plan"]),
        rows=current, target=load_verified(first["baseline"])["target"],
        schema=schema, hashes=hashes) == []
    problems = subject.validate_current(
        baseline_path=Path(first["baseline"]), plan_path=Path(second["plan"]),
        rows=current, target=load_verified(first["baseline"])["target"],
        schema=schema, hashes=hashes)
    assert any("exact baseline" in problem for problem in problems)
    current["documents"][0]["text_content"] = "drift"
    problems = subject.validate_current(
        baseline_path=Path(first["baseline"]), plan_path=Path(first["plan"]),
        rows=current, target=load_verified(first["baseline"])["target"],
        schema=schema, hashes=hashes)
    assert any("authoritative current rebuild" in problem for problem in problems)
