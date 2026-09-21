#!/usr/bin/env python3
"""Propose a non-applying continuation packet and a deterministic batch schedule.

Preparation only.  This builds the authorization-shaped artifact for the refreshed
receipt-bound plan, a deterministic batch schedule from the proven continuation
cursor, and writes both immutably.  It executes nothing: the design packet stays
disabled, the store's ``ENABLED`` stays false, and no database connection is opened.

The proposal deliberately does **not** invent an approver.  The contract requires the
field to be non-empty and asserts an authorized state, so the value here states its own
status instead of recording an approval nobody has given.  The artifact is also written
beside the plans rather than into the runner's terminal directory, whose
``kg-stage3-processing-receipt-apply-*.json`` glob would otherwise adopt it as a terminal
receipt.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.kg import stage3_processing_receipt_apply as apply  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply_packet as authorization  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

DEFAULT_PLAN = REPO / "data/kg-plans/kg-stage3-processing-dry-plan-20260921T193158Z.json"
DEFAULT_DESIGN = REPO / "data/kg-plans/kg-stage3-processing-receipt-store-packet-20260921T194940Z.json"
DEFAULT_BACKUP = REPO / "data/backups/kg-stage2-backup-receipt-20260921T184835Z.json"
BATCH_SIZE = 500
CURSOR = 6100
PROPOSAL_APPROVER = "PENDING MANAGER REVIEW (NOT AN AUTHORIZATION)"
CHECKPOINT_DIR = "data/kg-receipts"
TERMINAL_DIR = "data/kg-receipts"
PREFLIGHT_DIR = "data/kg-plans"


def selected_rows(plan: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    for value in plan.values():
        if isinstance(value, list) and value and isinstance(value[0], Mapping) \
                and "processing_identity" in value[0]:
            return value
    raise ValueError("plan exposes no selected rows carrying a processing identity")


def build_schedule(plan: Mapping[str, Any], *, cursor: int, batch_size: int) -> dict[str, Any]:
    """Deterministic positional windows from the proven cursor, with exact accounting."""
    rows = selected_rows(plan)
    outcomes = [str(row.get("outcome")) for row in rows]
    batches: list[dict[str, Any]] = []
    for index, start in enumerate(range(cursor, len(rows), batch_size)):
        end = min(start + batch_size, len(rows))
        window = Counter(outcomes[start:end])
        batches.append({
            "batch": index,
            "start_offset": start,
            "end_offset": end,
            "rows": end - start,
            "expected_outcomes": dict(sorted(window.items())),
            "expected_writes": window.get("planned", 0),
            "held_in_window": window.get("held", 0),
            "replay_in_window": window.get("replay", 0),
            "first_identity": [str(part) for part in rows[start]["processing_identity"]],
            "last_identity": [str(part) for part in rows[end - 1]["processing_identity"]],
        })
    totals = Counter(outcomes[cursor:])
    return {
        "kind": "kg-stage3-receipt-batch-schedule",
        "version": "1.0",
        "plan_digest": plan["digest"],
        "cursor": cursor,
        "batch_size": batch_size,
        "stop_on_first_failure": True,
        "held_identities_are_never_written": True,
        "checkpoint_dir": CHECKPOINT_DIR,
        "terminal_dir": TERMINAL_DIR,
        "preflight_dir": PREFLIGHT_DIR,
        "per_batch_artifact_naming": "the runner owns filenames: "
            "kg-stage3-processing-receipt-checkpoint-*.json in the checkpoint dir and "
            "kg-stage3-processing-receipt-apply-*.json in the terminal dir",
        "batches": batches,
        "totals": {"rows_after_cursor": len(rows) - cursor,
                   "expected_outcomes": dict(sorted(totals.items())),
                   "expected_writes": totals.get("planned", 0)},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--design", type=Path, default=DEFAULT_DESIGN)
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args(argv)
    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    plan = load_verified(args.plan)
    design = load_verified(args.design)
    backup = load_verified(args.backup)
    code = apply.code_digest()

    packet = authorization.build(
        plan=plan, design_packet=design, backup_receipt_path=str(args.backup.resolve()),
        backup_receipt_digest=str(backup["digest"]), code_digest=code,
        approver=PROPOSAL_APPROVER, writer_role="poliscopic", batch_size=BATCH_SIZE)
    problems = authorization.validate(packet, plan=plan, design_packet=design,
                                      current_code_digest=code)
    if problems:
        print(json.dumps({"outcome": "refused", "problems": problems[:5]}, sort_keys=True))
        return 1
    packet_path = args.out_dir / f"kg-stage3-processing-receipt-proposal-{stamp}.json"
    packet_digest = write_immutable(packet_path, packet)

    schedule = build_schedule(plan, cursor=CURSOR, batch_size=BATCH_SIZE)
    schedule_path = args.out_dir / f"kg-stage3-receipt-batch-schedule-{stamp}.json"
    schedule_digest = write_immutable(schedule_path, schedule)

    print(json.dumps({
        "outcome": "proposed", "nothing_executed": True,
        "proposal": {"path": str(packet_path), "digest": packet_digest,
                     "plan_digest": packet["plan_digest"],
                     "design_packet_digest": packet["design_packet_digest"],
                     "backup_receipt_digest": packet["backup_receipt_digest"],
                     "code_digest": packet["code_digest"],
                     "approver_status": PROPOSAL_APPROVER,
                     "batch_size": packet["batch_size"]},
        "schedule": {"path": str(schedule_path), "digest": schedule_digest,
                     "cursor": schedule["cursor"], "batches": len(schedule["batches"]),
                     "totals": schedule["totals"]},
        "store_enabled": False,
        "design_packet_enabled": design.get("enabled"),
        "apply_execution_enabled": apply.EXECUTION_ENABLED,
        "executed": False},
        indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
