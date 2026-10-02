"""City of Flagstaff meetings through AgendaQuick."""

from __future__ import annotations

import html as html_module
import re
import ssl
import urllib.parse
import urllib.request
from datetime import date

from bs4 import BeautifulSoup

BASE = "https://public.destinyhosted.com/35247/agenda/"
HEADERS = {"User-Agent": "Mozilla/5.0"}

BODY_PATTERNS = [
    ("city council", "flagstaff-cc", "City Council"),
    ("airport commission", "flagstaff-airport", "Airport Commission"),
    ("beautification", "flagstaff-bpac", "Beautification and Public Art Commission"),
    ("bicycle advisory", "flagstaff-bac", "Bicycle Advisory Committee"),
    ("board of adjustment", "flagstaff-boa", "Board of Adjustment"),
    ("building and fire", "flagstaff-bfca", "Building and Fire Code of Appeals"),
    ("diversity awareness", "flagstaff-coda", "Commission on Diversity Awareness"),
    ("inclusion and adaptive", "flagstaff-cial", "Commission on Inclusion and Adaptive Living"),
    ("housing authority", "flagstaff-fha", "Flagstaff Housing Authority"),
    ("heritage preservation", "flagstaff-hpc", "Heritage Preservation Commission"),
    ("housing commission", "flagstaff-hc", "Housing Commission"),
    ("indigenous commission", "flagstaff-ic", "Indigenous Commission"),
    ("joint parks", "flagstaff-jpro", "Joint Parks and Recreation/Open Space"),
    ("library board", "flagstaff-library", "Library Board"),
    ("open spaces", "flagstaff-osc", "Open Spaces Commission"),
    ("psprs", "flagstaff-psprs", "PSPRS Local Board"),
    ("parks & recreation", "flagstaff-pr", "Parks and Recreation Commission"),
    ("pedestrian advisory", "flagstaff-pac", "Pedestrian Advisory Committee"),
    ("planning & zoning", "flagstaff-pz", "Planning and Zoning Commission"),
    ("sustainability", "flagstaff-sc", "Sustainability Commission"),
    ("tourism commission", "flagstaff-tourism", "Tourism Commission"),
    ("water commission", "flagstaff-water", "Water Commission"),
]


def _get(url: str) -> str:
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=HEADERS), timeout=45, context=context
    ) as response:
        return response.read().decode("utf-8", "replace")


def _body(title: str) -> tuple[str, str]:
    lower = html_module.unescape(title).lower()
    for pattern, code, name in BODY_PATTERNS:
        if pattern in lower:
            return code, name
    return "flagstaff-general", "General Public Meeting"


def parse_listing(source: str) -> list[dict]:
    soup = BeautifulSoup(source, "html.parser")
    meetings = []
    for card in soup.select("div.p-2.border-top"):
        heading = card.select_one("h3")
        agenda = card.select_one('a[href*="agenda.cfm?seq="]')
        if not heading or not agenda:
            continue
        match = re.match(r"([A-Za-z]+ \d{1,2}, \d{4}):\s*(.+)", heading.get_text(" ", strip=True))
        seq_match = re.search(r"seq=(\d+)", agenda.get("href", ""))
        if not match or not seq_match:
            continue
        from datetime import datetime

        title = match.group(2).strip()
        body_code, body_name = _body(title)
        links = {
            link.get_text(" ", strip=True).lower(): urllib.parse.urljoin(BASE, link.get("href", ""))
            for link in card.select("a[href]")
        }
        lowered = title.lower()
        meeting_type = "Regular Meeting"
        if "work session" in lowered:
            meeting_type = "Work Session"
        elif "special" in lowered:
            meeting_type = "Special Meeting"
        elif "retreat" in lowered:
            meeting_type = "Retreat"
        elif "cancel" in lowered:
            meeting_type = "Cancelled"
        meetings.append(
            {
                "meeting_id": seq_match.group(1),
                "meeting_date": datetime.strptime(match.group(1), "%B %d, %Y").strftime("%Y-%m-%d"),
                "meeting_title": title,
                "meeting_type": meeting_type,
                "body_code": body_code,
                "body_name": body_name,
                "agenda_url": urllib.parse.urljoin(BASE, agenda.get("href", "")),
                "minutes_url": next(
                    (url for label, url in links.items() if label.startswith("minutes")), ""
                ),
                "source_url": urllib.parse.urljoin(BASE, agenda.get("href", "")),
            }
        )
    return meetings


