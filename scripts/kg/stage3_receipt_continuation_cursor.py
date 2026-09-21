#!/usr/bin/env python3
"""Derive the safe continuation cursor for a receipt-bound Stage 3 dry plan.

Offset-based continuation is only sound when the identities that already carry a
stored receipt occupy a contiguous prefix of the plan's own order.  This proves or
refutes that from the stored receipt identities themselves, and never infers a
cursor from a row count.

Read only: it reads three verified artifacts and writes nothing to any database.
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

from scripts.kg.stage2_artifacts import load_verified  # noqa: E402

DEFAULT_PLAN = REPO / "data/kg-plans/kg-stage3-processing-dry-plan-20260921T193158Z.json"
DEFAULT_SET = REPO / "data/kg-plans/kg-stage3-processing-receipt-set-20260921T192601Z.json"


def selected_rows(plan: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    for value in plan.values():
        if isinstance(value, list) and value and isinstance(value[0], Mapping) \
                and "processing_identity" in value[0]:
            return value
    raise ValueError("plan exposes no selected rows carrying a processing identity")


def identity_of(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(part) for part in row["processing_identity"])


def analyse(plan_path: Path, receipt_set_path: Path) -> dict[str, Any]:
    plan = load_verified(plan_path)
    receipt_set = load_verified(receipt_set_path)
    rows = selected_rows(plan)
    stored = {tuple(str(part) for part in body["processing_identity"])
              for body in receipt_set.get("receipts") or []}

    order = (plan.get("bound") or {}).get("order")
    source_ids = [int(identity_of(row)[1]) for row in rows]
    ordered = all(a < b for a, b in zip(source_ids, source_ids[1:]))
    outcomes = [str(row.get("outcome")) for row in rows]

    positions = {outcome: [] for outcome in sorted(set(outcomes))}
    for index, outcome in enumerate(outcomes):
        positions[outcome].append(index)

    replay_positions = positions.get("replay", [])
    planned_positions = positions.get("planned", [])
    first_planned_index = planned_positions[0] if planned_positions else None
    last_replay_index = replay_positions[-1] if replay_positions else None
    interleaved = [index for index in planned_positions
                   if last_replay_index is not None and index < last_replay_index]
    window_end = first_planned_index if first_planned_index is not None else len(rows)
    window_outcomes = Counter(outcomes[:window_end])
    window_non_replay = [{"index": index, "source_id": source_ids[index],
                          "outcome": outcomes[index]}
                         for index in range(window_end) if outcomes[index] != "replay"]
    max_replay_source_id = (max(int(identity_of(rows[i])[1]) for i in replay_positions)
                            if replay_positions else None)
    min_planned_source_id = (min(int(identity_of(rows[i])[1]) for i in planned_positions)
                             if planned_positions else None)
    outcome_by_identity = {identity_of(row): str(row.get("outcome")) for row in rows}
    stored_without_replay = sorted(
        "/".join(identity[:2]) for identity in stored
        if outcome_by_identity.get(identity) != "replay")

    return {
        "plan": {"path": str(plan_path), "digest": plan.get("digest"),
                 "bound_order": order, "selected": len(rows),
                 "source_id_strictly_ascending": ordered},
        "receipt_set": {"path": str(receipt_set_path), "digest": receipt_set.get("digest"),
                        "count": receipt_set.get("count"),
                        "identity_sha256": receipt_set.get("identity_sha256")},
        "outcome_counts": dict(sorted(Counter(outcomes).items())),
        "replay_positions_are_exactly_zero_to_n": replay_positions == list(range(len(replay_positions))),
        "replay_identities_precede_every_planned_identity":
            (max_replay_source_id is not None and min_planned_source_id is not None
             and max_replay_source_id < min_planned_source_id),
        "first_planned_index": first_planned_index,
        "first_planned_source_id": (int(identity_of(rows[first_planned_index])[1])
                                    if first_planned_index is not None else None),
        "first_planned_identity": (list(identity_of(rows[first_planned_index]))
                                   if first_planned_index is not None else None),
        "last_replay_index": last_replay_index,
        "planned_positions_before_first_planned": len(interleaved),
        "consumed_window": {"start": 0, "end": window_end, "size": window_end,
                            "outcomes": dict(sorted(window_outcomes.items())),
                            "non_replay_rows": window_non_replay},
        "safe_cursor_index": first_planned_index,
        "safe_cursor_is_proven": len(interleaved) == 0 and window_end == (first_planned_index or 0),
        "max_replay_source_id": max_replay_source_id,
        "min_planned_source_id": min_planned_source_id,
        "stored_not_replay": stored_without_replay,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--receipt-set", type=Path, default=DEFAULT_SET)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = analyse(args.plan, args.receipt_set)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
