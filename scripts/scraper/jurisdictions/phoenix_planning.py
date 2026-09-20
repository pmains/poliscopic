"""
Phoenix PDD planning data scraper.

Three data sources:

  1. PDD Calendar Events — JSON feed from the city events calendar,
     filtered to planning & development events.  Provides structured
     meeting data (date, time, location, maps URL, cancellation flag).

     Endpoint: event_search_1709872248.results.json?q=planning

     Also per-body event-card-list feeds for individual VPC pages.

  2. Staff Reports — static HTML page listing current staff reports for
     rezoning (Z-), General Plan Amendment (gpa-), Planning Hearing
     Officer (pho-), and Zoning Text Amendment (z-ta-) cases.

     URL: /pdd/about-us/reports-data/staff-reports.html

  3. PUD Cases — static HTML page listing Planned Unit Development case
     documents with hundreds of PDFs spanning many years.

     URL: /pdd/planning-zoning/zoning-rezoning/pud-cases.html

All documents are stored as Case records (with SupportingDocument links)
in the database.  Calendar events are stored as Meeting records.

Body code conventions:
  phoenix-vpc     → Village Planning Committees (all 15 villages)
  phoenix-pc      → Planning Commission
  phoenix-pho     → Planning Hearing Officer
  phoenix-pdd     → General PDD (catch-all for cases not linked to a body)
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

PHOENIX_GOV = "https://www.phoenix.gov"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ── Endpoints ───────────────────────────────────────────────────────────────

EVENTS_SEARCH_URL = (
    PHOENIX_GOV
    + "/calendar/_jcr_content/root/container/container/"
    + "event_search_1709872248.results.json"
)

STAFF_REPORTS_URL = (
    PHOENIX_GOV
    + "/administration/departments/pdd/about-us/reports-data/staff-reports.html"
)

PUD_CASES_URL = (
    PHOENIX_GOV
    + "/administration/departments/pdd/planning-zoning/zoning-rezoning/pud-cases.html"
)

# ── Body mapping for calendar events ────────────────────────────────────────
# Map event title patterns to (body_code, display_name)

EVENT_BODY_MAP: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"planning commission", re.I),
     "phoenix-pc", "Planning Commission"),
    (re.compile(r"planning hearing officer", re.I),
     "phoenix-pho", "Planning Hearing Officer"),
    (re.compile(r"board of adjustment", re.I),
     "phoenix-boa", "Board of Adjustment"),
    (re.compile(r"historic preservation", re.I),
     "phoenix-historic-preservation", "Historic Preservation Commission"),
    (re.compile(r"heritage commission", re.I),
     "phoenix-heritage-commission", "Heritage Commission"),
    (re.compile(r"zoning adjustment hearing", re.I),
     "phoenix-zoning-adjustment", "Zoning Adjustment"),
    (re.compile(r"design review committee", re.I),
     "phoenix-dr", "Design Review Committee"),
    (re.compile(r"design standards committee", re.I),
     "phoenix-ds", "Design Standards Committee"),
    (re.compile(r"development advisory board", re.I),
     "phoenix-dab", "Development Advisory Board"),
    (re.compile(r"village planning committee", re.I),
     "phoenix-village-planning", "Village Planning Committee"),
    (re.compile(r"site plan review", re.I),
     "phoenix-site-plan", "Site Plan Review Team"),
]

# ── Case number extraction patterns ─────────────────────────────────────────

CASE_NUMBER_PATTERNS: list[tuple[re.Pattern, str]] = [
    # gpa-XXX-YY-N  (general plan amendment) — match before Z- since filename may also contain Z
    (re.compile(r"gpa[-_]([a-z-]+\d+[-_]\d+[-_]?\d*)", re.I), "GPA-"),
    (re.compile(r"gpa[-_]([a-z-]+\d+[-_]\d+)", re.I), "GPA-"),
    # pho-XXX-YY  (planning hearing officer) — match before Z- since ref to Z-case follows
    (re.compile(r"pho[-_](\d+[-_]\d+)", re.I), "PHO-"),
    # z-ta-XXX-YY  (zoning text amendment)
    (re.compile(r"z[-_]ta[-_](\d+[-_]\d+[-_]\w*)", re.I), "Z-TA-"),
    # z-sp-XXX-YY  (special use permit)
    (re.compile(r"z[-_]sp[-_](\d+[-_]\d+[-_]\w*)", re.I), "Z-SP-"),
    # Z-XXX-YY-N  (standard rezoning)
    (re.compile(r"z[-_](\d+[-_][a-z]?[-_]?\d+[-_]?\d*)", re.I), "Z-"),
    # Fallback: any Z-XXX pattern in filename
    (re.compile(r"z[-_](\d+[-_][a-z]?[-_]?\d*[-_]?\d*)", re.I), "Z-"),
]

# Known labels to skip
SKIP_PDFS: list[re.Pattern] = [
    re.compile(r"pdd_pz_pdf_\d+\.pdf", re.I),  # directory index
    re.compile(r"pud-procedures-outline", re.I),  # procedural doc
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _fetch(url: str, timeout: int = 30) -> str:
    """Fetch a URL and return the decoded response body."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def _resolve_body_code(title: str) -> tuple[str, str]:
    """Map an event title to (body_code, display_name)."""
    for pattern, code, name in EVENT_BODY_MAP:
        if pattern.search(title):
            return code, name
    return "phoenix-aem", title.strip()


