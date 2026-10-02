"""
City of Apache Junction meeting extraction via Legistar (Granicus).

Legistar URL: https://apachejunction.legistar.com
Calendar: Calendar.aspx
Meeting Detail: MeetingDetail.aspx?ID=...
Agenda PDF: View.ashx?M=A&ID=...
Legislation detail: LegislationDetail.aspx?ID=...
"""

from __future__ import annotations

from datetime import datetime
import logging
import re
import urllib.parse

from scraper.common.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.common.io_utils import normalize_meeting_date

log = logging.getLogger(__name__)

SOURCE_SYSTEM = "legistar"
BASE_URL = "https://apachejunction.legistar.com"
CALENDAR_URL = f"{BASE_URL}/Calendar.aspx"

BODY_SLUG_MAP: dict[str, str] = {
    "city council meeting": "aj-city-council",
    "city council work session": "aj-city-council",
    "city council": "aj-city-council",
    "special meeting of the apache junction city council": "aj-city-council",
    "planning and zoning commission": "aj-planning-zoning",
    "parks & recreation commission": "aj-parks",
    "parks and recreation commission": "aj-parks",
    "public safety personnel retirement board": "aj-psprs",
    "library board": "aj-library",
    "superstition vistas community facilities district no. 1": "aj-svcfd-1",
    "superstition vistas community facilities district no. 2": "aj-svcfd-2",
    "water utilities community facilities district": "aj-wucfd",
}

BODY_CODE_MAP: dict[str, str] = {
    "aj-city-council": "apache-junction-cc",
    "aj-planning-zoning": "apache-junction-pz",
    "aj-parks": "apache-junction-parks",
    "aj-psprs": "apache-junction-psprs",
    "aj-library": "apache-junction-library",
    "aj-svcfd-1": "apache-junction-svcfd1",
    "aj-svcfd-2": "apache-junction-svcfd2",
    "aj-wucfd": "apache-junction-wucfd",
}

DEFAULT_BODY_SLUGS = ["aj-city-council", "aj-planning-zoning", "aj-parks"]

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def fetch_page(url: str, timeout: int = 30) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _attr(node, key: str) -> str:
    return (node.attrs.get(key) or "").strip()


def _text(node) -> str:
    return _clean_html_text(_node_text(node))


def _resolve_body_slug(body_name: str) -> str:
    key = body_name.strip().lower()
    for pattern, slug in BODY_SLUG_MAP.items():
        if pattern in key:
            return slug
    return "aj-city-council"


def parse_meetings_from_html(html: str) -> list[dict]:
    root = _parse_html(html)
    if root is None:
        return []
    meetings: list[dict] = []
    rows = _find_all(root, "tr")
    for row in rows:
        classes = row.attrs.get("class") or ""
        if "rgRow" not in str(classes) and "rgAltRow" not in str(classes):
            continue
        cells = _find_all(row, "td")
        if len(cells) < 3:
            continue
        body_cell = cells[0]
        body_text = _text(body_cell)
        body_slug = _resolve_body_slug(body_text)
        body_code = BODY_CODE_MAP.get(body_slug, "apache-junction-cc")
        meeting_date_raw = _text(cells[1]) if len(cells) > 1 else ""
        meeting_id = ""
        detail_url = ""
        agenda_url = ""
        minutes_url = ""
        for cell in cells:
            for link in _find_all(cell, "a"):
                href = _attr(link, "href") or ""
                full = urllib.parse.urljoin(BASE_URL, href) if href else ""
                if "MeetingDetail.aspx" in href:
                    detail_url = full
                    m = re.search(r"ID=(\d+)", href)
                    if m:
                        meeting_id = m.group(1)
                elif "View.ashx?M=A" in href:
                    agenda_url = full
                elif "View.ashx?M=M" in href:
                    minutes_url = full
        meeting_date = normalize_meeting_date(meeting_date_raw) or ""
        if not meeting_date and meeting_date_raw:
            d = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", meeting_date_raw)
            if d:
                meeting_date = f"{d.group(3)}-{int(d.group(1)):02d}-{int(d.group(2)):02d}"
        if not meeting_id:
            continue
        t = body_text.lower()
        mtype = "Regular Meeting"
        if "work session" in t or "work study" in t:
            mtype = "Work Study"
        elif "special" in t:
            mtype = "Special"
        meetings.append({
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "meeting_type": mtype,
            "meeting_title": body_text,
            "body_slug": body_slug,
            "body_code": body_code,
            "body_name": body_text,
            "detail_url": detail_url,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "source_url": CALENDAR_URL,
            "source_system": SOURCE_SYSTEM,
        })
    return meetings


