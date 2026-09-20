#!/usr/bin/env python3
"""``stage2_reservation_apply_run.py`` — validate, then apply the reservation plan.

Reads the plan from disk, re-validates it independently, checks the target and the
protected backup, and applies the additive schema in one transaction.  Applies
**only** the reservation plan: the correction, repair and containment plans are not
reachable from here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target,
)
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_reservation_apply as applying  # noqa: E402
from scripts.kg import stage2_reservation_plan as planning  # noqa: E402

PLANS = REPO / "data" / "kg-plans"
BACKUP = REPO / "data" / "backups" / "kg-stage1-backup-receipt-20260914T003343Z.v2.json"


def main() -> int:
    engine = get_engine()
    guard = assert_read_only_target(engine)
    print(f"target guard: {json.dumps(guard)}")
    hits = [p for p in sorted(PLANS.glob("kg-stage2-reservation-schema-plan-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (PLANS / (p.name + ".obsolete.json")).exists()]
    if len(hits) != 1:
        raise SystemExit(f"expected one live plan, got {[h.name for h in hits]}")
    path = hits[0]
    plan = artifacts.load_verified(path)
    digest = artifacts.recorded_digest(plan)
    print(f"plan: {path.name}")
    print(f"  digest {digest}")

    # Independent validation, from disk, before anything is applied.
    problems = planning.validate_plan(plan)
    print(f"independent validation: {problems if problems else 'CLEAN'}")
    if problems:
        print("STOPPING before mutation.")
        return 2

    backup = applying.require_backup(BACKUP, target=plan["target"])
    print(f"backup accepted: {json.dumps(backup)}")

    result = applying.apply_plan(engine, plan, supplied_digest=digest,
                                 backup_receipt=BACKUP, out_dir=PLANS)
    print("APPLY RESULT")
    for key in ("stage", "writes", "replay", "receipt_path", "receipt_digest",
                "statements", "protected_row_counts_before",
                "protected_row_counts_after", "postconditions"):
        if key in result:
            print(f"  {key}: {json.dumps(result[key])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
