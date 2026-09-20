#!/usr/bin/env python3
"""Build an immutable, read-only quality cohort from one exact candidate source.

The builder does not mine facts from Stage 3 reports.  A source must already carry
complete observed candidate cases, so no predicate, outcome, or stratum is invented.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:  # pragma: no cover - enables direct offline CLI use
    sys.path.insert(0, str(REPO))

from scripts.kg import stage3_quality_benchmark as benchmark
from scripts.kg import stage3_quality_candidate_source as candidate_source
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable

KIND = benchmark.COHORT_KIND
VERSION = "kg-stage3-quality-cohort/1.0"
SOURCE_KIND = "kg-stage3-quality-candidate-source"
SOURCE_CASES = "candidate_cases"
SOURCE_CASES_DIGEST = "candidate_cases_sha256"
COHORT_KEYS = ("kind", "version", "created_at", "mode", "applied", "write_path", "target",
               "source_binding", "code_hashes", "case_projections", "population",
               "observed_strata", "digest")


class CohortRefused(ValueError):
    """The source cannot prove complete observed benchmark candidates."""


def canonical_sha256(value: Any) -> str:
    return benchmark.canonical_sha256(value)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    names = ("scripts/kg/stage3_quality_cohort.py", "scripts/kg/stage3_quality_benchmark.py")
    return {name: _sha(repo / name) for name in names}


def _source_binding(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if is_obsolete(path):
        raise CohortRefused(f"candidate source is obsolete: {path.name}")
    source = load_verified(path)
    if source.get("kind") != SOURCE_KIND:
        raise CohortRefused(f"candidate source must be {SOURCE_KIND}")
    source_problems = candidate_source.validate_source(source)
    if source_problems:
        raise CohortRefused("candidate source reconstruction failed: " + "; ".join(source_problems[:2]))
    target = source.get("target")
    if not isinstance(target, Mapping) or target.get("tier") != "development":
        raise CohortRefused("candidate source target must be development")
    cases = source.get(SOURCE_CASES)
    if not isinstance(cases, list) or not cases:
        raise CohortRefused("candidate source has no complete observed candidate_cases")
    if source.get(SOURCE_CASES_DIGEST) != canonical_sha256(cases):
        raise CohortRefused("candidate source candidate_cases_sha256 differs from its cases")
    return source, {"path": str(path), "kind": source["kind"], "digest": source["digest"],
                    "file_sha256": _sha(path)}


def _projections(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for case in cases:
        problems = benchmark.validate_case(case)
        if problems:
            raise CohortRefused("candidate case is incomplete: " + "; ".join(problems))
        result.append(benchmark._case_projection(case))
    result.sort(key=lambda row: row["case_id"])
    if len({row["case_id"] for row in result}) != len(result):
        raise CohortRefused("candidate source has duplicate case IDs")
    return result


def sampled_projection_ids(projections: Sequence[Mapping[str, Any]], *, seed: str,
                           per_stratum: int) -> list[str]:
    """Replay benchmark sampling from immutable projections alone."""
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for projection in projections:
        dimensions = projection.get("dimensions") or {}
        key = tuple(str(dimensions.get(name, "")) for name in benchmark.STRATUM_FIELDS)
        groups.setdefault(key, []).append(projection)
    selected: list[str] = []
    for rows in groups.values():
        ranked = sorted(rows, key=lambda row: (
            hashlib.sha256((seed + "\0" + str(row["case_id"])).encode()).hexdigest(),
            str(row["case_id"])))
        selected.extend(str(row["case_id"]) for row in ranked[:per_stratum])
    return sorted(selected)


def validate_sample_items(items: Sequence[Mapping[str, Any]], cohort: Mapping[str, Any], *,
                          seed: str, per_stratum: int, context_chars: int,
                          algorithm: str = "sha256-rank-per-full-stratum/1.0",
                          max_reviews: int | None = None,
                          coverage_ledger: Any = None) -> list[str]:
    projections = {str(row.get("case_id")): row for row in cohort.get("case_projections") or []}
    ids = [str(item.get("case_id")) for item in items if isinstance(item, Mapping)]
    problems = []
    cases: dict[str, Mapping[str, Any]] = {}
    try:
        source, _binding = _source_binding(Path(str((cohort.get("source_binding") or {}).get("path"))))
        cases = {str(case.get("case_id")): case for case in source.get(SOURCE_CASES) or []}
    except (CohortRefused, OSError, TypeError, ValueError) as exc:
        problems.append(f"review evidence source cannot be reconstructed: {exc}")
    expected_ids: list[str] = []
    if algorithm == benchmark.BOUNDED_ALGORITHM and cases:
        expected_cases, expected_ledger = benchmark.sample_hierarchical(
            list(cases.values()), seed=seed, max_reviews=max_reviews)
        expected_ids = sorted(str(case["case_id"]) for case in expected_cases)
        if coverage_ledger != expected_ledger:
            problems.append("bounded coverage ledger differs from exact source reconstruction")
    else:
        expected_ids = sampled_projection_ids(list(projections.values()), seed=seed,
                                              per_stratum=per_stratum)
    if sorted(ids) != expected_ids:
        problems.append("review items are not the exact deterministic cohort sample")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        case_id, expected = str(item.get("case_id")), projections.get(str(item.get("case_id")))
        if expected is None or item.get("projection") != expected:
            problems.append(f"case {case_id} projection differs from the bound cohort")
            continue
        candidate, evidence_record = item.get("candidate") or {}, item.get("evidence_record") or {}
        document, evidence = item.get("document") or {}, item.get("evidence") or {}
        source_case = cases.get(case_id)
        if (benchmark.canonical_sha256(candidate) != expected.get("output_digest")
                or benchmark.canonical_sha256(evidence_record) != expected.get("evidence_digest")
                or document != expected.get("document")
                or item.get("stratum") != expected.get("dimensions")):
            problems.append(f"case {case_id} candidate/evidence/content/stratum drift")
        for field in ("coordinate_system", "start", "end", "span_sha256"):
            if evidence.get(field) != evidence_record.get(field):
                problems.append(f"case {case_id} evidence coordinate/hash drift")
        if source_case is not None:
            text = source_case.get("retained_text")
            start, end = evidence_record.get("start"), evidence_record.get("end")
            expected_snippet = None
            if (isinstance(text, str) and isinstance(start, int) and not isinstance(start, bool)
                    and isinstance(end, int) and not isinstance(end, bool)
                    and 0 <= start < end <= len(text)):
                expected_snippet = text[max(0, start - context_chars):min(len(text), end + context_chars)]
            if (evidence.get("retained_text_chars") != len(text or "")
                    or evidence.get("snippet") != expected_snippet):
                problems.append(f"case {case_id} displayed reviewer evidence differs from retained text")
    return problems


def build_cohort(*, source_path: str | Path, created_at: str,
                 repo: Path = REPO) -> dict[str, Any]:
    path = Path(source_path)
    source, binding = _source_binding(path)
    cases = source[SOURCE_CASES]
    projections = _projections(cases)
    population = {"count": len(cases), "sha256": benchmark.population_digest(cases)}
    strata = Counter(tuple(row["dimensions"][name] for name in benchmark.STRATUM_FIELDS)
                     for row in projections)
    body = {
        "kind": KIND, "version": VERSION, "created_at": created_at, "mode": "read-only",
        "applied": False, "write_path": "absent by design", "target": dict(source["target"]),
        "source_binding": binding, "code_hashes": code_hashes(repo),
        "case_projections": projections, "population": population,
        "observed_strata": [{"key": list(key), "count": count}
                             for key, count in sorted(strata.items())],
    }
    return {**body, "digest": canonical_sha256(body)}


def validate_cohort(cohort: Mapping[str, Any], *, repo: Path = REPO) -> list[str]:
    if not isinstance(cohort, Mapping):
        return ["cohort must be an object"]
    problems = []
    if set(cohort) != set(COHORT_KEYS):
        problems.append("cohort key set is not canonical")
    if cohort.get("digest") != canonical_sha256({k: v for k, v in cohort.items() if k != "digest"}):
        problems.append("cohort digest does not match body")
    binding = cohort.get("source_binding") or {}
    try:
        expected = build_cohort(source_path=binding.get("path"),
                                created_at=str(cohort.get("created_at")), repo=repo)
    except (CohortRefused, OSError, TypeError, ValueError) as exc:
        return problems + [f"cohort cannot be reconstructed: {exc}"]
    if expected != dict(cohort):
        problems.append("cohort differs from exact source reconstruction")
    return problems


def blocker_report(paths: Sequence[Path]) -> dict[str, Any]:
    found = []
    for path in paths:
        try:
            source = load_verified(path)
        except Exception:
            continue
        if source.get("kind") == SOURCE_KIND and not is_obsolete(path):
            found.append({"path": str(path), "digest": source.get("digest")})
    return {
        "status": "blocked", "code": "missing_authoritative_quality_candidate_source",
        "candidate_sources_found": found,
        "required": {"kind": SOURCE_KIND, "target": "exact development target",
                     SOURCE_CASES: "nonempty exact observed cases", SOURCE_CASES_DIGEST:
                     "canonical SHA-256 of candidate_cases"},
        "reason": "current Stage 3 artifacts carry counts, holds, or projections, not complete candidate output and evidence cases",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    parser.add_argument("--stamp", required=True)
    args = parser.parse_args(argv)
    if args.source is None:
        print(json.dumps(blocker_report(sorted((REPO / "data/kg-plans").glob("kg-stage3-*.json"))), sort_keys=True))
        return 2
    cohort = build_cohort(source_path=args.source, created_at=args.stamp)
    problems = validate_cohort(cohort)
    if problems:
        raise CohortRefused("refusing cohort: " + "; ".join(problems))
    path = args.out_dir / f"kg-stage3-quality-benchmark-cohort-{args.stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"status": "success", "path": str(path),
                      "digest": write_immutable(path, cohort)}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