def _extract_case_number(filename: str) -> Optional[str]:
    """Extract a normalized case number from a PDF filename."""
    fl = filename.lower()
    for pattern, prefix in CASE_NUMBER_PATTERNS:
        m = pattern.search(fl)
        if m:
            raw = m.group(1)
            # Normalize separators
            normalized = re.sub(r"[-_]+", "-", raw)
            norm_full = f"{prefix}{normalized}"
            return norm_full.upper()
    return None


def _classify_document_type(filename: str) -> str:
    """Classify a PDF document type from its filename."""
    fl = filename.lower()
    if "staff report" in fl or "-sr" in fl or "_sr" in fl or "staff_report" in fl:
        return "Staff Report"
    if "ordinance" in fl or "-ord" in fl or "_g-" in fl or re.search(r"g-\d{4}", fl):
        return "Ordinance"
    if "approval letter" in fl or "-fal" in fl or "_fal" in fl or "final approval" in fl or "final review approval" in fl:
        return "Approval Letter"
    if "narrative" in fl:
        return "Narrative"
    if "memo" in fl or "back up" in fl:
        return "Memo"
    if "addendum" in fl or "adda" in fl or "add" in fl:
        return "Addendum"
    if "pc memo" in fl or "-pcm" in fl:
        return "Planning Commission Memo"
    if "cc memo" in fl or "-cc" in fl:
        return "City Council Memo"
    if "minor amendment" in fl or "-ma" in fl:
        return "Minor Amendment"
    if "community correspondence" in fl:
        return "Community Correspondence"
    if "opposition" in fl:
        return "Opposition Letter"
    if "support" in fl:
        return "Support Letter"
    if "traffic study" in fl:
        return "Traffic Study"
    if "parking study" in fl:
        return "Parking Study"
    if "final" in fl:
        return "Final Document"
    return "Case Document"


def _is_skip_pdf(filename: str) -> bool:
    """Check if a PDF should be skipped (directory index, procedural doc)."""
    return any(p.search(filename) for p in SKIP_PDFS)


# ── Data Sources ────────────────────────────────────────────────────────────

def fetch_calendar_events(max_results: int = 50) -> list[dict]:
    """Fetch planning & development calendar events from the AEM search feed.

    Returns list of event dicts with:
      title, url, eventDate, startTime, endTime, location, mapsUrl, cancelled
    """
    events: list[dict] = []
    offset = 0
    page_size = 10

    while len(events) < max_results:
        url = f"{EVENTS_SEARCH_URL}?q=planning&offset={offset}"
        try:
            body = _fetch(url)
        except Exception:
            break

        data = json.loads(body)
        results = data.get("results", [])
        if not results:
            break

        for r in results:
            props = r.get("properties", {})
            events.append({
                "title": r.get("title", ""),
                "url": f"{PHOENIX_GOV}{r.get('url', '')}" if r.get("url", "").startswith("/") else r.get("url", ""),
                "event_date": props.get("eventDate", ""),
                "start_time": props.get("startTime", ""),
                "end_time": props.get("endTime", ""),
                "location": props.get("location", ""),
                "maps_url": props.get("mapsUrl", ""),
                "cancelled": props.get("cancelled"),
            })

            if len(events) >= max_results:
                break

        total_raw = data.get("resultTotal", 0)
        try:
            total = int(total_raw)
        except (ValueError, TypeError):
            total = 0

        offset += len(results)
        if total and offset >= total:
            break
        if offset > 500:
            break

    return events


