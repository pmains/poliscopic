#!/usr/bin/env python3
"""``stage2_s2_evidence_materialize.py`` — what the retained text actually proves.

A supporting document's item number is a claim.  This module asks whether the
document's own retained text *states* that item label, and if so where.

Two shapes carry an authoritative label, and only these two:

* a line that is nothing but the dotted label (``2.C``) — the first line of a
  Board Action Report;
* an ``AGENDA ITEM: 2.C. ...`` header, which also supplies the item's title.

Everything else — exhibits, notices, minutes, presentations, feasibility reports
— is an attachment.  An attachment is evidence that *a* document belongs to an
item; it is not evidence that the item number itself is right.  Attachments
therefore never materialise an item on their own, and nothing is ever inferred
from document order.

The output binds exact coordinates (offset and length into the retained text) and
a sha256 of the exact span, so a reviewer can re-read the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

__all__ = [
    "WITNESS_ROW_FIELDS",
    "content_sha256",
    "document_row_fingerprint",
    "ADDON_ENTRY",
    "ADDON_HEADER",
    "LABEL_ONLY",
    "AGENDA_ITEM_HEADER",
    "classify_document",
    "evidence_for",
    "fingerprint_span",
    "group_holds",
    "materialization_verdicts",
]

#: A line that is only a dotted label.
LABEL_ONLY = re.compile(r"(?m)^[ \t]*(\d{1,2}\.[A-Z]{1,3})[ \t]*$")
#: An "AGENDA ITEM: 2.C." header, which also names the item.
AGENDA_ITEM_HEADER = re.compile(
    r"AGENDA\s+ITEM:\s*(\d{1,2}\.[A-Z]{1,3})\.", re.IGNORECASE)

#: An add-on entry in a Phoenix-style packet listing: "*107 <title>".
ADDON_ENTRY = re.compile(r"(?m)^\*[ \t]*(\d{1,3})[ \t]+(\S.*)$")
#: The packet's revision block: "Item Added: 107".
ADDON_HEADER = re.compile(r"(?im)^\s*Item\s+Added:\s*(\d{1,3})\s*$")

#: The header carries the title after the label, up to the next field.
_TITLE_STOP = re.compile(
    r"(?:\n\s*(?:DATE PREPARED|MEETING DATE|DATE:|DEPARTMENT|DISTRICT|"
    r"ACTION REQUESTED|RECOMMENDATION|FINANCIAL IMPACT|BACKGROUND|"
    r"ATTACHMENTS?|FISCAL IMPACT|STAFF|PRESENTER)\b)|(?:\n\s*\n)")

#: Kinds of document, by the shape of their retained text.
KIND_BOARD_ACTION_REPORT = "board_action_report"
KIND_RESOLUTION = "resolution_or_notice"
KIND_ATTACHMENT = "attachment"


#: The document-row fields a witness fingerprint covers.  A witness is only
#: trustworthy if the row it was read from has not changed, so the fingerprint
#: names the fields rather than hashing whatever happens to be present.
WITNESS_ROW_FIELDS = ("id", "meeting_db_id", "body", "agenda_item_number",
                      "document_url", "file_name", "document_title")


def document_row_fingerprint(row: Mapping[str, Any]) -> str:
    """Canonical sha256 of the witness document row as it is now."""
    body = {field: (None if row.get(field) is None else str(row.get(field)))
            for field in WITNESS_ROW_FIELDS}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def content_sha256(text: str) -> str:
    """sha256 of the retained text a claim is read from."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _span(text: str, start: int, end: int, kind: str) -> dict[str, Any]:
    return {"kind": kind, "start": start, "end": end,
            "span": text[start:end], "sha256": fingerprint_span(text, start, end)}


def fingerprint_span(text: str, start: int, end: int) -> str:
    """sha256 of the exact bytes a claim rests on."""
    return hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()


def classify_document(text: str) -> str:
    """What shape is this document's retained text?"""
    if not text:
        return KIND_ATTACHMENT
    if AGENDA_ITEM_HEADER.search(text):
        return KIND_BOARD_ACTION_REPORT
    if re.search(r"(?im)^\s*(RESOLUTION|NOTICE OF|NOTICE OF PUBLIC HEARING)\b", text):
        return KIND_RESOLUTION
    return KIND_ATTACHMENT


