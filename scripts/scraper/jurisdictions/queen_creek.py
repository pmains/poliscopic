"""
Town of Queen Creek meeting extraction via Granicus RSS.

Granicus RSS: https://queencreekaz.granicus.com/ViewPublisherRSS.php?view_id=3&mode=agendas
Granicus Agenda PDF: https://queencreekaz.granicus.com/AgendaViewer.php?view_id=3&{event_id|clip_id}={id}
"""

from __future__ import annotations
import logging
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
import urllib.request

log = logging.getLogger(__name__)

BASE_URL = "https://queencreekaz.granicus.com"
VIEW_ID = 3
SOURCE_SYSTEM = "granicus"
RSS_URL = f"{BASE_URL}/ViewPublisherRSS.php?view_id={VIEW_ID}&mode=agendas"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

BODY_MAP: dict[str, tuple[str, str, str]] = {
    "Town Council": ("queen-creek-cc", "queen-creek-cc", "Town Council"),
    "Planning and Zoning Commission": ("queen-creek-pz", "queen-creek-pz", "Planning & Zoning Commission"),
    "Planning & Zoning Commission": ("queen-creek-pz", "queen-creek-pz", "Planning & Zoning Commission"),
    "Parks and Recreation": ("queen-creek-parks", "queen-creek-parks", "Parks & Recreation Advisory Committee"),
    "Parks & Recreation Advisory": ("queen-creek-parks", "queen-creek-parks", "Parks & Recreation Advisory Committee"),
    "Economic Development Commission": ("queen-creek-ed", "queen-creek-ed", "Economic Development Commission"),
    "Board of Adjustment": ("queen-creek-boa", "queen-creek-boa", "Board of Adjustment"),
    "Transportation Advisory": ("queen-creek-transpo", "queen-creek-transpo", "Transportation Advisory Committee"),
    "Public Safety Personnel Retirement": ("queen-creek-psprs", "queen-creek-psprs", "Public Safety Personnel Retirement Board"),
    "Teen Advisory": ("queen-creek-teen", "queen-creek-teen", "Mayor's Teen Advisory Committee"),
    "Downtown Arts": ("queen-creek-arts", "queen-creek-arts", "Downtown Arts & Placemaking Advisory"),
    "Strategic Planning": ("queen-creek-cc", "queen-creek-cc", "Town Council Strategic Planning"),
}

DEFAULT_BODY_SLUGS = ["queen-creek-cc", "queen-creek-pz"]

# ── PDF helpers ─────────────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            p = f.name
        r = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True, text=True, timeout=30)
        Path(p).unlink(missing_ok=True)
        return r.stdout.strip() if r.stdout.strip() else None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed: %s", e)
        return None

def fetch_pdf(url: str) -> Optional[bytes]:
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.read()
    except Exception as e:
        log.warning("Failed to fetch PDF %s: %s", url, e)
        return None

# ── Agenda item parsing ─────────────────────────────────────────────────────

def parse_agenda_items(pdf_text: str) -> list[dict]:
    """Extract agenda items from Queen Creek PDF agenda text.

    Divides the text into numbered items with optional lettered sub-items.
    """
    if not pdf_text:
        return []
    lines = pdf_text.splitlines()
    top_pat = re.compile(r'^\s*(\d+)\.\s+(.*)')
    sub_pat = re.compile(r'^\s*([A-Z])\.\s+(.*)')
    section_pat = re.compile(r'^\s*([A-Z][A-Z .]+)\s*$')

    items: list[dict] = []
    cur_num: Optional[str] = None
    cur_letter: Optional[str] = None
    cur_title = ""
    cur_text: list[str] = []

    def flush():
        nonlocal cur_num, cur_letter, cur_title, cur_text
        if cur_num is not None and cur_title:
            final_num = cur_num + (cur_letter or "")
            body = "\n".join(cur_text).strip()
            items.append({
                "agenda_item_number": final_num,
                "agenda_item_title": cur_title.strip(),
                "agenda_item_text": body,
            })
        cur_num = None
        cur_letter = None
        cur_title = ""
        cur_text = []

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if section_pat.match(s) and len(s) < 60:
            continue
        top_m = top_pat.match(line)
        if top_m:
            rest = top_m.group(2).strip()
            if rest:
                flush()
                cur_num = top_m.group(1)
                cur_title = rest
            continue
        sub_m = sub_pat.match(line)
        if sub_m and cur_num is not None:
            rest = sub_m.group(2).strip()
            if rest:
                if cur_letter is not None:
                    flush()
                elif (cur_title or cur_text):
                    flush()
                cur_letter = sub_m.group(1)
                cur_title = rest
            continue
        if cur_num is not None:
            cur_text.append(s)

    flush()
    # Generate agenda_item_id for each item (used as unique key in DB)
    for item in items:
        num = item["agenda_item_number"]
        item["agenda_item_id"] = f"qc-{num}"
    return items

# ── Supporting doc extraction ───────────────────────────────────────────────

def extract_supporting_docs(pdf_text: str) -> list[dict]:
    if not pdf_text:
        return []
    docs: list[dict] = []
    seen = set()
    for pat, dt in [(r"(Resolution\s+(?:No\.|#)\s*\d+[-\w]*)", "Resolution"),
                     (r"(Ordinance\s+(?:No\.|#)\s*\d+[-\w]*)", "Ordinance"),
                     (r"(Exhibit\s+[A-Z])", "Exhibit"),
                     (r"(Attachment\s+\d+)", "Attachment")]:
        for m in re.finditer(pat, pdf_text, re.IGNORECASE):
            ref = m.group(1).strip()
            k = ref.lower().replace(".", "").replace("#", "")
            if k not in seen:
                seen.add(k)
                docs.append({"document_title": ref, "document_type": dt, "document_url": ""})
    return docs