def fetch_staff_report_docs() -> list[dict]:
    """Fetch current staff reports from the PDD staff reports page.

    Returns list of document dicts with:
      case_number, document_url, document_title, document_type, source_url
    """
    try:
        html = _fetch(STAFF_REPORTS_URL)
    except Exception:
        return []

    docs: list[dict] = []
    # Extract PDF links from AEM DAM
    pdf_pattern = re.compile(
        r'href="(/content/dam/phoenix/pddsite/documents/staffreports/[^"]+\.pdf)"',
        re.I,
    )

    for m in pdf_pattern.finditer(html):
        pdf_path = m.group(1)
        if _is_skip_pdf(pdf_path):
            continue

        filename = pdf_path.rsplit("/", 1)[-1] if "/" in pdf_path else pdf_path
        doc_url = f"{PHOENIX_GOV}{pdf_path}"
        title = urllib.parse.unquote(filename)
        # Clean title
        title = title.replace(".pdf", "").replace("-", " ").title().strip()

        case_number = _extract_case_number(filename)
        doc_type = _classify_document_type(filename)

        docs.append({
            "case_number": case_number,
            "document_url": doc_url,
            "document_title": title,
            "document_type": doc_type,
            "source_url": STAFF_REPORTS_URL,
            "source_page": "staff-reports",
        })

    return docs


def fetch_pud_case_docs() -> list[dict]:
    """Fetch PUD case documents from the PDD PUD cases page.

    Returns list of document dicts with:
      case_number, document_url, document_title, document_type, source_url
    """
    try:
        html = _fetch(PUD_CASES_URL)
    except Exception:
        return []

    docs: list[dict] = []
    # Extract PDF links from AEM DAM — PUD docs live in planning-zoning-pud
    pdf_pattern = re.compile(
        r'href="(/content/dam/phoenix/pddsite/documents/planning-zoning-pud/[^"]+\.pdf)"',
        re.I,
    )
    # Also check /content/dam/phoenix/pddsite/documents/pz/ (other planning docs)
    pdf_pattern2 = re.compile(
        r'href="(/content/dam/phoenix/pddsite/documents/pz/[^"]+\.pdf)"',
        re.I,
    )

    seen_urls: set[str] = set()

    for pdf_path in pdf_pattern.findall(html) + pdf_pattern2.findall(html):
        if _is_skip_pdf(pdf_path):
            continue

        doc_url = f"{PHOENIX_GOV}{pdf_path}"
        if doc_url in seen_urls:
            continue
        seen_urls.add(doc_url)

        doc_filename = pdf_path.rsplit("/", 1)[-1] if "/" in pdf_path else pdf_path
        # Decode percent-encoded filename
        doc_filename = urllib.parse.unquote(doc_filename)
        title = doc_filename.replace(".pdf", "").replace("_", " ").replace("  ", " ").strip()

        case_number = _extract_case_number(doc_filename)
        doc_type = _classify_document_type(doc_filename)

        docs.append({
            "case_number": case_number,
            "document_url": doc_url,
            "document_title": title,
            "document_type": doc_type,
            "source_url": PUD_CASES_URL,
            "source_page": "pud-cases",
        })

    return docs


# ── DB Integration ──────────────────────────────────────────────────────────

def sync_calendar_events(session, events: list[dict], force: bool = False) -> int:
    """Store planning calendar events as Meeting records.

    Matches existing meetings by body + meeting_id (date-based).
    Returns count of meetings synced.
    """
    from db import replace_meeting_data_safe
    from db import Meeting as MeetingModel
    from sqlalchemy import select

    count = 0
    for ev in events:
        title = ev["title"]
        body_code, display_name = _resolve_body_code(title)
        if body_code == "phoenix-aem" and not title:
            continue

        # Build meeting_id from date + slug
        event_date = ev.get("event_date", "")
        date_part = event_date[:10] if event_date else "unknown"
        meeting_id = f"phx-planning-{date_part}-{body_code}"

        existing = session.execute(
            select(MeetingModel).where(
                MeetingModel.body == body_code,
                MeetingModel.meeting_id == meeting_id,
            )
        ).scalar_one_or_none()
        if existing and existing.sync_status == "complete" and not force:
            continue

        meeting_dict = {
            "meeting_id": meeting_id,
            "meeting_date": event_date[:10] if event_date else "",
            "meeting_type": "Planning Event",
            "meeting_title": title,
            "meeting_context": ev.get("location", ""),
            "display_name": display_name,
            "source_url": ev.get("url", ""),
        }

        replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
        count += 1

    session.commit()
    return count


