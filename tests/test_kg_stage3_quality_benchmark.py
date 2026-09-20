"""Adversarial tests for the offline Stage 3 source-balanced quality benchmark."""

import copy
import hashlib

import pytest

from scripts.kg import stage3_quality_benchmark as Q
from scripts.kg import stage3_quality_candidate_source as S
from scripts.kg import stage3_quality_cohort as C
from scripts.kg import stage3_processing_plan_inputs as P
from scripts.kg.stage2_artifacts import write_immutable


TARGET = {"tier": "development", "dialect": "postgresql", "host": "dev",
          "port": 5432, "database": "poliscopic_dev"}
HASHES = Q.producer_code_hashes()


def case(case_id, *, platform="legistar", method="pymupdf", doc_type="Meeting Result",
         body="phoenix_cc", predicate="PARTICIPATED_IN", output_type="mention",
         assistance="deterministic", outcome="success", promoted=False, agenda_item_db_id=None):
    text = "Mayor Alice approved item 3."
    numeric_id = int(hashlib.sha256(str(case_id).encode()).hexdigest()[:12], 16) + 1
    return {
        "case_id": f"meeting_event_extraction:{numeric_id}",
        "source": {"platform_or_source": platform, "extraction_method": method,
                    "document_type": doc_type, "body": body},
        "output": {"predicate": predicate, "output_type": output_type,
                    "assistance_mode": assistance, "outcome": outcome,
                    "promotion_applicability": "applicable", "promoted": promoted,
                    "promotion_state": "promoted" if promoted else "unpromoted", "subject": "Alice",
                    "materialization_state": "materialized" if agenda_item_db_id else "candidate_only",
                    "link_state": "agenda_item" if agenda_item_db_id else "unlinked",
                    "agenda_item_db_id": agenda_item_db_id},
        "document": {"source_kind": "supporting_document", "source_id": 10,
                      "content_sha256": hashlib.sha256(text.encode()).hexdigest()},
        "retained_text": text,
        "evidence": {"coordinate_system": Q.COORDINATE_SYSTEM, "start": 6, "end": 11,
                     "span_sha256": hashlib.sha256(b"Alice").hexdigest()},
    }


def artifact(tmp_path, cases):
    tmp_path.mkdir(parents=True, exist_ok=True)
    text = cases[0]["retained_text"]
    entry = {"source_id": 10, "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "text_present": True, "extraction_method": "pymupdf",
             "text_extracted_at": "2026-09-14T00:00:00Z", "scraped_at": None,
             "has_document_url": True, "legacy_swept_at_present": True}
    selection_path = tmp_path / "selection.json"
    write_immutable(selection_path, P.build_selection_snapshot(
        created_at="now", target=TARGET, bound={"selected": 1, "population": 1},
        entries=[entry], evidence={}, code_hashes={}))
    rows = []
    for case_value in cases:
        output, source, evidence = (case_value["output"], case_value["source"],
                                    case_value["evidence"])
        extraction_id = int(case_value["case_id"].rsplit(":", 1)[1])
        item_id = output.get("agenda_item_db_id")
        rows.append({"extraction_id": extraction_id, "source_id": 10,
                     "text_content": case_value["retained_text"],
                     "content_sha256": case_value["document"]["content_sha256"], **source,
                     "predicate": output["predicate"], "output_type": output["output_type"],
                     "assistance_mode": output["assistance_mode"], "outcome": output["outcome"],
                     "promotion_applicability": output["promotion_applicability"],
                     "promoted": output["promoted"], "promotion_state": output["promotion_state"],
                     "materialization_state": "materialized" if item_id else "candidate_only",
                     "link_state": "agenda_item" if item_id else "unlinked",
                     "agenda_item_db_id": item_id, "meeting_db_id": None,
                     "start": evidence["start"], "end": evidence["end"],
                     "span_sha256": evidence["span_sha256"]})
    source_path = tmp_path / "candidate-source.json"
    source_artifact = S.build_source(rows=rows, selection_path=selection_path, created_at="now")
    write_immutable(source_path, source_artifact)
    cases[:] = copy.deepcopy(source_artifact[C.SOURCE_CASES])
    cohort = C.build_cohort(source_path=source_path, created_at="now")
    path = tmp_path / "candidate-cohort.json"
    digest = write_immutable(path, cohort)
    return Q.artifact_binding(path, kind=Q.COHORT_KIND), digest


def packet(tmp_path, cases=None):
    cases = cases or [case("a"), case("b", platform="onbase", outcome="held")]
    binding, _ = artifact(tmp_path, cases)
    return Q.build_review_packet(cases,
                                 artifact_bindings=[binding], target=TARGET, code_hashes=HASHES,
                                 created_at="2026-09-15T01:00:00Z", seed="seed", per_stratum=1)


