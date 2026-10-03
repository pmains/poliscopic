"""City of Tolleson meeting extraction via CivicClerk."""

from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
import urllib.request

from scraper.platforms.civicclerk import (
    CivicClerkConfig,
    fetch_meeting_items,
    search_meetings,
)


log = logging.getLogger(__name__)

CONFIG = CivicClerkConfig(
    subdomain="tollesonaz",
    body_map={
        "City Council": ("tolleson-cc", "tolleson-cc", "City Council"),
        "Planning and Zoning Commission": (
            "tolleson-pz",
            "tolleson-pz",
            "Planning and Zoning Commission",
        ),
        "Fire Public Safety Personnel Retirement Board": (
            "tolleson-psprs-fire",
            "tolleson-psprs-fire",
            "Fire PSPRS Board",
        ),
        "Police Public Safety Personnel Retirement Board": (
            "tolleson-psprs-police",
            "tolleson-psprs-police",
            "Police PSPRS Board",
        ),
    },
    default_body="tolleson-cc",
)


def _agenda_id(event_id: int | str) -> int:
    """Load an event's agenda id, retaining the legacy empty-on-error behavior."""
    request = urllib.request.Request(
        f"{CONFIG.api_base}/Events/{event_id}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(json.loads(response.read()).get("agendaId", 0) or 0)
    except Exception:
        return 0


def sync(args) -> int:
    """Discover and persist Tolleson meetings for the requested date range."""
    from db import Meeting, get_session, init_db, replace_meeting_data_safe
    from db import update_sync_status
    from sqlalchemy import select

    init_db()
    year_value = getattr(args, "year", None)
    year = int(year_value) if year_value else dt.date.today().year
    start_date = getattr(args, "start_date", None) or f"{year - 1}-01-01"
    end_date = getattr(args, "end_date", None) or f"{year}-12-31"

    print(f"Searching Tolleson CivicClerk meetings from {start_date} to {end_date}...")
    meetings = search_meetings(CONFIG, start_date=start_date)
    meetings = [
        meeting
        for meeting in meetings
        if start_date <= meeting.get("meeting_date", "") <= end_date
    ]
    if not meetings:
        print("No Tolleson meetings found in date range.")
        return 0
    if getattr(args, "limit", 0):
        meetings = meetings[: args.limit]
    print(f"Found {len(meetings)} Tolleson meeting(s)")

    session = get_session()
    total_items = 0
    meeting_count = len(meetings)
    try:
        for index, meeting in enumerate(meetings, 1):
            event_id = meeting.get("event_id") or int(meeting.get("meeting_id", 0))
            meeting_id = str(event_id)
            meeting_date = meeting.get("meeting_date", "")
            body_code = meeting.get("body_code", "tolleson-cc")
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting.get("meeting_type", ""),
                "meeting_title": meeting.get("meeting_title", ""),
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
                count = existing.item_count_actual or 0
                print(
                    f"  [{index}/{meeting_count}] {event_id} {meeting_date}: "
                    f"already synced, {count} items"
                )
                total_items += count
                continue

            try:
                items: list[dict] = []
                supporting_documents: list[dict] = []
                agenda_id = _agenda_id(event_id)
                if agenda_id > 0:
                    items, supporting_documents = fetch_meeting_items(
                        CONFIG,
                        event_id,
                        agenda_id,
                        body_code,
                        meeting_date,
                    )
                replace_meeting_data_safe(
                    session,
                    body_code,
                    meeting_id,
                    meeting_dict,
                    items,
                    supporting_doc_dicts=supporting_documents,
                )
                total_items += len(items)
                document_summary = (
                    f" ({len(supporting_documents)} doc(s))"
                    if supporting_documents
                    else ""
                )
                timestamp = dt.datetime.now().strftime("%H:%M:%S")
                print(
                    f"{timestamp} [{index}/{meeting_count}] {event_id} {meeting_date}: "
                    f"{len(items)} items synced{document_summary}"
                )
            except Exception as error:
                log.error("Failed to sync Tolleson meeting %s: %s", event_id, error)
                traceback.print_exc()
                try:
                    update_sync_status(
                        session,
                        body_code,
                        meeting_id,
                        "failed",
                        error=str(error)[:500],
                    )
                    session.commit()
                except Exception:
                    pass
    finally:
        session.close()

    print(f"Synced {total_items} Tolleson items across {meeting_count} meeting(s)")
    return 0
