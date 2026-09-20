#!/usr/bin/env python3
"""Build an immutable, evidence-only OP-REPAIR candidate plan.

This module has deliberately no database, network, or apply path.  It accepts a
complete production-reference-evidence artifact, verifies both that artifact
and its bound G5 artifact, and proposes only public_body_id backfills whose row,
upstream meeting, and sole registry candidate express the same identity.

The result is deliberately NOT a G7-authorizable plan: the fresh-backup receipt,
release manifest, exact commit, expiry/nonce, and rollback owner do not exist yet.
Those missing bindings are machine-readable and ``apply_blocked`` is always true.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _candidate in (REPO, REPO / "scripts", Path(__file__).resolve().parent):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import production_preflight as preflight  # noqa: E402
import production_reference_evidence as evidence_tool  # noqa: E402

SCHEMA = "production-reference-repair-plan/1"
EXPECTED_EVIDENCE_SCHEMA = evidence_tool.SCHEMA
PROPOSABLE = {
    "meetings.public_body_id.dangling_or_null": "meetings",
    "agenda_items.public_body_id.dangling_or_null": "agenda_items",
}


class Refused(RuntimeError):
    """Fail-closed plan-construction refusal."""


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Refused(f"{label} could not be read as JSON") from exc
    if not isinstance(value, dict):
        raise Refused(f"{label} is not an object")
    return value


def _verify_digest(artifact: Mapping[str, Any], label: str) -> str:
    recorded = artifact.get("digest")
    body = {key: value for key, value in artifact.items() if key != "digest"}
    actual = preflight.digest(body)
    if not isinstance(recorded, str) or recorded != actual:
        raise Refused(f"{label} digest mismatch")
    return actual


def load_bound_evidence(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify the evidence artifact and its exact, path-bound G5 artifact."""
    evidence = _load_object(path, "reference evidence")
    evidence_digest = _verify_digest(evidence, "reference evidence")
    if (evidence.get("schema") != EXPECTED_EVIDENCE_SCHEMA or
            evidence.get("operation") != "OP-PREFLIGHT" or
            evidence.get("status") != "VALID" or
            evidence.get("mode") != "read-only-evidence-only"):
        raise Refused("reference evidence contract or status is invalid")
    transaction = evidence.get("transaction") or {}
    if (transaction.get("connection_count") != 1 or
            transaction.get("transaction_count") != 1 or
            str(transaction.get("isolation", "")).lower() != "repeatable read" or
            transaction.get("read_only") is not True):
        raise Refused("reference evidence does not prove its transaction contract")

    binding = evidence.get("g5_binding") or {}
    raw_path = binding.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise Refused("reference evidence has no G5 path binding")
    g5_path = Path(raw_path)
    if not g5_path.is_absolute():
        g5_path = REPO / g5_path
    g5, g5_digest = evidence_tool.load_g5(g5_path)
    if binding.get("digest") != g5_digest:
        raise Refused("reference evidence G5 digest binding mismatch")
    if evidence.get("target") != {
            key: g5["target"].get(key) for key in evidence.get("target", {})}:
        raise Refused("reference evidence target differs from bound G5 target")
    return evidence, {"path": raw_path, "digest": g5_digest,
                      "evidence_digest": evidence_digest}


def _before(row: Mapping[str, Any]) -> dict[str, Any]:
    """Retain every observed row value, excluding derived review metadata."""
    return {key: row[key] for key in sorted(row)
            if key not in {"candidate_parent_count", "candidate_parents",
                           "upstream_identity"}}