def test_sampling_is_deterministic_and_represents_every_full_stratum(tmp_path):
    cases = [case("a"), case("b"), case("c", platform="onbase"),
             case("d", platform="onbase", assistance="ai_assisted", predicate="ABOUT")]
    binding, _ = artifact(tmp_path, cases)
    first = Q.build_review_packet(cases, artifact_bindings=[binding], target=TARGET,
                                  code_hashes=HASHES, created_at="now", seed="s", per_stratum=1)
    second = Q.build_review_packet(list(reversed(cases)), artifact_bindings=[binding],
                                   target=TARGET, code_hashes=HASHES, created_at="now", seed="s",
                                   per_stratum=1)
    assert first["digest"] == second["digest"]
    assert len(first["items"]) == len(first["sample_strata"]) == 3
    assert all(item["evidence"]["retained_text_chars"] == len("Mayor Alice approved item 3.")
               and item["evidence"]["snippet"] for item in first["items"])
    assert Q.validate_packet(first) == []


def test_population_rejects_duplicate_or_incomplete_strata(tmp_path):
    with pytest.raises((Q.BenchmarkRefused, C.CohortRefused, S.CandidateSourceRefused),
                       match="unique nonempty|duplicate"):
        packet(tmp_path / "duplicate", [case("same"), case("same")])
    broken = case("broken")
    del broken["source"]["body"]
    with pytest.raises((Q.BenchmarkRefused, C.CohortRefused, S.CandidateSourceRefused),
                       match="body|key set is not canonical"):
        packet(tmp_path / "broken", [broken])
    bad = case("bad", outcome="not-a-state")
    with pytest.raises((Q.BenchmarkRefused, C.CohortRefused, S.CandidateSourceRefused),
                       match="outcome"):
        packet(tmp_path / "bad", [bad])


def test_cohort_and_code_bindings_refuse_candidate_or_producer_drift(tmp_path):
    cases = [case("bound")]
    binding, _ = artifact(tmp_path, cases)
    cases[0]["output"]["subject"] = "Changed"
    with pytest.raises(Q.BenchmarkRefused, match="cohort population"):
        Q.build_review_packet(cases, artifact_bindings=[binding], target=TARGET,
                              code_hashes=HASHES, created_at="now", seed="s")
    with pytest.raises(Q.BenchmarkRefused, match="producer code hash"):
        Q.build_review_packet([case("bound")], artifact_bindings=[binding], target=TARGET,
                              code_hashes={"scripts/kg/stage3_quality_benchmark.py": "0" * 64},
                              created_at="now", seed="s")


def test_link_labels_are_only_allowed_for_preexisting_canonical_links(tmp_path):
    value = packet(tmp_path, [case("unlinked")])
    labels = {case("unlinked")["case_id"]: {"evidence_coordinate": "valid", "support": "supported",
                            "container_link": "correct", "extraction": "tp"}}
    with pytest.raises(Q.BenchmarkRefused, match="container_link label is incompatible"):
        Q.evaluate_labels(value, labels)


def test_packet_tampering_of_bindings_or_review_state_is_refused(tmp_path):
    value = packet(tmp_path)
    broken = copy.deepcopy(value)
    broken["target"]["tier"] = "production"
    broken["digest"] = Q.canonical_sha256({k: v for k, v in broken.items() if k != "digest"})
    assert any("development target" in problem for problem in Q.validate_packet(broken))
    broken = copy.deepcopy(value)
    broken["items"][0]["review"]["decision"] = "accept"
    broken["digest"] = Q.canonical_sha256({k: v for k, v in broken.items() if k != "digest"})
    assert any("undecided" in problem for problem in Q.validate_packet(broken))
    broken = copy.deepcopy(value)
    broken["metrics"]["evidence_coordinate_validity"]["threshold"] = 0.99
    broken["digest"] = Q.canonical_sha256({k: v for k, v in broken.items() if k != "digest"})
    assert any("metric definitions" in problem for problem in Q.validate_packet(broken))
    broken = copy.deepcopy(value)
    broken["population_strata"][0]["count"] += 1
    broken["digest"] = Q.canonical_sha256({k: v for k, v in broken.items() if k != "digest"})
    assert any("population strata" in problem for problem in Q.validate_packet(broken))
    broken = copy.deepcopy(value)
    broken["extra"] = True
    broken["digest"] = Q.canonical_sha256({k: v for k, v in broken.items() if k != "digest"})
    assert any("key set" in problem for problem in Q.validate_packet(broken))


