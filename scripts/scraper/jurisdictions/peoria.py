"""
City of Peoria meeting extraction via PrimeGov.

Peoria has migrated from NovusAgenda to PrimeGov at peoriaaz.primegov.com.
"""

from __future__ import annotations
import json
import logging
import re
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

SOURCE_SYSTEM = "primegov"
BASE_URL = "https://peoriaaz.primegov.com"
API_URL = f"{BASE_URL}/api/v2/PublicPortal/ListArchivedMeetings"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

BODY_MAP: dict[str, str] = {
    "city council meeting": "peoria-cc",
    "regular council meeting": "peoria-cc",
    "boards and commission subcommittee": "peoria-sub",
    "virtual community facility district": "peoria-cfd",
    # Canonical code is the descriptive one; "peoria-pz" was retired in the
    # 2026-09-18 dev body-code cleanup (report docs/briefs/034-dev-body-cleanup-changes-2026-09-18.md).
    "planning and zoning": "peoria-planning-zoning",
}

DEFAULT_BODY_SLUGS = ["peoria-cc"]


def fetch_json(url: str) -> Optional[dict | list]:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None


def fetch_page(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _resolve_body(title: str) -> str:
    key = title.strip().lower()
    for pattern, code in BODY_MAP.items():
        if pattern in key:
            return code
    return "peoria-cc"


def format_date(dt_str: str) -> str:
    """Convert '2026-06-16T17:30:00' to '2026-06-16'."""
    if not dt_str:
        return ""
    return dt_str[:10]


def search_meetings() -> list[dict]:
    """Fetch archived meetings from the PrimeGov API."""
    from datetime import date
    year = date.today().year
    data = fetch_json(f"{API_URL}?year={year}")
    if not data:
        return []

    meetings: list[dict] = []
    for item in data:
        title = item.get("title", "")
        body_code = _resolve_body(title)
        meeting_id = str(item.get("id", ""))
        meeting_date = format_date(item.get("dateTime", ""))

        # Find the HTML meeting page and compiled document URLs
        agenda_url = ""
        compiled_docs = []
        for doc in item.get("documentList", []):
            tid = doc.get("templateId")
            ct = doc.get("compileOutputType")
            name = doc.get("templateName", "").lower()

            if ct == 3 and tid:
                # compileOutputType=3 = HTML meeting page
                agenda_url = f"{BASE_URL}/Portal/Meeting?meetingTemplateId={tid}"
                compiled_docs.append({
                    "title": doc.get("templateName", "HTML"),
                    "url": agenda_url,
                    "type": "html",
                })
            elif ct == 1 and tid:
                # compileOutputType=1 = PDF
                pdf_url = f"{BASE_URL}/Public/CompiledDocument?meetingTemplateId={tid}&compileOutputType=1"
                compiled_docs.append({
                    "title": doc.get("templateName", "PDF"),
                    "url": pdf_url,
                    "type": "pdf",
                })

        meetings.append({
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "meeting_title": title,
            "meeting_type": title,
            "body_code": body_code,
            "agenda_url": agenda_url,
            "compiled_docs": compiled_docs,
            "source_url": agenda_url or f"{BASE_URL}/Portal/Meeting",
            "source_system": SOURCE_SYSTEM,
        })

    return meetings


def extract_agenda_items(agenda_url: str, meeting_id: str = "") -> list[dict]:
    """Fetch the meeting page and extract agenda items."""
    html = fetch_page(agenda_url)
    m = re.search(r'id="MeetingContents"[^>]*>(.*?)</div>\s*</div>\s*</div>\s*</div>', html, re.DOTALL)
    if not m:
        m = re.search(r'id="MeetingContents"[^>]*>(.*?)</div>', html, re.DOTALL)
    if not m:
        log.warning("No MeetingContents found in %s", agenda_url)
        return []

    compiled = m.group(1)
    items: list[dict] = []
    sort_order = 0

    for p in re.finditer(r'<p[^>]*>(.*?)</p>', compiled, re.DOTALL):
        text = re.sub(r'<[^>]+>', ' ', p.group(1))
        text = re.sub(r'\s+', ' ', text).strip().replace('\u00a0', ' ')
        if not text or len(text) < 5:
            continue
        sort_order += 1
        num_m = re.match(r'^(\d+)\.\s+(.*)', text)
        if num_m:
            item_number = num_m.group(1)
            title = num_m.group(2)
        else:
            item_number = str(sort_order)
            title = text
        items.append({
            "agenda_item_number": item_number,
            "agenda_item_id": f"peoria-{meeting_id}-{item_number}",
            "agenda_item_title": title[:500],
            "agenda_item_text": "",
        })

    return items


def extract_supporting_docs(compiled_docs: list[dict]) -> list[dict]:
    docs: list[dict] = []
    for doc in compiled_docs:
        docs.append({
            "document_title": doc.get("title", "Document"),
            "document_url": doc.get("url", ""),
            "document_type": "Meeting Packet",
        })
    return docs
