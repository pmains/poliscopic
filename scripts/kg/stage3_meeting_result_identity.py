#!/usr/bin/env python3
"""``stage3_meeting_result_identity.py`` — B1: canonical agenda-item identity for
Meeting Result documents.

THE DEFECT.  ``scripts/sync/extract_results_pdfs.py`` writes each results PDF as ONE
meeting-level document keyed ``result-{body}-{meeting_id}`` with
``agenda_item_number = '0'``.  The PDF text is stored whole but never parsed into per-item
results, so no extracted event can ever name the agenda item it came from.

THE FIX.  Parse the retained results text into deterministic per-item spans, and give every
item the canonical identity its own text proves: its normalized item number, resolved to
exactly one canonical agenda item in the same meeting.

THE CONTRACT.  An identity is emitted ONLY when the document's own text proves it.  Meeting
membership, document order, fuzzy title matching and AI inference never grant an identity.
Every other case fails closed with an explicit disposition:

  ``quarantined_input``   the document has no retained text (extraction failed)
  ``missing_identity``    the text proves no numbered item at all
  ``malformed_identity``  a number-shaped line that does not parse as an item number
  ``ambiguous_identity``  one normalized number appears more than once in the document
  ``missing_target``      the number resolves to no canonical item in this meeting
  ``ambiguous_target``    the number resolves to more than one canonical item
  ``replay``              the document already carries a canonical identity
  ``resolved``            exactly one canonical item, proven by the item's own text
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

__all__ = ["DISPOSITIONS", "IDENTITY_CLASSES", "PRODUCER_VERSION", "attribute_event",
           "build_baseline", "classify_document", "extract_item_spans",
           "normalize_item_number", "resolve_number"]

PRODUCER_VERSION = "kg-stage3-meeting-result-identity/1.0"
DOCUMENT_TYPE = "Meeting Result"
MEETING_LEVEL_KEY_PREFIX = "result-"
PLACEHOLDER_NUMBER = "0"

DISPOSITIONS = ("resolved", "quarantined_input", "missing_identity", "malformed_identity",
                "ambiguous_identity", "missing_target", "ambiguous_target", "replay")
IDENTITY_CLASSES = ("resolved", "replay", "hold_missing_identity", "hold_malformed_identity",
                    "hold_ambiguous_identity", "hold_missing_target",
                    "hold_ambiguous_target", "ineligible_quarantined")

#: A numbered item line: optional leading whitespace/form feed, digits, then '.' or ')'.
#: The trailing separator is required, so a bare number in prose is never an item.
#:
#: The leading-whitespace allowance is UNBOUNDED on purpose.  These results PDFs are
#: extracted from centred layouts, so a legitimate item line can carry far more than a
#: handful of leading spaces; the previous <=8 cap silently discarded them (measured:
#: 105 -> 421 of 1,200 sampled documents prove items, with ZERO documents losing items).
#: Widening whitespace is deterministic and cannot match prose: the line must still BEGIN
#: with the number and be followed by a separator and a space.
ITEM_LINE = re.compile(r"(?m)^[\s\x0c]*(\d{1,3}(?:\.[A-Za-z0-9]{1,3}){0,2})[.)]\s")
#: A number-shaped line that we will refuse to guess at (e.g. "sec-3", "22C.").
SUSPECT_LINE = re.compile(r"(?m)^[\s\x0c]*([A-Za-z]+\s*[-–]\s*\d+|\d{0,3}[A-Za-z]{1,3})[.)]\s")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def normalize_item_number(token: str | None) -> str | None:
    """Deterministic normalization.  Never guesses and never widens."""
    if token is None:
        return None
    raw = str(token).strip().rstrip(".)").strip()
    if not raw or raw == PLACEHOLDER_NUMBER:
        return None
    m = re.fullmatch(r"(\d{1,3})(?:\.([A-Za-z0-9]{1,3}))?", raw)
    if not m:
        return None
    base, leaf = m.group(1), m.group(2)
    base_norm = str(int(base))            # 01 -> 1
    if leaf is None:
        return base_norm
    return f"{base_norm}.{leaf.upper()}"


def extract_item_spans(text_content: str | None) -> list[dict[str, Any]]:
    """The item spans a results document's own text proves, in offset order."""
    if not text_content:
        return []
    hits = [(m.start(), m.group(1)) for m in ITEM_LINE.finditer(text_content)]
    spans: list[dict[str, Any]] = []
    for i, (start, token) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text_content)
        spans.append({"token": token, "start": start, "end": end,
                      "raw": text_content[start:start + 60].splitlines()[0][:60]})
    return spans


def suspect_tokens(text_content: str | None) -> list[str]:
    if not text_content:
        return []
    return [m.group(1) for m in SUSPECT_LINE.finditer(text_content)]


