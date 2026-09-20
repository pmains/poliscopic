#!/usr/bin/env python3
"""``stage2_event_route.py`` — deterministic event -> agenda-item attachment truth.

CLASSIFICATION IS DELIBERATELY NARROW.  An event is promoted to a canonical agenda item
only when an EXACT documented route yields exactly one canonical target:

  A. source-document canonical link   event.supporting_doc_id -> doc.agenda_item_db_id -> item
  B. source-document canonical key    doc.agenda_item_id -> agenda_items.agenda_item_id (unique)
  C. coordinate containment           event offsets inside an agenda-item evidence span whose
                                      item is uniquely identified

Route C is declared and probed but is **unsupported** until an agenda-item evidence-span
source exists.  Meeting co-membership is NEVER evidence, and fuzzy title similarity, document
order and AI inference are never used for automatic promotion.

Every event lands in exactly one mutually exclusive class:
``would_link`` | ``replay`` | ``hold_missing_item_evidence`` | ``hold_ambiguous`` | ``ineligible``.
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

__all__ = ["CLASSES", "PRODUCER_VERSION", "ROUTES", "canonical_candidates",
           "classify_all", "fingerprint_event", "load_events", "span_source_status"]

PRODUCER_VERSION = "kg-stage2-event-route/1.0"
CLASSES = ("would_link", "replay", "hold_missing_item_evidence", "hold_ambiguous",
           "ineligible")
ROUTES = ("source_document_canonical_link", "source_document_canonical_key",
          "coordinate_containment")

#: Fields that define an event's identity for fingerprinting.
IDENTITY_FIELDS = ("id", "meeting_id", "supporting_doc_id", "event_type_id",
                   "text_offset_start", "text_offset_end")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def fingerprint_event(event: Mapping[str, Any]) -> str:
    return canonical_sha256({f: event.get(f) for f in IDENTITY_FIELDS})


def span_source_status(connection: Any) -> dict[str, Any]:
    """Is an agenda-item evidence-span source available for route C?"""
    rows = connection.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='document_text_chunks'""")).scalars().all()
    cols = set(rows)
    has_offsets = bool(cols & {"start_offset", "text_offset_start", "char_start"})
    has_item = bool(cols & {"agenda_item_id", "agenda_item_db_id", "item_id"})
    return {"table": "document_text_chunks", "columns": sorted(cols),
            "has_offsets": has_offsets, "has_item_linkage": has_item,
            "supported": has_offsets and has_item,
            "reason": ("no agenda-item evidence-span source: document_text_chunks carries "
                       "no offsets and no agenda-item linkage")}


def load_events(connection: Any) -> list[dict[str, Any]]:
    """Authoritative read-only loader over every meeting_event."""
    rows = connection.execute(text("""
        SELECT e.id, e.meeting_id, e.supporting_doc_id, e.agenda_item_id, e.event_type_id,
               e.text_offset_start, e.text_offset_end, e.case_number,
               d.id AS doc_id, d.agenda_item_id AS doc_agenda_item_id,
               d.agenda_item_db_id AS doc_agenda_item_db_id,
               d.agenda_item_number AS doc_agenda_item_number,
               d.document_type AS doc_document_type, d.content_hash AS doc_content_hash,
               d.meeting_db_id AS doc_meeting_db_id,
               x.quarantined_at AS extraction_quarantined_at,
               x.id AS extraction_id
        FROM meeting_events e
        LEFT JOIN supporting_documents d ON d.id = e.supporting_doc_id
        LEFT JOIN meeting_event_extractions x ON x.meeting_event_id = e.id
        ORDER BY e.id""")).mappings().all()
    return [dict(r) for r in rows]


def canonical_candidates(connection: Any, events: Sequence[Mapping[str, Any]]) -> dict:
    """Exact canonical targets per event, by documented route only."""
    items = {r[0] for r in connection.execute(text(
        "SELECT agenda_item_id FROM agenda_items WHERE agenda_item_id IS NOT NULL"))}
    by_db_id = {int(r[0]): r[1] for r in connection.execute(text(
        "SELECT id, agenda_item_id FROM agenda_items"))}
    out: dict[int, dict[str, list[str]]] = {}
    for e in events:
        routes: dict[str, list[str]] = {r: [] for r in ROUTES}
        db_id = e.get("doc_agenda_item_db_id")
        if db_id is not None and int(db_id) in by_db_id:
            key = by_db_id[int(db_id)]
            if key:
                routes["source_document_canonical_link"].append(key)
        doc_key = e.get("doc_agenda_item_id")
        if doc_key and doc_key in items:
            routes["source_document_canonical_key"].append(doc_key)
        # route C is unsupported: no span source exists, so it can never contribute.
        out[int(e["id"])] = routes
    return out


