#!/usr/bin/env python3
"""``stage2_subitem_regen.py`` — regenerate the containment baseline and plan.

P1 remediation regenerated both artifacts so the plan binds:

* a manifest whose **code hashes include the plan builder itself** and whose
  **semantic registry hashes** the validator recomputes from disk;
* a **focused ``agenda_items`` schema signature** (columns, types, nullability,
  defaults, primary key, indexes, foreign keys) plus the **exact target
  identity**, compared against the authoritative current schema for exact
  equality;
* an explicit **baseline projection** proof, with the collision population and the
  operation set rederived from the canonically loaded baseline.

Nothing here writes to the database.  Every read is SELECT-only, and every
artifact is written immutably (``O_CREAT|O_EXCL``); the superseded pair is
archived by sidecar, never deleted.
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

from sqlalchemy import text  # noqa: E402

from scripts.db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import assert_read_only_target  # noqa: E402
from scripts.entities.schema_parity import schema_signature  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_subitem_containment as containment  # noqa: E402
from scripts.kg import stage2_subitem_manifest as manifest_mod  # noqa: E402
from scripts.kg import stage2_subitem_plan as plan_mod  # noqa: E402
from scripts.kg import stage2_subitem_schema as schema_mod  # noqa: E402

PLANS = REPO / "data" / "kg-plans"
ITEMS = ("SELECT id, meeting_db_id, body, agenda_item_number FROM agenda_items "
         "ORDER BY id")


def _target(engine) -> dict:
    from scripts.db import config

    url = engine.url
    return {"dialect": url.drivername, "host": url.host, "port": url.port,
            "database": url.database, "tier": config.DB_TIER}


def _supersede(kind: str, keep: str, reason: str) -> list[str]:
    archived = []
    for candidate in sorted(PLANS.glob(f"kg-stage2-subitem-containment-{kind}-*.json")):
        if candidate.name.endswith(".obsolete.json") or candidate.name == keep:
            continue
        if (PLANS / (candidate.name + ".obsolete.json")).exists():
            continue
        artifacts.record_obsolete(PLANS, candidate, reason)
        archived.append(candidate.name)
    return archived


def main() -> int:
    engine = get_engine()
    guard = assert_read_only_target(engine)
    target = _target(engine)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"guard: {json.dumps(guard)}")
    print(f"target: {json.dumps(target)}")

    with engine.connect() as connection:
        items = [dict(r) for r in connection.execute(text(ITEMS)).mappings()]
        authoritative = schema_mod.read_schema_signature(connection, schema_mod.TABLE)
        full_signature = schema_signature(connection)
    print(f"agenda_items rows: {len(items)}")
    print(f"authoritative agenda_items signature: {authoritative['digest']} "
          f"({len(authoritative['columns'])} columns, pk {authoritative['primary_key']}, "
          f"{len(authoritative['indexes'])} indexes, "
          f"{len(authoritative['foreign_keys'])} foreign keys)")

    baseline = containment.audit(items)
    baseline["created_at"] = datetime.now(timezone.utc).isoformat()
    baseline["rows_sha256"] = manifest_mod.canonical_sha256(baseline["rows"])
    baseline_path = PLANS / f"kg-stage2-subitem-containment-baseline-{stamp}.json"
    artifacts.write_immutable(baseline_path, baseline)
    # Build from the ARTIFACT, not the in-memory dict: writing stamps the artifact
    # with its own digest, which the plan then binds and a validator rechecks.
    loaded_baseline = artifacts.load_verified(baseline_path)
    baseline_digest = artifacts.recorded_digest(loaded_baseline)
    print(f"baseline: {baseline_path.name} digest={baseline_digest}")
    print(f"  counts: {json.dumps(loaded_baseline['counts'])}")
    collisions = loaded_baseline["collision_population"]
    print(f"  collisions: keys={collisions['distinct_keys']} "
          f"rows={collisions['involved_rows']} excess={collisions['excess']}")

    plan = plan_mod.build_plan(
        loaded_baseline, baseline_path=baseline_path,
        created_at=loaded_baseline["created_at"],
        target=target, schema_signature=full_signature,
        agenda_items_schema=authoritative, column_present=False)
    plan_path = PLANS / f"kg-stage2-subitem-containment-plan-{plan['digest'][:16]}.json"
    artifacts.write_immutable(plan_path, plan)
    print(f"plan: {plan_path.name} digest={plan['digest']} "
          f"replay={plan['replay_digest']}")
    print(f"  operations: {len(plan['operations'])} | manifest code modules: "
          f"{len(plan['manifest']['code_hashes'])} | registries: "
          f"{len(plan['manifest']['semantic_dependencies'])}")

    # Re-validate from disk, with the authoritative schema supplied.
    reloaded = artifacts.load_verified(plan_path)
    problems = plan_mod.validate_plan(
        reloaded, baseline_path=baseline_path, plan_dir=PLANS,
        authoritative_schema=authoritative, authoritative_target=target)
    print(f"revalidation from disk: {problems if problems else 'CLEAN'}")
    if problems:
        raise SystemExit("; ".join(problems[:5]))

    for old in _supersede("baseline", baseline_path.name,
                          "superseded by the P1-corrected baseline and plan: manifest "
                          "code hashes covering the plan builder, recomputed semantic "
                          "registry hashes, focused agenda_items schema signature, and "
                          "an explicit exact-projection proof."):
        print(f"obsolete baseline: {old}")
    for old in _supersede("plan", plan_path.name,
                          "superseded by the P1-corrected plan: containment manifest "
                          "with the plan builder bound, authoritative agenda_items "
                          "schema equality, exact baseline projection, and rederived "
                          "collisions and operations."):
        print(f"obsolete plan: {old}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