def sync_case_docs(session, docs: list[dict], force: bool = False) -> tuple[int, int]:
    """Store planning case documents as Case + SupportingDocument records.

    For each document:
      - If it has a case_number, create/update a Case record.
      - Create a SupportingDocument record for the PDF.

    Returns (cases_synced, docs_synced) counts.
    """
    from db import replace_meeting_data_safe
    from db import Meeting as MeetingModel
    from sqlalchemy import select

    # Group docs by case number for meeting-level storage
    # Use a shared virtual meeting per source page
    source_meetings = {
        "staff-reports": {
            "meeting_id": "phx-planning-staff-reports",
            "meeting_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "meeting_type": "Planning Case Documents",
            "meeting_title": "PDD Staff Reports",
            "source_url": STAFF_REPORTS_URL,
        },
        "pud-cases": {
            "meeting_id": "phx-planning-pud-cases",
            "meeting_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "meeting_type": "Planning Case Documents",
            "meeting_title": "PDD PUD Cases",
            "source_url": PUD_CASES_URL,
        },
    }

    by_source: dict[str, list[dict]] = {}
    for d in docs:
        source = d.get("source_page", "staff-reports")
        by_source.setdefault(source, []).append(d)

    total_cases = 0
    total_docs = 0

    for source, source_docs in by_source.items():
        meeting = source_meetings.get(source, source_meetings["staff-reports"])

        # Build agenda items + supporting documents from the case documents
        agenda_dicts: list[dict] = []
        supporting_dicts: list[dict] = []

        for i, d in enumerate(source_docs):
            item_number = str(i + 1)
            case_no = d.get("case_number") or "UNKNOWN"
            doc_url = d.get("document_url", "")
            summary = d.get("case_summary", {}) or {}

            # Build the agenda item text, enriched with case summary if available
            item_text_parts = [f"Case: {case_no}", f"Type: {d.get('document_type', '')}"]
            if summary.get("request_from") and summary.get("request_to"):
                item_text_parts.append(f"Zone change: {summary['request_from']} \u2192 {summary['request_to']}")
            if summary.get("proposal"):
                item_text_parts.append(f"Proposal: {summary['proposal']}")
            if summary.get("location"):
                item_text_parts.append(f"Location: {summary['location']}")
            if summary.get("owner"):
                item_text_parts.append(f"Owner: {summary['owner']}")
            if summary.get("applicant"):
                item_text_parts.append(f"Applicant: {summary['applicant']}")
            if summary.get("staff_recommendation"):
                item_text_parts.append(f"Staff Recommendation: {summary['staff_recommendation']}")
            if summary.get("vpc_committee"):
                item_text_parts.append(f"VPC: {summary['vpc_committee']} ({summary.get('vpc_meeting_date', '?')})")
            if summary.get("pc_hearing_date"):
                item_text_parts.append(f"PC Hearing: {summary['pc_hearing_date']}")

            agenda_dicts.append({
                "agenda_item_id": f"phx-planning-{source}-{item_number}",
                "meeting_id": meeting["meeting_id"],
                "agenda_item_number": item_number,
                "agenda_item_title": d.get("document_title", ""),
                "agenda_item_text": "\n".join(item_text_parts),
                "source_body": "phoenix-pdd",
                "source_url": doc_url,
                "c_number": case_no,
                "c_number_base": case_no.split("-")[0] if "-" in case_no else case_no,
                "agenda_item_url": doc_url,
                "item_type": d.get("document_type", "Case Document"),
            })

            # Build supporting document entry
            file_name = doc_url.rsplit("/", 1)[-1] if "/" in doc_url else ""
            _, ext = (file_name.rsplit(".", 1) + [""])[:2]
            supporting_dicts.append({
                "agenda_item_id": f"phx-planning-{source}-{item_number}",
                "document_url": doc_url,
                "document_title": d.get("document_title", ""),
                "document_type": d.get("document_type", "Case Document"),
                "c_number": case_no,
                "c_number_base": case_no.split("-")[0] if "-" in case_no else case_no,
                "file_name": file_name,
                "file_extension": ext,
                "text_content": d.get("pdf_text") or "",
            })
            total_docs += 1

        replace_meeting_data_safe(
            session,
            "phoenix-pdd",
            meeting["meeting_id"],
            meeting,
            agenda_dicts,
            supporting_dicts,
        )
        total_cases += len(source_docs)

    session.commit()
    return total_cases, total_docs


# ── Case Cross-Referencing ──────────────────────────────────────────────────

