#!/usr/bin/env python3
"""Offline Stage 3 quality benchmark and human-review packet contract.

This module never calls a model, a database, or a network service.  It turns an
already materialised, evidence-bound candidate population into a deterministic,
source-balanced review sample.  Human labels are kept separate from candidate
outputs; no threshold is selected here and no label can promote an assertion.
"""

from __future__ import annotations

import hashlib
import json
import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:  # pragma: no cover - direct offline CLI use
    sys.path.insert(0, str(REPO))

from scripts.kg.stage2_artifacts import load_verified, write_immutable
PRODUCER_VERSION = "kg-stage3-quality-benchmark/1.0"
PACKET_KIND = "kg-stage3-quality-benchmark-review"
PACKET_VERSION = PRODUCER_VERSION
COHORT_KIND = "kg-stage3-quality-benchmark-cohort"
MODE = "review-only"
COORDINATE_SYSTEM = "unicode_codepoint_half_open"
STRATUM_FIELDS = (
    "platform_or_source", "extraction_method", "document_type", "body",
    "predicate", "output_type", "assistance_mode", "outcome",
)
ASSISTANCE_MODES = ("deterministic", "ai_assisted")
OUTCOMES = ("success", "held", "failure")
DECISIONS = ("accept", "reject", "uncertain", "not_applicable")
EVIDENCE_LABELS = ("valid", "invalid", "not_applicable")
SUPPORT_LABELS = ("supported", "unsupported", "not_applicable")
LINK_LABELS = ("correct", "incorrect", "not_applicable")
EXTRACTION_LABELS = ("tp", "fp", "fn", "not_applicable")
PROMOTION_APPLICABILITY = ("applicable", "not_represented")
PROMOTION_STATES = ("promoted", "unpromoted", "not_represented")

METRIC_CONTRACT = {
    "evidence_coordinate_validity": {
        "formula": "valid_coordinate_reviews / (valid_coordinate_reviews + invalid_coordinate_reviews)",
        "denominator": "reviewed outputs for which coordinate validity is applicable",
        "threshold": None,
    },
    "unsupported_promoted_assertions": {
        "formula": "unsupported_promoted_reviews / promoted_assertion_reviews",
        "denominator": "human-labeled outputs marked promoted whose support label is applicable",
        "threshold": None,
    },
    "container_link_precision": {
        "formula": "correct_link_reviews / (correct_link_reviews + incorrect_link_reviews)",
        "denominator": "human-labeled outputs carrying a proposed canonical container link",
        "threshold": None,
    },
    "extraction_precision_by_source_predicate": {
        "formula": "tp / (tp + fp), grouped by platform_or_source and predicate",
        "denominator": "human extraction labels in each source/predicate group",
        "threshold": None,
    },
    "extraction_recall_by_source_predicate": {
        "formula": "tp / (tp + fn), grouped by platform_or_source and predicate",
        "denominator": "human-positive extraction labels in each source/predicate group",
        "threshold": None,
    },
}
PACKET_KEYS = (
    "kind", "version", "created_at", "mode", "applied", "write_path", "target",
    "bindings", "sampling", "population_strata", "sample_strata", "metrics",
    "review_policy", "accounting", "coverage_ledger", "items", "digest",
)
HIERARCHICAL_COVERAGE_FIELDS = ("source_family", "body", "extraction_method", "predicate_normalized")
BOUNDED_ALGORITHM = "sha256-rank-hierarchical-proportional-residual/1.0"
MAX_INITIAL_REVIEWS_PER_DOCUMENT = 3

class BenchmarkRefused(ValueError):
    """The population or packet violates the benchmark contract."""