@pytest.mark.parametrize("mutation,needle", [
    (lambda value: value["items"][0]["candidate"].__setitem__("predicate", "FORGED"),
     "candidate/evidence/content/stratum drift"),
    (lambda value: value["items"][0]["evidence"].__setitem__("start", 999),
     "evidence coordinate/hash drift"),
    (lambda value: value["items"][0]["document"].__setitem__("content_sha256", "0" * 64),
     "candidate/evidence/content/stratum drift"),
    (lambda value: value["items"][0]["candidate"].__setitem__("promoted", "yes"),
     "candidate/evidence/content/stratum drift"),
])
def test_sampled_candidate_evidence_content_and_promotion_drift_is_refused(tmp_path, mutation, needle):
    value = packet(tmp_path, [case("a"), case("b", platform="onbase")])
    mutation(value)
    value["digest"] = Q.canonical_sha256({k: v for k, v in value.items() if k != "digest"})
    assert any(needle in problem for problem in Q.validate_packet(value))


@pytest.mark.parametrize("field,replacement", [
    ("snippet", "FORGED REVIEW CONTEXT"),
    ("retained_text_chars", 999),
])
def test_resigned_displayed_reviewer_evidence_drift_is_refused(tmp_path, field, replacement):
    value = packet(tmp_path, [case("a"), case("b", platform="onbase")])
    value["items"][0]["evidence"][field] = replacement
    value["digest"] = Q.canonical_sha256({k: v for k, v in value.items() if k != "digest"})
    assert any("displayed reviewer evidence differs" in problem
               for problem in Q.validate_packet(value))


def test_unrepresented_promotion_is_preserved_as_null_not_false():
    value = case("unrepresented")
    value["output"].update({"promotion_applicability": "not_represented",
                            "promoted": None, "promotion_state": "not_represented"})
    projection = Q._case_projection(value)
    assert projection["promotion_applicability"] == "not_represented"
    assert projection["promoted"] is None
    assert projection["promotion_state"] == "not_represented"


@pytest.mark.parametrize("patch", [
    {"promotion_applicability": "not_represented", "promoted": False,
     "promotion_state": "not_represented"},
    {"promotion_applicability": "applicable", "promoted": False,
     "promotion_state": "promoted"},
])
def test_promotion_applicability_state_disagreement_is_refused(patch):
    value = case("bad-promotion")
    value["output"].update(patch)
    assert any("promotion" in problem for problem in Q.validate_case(value))


def test_sample_stratum_swap_is_refused_against_cohort_projections(tmp_path):
    value = packet(tmp_path, [case("a"), case("b", platform="onbase")])
    value["items"][0]["stratum"], value["items"][1]["stratum"] = (
        value["items"][1]["stratum"], value["items"][0]["stratum"])
    value["digest"] = Q.canonical_sha256({k: v for k, v in value.items() if k != "digest"})
    assert any("projection differs" in problem or "candidate/evidence/content/stratum" in problem
               for problem in Q.validate_packet(value))


def test_duplicate_ids_and_population_accounting_drift_are_refused(tmp_path):
    value = packet(tmp_path, [case("a"), case("b", platform="onbase")])
    value["items"][1]["case_id"] = value["items"][0]["case_id"]
    value["digest"] = Q.canonical_sha256({k: v for k, v in value.items() if k != "digest"})
    problems = Q.validate_packet(value)
    assert any("duplicate case IDs" in problem for problem in problems)

    value = packet(tmp_path / "accounting", [case("a"), case("b", platform="onbase")])
    value["accounting"]["population"] += 1
    value["digest"] = Q.canonical_sha256({k: v for k, v in value.items() if k != "digest"})
    assert any("population accounting" in problem for problem in Q.validate_packet(value))


def test_evaluation_is_threshold_free_and_groups_precision_recall(tmp_path):
    cases = [case("tp", promoted=True, agenda_item_db_id=40),
             case("fp", promoted=True, agenda_item_db_id=41),
             case("fn", predicate="ABOUT", output_type="candidate", outcome="held")]
    value = Q.build_review_packet(cases, artifact_bindings=[artifact(tmp_path, cases)[0]], target=TARGET,
                                  code_hashes=HASHES, created_at="now", seed="s", per_stratum=2)
    labels = {
        case("tp")["case_id"]: {"evidence_coordinate": "valid", "support": "supported",
               "container_link": "correct", "extraction": "tp"},
        case("fp")["case_id"]: {"evidence_coordinate": "invalid", "support": "unsupported",
               "container_link": "incorrect", "extraction": "fp"},
        case("fn")["case_id"]: {"evidence_coordinate": "not_applicable", "support": "not_applicable",
               "container_link": "not_applicable", "extraction": "fn"},
    }
    result = Q.evaluate_labels(value, labels)
    assert result["pending"] == 0
    assert result["metrics"]["unsupported_promoted_assertions"]["value"] == 0.5
    assert result["metrics"]["container_link_precision"]["value"] == 0.5
    assert result["metrics"]["extraction_by_source_predicate"]["legistar|PARTICIPATED_IN"]["precision"]["value"] == 0.5
    assert result["metrics"]["extraction_by_source_predicate"]["legistar|ABOUT"]["recall"]["value"] == 0.0
    assert all(spec["threshold"] is None for spec in Q.METRIC_CONTRACT.values())


