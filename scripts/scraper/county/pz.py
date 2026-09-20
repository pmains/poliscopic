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

def build_pz_search_url(start_date: str, end_date: str) -> str:
    """Build AgendaCenter search URL for P&Z meetings."""
    params = {
        "term": "",
        "CIDs": "9,",
        "startDate": start_date,
        "endDate": end_date,
        "dateRange": "",
        "dateSelector": "",
    }
    qs = urllib.parse.urlencode(params)
    return f"{PZ_SEARCH_BASE}?{qs}"


async def extract_pz_meetings(page, search_url: str) -> list[Meeting]:
    """Extract P&Z meetings from AgendaCenter search results.

    The AgendaCenter search page renders year-based AJAX tabs.  Only one
    year's meetings are present in the initial HTML (the default year).
    We must click each year tab to load the other years' meetings.
    """
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    all_meetings: list[Meeting] = []
    seen_ids: set[str] = set()

    # Collect from the initial (default year) HTML
    html = await page.content()
    initial_meetings = parse_pz_meetings_from_html(html, search_url)
    all_meetings.extend(initial_meetings)
    for m in initial_meetings:
        seen_ids.add(m.meeting_id)

    # Find available year tabs (e.g. changeYear(2023, ...), changeYear(2024, ...))
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
        year_meetings = parse_pz_meetings_from_html(html, search_url)

        for m in year_meetings:
            if m.meeting_id not in seen_ids:
                all_meetings.append(m)
                seen_ids.add(m.meeting_id)

    return all_meetings


def parse_pz_meetings_from_html(html: str, base_url: str) -> list[Meeting]:
    """Parse P&Z meetings from AgendaCenter search HTML.

    Structure of each meeting row:
      <tr id="row3711..." class="catAgendaRow">
        <td>
          <h3><strong>Apr 23, 2026</strong></h3>
          <p>
            <a id="04232026-3722"
               href="/AgendaCenter/ViewFile/Agenda/_04232026-3722?html=true">
              April 23, 2026 Planning and Zoning Commission Meeting ...
            </a>
          </p>
        </td>
        <td class="minutes"></td>
        <td class="media"><a href="https://youtu.be/...">...</a></td>
      </tr>
    """
    root = _parse_html(html)
    meetings: list[Meeting] = []

    # Find all catAgendaRow rows
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
        # Format: '<strong aria-label="Agenda for April 23, 2026">
        #           <abbr title="April">Apr</abbr> 23, 2026</strong>'
        meeting_date = ""
        for h3 in _find_all(first_cell, "h3"):
            for strong in _find_all(h3, "strong"):
                # Check aria-label first (has full month name)
                aria = strong.attrs.get("aria-label", "")
                if aria:
                    dm = re.search(r"(\w+ \d{1,2},? \d{4})", aria)
                    if dm:
                        date_str = dm.group(1)
                        meeting_date = _normalize_text_date(date_str)
                        break
                # Fallback: extract from text content directly
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

        # Normalize title — detect ZIPPOR subcommittee, strip redundant prefix
        clean_title, clean_type = _normalize_pz_meeting_title(
            meeting_title, "Planning & Zoning"
        )

        # Note: meeting_id is extracted from agenda_url by the Meeting property
        meetings.append(Meeting(
            meeting_date=meeting_date,
            meeting_time="",
            meeting_title=clean_title,
            meeting_type=clean_type,
            body="pz",
            row_text=_clean_html_text(_node_text(row)),
            detail_url=agenda_url,
            agenda_url=agenda_url,
        ))

    return meetings