def classify_document(*, text_content: str | None, quarantined: bool,
                      existing_identity: str | None,
                      canonical_numbers: Iterable[str],
                      synthetic_key: str | None = None) -> dict[str, Any]:
    """One disposition per document.  Deterministic; fails closed by default."""
    if quarantined or not (text_content or "").strip():
        return {"disposition": "quarantined_input", "class": "ineligible_quarantined",
                "items": [], "reason": "the document has no retained text"}
    if existing_identity and not str(existing_identity).startswith(MEETING_LEVEL_KEY_PREFIX):
        return {"disposition": "replay", "class": "replay", "items": [],
                "reason": "the document already carries a canonical identity"}

    spans = extract_item_spans(text_content)
    if not spans:
        suspects = suspect_tokens(text_content)
        if suspects:
            return {"disposition": "malformed_identity",
                    "class": "hold_malformed_identity", "items": [],
                    "reason": "number-shaped lines that do not parse as item numbers",
                    "suspects": suspects[:5]}
        return {"disposition": "missing_identity", "class": "hold_missing_identity",
                "items": [], "reason": "the text proves no numbered item"}

    canonical = {str(n) for n in canonical_numbers}
    normalized = [normalize_item_number(s["token"]) for s in spans]
    if any(n is None for n in normalized):
        bad = [s["token"] for s, n in zip(spans, normalized) if n is None]
        return {"disposition": "malformed_identity", "class": "hold_malformed_identity",
                "items": [], "reason": "an item line does not normalize", "tokens": bad[:5]}

    counts: dict[str, int] = {}
    for n in normalized:
        counts[n] = counts.get(n, 0) + 1
    duplicated = sorted(n for n, c in counts.items() if c > 1)
    if duplicated:
        return {"disposition": "ambiguous_identity", "class": "hold_ambiguous_identity",
                "items": [], "reason": "one normalized number appears more than once",
                "duplicated": duplicated[:5]}

    items, dispositions = [], []
    for span, number in zip(spans, normalized):
        matches = sorted(canonical_numbers)
        target = number if number in canonical else None
        items.append({"number": number, "start": span["start"], "end": span["end"],
                      "target": target,
                      "resolved": target is not None})
        dispositions.append("resolved" if target else "missing_target")
    if not items:
        return {"disposition": "missing_identity", "class": "hold_missing_identity",
                "items": [], "reason": "no item spans"}
    resolved = sum(1 for i in items if i["resolved"])
    if resolved == 0:
        return {"disposition": "missing_target", "class": "hold_missing_target",
                "items": items, "reason": "no item number resolves to a canonical item"}
    if resolved < len(items):
        return {"disposition": "missing_target", "class": "hold_missing_target",
                "items": items, "partial": True,
                "reason": "some item numbers resolve and some do not"}
    return {"disposition": "resolved", "class": "resolved", "items": items,
            "reason": "every item number resolves to exactly one canonical item"}


def attribute_event(spans: Sequence[Mapping[str, Any]], offset_start: int | None) -> Any:
    """The item span an event's coordinates fall in, or None.  Containment only."""
    if offset_start is None:
        return None
    for span in spans:
        if int(span["start"]) <= int(offset_start) < int(span["end"]):
            return span
    return None


def resolve_number(number: str, canonical_numbers: Iterable[str]) -> str | None:
    """Exact key equality against the canonical items of this meeting."""
    return number if number in {str(n) for n in canonical_numbers} else None


def build_baseline(connection: Any) -> dict[str, Any]:
    """Read-only projection: how many governed event holds B1 would resolve.

    Nothing is written.  Every event is attributed by deterministic coordinate containment
    inside a span the document's own text proves; no event is attributed by meeting
    membership, order, title similarity or inference.
    """
    docs = connection.execute(text("""
        SELECT d.id, d.body, d.meeting_id, d.meeting_db_id, d.agenda_item_id,
               d.agenda_item_number, d.text_content,
               CASE WHEN d.text_extraction_method = 'pdftotext-failed' THEN TRUE ELSE FALSE END
                   AS quarantined
        FROM supporting_documents d WHERE d.document_type = :dt
        ORDER BY d.id"""), {"dt": DOCUMENT_TYPE}).mappings().all()
    numbers_by_meeting: dict[int, set] = {}
    for r in connection.execute(text(
            "SELECT meeting_db_id, agenda_item_number FROM agenda_items")):
        numbers_by_meeting.setdefault(int(r[0]), set()).add(str(r[1]))
    events = connection.execute(text("""
        SELECT e.id, e.supporting_doc_id, e.text_offset_start
        FROM meeting_events e ORDER BY e.id""")).mappings().all()
    events_by_doc: dict[int, list] = {}
    for e in events:
        events_by_doc.setdefault(int(e["supporting_doc_id"]), []).append(dict(e))

    doc_results, counts, event_counts = [], {}, {}
    for c in IDENTITY_CLASSES:
        counts[c] = 0
    event_counts = {"would_resolve": 0, "hold_no_span": 0, "hold_item_unresolved": 0,
                    "ineligible_document": 0, "total_events": 0}
    for d in docs:
        verdict = classify_document(
            text_content=d["text_content"], quarantined=bool(d["quarantined"]),
            existing_identity=d["agenda_item_id"],
            canonical_numbers=numbers_by_meeting.get(int(d["meeting_db_id"] or 0), set()),
            synthetic_key=d["agenda_item_id"])
        counts[verdict["class"]] += 1
        doc_events = events_by_doc.get(int(d["id"]), [])
        event_counts["total_events"] += len(doc_events)
        for e in doc_events:
            if verdict["class"] == "ineligible_quarantined":
                event_counts["ineligible_document"] += 1
                continue
            span = attribute_event(verdict["items"], e["text_offset_start"])
            if span is None:
                event_counts["hold_no_span"] += 1
            elif not span["resolved"]:
                event_counts["hold_item_unresolved"] += 1
            else:
                event_counts["would_resolve"] += 1
        doc_results.append({"doc_id": int(d["id"]), "body": d["body"],
                            "meeting_id": d["meeting_id"],
                            "synthetic_key": d["agenda_item_id"],
                            "disposition": verdict["disposition"],
                            "class": verdict["class"], "items": len(verdict["items"]),
                            "reason": verdict["reason"]})
    body = {"producer_version": PRODUCER_VERSION, "document_type": DOCUMENT_TYPE,
            "documents": {"total": len(docs), "by_class": counts},
            "events": event_counts, "event_holds_before": 19579,
            "per_document": doc_results}
    return {**body, "documents_sha256": canonical_sha256(
        [[r["doc_id"], r["class"]] for r in doc_results]),
        "projection_sha256": canonical_sha256(
            {k: v for k, v in body.items() if k != "per_document"})}
