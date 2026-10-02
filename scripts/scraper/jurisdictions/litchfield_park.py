"""City of Litchfield Park meeting extraction via CivicClerk.

Portal: https://litchfieldparkaz.portal.civicclerk.com/
API:    https://litchfieldparkaz.api.civicclerk.com/v1
"""

from __future__ import annotations

import logging
import urllib.request
import json

from scraper.platforms.civicclerk import (
    CivicClerkConfig,
    HEADERS,
    fetch_meeting_items,
    search_meetings,
)

log = logging.getLogger(__name__)

BODY_MAP = {
    "City Council": ("litchfield-park-cc", "litchfield-park-cc", "City Council"),
    "Planning and Zoning Commission": (
        "litchfield-park-pz", "litchfield-park-pz", "Planning and Zoning Commission"
    ),
    "Board of Adjustment": (
        "litchfield-park-boa", "litchfield-park-boa", "Board of Adjustment"
    ),
    "Design Review Board": (
        "litchfield-park-drb", "litchfield-park-drb", "Design Review Board"
    ),
    "Community Facilities District": (
        "litchfield-park-cfd", "litchfield-park-cfd", "Community Facilities District"
    ),
    "Recreation and Public Grounds Commission": (
        "litchfield-park-rpgc", "litchfield-park-rpgc",
        "Recreation and Public Grounds Commission",
    ),
    "General": (
        "litchfield-park-general", "litchfield-park-general", "General"
    ),
}

CONFIG = CivicClerkConfig(
    subdomain="litchfieldparkaz",
    body_map=BODY_MAP,
    default_body="litchfield-park-general",
)


def _event_agenda_id(event_id: int) -> int:
    """Resolve a public event ID to the structured CivicClerk meeting ID."""
    req = urllib.request.Request(f"{CONFIG.api_base}/Events/{event_id}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as response:
        event = json.loads(response.read())
    return int(event.get("agendaId") or 0)


def _refresh_document_urls(session, body_code: str, meeting_id: str,
                           documents: list[dict]) -> None:
    """Refresh expiring CivicClerk URLs without creating duplicate documents.

    CivicClerk report URLs contain rotating SAS signatures.  Their durable
    identity is the parent item + title + type, not the signed URL.
    """
    from db import SupportingDocument
    from sqlalchemy import select

    existing = session.execute(
        select(SupportingDocument).where(
            SupportingDocument.body == body_code,
            SupportingDocument.meeting_id == meeting_id,
        )
    ).scalars().all()
    by_key = {
        (row.agenda_item_number or "", row.document_title or "",
         row.document_type or ""): row
        for row in existing
    }
    for document in documents:
        key = (
            document.get("agenda_item_number") or "",
            document.get("document_title") or "",
            document.get("document_type") or "",
        )
        row = by_key.get(key)
        if row is not None and document.get("document_url"):
            row.document_url = document["document_url"]
    session.flush()


def sync(args) -> int:
    """Search, extract, and persist Litchfield Park meetings."""
    from db import (
        Meeting as MeetingModel,
        Jurisdiction,
        PublicBody,
        get_session,
        init_db,
        replace_meeting_data_safe,
        update_sync_status,
    )
    from sqlalchemy import select

    init_db()
    start_date = getattr(args, "start_date", None) or "2025-01-01"
    requested_bodies = getattr(args, "bodies", None)
    body_slugs = ([value.strip() for value in requested_bodies.split(",") if value.strip()]
                  if requested_bodies else None)

    print("Searching Litchfield Park meetings via CivicClerk API...")
    meetings = search_meetings(CONFIG, start_date=start_date, body_slugs=body_slugs)
    end_date = getattr(args, "end_date", None)
    if end_date:
        meetings = [m for m in meetings if m.get("meeting_date", "") <= end_date]
    meeting_id = getattr(args, "meeting_id", None)
    if meeting_id:
        meetings = [m for m in meetings if str(m.get("event_id")) == str(meeting_id)]
    if getattr(args, "limit", None):
        meetings = meetings[:args.limit]
    if not meetings:
        print("No Litchfield Park meetings found in the requested range.")
        return 0

    print(f"Found {len(meetings)} Litchfield Park meeting(s)")
    session = get_session()
    total_items = 0
    try:
        jurisdiction_id = session.execute(
            select(Jurisdiction.id).where(Jurisdiction.slug == "litchfield-park")
        ).scalar_one()
        for index, meeting in enumerate(meetings, 1):
            event_id = int(meeting["event_id"])
            event_key = str(event_id)
            body_code = meeting["body_code"]
            public_body_id = session.execute(
                select(PublicBody.id).where(PublicBody.body_code == body_code)
            ).scalar_one_or_none()
            existing = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == event_key,
                )
            ).scalar_one_or_none()
            if existing is not None and public_body_id:
                existing.public_body_id = public_body_id
            if (existing and existing.sync_status == "complete"
                    and (existing.item_count_actual or 0) > 0
                    and not getattr(args, "force", False)):
                print(f"  [{index}/{len(meetings)}] {event_key}: already synced "
                      f"({existing.item_count_actual} items)")
                total_items += existing.item_count_actual or 0
                continue

            meeting_dict = {
                "meeting_id": event_key,
                "meeting_date": meeting.get("meeting_date", ""),
                "meeting_type": meeting.get("meeting_type", ""),
                "meeting_title": meeting.get("meeting_title", ""),
                "source_url": meeting.get("source_url", ""),
                "agenda_url": meeting.get("agenda_url") or None,
                "minutes_url": meeting.get("minutes_url") or None,
                "jurisdiction_id": jurisdiction_id,
                "public_body_id": public_body_id,
            }
            try:
                structured_id = _event_agenda_id(event_id)
                items, documents = (fetch_meeting_items(
                    CONFIG, event_id, structured_id, body_code,
                    meeting.get("meeting_date", ""),
                ) if structured_id else ([], []))
                _refresh_document_urls(
                    session, body_code, event_key, documents
                )
                replace_meeting_data_safe(
                    session, body_code, event_key, meeting_dict, items,
                    supporting_doc_dicts=documents,
                )
                status = "complete" if items else "no_agenda"
                update_sync_status(session, body_code, event_key, status)
                session.commit()
                total_items += len(items)
                print(f"  [{index}/{len(meetings)}] {event_key} "
                      f"{meeting.get('meeting_date', '')}: {len(items)} items, "
                      f"{len(documents)} documents ({status})")
            except Exception as exc:
                session.rollback()
                log.exception("Failed Litchfield Park meeting %s", event_key)
                try:
                    update_sync_status(
                        session, body_code, event_key, "failed", error=str(exc)[:500]
                    )
                    session.commit()
                except Exception:
                    session.rollback()
    finally:
        session.close()

    print(f"Synced {total_items} Litchfield Park items across {len(meetings)} meeting(s)")
    return 0
