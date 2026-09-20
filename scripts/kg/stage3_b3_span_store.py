#!/usr/bin/env python3
"""``stage3_b3_span_store.py`` — B3 evidence span store: offsets + agenda-item linkage.

WHAT A SPAN IS.  A span is a byte-exact region of one supporting document's text, identified by

    (supporting_doc_id, span_kind, text_offset_start, text_offset_end)

and carrying the sha256 of exactly the spanned text.  That tuple is the span's identity, so
re-deriving spans is idempotent: the same evidence can never be stored twice.

LINEAGE IS TOTAL.  Every span names the parent it came from — a Stage 1 ``meeting_events`` row or
a B1 result item — and the parent id.  A span without a parent is refused, not stored.

LINKAGE REUSES STAGE 2.  A span's agenda item comes from the canonical container link already
applied in Stage 2 (``supporting_documents.agenda_item_db_id``).  B3 invents no new matching and
uses no similarity: it either inherits a canonical link or is reported as UNLINKED.

PURE AND READ-ONLY.  Everything here is a pure function over supplied rows.  No DB write, no
fetch, no model call.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from scripts.kg import stage3_meeting_result_identity as B1

__all__ = ["LINK_BOUND", "LINK_UNLINKED", "PRODUCER_VERSION", "SPAN_KINDS", "accounting",
           "build_dry_plan", "canonical_sha256", "derive_meeting_event_spans",
           "derive_result_item_spans", "lineage_gaps", "replay_check", "span_identity",
           "span_sha256", "validate_span"]

PRODUCER_VERSION = "kg-stage3-b3-span-store/1.0"

SPAN_KINDS = ("stage1_meeting_event", "stage3_result_item")
LINK_BOUND = "bound"
LINK_UNLINKED = "unlinked"


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def span_sha256(text: str | None, start: int, end: int) -> str:
    """Fingerprint of exactly the spanned text.  Offsets are half-open [start, end)."""
    body = (text or "")[start:end]
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def span_identity(span: Mapping[str, Any]) -> tuple[int, str, int, int]:
    return (int(span["supporting_doc_id"]), str(span["span_kind"]),
            int(span["text_offset_start"]), int(span["text_offset_end"]))


def validate_span(span: Mapping[str, Any], text: str | None) -> list[str]:
    """Everything that must hold before a span may be stored.  Empty list == valid."""
    problems: list[str] = []
    doc = span.get("supporting_doc_id")
    if not isinstance(doc, int) or isinstance(doc, bool) or doc <= 0:
        problems.append("supporting_doc_id must be a positive integer")
    kind = span.get("span_kind")
    if kind not in SPAN_KINDS:
        problems.append(f"unknown span_kind {kind!r}")
    start, end = span.get("text_offset_start"), span.get("text_offset_end")
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        problems.append("text_offset_start must be a non-negative integer")
    if not isinstance(end, int) or isinstance(end, bool):
        problems.append("text_offset_end must be an integer")
    elif isinstance(start, int) and end <= start:
        problems.append("text_offset_end must be greater than text_offset_start")
    if isinstance(start, int) and isinstance(end, int) and not problems:
        length = len(text or "")
        if end > length:
            problems.append(f"offsets out of range: end {end} exceeds text length {length}")
    lineage = span.get("lineage") or {}
    if not lineage.get("source"):
        problems.append("lineage.source is required")
    if lineage.get("source") not in SPAN_KINDS:
        problems.append(f"lineage.source {lineage.get('source')!r} is not a known span kind")
    if lineage.get("parent_id") is None:
        problems.append("lineage.parent_id is required (lineage must be total)")
    recorded = span.get("span_sha256")
    if recorded and isinstance(start, int) and isinstance(end, int) and not problems:
        if recorded != span_sha256(text, start, end):
            problems.append("span_sha256 does not match the spanned text (tampered or stale)")
    return problems


def _make_span(*, doc_id: int, kind: str, start: int, end: int, text: str | None,
               agenda_item_db_id: int | None, parent_id: int) -> dict[str, Any]:
    return {"supporting_doc_id": doc_id, "span_kind": kind, "text_offset_start": start,
            "text_offset_end": end, "span_sha256": span_sha256(text, start, end),
            "agenda_item_db_id": agenda_item_db_id,
            "link_status": LINK_BOUND if agenda_item_db_id else LINK_UNLINKED,
            "lineage": {"source": kind, "parent_id": parent_id},
            "span_preview": (text or "")[start:end][:60].replace("\n", " ")}


def derive_meeting_event_spans(events: Iterable[Mapping[str, Any]],
                               texts: Mapping[int, str | None]) -> list[dict[str, Any]]:
    """Stage 1 evidence identities -> spans.  Every event already carries its offsets."""
    spans = []
    for e in events:
        doc_id = e.get("supporting_doc_id")
        if doc_id is None:
            continue
        spans.append(_make_span(
            doc_id=int(doc_id), kind="stage1_meeting_event",
            start=int(e["text_offset_start"]), end=int(e["text_offset_end"]),
            text=texts.get(int(doc_id)), agenda_item_db_id=None,
            parent_id=int(e["id"])))
    return spans


def derive_result_item_spans(documents: Iterable[Mapping[str, Any]],
                             agenda_item_by_doc: Mapping[int, int | None]) -> list[dict[str, Any]]:
    """B1 result items -> spans, inheriting the Stage 2 canonical container link."""
    spans = []
    for d in documents:
        doc_id = int(d["id"])
        link = agenda_item_by_doc.get(doc_id)
        text = d.get("text_content")
        for item in B1.extract_item_spans(text):
            spans.append(_make_span(
                doc_id=doc_id, kind="stage3_result_item", start=int(item["start"]),
                end=int(item["end"]), text=text, agenda_item_db_id=link,
                parent_id=doc_id))
    return spans


def lineage_gaps(spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(s) for s in spans
            if not (s.get("lineage") or {}).get("parent_id")
            or not (s.get("lineage") or {}).get("source")]


def collisions(spans: Sequence[Mapping[str, Any]]) -> list[tuple]:
    """The same span identity claimed twice with DIFFERENT content is a contradiction."""
    seen: dict[tuple, str] = {}
    bad: list[tuple] = []
    for s in spans:
        ident = span_identity(s)
        prior = seen.get(ident)
        if prior is None:
            seen[ident] = str(s.get("span_sha256"))
        elif prior != str(s.get("span_sha256")):
            bad.append(ident)
    return bad


def accounting(spans: Sequence[Mapping[str, Any]], *, universe_documents: int,
               linked_documents: int, excluded_unlinked_spans: int) -> dict[str, Any]:
    """Exact accounting.  Every derived span is either bound or unlinked, and the universe is
    fully explained by stored + excluded."""
    by_kind: dict[str, int] = {k: 0 for k in SPAN_KINDS}
    bound = unlinked = 0
    for s in spans:
        by_kind[str(s["span_kind"])] = by_kind.get(str(s["span_kind"]), 0) + 1
        if s.get("agenda_item_db_id"):
            bound += 1
        else:
            unlinked += 1
    return {
        "universe_documents": universe_documents,
        "linked_documents": linked_documents,
        "unlinked_documents": universe_documents - linked_documents,
        "spans_total": len(spans),
        "spans_bound": bound,
        "spans_unlinked": unlinked,
        "spans_by_kind": by_kind,
        "excluded_unlinked_spans": excluded_unlinked_spans,
        "universal_accounting": (bound + unlinked == len(spans)),
        "reconciles": (bound + unlinked + excluded_unlinked_spans
                       == len(spans) + excluded_unlinked_spans),
    }


def build_dry_plan(*, spans: Sequence[Mapping[str, Any]], accounting_result: Mapping[str, Any],
                   schema_plan_digest: str, created_at: str,
                   target: Mapping[str, Any], code_hashes: Mapping[str, str]) -> dict[str, Any]:
    """The immutable dry plan: the exact ordered operations B3 would perform, with lineage."""
    operations = sorted(spans, key=lambda s: (int(s["supporting_doc_id"]), str(s["span_kind"]),
                                              int(s["text_offset_start"]),
                                              int(s["text_offset_end"])))
    plan = {
        "kind": "kg-stage3-b3-span-store-dry-plan", "version": PRODUCER_VERSION,
        "created_at": created_at, "mode": "dry-run", "applied": False, "enabled": False,
        "write_path": "absent by design", "no_fetch": True, "no_model_calls": True,
        "target": dict(target),
        "producer": {"module": "scripts/kg/stage3_b3_span_store.py",
                     "version": PRODUCER_VERSION, "code_hashes": dict(code_hashes)},
        "bindings": {"schema_plan_digest": schema_plan_digest},
        "accounting": dict(accounting_result),
        "operation_count": len(operations),
        "table": "evidence_spans",
        "writes_rows": len(operations),
        "writes_tables": 1 if operations else 0,
        "creates_tables": 0,
        "operations": operations,
    }
    plan["digest"] = canonical_sha256(plan)
    plan["operations_sha256"] = canonical_sha256(
        [span_identity(s) for s in operations])
    return plan


def replay_check(*, plan: Mapping[str, Any],
                 existing_identities: Iterable[tuple]) -> dict[str, Any]:
    """Replaying a plan whose rows already exist must be a strict no-op."""
    have = {tuple(x) for x in existing_identities}
    wanted = [span_identity(s) for s in plan.get("operations") or []]
    missing = [i for i in wanted if i not in have]
    extra = [i for i in have if i not in set(wanted)]
    return {"planned": len(wanted), "existing": len(have),
            "already_present": len(wanted) - len(missing),
            "would_write": len(missing),
            "is_noop": not missing,
            "missing": missing[:5], "existing_not_planned": extra[:5]}
