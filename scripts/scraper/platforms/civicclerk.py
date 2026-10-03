"""
Reusable CivicClerk platform scraper — used by Surprise, Avondale, and any
other city that uses CivicClerk for meeting management.

API: https://{city}.api.civicclerk.com/v1
Portal: https://{city}.portal.civicclerk.com
"""

from __future__ import annotations
import json
import logging
import re
import subprocess
import tempfile
import os
import time
import urllib.request
import urllib.parse
from typing import Optional

log = logging.getLogger(__name__)


def _print_timing_start(label: str) -> float:
    """Print a timing marker and return start time."""
    t = time.time()
    print(f"  [dbg] {label} start at {t:.3f}")
    return t


def _print_timing_end(label: str, start: float, note: str = "") -> None:
    """Print elapsed time from start."""
    elapsed = time.time() - start
    print(f"  [dbg] {label} done in {elapsed:.1f}s{('  (' + note + ')') if note else ''}")


class CivicClerkConfig:
    """Per-jurisdiction CivicClerk configuration."""

    def __init__(
        self,
        subdomain: str,
        body_map: dict[str, tuple[str, str, str]],
        default_body: str = "city-council",
    ):
        self.subdomain = subdomain
        self.api_base = f"https://{subdomain}.api.civicclerk.com/v1"
        self.portal_base = f"https://{subdomain}.portal.civicclerk.com"
        self.body_map = body_map
        self.default_body = default_body


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def resolve_body(config: CivicClerkConfig, category_name: str) -> tuple[str, str, str]:
    """Map CivicClerk category name to (slug, body_code, display_name)."""
    key = category_name.strip()
    if key in config.body_map:
        return config.body_map[key]
    for pattern, (slug, code, name) in config.body_map.items():
        if pattern.lower() in key.lower() or key.lower() in pattern.lower():
            return slug, code, name
    return config.default_body, config.default_body, key


def fetch_events(config: CivicClerkConfig, start_date: str = "2026-01-01") -> list[dict]:
    """Fetch all CivicClerk events from start_date onward, paginating."""
    all_events: list[dict] = []
    params = urllib.parse.urlencode({
        "$filter": f"eventDate ge {start_date}",
        "$orderby": "eventDate desc",
        "$top": 100,
    })
    url = f"{config.api_base}/Events?{params}"
    page = 0

    while url:
        page += 1
        t0 = time.time()
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            log.warning("Failed to fetch events from %s: %s", config.subdomain, e)
            break
        batch = data.get("value", [])
        all_events.extend(batch)
        elapsed = time.time() - t0
        print(f"  [dbg]   page {page}: {len(batch)} events in {elapsed:.1f}s (total: {len(all_events)})")
        url = data.get("@odata.nextLink", "")
        if url:
            # Avoid hammering the API
            time.sleep(0.5)

    print(f"  [dbg]   fetch done: {len(all_events)} total events across {page} page(s)")
    return all_events


def parse_events_to_meetings(config: CivicClerkConfig, events: list[dict]) -> list[dict]:
    """Parse CivicClerk events into meeting dicts."""
    meetings: list[dict] = []
    for e in events:
        cat = e.get("categoryName", "") or e.get("eventName", "")
        slug, code, display = resolve_body(config, cat)
        date_raw = (e.get("eventDate") or "")[:10]
        event_id = e.get("id")
        portal_url = f"{config.portal_base}/event/{event_id}/overview" if event_id else ""

        agenda_url = ""
        minutes_url = ""
        supporting_files = []

        # Map CivicClerk file types to portal URL path segments
        _PORTAL_TYPE_MAP = {
            "Agenda": "agenda",
            "Agenda Packet": "agenda",
            "Minutes": "minutes",
            "Report": "report",
            "Attachment": "attachment",
            "Supplemental": "supplemental",
            "Additional": "additional",
        }

        for pf in e.get("publishedFiles", []):
            ftype = pf.get("type", "")
            fid = pf.get("fileId")
            # Keep API download URL for agenda PDF parsing
            api_url = f"{config.api_base}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)" if fid else ""
            # Portal URL for browser viewing (no forced download)
            portal_type = _PORTAL_TYPE_MAP.get(ftype, "agenda")
            portal_file_url = f"{config.portal_base}/event/{event_id}/files/{portal_type}/{fid}" if (event_id and fid) else ""

            if ftype == "Agenda" and not agenda_url and fid:
                agenda_url = api_url
            elif ftype == "Minutes" and not minutes_url and fid:
                minutes_url = api_url

            supporting_files.append({
                "type": ftype,
                "file_name": pf.get("name", "") or f"{ftype}",
                "url": portal_file_url or api_url,
                "api_url": api_url,
            })

        meetings.append({
            "meeting_id": str(event_id) if event_id else f"cc-{date_raw}",
            "meeting_date": date_raw,
            "meeting_type": display,
            "meeting_title": e.get("eventName", ""),
            "body_slug": slug,
            "body_code": code,
            "event_id": event_id,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "supporting_files": supporting_files,
            "source_url": portal_url,
        })
    return meetings


