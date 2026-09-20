"""
City of Chandler meeting and agenda extraction via Destiny (AgendaQuick).

Uses ``scraper.destiny_common`` (HTML-parser-based) for all parsing.
"""

from __future__ import annotations
import logging
import re
from typing import Optional

from scraper.platforms.destiny_common import (
    BASE_URL,
    build_month_url as _build_month_url,
    extract_meeting_type,
    fetch_page,
    parse_agenda_items as _parse_agenda_items,
    parse_meetings as _parse_meetings,
)

log = logging.getLogger(__name__)

PUBLIC_BODY_CODE = "chandler-cc"
CHANDLER_ID = "24263"
CHANDLER_ORG_ID = 24263

DEFAULT_BODY_SLUGS = ["chandler-city-council"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ── Body map ──

BODY_MAP: dict[str, tuple[str, str]] = {
    "city council": ("chandler-city-council", "chandler-cc"),
    "planning and zoning": ("chandler-planning-zoning", "chandler-pz"),
    "planning & zoning": ("chandler-planning-zoning", "chandler-pz"),
    "design review": ("chandler-design-review", "chandler-drc"),
    "board of adjustment": ("chandler-board-of-adjustment", "chandler-boa"),
    "historic preservation": ("chandler-historic-preservation", "chandler-hpc"),
    "parks and rec": ("chandler-parks-rec", "chandler-prb"),
    "parks & rec": ("chandler-parks-rec", "chandler-prb"),
    "library board": ("chandler-library-board", "chandler-lb"),
    "arts commission": ("chandler-arts-commission", "chandler-arts"),
    "transportation commission": ("chandler-transportation", "chandler-tc"),
    "industrial development": ("chandler-ida", "chandler-ida"),
    "military and veterans": ("chandler-military-veterans", "chandler-mvc"),
    "housing and human services": ("chandler-housing-human-services", "chandler-hhsc"),
    "human relations": ("chandler-human-relations", "chandler-hrc"),
    "domestic violence": ("chandler-domestic-violence", "chandler-dvc"),
    "public housing authority": ("chandler-public-housing", "chandler-pha"),
    "neighborhood advisory": ("chandler-neighborhood-advisory", "chandler-nac"),
    "youth commission": ("chandler-youth-commission", "chandler-yc"),
    "disabilities committee": ("chandler-disabilities-committee", "chandler-pdc"),
    "economic development": ("chandler-economic-development", "chandler-eda"),
    "psprs fire": ("chandler-psprs-fire", "chandler-psprs-f"),
    "psprs police": ("chandler-psprs-police", "chandler-psprs-p"),
    "housing corporation": ("chandler-housing-corp", "chandler-hcc"),
    "citizens panel review": ("chandler-citizens-panel-review", "chandler-cpr"),
    "museum foundation": ("chandler-museum-foundation", "chandler-mf"),
    "cultural foundation": ("chandler-cultural-foundation", "chandler-cf"),
    "health care trust": ("chandler-health-care-trust", "chandler-hct"),
    "workers comp trust": ("chandler-workers-comp-trust", "chandler-wct"),
    "airport commission": ("chandler-airport-commission", "chandler-air"),
    "meeting": ("chandler-city-council", "chandler-cc"),
}


def _resolve_body(body_name: str) -> tuple[str, str]:
    import re as _re
    key = _re.sub(r'\s+', ' ', body_name).lower().strip()
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "chandler-city-council", "chandler-cc"


def build_month_url(year: int, month: int) -> str:
    """Build Chandler month view URL."""
    return _build_month_url(CHANDLER_ID, year, month)


# ── Meeting search ──


def search_chandler_meetings(
    year: int,
    body_slugs: Optional[list[str]] = None,
    start_month: int = 1,
    end_month: int = 12,
) -> list[dict]:
    """Search Chandler meetings for a given year, optionally restricted to a month range.

    Args:
        year: The year to search.
        body_slugs: Optional list of body slugs to filter by.
        start_month: First month to fetch (1-12, default 1).
        end_month: Last month to fetch (1-12, default 12).
    """
    all_m: list[dict] = []
    start_month = max(1, min(12, start_month))
    end_month = max(start_month, min(12, end_month))
    for m in range(start_month, end_month + 1):
        try:
            html = fetch_page(build_month_url(year, m), timeout=15)
            all_m.extend(_parse_meetings(html, BODY_MAP))
        except Exception as e:
            log.warning("Chandler %d-%02d failed: %s", year, m, e)
    if body_slugs:
        return [m for m in all_m if m["body_slug"] in body_slugs]
    return all_m


# ── Agenda item parsing (delegated to destiny_common) ──


def parse_agenda_items(html: str, meeting_seq: str) -> list[dict]:
    """Parse Chandler agenda items. Delegates to destiny_common."""
    return _parse_agenda_items(html, meeting_seq)


# ── Minutes / vote extraction from PDF pages ──


def build_attachments_url(meeting_seq: str, meeting_date: str) -> str:
    """Build Chandler attachments portal URL for minutes PDF discovery."""
    import datetime
    try:
        dt = datetime.datetime.strptime(meeting_date, "%Y-%m-%d")
    except ValueError:
        return ""
    mm = f"{dt.month:02d}"
    yyyy = f"{dt.year}"
    return (
        f"{BASE_URL}/agenda_publish.cfm?id={CHANDLER_ID}"
        f"&mt=ALL&get_month={mm}&get_year={yyyy}&dsp=min&seq={meeting_seq}"
    )


def fetch_attachments_page(attachments_url: str) -> Optional[str]:
    """Fetch Chandler minutes attachments page."""
    try:
        return fetch_page(attachments_url, timeout=30)
    except Exception as e:
        log.debug("Attachments page not available: %s", e)
        return None


def parse_attachments_for_minutes(
    html: str, meeting_seq: str = ""
) -> list[str]:
    """Extract minutes PDF URLs from Chandler attachments page."""
    import re
    pdfs: list[str] = []
    for m in re.finditer(
        r'href="([^"]+\.pdf[^"]*)"',
        html,
        re.IGNORECASE,
    ):
        url = urllib.parse.urljoin(BASE_URL, m.group(1))
        if url not in pdfs:
            pdfs.append(url)
    return pdfs


def fetch_minutes_pdf_bytes(pdf_url: str) -> Optional[bytes]:
    """Download a Chandler minutes PDF."""
    import urllib.request
    try:
        req = urllib.request.Request(pdf_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Minutes PDF not available: %s", e)
        return None


def build_results_url_from_minutes_url(minutes_url: str) -> Optional[str]:
    """Given a Chandler minutes URL, derive the Agenda-Results.pdf URL if it exists.

    Pattern:
      Minutes URL: .../2026/PZ/20260506_2271/2267_City-of-Chandler-Planning-Zoning-Minutes...pdf
      Results URL: .../2026/PZ/20260506_2271/2267_City-of-Chandler-Planning-Zoning-Agenda-Results.pdf

    The directory path is the same; only the filename changes from
    "Minutes..." to "Agenda-Results.pdf".
    """
    import re
    # Extract the base directory from the minutes URL
    m = re.match(r"(https?://[^/]+/chanddocs/\d+/[A-Z]+/\d+_\d+/\d+)_", minutes_url)
    if m:
        base = m.group(1)
        return f"{base}_City-of-Chandler-Planning-Zoning-Agenda-Results.pdf"
    return None


def try_discover_results_pdf(minutes_url: str) -> Optional[bytes]:
    """Try to find and download a Chandler Results PDF from a minutes URL.

    Attempts to derive the Agenda-Results.pdf URL from the minutes URL
    and download it if it exists.
    """
    results_url = build_results_url_from_minutes_url(minutes_url)
    if not results_url:
        return None
    pdf_bytes = fetch_results_pdf_bytes(results_url)
    return pdf_bytes


def parse_minutes_votes(text: str) -> dict:
    """Parse Chandler voting results from minutes PDF text."""
    from scraper.common.rollcall import extract_attendance

    attendance_text = re.split(
        r"(?im)^(?:Consent\s+Agenda|Item\s+\d+)", text, maxsplit=1
    )[0]
    supervisors = [
        member for member in extract_attendance(attendance_text)
        if (member.get("role") or "").lower()
        in {"mayor", "vice mayor", "councilmember", "council member"}
    ]
    votes: list[dict] = []
    lines = text.split("\n")
    i = 0
    vote_count_re = re.compile(r"(\d+)-(\d+)")
    current_names = {
        "hartke": "Kevin Hartke",
        "harris": "OD Harris",
        "encinas": "Angel Encinas",
        "ellis": "Christine Ellis",
        "stewart": "Mark Stewart",
        "orlando": "Matt Orlando",
        "poston": "Jane Poston",
    }
    attendance_by_surname = {
        member["name"].split()[-1].lower(): member["name"]
        for member in supervisors
        if member.get("name")
    }
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        all_vc = list(vote_count_re.finditer(line))
        if not all_vc:
            i += 1
            continue
        vc = all_vc[-1]
        ayes_count = int(vc.group(1))
        nays_count = int(vc.group(2))
        if nays_count == 0:
            result = "Carried Unanimously"
        elif ayes_count > nays_count:
            result = "Carried"
        else:
            result = "Failed"

        named_votes: list[dict] = []
        dissent = re.search(r";\s*(.*?)\s+dissenting", line, re.IGNORECASE)
        dissenting_names: set[str] = set()
        if dissent:
            names_text = re.sub(
                r"\b(?:Mayor|Vice\s+Mayor|Councilmembers?|Council\s+Members?)\b",
                "",
                dissent.group(1),
                flags=re.IGNORECASE,
            )
            for raw_name in re.split(r"\s*,\s*|\s+and\s+", names_text):
                surname = raw_name.strip().split()[-1].lower() if raw_name.strip() else ""
                if surname:
                    dissenting_names.add(surname)

        if dissenting_names:
            if supervisors:
                for member in supervisors:
                    surname = member["name"].split()[-1].lower()
                    named_votes.append({
                        "name": member["name"],
                        "vote": "no" if surname in dissenting_names else "yes",
                    })
            else:
                for surname in sorted(dissenting_names):
                    named_votes.append({
                        "name": attendance_by_surname.get(
                            surname, current_names.get(surname, surname.title())
                        ),
                        "vote": "no",
                    })
        votes.append({
            "agenda_item_number": "",
            "motion_result": result,
            "supervisor_votes": named_votes,
            "vote_text": line.strip(),
        })
        i += 1
    return {"supervisors": supervisors, "votes": votes}


def fetch_results_pdf_bytes(results_url: str) -> Optional[bytes]:
    """Download a Chandler Results PDF."""
    import urllib.request
    try:
        req = urllib.request.Request(results_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Results PDF not available: %s", e)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from a PDF via pdftotext."""
    import subprocess, tempfile, os
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed: %s", e)
        return None
    finally:
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


def parse_results_votes(text: str) -> dict:
    """Parse Chandler PZ Results PDF (Destiny Agenda-Results format).

    The PZ Results PDF is a full meeting agenda with vote results
    annotated inline. Vote patterns include:

    Consent agenda:
      "Items 1-4 approved unanimously (7-0)"
      "Items 1 & 2 approved 7-0"
      "Consent agenda items 1-3 approved unanimously (5-0)"

    Individual items (clean text):
      "Item 5 approved (7-0)"
      "Item 2 approved with stipulation (6-0, Commissioner Schwarzer recusing)"
      "Approved (6-0) with Koshiol abstaining."
      "Approved (5,1,1) with Commissioner Quinn dissenting and Bilsten abstaining."
      "Item 20 passed unanimously 6-0."

    Returns:
        {"supervisors": [...], "votes": [...]}
        Each vote dict has the same structure expected by persist_votes().
    """
    import re
    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()
    lines = text.split("\n")

    # Pre-compile regex patterns
    # Pattern 1: Consent block "Items 1-4 approved unanimously (7-0)"
    consent_block_re = re.compile(
        r'Items?\s+([\d\s,&-]+)\s+approved'
        r'(?:\s+unanimously)?'
        r'(?:\s*\(?\s*(\d+)[,\-]?(\d*)["]?\s*\)?)?',
        re.IGNORECASE,
    )
    # Pattern 2: Individual vote "approved (X-Y)" or "approved X-Y"
    item_vote_re = re.compile(
        r'Item\s+(\d+)\s+approved'
        r'(?:\s+with\s+stipulation)?'
        r'(?:\s*\(?\s*(\d+)[,\-](\d+).*?\)?)?',
        re.IGNORECASE,
    )
    # Pattern 3: "passed unanimously X-Y." (CC format)
    passed_re = re.compile(
        r"(?:Item\s+)?(\d+)\s+passed\s+unanimously\s+(\d+)-(\d+)",
        re.IGNORECASE,
    )
    # Pattern 4: Named vote "Commissioner X moved... Approved (A,B,C)"
    # Allow arbitrary text between "Approved" and the count (e.g. "Approved Calendar (X,Y,Z)")
    named_vote_re = re.compile(
        r'Approved.*?\(?\s*(\d+)[,\-](\d+)(?:[,\-](\d+))?\s*\)?',
        re.IGNORECASE | re.DOTALL,
    )
    # Pattern 5: Item with comma-separated vote "(5,1,1)"
    comma_vote_re = re.compile(
        r'Approved.*?\((\d+),(\d+),(\d+)\)',
        re.IGNORECASE | re.DOTALL,
    )
    # Pattern 6: Generic approved/vote X-Y pattern (fallback)
    generic_approved_re = re.compile(
        r"approved(?:\s+unanimously)?\s+(\d+)-(\d+)",
        re.IGNORECASE,
    )

    # Combine all text back into a single string for easier multi-line matching
    full_text = "\n".join(lines)

    # ---- Step 1: Extract named votes (Commissioner X moved...) ----
    # These have complex format with multiple lines, we handle them separately
    # Scan for "moved to" and "seconded" patterns
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Skip header boilerplate
        if any(
            keyword in line
            for keyword in [
                "Meeting Agenda", "Call to Order", "Pledge", "Unscheduled",
                "Consent Agenda", "Items listed", "discussion is required",
                "Members of the audience", "may be enacted", "removed from the",
                "Commission Members", "Pursuant to", "notice is hereby",
                "Persons with disabilities", "Please make requests",
                "Agendas are available", "Member Comments", "Calendar",
                "Focus Area", "Page ", "Adjourn", "RESULTS", "REVISED",
            ]
        ):
            i += 1
            continue

        # ---- Consent block: "Items 1-4 approved unanimously (7-0)" ----
        cm = consent_block_re.search(line)
        if cm:
            items_text = cm.group(1).strip()
            # Parse item range. Patterns: "1-4", "1 & 2", "1, 2, 3", "1"
            item_numbers = _parse_item_range(items_text)
            ayes_str = cm.group(2)
            nays_str = cm.group(3) if cm.lastindex and cm.lastindex >= 3 else "0"
            result = "Carried Unanimously" if (nays_str or "0") == "0" or nays_str in ("", None) else "Carried"
            for an in item_numbers:
                votes.append({
                    "agenda_item_number": str(an),
                    "motion_result": result,
                    "supervisor_votes": [],
                    "vote_text": cm.group(0).strip(),
                })
            i += 1
            continue

        # ---- Individual item vote: "Item X approved (Y-Z)" ----
        im = item_vote_re.search(line)
        if im:
            item_num = im.group(1)
            ayes_str = im.group(2)
            nays_str = im.group(3) if im.lastindex and im.lastindex >= 3 else "0"
            result = "Carried" if nays_str and nays_str.strip() not in ("", "0") else "Carried Unanimously"
            votes.append({
                "agenda_item_number": item_num,
                "motion_result": result,
                "supervisor_votes": [],
                "vote_text": im.group(0).strip(),
            })
            i += 1
            continue

        # ---- "passed unanimously X-Y" ----
        pm = passed_re.search(line)
        if pm:
            item_num = pm.group(1)
            votes.append({
                "agenda_item_number": item_num,
                "motion_result": "Carried Unanimously",
                "supervisor_votes": [],
                "vote_text": pm.group(0).strip(),
            })
            i += 1
            continue

        # ---- Generic "approved X-Y" (fallback for garbled PDFs) ----
        # Only match if the line has "Item" somewhere or has a sensible vote pattern
        gm = generic_approved_re.search(line)
        if gm and "Item" in line or gm and re.search(r"Item\s+\d+", line):
            ayes_str = gm.group(1)
            nays_str = gm.group(2)
            # Try to extract item number from the line
            item_m = re.search(r"Item\s+(\d+)-", line, re.IGNORECASE) or re.search(r"Item\s+(\d+)\s+", line, re.IGNORECASE)
            item_num = item_m.group(1) if item_m else ""
            result = "Carried" if nays_str != "0" else "Carried Unanimously"
            votes.append({
                "agenda_item_number": item_num,
                "motion_result": result,
                "supervisor_votes": [],
                "vote_text": gm.group(0).strip(),
            })
            i += 1
            continue

        i += 1

    # ---- Step 2: Extract named motion votes (multi-line) ----
    # These don't have "Item X" but have "moved to" / "seconded" patterns
    # that span multiple lines in the PDF.
    # Use a counter to assign unique pseudo-item-numbers for unnamed votes.
    unnamed_counter = 0
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Check for motion patterns across adjacent lines
        # Look ahead up to 5 lines for the full motion block
        if "moved" in line.lower() and ("to" in line.lower() or "elect" in line.lower()):
            # Collect up to 5 lines for this motion.
            # Allow lines that contain vote-pattern content even if they
            # also contain boilerplate words (e.g. "Calendar   (5,1,1)").
            block = [line]
            for j in range(1, 6):
                if i + j < len(lines):
                    nxt = lines[i + j].strip()
                    # Check if this line has vote content (numbers, approved, comma)
                    has_vote_content = bool(re.search(r'\d+\s*[,\-]\s*\d+|Approved|passed|moved', nxt, re.IGNORECASE))
                    if (
                        nxt
                        and (has_vote_content or (
                            "Member Comments" not in nxt
                            and "Calendar" not in nxt
                            and "Page " not in nxt
                            and "Focus Area" not in nxt
                            and "Council Focus" not in nxt
                            and "the next meeting" not in nxt.lower()
                        ))
                    ):
                        block.append(nxt)
                    else:
                        break
            block_text = " ".join(b.strip() for b in block if b.strip())

            # Extract vote from this block
            unnamed_counter += 1
            pseudo_num = f"_{unnamed_counter}"
            cv = comma_vote_re.search(block_text)
            if cv:
                ayes, nays, abst = (
                    int(cv.group(1)),
                    int(cv.group(2)),
                    int(cv.group(3)),
                )
                result = "Carried" if nays > 0 else "Carried Unanimously"
                votes.append({
                    "agenda_item_number": pseudo_num,
                    "motion_result": result,
                    "supervisor_votes": [],
                    "vote_text": block_text[:250],
                })
            else:
                # Try regular X-Y pattern with possible abstain info
                nv = named_vote_re.search(block_text)
                if nv:
                    ayes = int(nv.group(1))
                    nays = int(nv.group(2))
                    result = "Carried" if nays > 0 else "Carried Unanimously"
                    votes.append({
                        "agenda_item_number": pseudo_num,
                        "motion_result": result,
                        "supervisor_votes": [],
                        "vote_text": block_text[:250],
                    })
            i += 1
            continue

        i += 1

    # ---- Step 3: Extract "Approved" / "passed" patterns with vote counts ----
    # These catch remaining Approval patterns like "Approved (6-0) with Koshiol abstaining",
    # "passed unanimously 6-0", or "Approved (5,1,1) with Quinn dissenting".
    # In the PDF, "Approved" and the vote count may be on different lines.
    # Using a unique suffix for Step 3 unnamed votes
    unnamed_counter_3 = 100
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        if len(line_stripped) > 200:
            continue

        if re.search(r'Approved|adopted|passed', line_stripped, re.IGNORECASE):
            # Build a block that includes the next line in case the count is on a new line
            block = line_stripped
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and len(nxt) < 200:
                    block += " " + nxt

            # Try comma-separated vote first (5,1,1)
            cv = comma_vote_re.search(block)
            if cv:
                result = "Carried" if int(cv.group(2)) > 0 else "Carried Unanimously"
                vote_text_norm = cv.group(0).strip()
                if not any(vote_text_norm in v["vote_text"] for v in votes):
                    unnamed_counter_3 += 1
                    votes.append({
                        "agenda_item_number": f"_{unnamed_counter_3}",
                        "motion_result": result,
                        "supervisor_votes": [],
                        "vote_text": block[:250],
                    })
            else:
                # Try X-Y pattern with possible abstain info
                nv = named_vote_re.search(block)
                if nv:
                    ayes = int(nv.group(1))
                    nays = int(nv.group(2))
                    result = "Carried" if nays > 0 else "Carried Unanimously"
                    vote_text_norm = nv.group(0).strip()
                    if not any(vote_text_norm in v["vote_text"] for v in votes):
                        unnamed_counter_3 += 1
                        votes.append({
                            "agenda_item_number": f"_{unnamed_counter_3}",
                            "motion_result": result,
                            "supervisor_votes": [],
                            "vote_text": block[:250],
                        })

    # ---- Step 4: Extract individual per-member votes from CC format ----
    # Look for patterns like "Councilmember Hawkins absent excused"
    cc_absent_re = re.compile(r"Councilmember\s+(\w+)\s+absent", re.IGNORECASE)
    cc_dissenting_re = re.compile(r"Councilmember\s+(\w+)\s+dissent", re.IGNORECASE)

    for a in cc_absent_re.finditer(full_text):
        name = a.group(1)
        if name not in seen_sup:
            seen_sup.add(name)
            supervisors.append({
                "name": name,
                "normalized_name": name.lower(),
                "present": False,
            })
    for d in cc_dissenting_re.finditer(full_text):
        name = d.group(1)
        if name not in seen_sup:
            seen_sup.add(name)
            supervisors.append({
                "name": name,
                "normalized_name": name.lower(),
                "present": True,
            })

    return {"supervisors": supervisors, "votes": votes}


def _parse_item_range(items_text: str) -> list[int]:
    """Parse an item range string like "1-4", "1 & 2", "1, 2, 3"."""
    import re
    items_text = items_text.replace("&", ",")
    items_text = re.sub(r"\s+", "", items_text)
    parts = items_text.split(",")
    numbers: list[int] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if "-" in p:
            try:
                start, end = p.split("-", 1)
                numbers.extend(range(int(start), int(end) + 1))
            except ValueError:
                pass
        else:
            try:
                numbers.append(int(p))
            except ValueError:
                pass
    return numbers
