"""
Town of Paradise Valley meeting extraction via Granicus RSS.

Granicus RSS: https://paradisevalleyaz.granicus.com/ViewPublisherRSS.php?view_id=2&mode=agendas
"""

from __future__ import annotations
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional

import urllib.request

log = logging.getLogger(__name__)

BASE_URL = "https://paradisevalleyaz.granicus.com"
VIEW_ID = 2
SOURCE_SYSTEM = "granicus"

RSS_URL = f"{BASE_URL}/ViewPublisherRSS.php?view_id={VIEW_ID}&mode=agendas"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

BODY_MAP: dict[str, tuple[str, str, str]] = {
    "Town Council": ("paradise-valley-cc", "paradise-valley-cc", "Town Council"),
    "Planning Commission": ("paradise-valley-pc", "paradise-valley-pc", "Planning Commission"),
    "Board of Adjustment": ("paradise-valley-boa", "paradise-valley-boa", "Board of Adjustment"),
}

DEFAULT_BODY_SLUGS = ["paradise-valley-cc", "paradise-valley-pc"]


def _resolve_body(title: str) -> tuple[str, str, str]:
    for key, (slug, code, display) in BODY_MAP.items():
        if key.lower() in title.lower():
            return slug, code, display
    return "paradise-valley-cc", "paradise-valley-cc", title


def fetch_rss() -> Optional[str]:
    try:
        req = urllib.request.Request(RSS_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch Paradise Valley RSS: %s", e)
        return None


def search_meetings() -> list[dict]:
    """Search Paradise Valley meetings via Granicus RSS feed."""
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
        desc_el = item.find("description")
        if title_el is None:
            continue

        title = title_el.text or ""
        desc = desc_el.text or "" if desc_el is not None else ""

        # Extract clip_id or event_id from description (CDATA contains full URL)
        clip_match = re.search(r"clip_id=(\d+)", desc)
        event_match = re.search(r"event_id=(\d+)", desc)
        meeting_id_str = clip_match.group(1) if clip_match else (event_match.group(1) if event_match else None)
        if not meeting_id_str:
            continue
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        meeting_date = date_match.group(1) if date_match else ""

        body_name = title.split(" on ")[0].split(" - ")[0].strip() if " on " in title else "Town Council"
        slug, code, display = _resolve_body(body_name)
        if clip_match:
            agenda_url = f"{BASE_URL}/AgendaViewer.php?view_id={VIEW_ID}&clip_id={meeting_id_str}"
        else:
            agenda_url = f"{BASE_URL}/AgendaViewer.php?view_id={VIEW_ID}&event_id={meeting_id_str}"

        meetings.append({
            "meeting_id": meeting_id_str,
            "meeting_date": meeting_date,
            "meeting_type": display,
            "meeting_title": title.split(" - ")[0].strip() if " - " in title else body_name,
            "body_slug": slug,
            "body_code": code,
            "body_name": body_name,
            "agenda_url": agenda_url,
            "source_url": agenda_url,
            "source_system": SOURCE_SYSTEM,
        })

    return meetings


def sync(args) -> int:
    """Search and persist Paradise Valley meeting metadata."""
    from sqlalchemy import select

    from db import Meeting, get_session, init_db, replace_meeting_data_safe, update_sync_status

    init_db()
    print("Searching Paradise Valley meetings via Granicus RSS...")
    meetings = search_meetings()
    if not meetings:
        print("No Paradise Valley meetings found.")
        return 0

    start_date = getattr(args, "start_date", None)
    end_date = getattr(args, "end_date", None)
    if start_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") >= start_date]
    if end_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") <= end_date]
    if not meetings:
        print("No Paradise Valley meetings found in date range.")
        return 0
    if getattr(args, "limit", None):
        meetings = meetings[: args.limit]
    print(f"Found {len(meetings)} Paradise Valley meeting(s)")

    session = get_session()
    try:
        for index, meeting in enumerate(meetings, 1):
            meeting_id = meeting["meeting_id"]
            meeting_date = meeting.get("meeting_date", "")
            body_code = meeting.get("body_code", "paradise-valley-cc")
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
                    "already synced"
                )
                continue

            try:
                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict, []
                )
                print(
                    f"  [{index}/{len(meetings)}] {meeting_id} {meeting_date}: "
                    "meeting metadata synced"
                )
                update_sync_status(session, body_code, meeting_id, "no_agenda")
                session.commit()
            except Exception as exc:
                session.rollback()
                log.exception("Failed to sync Paradise Valley meeting %s", meeting_id)
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

    print(f"Synced {len(meetings)} Paradise Valley meeting(s)")
    return 0