def _identity_problem(category: str, row: Mapping[str, Any]) -> str | None:
    candidates = row.get("candidate_parents")
    count = row.get("candidate_parent_count")
    if not isinstance(count, int) or not isinstance(candidates, list):
        return "malformed_candidate_evidence"
    if count == 0:
        return "zero_candidate_parents"
    if count != 1 or len(candidates) != 1:
        return "multiple_candidate_parents" if count > 1 else "candidate_count_mismatch"
    if category not in PROPOSABLE:
        return "category_not_safe_for_automatic_repair"

    candidate = candidates[0]
    upstream = row.get("upstream_identity")
    if not isinstance(candidate, dict) or not isinstance(upstream, dict):
        return "malformed_identity_evidence"
    expected = (candidate.get("body_code"), candidate.get("jurisdiction_id"))
    if expected != (row.get("body"), row.get("jurisdiction_id")):
        return "row_identity_disagrees_with_candidate"

    if category.startswith("meetings."):
        if (upstream.get("meeting_db_id") != row.get("id") or
                upstream.get("meeting_id") != row.get("meeting_id") or
                upstream.get("body") != row.get("body") or
                upstream.get("jurisdiction_id") != row.get("jurisdiction_id")):
            return "upstream_identity_disagrees_with_row"
    else:
        if (upstream.get("meeting_db_id") != row.get("meeting_db_id") or
                upstream.get("meeting_id") != row.get("meeting_id") or
                upstream.get("body") != row.get("body") or
                upstream.get("jurisdiction_id") != row.get("jurisdiction_id")):
            return "upstream_identity_disagrees_with_row"
        upstream_parent = upstream.get("public_body_id")
        if upstream_parent is not None and upstream_parent != candidate.get("id"):
            return "upstream_parent_disagrees_with_candidate"
    return None


def build_plan(evidence: Mapping[str, Any], bindings: Mapping[str, Any]) -> dict[str, Any]:
    """Classify every evidence row; return a digest-bound plan with no apply path."""
    categories = evidence.get("categories")
    if not isinstance(categories, dict):
        raise Refused("reference evidence categories are absent")
    proposals: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    for category in sorted(categories):
        cohort = categories[category]
        if not isinstance(cohort, dict) or not isinstance(cohort.get("rows"), list):
            raise Refused(f"category {category} has malformed rows")
        rows = cohort["rows"]
        if cohort.get("truncated") is not False:
            raise Refused(f"category {category} is truncated")
        if cohort.get("total") != len(rows) or cohort.get("sample_count") != len(rows):
            raise Refused(f"category {category} is not a complete row population")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), int):
                raise Refused(f"category {category} has a row without an integer id")
            record = {"category": category, "table": PROPOSABLE.get(category),
                      "primary_key": {"id": row["id"]}, "before": _before(row)}
            problem = _identity_problem(category, row)
            if problem is None:
                candidate = row["candidate_parents"][0]
                record.update({
                    "set": {"public_body_id": candidate["id"]},
                    "candidate_parent": candidate,
                    "upstream_identity": row["upstream_identity"],
                })
                proposals.append(record)
            else:
                record.update({"reason": problem,
                               "candidate_parent_count": row.get("candidate_parent_count"),
                               "candidate_parents": row.get("candidate_parents"),
                               "upstream_identity": row.get("upstream_identity")})
                quarantine.append(record)

    body = {
        "schema": SCHEMA,
        "operation": "OP-REPAIR",
        "status": "CANDIDATE-NOT-AUTHORIZABLE",
        "mode": "immutable-candidate-plan-only-no-apply",
        "apply_blocked": True,
        "missing_g7_bindings": [
            "exact_commit", "release_manifest_digest", "fresh_backup_receipt",
            "expiry", "nonce", "rollback_owner",
        ],
        "target": evidence.get("target"),
        "bindings": {
            "reference_evidence": {"digest": bindings["evidence_digest"]},
            "g5": {"path": bindings["path"], "digest": bindings["digest"]},
        },
        "proposals": proposals,
        "quarantine": quarantine,
        "counts": {"proposals": len(proposals), "quarantine": len(quarantine)},
    }
    return {**body, "digest": preflight.digest(body)}


def run(*, evidence_path: Path, output: Path) -> dict[str, Any]:
    evidence, bindings = load_bound_evidence(evidence_path)
    plan = build_plan(evidence, bindings)
    try:
        preflight.write_exclusive(output, plan)
    except Exception as exc:
        raise Refused(f"repair plan creation failed: {exc}") from exc
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(evidence_path=args.evidence, output=args.output)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"status": "CANDIDATE-NOT-AUTHORIZABLE",
                      "apply_blocked": True, "path": str(args.output),
                      "digest": result["digest"], "counts": result["counts"]},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