def link_staff_reports_to_meetings(session) -> int:
    """Cross-reference staff reports with meeting agenda items by case number.

    For every meeting agenda item that has a c_number, look for a matching
    staff report (stored as an agenda item under the phoenix-pdd virtual
    meeting).  If found, create a SupportingDocument record linking the
    staff report PDF to the meeting's agenda item.

    Returns the number of links created.
    """
    from db.models import Meeting, AgendaItem, SupportingDocument, PZItemDetail
    from sqlalchemy import select, and_, or_

    link_count = 0

    # 1. Get all staff reports (agenda items under phoenix-pdd meetings)
    staff_report_items: dict[str, list[AgendaItem]] = {}
    stmt = select(AgendaItem).where(
        AgendaItem.body == "phoenix-pdd",
        AgendaItem.c_number != "",
        AgendaItem.c_number.isnot(None),
    )
    for item in session.execute(stmt).scalars().all():
        cn = item.c_number.strip().upper()
        staff_report_items.setdefault(cn, []).append(item)

    if not staff_report_items:
        log.info("No staff report case numbers found in DB")
        return 0

    log.info("Found %d unique staff report case numbers", len(staff_report_items))

    # 2. Get all PZItemDetails with case numbers (Planning Commission, ZA, etc.)
    # These are the meeting-level case references
    stmt_pz = select(PZItemDetail).where(
        PZItemDetail.case_number != "",
        PZItemDetail.case_number.isnot(None),
        PZItemDetail.body != "phoenix-pdd",
    )
    pz_items = session.execute(stmt_pz).scalars().all()
    log.info("Found %d PZItemDetails with case numbers", len(pz_items))

    # 3. Get all agenda items with case numbers (from any meeting body)
    stmt_items = select(AgendaItem).where(
        AgendaItem.c_number != "",
        AgendaItem.c_number.isnot(None),
        AgendaItem.body != "phoenix-pdd",
    )
    agenda_items = session.execute(stmt_items).scalars().all()

    # Map by case number for fast lookup
    meeting_items_by_case: dict[str, list[AgendaItem]] = {}
    meeting_items_by_case_stem: dict[str, list[AgendaItem]] = {}
    for item in agenda_items:
        cn = item.c_number.strip().upper()
        meeting_items_by_case.setdefault(cn, []).append(item)
        # Also match on stem (base case number without suffix)
        stem = cn.split("-")[0] if "-" in cn else cn
        meeting_items_by_case_stem.setdefault(stem, []).append(item)

    # 4. Match and link
    existing_links: set[tuple[int, str]] = set()
    stmt_links = select(SupportingDocument.agenda_item_id, SupportingDocument.document_url).where(
        SupportingDocument.body == "phoenix-pdd",
    )
    for aid, url in session.execute(stmt_links).all():
        existing_links.add((int(aid), url))

    for staff_cn, staff_items in staff_report_items.items():
        # Try exact case number match first
        matched_items = meeting_items_by_case.get(staff_cn, [])

        # Try stem match (e.g., Z-134-24 matches Z-134-24-5)
        if not matched_items:
            stem = staff_cn.split("-")[0] if "-" in staff_cn else staff_cn
            matched_items = [
                it for it in meeting_items_by_case_stem.get(stem, [])
                if it.c_number.strip().upper().startswith(staff_cn)
                or staff_cn.startswith(it.c_number.strip().upper())
            ]

        if not matched_items:
            continue

        # Get the staff report document URL from the first matching staff item
        staff_item = staff_items[0]
        doc_url = staff_item.source_url

        for meeting_item in matched_items:
            link_key = (meeting_item.id, doc_url)
            if link_key in existing_links:
                continue

            # Create SupportingDocument record
            file_name = doc_url.rsplit("/", 1)[-1] if "/" in doc_url else ""
            _, ext = (file_name.rsplit(".", 1) + [""])[:2]

            doc = SupportingDocument(
                body="phoenix-pdd",
                agenda_item_id=str(meeting_item.id),
                meeting_id=meeting_item.meeting_id,
                meeting_db_id=meeting_item.meeting_db_id,
                agenda_item_number=meeting_item.agenda_item_number,
                c_number=staff_cn,
                c_number_base=staff_cn.split("-")[0] if "-" in staff_cn else staff_cn,
                document_title=staff_item.agenda_item_title or "Staff Report",
                document_url=doc_url,
                document_type="Staff Report",
                file_name=file_name,
                file_extension=ext,
            )
            session.add(doc)
            existing_links.add(link_key)
            link_count += 1

    session.commit()
    log.info("Created %d staff report → meeting links", link_count)
    return link_count


# ── Main Entry Point ────────────────────────────────────────────────────────

