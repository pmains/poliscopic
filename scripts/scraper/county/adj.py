"""Board of Adjustment (ADJ) meeting and agenda extraction.

Board of Adjustment uses the same AgendaCenter system as Planning & Zoning
(CID=3 instead of CID=9), with the same year-tab pagination and h1.title
overview page structure.  Agenda items are PDF-first extraction, with a
different PDF format from PZ.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from typing import Optional

from scraper.common.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.common.io_utils import _normalize_text_date
from scraper.common.models import Meeting
from scraper.common.utils import PZ_SEARCH_BASE, PZ_AGENDA_BASE
from scraper.common.utils import CASE_PATTERN

# ── URL building ──

ADJ_CID = "3,"


def build_adj_search_url(start_date: str, end_date: str) -> str:
    """Build AgendaCenter search URL for Board of Adjustment meetings.

    Uses same base URL as PZ but with CID=3 instead of CID=9.
    """
    params = {
        "term": "",
        "CIDs": ADJ_CID,
        "startDate": start_date,
        "endDate": end_date,
        "dateRange": "",
        "dateSelector": "",
    }
    qs = urllib.parse.urlencode(params)
    return f"{PZ_SEARCH_BASE}?{qs}"


# ── Meeting extraction ──

async def extract_adj_meetings(page, search_url: str) -> list[Meeting]:
    """Extract Board of Adjustment meetings from AgendaCenter search results.

    Same year-tab clicking pattern as PZ.  The AgendaCenter only shows one
    year's meetings in the initial HTML; other years load via AJAX when the
    year tab (changeYear(...)) is clicked.
    """
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    all_meetings: list[Meeting] = []
    seen_ids: set[str] = set()

    # Collect from the initial (default year) HTML
    html = await page.content()
    initial_meetings = parse_adj_meetings_from_html(html, search_url)
    all_meetings.extend(initial_meetings)
    for m in initial_meetings:
        seen_ids.add(m.meeting_id)

    # Find available year tabs (e.g. changeYear(2023, 3, ...), changeYear(2024, 3, ...))
    year_tabs = await page.evaluate(
        r"""
        () => {
            const seen = new Set();
            const years = [];
            for (const a of document.querySelectorAll('a[href*="changeYear"]')) {
                const m = (a.getAttribute('href') || '').match(/changeYear\((\d{4})/);
                if (m && !seen.has(m[1])) {
                    seen.add(m[1]);
                    years.push(parseInt(m[1], 10));
                }
            }
            return years.sort((a, b) => a - b);
        }
        """
    )

    loaded_years = {int(m.meeting_date[:4]) for m in all_meetings}

    for year in year_tabs:
        if year in loaded_years:
            continue

        # Click the year tab to trigger the AJAX load via changeYear()
        try:
            tab_locator = page.locator(f'a[href*="changeYear({year}"]').first
            await tab_locator.evaluate("el => el.click()")
        except Exception:
            continue

        # Wait for AJAX response to populate
        await page.wait_for_timeout(2500)

        html = await page.content()
        year_meetings = parse_adj_meetings_from_html(html, search_url)

        for m in year_meetings:
            if m.meeting_id not in seen_ids:
                all_meetings.append(m)
                seen_ids.add(m.meeting_id)

    return all_meetings


def parse_adj_meetings_from_html(html: str, base_url: str) -> list[Meeting]:
    """Parse Board of Adjustment meetings from AgendaCenter search HTML.

    Same catAgendaRow structure as PZ.  body="adj", meeting_type="Board of Adjustment".
    """
    root = _parse_html(html)
    meetings: list[Meeting] = []

    rows = _find_all(root, "tr")
    for row in rows:
        classes = (row.attrs.get("class") or "").split()
        if "catAgendaRow" not in classes:
            continue

        cells = _find_all(row, "td")
        if not cells:
            continue

        first_cell = cells[0]
        cell_text = _clean_html_text(_node_text(first_cell))

        # Extract meeting date from the <strong> in the <h3>
        meeting_date = ""
        for h3 in _find_all(first_cell, "h3"):
            for strong in _find_all(h3, "strong"):
                aria = strong.attrs.get("aria-label", "")
                if aria:
                    dm = re.search(r"(\w+ \d{1,2},? \d{4})", aria)
                    if dm:
                        date_str = dm.group(1)
                        meeting_date = _normalize_text_date(date_str)
                        break
                abbr = _find_all(strong, "abbr")
                date_text = _clean_html_text(_node_text(strong))
                dm = re.search(r"(\w{3,9})\s+(\d{1,2}),?\s+(\d{4})", date_text)
                if dm:
                    meeting_date = _normalize_text_date(f"{dm.group(1)} {dm.group(2)}, {dm.group(3)}")
                    break
            if meeting_date:
                break
        if not meeting_date:
            continue

        # Find agenda link
        agenda_url = ""
        meeting_title = ""
        for a in _find_all(first_cell, "a"):
            href = a.attrs.get("href", "")
            if "/Agenda/" in href or "ViewFile/Agenda" in href:
                agenda_url = urllib.parse.urljoin(base_url, href)
                meeting_title = _clean_html_text(_node_text(a))
                break

        if not agenda_url:
            continue

        # Normalize title
        clean_title = _normalize_adj_meeting_title(meeting_title)

        meetings.append(Meeting(
            meeting_date=meeting_date,
            meeting_time="",
            meeting_title=clean_title,
            meeting_type="Board of Adjustment",
            body="adj",
            row_text=_clean_html_text(_node_text(row)),
            detail_url=agenda_url,
            agenda_url=agenda_url,
        ))

    return meetings


def _normalize_adj_meeting_title(title: str) -> str:
    """Normalize a Board of Adjustment meeting title.

    Strips location/webinar suffixes, redundant BOS Auditorium text.
    """
    if not title:
        return title

    # Strip location/webinar suffix
    title = re.sub(
        r"\s*[-–—]\s*BOS\s+Auditorium\s*&?\s*(?:Go\s*To)?\s*Web\w*\s*$",
        "", title
    ).strip()

    return title


# ── Agenda overview / PDF extraction ──

async def extract_adj_agenda_items(page, meeting_url: str) -> list[dict]:
    """Extract real agenda items from a Board of Adjustment AgendaCenter meeting.

    Same flow as PZ: load overview page, find agenda PDF and staff reports,
    parse the PDF for real items, match staff reports by case number.
    """
    await page.goto(meeting_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    html = await page.content()
    base_for_url = urllib.parse.urljoin(meeting_url, "/")

    # Parse overview: find agenda PDF link + staff report links
    overview = parse_adj_overview(html, meeting_url, base_for_url)

    agenda_pdf_url = ""
    staff_report_files: list[dict] = []
    if overview:
        agenda_pdf_url = overview.get("agenda_pdf_url", "")
        staff_report_files = overview.get("staff_report_files", [])

    if not agenda_pdf_url:
        found_count = len(staff_report_files) if staff_report_files else 0
        # Fallback: scan the page for any link with "agenda" in href or text
        for m in re.finditer(
            r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.I
        ):
            href = m.group(1)
            link_text = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
            if re.search(r"agenda", href + " " + link_text, re.I) and "ViewFile/Item/" in href:
                agenda_pdf_url = urllib.parse.urljoin(base_for_url, href)
                break
        if not agenda_pdf_url:
            print(f"    No agenda PDF found in overview (found {found_count} staff reports)")
            return []
        else:
            print(f"    Found agenda PDF (title did not match expected pattern)")

    if not agenda_pdf_url:
        print("    No agenda PDF found on overview page")
        return []

    # Download the agenda PDF directly (not via Playwright click)
    pdf_path_str = f"/tmp/adj_agenda_{Path(meeting_url).stem}.pdf"
    pdf_path = Path(pdf_path_str)
    try:
        pdf_req = urllib.request.Request(
            agenda_pdf_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
        )
        with urllib.request.urlopen(pdf_req, timeout=60) as pdf_resp:
            pdf_path.write_bytes(pdf_resp.read())
        if not pdf_path.exists() or pdf_path.stat().st_size < 100:
            raise RuntimeError(f"Downloaded file is {pdf_path.stat().st_size} bytes, too small")
    except Exception as e:
        print(f"    Failed to download agenda PDF: {e}")
        Path(pdf_path_str).unlink(missing_ok=True)
        return []

    # Parse the agenda PDF for real items
    pdf_items = parse_adj_agenda_pdf(pdf_path_str)
    pdf_path.unlink(missing_ok=True)

    if not pdf_items:
        print(f"    Agenda PDF parse returned no items — keeping existing data")
        return []

    # Build real agenda item dicts from PDF data
    items: list[dict] = []
    for pi in pdf_items:
        item_num = pi.get("agenda_item_number", 0)
        case_number = (pi.get("case_number") or "").strip()
        title = (pi.get("project_name") or f"Case {case_number}" if case_number else f"Agenda Item #{item_num}")

        item = {
            "source_body": "Board of Adjustment",
            "meeting_id": "",
            "meeting_date": "",
            "meeting_type": "Board of Adjustment",
            "agenda_item_number": str(item_num),
            "agenda_item_id": "",
            "agenda_item_title": title,
            "agenda_item_text": f"Case: {case_number}" if case_number else "",
            "agenda_item_url": meeting_url,
            "vote_or_action": "",
            "source_url": meeting_url,
            "c_number": "",
            "c_number_base": "",
            "c_number_revision": None,
            "case_number": case_number,
            "supporting_doc_dicts": [],
            "adj_data_complete": True,
            "adj_case_number": case_number,
            "adj_applicant": pi.get("applicant"),
            "adj_request": pi.get("request"),
            "adj_location": pi.get("location"),
            "adj_presented_by": pi.get("presented_by"),
            "staff_report_url": None,
        }
        items.append(item)

    # Build (all_case_numbers) -> staff report file lookups
    file_case_to_files: dict[str, list[dict]] = {}
    for srf in staff_report_files:
        all_cases = srf.get("all_case_numbers", [])
        primary_case = (srf.get("c_number") or "").upper()
        if primary_case and primary_case not in all_cases:
            all_cases.append(primary_case)
        if not all_cases and primary_case:
            all_cases = [primary_case]
        for cn in all_cases:
            file_case_to_files.setdefault(cn.upper(), []).append(srf)
        if primary_case and primary_case not in all_cases:
            file_case_to_files.setdefault(primary_case, []).append(srf)

    # Match staff report files to items by case number
    for it in items:
        it_case = (it.get("case_number") or "").upper()
        if it_case and it_case in file_case_to_files:
            for srf in file_case_to_files[it_case]:
                sd = {
                    "agenda_item_number": str(it["agenda_item_number"]),
                    "agenda_item_id": str(it["agenda_item_number"]),
                    "c_number": srf.get("c_number"),
                    "document_title": srf.get("document_title", ""),
                    "document_url": srf.get("document_url", ""),
                    "document_type": "PDF",
                    "file_name": f"{srf.get('document_title', '')}.pdf",
                    "file_extension": "pdf",
                }
                existing_urls = {d["document_url"] for d in it["supporting_doc_dicts"]}
                if sd["document_url"] not in existing_urls:
                    it["supporting_doc_dicts"].append(sd)
                if it.get("staff_report_url") is None:
                    it["staff_report_url"] = srf.get("document_url", "")

    return items


def parse_adj_overview(html: str, source_url: str, base_url: str) -> dict | None:
    """Parse a Board of Adjustment AgendaCenter overview/document-index page.

    Same h1.title + class="file" structure as PZ.  Delegates to parse_pz_overview
    since the HTML structure is identical.
    """
    from scraper.county.pz import parse_pz_overview
    return parse_pz_overview(html, source_url, base_url)


def parse_adj_agenda_pdf(filepath: str) -> list[dict]:
    """Parse a Board of Adjustment Agenda PDF and extract structured fields.

    ADJ agenda PDF format:
        1.      BA260015              Axt Property                        District 2
                Applicant:            Eric Bartha
                Location:             APN ...
                Request:              Variance to ...
                Presented by          Daniel Johnson

    Code Compliance Review items have the same structure but under
    a "Code Compliance Review" section heading.
    """
    import subprocess
    import tempfile

    if not filepath or not Path(filepath).exists():
        return []

    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            txt_path = f.name
        subprocess.run(
            ["pdftotext", "-layout", filepath, txt_path],
            capture_output=True, timeout=30,
        )
        text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
        Path(txt_path).unlink(missing_ok=True)
    except Exception:
        return []

    lines = text.split("\n")
    items: list[dict] = []
    current: dict | None = None
    last_set_field: str | None = None  # tracks most recently populated field

    # Patterns for ADJ PDF fields
    FIELD_PATTERNS = {
        "applicant": re.compile(r"Applicant\s*:\s*(.*)", re.I),
        "respondent": re.compile(r"Respondent\s*:\s*(.*)", re.I),
        "location": re.compile(r"Location\s*:\s*(.*)", re.I),
        "request": re.compile(r"Request\s*:\s*(.*)", re.I),
        "requests": re.compile(r"Requests\s*:\s*(.*)", re.I),
        "presented_by": re.compile(r"Presented\s*by\s*:?\s*(.*)", re.I),
        "violation": re.compile(r"Violation\s*:\s*(.*)", re.I),
    }

    # Pattern for item start with inline case number:
    # "1.      BA260015              Axt Property                        District 2"
    # "2.      V2501325              Code Compliance Review              District 4"
    ITEM_START = re.compile(
        r"^\s*(\d+)\.\s+(BA\d{6}|V\d{6,7}|VP?\d+)\s+(.*?)(?:\s+District\s+\d+)?\s*$",
        re.I,
    )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Skip section headings and boilerplate
        if re.match(
            r"^(Continuance|Consent|Regular|Withdrawn|Code Compliance|Agenda|Other\s+Matters|Adjournment)\s*",
            stripped, re.I,
        ):
            # Reset continuation tracking when hitting a section break
            last_set_field = None
            continue

        # Detect numbered item start with inline case number
        item_m = ITEM_START.match(stripped)
        if item_m:
            if current:
                items.append(current)
            item_num = int(item_m.group(1))
            case_number = item_m.group(2).upper()
            project_name = item_m.group(3).strip() or None
            current = {
                "agenda_item_number": item_num,
                "case_number": case_number,
                "project_name": project_name,
                "applicant": None,
                "location": None,
                "request": None,
                "presented_by": None,
            }
            last_set_field = "project_name"
            continue

        if current is None:
            continue

        # Match field patterns
        matched = False
        for field, pattern in FIELD_PATTERNS.items():
            if current.get(field):
                continue
            m = pattern.search(stripped)
            if m:
                val = m.group(1).strip() if m.lastindex >= 1 else ""
                if val:
                    current[field] = val
                    last_set_field = field
                matched = True
                break

        if not matched:
            # Continuation text: append to the most recently set field
            sub_m = re.match(r"^\s*\d+[\)\.]\s+(.*)", stripped)
            if sub_m:
                sub_text = sub_m.group(1).strip()
                target = "requests" if current.get("requests") else "request" if current.get("request") else "project_name"
                existing = current.get(target) or ""
                current[target] = (existing + " " + sub_text).strip()
            elif last_set_field and current.get(last_set_field):
                existing = current[last_set_field]
                current[last_set_field] = (existing + " " + stripped).strip()

    if current:
        items.append(current)

    return items


def _extract_adj_year_tabs_from_html(html: str) -> list[int]:
    """Extract unique year numbers from AgendaCenter year-tab links.

    Returns sorted list of years found in changeYear() anchor hrefs,
    e.g. [2023, 2024, 2025, 2026] for CID=3 (ADJ).
    """
    seen: set[int] = set()
    for m in re.finditer(r'changeYear\((\d{4})', html):
        year = int(m.group(1))
        if year not in seen:
            seen.add(year)
    return sorted(seen)


def _format_mm_dd_yyyy(date_iso: str) -> str | None:
    """Convert YYYY-MM-DD to MM/DD/YYYY. Returns None if input is empty."""
    if not date_iso:
        return None
    try:
        d = dt.date.fromisoformat(date_iso)
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    except (ValueError, TypeError):
        # Already in MM/DD/YYYY? Return as-is.
        if re.match(r"\d{1,2}/\d{1,2}/\d{4}", date_iso):
            return date_iso
        return date_iso
