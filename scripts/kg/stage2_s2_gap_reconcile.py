#!/usr/bin/env python3
"""``stage2_s2_gap_reconcile.py`` — reconciliation invariant for item references.

Every supporting document that records a **nonempty** item reference must either
resolve to a parsed canonical agenda item or be **surfaced and held**.  Nothing
may be dropped, and nothing may be invented: a missing ``2.C`` is held, never
reconstructed by counting the items around it.

The classifier already produces the four outcome classes.  This module states the
invariant that makes them auditable as one closed population:

    nonempty references == linked + held

and exposes the held set grouped by the reference that could not be resolved, so
an extraction gap is visible as a gap rather than as a silent absence.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ReconcileRefused",
    "assert_population_closed",
    "is_meeting_level_attachment",
    "reconcile_references",
    "summarize_held",
]

#: An attachment named "Agenda: 6.2.26 Agenda" or "Minutes: 6.2.26 Agenda" is a
#: meeting-level record, not a reference to a numbered item.  The leading word
#: and the colon are what make it a label rather than prose.
_MEETING_LEVEL_ATTACHMENT_RE = re.compile(r"^\s*(?:agenda|minutes)\s*:", re.IGNORECASE)


def is_meeting_level_attachment(title: str) -> bool:
    """True when *title* names a meeting-level Agenda/Minutes attachment.

    Such a row carries a stray number (the ``1`` a doc-check assigns to the
    first attachment) that is not an item reference.  Classifying it as a gap
    would invent a missing item; classifying it as meeting-level is the truth.
    """
    return bool(_MEETING_LEVEL_ATTACHMENT_RE.match(title or ""))


#: Classes that mean "a nonempty reference that did not resolve".
HELD_CLASSES = ("gap_missing_target",)

#: Classes where the document records no item reference, so the invariant is
#: not engaged.  A placeholder is not a reference.
NON_REFERENCE_CLASSES = ("unassigned_placeholder", "meeting_level_only")


class ReconcileRefused(RuntimeError):
    """The reference population is not closed; nothing may be reported as complete."""


def reconcile_references(
    references: Iterable[Mapping[str, Any]],
    canonical_numbers: Iterable[str],
) -> dict[str, Any]:
    """Split nonempty references into resolved and held, losing none.

    *references* is a sequence of mappings with at least ``document_id``,
    ``meeting_db_id``, ``item_number`` and optionally ``body``.  *canonical_numbers*
    is the set of item numbers parsed for the same meeting.
    """
    canonical = {str(n).strip() for n in canonical_numbers if str(n).strip()}
    resolved: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    non_references: list[dict[str, Any]] = []

    for reference in references:
        number = str(reference.get("item_number") or "").strip()
        if not number:
            non_references.append(dict(reference))
            continue
        if reference.get("meeting_level") or is_meeting_level_attachment(
                str(reference.get("title") or "")):
            record = dict(reference)
            record["class"] = "meeting_level_only"
            record["reason"] = "meeting-level Agenda/Minutes attachment, not an item reference"
            non_references.append(record)
            continue
        if number in canonical:
            resolved.append(dict(reference))
            continue
        record = dict(reference)
        record["reason"] = "gap_missing_target"
        record["class"] = "gap_missing_target"
        held.append(record)

    return {
        "resolved": resolved,
        "held": held,
        "non_references": non_references,
        "counts": {
            "references": len(resolved) + len(held),
            "resolved": len(resolved),
            "held": len(held),
            "non_references": len(non_references),
        },
    }


def assert_population_closed(result: Mapping[str, Any]) -> None:
    """Refuse if any nonempty reference went missing from the outcome."""
    counts = result.get("counts") or {}
    accounted = int(counts.get("resolved", 0)) + int(counts.get("held", 0))
    stated = int(counts.get("references", -1))
    if accounted != stated:
        raise ReconcileRefused(
            f"reference population is not closed: {stated} stated, "
            f"{accounted} accounted for")
    for record in result.get("held", []):
        if not record.get("reason"):
            raise ReconcileRefused(
                f"held reference {record.get('document_id')!r} carries no reason")
        if str(record.get("item_number") or "").strip() == "":
            raise ReconcileRefused(
                "held reference has an empty item number; that is not a reference")


def summarize_held(result: Mapping[str, Any]) -> dict[str, Any]:
    """Held references grouped by body and by the missing reference itself."""
    by_body: dict[str, int] = {}
    by_reference: dict[str, int] = {}
    by_meeting: dict[str, int] = {}
    for record in result.get("held", []):
        body = str(record.get("body") or "")
        reference = str(record.get("item_number") or "").strip()
        meeting = str(record.get("meeting_db_id") or "")
        by_body[body] = by_body.get(body, 0) + 1
        by_reference[reference] = by_reference.get(reference, 0) + 1
        by_meeting[meeting] = by_meeting.get(meeting, 0) + 1
    return {
        "total": len(result.get("held", [])),
        "by_body": dict(sorted(by_body.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_meeting": dict(sorted(by_meeting.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_reference": dict(sorted(by_reference.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def dotted(references: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Only the ``n.AB`` dotted references — the split-label population."""
    import re
    pattern = re.compile(r"^\d{1,2}\.[A-Z]{1,2}$")
    return [r for r in references if pattern.match(str(r.get("item_number") or "").strip())]
