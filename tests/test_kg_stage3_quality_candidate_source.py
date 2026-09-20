"""Adversarial tests for the disabled quality candidate-source producer."""

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from scripts.kg import stage3_quality_candidate_source as S
from scripts.kg import stage3_processing_plan_inputs as P
from scripts.kg.stage2_artifacts import write_immutable


TARGET = {"tier": "development", "database": "poliscopic_dev", "dialect": "postgresql"}


def _selection(tmp_path):
    path = tmp_path / "selection.json"
    entry = {"source_id": 17, "content_sha256": _row()["content_sha256"],
             "text_present": True, "extraction_method": "pymupdf",
             "text_extracted_at": "2026-09-14T00:00:00Z",
             "scraped_at": "2026-09-13T00:00:00Z", "has_document_url": True,
             "legacy_swept_at_present": True}
    value = P.build_selection_snapshot(
        created_at="now", target=TARGET, bound={"selected": 1, "population": 1},
        entries=[entry], evidence={}, code_hashes={})
    write_immutable(path, value)
    return path


def _row(**overrides):
    text = "Mayor Alice approved item 3."
    row = {
        "extraction_id": 8, "source_id": 17, "text_content": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "platform_or_source": "https://example.test/meetings", "extraction_method": "pymupdf",
        "document_type": "Meeting Result", "body": "phoenix_cc", "predicate": "Approved",
        "output_type": "meeting_event_extraction", "assistance_mode": "deterministic",
        "outcome": "held", "promotion_applicability": "not_represented",
        "promoted": None, "promotion_state": "not_represented",
        "materialization_state": "candidate_only", "link_state": "unlinked",
        "agenda_item_db_id": None, "meeting_db_id": None,
        "start": 6, "end": 11, "span_sha256": hashlib.sha256(b"Alice").hexdigest(),
    }
    row.update(overrides)
    return row


def test_source_binds_exact_selection_and_complete_observed_case(tmp_path):
    source = S.build_source(rows=[_row()], selection_path=_selection(tmp_path), created_at="now")
    assert source["target"] == TARGET
    assert source["population"]["candidate_cases"] == 1
    case = source["candidate_cases"][0]
    assert case["document"] == {"source_kind": "supporting_document", "source_id": 17,
                                 "content_sha256": _row()["content_sha256"]}
    assert case["output"]["promoted"] is None
    assert case["output"]["promotion_applicability"] == "not_represented"
    assert case["output"]["link_state"] == "unlinked"
    assert source["source_accounting"]["selected_sources"] == 1
    assert source["source_accounting"]["zero_candidate_sources"] == 0
    assert S.validate_source(source) == []


def test_source_accounting_includes_selected_documents_with_zero_candidates(tmp_path):
    selection = _selection(tmp_path)
    value = S.load_verified(selection)
    second = {**value["entries"][0], "source_id": 18}
    selection2 = tmp_path / "selection-two.json"
    write_immutable(selection2, P.build_selection_snapshot(
        created_at="now", target=TARGET, bound={"selected": 2, "population": 2},
        entries=[value["entries"][0], second], evidence={}, code_hashes={}))
    source = S.build_source(rows=[_row()], selection_path=selection2, created_at="now")
    accounting = source["source_accounting"]
    assert accounting["selected_sources"] == 2
    assert accounting["sources_with_candidates"] == 1
    assert accounting["zero_candidate_sources"] == 1
    assert accounting["entries"] == [
        {"source_id": 17, "candidate_count": 1, "extraction_ids": [8]},
        {"source_id": 18, "candidate_count": 0, "extraction_ids": []},
    ]
    assert S.validate_source(source) == []


def test_duplicate_extraction_and_resigned_accounting_omission_fail_closed(tmp_path):
    selection = _selection(tmp_path)
    with pytest.raises(S.CandidateSourceRefused, match="duplicate extraction IDs"):
        S.build_source(rows=[_row(), _row()], selection_path=selection, created_at="now")
    source = S.build_source(rows=[_row()], selection_path=selection, created_at="now")
    source["source_accounting"]["entries"] = []
    source["source_accounting"]["entries_sha256"] = S.canonical_sha256([])
    source["digest"] = S.canonical_sha256({k: v for k, v in source.items() if k != "digest"})
    assert any("exact row reconstruction" in problem for problem in S.validate_source(source))


def test_promotion_link_and_evidence_inference_fail_closed(tmp_path):
    selection = _selection(tmp_path)
    with pytest.raises(S.CandidateSourceRefused, match="promotion"):
        S.build_source(rows=[_row(promotion_state="")], selection_path=selection, created_at="now")
    with pytest.raises(S.CandidateSourceRefused, match="link_state"):
        S.build_source(rows=[_row(link_state="meeting")], selection_path=selection, created_at="now")
    with pytest.raises(S.CandidateSourceRefused, match="span_sha256"):
        S.build_source(rows=[_row(span_sha256="0" * 64)], selection_path=selection, created_at="now")
    with pytest.raises(S.CandidateSourceRefused, match="unexpected source IDs"):
        S.build_source(rows=[_row(source_id=99)], selection_path=selection, created_at="now")