# ── RSS helpers ─────────────────────────────────────────────────────────────

def _resolve_body(title: str) -> tuple[str, str, str]:
    for key, (slug, code, display) in BODY_MAP.items():
        if key.lower() in title.lower():
            return slug, code, display
    return "queen-creek-cc", "queen-creek-cc", title

def fetch_rss() -> Optional[str]:
    try:
        req = urllib.request.Request(RSS_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch Queen Creek RSS: %s", e)
        return None

def _parse_date_from_title(title: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    if m:
        return m.group(1)
    m = re.search(r"(\w{3,9}\s+\d{1,2},\s+\d{4})", title)
    if m:
        from datetime import datetime
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""

# ── Search / Sync entry points ──────────────────────────────────────────────

def search_meetings() -> list[dict]:
    rss_xml = fetch_rss()
    if not rss_xml:
        return []
    meetings: list[dict] = []
    try:
        root = ET.fromstring(rss_xml)
    except ET.ParseError:
        return []
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        if title_el is None or link_el is None:
            continue
        title = title_el.text or ""
        link = link_el.text or ""
        em = re.search(r"event_id=(\d+)", link)
        cm = re.search(r"clip_id=(\d+)", link)
        mid = em.group(1) if em else (cm.group(1) if cm else None)
        if not mid:
            continue
        mdate = _parse_date_from_title(title)
        body_name = title.split(" - ")[0].strip() if " - " in title else title
        slug, code, display = _resolve_body(body_name)
        ag_url = f"{BASE_URL}/AgendaViewer.php?view_id={VIEW_ID}&{'event' if em else 'clip'}_id={mid}"
        meetings.append({
            "meeting_id": mid, "meeting_date": mdate, "meeting_type": display,
            "meeting_title": body_name, "body_slug": slug, "body_code": code,
            "body_name": body_name, "agenda_url": ag_url, "source_url": link,
            "source_system": SOURCE_SYSTEM,
        })
    return meetings

def extract_meeting_items(agenda_url: str) -> tuple[list[dict], list[dict]]:
    pdf_bytes = fetch_pdf(agenda_url)
    if not pdf_bytes:
        return [], []
    pdf_text = extract_pdf_text(pdf_bytes)
    if not pdf_text:
        return [], []
    items = parse_agenda_items(pdf_text)
    docs = extract_supporting_docs(pdf_text)
    return items, docs


def sync(args) -> int:
    """Search, extract, and persist Queen Creek meetings."""
    from sqlalchemy import select

    from db import Meeting, get_session, init_db, replace_meeting_data_safe, update_sync_status

    init_db()
    print("Searching Queen Creek meetings via Granicus RSS...")
    meetings = search_meetings()
    if not meetings:
        print("No Queen Creek meetings found.")
        return 0

    start_date = getattr(args, "start_date", None)
    end_date = getattr(args, "end_date", None)
    if start_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") >= start_date]
    if end_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") <= end_date]
    if not meetings:
        print("No Queen Creek meetings found in date range.")
        return 0
    if getattr(args, "limit", None):
        meetings = meetings[: args.limit]
    print(f"Found {len(meetings)} Queen Creek meeting(s)")

    session = get_session()
    try:
        for index, meeting in enumerate(meetings, 1):
            meeting_id = meeting["meeting_id"]
            meeting_date = meeting.get("meeting_date", "")
            body_code = meeting.get("body_code", "queen-creek-cc")
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting.get("meeting_type", ""),
                "meeting_title": meeting.get(
                    "meeting_title", meeting.get("body_name", "")
                ),
                "source_url": meeting.get("source_url", ""),
            }
            existing = session.execute(
                select(Meeting).where(
                    Meeting.body == body_code,
                    Meeting.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()
            if (
                existing
                and existing.sync_status == "complete"
                and (existing.item_count_actual or 0) > 0
                and not getattr(args, "force", False)
            ):
                print(
                    f"  [{index}/{len(meetings)}] {meeting_id} {meeting_date}: "
                    f"already synced (items={existing.item_count_actual})"
                )
                continue

            items, documents = [], []
            if meeting.get("agenda_url"):
                items, documents = extract_meeting_items(meeting["agenda_url"])
            try:
                replace_meeting_data_safe(
                    session,
                    body_code,
                    meeting_id,
                    meeting_dict,
                    list(items),
                    supporting_doc_dicts=list(documents),
                )
                status = "complete" if items else "no_agenda"
                print(
                    f"  [{index}/{len(meetings)}] {meeting_id} {meeting_date}: "
                    f"{len(items)} items, {len(documents)} docs ({status})"
                )
                update_sync_status(session, body_code, meeting_id, status)
                session.commit()
            except Exception as exc:
                session.rollback()
                log.exception("Failed to sync Queen Creek meeting %s", meeting_id)
                try:
                    update_sync_status(
                        session,
                        body_code,
                        meeting_id,
                        "failed",
                        error=str(exc)[:500],
                    )
                    session.commit()
                except Exception:
                    session.rollback()
    finally:
        session.close()

    print(f"Synced {len(meetings)} Queen Creek meeting(s)")
    return 0