def classify_event(event: Mapping[str, Any], routes: Mapping[str, Sequence[str]],
                   *, span_supported: bool) -> dict[str, Any]:
    """Exactly one class per event.  Co-membership, titles, order and AI are never used."""
    def result(cls: str, reason: str, **extra) -> dict[str, Any]:
        return {"class": cls, "reason": reason, **extra}

    if event.get("supporting_doc_id") is None:
        return result("ineligible", "no source document reference")
    if event.get("doc_id") is None:
        return result("ineligible", "the source document row is missing")
    if event.get("text_offset_start") is None or event.get("text_offset_end") is None:
        return result("ineligible", "the event carries no source coordinates")
    if event.get("extraction_quarantined_at") is not None:
        return result("ineligible", "the event extraction is quarantined")
    if event.get("agenda_item_id"):
        return result("replay", "the event already carries an agenda_item_id",
                      target=event["agenda_item_id"])

    found: list[tuple[str, str]] = []
    for route, keys in (routes or {}).items():
        if route == "coordinate_containment" and not span_supported:
            continue
        for key in keys:
            found.append((route, key))
    unique = sorted({k for _, k in found})
    if len(unique) > 1:
        return result("hold_ambiguous", "more than one exact canonical target",
                      candidates=unique)
    if len(unique) == 1:
        route = sorted({r for r, k in found if k == unique[0]})[0]
        return result("would_link", f"exact route: {route}", target=unique[0],
                      route=route)
    return result("hold_missing_item_evidence",
                  "the source document supplies no canonical agenda-item evidence")


def classify_all(connection: Any) -> dict[str, Any]:
    events = load_events(connection)
    spans = span_source_status(connection)
    routes = canonical_candidates(connection, events)
    classified: list[dict[str, Any]] = []
    counts = {c: 0 for c in CLASSES}
    for e in events:
        verdict = classify_event(e, routes.get(int(e["id"]), {}),
                                 span_supported=spans["supported"])
        counts[verdict["class"]] += 1
        classified.append({
            "event_id": int(e["id"]), "meeting_id": e["meeting_id"],
            "supporting_doc_id": e["supporting_doc_id"],
            "doc_document_type": e.get("doc_document_type"),
            "class": verdict["class"], "reason": verdict["reason"],
            "target": verdict.get("target"), "route": verdict.get("route"),
            "candidates": verdict.get("candidates"),
            "event_fingerprint": fingerprint_event(e),
        })
    total = len(classified)
    return {"producer_version": PRODUCER_VERSION, "total": total, "counts": counts,
            "reconciles": sum(counts.values()) == total,
            "span_source": spans, "events": classified,
            "classes_sha256": canonical_sha256(
                [[c["event_id"], c["class"]] for c in classified]),
            "fingerprints_sha256": canonical_sha256(
                [c["event_fingerprint"] for c in classified])}


def audit(connection: Any, classified: Mapping[str, Any]) -> dict[str, Any]:
    """The lineage audit the plan records alongside the classification."""
    q = lambda s: connection.execute(text(s)).scalar()
    return {
        "events_total": q("SELECT COUNT(*) FROM meeting_events"),
        "with_offsets": q("SELECT COUNT(*) FROM meeting_events WHERE "
                          "text_offset_start IS NOT NULL AND text_offset_end IS NOT NULL"),
        "with_case_number": q("SELECT COUNT(*) FROM meeting_events WHERE case_number IS NOT NULL"),
        "with_action_verb": q("SELECT COUNT(*) FROM meeting_events WHERE action_verb IS NOT NULL"),
        "with_source_document": q("SELECT COUNT(*) FROM meeting_events "
                                  "WHERE supporting_doc_id IS NOT NULL"),
        "extractions_quarantined": q("SELECT COUNT(*) FROM meeting_event_extractions "
                                     "WHERE quarantined_at IS NOT NULL"),
        "distinct_source_documents": q("SELECT COUNT(DISTINCT supporting_doc_id) "
                                       "FROM meeting_events"),
        "source_document_roles": [list(r) for r in connection.execute(text(
            "SELECT d.document_type, COUNT(*) FROM meeting_events e "
            "JOIN supporting_documents d ON d.id = e.supporting_doc_id "
            "GROUP BY 1 ORDER BY 2 DESC"))],
        "source_docs_with_canonical_link": q(
            "SELECT COUNT(DISTINCT e.supporting_doc_id) FROM meeting_events e "
            "JOIN supporting_documents d ON d.id = e.supporting_doc_id "
            "WHERE d.agenda_item_db_id IS NOT NULL"),
        "source_docs_with_content_hash": q(
            "SELECT COUNT(DISTINCT d.id) FROM meeting_events e "
            "JOIN supporting_documents d ON d.id = e.supporting_doc_id "
            "WHERE d.content_hash IS NOT NULL"),
        "doc_keys_resolving_to_canonical": q(
            "SELECT COUNT(*) FROM (SELECT DISTINCT d.agenda_item_id k FROM meeting_events e "
            "JOIN supporting_documents d ON d.id = e.supporting_doc_id "
            "WHERE d.agenda_item_id IS NOT NULL) s "
            "JOIN agenda_items a ON a.agenda_item_id = s.k"),
        "holder_unlinked_documents": q(
            "SELECT COUNT(*) FROM supporting_documents WHERE agenda_item_db_id IS NOT NULL"),
        "span_source": classified.get("span_source"),
    }
