#!/usr/bin/env python3
"""Read-only exact disposition plan for the B3 spans held outside agenda items.

This reconciles existing immutable B3 evidence only.  It creates no container, makes
no database request, and has no apply surface.  A span may be item-linked only from
its pre-existing canonical item id; otherwise a proven document meeting is the most
specific honest outcome.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:  # pragma: no cover - enables direct offline CLI use
    sys.path.insert(0, str(REPO))

from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable
from scripts.kg import stage3_b3_baseline as baseline_contract
from scripts.kg import stage3_b3_span_contract as span_contract
from scripts.kg.stage3_b3_span_contract import SPAN_KINDS

KIND = "kg-stage3-b3-held-span-disposition-plan"
VERSION = "kg-stage3-b3-held-span-disposition-plan/1.0"
BASELINE_KIND = "kg-stage3-b3-span-baseline"
DRY_PLAN_KIND = "kg-stage3-b3-span-dry-plan"
OUTCOMES = ("item_linked", "meeting_linked", "document_only", "source_gap", "invalid_span")
REASON_CODES = {
    "item_linked": "canonical_document_item_id",
    "meeting_linked": "canonical_document_meeting_id",
    "document_only": "document_evidence_without_meeting_or_item",
    "source_gap": "document_evidence_missing_or_incomplete",
    "invalid_span": "invalid_held_span",
}
CODE_MODULES = (
    "scripts/kg/stage3_b3_disposition_plan.py",
    "scripts/kg/stage3_b3_span_contract.py",
    "scripts/kg/stage3_b3_baseline.py",
    "scripts/kg/stage2_artifacts.py",
)
PLAN_KEYS = ("kind", "version", "created_at", "mode", "applied", "write_path", "target",
             "bindings", "producer", "outcome_registry", "accounting", "dispositions", "digest")


class PlanRefused(RuntimeError):
    """The evidence cannot support a total, no-fabrication disposition."""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    result = {}
    for name in CODE_MODULES:
        path = repo / name
        if not path.is_file():
            raise PlanRefused(f"required code module is absent: {name}")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _identity(value: Any) -> tuple[int, str, int, int] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    doc_id, kind, start, end = value
    if (not isinstance(doc_id, int) or isinstance(doc_id, bool) or doc_id <= 0
            or not isinstance(kind, str) or kind not in SPAN_KINDS
            or not isinstance(start, int) or isinstance(start, bool) or start < 0
            or not isinstance(end, int) or isinstance(end, bool) or end <= start):
        return None
    return doc_id, kind, start, end


def _document_evidence(document: Mapping[str, Any]) -> bool:
    text_digest = document.get("text_sha256")
    method = document.get("text_extraction_method")
    return (isinstance(text_digest, str) and len(text_digest) == 64
            and all(char in "0123456789abcdef" for char in text_digest.lower())
            and isinstance(method, str) and bool(method.strip()))


def outcome_registry() -> dict[str, dict[str, Any]]:
    return {
        "item_linked": {"reason_code": REASON_CODES["item_linked"], "container": "agenda_item",
                        "requires": ["one positive canonical item id"],
                        "forbids": ["meeting-membership inference", "title or number matching"]},
        "meeting_linked": {"reason_code": REASON_CODES["meeting_linked"], "container": "meeting",
                           "requires": ["positive supporting-document meeting id"],
                           "forbids": ["agenda-item fabrication"]},
        "document_only": {"reason_code": REASON_CODES["document_only"], "container": "supporting_document",
                          "requires": ["bound document evidence"],
                          "forbids": ["meeting or agenda-item inference"]},
        "source_gap": {"reason_code": REASON_CODES["source_gap"], "container": None,
                       "requires": ["missing or incomplete bound document evidence"],
                       "forbids": ["container fabrication"]},
        "invalid_span": {"reason_code": REASON_CODES["invalid_span"], "container": None,
                         "requires": ["invalid or contradictory source assertion"],
                         "forbids": ["any linkage"]},
    }


def _disposition(hold: Mapping[str, Any], documents: Mapping[int, Mapping[str, Any]],
                 *, baseline_digest: str) -> dict[str, Any]:
    identity = _identity(hold.get("identity"))
    problems = list(hold.get("problems") or [])
    if identity is None or problems or hold.get("disposition") != "hold_unlinked_container":
        return {"source_index": hold.get("index"), "outcome": "invalid_span",
                "reason_code": REASON_CODES["invalid_span"], "evidence_identity": {"baseline_digest": baseline_digest,
                "held_identity": hold.get("identity"), "source_disposition": hold.get("disposition")}}
    doc_id, kind, start, end = identity
    document = documents.get(doc_id)
    evidence = {"baseline_digest": baseline_digest, "span_identity": list(identity),
                "supporting_doc_id": doc_id, "span_kind": kind, "offsets": [start, end]}
    if not isinstance(document, Mapping) or not _document_evidence(document):
        return {"source_index": hold["index"], "outcome": "source_gap",
                "reason_code": REASON_CODES["source_gap"], "evidence_identity": evidence}
    evidence.update({"document_text_sha256": document.get("text_sha256"),
                     "extraction_method": document.get("text_extraction_method")})
    item_id = document.get("agenda_item_db_id")
    if isinstance(item_id, int) and not isinstance(item_id, bool) and item_id > 0:
        return {"source_index": hold["index"], "outcome": "item_linked",
                "reason_code": REASON_CODES["item_linked"], "agenda_item_db_id": item_id,
                "evidence_identity": evidence}
    meeting_id = document.get("meeting_db_id")
    if isinstance(meeting_id, int) and not isinstance(meeting_id, bool) and meeting_id > 0:
        return {"source_index": hold["index"], "outcome": "meeting_linked",
                "reason_code": REASON_CODES["meeting_linked"], "meeting_db_id": meeting_id,
                "evidence_identity": evidence}
    return {"source_index": hold["index"], "outcome": "document_only",
            "reason_code": REASON_CODES["document_only"], "evidence_identity": evidence}


def _load(path: str | Path, expected_kind: str) -> dict[str, Any]:
    artifact = Path(path)
    if is_obsolete(artifact):
        raise PlanRefused(f"{artifact.name} is obsolete")
    document = load_verified(artifact)
    if document.get("kind") != expected_kind:
        raise PlanRefused(f"{artifact.name} is not {expected_kind}")
    return document


def _artifact_path(reference: str | Path, *, repo: Path = REPO) -> Path:
    """Resolve a stored repo-relative artifact path deterministically."""
    path = Path(reference)
    if not path.is_absolute():
        repo_path = repo / path
        if repo_path.exists():
            return repo_path.resolve()
    return path.resolve()


def _same_artifact(left: str | Path, right: str | Path, *, repo: Path = REPO) -> bool:
    """Compare artifact references, including repo-relative stored paths."""
    if not isinstance(left, (str, Path)) or not isinstance(right, (str, Path)):
        return False
    return _artifact_path(left, repo=repo) == _artifact_path(right, repo=repo)


def _validate_upstream_contract(*, baseline: Mapping[str, Any], dry_plan: Mapping[str, Any],
                                baseline_path: str | Path, dry_plan_path: str | Path,
                                repo: Path = REPO) -> None:
    """Refuse re-signed upstream artifacts whose producer contract drifted.

    ``load_verified`` proves only that an artifact is internally self-consistent;
    it cannot prove that a caller did not edit the body and recompute its digest.
    The disposition therefore pins the complete upstream producer/version/hash
    contract before using the held population.
    """
    if baseline.get("version") != baseline_contract.PRODUCER_VERSION:
        raise PlanRefused("baseline producer version is not the current B3 baseline contract")
    if baseline.get("mode") != "read-only" or baseline.get("applied") is not False:
        raise PlanRefused("baseline is not an unapplied read-only artifact")
    if baseline.get("write_path") != "absent by design":
        raise PlanRefused("baseline has a write path")
    expected_hashes = baseline_contract.code_hashes(repo)
    if baseline.get("code_hashes") != expected_hashes:
        raise PlanRefused("baseline producer code hashes do not match the current B3 baseline")

    if dry_plan.get("version") != span_contract.PRODUCER_VERSION:
        raise PlanRefused("dry plan producer version is not the current B3 span contract")
    if dry_plan.get("mode") != "dry-run" or dry_plan.get("applied") is not False:
        raise PlanRefused("dry plan is not an unapplied dry-run artifact")
    if dry_plan.get("write_path") != "absent by design":
        raise PlanRefused("dry plan has a write path")
    producer = dry_plan.get("producer")
    expected_producer = {
        "module": "scripts/kg/stage3_b3_span_contract.py",
        "version": span_contract.PRODUCER_VERSION,
        "code_hashes": expected_hashes,
    }
    if producer != expected_producer:
        raise PlanRefused("dry plan producer contract does not match the current B3 span contract")
    operations = dry_plan.get("operations")
    if not isinstance(operations, list) or dry_plan.get("operations_sha256") != canonical_sha256(operations):
        raise PlanRefused("dry plan operations digest is not canonical")
    if not _same_artifact((dry_plan.get("baseline_artifact") or {}).get("path"), baseline_path,
                          repo=repo):
        raise PlanRefused("dry plan baseline path is not the exact baseline artifact")


def build_plan(*, baseline_path: str | Path, dry_plan_path: str | Path,
               created_at: str, repo: Path = REPO) -> dict[str, Any]:
    baseline = _load(baseline_path, BASELINE_KIND)
    dry_plan = _load(dry_plan_path, DRY_PLAN_KIND)
    _validate_upstream_contract(
        baseline=baseline, dry_plan=dry_plan,
        baseline_path=baseline_path, dry_plan_path=dry_plan_path, repo=repo)
    if baseline.get("target") != dry_plan.get("target"):
        raise PlanRefused("B3 baseline and dry plan targets differ")
    binding = dry_plan.get("baseline_artifact") or {}
    if (not isinstance(binding, Mapping) or binding.get("digest") != baseline.get("digest")
            or not isinstance(binding.get("path"), str)
            or not _same_artifact(binding["path"], baseline_path, repo=repo)):
        raise PlanRefused("B3 dry plan does not bind the exact baseline artifact")
    if dry_plan.get("baseline_digest") != baseline.get("digest"):
        raise PlanRefused("B3 dry plan baseline digest differs from the exact baseline")
    if dry_plan.get("schema_sha256") != (baseline.get("schema") or {}).get("sha256"):
        raise PlanRefused("B3 dry plan schema binding differs from the exact baseline")
    if dry_plan.get("input_bindings") != baseline.get("inputs"):
        raise PlanRefused("B3 dry plan input bindings differ from the exact baseline")
    holds = list(baseline.get("holds") or [])
    coverage = baseline.get("coverage") or {}
    expected_population = int(coverage.get("held", -1))
    if (expected_population != len(holds) or coverage.get("accepted") != 0
            or coverage.get("proposed") != expected_population or coverage.get("reconciles") is not True):
        raise PlanRefused("baseline does not describe one exact held-only population")
    dry_accounting = dry_plan.get("accounting") or {}
    if (dry_plan.get("operations") != [] or dry_accounting.get("proposed") != expected_population
            or dry_accounting.get("held") != expected_population
            or dry_accounting.get("would_insert") != 0
            or dry_accounting.get("reconciles") is not True):
        raise PlanRefused("B3 dry plan is not the exact no-operation held population")
    document_rows = list(baseline.get("documents") or [])
    documents = {int(row["id"]): dict(row) for row in document_rows
                 if isinstance(row, Mapping) and isinstance(row.get("id"), int)
                 and not isinstance(row.get("id"), bool) and int(row["id"]) > 0}
    if len(documents) != len(document_rows):
        raise PlanRefused("document projection has a missing, invalid, or duplicate canonical id")
    records = [_disposition(hold, documents, baseline_digest=str(baseline["digest"]))
               for hold in holds]
    records.sort(key=lambda row: int(row.get("source_index", -1)))
    indexes = [row["source_index"] for row in records]
    if indexes != list(range(expected_population)):
        raise PlanRefused("every held span must have exactly one unique source index")
    counts = {name: sum(row["outcome"] == name for row in records) for name in OUTCOMES}
    body = {"kind": KIND, "version": VERSION, "created_at": created_at, "mode": "read-only",
            "applied": False, "write_path": "absent by design", "target": dict(baseline["target"]),
            "bindings": {"baseline": {"path": binding["path"], "digest": baseline["digest"]},
                         "dry_plan": {"path": str(dry_plan_path), "digest": dry_plan["digest"]},
                         "population": {"held_spans": expected_population,
                                        "identity_sha256": canonical_sha256([r["evidence_identity"]
                                                                              for r in records])}},
            "producer": {"module": "scripts/kg/stage3_b3_disposition_plan.py", "code_hashes": code_hashes(repo)},
            "outcome_registry": outcome_registry(),
            "accounting": {"population": expected_population, "by_outcome": counts,
                           "classified": len(records), "reconciles": len(records) == expected_population,
                           "item_container_fabricated": False, "data_operations_proposed": 0},
            "dispositions": records}
    return {**body, "digest": canonical_sha256(body)}


def validate_plan(plan: Any, *, repo: Path = REPO) -> list[str]:
    if not isinstance(plan, Mapping):
        return ["plan must be an object"]
    problems: list[str] = []
    if set(plan) != set(PLAN_KEYS):
        problems.append("top-level key set is not canonical")
    if plan.get("digest") != canonical_sha256({k: v for k, v in plan.items() if k != "digest"}):
        problems.append("plan digest does not match its body")
    bindings = plan.get("bindings") or {}
    try:
        expected = build_plan(baseline_path=(bindings.get("baseline") or {}).get("path"),
                              dry_plan_path=(bindings.get("dry_plan") or {}).get("path"),
                              created_at=str(plan.get("created_at")), repo=repo)
    except (PlanRefused, OSError, ValueError, KeyError) as exc:
        return problems + [f"plan cannot be reconstructed: {exc}"]
    if expected != dict(plan):
        problems.append("plan differs from the exact bound-artifact reconstruction")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--dry-plan", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    parser.add_argument("--stamp", required=True)
    args = parser.parse_args(argv)
    plan = build_plan(baseline_path=args.baseline, dry_plan_path=args.dry_plan, created_at=args.stamp)
    problems = validate_plan(plan)
    if problems:
        raise PlanRefused(f"refusing generated plan: {problems[:2]}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f"kg-stage3-b3-held-span-disposition-{args.stamp}.json"
    digest = write_immutable(path, plan)
    print(json.dumps({"status": "success", "path": str(path), "digest": digest,
                      "accounting": plan["accounting"]}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
