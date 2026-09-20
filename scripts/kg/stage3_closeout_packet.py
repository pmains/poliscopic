#!/usr/bin/env python3
"""Assemble the immutable Stage 3 closeout-readiness packet; never mutates data."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable  # noqa: E402

VERSION = "kg-stage3-closeout-readiness/1.0"
KIND = "kg-stage3-closeout-readiness"
CODE_MODULES = (
    "scripts/kg/stage3_closeout_packet.py",
    "scripts/kg/stage2_artifacts.py",
)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def code_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
            for name in CODE_MODULES}


def _load(path: str | Path, kind: str) -> dict[str, Any]:
    p = Path(path)
    if is_obsolete(p):
        raise ValueError(f"{p.name} is obsolete")
    value = load_verified(p)
    if value.get("kind") != kind:
        raise ValueError(f"{p.name} has wrong kind")
    return value


def build(*, b4_path: str | Path, eligibility_path: str | Path,
          processing_path: str | Path, b3_baseline_path: str | Path,
          created_at: str) -> dict[str, Any]:
    b4 = _load(b4_path, "kg-stage3-b4-quarantine-closeout")
    eligible = _load(eligibility_path, "kg-stage3-source-eligibility-baseline")
    processing = _load(processing_path, "kg-stage3-processing-identity-baseline")
    b3 = _load(b3_baseline_path, "kg-stage3-b3-span-baseline")
    targets = [x.get("target") for x in (b4, eligible, processing, b3)]
    if any(target != targets[0] for target in targets[1:]):
        raise ValueError("Stage 3 evidence artifacts bind different targets")
    if processing.get("eligibility_binding", {}).get("digest") != eligible.get("digest"):
        raise ValueError("processing baseline does not bind this eligibility baseline")

    doc_counts = eligible["accounting"]["by_source_kind"]["supporting_document"]
    total_docs = sum(int(v) for v in doc_counts.values())
    text_docs = int(doc_counts.get("eligible_current", 0)) + int(
        doc_counts.get("eligible_stale", 0))
    current_proven = int(processing["accounting"]["by_disposition"]["current_proven"])
    text_rate = text_docs / total_docs if total_docs else 1.0
    processing_rate = current_proven / text_docs if text_docs else 1.0
    b4_closed = b4.get("verdict") == "CLOSED_GOVERNED_EXCEPTION"
    document_exceptions = sum(
        1 for row in eligible.get("exceptions", [])
        if row.get("source_kind") == "supporting_document")
    exceptions_documented = (
        document_exceptions == total_docs - text_docs
        and document_exceptions > 0
        and bool(eligible.get("exceptions_sha256")))
    blockers = []
    if not b4_closed:
        blockers.append("B4 quarantine is not closed")
    if not exceptions_documented and text_rate < 0.99:
        blockers.append("document text coverage is below 99% without an exception ledger")
    if processing_rate < 1.0:
        blockers.append(
            f"{text_docs - current_proven} eligible text versions lack current extractor receipts")
    if int(b3["coverage"]["accepted"]) == 0:
        blockers.append("all B3 spans remain held outside canonical agenda-item containers")
    blockers.append("source-balanced benchmark thresholds are not yet approved or measured")
    body = {
        "kind": KIND, "version": VERSION, "created_at": created_at,
        "mode": "read-only", "applied": False, "write_path": "absent by design",
        "target": targets[0], "code_hashes": code_hashes(),
        "bindings": {
            "b4": {"path": str(b4_path), "digest": b4["digest"]},
            "eligibility": {"path": str(eligibility_path), "digest": eligible["digest"]},
            "processing": {"path": str(processing_path), "digest": processing["digest"]},
            "b3": {"path": str(b3_baseline_path), "digest": b3["digest"]},
        },
        "gates": {
            "b4_quarantine_closed": b4_closed,
            "document_text": {"total": total_docs, "with_supported_text": text_docs,
                              "rate": text_rate, "threshold": 0.99,
                              "document_exceptions": document_exceptions,
                              "exception_ledger_present": exceptions_documented,
                              "passes": text_rate >= 0.99 or exceptions_documented},
            "current_processing": {"eligible_text_versions": text_docs,
                                   "current_proven": current_proven,
                                   "rate": processing_rate, "threshold": 1.0,
                                   "passes": processing_rate == 1.0},
            "span_linkage": {"proposed": int(b3["coverage"]["proposed"]),
                             "accepted": int(b3["coverage"]["accepted"]),
                             "held": int(b3["coverage"]["held"]),
                             "governed": bool(b3["coverage"]["reconciles"])},
            "quality_benchmark": {"thresholds_approved": False, "measured": False,
                                  "passes": False},
        },
        "blockers": blockers, "verdict": "STAGE3_NOT_READY" if blockers else "STAGE3_COMPLETE",
        "mutations_proposed": 0,
    }
    return {**body, "digest": canonical_sha256(body)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b4", required=True)
    parser.add_argument("--eligibility", required=True)
    parser.add_argument("--processing", required=True)
    parser.add_argument("--b3", required=True)
    parser.add_argument("--out-dir", default="data/kg-plans")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    value = build(b4_path=args.b4, eligibility_path=args.eligibility,
                  processing_path=args.processing, b3_baseline_path=args.b3,
                  created_at=stamp)
    path = Path(args.out_dir) / f"kg-stage3-closeout-readiness-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = write_immutable(path, value)
    print(json.dumps({"status": "success", "path": str(path), "digest": digest,
                      "verdict": value["verdict"], "blockers": value["blockers"]},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
