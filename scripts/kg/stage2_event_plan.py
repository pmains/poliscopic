#!/usr/bin/env python3
"""``stage2_event_plan.py`` — immutable event attachment plan / governed hold ledger.

When the deterministic population is empty the artifact is a **hold ledger**, not a plan: it
records every event's class, the evidence audit that produced it, the current state, and a
concrete upstream acquisition/parser backlog.  Its write path is absent by design.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_event_route as route  # noqa: E402

__all__ = ["CODE_MODULES", "PLAN_KIND", "TABLE", "build_plan", "code_hashes",
           "table_signature", "upstream_backlog", "validate_plan"]

PLAN_KIND = "kg-stage2-event-attachment-plan"
PLAN_VERSION = "kg-stage2-event-attachment-plan/1.0"
TABLE = "meeting_events"
TARGET_FIELD = "agenda_item_id"

CODE_MODULES = (
    "scripts/kg/stage2_event_route.py",
    "scripts/kg/stage2_event_plan.py",
    "scripts/kg/stage2_subitem_schema_plan.py",
    "scripts/kg/stage2_backup_verify.py",
    "scripts/kg/stage2_artifacts.py",
)
TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def code_hashes(modules: Sequence[str] = CODE_MODULES) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in modules:
        p = REPO / rel
        if p.exists():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def table_signature(connection: Any, table: str) -> dict[str, Any]:
    cols = [dict(r) for r in connection.execute(text("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns WHERE table_schema='public' AND table_name=:t
        ORDER BY ordinal_position"""), {"t": table}).mappings().all()]
    pk = [r[0] for r in connection.execute(text("""
        SELECT a.attname FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.contype='p' AND c.conrelid=CAST(:t AS regclass) ORDER BY k.ord"""),
        {"t": table}).scalars()]
    indexes = [r[0] for r in connection.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename=:t "
        "ORDER BY indexname"), {"t": table})]
    fks = [r[0] for r in connection.execute(text(
        "SELECT conname FROM pg_constraint WHERE contype='f' AND conrelid=CAST(:t AS regclass)"),
        {"t": table}).scalars()]
    body = {"table": table, "columns": cols, "primary_key": pk, "indexes": indexes,
            "foreign_keys": fks}
    return {**body, "digest": canonical_sha256(body)}


def current_state(connection: Any) -> dict[str, Any]:
    q = lambda s: int(connection.execute(text(s)).scalar() or 0)
    return {
        "meeting_events": q("SELECT COUNT(*) FROM meeting_events"),
        "events_with_agenda_item_id": q(
            "SELECT COUNT(*) FROM meeting_events WHERE agenda_item_id IS NOT NULL"),
        "agenda_items": q("SELECT COUNT(*) FROM agenda_items"),
        "agenda_items_with_parent": q(
            "SELECT COUNT(*) FROM agenda_items WHERE parent_item_id IS NOT NULL"),
        "supporting_documents": q("SELECT COUNT(*) FROM supporting_documents"),
        "supporting_documents_linked": q(
            "SELECT COUNT(*) FROM supporting_documents WHERE agenda_item_db_id IS NOT NULL"),
        "meetings": q("SELECT COUNT(*) FROM meetings"),
    }


def upstream_backlog(classified: Mapping[str, Any], audit: Mapping[str, Any]) -> dict[str, Any]:
    """Concrete acquisition / parser work required before automatic links can exist."""
    return {
        "blocking_finding": (
            "Every one of the 19,588 events sources from a document of role 'Meeting Result'. "
            "Those documents carry doc-scoped synthetic agenda_item_id keys (e.g. "
            "'result-...-260722003R') that resolve to ZERO canonical agenda_items, and their "
            "agenda_item_number is uniformly '0'. No agenda-item evidence-span source exists, "
            "so coordinate containment cannot be tested either."),
        "items": [
            {"id": "B1", "area": "parser",
             "need": "Meeting Result parsing must emit the canonical agenda item identity "
                     "(the real item number/key of the meeting it reports on) instead of the "
                     "doc-scoped synthetic key and the constant '0' number.",
             "blocked_events": classified["counts"]["hold_missing_item_evidence"],
             "evidence": {"doc_keys_resolving_to_canonical":
                          audit.get("doc_keys_resolving_to_canonical")}},
            {"id": "B2", "area": "acquisition",
             "need": "Acquire the source agenda/packet documents for these meetings so an "
                     "event's coordinates can be tested against a uniquely identified "
                     "agenda-item evidence span (route C).",
             "blocked_events": classified["counts"]["hold_missing_item_evidence"],
             "evidence": {"span_source": audit.get("span_source")}},
            {"id": "B3", "area": "schema",
             "need": "Add offsets and agenda-item linkage to the chunk/span store "
                     "(document_text_chunks has neither) before route C can be enabled.",
             "blocked_events": classified["counts"]["hold_missing_item_evidence"],
             "evidence": {"source_docs_with_canonical_link":
                          audit.get("source_docs_with_canonical_link")}},
            {"id": "B4", "area": "lineage",
             "need": "Resolve the 9 quarantined extractions, which currently make those "
                     "events ineligible.",
             "blocked_events": classified["counts"]["ineligible"],
             "evidence": {"extractions_quarantined": audit.get("extractions_quarantined")}},
        ],
        "notes": ["Meeting co-membership is never evidence.",
                  "Fuzzy title similarity, document order and AI inference are never used "
                  "for automatic promotion."],
    }