def sync_all(session, force: bool = False) -> dict:
    """Fetch all three data sources and sync to database.

    Args:
        session: SQLAlchemy session
        force: Re-sync even if already complete

    Returns:
        Dict with counts: events, staff_docs, pud_docs
    """
    results = {}

    # 1. Calendar events
    log.info("Fetching PDD calendar events...")
    events = fetch_calendar_events()
    event_count = sync_calendar_events(session, events, force=force)
    log.info("Synced %d calendar events", event_count)
    results["events"] = {"fetched": len(events), "synced": event_count}

    # 2. Staff reports — fetch, enrich with PDF text, then sync
    log.info("Fetching staff reports...")
    staff_docs = fetch_staff_report_docs()
    log.info("Downloading and extracting %d staff report PDFs...", len(staff_docs))
    staff_docs = enrich_staff_report_docs_with_text(staff_docs, force=force)
    staff_cases, staff_doc_count = sync_case_docs(session, staff_docs, force=force)
    log.info("Synced %d staff report docs (with extracted text) across %d cases",
             staff_doc_count, staff_cases)
    results["staff_reports"] = {
        "fetched": len(staff_docs),
        "docs_synced": staff_doc_count,
        "text_extracted": staff_doc_count,
    }

    # 3. PUD cases
    log.info("Fetching PUD case docs...")
    pud_docs = fetch_pud_case_docs()
    pud_cases, pud_doc_count = sync_case_docs(session, pud_docs, force=force)
    log.info("Synced %d PUD docs across %d cases", pud_doc_count, pud_cases)
    results["pud_cases"] = {"fetched": len(pud_docs), "docs_synced": pud_doc_count}

    # 4. Cross-reference staff reports to meeting agenda items
    log.info("Cross-referencing staff reports to meetings by case number...")
    links = link_staff_reports_to_meetings(session)
    results["links"] = {"created": links}
    log.info("Created %d staff report → meeting links", links)

    return results


# ── PDF Text Extraction ────────────────────────────────────────────────────

