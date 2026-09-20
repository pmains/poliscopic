#!/usr/bin/env python3
"""``stage2_s2_row_derivation.py`` — complete ``agenda_items`` rows from evidence.

The earlier insert contract was defective: it named ``item_type_category``, which does
not exist on the live table, and supplied seven columns while ``agenda_items`` requires
sixteen.  This module derives the **complete** row instead.

Values come from evidence, never from the schema.  The live schema and the ORM supply
*structure*; the values come from three authoritative places:

* the **meeting row** — ``body``, ``meeting_id``, ``source_url``;
* the repository's **canonical writer** (:func:`scripts.db.persist.persist_agenda_items`),
  whose ``AgendaItem(...)`` construction defaults ``agenda_item_url``, ``vote_or_action``,
  ``c_number``, ``c_number_base``, ``case_number``, ``agenda_category`` and ``item_type``
  to ``""`` and sets ``source_body`` to the body;
* the **source-key convention** ``f"{body}-{meeting_id}_{number}"``, which holds for
  every numbered sibling of these meetings (117/117; the only exceptions are
  ``agenda_item_number == "0"`` auto items, which no proposed row is).

``created_at`` is left to the schema's ``now()`` default.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

__all__ = ["DERIVATION_RULES", "derive_row", "derive_rows", "meeting_evidence",
           "validate_row"]

#: Every ``agenda_items`` column this module supplies, with its authority.
DERIVATION_RULES = {
    "body": "meetings.body",
    "meeting_id": "meetings.meeting_id",
    "meeting_db_id": "the plan's meeting id",
    "agenda_item_number": "the plan's corrected label",
    "agenda_item_id": 'f"{body}-{meeting_id}_{agenda_item_number}" (117/117 siblings)',
    "agenda_item_title": "plan witness title span",
    "agenda_item_text": "plan witness retained text",
    "source_body": "= body (persist.py: source_body or body or body)",
    "source_url": "meetings.source_url (uniform per meeting)",
    "agenda_item_url": '"" (persist.py default)',
    "vote_or_action": '"" (persist.py default)',
    "c_number": '"" (persist.py default)',
    "c_number_base": '"" (persist.py default)',
    "case_number": '"" (persist.py default)',
    "agenda_category": '"" (persist.py default)',
    "item_type": '"" (persist.py default; the real column, not item_type_category)',
    "created_at": "ORM Python-side default datetime.now(timezone.utc) "
                  "(models.AgendaItem.created_at).  The live column has NO database "
                  "default, so the writer must supply it exactly as the ORM does.",
}

#: Columns a complete row must carry before it may be inserted.
REQUIRED = tuple(DERIVATION_RULES)


def meeting_evidence(connection: Any,
                     meeting_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The authoritative per-meeting facts, read once.  Reads only."""
    from sqlalchemy import text

    if not meeting_ids:
        return {}
    rows = connection.execute(text(
        "SELECT id, body, meeting_id, COALESCE(source_url, '') AS source_url, "
        "jurisdiction_id, public_body_id FROM meetings WHERE id = ANY(:m)"),
        {"m": sorted({int(m) for m in meeting_ids})}).mappings()
    return {int(r["id"]): dict(r) for r in rows}


def derive_row(meeting: Mapping[str, Any], *, number: str, title: str, text: str,
               section_level: int = 0, sort_order: int = 0) -> dict[str, Any]:
    """One complete row, from the meeting's evidence and the repository's convention."""
    body = str(meeting["body"])
    meeting_id = str(meeting["meeting_id"])
    return {
        "meeting_db_id": int(meeting["id"]),
        "body": body,
        "meeting_id": meeting_id,
        "agenda_item_number": str(number),
        "agenda_item_id": f"{body}-{meeting_id}_{number}",
        "agenda_item_title": str(title),
        "agenda_item_text": str(text),
        "agenda_item_url": "",
        "vote_or_action": "",
        "source_body": body,
        "source_url": str(meeting.get("source_url") or ""),
        "c_number": "",
        "c_number_base": "",
        "case_number": "",
        "agenda_category": "",
        "item_type": "",
        "section_level": int(section_level),
        "sort_order": int(sort_order),
        "created_at": datetime.now(timezone.utc),
    }


def validate_row(row: Mapping[str, Any]) -> list[str]:
    """A row may only be inserted when every required field is present and evidenced."""
    problems = [f"{name} is missing" for name in REQUIRED if name not in row]
    if problems:
        return problems
    if row.get("source_body") != row.get("body"):
        problems.append("source_body must equal body (persist.py semantics)")
    if str(row.get("agenda_item_number")) == "0":
        problems.append("an auto item has no derivable agenda_item_id convention")
    expected = f"{row.get('body')}-{row.get('meeting_id')}_{row.get('agenda_item_number')}"
    if row.get("agenda_item_id") != expected:
        problems.append("agenda_item_id does not follow the source-key convention")
    for name in ("meeting_db_id",):
        if not isinstance(row.get(name), int):
            problems.append(f"{name} must be an integer identity")
    return problems


def derive_rows(connection: Any,
                pairs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Complete rows for ``pairs`` (``meeting_db_id`` + ``to_label`` + title/text).

    Fails closed: a pair whose meeting has no row, or whose derived row does not
    validate, is simply absent from the result, so the caller can refuse rather than
    insert something unevidenced.
    """
    evidence = meeting_evidence(connection, [p["meeting_db_id"] for p in pairs])
    out: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        meeting = evidence.get(int(pair["meeting_db_id"]))
        if meeting is None:
            continue
        row = derive_row(meeting, number=str(pair["to_label"]),
                         title=str(pair.get("title") or ""),
                         text=str(pair.get("text") or pair.get("title") or ""),
                         section_level=int(pair.get("section_level") or 0),
                         sort_order=int(pair.get("sort_order") or 0))
        if validate_row(row):
            continue
        out[f"{int(pair['meeting_db_id'])}|{pair['to_label']}"] = row
    return out