async def extract_pz_agenda_items(page, meeting_url: str) -> list[dict]:
    """Extract real agenda items from a P&Z AgendaCenter meeting.

    The AgendaCenter overview page (ViewFile/Agenda/...) is a document index,
    NOT an agenda. Real agenda items come from the linked agenda PDF.
    Staff reports on the overview page become supporting documents.
    """
    await page.goto(meeting_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    html = await page.content()
    base_for_url = urllib.parse.urljoin(meeting_url, "/")

    # Parse overview: find agenda PDF link + staff reports
    overview = parse_pz_overview(html, meeting_url, base_for_url)

    agenda_pdf_url = ""
    staff_report_files: list[dict] = []
    if overview:
        agenda_pdf_url = overview.get("agenda_pdf_url", "")
        staff_report_files = overview.get("staff_report_files", [])

    # If overview parsing found no agenda PDF, try fallback link search
    if not agenda_pdf_url:
        # Fallback: scan the page for any link with "agenda" in href or text
        found_count = len(staff_report_files) if staff_report_files else 0
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

    # Download the agenda PDF directly (not via Playwright click, which is unreliable)
    pdf_path_str = f"/tmp/pz_agenda_{Path(meeting_url).stem}.pdf"
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
    pdf_items = parse_pz_agenda_pdf(pdf_path_str)
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
            "source_body": "Planning & Zoning",
            "meeting_id": "",
            "meeting_date": "",
            "meeting_type": "Planning & Zoning",
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
            "pz_data_complete": True,
            "pz_case_number": case_number,
            "pz_district": pi.get("district"),
            "pz_project_name": pi.get("project_name"),
            "pz_applicant": pi.get("applicant"),
            "pz_request": pi.get("request"),
            "pz_location": pi.get("location"),
            "pz_recommendation": pi.get("recommendation"),
            "pz_presented_by": pi.get("presented_by"),
            "staff_report_url": None,
        }
        items.append(item)

    # Build (all_case_numbers) -> staff report file lookups
    # A file like "CPAZ250011 & Z250034 P&Z Report" applies to BOTH cases
    file_case_to_files: dict[str, list[dict]] = {}
    for srf in staff_report_files:
        # Register under ALL case numbers this file applies to
        all_cases = srf.get("all_case_numbers", [])
        primary_case = (srf.get("c_number") or "").upper()
        if primary_case and primary_case not in all_cases:
            all_cases.append(primary_case)
        if not all_cases and primary_case:
            all_cases = [primary_case]
        for cn in all_cases:
            file_case_to_files.setdefault(cn.upper(), []).append(srf)
        # Also register under the direct c_number for backward compat
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
                # Deduplicate by URL
                existing_urls = {d["document_url"] for d in it["supporting_doc_dicts"]}
                if sd["document_url"] not in existing_urls:
                    it["supporting_doc_dicts"].append(sd)
                if it.get("staff_report_url") is None:
                    it["staff_report_url"] = srf.get("document_url", "")

    return items