def search_meetings(
    config: CivicClerkConfig,
    start_date: str = "2026-01-01",
    body_slugs: Optional[list[str]] = None,
) -> list[dict]:
    """Search meetings via CivicClerk API."""
    t_start = _print_timing_start(f"search_meetings({config.subdomain})")

    events = fetch_events(config, start_date)
    _print_timing_end("fetch_events", t_start, f"{len(events)} raw events")

    t_parse = time.time()
    meetings = parse_events_to_meetings(config, events)
    _print_timing_end("parse_events_to_meetings", t_parse, f"{len(meetings)} meetings")

    if body_slugs:
        n_before = len(meetings)
        meetings = [m for m in meetings if m["body_slug"] in body_slugs]
        print(f"  [dbg]   body filter: {n_before} -> {len(meetings)} meetings (slugs={body_slugs})")

    _print_timing_end("search_meetings", t_start, f"{len(meetings)} final meetings")
    return meetings


# ── PDF extraction ──

def fetch_pdf_bytes(url: str) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


def _strip_right_column(text: str, gap_min: int = 12) -> str:
    """Strip text from a right column when there's a large whitespace gap.

    Multi-column PDFs processed with ``pdftotext -layout`` preserve column
    positions, so a line like::

        District 1                                                                    Chris Sexton

    contains both the item title (left column) and the staff contact (right
    column) on a single line.  This function detects runs of whitespace >=
    *gap_min* characters and returns only the left-column portion.
    """
    m = re.match(r"^(.*?)([\t ]{%d,})(.*)$" % gap_min, text)
    if m:
        return m.group(1).strip()
    return text.strip()


def parse_agenda_items(text: str, meeting_id: str) -> list[dict]:
    """Parse numbered agenda items from PDF text.

    Handles:
    - Multi-column layouts (right-column staff names are stripped)
    - Continuation lines (appended to the previous item)
    - Lines starting with large numbers that are addresses, not item numbers
    """
    items: list[dict] = []
    sort_order = 0
    lines = text.split("\n") if text else []
    seen: set[str] = set()
    MAX_ITEM_NUM = 200

    for line in lines:
        raw_line = line
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^\s*(\d+)\.?\s+(.+?)$", s)
        if m:
            num_str = m.group(1)
            num_val = int(num_str)

            # Reject lines that start with large numbers — likely addresses
            # or page numbers, not real item numbers
            if num_val > MAX_ITEM_NUM:
                # Append as continuation text to the last item if applicable
                if items:
                    strip_cont = _strip_right_column(s)
                    items[-1]["agenda_item_text"] += "\n" + strip_cont
                continue

            title_raw = m.group(2).strip()
            title = _strip_right_column(title_raw)
            text_content = _strip_right_column(s)

            # Normalize runs of internal whitespace to single spaces
            title = re.sub(r"  +", " ", title)
            text_content = re.sub(r"  +", " ", text_content)

            key = f"{num_str}:{title[:40]}"
            if key in seen:
                continue
            seen.add(key)
            sort_order += 1
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": num_str,
                "item_type_category": "item",
                "agenda_item_title": title,
                "agenda_item_text": text_content,
                "sort_order": sort_order,
            })
        else:
            # Continuation line — append to the last item's text
            if items:
                strip_cont = _strip_right_column(s)
                strip_cont = re.sub(r"  +", " ", strip_cont)
                items[-1]["agenda_item_text"] += "\n" + strip_cont
                # If the last item has no title yet, use this as the title
                if not items[-1]["agenda_item_title"]:
                    items[-1]["agenda_item_title"] = strip_cont
    return items


