#!/usr/bin/env python3
"""``buckeye_agenda_parse.py`` — Buckeye agenda packet item parsing.

Split out of ``buckeye_granicus.py`` so that module stays under the 500-line
limit and so the parsing decisions live next to their tests.  The text-line
parser is unchanged; the positioned-block entry point is new and is what fixes
split labels and resolution-number capture.

``parse_agenda_pdf_items`` keeps its exact signature and behaviour — callers that
import it from ``buckeye_granicus`` still get it, re-exported.
"""

from __future__ import annotations

import re

from scraper.platforms import granicus_agenda_blocks as blocks

__all__ = ["parse_agenda_blocks", "parse_agenda_pdf_items"]

def _is_pdf_packet_noise(text: str) -> bool:
    """Return True if *text* looks like supporting-document noise, not a real
    agenda item title. Packet PDFs bundle the agenda with attachments (engineering
    plans, development code extracts, landscaping specs, parcel lists, etc.) that
    often contain numbered fragments that resemble item titles."""
    up = text.upper()
    # Mid-paragraph continuations (lowercase start) — always noise
    if text and text[0].islower():
        return True
    # All-caps engineering/construction/legal boilerplate.
    # Two tiers: short uppercase fragments (section headers like "MINUTES") are
    # kept; long uppercase lines (engineering specs, fire codes, street specs)
    # are noise.
    alpha_chars = [ch for ch in text if ch.isalpha()]
    caps_ratio = sum(1 for ch in alpha_chars if ch.isupper()) / max(len(alpha_chars), 1)
    if caps_ratio >= 0.85:
        # Long all-caps = definitely noise (plan specs, code text)
        if len(text) > 25:
            return True
        # Shorter all-caps: only reject if it contains engineering/legal keywords
        engineering_kw = ["CONTRACTOR", "SHALL", "PIPE", "TRENCH", "WARRANTY",
                          "INSTALL", "FOOTING", "REINFORCE", "CONSTRUCTION",
                          "DRAINAGE", "PAVEMENT", "EMITTER", "FABRICATE",
                          "SUBMITTAL", "SPECIFICATION", "REMOTE CONTROL",
                          "VALVE", "MAINLINE", "IRRIGATION", "LANDSCAP",
                          "LANDSCAPE", "SITE PLAN", "GRADING", "EXCAVAT",
                          "RETAINING", "ELEVATION", "FIRE HYDRANT",
                          "FIRE APPARATUS", "FIRE STATION", "FIRE SPRINKLER",
                          "STREET", "SHRUBBERY", "RIGHT OF WAY", "EXISTING"]
        if any(kw in up for kw in engineering_kw):
            return True
    # Owner/parcel lists (property owner names in all-caps)
    if re.search(r"\bTRUST\b", up) and re.search(r"\bLIVING\b|\bFAMILY\b|\bREVOCABLE\b", up):
        return True
    if re.search(r"\bLP\b|\bLLC\b|\bINC\b|\bTRS\b", up) and len(text.split()) >= 3:
        return True
    # Table of contents artifacts like "of this CMP for more information"
    if re.search(r"\b(?:CMP|BDC)\b", up) and len(text) < 80:
        return True
    # Construction-detail fragments ending mid-sentence
    if text.endswith(":") and caps_ratio >= 0.7:
        return True
    return False


def parse_agenda_pdf_items(text: str, meeting_id: str) -> list[dict]:
    items: list[dict] = []
    sort_order = 0
    pending_num: Optional[str] = None
    lines = text.split("\n") if text else []
    in_listing = False

    def emit_item(num, title, raw=""):
        nonlocal sort_order
        sort_order += 1
        items.append({"meeting_id": meeting_id, "agenda_item_number": num,
            "item_type_category": "item", "section_level": 0,
            "agenda_item_title": title, "agenda_item_text": raw or title,
            "sort_order": sort_order})

    def emit_section(t):
        nonlocal sort_order
        sort_order += 1
        items.append({"meeting_id": meeting_id, "agenda_item_number": "",
            "item_type_category": "section", "section_level": 1,
            "agenda_item_title": t.rstrip(".").strip(),
            "agenda_item_text": t, "sort_order": sort_order})

    for line in lines:
        s = line.strip()
        if not s:
            continue
        up = s.upper()

        if "CONSENT AGENDA ITEMS" in up or "CONSENT AGENDA / NEW BUSINESS" in up:
            pending_num = None; emit_section(s); in_listing = True; continue
        if "NON CONSENT" in up and ("AGENDA" in up or "ITEMS" in up or "BUSINESS" in up):
            pending_num = None; emit_section(s); in_listing = True; continue
        if ("CALL TO ORDER" in up or "ADJOURNMENT" in up or "EXECUTIVE SESSION" in up) and len(s) < 60:
            pending_num = None; emit_section(s); continue

        # Item number on its own line: *4.A
        m = re.match(r"^(\*?)(\d+\.[A-Z])\.?\s*$", s)
        if m:
            if pending_num:
                emit_item(pending_num, "(see details)")
            pending_num = m.group(2)
            continue

        # Item on same line: *4.A  Council to take action...
        m = re.match(r"^(\*?)(\d+\.[A-Za-z])\.?\s+(.+)$", s)
        if m:
            pending_num = None
            emit_item(m.group(2), m.group(3).strip(), s)
            continue

        # Pending number with action text on next line
        if pending_num and (s.startswith("Council to ") or s.startswith("No action was taken")):
            emit_item(pending_num, s, s)
            pending_num = None
            continue

        # Simple numbered sections — accepts only lines that look like real agenda
        # items, not supporting-document boilerplate (engineering specs, code
        # extracts, plan notes, parcel lists, etc.).
        if not in_listing:
            m = re.match(r"^\s*(\d+)\.\s+(.+)$", s)
            if m and len(s) < 100:
                title_text = m.group(2).strip()
                # Skip lines that are supporting-document boilerplate:
                # mostly-uppercase engineering/legal text, property owner names,
                # ordinance codes, or address-like fragments.
                if _is_pdf_packet_noise(title_text):
                    continue
                pending_num = None
                sort_order += 1
                items.append({"meeting_id": meeting_id, "agenda_item_number": m.group(1),
                    "item_type_category": "section", "section_level": 0,
                    "agenda_item_title": title_text,
                    "agenda_item_text": s, "sort_order": sort_order})

    if pending_num:
        emit_item(pending_num, "(see details)")

    return items