def parse_agenda(source: str, meeting: dict) -> tuple[list[dict], list[dict]]:
    soup = BeautifulSoup(source, "html.parser")
    items, docs, seen_docs = [], [], set()
    current_parent = ""
    seen_numbers = set()
    for marker in soup.select('div.mediumText[id^="item-"]'):
        number_parts = []
        cursor = marker
        content = None
        for _ in range(8):
            cursor = cursor.find_next_sibling("div")
            if cursor is None:
                break
            classes = set(cursor.get("class") or [])
            if "start-at-content" in classes and "extend-to-end" in classes:
                content = cursor
                break
            if "mediumText" in classes:
                value = cursor.get_text(" ", strip=True).rstrip(".")
                if value:
                    number_parts.append(value)
        marker_value = marker.get_text(" ", strip=True).rstrip(".")
        if marker_value:
            number_parts.insert(0, marker_value)
        number = "".join(number_parts)
        if marker_value and re.fullmatch(r"\d+(?:\.\d+)*", marker_value):
            current_parent = marker_value
        elif not marker_value and number and current_parent:
            number = current_parent + number
        if not number or content is None:
            continue
        if number in seen_numbers:
            continue
        seen_numbers.add(number)
        text = " ".join(content.get_text(" ", strip=True).split())
        if not text:
            continue
        strong = content.find(["strong", "u"])
        title = " ".join((strong.get_text(" ", strip=True) if strong else text).split())[:500]
        item_id = f"{meeting['body_code']}-{meeting['meeting_id']}_{number}"
        items.append(
            {
                "agenda_item_id": item_id,
                "meeting_id": meeting["meeting_id"],
                "agenda_item_number": number,
                "agenda_item_title": title,
                "agenda_item_text": text,
                "agenda_item_url": meeting["agenda_url"],
                "source_body": meeting["body_code"],
                "source_url": meeting["agenda_url"],
                "sort_order": len(items) + 1,
            }
        )
        for link in content.select("a[href]"):
            url = urllib.parse.urljoin(meeting["agenda_url"], link.get("href", ""))
            path = urllib.parse.urlsplit(url).path.lower()
            if (
                not path.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"))
                or url in seen_docs
            ):
                continue
            seen_docs.add(url)
            docs.append(
                {
                    "agenda_item_id": item_id,
                    "agenda_item_number": number,
                    "document_title": link.get_text(" ", strip=True) or path.rsplit("/", 1)[-1],
                    "document_url": url,
                    "document_type": "Attachment",
                }
            )
    return items, docs


def sync(args) -> int:
    from db import (
        Meeting,
        PublicBody,
        get_session,
        init_db,
        replace_meeting_data_safe,
        update_sync_status,
    )
    from sqlalchemy import select

    init_db()
    year = int(
        getattr(args, "year", None)
        or (getattr(args, "start_date", "") or str(date.today().year))[:4]
    )
    meetings, seen = [], set()
    for month in range(1, 13):
        for meeting in parse_listing(_get(f"{BASE}default.cfm?mt=ALL&month={month}&year={year}")):
            if meeting["meeting_id"] not in seen:
                seen.add(meeting["meeting_id"])
                meetings.append(meeting)
    start, end = getattr(args, "start_date", None), getattr(args, "end_date", None)
    if start:
        meetings = [meeting for meeting in meetings if meeting["meeting_date"] >= start]
    if end:
        meetings = [meeting for meeting in meetings if meeting["meeting_date"] <= end]
    if getattr(args, "meeting_id", None):
        meetings = [meeting for meeting in meetings if meeting["meeting_id"] == args.meeting_id]
    meetings.sort(
        key=lambda meeting: (meeting["meeting_date"], meeting["meeting_id"]), reverse=True
    )
    if getattr(args, "limit", None):
        meetings = meetings[: args.limit]
    print(f"Found {len(meetings)} Flagstaff meeting(s)")
    session = get_session()
    try:
        for index, meeting in enumerate(meetings, 1):
            body, mid = meeting["body_code"], meeting["meeting_id"]
            existing = session.execute(
                select(Meeting).where(Meeting.body == body, Meeting.meeting_id == mid)
            ).scalar_one_or_none()
            if (
                existing
                and existing.sync_status == "complete"
                and existing.item_count_actual
                and not getattr(args, "force", False)
            ):
                print(f"  [{index}/{len(meetings)}] {mid}: already synced")
                continue
            items, docs = parse_agenda(_get(meeting["agenda_url"]), meeting)
            replace_meeting_data_safe(session, body, mid, meeting, items, supporting_doc_dicts=docs)
            body_id = session.execute(
                select(PublicBody.id).where(PublicBody.body_code == body)
            ).scalar_one_or_none()
            stored = session.execute(
                select(Meeting).where(Meeting.body == body, Meeting.meeting_id == mid)
            ).scalar_one()
            stored.public_body_id = body_id
            update_sync_status(session, body, mid, "complete" if items else "no_agenda")
            session.commit()
            print(
                f"  [{index}/{len(meetings)}] {mid} {meeting['meeting_date']}: {len(items)} items, {len(docs)} documents"
            )
    finally:
        session.close()
    return 0