def test_resigned_candidate_or_selection_binding_tamper_is_refused(tmp_path):
    source = S.build_source(rows=[_row()], selection_path=_selection(tmp_path), created_at="now")
    forged = deepcopy(source)
    forged["candidate_cases"][0]["output"]["predicate"] = "Invented"
    forged["candidate_cases_sha256"] = S.canonical_sha256(forged["candidate_cases"])
    forged["population"]["case_sha256"] = S.benchmark.population_digest(forged["candidate_cases"])
    forged["digest"] = S.canonical_sha256({k: v for k, v in forged.items() if k != "digest"})
    assert any("exact row reconstruction" in problem for problem in S.validate_source(forged))
    forged["selection_binding"]["digest"] = "0" * 64
    forged["digest"] = S.canonical_sha256({k: v for k, v in forged.items() if k != "digest"})
    assert any("selection binding differs" in problem for problem in S.validate_source(forged))


def test_current_execution_is_precisely_blocked_without_a_guarded_reader():
    report = S.execution_blocker(S.REPO / S.DEFAULT_SELECTION)
    assert report["status"] == "blocked"
    assert report["code"] == "missing_explicit_extraction_promotion_authority"
    assert report["target"]["tier"] == "development"
    assert "promoted" in report["required_projection_fields"]
    assert len(report["promotion_authority_problems"]) == 2


def test_projection_records_materialization_but_never_infers_promotion():
    projected = S._project_row({
        "extraction_id": 8, "source_id": 17,
        "text_content": "Mayor Alice approved item 3.",
        "extraction_method": "pymupdf", "document_type": "Meeting Result",
        "body": "phoenix_cc", "platform_or_source": "legistar", "extractor": "pattern",
        "predicate": "Approved", "start": 6, "end": 11, "event_id": 40,
        "event_source_id": 17, "agenda_item_db_id": 50,
        "agenda_item_meeting_db_id": 60, "document_meeting_db_id": 60,
    })
    assert projected["materialization_state"] == "materialized"
    assert projected["link_state"] == "agenda_item"
    assert projected["promotion_applicability"] == "not_represented"
    assert projected["promoted"] is None


@pytest.mark.parametrize("dialect,database", [
    ("postgresql", "poliscopic"),
    ("postgresql", "production"),
    ("sqlite", "poliscopic_dev"),
])
def test_capture_refuses_wrong_target_before_connection(tmp_path, dialect, database):
    engine = type("NeverConnect", (), {
        "url": type("URL", (), {"host": None, "port": None, "database": database})(),
        "dialect": type("Dialect", (), {"name": dialect})(),
        "connect": lambda self: (_ for _ in ()).throw(AssertionError("target guard must not connect")),
    })()

    with pytest.raises(S.CandidateSourceRefused, match="not the exact development target"):
        S.capture_rows(engine, selection_path=_selection(tmp_path))


def test_selection_label_cannot_disguise_production_target(tmp_path):
    path = tmp_path / "selection.json"
    entry = {"source_id": 17, "content_sha256": _row()["content_sha256"],
             "text_present": True, "extraction_method": "pymupdf",
             "text_extracted_at": None, "scraped_at": None, "has_document_url": True,
             "legacy_swept_at_present": True}
    value = P.build_selection_snapshot(
        created_at="now", target={**TARGET, "database": "poliscopic"},
        bound={"selected": 1, "population": 1}, entries=[entry], evidence={}, code_hashes={})
    write_immutable(path, value)
    with pytest.raises(S.CandidateSourceRefused, match="not the exact development target"):
        S.execution_blocker(path)


def _database_projection(**overrides):
    text = _row()["text_content"]
    value = {
        "extraction_id": 8, "source_id": 17, "text_content": text,
        "extraction_method": "pymupdf", "document_type": "Meeting Result",
        "body": "phoenix_cc", "platform_or_source": "legistar", "extractor": "pattern",
        "predicate": "approved", "start": 6, "end": 11, "event_id": 20,
        "event_source_id": 17, "agenda_item_db_id": 30,
        "agenda_item_meeting_db_id": 40, "document_meeting_db_id": 40,
    }
    value.update(overrides)
    return value


def test_projection_refuses_cross_document_materialization():
    with pytest.raises(S.CandidateSourceRefused, match="event from another source"):
        S._project_row(_database_projection(event_source_id=99))


def test_projection_refuses_cross_meeting_agenda_item_link():
    with pytest.raises(S.CandidateSourceRefused, match="agenda item from another meeting"):
        S._project_row(_database_projection(agenda_item_meeting_db_id=99))


def test_module_has_no_database_model_or_write_execution_surface():
    source = Path(S.__file__).read_text(encoding="utf-8")
    assert not hasattr(S, "run")
    assert all(word not in S.PROJECTION_SQL.upper() for word in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))
