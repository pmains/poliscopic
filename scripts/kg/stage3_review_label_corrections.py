#!/usr/bin/env python3
"""Create and validate immutable corrections to the Stage 3 review ledger.

The original human-label file is evidence and is never edited. Corrections are
an additive overlay whose ``before`` value must exactly match the bound base
ledger. Consumers may apply the overlay only after validating that binding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.kg.stage2_artifacts import load_verified, write_immutable

BASE = REPO / "data/kg-review-labels/45280941fef959143ba7d754675d9ca0d27dfbd44cdb46be510bd5edbe1a9873.json"
OUTPUT = REPO / "data/kg-review-labels/stage3-meeting-event-corrections-v2.json"
SUPERSEDES = REPO / "data/kg-review-labels/stage3-meeting-event-corrections-v1.json"

CORRECTIONS = [{
    "case_id": "meeting_event_extraction:57989",
    "before": {
        "decision": "accept",
        "extraction": "tp",
        "notes": "Preliminary Approval",
    },
    "after": {
        "decision": "reject",
        "extraction": "fp",
        "notes": (
            "Correction: 'Preliminary Review of Preliminary Site Plan' is the "
            "item type; the result shown in the adjacent result column is "
            "Approval. The Preliminary Review candidate is not a meeting result."
        ),
    },
    "reason": "rubric_consistency_preliminary_review_title_vs_result",
    "evidence": {
        "source_id": 114366,
        "source_url": "https://www.phoenix.gov/content/dam/phoenix/cityclerksite/publicmeetings/results/2022/april/220426003r.pdf",
        "source_text": (
            "Preliminary Review of Preliminary Site Plan ... Approval PRELIM 2202132"
        ),
        "consistent_with_case_id": "meeting_event_extraction:40302",
    },
}, {
    "case_id": "meeting_event_extraction:45239",
    "before": {
        "decision": "reject",
        "extraction": "fp",
        "notes": "Approved",
    },
    "after": {
        "decision": "accept",
        "extraction": "tp",
        "notes": (
            "Correction: the cited action is the explicit result for item 6: "
            "'Continued to June 12, 2025 at 9:00 AM.' The Approved result in "
            "the excerpt belongs to the following item 7."
        ),
    },
    "reason": "review_alignment_following_item_action",
    "evidence": {
        "source_id": 111604,
        "source_url": "https://www.phoenix.gov/content/dam/phoenix/cityclerksite/publicmeetings/results/2025/may/250501004R.pdf",
        "item_number": "6",
        "source_text": "Continued 6. Application #: ZA-240-25-4 ... Continued to June 12, 2025 at 9:00 AM.",
        "following_item": "Approved 7. Application #: ZA-156-25-7",
    },
}, {
    "case_id": "meeting_event_extraction:45243",
    "before": {
        "decision": "reject",
        "extraction": "fp",
        "notes": "Approved",
    },
    "after": {
        "decision": "accept",
        "extraction": "tp",
        "notes": (
            "Correction: the cited action is the explicit result for item 9: "
            "'Continued to June 12, 2025 at 1:30 PM.' The Approved result in "
            "the excerpt belongs to the following item 10."
        ),
    },
    "reason": "review_alignment_following_item_action",
    "evidence": {
        "source_id": 111604,
        "source_url": "https://www.phoenix.gov/content/dam/phoenix/cityclerksite/publicmeetings/results/2025/may/250501004R.pdf",
        "item_number": "9",
        "source_text": "Continued 9. Application #: ZA-16-24-7 ... Continued to June 12, 2025 at 1:30 PM.",
        "following_item": "Approved 10. Application #: ZA-212-25-6",
    },
}]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_corrections(document: dict[str, Any], base_path: Path = BASE) -> list[str]:
    problems = []
    if document.get("kind") != "kg-stage3-review-label-corrections":
        problems.append("wrong correction kind")
    if document.get("base_labels_sha256") != file_sha256(base_path):
        problems.append("base label digest mismatch")
    base = json.loads(base_path.read_text(encoding="utf-8"))
    seen = set()
    for correction in document.get("corrections", []):
        case_id = correction.get("case_id")
        if not case_id or case_id in seen:
            problems.append(f"duplicate or absent case id: {case_id!r}")
            continue
        seen.add(case_id)
        label = base.get("labels", {}).get(case_id)
        if not label:
            problems.append(f"unknown case id: {case_id}")
            continue
        for key, value in correction.get("before", {}).items():
            if label.get(key) != value:
                problems.append(f"before value mismatch: {case_id}.{key}")
        after = correction.get("after", {})
        if after.get("decision") not in {"accept", "reject", "uncertain", "not_applicable"}:
            problems.append(f"invalid corrected decision: {case_id}")
        if after.get("extraction") not in {"tp", "fp", "fn", "not_applicable"}:
            problems.append(f"invalid corrected extraction: {case_id}")
        if not correction.get("reason") or not correction.get("evidence"):
            problems.append(f"correction lacks reason/evidence: {case_id}")
    return problems


def build() -> dict[str, Any]:
    return {
        "kind": "kg-stage3-review-label-corrections",
        "version": "2.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_labels_path": str(BASE),
        "base_labels_sha256": file_sha256(BASE),
        "supersedes": (
            {"path": str(SUPERSEDES), "sha256": file_sha256(SUPERSEDES)}
            if SUPERSEDES.exists() else None
        ),
        "corrections": CORRECTIONS,
        "applied_to_base_file": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args()
    if args.validate:
        document = load_verified(args.validate)
        problems = validate_corrections(document)
        print(json.dumps({"valid": not problems, "problems": problems}, sort_keys=True))
        return int(bool(problems))
    document = build()
    problems = validate_corrections(document)
    if problems:
        raise SystemExit("; ".join(problems))
    if not args.write:
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    digest = write_immutable(OUTPUT, document)
    print(json.dumps({"path": str(OUTPUT), "digest": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
