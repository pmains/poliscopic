#!/usr/bin/env python3
"""``stage2_dupkey_investigate.py`` — read-only duplicate natural-key investigation.

Classifies **every** row and group behind the duplicate
``(meeting_db_id, agenda_item_number)`` keys into mutually exclusive,
evidence-based families, binds the evidence each verdict rests on, measures the
downstream references a cleanup would disturb, and emits one immutable artifact.

The families are applied **first-match**, so every row lands in exactly one and the
counts reconcile by construction:

1. ``auto_positional``   — the source key is a positional pseudo-key (``*_auto-N``):
   the scraper numbered agenda *lines* it could not print a number for. Distinct
   rows, not duplicates of one another.
2. ``sentinel_number``   — the number is a sentinel (``0``, ``-``, ``n/a``, ...).
3. ``exact_duplicate``   — the row repeats another row's (source key, title, text):
   the same record ingested twice.
4. ``distinct_same_number`` — a distinct source key and a distinct title: two
   genuinely different items that legitimately carry the same printed number.
5. ``source_version``    — same source key as a sibling but different provenance
   (a re-ingest by a different source version).
6. ``unresolved``        — none of the above; reported, never guessed at.

Read-only: every statement is a SELECT.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

__all__ = [
    "FAMILIES",
    "GROUP_FAMILIES",
    "canonical_fingerprint",
    "classify_rows",
    "family_counts",
]

#: The row families, in first-match order.
FAMILIES = ("auto_positional", "sentinel_number", "multi_body_meeting",
            "exact_duplicate", "distinct_same_number", "source_version",
            "unresolved")

#: What a group's verdict means when every row in it is explained.
GROUP_FAMILIES = ("placeholder_only", "multi_body_meeting", "exact_duplicate",
                  "distinct_same_number", "source_version", "mixed", "unresolved")

#: Numbers that carry no identity of their own.
SENTINEL_NUMBERS = frozenset({"", "0", "-", "--", "n/a", "na", "none", "null",
                              "tbd", "tba", "?"})

#: The positional pseudo-key a scraper assigns when no printed number exists.
AUTO_KEY = re.compile(r"_auto-\d+$")

#: The fields a row fingerprint covers.  Naming them keeps two different rows from
#: ever hashing to the same value by accident.
FINGERPRINT_FIELDS = ("id", "meeting_db_id", "agenda_item_number", "agenda_item_id",
                      "agenda_item_title", "agenda_item_text")


def canonical_fingerprint(row: Mapping[str, Any]) -> str:
    body = {field: (None if row.get(field) is None else str(row.get(field)))
            for field in FINGERPRINT_FIELDS}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _text_sha256(value: Any) -> str:
    return hashlib.sha256((str(value) if value is not None else "").encode("utf-8")
                          ).hexdigest()


def is_sentinel(number: Any) -> bool:
    return str(number or "").strip().lower() in SENTINEL_NUMBERS


def is_auto_key(source_key: Any) -> bool:
    return bool(AUTO_KEY.search(str(source_key or "")))


def classify_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Assign every row in a group exactly one family, by first match."""
    by_identity: dict[tuple, list[int]] = defaultdict(list)
    for row in rows:
        by_identity[(str(row.get("agenda_item_id")),
                     _text_sha256(row.get("agenda_item_title")),
                     _text_sha256(row.get("agenda_item_text")))].append(int(row["id"]))
    duplicates_of: dict[int, int] = {}
    for ids in by_identity.values():
        if len(ids) > 1:
            keeper = min(ids)
            for other in sorted(ids):
                if other != keeper:
                    duplicates_of[other] = keeper

    by_source_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source_key[str(row.get("agenda_item_id"))].append(row)

    classified: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r["id"])):
        row_id = int(row["id"])
        number = row.get("agenda_item_number")
        if is_auto_key(row.get("agenda_item_id")):
            family = "auto_positional"
            reason = ("the source key is a positional pseudo-key; no printed number "
                      "exists for this agenda line")
        elif is_sentinel(number):
            family = "sentinel_number"
            reason = f"the number {number!r} is a sentinel with no identity"
        elif row_id in duplicates_of or any(
                row_id == keeper for keeper in duplicates_of.values()):
            # One meeting, several bodies: each body numbers its own agenda from 1,
            # so the same number under different bodies is legitimately distinct.
            family = "exact_duplicate"
            reason = ("this row shares an identical source key, title and text with "
                      "another row in the group: the same record ingested twice")
        elif len({str(r.get("body")) for r in rows}) > 1:
            family = "multi_body_meeting"
            reason = ("the group spans more than one body, and each body numbers "
                      "its own agenda independently")
        elif len(by_source_key[str(row.get("agenda_item_id"))]) > 1:
            family = "source_version"
            reason = "the same source key appears with different provenance"
        else:
            titles = {_text_sha256(r.get("agenda_item_title")) for r in rows}
            keys = {str(r.get("agenda_item_id")) for r in rows}
            if len(titles) == len(rows) and len(keys) == len(rows):
                family = "distinct_same_number"
                reason = ("a distinct source key and a distinct title: a different "
                          "item that legitimately carries the same number")
            else:
                family = "unresolved"
                reason = "no declared family matched"
        classified.append({
            "id": row_id,
            "family": family,
            "reason": reason,
            "fingerprint": canonical_fingerprint(row),
            "source_key": str(row.get("agenda_item_id")),
            "number": str(number),
            "title_sha256": _text_sha256(row.get("agenda_item_title")),
            "text_sha256": _text_sha256(row.get("agenda_item_text")),
            "meeting_db_id": int(row["meeting_db_id"]),
            "body": str(row.get("body")),
            "jurisdiction_id": row.get("jurisdiction_id"),
            "lifecycle_status": row.get("lifecycle_status"),
            "sort_order": row.get("sort_order"),
            "duplicate_of": duplicates_of.get(row_id),
        })
    return classified


def family_counts(classified: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(row["family"] for row in classified)
    return {family: counts.get(family, 0) for family in FAMILIES}


def group_family(classified: Sequence[Mapping[str, Any]]) -> str:
    families = {row["family"] for row in classified}
    if families <= {"auto_positional", "sentinel_number"}:
        return "placeholder_only"
    if families == {"multi_body_meeting"}:
        return "multi_body_meeting"
    if families == {"exact_duplicate"}:
        return "exact_duplicate"
    if families == {"distinct_same_number"}:
        return "distinct_same_number"
    if families == {"source_version"}:
        return "source_version"
    if "unresolved" in families:
        return "unresolved"
    return "mixed"
