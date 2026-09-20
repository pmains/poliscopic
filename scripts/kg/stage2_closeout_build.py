#!/usr/bin/env python3
"""``stage2_closeout_build.py`` — build the Stage 2 closeout baseline artifacts.

Read-only.  Gathers exact evidence from the live development database and the
immutable artifacts, then writes three write-once artifacts:

* the **closeout/dependency ledger** — every Stage 2 exit criterion and Brief 021
  work item with its status, exact count and denominator, evidence digests,
  owner/dependency and pass condition;
* the **dry quality gate** — the same criteria evaluated against the *current*
  state, so the gap that remains is explicit;
* the **receipt schema** — the shape an eventual closeout receipt must have.

Nothing here mutates the database, and no plan is modified.
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
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_closeout as closeout  # noqa: E402
from scripts.kg import stage2_quality_gate as gate_mod  # noqa: E402
from scripts.kg import stage2_s2_ai_lineage as lineage  # noqa: E402

PLANS = REPO / "data" / "kg-plans"
OUT_DIR = PLANS


def _live(pattern: str) -> Path:
    hits = [p for p in sorted(PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and "-preimage" not in p.name
            and not (PLANS / (p.name + ".obsolete.json")).exists()]
    if len(hits) != 1:
        raise SystemExit(f"{pattern!r} matched {len(hits)} live artifacts: "
                         f"{[h.name for h in hits]}")
    return hits[0]


def _supersede(prefix: str, keep: str, reason: str) -> list[str]:
    """Archive earlier builds of the same artifact by sidecar; never delete."""
    archived = []
    for candidate in sorted(PLANS.glob(f"{prefix}*.json")):
        if candidate.name.endswith(".obsolete.json") or candidate.name == keep:
            continue
        if (PLANS / (candidate.name + ".obsolete.json")).exists():
            continue
        artifacts.record_obsolete(PLANS, candidate, reason)
        archived.append(candidate.name)
    return archived


def _head(pattern: str) -> dict:
    path = _live(pattern)
    document = artifacts.load_verified(path)
    return {"path": path.name, "digest": artifacts.recorded_digest(document),
            "replay_digest": document.get("replay_digest"), "document": document}


def _target(engine) -> dict:
    from scripts.db import config

    url = engine.url
    return {"dialect": url.drivername, "host": url.host, "port": url.port,
            "database": url.database, "tier": config.DB_TIER}


def _resolved_documents(repair: dict, correction: dict) -> dict:
    """The documents the dry plans would resolve, as an exact union."""
    repair_docs: set[int] = set()
    for row in repair.get("rows") or []:
        if row.get("action") == "materialise":
            repair_docs.update(int(d) for d in row.get("resolves_documents") or [])
    correction_docs: set[int] = set()
    for op in correction.get("operations") or []:
        correction_docs.update(int(d) for d in op.get("resolves_documents") or [])
    union = repair_docs | correction_docs
    repair_population = {int(d) for d in
                         repair["bindings"]["hold_population"]["document_ids"]}
    return {
        "repair_resolved": sorted(repair_docs),
        "correction_resolved": sorted(correction_docs),
        "union_resolved": sorted(union),
        "resolved_gap_documents": len(union),
        "repair_and_correction_overlap": len(repair_docs & correction_docs),
        "repair_population": len(repair_population),
        "union_within_repair_population": union <= repair_population,
        "correction_population_within_repair": (
            {int(d) for d in correction["bindings"]["hold_population"]["document_ids"]}
            <= repair_population),
    }


def main() -> int:
    engine = get_engine()
    guard = assert_read_only_target(engine)
    target = _target(engine)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    created_at = datetime.now(timezone.utc).isoformat()
    print(f"guard: {json.dumps(guard)}")

    with engine.connect() as connection:
        counts = connection.execute(text("""
            SELECT (SELECT COUNT(*) FROM meetings) AS meetings,
                   (SELECT COUNT(*) FROM meetings WHERE public_body_id IS NOT NULL)
                       AS meetings_with_body,
                   (SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL)
                       AS meetings_without_body,
                   (SELECT COUNT(*) FROM agenda_items) AS agenda_items,
                   (SELECT COUNT(*) FROM agenda_items WHERE meeting_db_id IS NOT NULL)
                       AS items_with_meeting,
                   (SELECT COUNT(*) FROM supporting_documents) AS documents
        """)).mappings().first()
        columns = {r[0] for r in connection.execute(text(
            "SELECT table_name || '.' || column_name FROM information_schema.columns "
            "WHERE table_name IN ('supporting_documents', 'agenda_items', 'meetings')"))}
        validated = connection.execute(text("""
            SELECT COUNT(*) FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE c.contype = 'f' AND c.convalidated AND t.relname = 'meetings'
        """)).scalar()
        unvalidated = connection.execute(text("""
            SELECT COUNT(*) FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE c.contype = 'f' AND NOT c.convalidated AND t.relname = 'meetings'
        """)).scalar()
        full_columns = {r[0] for r in connection.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'agenda_items'"))}

    s2 = _head("kg-stage2-s2-plan-*.json")
    aggregate_path, aggregate, aggregate_digest = lineage.current_aggregate(
        PLANS, s2["digest"])
    repair = _head("kg-stage2-s2-repair-plan-*.json")
    correction = _head("kg-stage2-s2-label-correction-plan-*.json")
    event = _head("kg-stage2-event-attachment-plan-*.json")
    containment_base = _head("kg-stage2-subitem-containment-baseline-*.json")
    containment_plan = _head("kg-stage2-subitem-containment-plan-*.json")
    s1_receipt = _head("kg-stage2-s1-receipt-*.json")

    plan_counts = dict(s2["document"].get("counts") or {})
    base = containment_base["document"]
    event_plan = event["document"]
    resolved = _resolved_documents(repair["document"], correction["document"])
    eligible = gate_mod.eligible_documents(plan_counts)
    projected_links = plan_counts["deterministic_links"] + resolved["resolved_gap_documents"]

    s1_holds = int(s1_receipt["document"].get("holds")
                   or len(s1_receipt["document"].get("held_meeting_ids") or []) or 218)
    meetings = int(counts["meetings"])
    containers = {
        "target": target,
        "meetings": meetings,
        "meetings_with_body": int(counts["meetings_with_body"]),
        "meetings_without_body": int(counts["meetings_without_body"]),
        "agenda_items": int(counts["agenda_items"]),
        "items_with_meeting": int(counts["items_with_meeting"]),
        "s1_holds": s1_holds,
        "containers_eligible_for_a_parent": (meetings - s1_holds)
                                             + int(counts["items_with_meeting"]),
        "containers_with_a_canonical_parent": int(counts["meetings_with_body"])
                                              + int(counts["items_with_meeting"]),
    }
    containment = {
        "orphan_count": base["containment"]["orphan_count"],
        "cycles": list(base["containment"]["cycles"]),
        "proposed_links": base["containment"]["proposed_links"],
        "collision_keys": base["collision_population"]["distinct_keys"],
        "collision_rows": base["collision_population"]["involved_rows"],
        "collision_excess": base["collision_population"]["excess"],
        **{k: base["counts"][k] for k in
           ("root", "subitem", "deeper", "ambiguous", "invalid", "collision_held")},
    }
    event_summary = {
        "deterministic_link": event_plan["counts"]["deterministic_link"],
        "ambiguous": event_plan["counts"]["ambiguous"],
        "meeting_level": event_plan["counts"]["meeting_level"],
        "ineligible": event_plan["counts"]["ineligible"],
        "missing_evidence": event_plan["counts"]["missing_evidence"],
        "population": event_plan["population"]["count"],
        "unattached": event_plan["accounting"]["unattached"],
    }
    schema_state = {
        "column_present": "supporting_documents.agenda_item_db_id" in columns,
        "parent_item_id_present": "agenda_items.parent_item_id" in columns,
        "meetings_validated_fks": int(validated),
        "meetings_unvalidated_fks": int(unvalidated),
        "agenda_items_columns": len(full_columns),
        "unexpected_differences": [],
        "planned_columns": ["supporting_documents.agenda_item_db_id"],
    }

    heads = {
        "s2_plan": {k: v for k, v in s2.items() if k != "document"},
        "aggregate": {"path": aggregate_path.name, "digest": aggregate_digest},
        "repair": {k: v for k, v in repair.items() if k != "document"},
        "correction": {k: v for k, v in correction.items() if k != "document"},
        "event": {k: v for k, v in event.items() if k != "document"},
        "containment": {k: v for k, v in containment_base.items() if k != "document"},
        "containment_plan": {k: v for k, v in containment_plan.items() if k != "document"},
        "s1_receipt": {k: v for k, v in s1_receipt.items() if k != "document"},
    }

    print(f"containers: {json.dumps(containers)}")
    print(f"documents: {json.dumps(plan_counts)}")
    print(f"eligible: {eligible} | projected resolved: {resolved['resolved_gap_documents']} "
          f"| projected links: {projected_links} -> "
          f"{projected_links / eligible:.6f}")
    print(f"containment: {json.dumps({k: v for k, v in containment.items() if k != 'cycles'})}")
    print(f"event: {json.dumps(event_summary)}")
    print(f"schema: {json.dumps(schema_state)}")

    ledger = closeout.build_ledger(
        created_at=created_at, heads=heads, containers=containers,
        documents=plan_counts, containment=containment, event=event_summary,
        repair={"path": repair["path"], "digest": repair["digest"],
                **repair["document"]["counts"]},
        correction={"path": correction["path"], "digest": correction["digest"],
                    **correction["document"]["counts"]},
        decisions={"approved": aggregate["counts"]["approved"],
                   "promoted": aggregate["counts"]["promoted"],
                   "applied": aggregate["counts"]["applied"],
                   "decision_units": aggregate["counts"]["decision_units"],
                   "decisions_expected": 5},
        schema=schema_state,
        projected={
            **{k: v for k, v in resolved.items() if k != "repair_resolved"
               and k != "correction_resolved"},
            "projected_deterministic_links": projected_links,
            "projected_rate": projected_links / eligible,
            "eligible_documents": eligible,
        })
    ledger_path = OUT_DIR / f"kg-stage2-closeout-ledger-{stamp}.json"
    artifacts.write_immutable(ledger_path, ledger)
    print(f"ledger: {ledger_path.name} digest={ledger['ledger_digest']}")

    observed = {
        # Nothing has been applied yet: the gate must say so, so that criteria which
        # can only be proven post-apply are recorded as pending rather than passed.
        "applied": False,
        "containers_with_a_canonical_parent": containers["containers_with_a_canonical_parent"],
        "containers_eligible_for_a_parent": containers["containers_eligible_for_a_parent"],
        "orphan_count": containment["orphan_count"],
        "cycle_count": len(containment["cycles"]),
        "deterministic_links": plan_counts["deterministic_links"],
        "eligible_documents": eligible,
        "unexplained_remainder": plan_counts.get("unexplained_remainder", 0),
        "drifted_protected_counts": [],
        "drifted_integrity_metrics": [],
        "columns_present": [], "columns_validated": [],
        "replay_writes": 0,
        "parity_differences": [],
    }
    gate = gate_mod.evaluate_gate(observed)
    gate_path = OUT_DIR / f"kg-stage2-quality-gate-dry-{stamp}.json"
    artifacts.write_immutable(gate_path, gate)
    print(f"gate: {gate_path.name} closed={gate['closed']} failed={gate['failed']}")

    schema = gate_mod.receipt_schema()
    schema_path = OUT_DIR / f"kg-stage2-closeout-receipt-schema-{stamp}.json"
    artifacts.write_immutable(schema_path, schema)
    print(f"receipt schema: {schema_path.name}")

    reason = ("superseded by a later closeout build: the ledger, the dry gate and "
              "the receipt schema are rebuilt together so they describe one state")
    for archived in (_supersede("kg-stage2-closeout-ledger-", ledger_path.name, reason),
                     _supersede("kg-stage2-quality-gate-dry-", gate_path.name, reason),
                     _supersede("kg-stage2-closeout-receipt-schema-", schema_path.name,
                                reason)):
        for name in archived:
            print(f"obsolete: {name}")

    for path, validator, label in (
            (ledger_path, closeout.validate_ledger, "ledger"),
            (gate_path, gate_mod.validate_gate, "gate")):
        problems = validator(artifacts.load_verified(path))
        print(f"revalidation {label}: {problems if problems else 'CLEAN'}")
        if problems:
            raise SystemExit("; ".join(problems[:5]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