def parse_agenda_items_from_html(html: str, meeting_id: str) -> list[dict]:
    root = _parse_html(html)
    if root is None:
        return []
    items: list[dict] = []
    rows = _find_all(root, "tr")
    for row in rows:
        c = row.attrs.get("class") or ""
        if "rgRow" not in str(c) and "rgAltRow" not in str(c):
            continue
        cells = _find_all(row, "td")
        if len(cells) < 4:
            continue
        file_cell = cells[0]
        type_cell = cells[2]
        title_cell = cells[3]
        file_number = _text(file_cell)
        item_type = _text(type_cell)
        item_title = _text(title_cell)
        legislation_url = ""
        for link in _find_all(file_cell, "a"):
            href = _attr(link, "href") or ""
            if "LegislationDetail.aspx" in href:
                legislation_url = urllib.parse.urljoin(BASE_URL, href)
                break
        item_number = file_number if file_number and file_number.strip() else str(len(items) + 1)
        items.append({
            "agenda_item_number": item_number,
            "agenda_item_title": item_title,
            "agenda_item_text": f"Type: {item_type}" if item_type else "",
            "agenda_item_url": legislation_url,
            "agenda_item_id": f"aj-{meeting_id}-{item_number.replace('-','_')}",
        })
    return items


def parse_legislation_detail_from_html(html: str) -> list[dict]:
    root = _parse_html(html)
    if root is None:
        return []
    docs: list[dict] = []
    for link in _find_all(root, "a"):
        href = _attr(link, "href") or ""
        if "View.ashx?M=F" in href or "View.ashx?M=PDF" in href:
            full_url = urllib.parse.urljoin(BASE_URL, href)
            title = _text(link) or full_url.split("/")[-1]
            docs.append({"document_title": title, "document_url": full_url, "document_type": "Attachment"})
    return docs


def search_meetings(body_slugs: list[str] | None = None,
                     start_date: str | None = None,
                     end_date: str | None = None) -> list[dict]:
    """Search for Apache Junction meetings via the Legistar Calendar page.

    Parameters
    ----------
    body_slugs : list[str], optional
        Only return meetings whose body slug is in this list.
    start_date : str, optional
        ISO-8601 start date (e.g. "2026-01-01"). Results are post-filtered
        since Legistar's calendar doesn't support server-side date queries.
    end_date : str, optional
        ISO-8601 end date. Requires *start_date*.

    Returns
    -------
    list[dict]
        Meetings with keys: meeting_id, meeting_date, meeting_type, etc.
    """
    html = fetch_page(CALENDAR_URL)
    meetings = parse_meetings_from_html(html)
    if body_slugs:
        meetings = [m for m in meetings if m["body_slug"] in body_slugs]
    if start_date and end_date:
        meetings = [
            m for m in meetings
            if m.get("meeting_date") and start_date <= m["meeting_date"] <= end_date
        ]
    return meetings


def fetch_agenda_items(detail_url: str) -> list[dict]:
    html = fetch_page(detail_url)
    m = re.search(r"ID=(\d+)", detail_url)
    meeting_id = m.group(1) if m else "0"
    return parse_agenda_items_from_html(html, meeting_id)


def fetch_supporting_docs(legislation_url: str) -> list[dict]:
    html = fetch_page(legislation_url)
    return parse_legislation_detail_from_html(html)


def sync(args) -> int:
    """Search, extract, and persist Apache Junction meetings."""
    from sqlalchemy import select

    from db import (
        Meeting,
        get_session,
        init_db,
        replace_meeting_data_safe,
        update_sync_status,
    )

    init_db()
    requested_bodies = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
    body_slugs = [value.strip() for value in requested_bodies.split(",") if value.strip()]

    print("Searching Apache Junction meetings via Legistar...")
    meetings = search_meetings(body_slugs=body_slugs)
    if not meetings:
        print("No Apache Junction meetings found.")
        return 0

    start_date = getattr(args, "start_date", None)
    end_date = getattr(args, "end_date", None)
    if start_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") >= start_date]
    if end_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") <= end_date]
    if not meetings:
        print("No Apache Junction meetings found in date range.")
        return 0
    if getattr(args, "limit", None):
        meetings = meetings[: args.limit]
    print(f"Found {len(meetings)} Apache Junction meeting(s)")

    session = get_session()
    try:
        for index, meeting in enumerate(meetings, 1):
            meeting_id = meeting["meeting_id"]
            meeting_date = meeting.get("meeting_date", "")
            body_code = meeting.get("body_code", "apache-junction-cc")
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

            items = (
                fetch_agenda_items(meeting["detail_url"])
                if meeting.get("detail_url")
                else []
            )
            documents: list[dict] = []
            for item in items:
                item_url = item.get("agenda_item_url")
                if not item_url:
                    continue
                try:
                    item_documents = fetch_supporting_docs(item_url)
                    for document in item_documents:
                        document["agenda_item_number"] = item.get(
                            "agenda_item_number", "0"
                        )
                    documents.extend(item_documents)
                except Exception as exc:
                    log.warning("Failed to fetch docs for %s: %s", item_url, exc)

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
                    f"{datetime.now():%H:%M:%S} [{index}/{len(meetings)}] "
                    f"{meeting_id} {meeting_date}: {len(items)} items, "
                    f"{len(documents)} docs ({status})"
                )
                update_sync_status(session, body_code, meeting_id, status)
                session.commit()
            except Exception as exc:
                session.rollback()
                log.exception("Failed to sync Apache Junction meeting %s", meeting_id)
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

    print(f"Synced {len(meetings)} Apache Junction meeting(s)")
    return 0