def fetch_and_parse_agenda(agenda_url: str, meeting_id: str, body_code: str = "avondale-cc") -> list[dict]:
    """Download and parse agenda items from a CivicClerk agenda PDF."""
    pdf_bytes = fetch_pdf_bytes(agenda_url)
    if not pdf_bytes:
        return []
    text = extract_pdf_text(pdf_bytes)
    if not text or len(text) < 100:
        return []
    items = parse_agenda_items(text, meeting_id)
    for item in items:
        an = item.get("agenda_item_number", "") or ""
        item["agenda_item_id"] = f"{body_code}-{meeting_id}_{an}"
        item["source_body"] = body_code
        item["source_url"] = agenda_url
    return items


# ── Portal file discovery (Playwright) ──

def fetch_event_files_via_portal(config: CivicClerkConfig, event_id: int) -> list[dict]:
    """Scrape the CivicClerk portal files page to discover ALL files for an event.

    The CivicClerk API only exposes published files (Agenda, Minutes) through
    its Events endpoint.  Additional files (reports, attachments) associated with
    individual agenda items are NOT available via any API endpoint \u2014 they are
    only rendered client-side by the React portal at::

        {portal_base}/event/{event_id}/files

    This function uses Playwright to load that page, wait for the React app to
    render, and extract all file links from the DOM.

    Parameters
    ----------
    config : CivicClerkConfig
        Jurisdiction configuration.
    event_id : int
        CivicClerk event ID.

    Returns
    -------
    list of dict
        Each dict has keys: ``file_name``, ``file_url``, ``file_type``.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("playwright not installed \u2014 cannot scrape portal files for event %s", event_id)
        return []

    import asyncio

    files_url = f"{config.portal_base}/event/{event_id}/files"
    log.info("Scraping portal files page: %s", files_url)

    async def _scrape() -> list[dict]:
        discovered: list[dict] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            )
            page = await context.new_page()

            try:
                await page.goto(files_url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(5000)

                file_entries = await page.eval_on_selector_all(
                    'a[href*="blob.core.windows.net"]',
                    """(elements) => elements.map(el => ({
                        href: el.href,
                        text: el.textContent.trim(),
                        id: el.id || \"\"
                    }))""",
                )

                for entry in file_entries:
                    text = entry.get("text", "").strip()
                    el_id = entry.get("id", "")
                    if not text or not el_id:
                        continue

                    clean_name = re.sub(r'\s*\(PDF\)\s*$', '', text, flags=re.IGNORECASE).strip()

                    discovered.append({
                        "file_name": clean_name or f"File ({event_id})",
                        "file_url": "",  # will construct portal URL from element ID
                        "file_type": "Document",
                        "element_id": el_id,
                    })

            except Exception as e:
                log.warning("Failed to scrape portal files for event %s: %s", event_id, e)
            finally:
                await browser.close()

        return discovered

    # Run async scrape in a separate thread with its own event loop
    # to avoid conflicts with any existing running loop
    import threading
    result_holder: list[list[dict]] = []
    exc_holder: list[Exception] = []

    def _run_in_thread():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            r = loop.run_until_complete(_scrape())
            loop.close()
            result_holder.append(r)
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    t.join(timeout=45)

    if exc_holder:
        log.warning("Portal file scrape failed for event %s: %s", event_id, exc_holder[0])
        return []
    if not result_holder:
        return []

    discovered = result_holder[0]

    # Construct permanent portal view URLs from extracted file IDs
    # (never store time-limited Azure blob SAS URLs)
    portal_files: list[dict] = []
    seen_ids: set[int] = set()

    for f in discovered:
        el_id = f.get("element_id", "")
        if not el_id:
            continue

        # Extract fileId from element ID like "downloadReportFilesMenu-8319-menuitem-0"
        id_match = re.search(r"-(\d+)-menuitem", el_id)
        if not id_match:
            continue
        file_id = int(id_match.group(1))
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)

        # Determine file type from element ID prefix
        portal_type = "document"
        if el_id.startswith("downloadReportFilesMenu-"):
            portal_type = "report"
        elif el_id.startswith("downloadMinutesFileMenu-") or el_id.startswith("downloadMinutesFilesMenu-"):
            portal_type = "minutes"
        elif el_id.startswith("downloadAgendaFileMenu-") or el_id.startswith("downloadAgendaFilesMenu-"):
            portal_type = "agenda"
        elif el_id.startswith("downloadAttachmentFileMenu-") or el_id.startswith("downloadAttachmentFilesMenu-"):
            portal_type = "attachment"

        portal_url = f"{config.portal_base}/event/{event_id}/files/{portal_type}/{file_id}"

        display_type = portal_type.capitalize()
        display_type = display_type.replace("Report", "Report").replace("Attachment", "Attachment")

        portal_files.append({
            "file_name": f["file_name"],
            "file_url": portal_url,
            "file_type": display_type,
        })

    log.info("Discovered %d files via portal for event %s", len(portal_files), event_id)
    return portal_files


# ── Meeting API item extraction ──

def fetch_meeting_items(
    config: CivicClerkConfig,
    event_id: int,
    meeting_id: int,
    body_code: str,
    meeting_date: str = "",
) -> tuple[list[dict], list[dict]]:
    """Fetch structured agenda items and supporting docs from the CivicClerk Meetings API.

    Uses the ``Meetings/{meeting_id}`` endpoint which returns:
    - ``items`` — a tree of section headers and child items with outline numbers
    - ``publishedFiles`` — meeting-level PDFs (Agenda, Agenda Packet, Minutes)
    - Per-item ``attachmentsList`` and ``reportsList`` — staff reports & exhibits

    Returns
    -------
    (agenda_items, support_docs)
        Each item dict matches the schema expected by
        ``db.persist.replace_meeting_data_safe``.
    """
    import urllib.request

    t0 = time.time()
    url = f"{config.api_base}/Meetings/{meeting_id}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning("Failed to fetch meeting %s for event %s: %s", meeting_id, event_id, e)
        return [], []
    api_elapsed = time.time() - t0
    raw_items = data.get("items", [])
    pfs = data.get("publishedFiles", [])
    sort_order = 0
    agenda_items: list[dict] = []
    docs: list[dict] = []
    seen_numbers: set[str] = set()

    # -- Parse all items (sections + children) --
    for section in raw_items:
        outline = (section.get("agendaObjectItemOutlineNumber") or "").strip()
        name = (section.get("agendaObjectItemName") or "").strip()
        desc = (section.get("agendaObjectItemDescription") or "").strip()

        # Clean HTML from name and description
        name = re.sub(r"<[^>]+>", "", name).strip()
        if desc:
            desc = re.sub(r"<[^>]+>", "", desc).strip()

        # Skip empty/deleted items
        if not name and not outline:
            continue

        # Build a composite text (title + description)
        item_text = name
        if desc and desc != name:
            item_text += "\n" + desc

        item_number = outline.rstrip(".") if outline else f"s{sort_order + 1}"
        dedup_key = f"{item_number}:{name[:60]}"

        if dedup_key not in seen_numbers:
            sort_order += 1
            seen_numbers.add(dedup_key)
            aid = f"{body_code}-{meeting_date}_{item_number}" if meeting_date else f"{body_code}-e{event_id}_{item_number}"

            agenda_items.append({
                "agenda_item_id": aid,
                "agenda_item_number": item_number,
                "item_type_category": "item",
                "agenda_item_title": name,
                "agenda_item_text": item_text,
                "sort_order": sort_order,
                "source_body": body_code,
            })

        # -- Extract child items --
        children = section.get("childItems") or []
        for child in children:
            child_outline = (child.get("agendaObjectItemOutlineNumber") or "").strip()
            child_name = (child.get("agendaObjectItemName") or "").strip()
            child_desc = (child.get("agendaObjectItemDescription") or "").strip()

            child_name = re.sub(r"<[^>]+>", "", child_name).strip()

            if not child_name:
                continue

            if child_desc:
                child_desc = re.sub(r"<[^>]+>", "", child_desc).strip()

            child_text = child_name
            if child_desc and child_desc != child_name:
                child_text += "\n" + child_desc

            child_leaf = child_outline.rstrip(".")
            if child_leaf:
                # CivicClerk commonly restarts A/B/C beneath each Roman-numeral
                # section. A bare child outline is therefore not meeting-unique;
                # preserve its parent path (I.A, IV.A, ...). Some tenants already
                # return a fully qualified child outline, which we keep as-is.
                child_number = (
                    child_leaf
                    if child_leaf.startswith(f"{item_number}.")
                    else f"{item_number}.{child_leaf}"
                )
            else:
                child_number = f"{item_number}.{sort_order + 1}"
            child_key = f"{child_number}:{child_name[:60]}"

            if child_key not in seen_numbers:
                sort_order += 1
                seen_numbers.add(child_key)
                caid = f"{body_code}-{meeting_date}_{child_number}" if meeting_date else f"{body_code}-e{event_id}_{child_number}"

                agenda_items.append({
                    "agenda_item_id": caid,
                    "agenda_item_number": child_number,
                    "item_type_category": "item",
                    "agenda_item_title": child_name,
                    "agenda_item_text": child_text,
                    "sort_order": sort_order,
                    "source_body": body_code,
                })

            # -- Extract attachments from child items --
            for att in child.get("attachmentsList") or []:
                # Prefer CivicClerk's PDF rendition (including for uploaded DOCX
                # files) so extraction receives a directly readable document.
                # mediaFullPath is often a relative storage key on newer tenants.
                doc_url = (
                    att.get("pdfAVersionFullPath")
                    or att.get("pdfVersionFullPath")
                    or att.get("mediaFullPath", "")
                )
                if doc_url and not doc_url.startswith("http"):
                    doc_url = f"{config.api_base}/{doc_url}"
                doc_name = att.get("fileName", "") or att.get("mediaFileName", "Attachment")
                if doc_url:
                    docs.append({
                        "agenda_item_id": "0",
                        "agenda_item_number": child_number,
                        "document_title": doc_name,
                        "document_url": doc_url,
                        "document_type": "Attachment",
                        "body": body_code,
                        "meeting_id": str(event_id),
                    })

            # -- Extract reports from child items --
            for rep in child.get("reportsList") or []:
                rep_url = rep.get("pdfMediaFullPath", "")
                rep_name = rep.get("agendaObjItemReportName", "Item Report")
                if rep_url:
                    docs.append({
                        "agenda_item_id": "0",
                        "agenda_item_number": child_number,
                        "document_title": rep_name,
                        "document_url": rep_url,
                        "document_type": "Staff Report",
                        "body": body_code,
                        "meeting_id": str(event_id),
                    })

        # -- Extract attachments from the section itself --
        for att in section.get("attachmentsList") or []:
            doc_url = (
                att.get("pdfAVersionFullPath")
                or att.get("pdfVersionFullPath")
                or att.get("mediaFullPath", "")
            )
            if doc_url and not doc_url.startswith("http"):
                doc_url = f"{config.api_base}/{doc_url}"
            doc_name = att.get("fileName", "") or att.get("mediaFileName", "Attachment")
            if doc_url:
                docs.append({
                    "agenda_item_id": "0",
                    "agenda_item_number": item_number,
                    "document_title": doc_name,
                    "document_url": doc_url,
                    "document_type": "Attachment",
                    "body": body_code,
                    "meeting_id": str(event_id),
                })

        for rep in section.get("reportsList") or []:
            rep_url = rep.get("pdfMediaFullPath", "")
            rep_name = rep.get("agendaObjItemReportName", "Item Report")
            if rep_url:
                docs.append({
                    "agenda_item_id": "0",
                    "agenda_item_number": item_number,
                    "document_title": rep_name,
                    "document_url": rep_url,
                    "document_type": "Staff Report",
                    "body": body_code,
                    "meeting_id": str(event_id),
                })

    # -- Extract meeting-level published files --
    for pf in pfs:
        ftype = pf.get("type", "")
        file_id = pf.get("fileId")
        # GetMeetingFile returns JSON metadata on current CivicClerk tenants;
        # GetMeetingFileStream is the stable binary document endpoint.
        furl = (
            f"{config.api_base}/Meetings/"
            f"GetMeetingFileStream(fileId={file_id},plainText=false)"
            if file_id
            else (pf.get("url", "") or "")
        )
        fname = pf.get("name", "") or ftype or "Meeting Document"
        if furl:
            docs.append({
                "agenda_item_id": "0",
                "agenda_item_number": "",
                "document_title": fname,
                "document_url": furl,
                "document_type": ftype or "Meeting Document",
                "body": body_code,
                "meeting_id": str(event_id),
            })

    total_elapsed = time.time() - t0
    print(f"  [dbg]   fetch_meeting_items(event={event_id}, meeting={meeting_id}): "
          f"{len(agenda_items)} items, {len(docs)} docs, "
          f"API call {api_elapsed:.1f}s, total {total_elapsed:.1f}s")
    log.info(
        "Fetched meeting %s for event %s: %d items, %d docs (%.1fs)",
        meeting_id, event_id, len(agenda_items), len(docs), total_elapsed,
    )
    return agenda_items, docs
