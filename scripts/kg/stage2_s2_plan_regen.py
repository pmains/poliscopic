#!/usr/bin/env python3
"""``stage2_s2_plan_regen.py`` — regenerate the two S2 repair/correction plans.

P1 remediation regenerated both plans so they bind:

* a **per-document row fingerprint** for every hold document, not only a
  population digest;
* the **complete backup binding** — receipt path, digest, live mode and ownership,
  the dump's path, hash, mode and ownership, the restore evidence, the source
  counts and schema fingerprints, and the plan's own current baseline — so the
  runner can compare every field for exact equality;
* a **current-state digest recomputed against the live database** through the same
  capture the admission uses.

The current-state digest depends on the plans' *structure*, not on their bound
digests, so the build is two-pass: build, capture, rebuild once more.  The second
pass is asserted to be stable.

Nothing here writes to the database.  Every read is SELECT-only, and the plans are
written immutably (``O_CREAT|O_EXCL``); the superseded pair is archived by
sidecar, never deleted.
"""

from __future__ import annotations

import argparse

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from scripts.db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import assert_read_only_target  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_ai_lineage as lineage  # noqa: E402
from scripts.kg import stage2_s2_current_state as current_state  # noqa: E402
from scripts.kg import stage2_s2_label_correction as correction_mod  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402
from scripts.kg import stage2_reservation as reservation  # noqa: E402
from scripts.kg import stage2_s2_repair_plan as repair_mod  # noqa: E402

PLANS = REPO / "data" / "kg-plans"

#: The document rows the plans were built from: every field the witness
#: fingerprint covers, plus the retained text and the recorded item number.
DOCUMENT_QUERY = (
    f"SELECT {current_state.WITNESS_ROW_SQL}, COALESCE(text_content, '') AS text_content "
    "FROM supporting_documents WHERE id = ANY(:ids)"
)

ITEM_QUERY = """
    SELECT meeting_db_id, agenda_item_number FROM agenda_items
    WHERE meeting_db_id = ANY(:meetings)
"""


def _collision_control(connection: Any) -> dict[str, Any]:
    """The GOVERNED collision control, read from the applied reservation table."""
    problems = reservation.verify_reservation_contract(connection)
    if problems:
        raise SystemExit("the reservation contract is not satisfied: "
                         + "; ".join(problems[:4]))
    signature = reservation.read_reservation_signature(connection)
    return {"mode": "exact_key_reservation", "table": reservation.RESERVATION_TABLE,
            "primary_key": signature["primary_key"],
            "invariant_carrier": "primary key", "advisory_locking": True,
            "scope": "governed writers only", "historical_key_is_unique": False,
            "signature_digest": binding.canonical_sha256(signature)}


