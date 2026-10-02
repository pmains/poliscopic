"""
Town of Fountain Hills meeting extraction via CivicClerk API.

Fountain Hills uses CivicClerk (same platform as Surprise).
API: https://fountainhillsaz.api.civicclerk.com/v1
Portal: https://fountainhillsaz.portal.civicclerk.com
"""

from __future__ import annotations
import json
import logging
import urllib.request
from typing import Optional

from scraper.platforms.civicclerk import (
    CivicClerkConfig,
    fetch_meeting_items,
    search_meetings as search_civicclerk_meetings,
)

log = logging.getLogger(__name__)

SOURCE_SYSTEM = "civicclerk"
SUBDOMAIN = "fountainhillsaz"
API_BASE = f"https://{SUBDOMAIN}.api.civicclerk.com/v1"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# body mapping: category name from eventCategoryName → (slug, body_code, display_name)
BODY_MAP: dict[str, tuple[str, str, str]] = {
    "Town Council": ("fountain-hills-cc", "fountain-hills-cc", "Town Council"),
    "Planning and Zoning Commission": ("fountain-hills-pz", "fountain-hills-pz", "Planning & Zoning Commission"),
    "Board of Adjustment": ("fountain-hills-boa", "fountain-hills-boa", "Board of Adjustment"),
    "Strategic Planning Advisory Commission": ("fountain-hills-spac", "fountain-hills-spac", "Strategic Planning Advisory Commission"),
    "Community Services Advisory Commission": ("fountain-hills-csac", "fountain-hills-csac", "Community Services Advisory Commission"),
    "History and Culture Advisory Commission": ("fountain-hills-hcac", "fountain-hills-hcac", "History and Culture Advisory Commission"),
    "Municipal Property Corporation": ("fountain-hills-mpc", "fountain-hills-mpc", "Municipal Property Corporation"),
    "Sub-Committee": ("fountain-hills-sub", "fountain-hills-sub", "Sub-Committee"),
}

DEFAULT_BODY_SLUGS = ["fountain-hills-cc", "fountain-hills-pz"]

CONFIG = CivicClerkConfig(
    subdomain=SUBDOMAIN,
    body_map=BODY_MAP,
    default_body="fountain-hills-cc",
)


