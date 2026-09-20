"""Tests for the evidence-bound, read-only quality candidate cohort builder."""

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from scripts.kg import stage3_quality_cohort as C
from scripts.kg import stage3_quality_candidate_source as S
from scripts.kg import stage3_processing_plan_inputs as P
from scripts.kg.stage2_artifacts import load_verified, write_immutable


TARGET = {"tier": "development", "database": "poliscopic_dev", "dialect": "postgresql"}


def _case(case_id, *, body="phoenix_cc", predicate="PARTICIPATED_IN"):
    text = "Mayor Alice approved item 3."
    return {
        "case_id": case_id,
        "source": {"platform_or_source": "legistar", "extraction_method": "pymupdf",
                   "document_type": "Meeting Result", "body": body},
        "output": {"predicate": predicate, "output_type": "mention",
                   "assistance_mode": "deterministic", "outcome": "held",
                   "promotion_applicability": "applicable", "promoted": False,
                   "promotion_state": "unpromoted", "agenda_item_db_id": None},
        "document": {"source_kind": "supporting_document", "source_id": 17,
                     "content_sha256": hashlib.sha256(text.encode()).hexdigest()},
        "retained_text": text,
        "evidence": {"coordinate_system": "unicode_codepoint_half_open", "start": 6,
                     "end": 11, "span_sha256": hashlib.sha256(b"Alice").hexdigest()},
    }


def _source(tmp_path, cases=None):
    cases = cases or [_case("a"), _case("b", body="mesa_cc", predicate="ABOUT")]
    text = cases[0]["retained_text"]
    entry = {"source_id": 17, "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "text_present": True, "extraction_method": "pymupdf",
             "text_extracted_at": "2026-09-14T00:00:00Z", "scraped_at": None,
             "has_document_url": True, "legacy_swept_at_present": True}
    selection = tmp_path / "selection.json"
    write_immutable(selection, P.build_selection_snapshot(
        created_at="now", target=TARGET, bound={"selected": 1, "population": 1},
        entries=[entry], evidence={}, code_hashes={}))
    rows = []
    for extraction_id, case in enumerate(cases, 1):
        output, source, evidence = case["output"], case["source"], case["evidence"]
        rows.append({"extraction_id": extraction_id, "source_id": 17,
                     "text_content": case["retained_text"],
                     "content_sha256": case["document"]["content_sha256"], **source,
                     "predicate": output["predicate"], "output_type": output["output_type"],
                     "assistance_mode": output["assistance_mode"], "outcome": output["outcome"],
                     "promotion_applicability": output["promotion_applicability"],
                     "promoted": output["promoted"], "promotion_state": output["promotion_state"],
                     "materialization_state": "candidate_only", "link_state": "unlinked",
                     "agenda_item_db_id": None,
                     "meeting_db_id": None, "start": evidence["start"], "end": evidence["end"],
                     "span_sha256": evidence["span_sha256"]})
    path = tmp_path / "candidate-source.json"
    source = S.build_source(rows=rows, selection_path=selection, created_at="now")
    write_immutable(path, source)
    return path, source[C.SOURCE_CASES]


def test_cohort_reconstructs_exact_observed_case_projections(tmp_path):
    source, cases = _source(tmp_path)
    cohort = C.build_cohort(source_path=source, created_at="2026-09-15T03:00:00Z")
    assert cohort["target"] == TARGET
    assert cohort["population"] == {"count": 2, "sha256": C.benchmark.population_digest(cases)}
    assert [row["case_id"] for row in cohort["case_projections"]] == [
        "meeting_event_extraction:1", "meeting_event_extraction:2"]
    assert cohort["case_projections"][0]["document"]["source_kind"] == "supporting_document"
    assert cohort["case_projections"][0]["document"]["retained_text_sha256"]
    assert cohort["case_projections"][0]["output_digest"]
    assert cohort["case_projections"][0]["evidence_digest"]
    assert len(cohort["observed_strata"]) == 2
    assert C.validate_cohort(cohort) == []


def test_source_and_resigned_cohort_drift_fail_closed(tmp_path):
    source, _ = _source(tmp_path)
    cohort = C.build_cohort(source_path=source, created_at="now")
    forged = deepcopy(cohort)
    forged["case_projections"][0]["promoted"] = True
    forged["digest"] = C.canonical_sha256({k: v for k, v in forged.items() if k != "digest"})
    assert C.validate_cohort(forged) == ["cohort differs from exact source reconstruction"]

    bad = load_verified(source)
    bad[C.SOURCE_CASES][0]["output"]["outcome"] = "invented"
    bad["digest"] = C.canonical_sha256({k: v for k, v in bad.items() if k != "digest"})
    bad_path = tmp_path / "bad-source.json"
    write_immutable(bad_path, bad)
    with pytest.raises(C.CohortRefused, match="candidate case digest differs"):
        C.build_cohort(source_path=bad_path, created_at="now")


def test_current_stage3_artifacts_discover_valid_source_and_exact_cohort_binding():
    paths = sorted((C.REPO / "data/kg-plans").glob("kg-stage3-*.json"))
    report = C.blocker_report(paths)
    current_source = C.REPO / "data/kg-plans/kg-stage3-quality-candidate-source-20260916T002029Z.json"
    current_cohort = C.REPO / "data/kg-plans/kg-stage3-quality-cohort-20260916T002029Z.json"
    source = load_verified(current_source)
    cohort = load_verified(current_cohort)
    assert C.candidate_source.validate_source(source) == []
    assert C.validate_cohort(cohort) == []
    assert cohort["source_binding"]["path"] == str(current_source.relative_to(C.REPO))
    assert cohort["source_binding"]["digest"] == source["digest"]
    assert {item["path"] for item in report["candidate_sources_found"]} >= {str(current_source)}


def test_missing_candidate_source_blocker_remains_explicit_for_empty_inputs():
    report = C.blocker_report([])
    assert report["status"] == "blocked"
    assert report["code"] == "missing_authoritative_quality_candidate_source"
    assert report["candidate_sources_found"] == []
    assert report["required"]["candidate_cases"] == "nonempty exact observed cases"


def test_no_database_or_model_surface_is_present():
    source = Path(C.__file__).read_text(encoding="utf-8")
    assert not hasattr(C, "apply")
    assert "sqlalchemy" not in source
    assert "get_engine" not in source