def _reservation_receipt() -> dict[str, Any]:
    hits = [p for p in sorted(PLANS.glob("kg-stage2-reservation-schema-receipt-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (PLANS / (p.name + ".obsolete.json")).exists()]
    if len(hits) != 1:
        raise SystemExit(f"expected one reservation receipt, got {[h.name for h in hits]}")
    document = artifacts.load_verified(hits[0])
    return {"path": hits[0].name, "digest": artifacts.recorded_digest(document),
            "plan_digest": document.get("plan_digest"),
            "applied_at": document.get("applied_at")}


def _occupied_keys(connection: Any, meetings: list[int]) -> list[str]:
    """Every natural key a live agenda item already holds, in the affected meetings."""
    rows = connection.execute(text(
        "SELECT meeting_db_id, agenda_item_number FROM agenda_items "
        "WHERE meeting_db_id = ANY(:m)"), {"m": meetings}).mappings()
    return sorted(reservation.reservation_key(int(r["meeting_db_id"]),
                                              str(r["agenda_item_number"]))
                  for r in rows)


def _target(engine: Any) -> dict[str, Any]:
    from scripts.db import config

    url = engine.url
    return {
        "dialect": url.drivername, "host": url.host, "port": url.port,
        "database": url.database, "tier": config.DB_TIER,
    }


def _hold_ids(plan: Mapping[str, Any]) -> list[int]:
    population = (plan.get("bindings") or {}).get("hold_population") or {}
    return sorted(int(d) for d in population.get("document_ids") or [])


def _unlinked_ids(connection: Any, ids: list[int]) -> list[int]:
    """Only documents with NO row link.

    A document the committed correction has linked is no longer an *unlinked* hold, so
    the hold population is re-derived from live linkage rather than carried forward
    from the previous plan.  Editing counts would hide the same defect.
    """
    from sqlalchemy import text as _text

    if not ids:
        return []
    rows = connection.execute(_text(
        "SELECT id FROM supporting_documents WHERE id = ANY(:ids) "
        "AND agenda_item_db_id IS NULL"), {"ids": [int(i) for i in ids]}).scalars().all()
    return sorted(int(x) for x in rows)


def _applied_correction_identity(plan_dir: Path) -> dict[str, Any]:
    """The IMMUTABLE correction artifact on disk - the one that was actually applied.

    A repair-only regeneration must depend on this, never on the in-memory rebuild:
    the rebuild is never written, and its digest moves with ``created_at``.
    """
    hits = [p for p in sorted(plan_dir.glob("kg-stage2-s2-label-correction-plan-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (plan_dir / (p.name + ".obsolete.json")).exists()]
    if len(hits) != 1:
        raise SystemExit(f"expected one live correction head, found "
                         f"{[h.name for h in hits]}")
    doc = artifacts.load_verified(hits[0])
    return {
        "path": hits[0].name,
        "digest": artifacts.recorded_digest(doc),
        "replay_digest": doc.get("replay_digest"),
        "reserved_keys": sorted(
            o["key"] for o in (doc.get("reservation_operations") or {}).get("operations", [])
            if o.get("outcome") == "reserve"),
    }


def _documents(connection: Any, ids: list[int]) -> list[Mapping[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(DOCUMENT_QUERY), {"ids": ids}).mappings()]


def _items(connection: Any, meetings: list[int]) -> list[Mapping[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(ITEM_QUERY), {"meetings": meetings}).mappings()]


def _decisions(plan_dir: Path, aggregate: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Load the approved decisions as records the binding can canonicalise."""
    out: list[dict[str, Any]] = []
    for document_id, meta in sorted((aggregate.get("decisions") or {}).items(),
                                    key=lambda kv: int(kv[0])):
        path = plan_dir / Path(str(meta["path"])).name
        if not path.exists():
            raise SystemExit(f"decision artifact {meta['path']!r} is missing")
        record = artifacts.load_verified(path)
        proposal = record.get("proposal") or {}
        out.append({
            "document_id": int(record["document_id"]),
            "decision_id": record.get("decision_id"),
            "decision": record.get("decision"),
            "adjudicator": record.get("adjudicator"),
            "decided_at": record.get("decided_at"),
            "digest": artifacts.recorded_digest(record),
            "path": path.name,
            "document_role": record.get("document_role"),
            "document_fingerprint": record.get("document_fingerprint"),
            "proposal": {"path": proposal.get("path"), "digest": proposal.get("digest"),
                         # the adjudication unit, so the anchor's own membership can
                         # be checked unit-for-unit rather than only by digest
                         "decision_unit_id": proposal.get("decision_unit_id")},
            "candidate": record.get("candidate") or {},
            "lineage": dict(record.get("lineage") or {}),
            "human_stated_item": record.get("human_stated_item"),
            "item_number_mismatch": bool(record.get("item_number_mismatch")),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only", choices=("both", "correction", "repair"), default="both",
        help="Regenerate only one plan family.  After a correction has been APPLIED "
             "its plan must never be superseded, so the post-correction run uses "
             "--only repair.")
    args = parser.parse_args(argv)
    only = args.only
    engine = get_engine()
    target_ref = assert_read_only_target(engine)
    target = _target(engine)
    print(f"target: {target}")
    print(f"guard: {json.dumps(target_ref)}")

    head_path, head_plan, head_digest = lineage.current_plan(PLANS)
    agg_path, aggregate, agg_digest = lineage.current_aggregate(PLANS, head_digest)
    print(f"current plan head: {head_path.name} {head_digest}")
    print(f"current aggregate: {agg_path.name} {agg_digest}")

    # The two populations come from the artifact pair being superseded: this is a
    # regeneration of the same populations with corrected bindings, not a new
    # selection.
    repair_old = _read_latest("kg-stage2-s2-repair-plan-*.json")
    correction_old = _read_latest("kg-stage2-s2-label-correction-plan-*.json")
    repair_ids = _hold_ids(repair_old)
    correction_ids = _hold_ids(correction_old)
    inherited_repair = list(repair_ids)
    print(f"repair population (inherited): {len(repair_ids)} | correction population: "
          f"{len(correction_ids)}")
    if len(correction_ids) != 101:
        raise SystemExit(f"unexpected correction population: {len(correction_ids)}")

    backup_path = REPO / "data" / "backups" / str(
        (repair_old.get("bindings") or {}).get("backup", {}).get("path"))
    receipt = json.loads(backup_path.read_text())
    print(f"backup receipt: {backup_path.name}")

    with engine.connect() as connection:
        repair_ids = _unlinked_ids(connection, repair_ids)
        excluded = sorted(set(inherited_repair) - set(repair_ids))
        print(f"repair population (post-correction, unlinked only): {len(repair_ids)}"
              f" | excluded as linked by the correction: {len(excluded)} {excluded[:6]}")
        if not repair_ids:
            raise SystemExit("every repair hold document is linked now; nothing to plan")
        repair_rows = _documents(connection, repair_ids)
        correction_rows = _documents(connection, correction_ids)
        meetings = sorted({int(r["meeting_db_id"]) for r in repair_rows + correction_rows})
        items = _items(connection, meetings)
    decisions = _decisions(PLANS, aggregate)
    print(f"documents: repair {len(repair_rows)} correction {len(correction_rows)} "
          f"| items {len(items)} | decisions {len(decisions)}")

    created_at = datetime.now(timezone.utc).isoformat()
    with engine.connect() as connection:
        collision_control = _collision_control(connection)
        occupied = _occupied_keys(connection, meetings)
        reserved = sorted(reservation.reservation_key(r["meeting_db_id"],
                                                      r["agenda_item_number"])
                          for r in connection.execute(text(
                              f"SELECT meeting_db_id, agenda_item_number FROM "
                              f"{reservation.RESERVATION_TABLE}")).mappings())
    reservation_receipt = _reservation_receipt()
    print(f"collision control: {collision_control['mode']} "
          f"({collision_control['table']}) | occupied keys {len(occupied)} | "
          f"reserved {len(reserved)}")
    print(f"reservation receipt: {reservation_receipt['path']}")

    def _build(repair_state: str | None, correction_state: str | None):
        # The CORRECTION plan is built first: the repair plan depends on its
        # postimage, so the dependency must name that exact artifact by digest
        # rather than pointing at nothing.
        correction_backup = binding.backup_binding(
            backup_path, receipt=receipt,
            plan_baseline_counts=dict(receipt.get("counts") or {}))
        with engine.connect() as derivation_connection:
            correction_plan = correction_mod.build_plan(
                correction_rows, canonical_items=items, decisions=decisions,
                created_at=created_at, target=target, plan_dir=PLANS,
                backup=correction_backup, current_state_sha256=correction_state,
                collision_control=collision_control,
                reservation_receipt=reservation_receipt,
                live_keys=occupied, reserved_keys=reserved,
                derivation_connection=derivation_connection)
        # A repair-only run depends on the APPLIED correction artifact on disk; binding
        # the in-memory rebuild would name a plan that was never written.
        if only == "repair":
            correction_identity = _applied_correction_identity(PLANS)
        else:
            correction_identity = {
                "path": f"kg-stage2-s2-label-correction-plan-"
                        f"{correction_plan['digest'][:16]}.json",
                "digest": correction_plan["digest"],
                "replay_digest": correction_plan["replay_digest"],
                "reserved_keys": sorted(
                    o["key"] for o in
                    correction_plan["reservation_operations"]["operations"]
                    if o["outcome"] == "reserve"),
            }
        repair_backup = binding.backup_binding(
            backup_path, receipt=receipt,
            plan_baseline_counts=dict(receipt.get("counts") or {}))
        repair_plan = repair_mod.build_plan(
            repair_rows, canonical_items=items, decisions=decisions,
            created_at=created_at, target=target, plan_dir=PLANS,
            backup=repair_backup, current_state_sha256=repair_state,
            collision_control=collision_control, reservation_receipt=reservation_receipt,
            live_keys=occupied, reserved_keys=reserved,
            depends_on=correction_identity)
        return repair_plan, correction_plan

    # Pass 1: build without a state digest, capture, then rebuild binding it.
    repair_plan, correction_plan = _build(None, None)
    # The APPLIED correction plan's bound holds were CONSUMED by the correction itself,
    # so comparing them against live linkage could only ever fail.  This filtering lives
    # in this UNBOUND driver deliberately: putting it in the bound current-state module
    # would change a hash that the already-applied correction plan binds, and break the
    # historical verifiability of that artifact.  For a repair-only regeneration the
    # live-state binding is therefore scoped to the plan being regenerated; the
    # correction plan still enters the repair plan's DEPENDENCY lineage unchanged.
    verification_correction = repair_plan if only == "repair" else correction_plan
    with engine.connect() as connection:
        state = current_state.capture_current_state(
            connection, repair_plan=repair_plan,
            correction_plan=verification_correction)
    print(f"pass 1 state: {state['sha256']} counts={state['counts']}")

    repair_plan, correction_plan = _build(state["sha256"], state["sha256"])
    # Recompute for the pass-2 plans: binding the pass-1 plan object here would compare
    # a stale digest and refuse correctly but pointlessly.
    verification_correction = repair_plan if only == "repair" else correction_plan
    with engine.connect() as connection:
        state2 = current_state.capture_current_state(
            connection, repair_plan=repair_plan,
            correction_plan=verification_correction)
    if state2["sha256"] != state["sha256"]:
        raise SystemExit(f"the state digest is not stable: {state['sha256']} -> {state2['sha256']}")
    print(f"pass 2 state stable: {state2['sha256']}")

    with engine.connect() as connection:
        live = current_state.verify_live_state(
            state2, repair_plan=repair_plan,
            correction_plan=verification_correction)
    print(f"live state verified: {json.dumps(live)}")

    selected: list[tuple[Any, str]] = []
    if only in ("both", "correction"):
        selected.append((correction_plan, "label-correction-plan"))
    if only in ("both", "repair"):
        selected.append((repair_plan, "repair-plan"))
    print(f"--only {only}: writing {[k for _, k in selected]}")

    for plan, kind in selected:
        path = PLANS / f"kg-stage2-s2-{kind}-{plan['digest'][:16]}.json"
        artifacts.write_immutable(path, plan)
        print(f"wrote {path.name} digest={plan['digest']} replay={plan['replay_digest']}")

    for plan, kind in selected:
        for candidate in sorted(PLANS.glob(f"kg-stage2-s2-{kind}-*.json")):
            if candidate.name.endswith(".obsolete.json") or \
                    candidate.name == f"kg-stage2-s2-{kind}-{plan['digest'][:16]}.json":
                continue
            marker = PLANS / (candidate.name + ".obsolete.json")
            if marker.exists():
                continue
            artifacts.record_obsolete(
                PLANS, candidate,
                "superseded by the P1-corrected plan: per-document hold fingerprints, "
                "the complete backup binding (live modes and ownership, dump hash, "
                "restore evidence, source counts and schema, current baseline), and a "
                "live-recomputed current-state digest.")
            print(f"obsolete: {candidate.name}")
    return 0


def _read_latest(pattern: str) -> Mapping[str, Any]:
    """The newest live artifact matching a glob, by recorded supersession."""
    candidates = [p for p in sorted(PLANS.glob(pattern))
                  if not p.name.endswith(".obsolete.json")
                  and not (PLANS / (p.name + ".obsolete.json")).exists()]
    if len(candidates) != 1:
        raise SystemExit(f"{pattern!r} matched {len(candidates)} live artifacts")
    return artifacts.load_verified(candidates[0])


if __name__ == "__main__":
    raise SystemExit(main())