def _title_region(text: str, start: int) -> tuple[str, int, int]:
    """The header's title as ``(text, raw_start, raw_end)``.

    The title is returned with whitespace collapsed, but the offsets point at the
    RAW span, because the stored text wraps titles across lines and a collapsed
    string cannot be found in it by searching.
    """
    tail = text[start:start + 900]
    stop = _TITLE_STOP.search(tail)
    region = tail[:stop.start()] if stop else tail
    if not region.strip():
        return "", start, start
    lead = len(region) - len(region.lstrip())
    trail = len(region.rstrip())
    raw = text[start + lead:start + trail]
    return " ".join(raw.split()).strip(" -–—."), start + lead, start + trail


def _title_from_header(text: str, start: int) -> str:
    return _title_region(text, start)[0]


def addon_evidence(text: str, item_number: str) -> dict[str, Any] | None:
    """Evidence that an item was ADDED to an agenda, from the packet's own text.

    Requires two independent statements in the same retained text: the revision
    block naming ``Item Added: <n>``, and a ``*<n> <title>`` entry in the item
    listing.  The asterisk marks an add-on in these packets.  Both are printed in
    the source; neither is inferred from position.
    """
    number = str(item_number).strip()
    header = None
    for match in ADDON_HEADER.finditer(text):
        if match.group(1) == number:
            header = match
            break
    if header is None:
        return None
    entry = None
    for match in ADDON_ENTRY.finditer(text):
        if match.group(1) == number:
            entry = match
            break
    if entry is None:
        return None
    # The precise evidence is the listing entry; the revision header is bound
    # separately.  A single span from one to the other would be 18KB of packet
    # and prove nothing about the item.
    start, end = entry.start(), entry.end()
    title = " ".join(entry.group(2).split())
    title_offset = entry.start(2)
    return {
        "kind": "addon_entry",
        "start": start,
        "end": end,
        "span": text[start:end],
        "span_revision": text[header.start():header.end()],
        "entry_token": entry.group(0)[:len(entry.group(0)) - len(entry.group(2))].rstrip(),
        "title_offset": title_offset,
        "sha256": fingerprint_span(text, start, end),
        "corroboration": "revision header 'Item Added: <n>' at "
                         f"offset {header.start()}",
        "header_offset": header.start(),
        "header_sha256": fingerprint_span(text, header.start(), header.end()),
        "entry_offset": entry.start(),
        "entry_sha256": fingerprint_span(text, entry.start(), entry.end()),
        "title": title,
    }