def test_labels_refuse_unknown_cases_and_invalid_values(tmp_path):
    value = packet(tmp_path)
    with pytest.raises(Q.BenchmarkRefused, match="unknown case"):
        Q.evaluate_labels(value, {"missing": {}})
    with pytest.raises(Q.BenchmarkRefused, match="invalid evidence_coordinate"):
        Q.evaluate_labels(value, {case("a")["case_id"]: {"evidence_coordinate": "maybe", "support": "supported",
                                         "container_link": "correct", "extraction": "tp"}})


def test_zero_denominators_are_explicitly_undefined(tmp_path):
    value = packet(tmp_path, [case("held", outcome="held")])
    result = Q.evaluate_labels(value, {case("held")["case_id"]: {"evidence_coordinate": "not_applicable",
                                                  "support": "not_applicable",
                                                  "container_link": "not_applicable",
                                                  "extraction": "not_applicable"}})
    for key in ("evidence_coordinate_validity", "unsupported_promoted_assertions",
                "container_link_precision"):
        assert result["metrics"][key]["defined"] is False


def test_writer_emits_only_a_valid_immutable_review_packet(tmp_path):
    value = packet(tmp_path)
    path = tmp_path / "review-packet.json"
    assert Q.write_review_packet(path, value) == value["digest"]
    assert Q.validate_packet(Q.load_verified(path)) == []


def test_bounded_hierarchical_sample_replays_coverage_and_probabilities(tmp_path):
    cases = [case("a"), case("b"), case("c", predicate=" Approved  "),
             case("d", predicate="approved"), case("e", body="mesa_cc")]
    binding, _ = artifact(tmp_path, cases)
    value = Q.build_review_packet(
        cases, artifact_bindings=[binding], target=TARGET, code_hashes=HASHES,
        created_at="now", seed="governed-seed", max_reviews=3)
    assert len(value["items"]) == 3
    assert value["sampling"]["algorithm"] == Q.BOUNDED_ALGORITHM
    assert value["coverage_ledger"]["coverage_fields"] == list(Q.HIERARCHICAL_COVERAGE_FIELDS)
    assert sum(cell["population"] for cell in value["coverage_ledger"]["cells"]) == 5
    assert sum(cell["selected"] for cell in value["coverage_ledger"]["cells"]) == 3
    assert all(cell["inclusion_probability"] == cell["selected"] / cell["population"]
               and cell["analysis_weight"] == cell["population"] / cell["selected"]
               for cell in value["coverage_ledger"]["cells"])
    source_family = next(item for item in value["coverage_ledger"]["marginals"]
                         if item["field"] == "source_family")
    assert source_family["values"][0]["value"] == "legistar"
    assert Q.validate_packet(value) == []


@pytest.mark.parametrize("mutation", [
    lambda value: value["coverage_ledger"]["cells"][0].__setitem__("selected", 99),
    lambda value: value["coverage_ledger"]["cells"][0].__setitem__("inclusion_probability", 0.5),
    lambda value: value["coverage_ledger"]["cells"][0].__setitem__("analysis_weight", 999),
    lambda value: value["coverage_ledger"]["marginals"][0]["values"][0].__setitem__("unsampled", 999),
])
def test_resigned_bounded_coverage_ledger_tampering_is_refused(tmp_path, mutation):
    cases = [case("a"), case("b"), case("c", predicate="ABOUT")]
    binding, _ = artifact(tmp_path, cases)
    value = Q.build_review_packet(
        cases, artifact_bindings=[binding], target=TARGET, code_hashes=HASHES,
        created_at="now", seed="s", max_reviews=2)
    mutation(value)
    value["digest"] = Q.canonical_sha256({k: v for k, v in value.items() if k != "digest"})
    assert "coverage ledger differs from exact bounded replay" in Q.validate_packet(value)


def test_resigned_bounded_reviewer_evidence_tampering_is_refused(tmp_path):
    cases = [case("a"), case("b"), case("c", predicate="ABOUT")]
    binding, _ = artifact(tmp_path, cases)
    value = Q.build_review_packet(
        cases, artifact_bindings=[binding], target=TARGET, code_hashes=HASHES,
        created_at="now", seed="s", max_reviews=2)
    value["items"][0]["evidence"]["snippet"] = "FORGED"
    value["digest"] = Q.canonical_sha256({k: v for k, v in value.items() if k != "digest"})
    assert any("displayed reviewer evidence differs" in problem
               for problem in Q.validate_packet(value))