def fetch_all_events() -> list[dict]:
    """Fetch all events via OData pagination."""
    all_events: list[dict] = []
    next_url: Optional[str] = f"{API_BASE}/Events"
    while next_url:
        req = urllib.request.Request(next_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            log.warning("Failed to fetch events: %s", e)
            break
        all_events.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")
    return all_events


def _resolve_body(category_name: str) -> tuple[str, str, str]:
    return BODY_MAP.get(category_name, ("fountain-hills-cc", "fountain-hills-cc", category_name))


def search_meetings() -> list[dict]:
    """Fetch all meetings from the CivicClerk API."""
    events = fetch_all_events()
    meetings: list[dict] = []
    for evt in events:
        event_id = str(evt.get("id", ""))
        if not event_id:
            continue
        event_name = evt.get("eventName", "")
        event_date = (evt.get("eventDate", "") or "")[:10]
        cat_name = evt.get("categoryName", evt.get("eventCategoryName", ""))
        slug, code, display = _resolve_body(cat_name)

        # Determine meeting type
        name_lower = event_name.lower()
        mtype = "Regular Meeting"
        if "executive session" in name_lower:
            mtype = "Executive Session"
        elif "special" in name_lower:
            mtype = "Special"
        elif "work session" in name_lower or "study session" in name_lower:
            mtype = "Work Study"
        elif "canceled" in name_lower or "cancelled" in name_lower:
            mtype = "Cancelled"
        elif "regular" in name_lower:
            mtype = "Regular"

        # Extract document URLs from published files
        agenda_url = ""
        packet_url = ""
        minutes_url = ""
        for pf in evt.get("publishedFiles", []):
            ftype = pf.get("type", "")
            fname = pf.get("name", "")
            path = pf.get("url", "")
            doc_url = f"https://{SUBDOMAIN}.api.civicclerk.com/v1/{path}" if path and not path.startswith("http") else path
            if ftype == "Agenda" or ftype == "Event":
                agenda_url = doc_url
            elif ftype == "Agenda Packet" or "Packet" in fname:
                packet_url = doc_url
            elif ftype == "Minutes":
                minutes_url = doc_url

        # Also try the legacy agendaFile
        if not agenda_url:
            ag_file = evt.get("agendaFile", {})
            if ag_file and ag_file.get("fileName"):
                agenda_url = f"{API_BASE}/stream/{SUBDOMAIN}/{ag_file['fileName']}"

        meetings.append({
            "meeting_id": event_id,
            "meeting_date": event_date,
            "meeting_title": event_name,
            "meeting_type": mtype,
            "body_code": code,
            "body_slug": slug,
            "category_name": cat_name,
            "agenda_url": agenda_url,
            "packet_url": packet_url,
            "minutes_url": minutes_url,
            "source_url": f"https://{SUBDOMAIN}.portal.civicclerk.com/",
            "source_system": SOURCE_SYSTEM,
        })
    return meetings


def extract_supporting_docs(evt: dict) -> list[dict]:
    """Extract document URLs from the published files."""
    docs: list[dict] = []
    for pf in evt.get("publishedFiles", []):
        ftype = pf.get("type", "")
        fname = pf.get("name", "")
        path = pf.get("url", "")
        doc_url = f"https://{SUBDOMAIN}.api.civicclerk.com/v1/{path}" if path and not path.startswith("http") else path
        if doc_url:
            docs.append({
                "document_title": fname or ftype or "Document",
                "document_url": doc_url,
                "document_type": ftype or "Meeting Document",
            })
    return docs


def sync(args) -> int:
    """Search, extract, and persist Fountain Hills meetings."""
    from sqlalchemy import select

    from db import (
        Meeting,
        get_session,
        init_db,
        replace_meeting_data_safe,
        update_sync_status,
    )

    init_db()
    print("Searching Fountain Hills meetings via CivicClerk API...")
    meetings = search_civicclerk_meetings(CONFIG, start_date="2025-08-01")
    if not meetings:
        print("No Fountain Hills meetings found.")
        return 0

    start_date = getattr(args, "start_date", None)
    end_date = getattr(args, "end_date", None)
    if start_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") >= start_date]
    if end_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") <= end_date]
    if not meetings:
        print("No Fountain Hills meetings found in date range.")
        return 0
    if getattr(args, "limit", None):
        meetings = meetings[: args.limit]
    print(f"Found {len(meetings)} Fountain Hills meeting(s)")

    session = get_session()
    total_items = 0
    try:
        for index, meeting in enumerate(meetings, 1):
            event_id = meeting.get("event_id") or int(meeting.get("meeting_id", 0))
            event_key = str(event_id)
            meeting_date = meeting.get("meeting_date", "")
            body_code = meeting.get("body_code", "fountain-hills-cc")
            meeting_dict = {
                "meeting_id": event_key,
                "meeting_date": meeting_date,
                "meeting_type": meeting.get("meeting_type", ""),
                "meeting_title": meeting.get("meeting_title", ""),
                "source_url": meeting.get("source_url", ""),
            }

            existing = session.execute(
                select(Meeting).where(
                    Meeting.body == body_code,
                    Meeting.meeting_id == event_key,
                )
            ).scalar_one_or_none()
            if (
                existing
                and existing.sync_status == "complete"
                and (existing.item_count_actual or 0) > 0
                and not getattr(args, "force", False)
            ):
                print(
                    f"  [{index}/{len(meetings)}] {event_id} {meeting_date}: "
                    f"already synced, {existing.item_count_actual or 0} items"
                )
                total_items += existing.item_count_actual or 0
                continue

            try:
                items: list[dict] = []
                documents: list[dict] = []
                if event_id:
                    request = urllib.request.Request(
                        f"{CONFIG.api_base}/Events/{event_id}",
                        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=10) as response:
                            event = json.loads(response.read())
                        agenda_id = int(event.get("agendaId") or 0)
                        if agenda_id > 0:
                            items, documents = fetch_meeting_items(
                                CONFIG,
                                int(event_id),
                                agenda_id,
                                body_code,
                                meeting_date,
                            )
                    except Exception:
                        log.debug(
                            "Structured agenda unavailable for Fountain Hills %s",
                            event_id,
                            exc_info=True,
                        )

                replace_meeting_data_safe(
                    session,
                    body_code,
                    event_key,
                    meeting_dict,
                    items,
                    supporting_doc_dicts=documents,
                )
                total_items += len(items)
                document_summary = f" ({len(documents)} doc(s))" if documents else ""
                print(
                    f"  [{index}/{len(meetings)}] {event_id} {meeting_date}: "
                    f"{len(items)} items synced{document_summary}"
                )
            except Exception as exc:
                log.exception("Failed Fountain Hills meeting %s", event_id)
                try:
                    update_sync_status(
                        session,
                        body_code,
                        event_key,
                        "failed",
                        error=str(exc)[:500],
                    )
                    session.commit()
                except Exception:
                    session.rollback()
    finally:
        session.close()

    print(
        f"Synced {total_items} Fountain Hills items across "
        f"{len(meetings)} meeting(s)"
    )
    return 0