def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str).encode("utf-8")).hexdigest()
def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
def artifact_binding(path: str | Path, *, kind: str | None = None) -> dict[str, Any]:
    """Bind one existing immutable artifact without changing it."""
    path = Path(path)
    document = load_verified(path)
    if kind is not None and document.get("kind") != kind:
        raise BenchmarkRefused(f"{path.name} is not {kind}")
    return {"path": str(path), "kind": document.get("kind"),
            "digest": document.get("digest"),
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
def producer_code_hashes() -> dict[str, str]:
    """Bind this packet producer, not merely a caller-supplied placeholder."""
    module = "scripts/kg/stage3_quality_benchmark.py"
    return {module: hashlib.sha256((REPO / module).read_bytes()).hexdigest()}
def _nonempty(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise BenchmarkRefused(f"{label} is required")
    return result
def _source(case: Mapping[str, Any]) -> Mapping[str, Any]:
    value = case.get("source") or {}
    if not isinstance(value, Mapping):
        raise BenchmarkRefused(f"case {case.get('case_id')!r} source is not an object")
    return value
def _output(case: Mapping[str, Any]) -> Mapping[str, Any]:
    value = case.get("output") or {}
    if not isinstance(value, Mapping):
        raise BenchmarkRefused(f"case {case.get('case_id')!r} output is not an object")
    return value
def _has_canonical_container_link(output: Mapping[str, Any]) -> bool:
    """Only pre-existing positive canonical IDs make link review applicable."""
    for field in ("agenda_item_db_id", "meeting_db_id"):
        value = output.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise BenchmarkRefused(f"output.{field} must be a positive canonical id or null")
        return True
    return False
def _promotion(output: Mapping[str, Any]) -> tuple[str, bool | None, str]:
    applicability = output.get("promotion_applicability")
    promoted = output.get("promoted")
    state = output.get("promotion_state")
    if applicability not in PROMOTION_APPLICABILITY:
        raise BenchmarkRefused("output.promotion_applicability is not canonical")
    if applicability == "not_represented":
        if promoted is not None or state != "not_represented":
            raise BenchmarkRefused("unrepresented promotion must be null and state not_represented")
    elif not isinstance(promoted, bool) or state != ("promoted" if promoted else "unpromoted"):
        raise BenchmarkRefused("applicable promotion boolean and state disagree")
    return applicability, promoted, state
def case_dimensions(case: Mapping[str, Any]) -> dict[str, str]:
    source, output = _source(case), _output(case)
    _has_canonical_container_link(output)
    _promotion(output)
    values = {
        "platform_or_source": source.get("platform_or_source"),
        "extraction_method": source.get("extraction_method"),
        "document_type": source.get("document_type"),
        "body": source.get("body"),
        "predicate": output.get("predicate"),
        "output_type": output.get("output_type"),
        "assistance_mode": output.get("assistance_mode"),
        "outcome": output.get("outcome"),
    }
    result = {key: _nonempty(value, f"{key} in case {case.get('case_id')!r}")
              for key, value in values.items()}
    if result["assistance_mode"] not in ASSISTANCE_MODES:
        raise BenchmarkRefused(f"case {case.get('case_id')!r} has unknown assistance mode")
    if result["outcome"] not in OUTCOMES:
        raise BenchmarkRefused(f"case {case.get('case_id')!r} has unknown outcome")
    return result
def _document(case: Mapping[str, Any]) -> Mapping[str, Any]:
    value = case.get("document") or {}
    if not isinstance(value, Mapping):
        raise BenchmarkRefused(f"case {case.get('case_id')!r} document is not an object")
    _nonempty(value.get("source_kind"), "document.source_kind")
    source_id = value.get("source_id")
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        raise BenchmarkRefused(f"case {case.get('case_id')!r} document.source_id is invalid")
    _nonempty(value.get("content_sha256"), "document.content_sha256")
    return value
def _evidence(case: Mapping[str, Any]) -> dict[str, Any]:
    value = case.get("evidence") or {}
    if not isinstance(value, Mapping):
        raise BenchmarkRefused(f"case {case.get('case_id')!r} evidence is not an object")
    result = dict(value)
    result.setdefault("coordinate_system", COORDINATE_SYSTEM)
    for key in ("start", "end"):
        if key in result and (not isinstance(result[key], int) or isinstance(result[key], bool)):
            raise BenchmarkRefused(f"case {case.get('case_id')!r} evidence.{key} is not an integer")
    if "start" in result and "end" in result and result["end"] < result["start"]:
        raise BenchmarkRefused(f"case {case.get('case_id')!r} evidence ends before it starts")
    return result
def validate_case(case: Mapping[str, Any]) -> list[str]:
    """Return structural problems without deciding whether an output is correct."""
    problems: list[str] = []
    try:
        _nonempty(case.get("case_id"), "case_id")
        case_dimensions(case)
        _document(case)
        _evidence(case)
    except BenchmarkRefused as exc:
        problems.append(str(exc))
    return problems
def _case_projection(case: Mapping[str, Any]) -> dict[str, Any]:
    source, output, document, evidence = _source(case), _output(case), _document(case), _evidence(case)
    retained = str(case.get("retained_text") or "")
    return {
        "case_id": _nonempty(case.get("case_id"), "case_id"),
        "dimensions": case_dimensions(case),
        "document": {"source_kind": document["source_kind"], "source_id": int(document["source_id"]),
                     "content_sha256": str(document["content_sha256"]),
                     "retained_text_sha256": text_sha256(retained) if retained else None},
        "output_digest": canonical_sha256(output),
        "evidence_digest": canonical_sha256(evidence),
        "promotion_applicability": _promotion(output)[0],
        "promoted": _promotion(output)[1],
        "promotion_state": _promotion(output)[2],
        "materialization_state": _nonempty(output.get("materialization_state"), "output.materialization_state"),
        "link_state": _nonempty(output.get("link_state"), "output.link_state"),
    }


def population_digest(cases: Sequence[Mapping[str, Any]]) -> str:
    projections = [_case_projection(case) for case in cases]
    if len({item["case_id"] for item in projections}) != len(projections):
        raise BenchmarkRefused("population contains duplicate case_id values")
    return canonical_sha256(sorted(projections, key=lambda item: item["case_id"]))


def _require_exact_cohort(artifact_bindings: Sequence[Mapping[str, Any]], *,
                          target: Mapping[str, Any], population: Mapping[str, Any]) -> dict[str, Any]:
    """Require an immutable cohort record that names this exact review population."""
    for binding in artifact_bindings:
        if not isinstance(binding, Mapping) or binding.get("kind") != COHORT_KIND:
            continue
        path = binding.get("path")
        if not isinstance(path, str):
            continue
        try:
            cohort = load_verified(path)
        except Exception as exc:  # noqa: BLE001 - evidence failure is a hard refusal
            raise BenchmarkRefused(f"candidate-cohort artifact cannot be verified: {exc}") from exc
        if cohort.get("target") != dict(target):
            raise BenchmarkRefused("candidate-cohort target differs from the review packet target")
        if cohort.get("population") != dict(population):
            raise BenchmarkRefused("candidate-cohort population differs from the review population")
        if cohort.get("digest") != binding.get("digest"):
            raise BenchmarkRefused("candidate-cohort digest differs from its binding")
        from scripts.kg import stage3_quality_cohort as cohort_contract
        problems = cohort_contract.validate_cohort(cohort)
        if problems:
            raise BenchmarkRefused("candidate-cohort reconstruction failed: " + "; ".join(problems[:2]))
        return cohort
    raise BenchmarkRefused("exact immutable candidate-cohort artifact is required")


def _stratum_key(case: Mapping[str, Any]) -> tuple[str, ...]:
    dims = case_dimensions(case)
    return tuple(dims[name] for name in STRATUM_FIELDS)


def sample_stratified(cases: Sequence[Mapping[str, Any]], *, seed: str,
                      per_stratum: int = 1) -> list[Mapping[str, Any]]:
    """Select at least one deterministic case from every full source/output stratum."""
    if not isinstance(seed, str) or not seed:
        raise BenchmarkRefused("sampling seed is required")
    if not isinstance(per_stratum, int) or isinstance(per_stratum, bool) or per_stratum < 1:
        raise BenchmarkRefused("per_stratum must be a positive integer")
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for case in cases:
        problems = validate_case(case)
        if problems:
            raise BenchmarkRefused("invalid benchmark case: " + "; ".join(problems))
        groups.setdefault(_stratum_key(case), []).append(case)
    selected: list[Mapping[str, Any]] = []
    for key in sorted(groups):
        ranked = sorted(groups[key], key=lambda item: (
            hashlib.sha256((seed + "\0" + str(item["case_id"])).encode()).hexdigest(),
            str(item["case_id"])))
        selected.extend(ranked[:per_stratum])
    return sorted(selected, key=lambda item: str(item["case_id"]))


def _coverage_dimensions(case: Mapping[str, Any]) -> dict[str, str]:
    return _coverage_dimensions_from_dims(case_dimensions(case))


def _coverage_dimensions_from_dims(dimensions: Mapping[str, Any]) -> dict[str, str]:
    dims = {name: str(dimensions[name]) for name in STRATUM_FIELDS}
    dims["predicate_normalized"] = " ".join(dims["predicate"].split()).casefold()
    source = dims["platform_or_source"]
    dims["source_family"] = (urlsplit(source).hostname or source.split("/", 1)[0]).lower()
    return dims


def _bounded_allocations(cases: Sequence[Mapping[str, Any]], *, max_reviews: int,
                         fields: Sequence[str] = HIERARCHICAL_COVERAGE_FIELDS) -> dict[tuple[str, ...], int]:
    groups = Counter(tuple(_coverage_dimensions(case)[field] for field in fields) for case in cases)
    return _allocate_counts(groups, max_reviews=max_reviews)


def _allocate_counts(groups: Mapping[tuple[str, ...], int], *, max_reviews: int) -> dict[tuple[str, ...], int]:
    if not isinstance(max_reviews, int) or isinstance(max_reviews, bool) or max_reviews < len(groups):
        raise BenchmarkRefused(
            f"max_reviews must cover all {len(groups)} hierarchical cells")
    target = min(max_reviews, sum(groups.values()))
    allocations = {key: 1 for key in groups}
    remaining = target - len(groups)
    residual_total = sum(count - 1 for count in groups.values())
    if not remaining or not residual_total:
        return allocations
    quotas = {key: remaining * (count - 1) / residual_total for key, count in groups.items()}
    for key in groups:
        allocations[key] += min(groups[key] - 1, int(quotas[key]))
    left = target - sum(allocations.values())
    order = sorted(groups, key=lambda key: (-(quotas[key] - int(quotas[key])), key))
    for key in order:
        if not left:
            break
        if allocations[key] < groups[key]:
            allocations[key] += 1
            left -= 1
    if left:
        raise BenchmarkRefused("bounded allocation could not reconcile its exact sample size")
    return allocations


def _replay_bounded_projections(projections: Sequence[Mapping[str, Any]], *, seed: str,
                                max_reviews: int) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for projection in projections:
        dims = _coverage_dimensions_from_dims(projection.get("dimensions") or {})
        key = tuple(dims[field] for field in HIERARCHICAL_COVERAGE_FIELDS)
        grouped.setdefault(key, []).append(projection)
    allocations = _allocate_counts(Counter({key: len(rows) for key, rows in grouped.items()}),
                                   max_reviews=max_reviews)
    selected = []
    cells = []
    for key in sorted(grouped):
        ranked = sorted(grouped[key], key=lambda item: (
            hashlib.sha256((seed + "\0" + str(item["case_id"])).encode()).hexdigest(),
            str(item["case_id"])))
        take, population = allocations[key], len(ranked)
        selected.extend(ranked[:take])
        cells.append({"key": list(key), "population": population, "selected": take,
                      "unsampled": population - take,
                      "inclusion_probability": take / population,
                      "analysis_weight": population / take})
    selected_dims = [_coverage_dimensions_from_dims(item["dimensions"]) for item in selected]
    population_dims = [_coverage_dimensions_from_dims(item["dimensions"]) for item in projections]
    marginals = []
    for field in HIERARCHICAL_COVERAGE_FIELDS:
        pop, sample = (Counter(dims[field] for dims in population_dims),
                       Counter(dims[field] for dims in selected_dims))
        marginals.append({"field": field, "values": [
            {"value": value, "population": count, "selected": sample[value],
             "unsampled": count - sample[value]} for value, count in sorted(pop.items())]})
    documents = Counter(int(item["document"]["source_id"]) for item in selected)
    if any(count > MAX_INITIAL_REVIEWS_PER_DOCUMENT for count in documents.values()):
        raise BenchmarkRefused("bounded sample exceeds the governed per-document review cap")
    ledger = {"coverage_fields": list(HIERARCHICAL_COVERAGE_FIELDS), "cells": cells,
              "marginals": marginals,
              "document_cluster": {"identity": "document.source_id",
                                   "max_per_document": MAX_INITIAL_REVIEWS_PER_DOCUMENT,
                                   "observed_max_selected": max(documents.values(), default=0)},
              "weighting_caveat": "unequal inclusion probabilities require cell analysis weights; raw URLs are cluster identities, not platform strata"}
    return sorted(selected, key=lambda item: str(item["case_id"])), ledger


def sample_hierarchical(cases: Sequence[Mapping[str, Any]], *, seed: str,
                        max_reviews: int) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Cover every body/method/predicate cell, then allocate residual slots proportionally."""
    if not isinstance(seed, str) or not seed:
        raise BenchmarkRefused("sampling seed is required")
    allocations = _bounded_allocations(cases, max_reviews=max_reviews)
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for case in cases:
        problems = validate_case(case)
        if problems:
            raise BenchmarkRefused("invalid benchmark case: " + "; ".join(problems))
        dims = _coverage_dimensions(case)
        key = tuple(dims[field] for field in HIERARCHICAL_COVERAGE_FIELDS)
        groups.setdefault(key, []).append(case)
    selected = []
    cell_ledger = []
    for key in sorted(groups):
        ranked = sorted(groups[key], key=lambda item: (
            hashlib.sha256((seed + "\0" + str(item["case_id"])).encode()).hexdigest(),
            str(item["case_id"])))
        take, population = allocations[key], len(ranked)
        selected.extend(ranked[:take])
        cell_ledger.append({"key": list(key), "population": population, "selected": take,
                            "unsampled": population - take,
                            "inclusion_probability": take / population,
                            "analysis_weight": population / take})
    selected_dims = [_coverage_dimensions(case) for case in selected]
    population_dims = [_coverage_dimensions(case) for case in cases]
    marginals = []
    for field in HIERARCHICAL_COVERAGE_FIELDS:
        pop = Counter(dims[field] for dims in population_dims)
        sample = Counter(dims[field] for dims in selected_dims)
        marginals.append({"field": field, "values": [
            {"value": value, "population": count, "selected": sample[value],
             "unsampled": count - sample[value]} for value, count in sorted(pop.items())]})
    documents = Counter(int(case["document"]["source_id"]) for case in selected)
    if any(count > MAX_INITIAL_REVIEWS_PER_DOCUMENT for count in documents.values()):
        raise BenchmarkRefused("bounded sample exceeds the governed per-document review cap")
    ledger = {"coverage_fields": list(HIERARCHICAL_COVERAGE_FIELDS),
              "cells": cell_ledger, "marginals": marginals,
              "document_cluster": {"identity": "document.source_id",
                                   "max_per_document": MAX_INITIAL_REVIEWS_PER_DOCUMENT,
                                   "observed_max_selected": max(documents.values(), default=0)},
              "weighting_caveat": "unequal inclusion probabilities require cell analysis weights; raw URLs are cluster identities, not platform strata"}
    return sorted(selected, key=lambda item: str(item["case_id"])), ledger


def _review_item(case: Mapping[str, Any], *, context_chars: int) -> dict[str, Any]:
    dims = case_dimensions(case)
    document, evidence, output = _document(case), _evidence(case), _output(case)
    text = str(case.get("retained_text") or "")
    start, end = evidence.get("start"), evidence.get("end")
    coordinate_valid = (isinstance(start, int) and isinstance(end, int) and
                        0 <= start < end <= len(text) and
                        evidence.get("coordinate_system") == COORDINATE_SYSTEM)
    snippet = None
    if coordinate_valid:
        left, right = max(0, start - context_chars), min(len(text), end + context_chars)
        snippet = text[left:right]
    projection = _case_projection(case)
    return {
        "case_id": str(case["case_id"]),
        "stratum": dims,
        "document": {**projection["document"]},
        "candidate": dict(output),
        "projection": projection,
        "evidence_record": dict(evidence),
        "evidence": {"coordinate_system": evidence.get("coordinate_system"),
                     "start": start, "end": end, "span_sha256": evidence.get("span_sha256"),
                     "retained_text_chars": len(text), "snippet": snippet},
        "review": {"decision": None, "evidence_coordinate": None,
                   "support": None, "container_link": None, "extraction": None},
        "allowed": {"decisions": list(DECISIONS), "evidence_coordinate": list(EVIDENCE_LABELS),
                    "support": list(SUPPORT_LABELS), "container_link": list(LINK_LABELS),
                    "extraction": list(EXTRACTION_LABELS)},
    }


def build_review_packet(cases: Sequence[Mapping[str, Any]], *, artifact_bindings: Sequence[Mapping[str, Any]],
                        target: Mapping[str, Any], code_hashes: Mapping[str, str],
                        created_at: str, seed: str, per_stratum: int = 1,
                        context_chars: int = 160, max_reviews: int | None = None) -> dict[str, Any]:
    """Build a review-only packet; all human fields arrive null and promotion is impossible."""
    if not isinstance(target, Mapping) or target.get("tier") != "development":
        raise BenchmarkRefused("benchmark target must be the development target")
    if not artifact_bindings:
        raise BenchmarkRefused("at least one authoritative artifact binding is required")
    if not isinstance(context_chars, int) or isinstance(context_chars, bool) or context_chars < 0:
        raise BenchmarkRefused("context_chars must be a non-negative integer")
    normalized = [dict(case) for case in cases]
    population = {"count": len(normalized), "sha256": population_digest(normalized)}
    expected_hashes = producer_code_hashes()
    if dict(code_hashes) != expected_hashes:
        raise BenchmarkRefused("benchmark producer code hash is not exact")
    _require_exact_cohort(artifact_bindings, target=target, population=population)
    if max_reviews is None:
        selected = sample_stratified(normalized, seed=seed, per_stratum=per_stratum)
        coverage_ledger = {}
        algorithm = "sha256-rank-per-full-stratum/1.0"
    else:
        selected, coverage_ledger = sample_hierarchical(
            normalized, seed=seed, max_reviews=max_reviews)
        algorithm = BOUNDED_ALGORITHM
    projection = [_case_projection(case) for case in normalized]
    pop_counts = Counter(tuple(item["dimensions"][name] for name in STRATUM_FIELDS)
                         for item in projection)
    sample_counts = Counter(_stratum_key(case) for case in selected)
    strata = lambda counts: [{"key": list(key), "count": count}
                             for key, count in sorted(counts.items())]
    body = {
        "kind": PACKET_KIND, "version": PACKET_VERSION, "created_at": created_at,
        "mode": MODE, "applied": False, "write_path": "absent by design",
        "target": dict(target),
        "bindings": {"artifacts": [dict(item) for item in artifact_bindings],
                      "code_hashes": dict(code_hashes),
                      "population": population},
        "sampling": {"algorithm": algorithm, "seed": seed,
                     "stratum_fields": list(STRATUM_FIELDS), "per_stratum": per_stratum,
                     "context_chars": context_chars, "max_reviews": max_reviews,
                     "coverage_fields": list(HIERARCHICAL_COVERAGE_FIELDS)},
        "population_strata": strata(pop_counts), "sample_strata": strata(sample_counts),
        "metrics": dict(METRIC_CONTRACT),
        "review_policy": {"requires_human_decision": True, "promotes_to_canonical": False,
                          "thresholds_approved": False, "thresholds": None},
        "coverage_ledger": coverage_ledger,
        "accounting": {"population": len(normalized), "selected": len(selected),
                       "strata": len(pop_counts), "reconciles": len(selected) == sum(sample_counts.values())},
        "items": [_review_item(case, context_chars=context_chars) for case in selected],
    }
    return {**body, "digest": canonical_sha256(body)}


def validate_packet(packet: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if not isinstance(packet, Mapping):
        return ["packet must be an object"]
    if packet.get("kind") != PACKET_KIND or packet.get("version") != PACKET_VERSION:
        problems.append("packet kind/version is not canonical")
    missing = sorted(set(PACKET_KEYS) - set(packet))
    unexpected = sorted(set(packet) - set(PACKET_KEYS))
    if missing or unexpected:
        problems.append(f"packet key set is not canonical: missing {missing}, unexpected {unexpected}")
    if packet.get("digest") != canonical_sha256({k: v for k, v in packet.items() if k != "digest"}):
        problems.append("packet digest does not match body")
    if packet.get("mode") != MODE or packet.get("applied") is not False:
        problems.append("packet must remain review-only and unapplied")
    if packet.get("write_path") != "absent by design":
        problems.append("packet must declare no write path")
    if not isinstance(packet.get("target"), Mapping) or packet["target"].get("tier") != "development":
        problems.append("packet target must be the development target")
    if (packet.get("review_policy") or {}).get("promotes_to_canonical") is not False:
        problems.append("review policy cannot promote to canonical")
    if (packet.get("review_policy") or {}).get("thresholds") is not None:
        problems.append("packet must not choose quality thresholds")
    bindings = packet.get("bindings") or {}
    artifacts = bindings.get("artifacts") or []
    if not artifacts:
        problems.append("at least one artifact binding is required")
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            problems.append("artifact binding must be an object")
            continue
        path = Path(str(artifact.get("path") or ""))
        if not path.is_file():
            problems.append(f"bound artifact is absent: {path}")
            continue
        try:
            loaded = load_verified(path)
            if loaded.get("digest") != artifact.get("digest") or loaded.get("kind") != artifact.get("kind"):
                problems.append(f"bound artifact identity differs: {path.name}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.get("file_sha256"):
                problems.append(f"bound artifact bytes differ: {path.name}")
        except Exception as exc:  # noqa: BLE001 - invalid evidence is a refusal
            problems.append(f"bound artifact failed verification: {exc}")
    hashes = bindings.get("code_hashes") or {}
    if dict(hashes) != producer_code_hashes():
        problems.append("benchmark producer code hash is not exact")
    population = bindings.get("population") or {}
    if not isinstance(population.get("count"), int) or population.get("count") < 1:
        problems.append("population count must be positive")
    if not population.get("sha256"):
        problems.append("population digest is required")
    cohort = None
    try:
        cohort = _require_exact_cohort(artifacts, target=packet.get("target") or {}, population=population)
    except BenchmarkRefused as exc:
        problems.append(str(exc))
    sampling = packet.get("sampling") or {}
    if tuple(sampling.get("stratum_fields") or ()) != STRATUM_FIELDS:
        problems.append("sampling stratum fields are not canonical")
    algorithm = sampling.get("algorithm")
    if not sampling.get("seed") or algorithm not in ("sha256-rank-per-full-stratum/1.0", BOUNDED_ALGORITHM):
        problems.append("sampling algorithm and seed are required")
    if tuple(sampling.get("coverage_fields") or ()) != HIERARCHICAL_COVERAGE_FIELDS:
        problems.append("sampling coverage fields are not canonical")
    if (not isinstance(sampling.get("per_stratum"), int)
            or isinstance(sampling.get("per_stratum"), bool)
            or sampling.get("per_stratum", 0) < 1):
        problems.append("sampling per_stratum must be a positive integer")
    if (not isinstance(sampling.get("context_chars"), int)
            or isinstance(sampling.get("context_chars"), bool)
            or sampling.get("context_chars", -1) < 0):
        problems.append("sampling context_chars must be a non-negative integer")
    if packet.get("metrics") != METRIC_CONTRACT:
        problems.append("metric definitions differ from the threshold-free contract")
    accounting = packet.get("accounting") or {}
    if accounting.get("population") != population.get("count"):
        problems.append("population accounting does not match its binding")
    items = packet.get("items") or []
    ids = [item.get("case_id") for item in items if isinstance(item, Mapping)]
    if len(ids) != len(set(ids)):
        problems.append("review packet contains duplicate case IDs")
    if (cohort is not None and algorithm == "sha256-rank-per-full-stratum/1.0"
            and isinstance(sampling.get("per_stratum"), int)
            and isinstance(sampling.get("context_chars"), int)
            and not isinstance(sampling.get("context_chars"), bool)
            and sampling["context_chars"] >= 0):
        from scripts.kg.stage3_quality_cohort import validate_sample_items
        problems.extend(validate_sample_items(items, cohort, seed=str(sampling.get("seed")),
                                              per_stratum=sampling["per_stratum"],
                                              context_chars=sampling.get("context_chars", -1)))
        if packet.get("coverage_ledger") != {}:
            problems.append("full-stratum sampling must not carry a hierarchical coverage ledger")
    elif cohort is not None and algorithm == BOUNDED_ALGORITHM:
        max_reviews = sampling.get("max_reviews")
        if not isinstance(max_reviews, int) or isinstance(max_reviews, bool) or max_reviews < 1:
            problems.append("bounded sampling max_reviews must be a positive integer")
        else:
            expected, expected_ledger = _replay_bounded_projections(
                cohort.get("case_projections") or [], seed=str(sampling.get("seed")),
                max_reviews=max_reviews)
            expected_ids = [str(item["case_id"]) for item in expected]
            if sorted(ids) != expected_ids:
                problems.append("review items are not the exact bounded hierarchical sample")
            if packet.get("coverage_ledger") != expected_ledger:
                problems.append("coverage ledger differs from exact bounded replay")
            expected_by_id = {str(item["case_id"]): item for item in expected}
            for item in items:
                if isinstance(item, Mapping) and item.get("projection") != expected_by_id.get(str(item.get("case_id"))):
                    problems.append(f"case {item.get('case_id')} projection differs from bounded cohort replay")
            from scripts.kg.stage3_quality_cohort import validate_sample_items
            problems.extend(validate_sample_items(
                items, cohort, seed=str(sampling.get("seed")),
                per_stratum=sampling.get("per_stratum", 1),
                context_chars=sampling.get("context_chars", -1),
                algorithm=BOUNDED_ALGORITHM, max_reviews=max_reviews,
                coverage_ledger=packet.get("coverage_ledger")))
    population_strata = packet.get("population_strata") or []
    sample_strata = packet.get("sample_strata") or []
    population_total = sum(int(item.get("count", 0)) for item in population_strata)
    sample_total = sum(int(item.get("count", 0)) for item in sample_strata)
    if population_total != population.get("count"):
        problems.append("population strata do not reconcile to the population binding")
    if accounting.get("selected") != len(items) or accounting.get("strata") != len(population_strata):
        problems.append("selection accounting does not match packet contents")
    population_by_key = {tuple(item.get("key") or ()): int(item.get("count", 0))
                         for item in population_strata}
    sample_by_key = {tuple(item.get("key") or ()): int(item.get("count", 0))
                     for item in sample_strata}
    if any(len(key) != len(STRATUM_FIELDS) for key in population_by_key):
        problems.append("population stratum key has the wrong dimensions")
    if any(count > population_by_key.get(key, -1) for key, count in sample_by_key.items()):
        problems.append("sample stratum exceeds its population stratum")
    if sample_total != len(items):
        problems.append("sample strata do not reconcile to review items")
    observed_sample_keys = Counter()
    for item in items:
        if not isinstance(item, Mapping):
            problems.append("review item must be an object")
            continue
        if tuple((item.get("stratum") or {}).get(name)
                 for name in STRATUM_FIELDS) not in sample_by_key:
            problems.append(f"case {item.get('case_id') if isinstance(item, Mapping) else None} has an unknown stratum")
        else:
            observed_sample_keys[tuple((item.get("stratum") or {}).get(name)
                                       for name in STRATUM_FIELDS)] += 1
        review = item.get("review") or {}
        if any(review.get(field) is not None for field in
               ("decision", "evidence_coordinate", "support", "container_link", "extraction")):
            problems.append(f"case {item.get('case_id')} is not an undecided review item")
    if dict(observed_sample_keys) != sample_by_key or not accounting.get("reconciles"):
        problems.append("sample strata do not reconcile to review items")
    return problems


def write_review_packet(path: str | Path, packet: Mapping[str, Any]) -> str:
    """Write one validated review packet as an immutable local artifact."""
    problems = validate_packet(packet)
    if problems:
        raise BenchmarkRefused("review packet refused: " + "; ".join(problems[:3]))
    return write_immutable(Path(path), dict(packet))


def render_review_markdown(packet: Mapping[str, Any]) -> str:
    """Render a compact human queue; immutable JSON remains the authority."""
    problems = validate_packet(packet)
    if problems:
        raise BenchmarkRefused("cannot render invalid review packet: " + "; ".join(problems[:3]))
    lines = ["# Stage 3 quality review packet", "",
             f"- Population: {packet['accounting']['population']:,}",
             f"- Initial review sample: {packet['accounting']['selected']:,}",
             f"- Full population strata (reported separately): {packet['accounting']['strata']:,}",
             f"- Sampling: `{packet['sampling']['algorithm']}`",
             "- Thresholds: not selected", "- Canonical promotion: prohibited", "",
             "| Case | Body | Predicate | Link | Evidence snippet |",
             "|---|---|---|---|---|"]
    for item in packet["items"]:
        snippet = str(item["evidence"].get("snippet") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{item['case_id']}` | {item['stratum']['body']} | "
                     f"{item['stratum']['predicate']} | {item['candidate']['link_state']} | {snippet} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a bounded offline Stage 3 review packet")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-out", type=Path, required=True)
    parser.add_argument("--cohort-out", type=Path, required=True)
    parser.add_argument("--packet-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--max-reviews", type=int, default=400)
    args = parser.parse_args(argv)
    from scripts.kg import stage3_quality_candidate_source as source_contract
    from scripts.kg import stage3_quality_cohort as cohort_contract
    prior = load_verified(args.source)
    refreshed = source_contract.build_source(
        rows=prior["rows"], selection_path=prior["selection_binding"]["path"],
        created_at=args.stamp)
    write_immutable(args.source_out, refreshed)
    cohort = cohort_contract.build_cohort(source_path=args.source_out, created_at=args.stamp)
    write_immutable(args.cohort_out, cohort)
    binding = artifact_binding(args.cohort_out, kind=COHORT_KIND)
    packet = build_review_packet(
        refreshed["candidate_cases"], artifact_bindings=[binding], target=refreshed["target"],
        code_hashes=producer_code_hashes(), created_at=args.stamp, seed=args.seed,
        max_reviews=args.max_reviews)
    write_review_packet(args.packet_out, packet)
    args.markdown_out.write_text(render_review_markdown(packet), encoding="utf-8")
    print(json.dumps({"status": "success", "source": refreshed["digest"],
                      "cohort": cohort["digest"], "packet": packet["digest"],
                      "population": packet["accounting"]["population"],
                      "selected": packet["accounting"]["selected"]}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def evaluate_labels(packet: Mapping[str, Any], labels: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Compute descriptive metrics only; absent denominators remain explicitly undefined."""
    if validate_packet(packet):
        raise BenchmarkRefused("cannot evaluate an invalid review packet")
    items = {str(item["case_id"]): item for item in packet.get("items") or []}
    unknown = sorted(set(labels) - set(items))
    if unknown:
        raise BenchmarkRefused(f"labels contain unknown case IDs: {unknown[:3]}")
    required = ("evidence_coordinate", "support", "container_link", "extraction")
    problems = []
    for case_id, label in labels.items():
        for field in required:
            value = label.get(field)
            allowed = {"evidence_coordinate": EVIDENCE_LABELS, "support": SUPPORT_LABELS,
                       "container_link": LINK_LABELS, "extraction": EXTRACTION_LABELS}[field]
            if value not in allowed:
                problems.append(f"case {case_id} has invalid {field} label")
        if case_id in items:
            linked = _has_canonical_container_link(items[case_id]["candidate"])
            applicable = label.get("container_link") != "not_applicable"
            if linked != applicable:
                problems.append(f"case {case_id} container_link label is incompatible with its candidate")
    if problems:
        raise BenchmarkRefused("; ".join(problems))
    coordinate = Counter(label["evidence_coordinate"] for label in labels.values()
                         if label["evidence_coordinate"] != "not_applicable")
    support = Counter(label["support"] for case_id, label in labels.items()
                      if items[case_id]["candidate"].get("promoted") is True
                      and label["support"] != "not_applicable")
    links = Counter(label["container_link"] for label in labels.values()
                    if label["container_link"] != "not_applicable")
    groups: dict[tuple[str, str], Counter] = {}
    for case_id, label in labels.items():
        dims = items[case_id]["stratum"]
        if label["extraction"] != "not_applicable":
            groups.setdefault((dims["platform_or_source"], dims["predicate"]),
                              Counter())[label["extraction"]] += 1
    ratio = lambda num, den: {"numerator": num, "denominator": den,
                              "value": (num / den) if den else None,
                              "defined": bool(den)}
    extraction = {}
    for key, counts in sorted(groups.items()):
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        extraction["|".join(key)] = {"counts": dict(counts),
                                      "precision": ratio(tp, tp + fp),
                                      "recall": ratio(tp, tp + fn)}
    return {"packet_digest": packet["digest"], "labeled": len(labels),
            "pending": len(items) - len(labels),
            "metrics": {"evidence_coordinate_validity": ratio(coordinate["valid"], sum(coordinate.values())),
                        "unsupported_promoted_assertions": ratio(support["unsupported"], sum(support.values())),
                        "container_link_precision": ratio(links["correct"], sum(links.values())),
                        "extraction_by_source_predicate": extraction}}
