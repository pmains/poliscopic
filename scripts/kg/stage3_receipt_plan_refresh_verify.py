#!/usr/bin/env python3
"""Verify a refreshed receipt-bound Stage 3 dry plan and report its accounting.

Read only.  This answers the review questions an operator asks before any write
authorization: what the live receipt state is, what the refreshed plan selects, how
every selected row is accounted for, whether the stored receipts are all replay
no-ops, and exactly which identities moved relative to a prior plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.kg import stage3_processing_receipt as receipt  # noqa: E402
from scripts.kg import stage3_processing_receipt_set_export as export  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified  # noqa: E402

DEFAULT_NEW = REPO / "data/kg-plans/kg-stage3-processing-dry-plan-20260921T193158Z.json"
DEFAULT_OLD = REPO / "data/kg-plans/kg-stage3-processing-dry-plan-20260920T174924Z.json"
DEFAULT_SET = REPO / "data/kg-plans/kg-stage3-processing-receipt-set-20260921T192601Z.json"


def selected_rows(plan: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """The plan's own selected rows, found by shape rather than by a guessed key."""
    for value in plan.values():
        if isinstance(value, list) and value and isinstance(value[0], Mapping) \
                and "processing_identity" in value[0]:
            return value
    raise ValueError("plan exposes no selected rows carrying a processing identity")


def identity_of(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(part) for part in row["processing_identity"])


def verify(new_plan: Path, old_plan: Path, receipt_set: Path) -> dict[str, Any]:
    new = load_verified(new_plan)
    old = load_verified(old_plan)
    set_document = load_verified(receipt_set)
    bodies = list(set_document.get("receipts") or [])

    rows = selected_rows(new)
    outcomes = Counter(str(row.get("outcome")) for row in rows)
    new_identities = {identity_of(row): str(row.get("outcome")) for row in rows}
    old_identities = {identity_of(row): str(row.get("outcome")) for row in selected_rows(old)}

    stored: dict[tuple[str, ...], list[str]] = {}
    for body in bodies:
        stored.setdefault(tuple(str(part) for part in body["processing_identity"]), []).append(
            str(body.get("status")))

    replay_noops = sorted(key for key in stored if new_identities.get(key) == "replay")
    stored_not_replay = {"/".join(key[:2]): new_identities.get(key, "absent")
                         for key in stored if new_identities.get(key) != "replay"}

    removed = sorted(set(old_identities) - set(new_identities))
    added = sorted(set(new_identities) - set(old_identities))
    changed = sorted(key for key in set(old_identities) & set(new_identities)
                     if old_identities[key] != new_identities[key])

    return {
        "plan": {"path": str(new_plan), "digest": new.get("digest"),
                 "kind": new.get("kind"), "version": new.get("version"),
                 "producer_version": new.get("producer_version"),
                 "applied": new.get("applied"),
                 "writes_performed": new.get("writes_performed"),
                 "write_path": new.get("write_path")},
        "bound": dict(new.get("bound") or {}),
        "recorded_accounting": dict(new.get("accounting") or {}),
        "selected_row_outcomes": dict(sorted(outcomes.items())),
        "selected_rows": len(rows),
        "counts": {
            "replay_noop": outcomes.get("replay", 0),
            "planned_remaining": outcomes.get("planned", 0),
            "held": outcomes.get("held", 0),
            "retry_eligible_failure": outcomes.get("failure", 0),
        },
        "receipt_set": {"path": str(receipt_set), "digest": set_document.get("digest"),
                        "count": set_document.get("count"),
                        "identity_sha256": set_document.get("identity_sha256"),
                        "stored_identities": len(stored)},
        "all_stored_receipts_are_replay_noops": len(stored_not_replay) == 0,
        "stored_not_replaying": stored_not_replay,
        "replay_noop_count_matches_receipt_set": len(replay_noops) == len(stored),
        "versus_previous_plan": {
            "previous_path": str(old_plan), "previous_digest": old.get("digest"),
            "previous_selected": len(old_identities),
            "added_identities": len(added),
            "removed_identities": len(removed),
            "outcome_changed_identities": len(changed),
            "added_source_ids": sorted({int(key[1]) for key in added})[:25],
            "removed_source_ids": sorted({int(key[1]) for key in removed})[:25],
        },
        "replay": dict(new.get("replay") or {}),
        "evidence_binding": dict(new.get("evidence_binding") or {}),
        "receipts_binding": dict(new.get("receipts_binding") or {}),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new-plan", type=Path, default=DEFAULT_NEW)
    parser.add_argument("--old-plan", type=Path, default=DEFAULT_OLD)
    parser.add_argument("--receipt-set", type=Path, default=DEFAULT_SET)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = verify(args.new_plan, args.old_plan, args.receipt_set)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