def _download_pdf_text(pdf_url: str, timeout: int = 30) -> Optional[str]:
    """Download a PDF and extract its text using pdftotext.

    Returns the extracted text (with layout preserved) or None on failure.
    """
    import subprocess
    import tempfile
    import os

    tmp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = tmp_pdf.name
    try:
        req = urllib.request.Request(pdf_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tmp_pdf.write(resp.read())
        tmp_pdf.close()

        result = subprocess.run(
            ["pdftotext", "-layout", tmp_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("pdftotext failed for %s: %s", pdf_url, result.stderr[:200])
            return None
        text = result.stdout
        if not text.strip():
            log.warning("pdftotext returned empty for %s", pdf_url)
            return None
        return text
    except Exception as e:
        log.warning("PDF extraction failed for %s: %s", pdf_url, e)
        return None
    finally:
        tmp_pdf.close()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def extract_agenda_items_from_notice(text: str) -> list[dict]:
    """Parse agenda items from a Phoenix AEM meeting notice PDF.

    Handles several formats seen across Phoenix public bodies:

      1. Numbered items (VPCs, Board of Adjustment, etc.):

          1.   Call to order, introductions and announcements by Chair.
          2.   Review and approval of minutes...

      2. Single-item formats (Historic Preservation, Site Plan Review):

          Property: 2944 North 16th Avenue
          Review of Preliminary Site Plan...

      3. Plain text agendas with no numbered items (Planning Hearing Officer)

    Returns a list of item dicts with:
      agenda_item_number, agenda_item_title, agenda_item_text, c_number
    """
    items: list[dict] = []

    # Find the agenda section
    agenda_marker = re.search(
        r"(?:the\s+)?agenda\s+(?:of|for)\s+the\s+meeting\s+is\s+as\s+follows\s*:",
        text, re.I
    )
    if not agenda_marker:
        # Try alternate markers
        alt_marker = re.search(
            r"agenda\s*(?:of|for|is|\-|:)|hearing\s+agenda|review\s+of\s+(?:preliminary\s+)?site\s+plan",
            text, re.I
        )
        if alt_marker:
            agenda_marker = alt_marker

    if not agenda_marker:
        log.debug("No agenda marker found in notice PDF")
        return items

    agenda_text = text[agenda_marker.end():]
    # Stop at common post-agenda markers
    end_markers = [
        r"the\s+next\s+.+?meeting\s+is\s+scheduled",
        r"for\s+further\s+information\s*,",
        r"to\s+request\s+a\s+reasonable\s+accommodation",
        r"july\s+\d+,\s+\d{4}",  # date stamp at end
    ]
    earliest_end = len(agenda_text)
    for marker in end_markers:
        m = re.search(marker, agenda_text, re.I)
        if m and m.start() < earliest_end:
            earliest_end = m.start()
    agenda_text = agenda_text[:earliest_end].strip()
    if not agenda_text:
        return items

    # Try format 1: numbered or lettered agenda items
    # Matches: "1. Item", "A. Item", "10. Item", "A1. Item"
    item_pattern = re.compile(
        r"^\s*([A-Za-z0-9]+)\.{1,2}\s+(.*?)$",
        re.MULTILINE
    )
    matches = list(item_pattern.finditer(agenda_text))

    if len(matches) >= 2:
        # Numbered/lettered items found — parse them
        for i, m in enumerate(matches):
            item_num = m.group(1).strip()
            item_start = m.start()
            if i + 1 < len(matches):
                item_end = matches[i + 1].start()
            else:
                item_end = len(agenda_text)

            item_text = agenda_text[item_start:item_end].strip()
            lines = [l.strip() for l in item_text.split("\n") if l.strip()]
            title_line = lines[0] if lines else ""
            title_line = re.sub(r"^[A-Za-z0-9]+\.{1,2}\s+", "", title_line).strip()
            body = "\n".join(lines[1:]) if len(lines) > 1 else ""

            case_number = ""
            cn_match = re.search(
                r"z[-_]?\d+[-_][a-z]?[-_]?\d+",
                item_text, re.I
            )
            if cn_match:
                case_number = cn_match.group(0).replace("_", "-").upper()

            items.append({
                "agenda_item_number": str(item_num),
                "agenda_item_title": title_line,
                "agenda_item_text": body,
                "c_number": case_number,
                "c_number_base": case_number.split("-")[0] if case_number else "",
            })
        return items

    # Fallback format 2: single-item with "Property:", "Review of", or project info
    # Extract as one agenda item with the full description as text
    lines = [l.strip() for l in agenda_text.split("\n") if l.strip()]
    if lines:
        title = lines[0][:200]
        body = "\n".join(lines[1:]) if len(lines) > 1 else ""
        case_number = ""
        cn_match = re.search(
            r"z[-_]?\d+[-_][a-z]?[-_]?\d+|prelim\s+\d+",
            agenda_text, re.I
        )
        if cn_match:
            case_number = cn_match.group(0).replace("_", "-").upper()

        items.append({
            "agenda_item_number": "1",
            "agenda_item_title": title[:200],
            "agenda_item_text": body,
            "c_number": case_number,
            "c_number_base": case_number.split("-")[0] if case_number else "",
        })

    return items


def extract_case_summary_from_staff_report(text: str) -> dict:
    """Parse structured case info from the first page of a staff report PDF.

    Staff reports have a standard header with:
      - Case number / title
      - Meeting dates (VPC, Planning Commission)
      - Request From/To zoning
      - Proposal description
      - Location
      - Owner / Applicant
      - Staff recommendation
    """
    info: dict[str, str] = {}

    # Case number (already extracted from filename, but capture from text too)
    cn_match = re.search(
        r"staff\s+report\s+(z[-_]?\d+[-_][a-z]?[-_]?\d+)",
        text, re.I
    )
    if cn_match:
        info["case_number"] = cn_match.group(1).replace("_", "-").upper()

    # Meeting dates
    vpc_match = re.search(r"(\w+\s+village\s+planning\s+committee).*?([a-z]+\s+\d+,\s+\d{4})", text, re.I)
    if vpc_match:
        info["vpc_meeting_date"] = vpc_match.group(2)
        info["vpc_committee"] = vpc_match.group(1).strip()

    pc_match = re.search(r"planning\s+commission\s+hearing\s+date\s*:\s*([a-z]+\s+\d+,\s+\d{4})", text, re.I)
    if pc_match:
        info["pc_hearing_date"] = pc_match.group(1)

    # Request From/To zoning
    req_from = re.search(r"request\s+from\s*:\s*(.+?)(?=request\s+to|proposal|location|owner|$)", text, re.I | re.DOTALL)
    if req_from:
        info["request_from"] = req_from.group(1).strip()[:200]

    req_to = re.search(r"request\s+to\s*:\s*(.+?)(?=proposal|location|owner|applicant|staff\s+recommendation|$)", text, re.I | re.DOTALL)
    if req_to:
        info["request_to"] = req_to.group(1).strip()[:200]

    # Proposal
    proposal = re.search(r"proposal\s*:\s*(.+?)(?=location|owner|applicant|staff\s+recommendation|$)", text, re.I | re.DOTALL)
    if proposal:
        info["proposal"] = proposal.group(1).strip()[:300]

    # Location
    loc = re.search(r"location\s*:\s*(.+?)(?=owner|applicant|staff\s+recommendation|$)", text, re.I | re.DOTALL)
    if loc:
        info["location"] = loc.group(1).strip()[:300]

    # Owner / Applicant
    owner = re.search(r"owner\s*:\s*(.+?)(?=applicant|staff\s+recommendation|$)", text, re.I | re.DOTALL)
    if owner:
        info["owner"] = owner.group(1).strip()[:200]

    applicant = re.search(r"applicant/representative\s*:\s*(.+?)(?=staff\s+recommendation|$)", text, re.I | re.DOTALL)
    if applicant:
        info["applicant"] = applicant.group(1).strip()[:200]

    # Staff recommendation — match until double newline, end of section, or next heading
    rec = re.search(
        r"staff\s+recommendation\s*:\s*(.+?)(?:\n{2,}|general\s+plan\s+conformity|$)",
        text, re.I | re.DOTALL
    )
    if rec:
        info["staff_recommendation"] = rec.group(1).strip()[:300]

    return info


def enrich_notice_meetings_with_pdf_items(meetings: list[dict], force: bool = False) -> list[dict]:
    """For each meeting dict that has a pdf_url, download the PDF and extract
    agenda items.  Mutates meetings in place and returns them.

    Only processes each meeting once (unless force=True).
    """
    from scraper.common.utils import log as _log

    for m in meetings:
        pdf_url = m.get("pdf_url") or m.get("source_url", "")
        if not pdf_url:
            continue

        # Skip if already has agenda items (unless forced)
        if not force and m.get("agenda_items"):
            continue

        log.info("Extracting PDF for %s: %s", m.get("meeting_id", "?"), pdf_url[:80])
        text = _download_pdf_text(pdf_url)
        if not text:
            log.warning("  No text extracted from %s", pdf_url)
            continue

        items = extract_agenda_items_from_notice(text)
        log.info("  Extracted %d agenda items", len(items))
        m["agenda_items"] = items

    return meetings


def enrich_staff_report_docs_with_text(docs: list[dict], force: bool = False) -> list[dict]:
    """Download each staff report PDF and extract structured case summary + full text.

    Adds the following keys to each doc dict:
      - pdf_text: full extracted text
      - case_summary: structured summary (zoning, location, applicant, recommendation)
    """
    for d in docs:
        doc_url = d.get("document_url", "")
        if not doc_url:
            continue

        # Skip if already processed (unless forced)
        if not force and d.get("pdf_text"):
            continue

        log.info("Downloading staff report PDF: %s", doc_url[:80])
        text = _download_pdf_text(doc_url)
        if not text:
            log.warning("  No text extracted from %s", doc_url)
            continue

        d["pdf_text"] = text

        # Extract structured case summary from first page
        summary = extract_case_summary_from_staff_report(text)
        if summary:
            d["case_summary"] = summary
            log.info("  Case: %s | Rec: %s",
                     summary.get("case_number", "?"),
                     summary.get("staff_recommendation", "?")[:50])

    return docs


def sync_staff_report_text_content(session, docs: list[dict]) -> int:
    """Store PDF-extracted text content on existing supporting documents.

    For each doc with pdf_text, updates the meeting's agenda item with
    the extracted text and case summary.
    """
    from db import Meeting as MeetingModel, AgendaItem as AgendaItemModel
    from sqlalchemy import select, update

    count = 0
    for d in docs:
        doc_url = d.get("document_url", "")
        text = d.get("pdf_text", "")
        summary = d.get("case_summary", {})
        if not doc_url or not text:
            continue

        # Find the agenda item that matches this doc URL
        stmt = select(AgendaItemModel).where(
            AgendaItemModel.source_url == doc_url
        )
        item = session.execute(stmt).scalar_one_or_none()
        if not item:
            # Try matching by case number prefix in c_number
            case_no = d.get("case_number", summary.get("case_number", ""))
            if case_no:
                stmt = select(AgendaItemModel).where(
                    AgendaItemModel.c_number == case_no
                )
                item = session.execute(stmt).scalar_one_or_none()

        if item:
            item.agenda_item_text = text
            if summary.get("staff_recommendation"):
                item.agenda_item_text = (
                    f"Staff Recommendation: {summary['staff_recommendation']}\n\n"
                    + item.agenda_item_text
                )
            if summary.get("request_from"):
                item.meeting_context = (
                    f"Zone change: {summary['request_from']} → {summary['request_to']}"
                )
            count += 1

    session.commit()
    return count


if __name__ == "__main__":
    # Quick test — print what we'd collect
    logging.basicConfig(level=logging.INFO)

    print("=== Calendar Events ===")
    events = fetch_calendar_events()
    print(f"  Found {len(events)} events")
    for e in events[:10]:
        print(f"  {e['event_date'][:10]} {e['title'][:50]}")

    print("\n=== Staff Reports ===")
    staff = fetch_staff_report_docs()
    print(f"  Found {len(staff)} docs")
    for d in staff[:15]:
        cn = d.get("case_number", "?")
        print(f"  {cn:25s} {d['document_type'][:20]:20s} {d['document_title'][:50]}")

    print("\n=== PUD Cases ===")
    pud = fetch_pud_case_docs()
    print(f"  Found {len(pud)} docs")
    for d in pud[:20]:
        cn = d.get("case_number", "?")
        print(f"  {cn:25s} {d['document_type'][:20]:20s} {d['document_title'][:50]}")
