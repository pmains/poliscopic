#!/usr/bin/env python3
"""Propose a non-executable continuation and verify the batch schedule is unchanged.

Preparation only.  This builds a **proposal** - a distinct contract with its own kind,
its own `proposed` state, `enabled: false`, `executable: false`, and no approver field -
so it can never be mistaken for, or upgraded into, an authorization.  It is deliberately
not built through the authorization builder, and it carries no approver because none has
been supplied.

The batch schedule is *verified*, not rewritten: it is a pure function of the plan, the
proven cursor, and the batch size, none of which this correction changes, so its artifact
must stay byte-identical.
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
from scripts.kg import stage3_processing_receipt_apply_proposal as proposal  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

DEFAULT_PLAN = REPO / "data/kg-plans/kg-stage3-processing-dry-plan-20260921T193158Z.json"
DEFAULT_DESIGN = REPO / "data/kg-plans/kg-stage3-processing-receipt-store-packet-20260921T194940Z.json"
DEFAULT_BACKUP = REPO / "data/backups/kg-stage2-backup-receipt-20260921T184835Z.json"
DEFAULT_SCHEDULE = REPO / "data/kg-plans/kg-stage3-receipt-batch-schedule-20260921T195036Z.json"
BATCH_SIZE = 500
CURSOR = 6100
WRITER_ROLE = "poliscopic"
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
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args(argv)
    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    plan = load_verified(args.plan)
    design = load_verified(args.design)
    backup = load_verified(args.backup)
    code = apply.code_digest()

    record = proposal.build(
        plan=plan, design_packet=design, backup_receipt_path=str(args.backup.resolve()),
        backup_receipt_digest=str(backup["digest"]), code_digest=code,
        writer_role=WRITER_ROLE, batch_size=BATCH_SIZE)
    problems = proposal.validate_proposal(record, plan=plan, design_packet=design,
                                          current_code_digest=code)
    # Self-check: the authorization contract must refuse this by content alone.
    refused = authorization.proposal_problems(record)
    validated_as_authorization = authorization.validate(
        record, plan=plan, design_packet=design, current_code_digest=code)
    if problems or not refused or not validated_as_authorization:
        print(json.dumps({"outcome": "refused",
                          "problems": (problems or refused or
                                       ["the proposal was accepted as an authorization"])[:5]},
                         sort_keys=True))
        return 1
    record_path = args.out_dir / f"kg-stage3-processing-receipt-proposal-{stamp}.json"
    record_digest = write_immutable(record_path, record)

    stored = load_verified(args.schedule)
    rebuilt = build_schedule(plan, cursor=stored.get("cursor"), batch_size=stored["batch_size"])
    unchanged = {key: value for key, value in stored.items() if key != "digest"} == rebuilt

    print(json.dumps({
        "outcome": "proposed", "nothing_executed": True,
        "proposal": {"path": str(record_path), "digest": record_digest,
                     "kind": record["kind"], "state": record["state"],
                     "enabled": record["enabled"], "executable": record["executable"],
                     "approver_present": "approver" in record,
                     "plan_digest": record["plan_digest"],
                     "design_packet_digest": record["design_packet_digest"],
                     "backup_receipt_digest": record["backup_receipt_digest"],
                     "code_digest": record["code_digest"], "batch_size": record["batch_size"]},
        "refused_as_authorization": refused,
        "schedule": {"path": str(args.schedule), "digest": stored.get("digest"),
                     "unchanged": unchanged,
                     "batches": len(stored["batches"]), "cursor": stored.get("cursor")},
        "executed": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
