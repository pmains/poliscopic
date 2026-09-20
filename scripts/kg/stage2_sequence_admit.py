#!/usr/bin/env python3
"""``stage2_sequence_admit.py`` — Phase A2: authoritative read-only admission.

The plan digests and code hashes are only half the precondition.  The decisive one
is that the plans still describe the **live** database: the affected scope must have
the same current-state digest, every bound hold document must be unchanged and still
unlinked, every witness span must still resolve, and every proposed natural key must
still be unoccupied.

That is exactly what :func:`stage2_s2_apply_runner.admit` checks, inside one
SERIALIZABLE transaction.  This script runs it and reports, and it does not write:
the write body is absent.
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
from scripts.kg import stage2_s2_apply_runner as AR  # noqa: E402

PLANS = REPO / "data" / "kg-plans"
BACKUP = REPO / "data" / "backups" / "kg-stage1-backup-receipt-20260914T003343Z.v2.json"


def _live(pattern: str) -> Path:
    hits = [p for p in sorted(PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (PLANS / (p.name + ".obsolete.json")).exists()]
    if len(hits) != 1:
        raise SystemExit(f"{pattern!r} matched {len(hits)}: {[h.name for h in hits]}")
    return hits[0]


def main() -> int:
    engine = get_engine()
    guard = assert_read_only_target(engine)
    print(f"target guard: {json.dumps(guard)}")
    # The config identity is REQUIRED: the tier lives there, and the target check
    # compares engine, config, plan and backup.  Omitting it is a refusal, not a
    # wildcard.
    config = {"dialect": guard["dialect"], "host": guard["host"],
              "port": guard["port"], "database": guard["database"],
              "tier": guard["tier"]}
    repair_path = _live("kg-stage2-s2-repair-plan-*.json")
    correction_path = _live("kg-stage2-s2-label-correction-plan-*.json")
    repair = AR.AuthorizedArtifact(repair_path.name,
                                   artifacts.recorded_digest(
                                       artifacts.load_verified(repair_path)))
    correction = AR.AuthorizedArtifact(
        correction_path.name,
        artifacts.recorded_digest(artifacts.load_verified(correction_path)))
    print(f"repair     : {repair.path} {repair.digest}")
    print(f"correction : {correction.path} {correction.digest}")
    try:
        admission = AR.admit(repair=repair, correction=correction, engine=engine,
                             backup_path=BACKUP, plan_dir=PLANS, config=config)
    except AR.ApplyRefused as exc:
        print(f"ADMISSION REFUSED: {exc}")
        return 2
    print("ADMISSION PASSED")
    print(f"  state sha256      : {admission['current_state_sha256']}")
    print(f"  live state        : {json.dumps(admission['live_state'])}")
    print(f"  unique index      : {json.dumps(admission['unique_index'])}")
    print(f"  backup            : {json.dumps(admission['backup'])}")
    print(f"  decisions         : {len(admission['decisions'])}")
    print(f"  writes            : {admission['writes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
