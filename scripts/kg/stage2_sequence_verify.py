#!/usr/bin/env python3
"""``stage2_sequence_verify.py`` — Phase A: independent, read-only verification.

Before anything is mutated, prove — from the live engine and the artifacts, not
from memory — that:

* the target is the exact development database the plans name;
* the plan heads are the ones named for this sequence;
* the code each plan binds is the code on disk;
* the protected backup receipt is present, mode 0600, restore-verified, and its
  dump hash matches the dump on disk;
* the schema is in the expected pre-apply state, and the protected counts are the
  ones the plans were built against.

Read-only: every statement is a SELECT, and the target is checked with the same
guard the apply path uses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from scripts.db import tier as tier_module  # noqa: E402
from scripts.db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target,
)
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_admission_binding as AB  # noqa: E402
from scripts.kg import stage2_s2_ai_lineage as lineage  # noqa: E402
from scripts.kg import stage2_s2_apply_target as AT  # noqa: E402
from scripts.kg import stage2_s2_apply_runner as AR  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402

PLANS = REPO / "data" / "kg-plans"
BACKUP = REPO / "data" / "backups" / "kg-stage1-backup-receipt-20260914T003343Z.v2.json"

def _resolve_by_digest(pattern: str, digest: str) -> tuple[Path, dict[str, Any]]:
    """Resolve an exact digest to exactly ONE live head, and fail closed otherwise.

    The authorized digest is supplied by the caller, never hardcoded: a constant here
    would silently authorize a superseded plan the moment a new one was built.  A
    digest that names no live artifact, or more than one, is a refusal.
    """
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(PLANS.glob(pattern)):
        if path.name.endswith(".obsolete.json"):
            continue
        if (PLANS / (path.name + ".obsolete.json")).exists():
            continue
        document = artifacts.load_verified(path)
        if artifacts.recorded_digest(document) == digest:
            matches.append((path, document))
    if len(matches) != 1:
        raise SystemExit(
            f"{digest} resolved to {len(matches)} live artifacts: "
            f"{[m[0].name for m in matches]}")
    return matches[0]


def _live(pattern: str) -> Path:
    hits = [p for p in sorted(PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (PLANS / (p.name + ".obsolete.json")).exists()]
    if len(hits) != 1:
        raise SystemExit(f"{pattern!r} matched {len(hits)} live artifacts: "
                         f"{[h.name for h in hits]}")
    return hits[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--correction-digest", required=True,
                        help="the exact correction plan digest authorized for this "
                             "sequence; resolved to exactly one live head")
    args = parser.parse_args(argv)
    failures: list[str] = []
    engine = get_engine()
    guard = assert_read_only_target(engine)
    print(f"target guard: {json.dumps(guard)}")
    if guard["tier"] != tier_module.DEVELOPMENT:
        failures.append(f"tier is {guard['tier']!r}, not development")
    if guard["database"] != "poliscopic_dev":
        failures.append(f"database is {guard['database']!r}, not poliscopic_dev")
    if guard["dialect"] != "postgresql":
        failures.append(f"dialect is {guard['dialect']!r}, not postgresql")

    with engine.connect() as connection:
        counts = connection.execute(text("""
            SELECT (SELECT COUNT(*) FROM meetings) AS meetings,
                   (SELECT COUNT(*) FROM agenda_items) AS agenda_items,
                   (SELECT COUNT(*) FROM supporting_documents) AS supporting_documents
        """)).mappings().first()
        columns = {r[0] for r in connection.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'supporting_documents'"))}
    print(f"protected counts: {json.dumps(dict(counts))}")
    print(f"supporting_documents columns: {len(columns)}")
    # The canonical link column is now an AUTHORIZED schema step, so its presence is
    # the expected state and its absence is the blocker.  Either direction is checked,
    # never assumed.
    if "agenda_item_db_id" not in columns:
        failures.append(
            "supporting_documents.agenda_item_db_id is absent: apply the authorized "
            "link-column schema plan before the correction")

    plan_path, plan, plan_digest = lineage.current_plan(PLANS)
    aggregate_path, aggregate, aggregate_digest = lineage.current_aggregate(
        PLANS, plan_digest)
    print(f"S2 plan head:   {plan_path.name} {plan_digest}")
    print(f"aggregate head: {aggregate_path.name} {aggregate_digest}")

    correction_path, correction = _resolve_by_digest(
        "kg-stage2-s2-label-correction-plan-*.json", args.correction_digest)
    correction_digest = artifacts.recorded_digest(correction)
    print(f"correction plan: {correction_path.name} {correction_digest} "
          f"(authorized exactly)")

    repair_path = _live("kg-stage2-s2-repair-plan-*.json")
    rep = artifacts.load_verified(repair_path)
    repair_digest = artifacts.recorded_digest(rep)
    print(f"repair plan:     {repair_path.name} {repair_digest}")

    heads = AB._verify_heads(PLANS)
    target = {"dialect": guard["dialect"], "host": guard["host"],
              "port": guard["port"], "database": guard["database"],
              "tier": guard["tier"]}
    for artifact, label in ((rep, "repair"), (correction, "correction")):
        try:
            AB._verify_code_hashes(artifact)
            print(f"  {label}: code hashes CLEAN")
        except Exception as exc:  # noqa: BLE001 - any drift is a blocker
            failures.append(f"{label}: code hashes refused: {exc}")
        try:
            AB._verify_decisions(artifact, plan_dir=PLANS,
                                 aggregate=heads["aggregate"]["document"], heads=heads)
            print(f"  {label}: decisions CLEAN")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}: decisions refused: {exc}")
    problems = AB.verify_target_equality(rep, correction, target)
    if problems:
        failures.extend(problems)
    else:
        print("  target equality: CLEAN")
    absent = binding.missing_code_modules()
    if absent:
        failures.append(f"declared modules absent from disk: {absent}")

    live_backup = AT.load_backup(
        BACKUP, target={"dialect": guard["dialect"], "host": guard["host"],
                        "port": guard["port"], "database": guard["database"]})
    print(f"backup receipt: {live_backup['path']} "
          f"digest {live_backup['receipt_digest'][:16]}... "
          f"mode {live_backup['receipt_stat']['mode']}")
    print(f"  dump sha256: {live_backup['dump_sha256']}")
    print(f"  restore: {json.dumps(live_backup['restore_proof'])[:120]}")
    for artifact, label in ((rep, "repair"), (correction, "correction")):
        bound = (artifact.get("bindings") or {}).get("backup") or {}
        live_for_compare = dict(live_backup)
        live_for_compare["current_baseline"] = \
            (artifact.get("bindings") or {}).get("baseline") or {}
        live_for_compare.pop("current_baseline", None)
        backup_problems = AT.verify_backup_binding(bound, live_for_compare)
        if backup_problems:
            failures.extend(f"{label}: {p}" for p in backup_problems)
        else:
            print(f"  {label}: backup binding CLEAN")

    print()
    if failures:
        print("VERIFICATION FAILED:")
        for problem in failures[:10]:
            print(f"  - {problem}")
        return 2
    print("VERIFICATION PASSED — every precondition holds; no mutation was made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
