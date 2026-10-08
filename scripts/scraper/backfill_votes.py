#!/usr/bin/env python3
"""
Backfill vote records from meeting minutes PDFs for Destiny/AgendaQuick
jurisdictions (Glendale, El Mirage).

Usage:
    python -m scripts.scraper.backfill_votes glendale [--dry-run] [--limit N] [--body BODY_SLUG]
    python -m scripts.scraper.backfill_votes el-mirage [--dry-run] [--limit N] [--body BODY_SLUG]

Process:
    1. Find meetings that have minute documents as supporting_docs
    2. Extract the meeting date from the minute doc title
    3. Map to the correct meeting record
    4. Download the PDF, run pdftotext
    5. Parse votes (per-member breakdowns)
    6. Persist directly to meeting_members, agenda_item_votes, member_votes
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from argparse import ArgumentParser
from typing import Optional

from sqlalchemy import select

from db import get_session, init_db
from db.meeting_members import AttendanceRecord, reconcile_meeting_members
from db.models import (
    AgendaItem,
    AgendaItemVote,
    Meeting,
    Person,
    MemberVote,
)

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

JURISDICTION_CONFIG: dict[str, dict] = {
    "glendale": {
        "jurisdiction_name": "City of Glendale",
        "default_body_code": "glendale-cc",
    },
    "el-mirage": {
        "jurisdiction_name": "City of El Mirage",
        "default_body_code": "el-mirage-cc",
    },
}


# ── Helpers ──


def download_pdf(url: str, timeout: int = 30) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.warning("Failed to download PDF %s: %s", url, e)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", tmp, "-"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.warning("pdftotext failed: %s", e)
        return None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _name_looks_valid(name: str) -> bool:
    if not name or len(name) < 3 or len(name) > 60:
        return False
    if not re.match(r"^[A-Za-z]", name):
        return False
    if re.search(r"\b(with|and|the|for|of|that|this|from)\b", name, re.I):
        return False
    if re.search(r"^\d", name):
        return False
    return True


def _strip_title(name: str) -> str:
    return re.sub(
        r"^(Mayor|Vice\s+Mayor|Councilmember|Committee\s+Member|Member|Chairperson|Commissioner|Vice\s+Chairperson)\s+",
        "", name,
    ).strip().rstrip(",")


# ── Date extraction from minute doc titles ──


_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

_SHORT_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
    "nov": 11, "dec": 12,
}

# Make _MONTH_NAMES include short names too
for k, v in list(_SHORT_MONTHS.items()):
    if k not in _MONTH_NAMES:
        _MONTH_NAMES[k] = v

_DATE_PATTERNS = [
    # YYYY-MM-DD (El Mirage style)
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "y m d"),
    # YYYY.MM.DD (dot with 4-digit year, e.g. "2026.04.08 DRAFT-LAB Minutes")
    (re.compile(r"(\d{4})\.(\d{2})\.(\d{2})"), "y m d"),
    # Month D, YYYY (text month, full or short)
    (re.compile(r"(January|February|March|April|May|June|July|August|September|"
                r"October|November|December|"
                r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
                r"\s+(\d{1,2}),?\s+(\d{4})", re.I), "m d y"),
    # M/D/YYYY (slash date)
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"), "fmt_md"),
    # M.D.YYYY or D.M.YYYY (dot date)
    (re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})"), "fmt_md"),
    # MM-DD-YYYY (dash date)
    (re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})"), "fmt_md"),
    # MMDD YYYY (4-digit monthday, space, 4-digit year, e.g. "1011 2022")
    (re.compile(r"(\d{2})(\d{2})\s+(\d{4})"), "fmt_mmddyyyy"),
    # M D YYYY or M D YY (space-separated, with/without Draft prefix)
    # This comes AFTER MMDD YYYY so that "1011 2022" isn't split as "10" "11" "2022"
    (re.compile(r"(?:Draft\s+Minutes?\s+)?"
                r"(\d{1,2})\s+(\d{1,2})\s+(\d{2,4})"
                r"(?:\s+Draft\s+Meeting\s+Minutes?)?", re.I), "fmt_mdyy"),
    # MMDDYY (6 consecutive digits)
    (re.compile(r"(\d{2})(\d{2})(\d{2})(?=\D|$)"), "fmt_66"),
    # M-D-YY (dash with 2-digit year)
    (re.compile(r"(\d{1,2})-(\d{1,2})-(\d{2})(?=\D|$)"), "fmt_66"),
    # M/D/YY (slash with 2-digit year)
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2})(?=\D|$)"), "fmt_66"),
    # M.D.YY (dot with 2-digit year)
    (re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2})(?=\D|$)"), "fmt_66"),
]


def _parse_date_from_title(title: str) -> Optional[str]:
    """Try every known date format against a minute document title."""
    for pat, fmt in _DATE_PATTERNS:
        m = pat.search(title)
        if not m:
            continue
        if fmt == "y m d":
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if fmt == "m d y":
            mon = _MONTH_NAMES.get(m.group(1).lower())
            if mon:
                return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
        if fmt == "fmt_md":
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a <= 12 and b <= 31:
                return f"{c:04d}-{a:02d}-{b:02d}"
            if b <= 12 and a <= 31:
                return f"{c:04d}-{b:02d}-{a:02d}"
        if fmt == "fmt_mdyy":
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if c < 100:
                c += 2000
            if a <= 12 and b <= 31 and c >= 2019:
                return f"{c:04d}-{a:02d}-{b:02d}"
            if b <= 12 and a <= 31 and c >= 2019:
                return f"{c:04d}-{b:02d}-{a:02d}"
        if fmt == "fmt_66":
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= a <= 12 and 1 <= b <= 31:
                yr = c + 2000 if c < 50 else c + 1900
                if 2019 <= yr <= 2030:
                    return f"{yr:04d}-{a:02d}-{b:02d}"
        if fmt == "fmt_mmddyyyy":
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= a <= 12 and 1 <= b <= 31 and 2019 <= c <= 2030:
                return f"{c:04d}-{a:02d}-{b:02d}"
    return None


def _parse_glendale_title_date(title: str) -> Optional[str]:
    return _parse_date_from_title(title)


def _parse_elmirage_title_date(title: str) -> Optional[str]:
    return _parse_date_from_title(title)


# ── Glendale minutes vote parser ──

# Title patterns shared between CC and GSC (and other bodies)
_GL_TITLES = r"Mayor|Vice\s+Mayor|Councilmember|Committee\s+Member|Member|Chairperson|Councilmember,?"

_GL_MOTION = re.compile(
    r"A\s+motion\s+was\s+made\s+by\s+(?:" + _GL_TITLES + r")\s+"
    r"([^,]+?)(?:,\s+seconded\s+by\s+(?:" + _GL_TITLES + r")\s+"
    r"([^,]+?))?\s+to\s+(.+?)(?:\.|$)", re.I | re.DOTALL,
)
_GL_AYE_LINE = re.compile(
    r"^\s{0,20}((?:" + _GL_TITLES + r")\s+.+)$", re.MULTILINE,
)
_GL_RESULT = re.compile(r"^\s*(Passed|Failed|Carried|Denied|Withdrawn)", re.MULTILINE)
_GL_CONSENT_RANGE = re.compile(
    r"(?:Consent\s+Agenda\s+)?items?\s+"
    r"(\d+)\s*(?:through|-)\s*(\d+)"
    r"(?:\s+and\s+(\d+)\s*(?:through|-)\s*(\d+))?", re.I,
)


def parse_glendale_minutes_votes(text: str) -> dict:
    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()
    lines = text.split("\n")

    in_roll_call = False
    for line in lines:
        ls = line.strip()
        if "ROLL CALL" in ls.upper() or "Present:" in ls:
            in_roll_call = True
            ls = re.sub(r"^Present:\s*", "", ls)
        if in_roll_call:
            if not ls or ls.startswith("ALSO") or ls.startswith("Also") or ls.startswith("Absent"):
                in_roll_call = False
                continue
            # Try "Title Name" format (CC style)
            m = re.match(
                r"((?:Mayor|Vice\s+Mayor|Councilmember|Committee\s+Member|Chairperson)\s+"
                r"[A-Za-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Za-z]+(?:[-'][A-Za-z]+)?)*)", ls,
            )
            if m:
                name = m.group(1).strip().rstrip(",")
            else:
                # Try "Name, Title" format (GSC style)
                # First strip any "Present:" prefix
                clean = re.sub(r"^Present:\s*", "", ls)
                # Split on comma to find name part before title
                m2 = re.match(r"^(.+?),(?:\s*)(Councilmember|Committee\s+Member|Member|Mayor|Vice\s+Mayor)", clean)
                if m2:
                    name = m2.group(1).strip()
                else:
                    continue
                if name and _name_looks_valid(name):
                    norm = _normalize_name(name)
                    if norm not in seen_sup:
                        seen_sup.add(norm)
                        role = "Councilmember"
                        if name.startswith("Mayor"):
                            role = "Mayor"
                        elif name.startswith("Vice Mayor"):
                            role = "Vice Mayor"
                        clean_name = _strip_title(name)
                        clean_norm = _normalize_name(clean_name)
                        supervisors.append({
                            "name": clean_name, "normalized_name": clean_norm,
                            "role": role, "present": True,
                        })

    # Build item index from full text
    item_num_pattern = re.compile(r"^\s*(\d+)\.\s+(.+)$", re.MULTILINE)
    all_items: list[tuple[int, int, str]] = []
    for m in item_num_pattern.finditer(text):
        num = int(m.group(1))
        title = m.group(2).strip()
        if title and num <= 50:
            all_items.append((m.start(), num, title))

    # Split by motion blocks
    sections = re.split(r"(?=A\s+motion\s+was\s+made)", text)
    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Find motion
        motion_match = _GL_MOTION.search(section)
        if not motion_match:
            continue

        motion_text = motion_match.group(motion_match.lastindex) if motion_match.lastindex else ""

        # Extract AYES
        ayes: list[str] = []
        in_ayes = False
        for line in section.split("\n"):
            ls = line.strip()
            if re.match(r"^AYE:", ls):
                in_ayes = True
                rest = re.sub(r"^AYE:\s*", "", ls).strip()
                if rest:
                    ayes.append(rest)
                continue
            if in_ayes:
                if not ls:
                    continue
                if re.match(r"^(Passed|Failed|Carried|Denied|NAY|Nay|NAZE)", ls):
                    in_ayes = False
                    continue
                m = re.match(
                    r"((?:" + _GL_TITLES + r")\s+"
                    r"[A-Za-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Za-z]+(?:[-'][A-Za-z]+)?)*)", ls,
                )
                if m:
                    name = m.group(1).strip().rstrip(",")
                    if name and _name_looks_valid(name) and name not in ayes:
                        ayes.append(name)

        # Extract NAYS
        nay_lines: list[str] = []
        in_nays = False
        for line in section.split("\n"):
            ls = line.strip()
            if re.match(r"^NAY:", ls) or re.match(r"^NAZE:", ls):
                in_nays = True
                rest = re.sub(r"^(NAY|NAZE):\s*", "", ls).strip()
                if rest:
                    nay_lines.append(rest)
                continue
            if in_nays:
                if not ls:
                    continue
                if re.match(r"^(Passed|Failed|Carried|Denied)", ls):
                    in_nays = False
                    continue
                m = re.match(
                    r"((?:" + _GL_TITLES + r")\s+"
                    r"[A-Za-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Za-z]+(?:[-'][A-Za-z]+)?)*)", ls,
                )
                if m:
                    name = m.group(1).strip().rstrip(",")
                    if name and _name_looks_valid(name) and name not in nay_lines:
                        nay_lines.append(name)

        result_match = _GL_RESULT.search(section)
        result = result_match.group(1) if result_match else "Passed"
        if result.lower() == "carried":
            result = "Passed"
        elif result.lower() == "denied":
            result = "Failed"

        # Build supervisor votes
        supervisor_votes: list[dict] = []
        seen_sv: set[str] = set()
        for name in ayes:
            clean_n = _strip_title(name)
            norm = _normalize_name(clean_n)
            if norm not in seen_sv:
                seen_sv.add(norm)
                supervisor_votes.append({"name": clean_n, "normalized_name": norm, "vote": "yes"})
        for name in nay_lines:
            clean_n = _strip_title(name)
            norm = _normalize_name(clean_n)
            if norm not in seen_sv:
                seen_sv.add(norm)
                supervisor_votes.append({"name": clean_n, "normalized_name": norm, "vote": "no"})

        unanimous = len(nay_lines) == 0 and len(ayes) > 0

        # Determine item numbers
        item_numbers: list[int] = []
        is_consent = bool(_GL_CONSENT_RANGE.search(motion_text))
        if is_consent:
            consent_m = _GL_CONSENT_RANGE.search(motion_text)
            if consent_m:
                start1 = int(consent_m.group(1))
                end1 = int(consent_m.group(2))
                item_numbers = list(range(start1, end1 + 1))
                if consent_m.group(3) and consent_m.group(4):
                    start2 = int(consent_m.group(3))
                    end2 = int(consent_m.group(4))
                    item_numbers.extend(range(start2, end2 + 1))
            else:
                for heading_m in item_num_pattern.finditer(section):
                    num = int(heading_m.group(1))
                    if num not in item_numbers:
                        item_numbers.append(num)
        else:
            for heading_m in item_num_pattern.finditer(section):
                num = int(heading_m.group(1))
                if num not in item_numbers:
                    item_numbers.append(num)

        vote_text = section[:500]
        for item_num in item_numbers:
            votes.append({
                "agenda_item_number": str(item_num),
                "motion_result": "Carried Unanimously" if unanimous else "Carried",
                "supervisor_votes": list(supervisor_votes),
                "vote_text": vote_text,
                "is_split_vote": not unanimous and len(nay_lines) > 0,
                "unanimous": unanimous,
                "majority_position": "yes" if len(ayes) >= len(nay_lines) else "no",
            })

    seen = set()
    deduped = []
    for sup in supervisors:
        key = sup["normalized_name"]
        if key not in seen:
            seen.add(key)
            deduped.append(sup)

    return {"supervisors": deduped, "votes": votes}


# ── El Mirage minutes vote parser ──

# Title patterns for El Mirage (CC + P&Z)
_EM_TITLES_RAW = r"Mayor|Vice\s+Mayor|Councilmember|Commissioner|Chairperson|Vice\s+Chairperson"

_EM_TITLES_GROUP = r"(?:Mayor|Vice\s+Mayor|Councilmember|Commissioner|Chairperson|Vice\s+Chairperson)"

_EM_NAME_PATTERN = r"[A-Za-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Za-z]+(?:[-'][A-Za-z]+)?)*"

_EM_MOTION = re.compile(
    r"(" + _EM_TITLES_GROUP + r"\s+" + _EM_NAME_PATTERN + r")"
    r"\s+moved\s+to\s+(.+?),?\s+seconded\s+by\s+"
    r"(" + _EM_TITLES_GROUP + r"\s+" + _EM_NAME_PATTERN + r")",
    re.I | re.DOTALL,
)

_EM_RESULT = re.compile(
    r"Motion\s+(passed|failed|carried|denied)\s*"
    r"(?:\((\d+)/(\d+)\))?"
    r"(?:\s+NAY\s*-\s*(.+))?",
    re.I,
)

_EM_CONSENT = re.compile(r"[Cc]onsent\s+[Aa]genda")


def parse_elmirage_minutes_votes(text: str) -> dict:
    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()
    lines = text.split("\n")

    # Extract roll call / present members (multi-line semicolon-separated)
    present_block = ""
    in_present = False
    for line in lines:
        ls = line.strip()
        if re.match(r"^Present:", ls):
            in_present = True
            present_block = re.sub(r"^Present:\s*", "", ls)
            continue
        if in_present:
            if re.match(r"^\d+\.\s+", ls) or "CALL TO ORDER" in ls.upper():
                in_present = False
            else:
                present_block += " " + ls

    if present_block:
        for part in re.split(r";\s*", present_block):
            part = part.strip().rstrip(".,;")
            if not part:
                continue
            # Try "Title Name" format (CC style): e.g. "Councilmember Donna Winston"
            m = re.match(
                r"((?:" + _EM_TITLES_RAW + r")\s+"
                r"[A-Za-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Za-z]+(?:[-'][A-Za-z]+)?)*)", part,
            )
            if m:
                name = m.group(1).strip()
            else:
                # Try "Name, Title" format (P&Z style): e.g. "Brian Campbell-Sanderfield, Commissioner"
                cm = re.match(
                    r"([A-Za-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Za-z]+(?:[-'][A-Za-z]+)?)*),\s*"
                    r"(Commissioner|Chairperson|Vice\s+Chairperson|Alternate\s+Commissioner|Alternate)",
                    part,
                )
                if cm:
                    raw_name = cm.group(1).strip()
                    role = cm.group(2)
                    if "Chairperson" in role or "Commissioner" in role:
                        role = "Commissioner"
                    if _name_looks_valid(raw_name):
                        norm = _normalize_name(raw_name)
                        if norm not in seen_sup:
                            seen_sup.add(norm)
                            supervisors.append({
                                "name": raw_name, "normalized_name": norm,
                                "role": role, "present": True,
                            })
                    continue
                else:
                    continue
            
            # From the "Title Name" branch
            if name and _name_looks_valid(name):
                norm = _normalize_name(name)
                if norm not in seen_sup:
                    seen_sup.add(norm)
                    role = "Councilmember"
                    if name.startswith("Mayor"):
                        role = "Mayor"
                    elif name.startswith("Vice Mayor"):
                        role = "Vice Mayor"
                    elif name.startswith("Commissioner") or name.startswith("Chairperson"):
                        role = "Commissioner"
                    clean_name = _strip_title(name)
                    clean_norm = _normalize_name(clean_name)
                    supervisors.append({
                        "name": clean_name, "normalized_name": clean_norm,
                        "role": role, "present": True,
                    })

    # Build last-name lookup for NAY resolution
    known_sup_short: dict[str, str] = {}
    for s in supervisors:
        for p in s["name"].split():
            pnorm = _normalize_name(p)
            known_sup_short[pnorm] = s["name"]

    # Build item index
    item_num_pat = re.compile(r"^\s*(\d+)\.\s+(.+)$", re.MULTILINE)
    all_items: list[tuple[int, int, str]] = []
    for m in item_num_pat.finditer(text):
        num = int(m.group(1))
        title = m.group(2).strip()
        if title and num <= 15:
            all_items.append((m.start(), num, title))

    # Process motion-result pairs on the full text
    for m in _EM_MOTION.finditer(text):
        motion_start = m.start()
        motion_end = m.end()
        motion_text = m.group(0)

        result_match = _EM_RESULT.search(text, motion_end)
        if not result_match:
            continue

        is_passed = result_match.group(1).lower() in ("passed", "carried")
        nay_raw = result_match.group(4) if result_match.lastindex and result_match.lastindex >= 4 else None

        # Parse NAY names by matching against known supervisor last names
        nays: list[str] = []
        if nay_raw:
            cleaned = re.sub(r"\s*(?:Councilmember|Mayor|Vice\s+Mayor|Commissioner|Chairperson|Vice\s+Chairperson)\s*", " ", nay_raw).strip()
            for part in re.split(r"[,;]+", cleaned):
                part = part.strip()
                if not part:
                    continue
                pnorm = _normalize_name(part)
                if pnorm in known_sup_short:
                    full = known_sup_short[pnorm]
                    fnorm = _normalize_name(full)
                    if fnorm not in {_normalize_name(n) for n in nays}:
                        nays.append(full)

        # AYES = present minus nays
        ayes_names: list[str] = []
        all_present = [s["name"] for s in supervisors]
        nay_norms = {_normalize_name(n) for n in nays}
        for name in all_present:
            norm = _normalize_name(name)
            if norm not in nay_norms:
                ayes_names.append(name)

        # Build supervisor votes (dedup)
        sv_list: list[dict] = []
        seen_sv: set[str] = set()
        for name in ayes_names:
            norm = _normalize_name(name)
            if norm not in seen_sv:
                seen_sv.add(norm)
                sv_list.append({"name": name, "normalized_name": norm, "vote": "yes"})
        for name in nays:
            norm = _normalize_name(name)
            if norm not in seen_sv:
                seen_sv.add(norm)
                sv_list.append({"name": name, "normalized_name": norm, "vote": "no"})

        unanimous = len(nays) == 0

        # Determine item number
        item_numbers: list[int] = []
        if _EM_CONSENT.search(motion_text):
            for pos, num, title in all_items:
                if "consent" in title.lower():
                    item_numbers.append(num)
                    break
            if not item_numbers:
                item_numbers.append(6)
        else:
            best_num = None
            best_dist = float("inf")
            for pos, num, title in all_items:
                if pos < motion_start:
                    dist = motion_start - pos
                    if dist < best_dist:
                        best_dist = dist
                        best_num = num
            if best_num:
                item_numbers.append(best_num)

        if not item_numbers:
            continue

        ctx = text[motion_start:min(len(text), motion_start + 300)]
        for item_num in item_numbers:
            votes.append({
                "agenda_item_number": str(item_num),
                "motion_result": "Carried Unanimously" if unanimous else "Carried",
                "supervisor_votes": list(sv_list),
                "vote_text": ctx[:500],
                "is_split_vote": not unanimous,
                "unanimous": unanimous,
                "majority_position": "yes" if len(ayes_names) >= len(nays) else "no",
            })

    seen = set()
    deduped = []
    for sup in supervisors:
        key = sup["normalized_name"]
        if key not in seen:
            seen.add(key)
            deduped.append(sup)

    return {"supervisors": deduped, "votes": votes}


# ── Persist logic ──


def _persist_minutes_votes(
    session, body: str, meeting_id: str, meeting_db_id: int,
    supervisors: list[dict], votes: list[dict],
) -> int:
    """Replace minutes vote records while reconciling stable attendance rows.

    Vote and member-vote rows are derived from the current minutes parse and
    are deliberately replaced.  Meeting-member rows can be graph provenance,
    so their stable IDs must survive a later parse of the same meeting.
    """
    count = 0

    # Vote rows are a replaceable snapshot of this minutes parse.  Attendance
    # is reconciled below instead of being deleted, preserving provenance IDs.
    subq = select(AgendaItemVote.id).where(
        AgendaItemVote.body == body, AgendaItemVote.meeting_id == meeting_id,
    ).scalar_subquery()
    session.execute(MemberVote.__table__.delete().where(
        MemberVote.agenda_item_vote_id.in_(subq),
    ))
    session.execute(AgendaItemVote.__table__.delete().where(
        AgendaItemVote.body == body, AgendaItemVote.meeting_id == meeting_id,
    ))
    session.flush()

    # Upsert Person records
    person_map: dict[str, int] = {}
    for sup in supervisors:
        norm = sup.get("normalized_name")
        name = sup.get("name", "")
        if not norm or not name:
            continue
        existing = session.execute(select(Person).where(Person.normalized_name == norm)).scalar_one_or_none()
        if existing:
            person_map[norm] = existing.id
        else:
            p = Person(name=name, normalized_name=norm)
            session.add(p)
            session.flush()
            person_map[norm] = p.id

    # Reconcile attendance by the stable meeting/member key.  The reconciler
    # retains members omitted by a later parse, because omission is not proof
    # that historical attendance was false and graph evidence may cite it.
    attendance: list[AttendanceRecord] = []
    for sup in supervisors:
        norm = sup.get("normalized_name")
        if norm not in person_map:
            continue
        attendance.append(AttendanceRecord(
            member_id=person_map[norm],
            role=sup.get("role"),
            present=sup.get("present", True),
        ))
    reconcile_meeting_members(
        session,
        body=body,
        meeting_id=meeting_id,
        meeting_db_id=meeting_db_id,
        attendance=attendance,
    )

    # Insert agenda_item_votes
    seen_nums: set[str] = set()
    for vote in votes:
        item_num = str(vote.get("agenda_item_number", "0"))
        if item_num in seen_nums:
            continue
        seen_nums.add(item_num)

        db_item = session.execute(select(AgendaItem).where(
            AgendaItem.body == body, AgendaItem.meeting_id == meeting_id,
            AgendaItem.agenda_item_number == item_num,
        ).limit(1)).scalar_one_or_none()

        aiv = AgendaItemVote(
            body=body,
            agenda_item_id=db_item.id if db_item else -1,
            meeting_id=meeting_id,
            meeting_db_id=meeting_db_id,
            agenda_item_number=item_num,
            motion_result=vote.get("motion_result", "approved"),
            vote_text=(vote.get("vote_text") or "")[:2000],
            is_split_vote=vote.get("is_split_vote", False),
            unanimous=vote.get("unanimous", True),
            majority_position=vote.get("majority_position", "yes"),
        )
        session.add(aiv)
        session.flush()

        # Insert supervisor_votes
        for sv in vote.get("supervisor_votes", []):
            sv_norm = sv.get("normalized_name")
            if sv_norm and sv_norm not in person_map:
                existing = session.execute(select(Person).where(Person.normalized_name == sv_norm)).scalar_one_or_none()
                if existing:
                    person_map[sv_norm] = existing.id
                else:
                    p = Person(name=sv.get("name", ""), normalized_name=sv_norm)
                    session.add(p)
                    session.flush()
                    person_map[sv_norm] = p.id
            sv_rec = MemberVote(
                body=body,
                agenda_item_vote_id=aiv.id,
                member_id=person_map.get(sv_norm, 0),
                vote=sv.get("vote", "yes"),
            )
            session.add(sv_rec)
        count += 1

    session.flush()
    return count


# ── Main backfill logic ──


def find_minute_docs(session, jurisdiction_name: str) -> list[dict]:
    from sqlalchemy import text as sa_text

    is_glendale = jurisdiction_name.lower() == "glendale"
    is_elmirage = jurisdiction_name.lower() == "el-mirage"

    config = JURISDICTION_CONFIG.get(jurisdiction_name.lower())
    if not config:
        raise ValueError(f"Unknown jurisdiction: {jurisdiction_name}")
    db_jname = config["jurisdiction_name"]

    rows = session.execute(sa_text("""
        SELECT m.id AS meeting_db_id, m.meeting_id, m.body, m.meeting_date,
               sd.document_title, sd.document_url
        FROM supporting_documents sd
        JOIN meetings m ON sd.meeting_db_id = m.id
        WHERE m.jurisdiction_id = (SELECT id FROM jurisdictions WHERE name = :jname)
          AND LOWER(sd.document_title) LIKE '%minute%'
          AND sd.document_url LIKE '%.pdf'
        ORDER BY m.meeting_date DESC
    """), {"jname": db_jname}).fetchall()

    results = []
    for row in rows:
        if is_glendale:
            target_date = _parse_glendale_title_date(row.document_title)
        elif is_elmirage:
            target_date = _parse_elmirage_title_date(row.document_title)
        else:
            target_date = None
        results.append({
            "meeting_db_id": row.meeting_db_id,
            "meeting_id": row.meeting_id,
            "body_code": row.body,
            "meeting_date": row.meeting_date,
            "doc_title": row.document_title,
            "doc_url": row.document_url,
            "target_date": target_date,
        })
    return results


def find_target_meeting(session, body_code: str, target_date: str) -> tuple[Optional[str], Optional[int]]:
    """Find a meeting by body code + date.

    Returns (meeting_id VARCHAR, meeting_db_id INTEGER) or (None, None).
    """
    row = session.execute(
        select(Meeting.meeting_id, Meeting.id).where(
            Meeting.body == body_code, Meeting.meeting_date == target_date,
        ).limit(1)
    ).fetchone()
    return (row.meeting_id, row.id) if row else (None, None)


def backfill_jurisdiction(
    jurisdiction_name: str, dry_run: bool = False,
    limit: Optional[int] = None, body_filter: Optional[str] = None,
) -> int:
    init_db()
    session = get_session()

    docs = find_minute_docs(session, jurisdiction_name)
    log.info("Found %d minute documents for %s", len(docs), jurisdiction_name)

    if body_filter:
        docs = [d for d in docs if d["body_code"] == body_filter]
        log.info("Filtered to body %s: %d remaining", body_filter, len(docs))
    if limit:
        docs = docs[:limit]

    is_glendale = jurisdiction_name.lower() == "glendale"
    parse_fn = parse_glendale_minutes_votes if is_glendale else parse_elmirage_minutes_votes

    processed = 0
    skipped_no_target = 0
    skipped_no_votes = 0
    errors = 0

    for doc in docs:
        target_date = doc.get("target_date")
        if not target_date:
            log.info("  SKIP %s (%s): no target date", doc["doc_title"], doc["body_code"])
            skipped_no_target += 1
            continue

        target_mid, target_db_id = find_target_meeting(session, doc["body_code"], target_date)
        if not target_mid or not target_db_id:
            log.info("  SKIP %s: no meeting for %s on %s", doc["doc_title"], doc["body_code"], target_date)
            skipped_no_target += 1
            continue

        log.info("  %s: %s -> meeting %s (db_id=%d), downloading...",
                 doc["doc_title"], target_date, target_mid, target_db_id)

        if dry_run:
            processed += 1
            continue

        try:
            pdf_bytes = download_pdf(doc["doc_url"])
            if not pdf_bytes:
                log.warning("    download failed")
                errors += 1
                continue
            text = extract_pdf_text(pdf_bytes)
            if not text:
                log.warning("    pdftotext returned empty")
                errors += 1
                continue

            vote_data = parse_fn(text)
            if not vote_data.get("votes"):
                log.info("    no votes found")
                skipped_no_votes += 1
                continue

            # Use the target meeting's own meeting_db_id, NOT the supporting doc's
            # parent meeting ID.  Minute documents from a past meeting are often
            # attached to the NEXT meeting's agenda for approval, so doc["meeting_db_id"]
            # points to the wrong meeting.
            c = _persist_minutes_votes(session, doc["body_code"], target_mid, target_db_id,
                                       vote_data["supervisors"], vote_data["votes"])
            session.commit()
            log.info("    %d vote(s) persisted (%d supervisors)", c, len(vote_data["supervisors"]))
            processed += 1

        except Exception as e:
            log.error("    Error: %s", e, exc_info=True)
            errors += 1

    session.close()
    log.info("Done: %d processed, %d skipped (no target), %d skipped (no votes), %d errors",
             processed, skipped_no_target, skipped_no_votes, errors)
    return processed


# ── CLI ──


def parse_args(argv: list[str] | None = None) -> object:
    p = ArgumentParser(description="Backfill votes from meeting minutes PDFs")
    p.add_argument("jurisdiction", choices=["glendale", "el-mirage"], help="Jurisdiction")
    p.add_argument("--dry-run", action="store_true", help="Don't persist")
    p.add_argument("--limit", type=int, default=None, help="Max meetings")
    p.add_argument("--body", type=str, default=None, help="Body slug filter")
    p.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    return backfill_jurisdiction(args.jurisdiction, dry_run=args.dry_run, limit=args.limit, body_filter=args.body)


if __name__ == "__main__":
    sys.exit(main())