def parse_pz_overview(html: str, source_url: str, base_url: str) -> dict | None:
    """Parse a P&Z AgendaCenter overview/document-index page.

    Each h1.title section may have multiple file links (staff reports, exhibits).
    Titles like "CPAZ250011 & Z250034 P&Z Report" contain multiple case numbers.

    Returns:
      {
        "agenda_pdf_url": str | "",
        "agenda_title": str | "",
        "staff_report_files": list[dict],  # One dict per actual file
      }
    Returns None if no h1.title structure found.
    """
    from html import unescape
    base_for_url = base_url

    item_blocks: list[tuple[str, str]] = []
    for m in re.finditer(
        r'<h1[^>]*class="title"[^>]*>(.*?)</h1>\s*(.*?)(?=<h1[^>]*class="title"|\Z)',
        html, re.DOTALL | re.I
    ):
        title_html = m.group(1).strip()
        section = m.group(2).strip()
        item_blocks.append((title_html, section))

    if not item_blocks:
        return None

    agenda_pdf_url = ""
    agenda_title = ""
    staff_report_files: list[dict] = []

    for title_html, section in item_blocks:
        title = re.sub(r"<[^>]+>", " ", title_html)
        title = unescape(re.sub(r"\s+", " ", title)).strip()
        if not title:
            continue

        if re.search(r"GoToWebinar|Webinar User Guide", title, re.I):
            continue

        # Extract ALL case numbers from the title (e.g. "CPAZ250011 & Z250034")
        title_cases: list[str] = []
        for c_m in CASE_PATTERN.finditer(title):
            cn = c_m.group(1).upper()
            if cn not in title_cases:
                title_cases.append(cn)

        # Find ALL document links in this section
        doc_urls: list[tuple[str, str]] = []  # (url, link_text)
        for a_m in re.finditer(
            r'<a[^>]*class="[^"]*file[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            section, re.DOTALL | re.I
        ):
            href = a_m.group(1)
            link_text = re.sub(r"<[^>]+>", " ", a_m.group(2))
            link_text = re.sub(r"\s+", " ", link_text).strip()
            if "/ViewFile/Item/" in href:
                doc_url = urllib.parse.urljoin(base_for_url, href)
                doc_urls.append((doc_url, link_text))

        if not doc_urls:
            continue

        # Identify the agenda document vs staff reports
        # Identify the agenda document: match by "agenda", "Planning and Zoning" (without staff report),
        # or any title that looks like a date-range descriptor (e.g. "April 23, 2026 - Planning and Zoning")
        has_no_case = not title_cases
        is_agenda = (
            (bool(re.search(r"agenda", title, re.I)) and not re.search(r"staff report", title, re.I))
            or (bool(re.search(r"planning\s+and\s+zoning", title, re.I))
                and not re.search(r"staff report", title, re.I)
                and has_no_case)
            or (has_no_case and bool(re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)", title, re.I)))
        )

        if is_agenda:
            # The agenda document is typically the first file link
            agenda_pdf_url = doc_urls[0][0]
            agenda_title = title
        else:
            # Each file link is a separate staff report document
            for doc_url, link_text in doc_urls:
                # Extract case number from this specific file
                file_case = ""
                dc = CASE_PATTERN.search(doc_url + " " + link_text)
                if dc:
                    file_case = dc.group(1).upper()
                
                # Also try to extract from the section title
                doc_case = file_case or (title_cases[0] if title_cases else "")
                
                staff_report_files.append({
                    "document_title": link_text.replace(".pdf", "").replace(".PDF", "").strip() or title,
                    "document_url": doc_url,
                    "c_number": doc_case if doc_case else None,
                    "all_case_numbers": title_cases.copy(),
                })

    return {
        "agenda_pdf_url": agenda_pdf_url,
        "agenda_title": agenda_title,
        "staff_report_files": staff_report_files,
    }

def _extract_pz_year_tabs_from_html(html: str) -> list[int]:
    """Extract unique year numbers from AgendaCenter year-tab links.

    Returns sorted list of years found in changeYear() anchor hrefs,
    e.g. [2023, 2024, 2025, 2026].
    """
    seen: set[int] = set()
    for m in re.finditer(r'changeYear\((\d{4})', html):
        year = int(m.group(1))
        if year not in seen:
            seen.add(year)
    return sorted(seen)


def _normalize_pz_meeting_title(title: str, meeting_type: str) -> tuple[str, str]:
    """Normalize a PZ meeting title and detect subcommittee type.

    For regular PZ Commission meetings the title is clean already:
      "May 7, 2026 Planning and Zoning Commission Meeting"

    ZIPPOR and other subcommittee titles may have a leading date prefix:
      "February 19, 2026 - Zoning, Infrastructure,
       Policy, Procedure, and Ordinance Review (ZIPPOR) Committee Meeting"
    → becomes:  "Zoning, Infrastructure, Policy, Procedure, and Ordinance
                 Review (ZIPPOR) Committee Meeting — Feb 19, 2026"
        with meeting_type: "ZIPPOR"

    Some ZIPPOR titles have a redundant "Planning & Zoning Meeting — DATE - "
    prefix, which is also stripped.

    Returns (normalized_title, normalized_meeting_type).
    """
    if not title:
        return title, meeting_type

    # Detect ZIPPOR meetings by title content
    if re.search(r"ZIPPOR", title, re.I):
        # Extract leading date if present (e.g., "February 19, 2026 - ")
        date_prefix = re.match(
            r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\s*[-–—]\s*", title
        )
        if date_prefix:
            month_name = date_prefix.group(1)
            day = date_prefix.group(2)
            year = date_prefix.group(3)
            # Build abbreviated date: "Feb 19, 2026"
            MONTH_ABBR = {
                "january": "Jan", "february": "Feb", "march": "Mar",
                "april": "Apr", "may": "May", "june": "Jun",
                "july": "Jul", "august": "Aug", "september": "Sep",
                "october": "Oct", "november": "Nov", "december": "Dec",
            }
            abbr = MONTH_ABBR.get(month_name.lower(), month_name[:3])
            short_date = f"{abbr} {int(day)}, {year}"
            body = title[date_prefix.end():].strip()
            normalized = f"{body} — {short_date}"
            return normalized, "ZIPPOR"

        # Fallback: strip "Planning & Zoning Meeting — [date] - " prefix
        normalized = re.sub(
            r"^Planning\s*[&/]\s*Zoning\s+Meeting\s*—\s*[A-Za-z]+\s+\d{1,2},?\s+\d{4}\s*[-–—]\s*",
            "", title
        ).strip()
        if not normalized:
            normalized = title
        return normalized, "ZIPPOR"

    # Strip location/webinar suffix for all PZ titles
    # e.g. " - BOS Auditorium & GoTo Webinar", " - BOS Auditorium & Go To Webinar"
    title = re.sub(
        r"\s*[-–—]\s*BOS\s+Auditorium\s*&?\s*(?:Go\s*To)?\s*Web\w*\s*$",
        "", title
    ).strip()

    return title, meeting_type


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



def parse_pz_agenda_pdf(filepath: str) -> list[dict]:
    """Parse a P&Z Agenda PDF and extract structured field data per item."""
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

    FIELD_PATTERNS = {
        "case_number": re.compile(r"^\s*\d*\.?\s*Case\s*:?\s*(.+?)\s{2,}|^\s*\d*\.?\s*Case\s{2,}(.+?)\s{2,}"),
        "district": re.compile(r"District\s+(\d+)"),
        "project_name": re.compile(r"Project\s+name\s*:?\s*(.*)"),
        "applicant": re.compile(r"Applicant\s*:?\s*(.*)"),
        "request": re.compile(r"Request\s*:?\s*(.*)"),
        "location": re.compile(r"Location\s*:?\s*(.*)"),
        "recommendation": re.compile(r"Recommendation\s*:?\s*(.*)"),
        "presented_by": re.compile(r"Presented\s+by\s*:?\s*(.*)"),
    }

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if re.match(r"^\s*(Continuance|Consent|Regular)\s+Agenda\s*$", stripped, re.I):
            continue

        # Match items starting with "N. Case: ...", "N. CASENUMBER ..." (ZIPPOR format),
        # or "N. CategoryName: ..." (ZIPPOR items without case numbers, e.g. "Area Plan:")
        item_start = re.match(r"^\s*(\d+)\.?\s*Case\b", stripped, re.I)
        if not item_start:
            item_start = re.match(r"^\s*(\d+)\.\s+([A-Z]+-?\d{3,})", stripped)
        if not item_start:
            # ZIPPOR items with a category name but no case number
            # e.g. "1. Area Plan: White Tank..."
            item_start = re.match(
                r"^\s*(\d+)\.\s+(?!Case\b)(?!case\b)[A-Z][a-zA-Z\s/]+:", stripped
            )
        if item_start:
            if current:
                items.append(current)

            item_num = item_start.group(1)
            current = {
                "agenda_item_number": int(item_num), "case_number": "", "district": None,
                "project_name": None, "applicant": None, "request": None,
                "location": None, "recommendation": None, "presented_by": None,
            }
            rest = stripped[item_start.end():].strip()
            # For "N. CASENUMBER" format (ZIPPOR), the case number is in group(2)
            # and was consumed by the regex — prepend it back to rest
            if len(item_start.groups()) >= 2:
                rest = (item_start.group(2) + " " + rest).strip()
            case_m = re.search(r"([A-Z]+-?\d{3,})", rest)
            if case_m:
                current["case_number"] = case_m.group(1)
                # For ZIPPOR format (no "Case:" label), capture description after
                # the case number as the project_name
                if len(item_start.groups()) >= 2:
                    cn_end = rest.index(case_m.group(1)) + len(case_m.group(1))
                    desc = rest[cn_end:].strip().lstrip("-–— ").strip()
                    if desc:
                        current["project_name"] = desc
            else:
                # ZIPPOR items without a case number (e.g. "1. Area Plan: ...")
                # — use the rest of the line as project_name
                if rest and not current["project_name"]:
                    current["project_name"] = rest
            dist_m = FIELD_PATTERNS["district"].search(rest)
            if dist_m:
                current["district"] = f"District {dist_m.group(1)}"
            continue

        if current is None:
            continue

        # Check if this is a continuation line for ZIPPOR items:
        # indented descriptive text that follows the case number line.
        if not any(pattern.search(stripped) for pattern in FIELD_PATTERNS.values()):
            pn = current.get("project_name")
            if pn:
                # Stop if we've already captured both project_name and presented_by
                if current.get("presented_by"):
                    pass  # item is complete, skip all trailing lines
                elif re.match(r"^(Other\s+Matters|Call\s+to\s+Order|Roll\s+Call|Adjournment|Announcements)", stripped, re.I):
                    pass  # section break
                elif re.match(r"^(Regular|Consent|Continuance|Study|Public)\s+Agenda", stripped, re.I):
                    pass  # section heading
                elif re.match(r"^\w+\s+\d+,?\s+\d{4}\s+ZIPPOR\s+Agenda", stripped, re.I):
                    pass  # page header/footer
                elif re.match(r"^Page\s+\d+\s+of\s+\d+", stripped, re.I):
                    pass  # page footer
                else:
                    # Remove trailing hyphens/em-dashes from previous text, then append
                    current["project_name"] = pn.rstrip("-–—") + " " + stripped
            continue

        for field, pattern in FIELD_PATTERNS.items():
            if current.get(field):
                continue
            m = pattern.search(stripped)
            if m:
                val = m.group(1) if m.lastindex >= 1 else ""
                if not val and m.lastindex >= 2:
                    val = m.group(2)
                val = (val or "").strip()
                if val:
                    current[field] = val
                break

    if current:
        items.append(current)

    return items