def evidence_for(row: Mapping[str, Any]) -> dict[str, Any]:
    """The retained-text evidence for one supporting document.

    ``states_label`` is the label the document's own text asserts, or None.  A
    document may assert a label that disagrees with its recorded item number;
    that disagreement is reported rather than smoothed over.
    """
    text = str(row.get("text_content") or "")
    kind = classify_document(text)
    label = None
    coordinates: dict[str, Any] | None = None
    title = ""

    header = AGENDA_ITEM_HEADER.search(text)
    if header:
        label = header.group(1)
        title, _title_start, _title_end = _title_region(text, header.end())
        coordinates = {
            "kind": "agenda_item_header",
            "start": header.start(),
            "end": header.end(),
            "span": text[header.start():header.end()],
            "sha256": fingerprint_span(text, header.start(), header.end()),
            "title_start": _title_start,
            "title_end": _title_end,
        }
    else:
        line = LABEL_ONLY.search(text)
        if line and line.start() < 200:
            label = line.group(1)
            coordinates = {
                "kind": "label_line",
                "start": line.start(),
                "end": line.end(),
                "span": text[line.start():line.end()],
                "sha256": fingerprint_span(text, line.start(), line.end()),
            }

    number_span = None
    title_span = None
    revision_span = None
    listing_span = None
    if coordinates is not None:
        number_span = _span(text, coordinates["start"], coordinates["end"],
                            coordinates["kind"] + ":number")
    if header is not None and title and coordinates.get("title_end") is not None:
        title_span = _span(text, coordinates["title_start"], coordinates["title_end"],
                           "agenda_item_header:title")

    recorded = str(row.get("item_number") or "").strip()
    addon = addon_evidence(text, recorded) if recorded else None
    if addon is not None and label is None:
        label = recorded
        title = addon["title"]
        coordinates = {k: addon[k] for k in
                       ("kind", "start", "end", "span", "sha256")}
        coordinates["header_offset"] = addon["header_offset"]
        coordinates["header_sha256"] = addon["header_sha256"]
        coordinates["entry_offset"] = addon["entry_offset"]
        coordinates["entry_sha256"] = addon["entry_sha256"]
        # An add-on needs BOTH statements bound: the revision header that records
        # the item was added, and the listing entry that carries its title.
        revision_span = _span(text, addon["header_offset"],
                              addon["header_offset"] + len(addon["span_revision"]),
                              "addon:revision")
        listing_span = _span(text, addon["start"], addon["end"], "addon:listing")
        number_span = _span(text, addon["entry_offset"],
                            addon["entry_offset"] + len(addon["entry_token"]),
                            "addon:number")
        title_span = _span(text, addon["title_offset"],
                           addon["title_offset"] + len(addon["title"]), "addon:title")
    return {
        "document_id": int(row["id"]),
        "meeting_db_id": int(row["meeting_db_id"]),
        "body": row.get("body"),
        "recorded_item": recorded,
        "kind": kind,
        "states_label": label,
        "agrees": bool(label) and label == recorded,
        "title": title,
        "coordinates": coordinates,
        "number_span": number_span,
        "title_span": title_span,
        "revision_span": revision_span,
        "listing_span": listing_span,
        "document_row_fingerprint": document_row_fingerprint(row),
        "content_sha256": content_sha256(text),
        "has_text": bool(text),
        "text_length": len(text),
    }


def group_holds(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[int, str], list[dict]]:
    """Group evidence by (meeting, recorded item number)."""
    groups: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        evidence = evidence_for(row)
        groups.setdefault((evidence["meeting_db_id"], evidence["recorded_item"]), []).append(evidence)
    return groups


def materialization_verdicts(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One verdict per (meeting, item) group: materialisable, or held and why.

    A group is materialisable when at least one of its documents states the label
    in its own retained text, and that label agrees with the recorded number.
    That document supplies the item number and title; the other documents in the
    group are attachments that then resolve.
    """
    verdicts = []
    for (meeting, item), documents in sorted(group_holds(rows).items()):
        witnesses = [d for d in documents if d["agrees"]]
        disagreements = [d for d in documents if d["states_label"] and not d["agrees"]]
        attachments = [d for d in documents if not d["states_label"]]

        if witnesses:
            primary = sorted(witnesses, key=lambda d: (
                0 if d["kind"] == KIND_BOARD_ACTION_REPORT else 1, d["document_id"]))[0]
            verdicts.append({
                "meeting_db_id": meeting,
                "item_number": item,
                "verdict": "materialisable",
                "witness_document_id": primary["document_id"],
                "witness_kind": primary["kind"],
                "title": primary["title"],
                "coordinates": primary["coordinates"],
                "documents": [d["document_id"] for d in documents],
                "attachments": [d["document_id"] for d in attachments],
                "reasons": [],
            })
        else:
            reasons = []
            if disagreements:
                reasons.append(
                    "document text states a different label: " +
                    ", ".join(f"{d['document_id']} says {d['states_label']!r}"
                              for d in disagreements))
            if not any(d["has_text"] for d in documents):
                reasons.append("no retained text for any document in the group")
            elif attachments:
                reasons.append(
                    f"{len(attachments)} document(s) are attachments with no "
                    f"label of their own")
            verdicts.append({
                "meeting_db_id": meeting,
                "item_number": item,
                "verdict": "held",
                "witness_document_id": None,
                "witness_kind": None,
                "title": "",
                "coordinates": None,
                "documents": [d["document_id"] for d in documents],
                "attachments": [d["document_id"] for d in attachments],
                "reasons": reasons,
            })
    return verdicts