def build_plan(connection: Any, *, target: Mapping[str, Any], created_at: str,
               created_by: str, backup_receipt: Mapping[str, Any],
               safety_modules: Sequence[str] | None = None) -> dict[str, Any]:
    classified = route.classify_all(connection)
    if not classified["reconciles"]:
        raise ValueError("the classification does not reconcile to the event population")
    audited = route.audit(connection, classified)
    state = current_state(connection)
    if state["meeting_events"] != classified["total"]:
        raise ValueError("the loaded event population is not the whole table")
    deterministic = classified["counts"]["would_link"]
    modules = tuple(safety_modules or ()) + CODE_MODULES
    plan = {
        "kind": PLAN_KIND, "version": PLAN_VERSION, "created_at": created_at,
        "created_by": created_by,
        "mode": "hold" if deterministic == 0 else "dry-run",
        "applied": False,
        "target": {f: target.get(f) for f in TARGET_FIELDS},
        "table": TABLE, "target_field": TARGET_FIELD,
        "producer": {"module": "scripts/kg/stage2_event_route.py",
                     "version": route.PRODUCER_VERSION,
                     "code_hashes": code_hashes(modules)},
        "classified": {"counts": classified["counts"], "total": classified["total"],
                       "reconciles": classified["reconciles"],
                       "classes_sha256": classified["classes_sha256"],
                       "fingerprints_sha256": classified["fingerprints_sha256"]},
        "deterministic_population": deterministic,
        "operations": [
            {"event_id": e["event_id"], "target": e["target"], "route": e["route"],
             "class": e["class"]}
            for e in classified["events"] if e["class"] == "would_link"],
        "ledger": classified["events"],
        "audit": audited,
        "upstream_backlog": upstream_backlog(classified, audited),
        "bindings": {
            "schema_signatures": {"agenda_items": budget_sig(connection),
                                  "meeting_events": table_signature(connection, TABLE)},
            "current_state": state,
            "backup_receipt": dict(backup_receipt),
            "classes_sha256": classified["classes_sha256"],
            "fingerprints_sha256": classified["fingerprints_sha256"],
        },
        "rules": {
            "exact_evidence_only": True,
            "unique_canonical_target_required": True,
            "co_membership_is_never_evidence": True,
            "no_fuzzy_title_similarity": True,
            "no_document_order": True,
            "no_ai_inference_for_promotion": True,
            "routes": list(route.ROUTES),
            "supported_routes": ["source_document_canonical_link",
                                 "source_document_canonical_key"],
            "unsupported_routes": {"coordinate_containment":
                                   classified["span_source"]["reason"]},
        },
        "write_path": "absent by design" if deterministic == 0 else "governed apply",
        "promoted": False,
    }
    if deterministic == 0:
        plan["hold"] = {
            "reason": "no deterministic population: every event's source document supplies "
                      "no canonical agenda-item evidence",
            "held_events": classified["total"],
            "classes": classified["counts"],
            "no_mutation": True,
        }
    plan["replay_digest"] = canonical_sha256(
        {k: v for k, v in plan.items()
         if k not in (artifacts.DIGEST_FIELD, "replay_digest", "created_at")})
    plan[artifacts.DIGEST_FIELD] = artifacts.compute_digest(plan)
    problems = validate_plan(plan)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return plan


def budget_sig(connection: Any) -> dict[str, Any]:
    from scripts.kg import stage2_subitem_schema_plan as schema_plan
    return schema_plan.schema_signature(connection)


def validate_plan(plan: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if plan.get("kind") != PLAN_KIND:
        problems.append(f"kind must be {PLAN_KIND!r}")
    if plan.get("applied") is not False:
        problems.append("the plan must be unapplied")
    counts = (plan.get("classified") or {}).get("counts") or {}
    for cls in route.CLASSES:
        if cls not in counts:
            problems.append(f"the classification omits the class {cls!r}")
    if sum(counts.values()) != plan.get("classified", {}).get("total"):
        problems.append("the classification does not reconcile to the population")
    ledger = plan.get("ledger") or []
    if len(ledger) != plan.get("classified", {}).get("total"):
        problems.append("the ledger does not cover every event exactly once")
    if len({e["event_id"] for e in ledger}) != len(ledger):
        problems.append("the ledger names an event twice")
    deterministic = plan.get("deterministic_population")
    operations = plan.get("operations") or []
    if deterministic != len(operations):
        problems.append("the deterministic population disagrees with the operations")
    if deterministic == 0:
        if plan.get("mode") != "hold":
            problems.append("an empty population must be a hold")
        if plan.get("write_path") != "absent by design":
            problems.append("a hold must have no write path")
        if (plan.get("hold") or {}).get("no_mutation") is not True:
            problems.append("a hold must assert no mutation")
    else:
        for op in operations:
            if not op.get("target") or not op.get("route"):
                problems.append("an operation carries no exact evidence")
                break
    recorded = (plan.get("producer") or {}).get("code_hashes") or {}
    live = code_hashes(tuple(recorded))
    drift = sorted(r for r, d in recorded.items() if live.get(r) != d)
    if drift:
        problems.append(f"code has drifted for {drift}")
    b = plan.get("bindings") or {}
    if not (b.get("schema_signatures") or {}).get("agenda_items", {}).get("digest"):
        problems.append("the plan binds no agenda_items signature")
    if not (b.get("schema_signatures") or {}).get("meeting_events", {}).get("digest"):
        problems.append("the plan binds no meeting_events signature")
    if not b.get("classes_sha256") or not b.get("fingerprints_sha256"):
        problems.append("the plan binds no classification fingerprints")
    state = b.get("current_state") or {}
    if int(state.get("meeting_events") or 0) <= 0:
        problems.append("the plan binds no current state")
    if (plan.get("target") or {}).get("tier") != "development":
        problems.append("the plan target is not development")
    if plan.get(artifacts.DIGEST_FIELD) != artifacts.compute_digest(plan):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems
