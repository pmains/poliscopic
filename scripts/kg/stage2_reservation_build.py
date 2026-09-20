#!/usr/bin/env python3
"""``stage2_reservation_build.py`` — generate the reservation schema plan.

Read-only: it reads the live schema and the justifying artifacts and writes one
immutable dry-run plan.  Applying it is a separate, reviewed step.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
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
from scripts.kg import stage2_reservation_plan as planning  # noqa: E402

PLANS = REPO / "data" / "kg-plans"


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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"target guard: {json.dumps(guard)}")
    target = {k: guard[k] for k in ("dialect", "host", "port", "database", "tier")}
    investigation = _live("kg-stage2-duplicate-key-investigation-*.json")
    contract = _live("kg-stage2-collision-control-contract-*.json")
    with engine.connect() as connection:
        plan = planning.build_plan(
            connection, target=target, investigation_path=investigation,
            contract_path=contract, created_at=datetime.now(timezone.utc).isoformat())
    path = PLANS / f"kg-stage2-reservation-schema-plan-{plan['digest'][:16]}.json"
    artifacts.write_immutable(path, plan)
    print(f"plan: {path.name}")
    print(f"  digest        {plan['digest']}")
    print(f"  replay_digest {plan['replay_digest']}")
    print(f"  ddl           {plan['ddl'][0][:100]}...")
    print(f"  binds investigation {plan['bindings']['duplicate_investigation']['path']}")
    print(f"  binds contract      {plan['bindings']['collision_contract']['path']}")
    print(f"  code modules        {len(plan['bindings']['code_hashes'])}")
    for earlier in sorted(PLANS.glob("kg-stage2-reservation-schema-plan-*.json")):
        if earlier.name == path.name or earlier.name.endswith(".obsolete.json"):
            continue
        if (PLANS / (earlier.name + ".obsolete.json")).exists():
            continue
        artifacts.record_obsolete(PLANS, earlier, "superseded by a later plan build")
        print(f"  obsolete: {earlier.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
