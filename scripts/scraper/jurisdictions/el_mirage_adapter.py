"""City of El Mirage persistence adapter for Destiny/AgendaQuick."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from scraper.jurisdictions.el_mirage import (
    DEFAULT_BODY_SLUGS,
    fetch_page,
    parse_agenda_items,
    search_el_mirage_meetings,
)
from scraper.platforms.destiny_common import fetch_agenda_memo_docs


log = logging.getLogger(__name__)


def sync(args) -> int:
    """Discover and persist El Mirage meetings for a year or month."""
    from db import Meeting, get_session, init_db, replace_meeting_data_safe

    init_db()
    body_value = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
    body_slugs = [slug.strip() for slug in body_value.split(",") if slug.strip()]
    month_value = getattr(args, "month", None)
    year_value = getattr(args, "year", None)
    if month_value:
        year = int(month_value.split("-")[0])
    elif year_value:
        year = int(year_value)
    else:
        year = dt.date.today().year

    print(f"Searching El Mirage meetings for {year}...")
    meetings = search_el_mirage_meetings(year, body_slugs=body_slugs)
    if getattr(args, "limit", 0):
        meetings = meetings[: args.limit]
    if month_value:
        meetings = [
            meeting
            for meeting in meetings
            if meeting.get("meeting_date", "").startswith(month_value)
        ]
        print(f"Filtered to {len(meetings)} meeting(s) in {month_value}")
    if not meetings:
        print(f"No El Mirage meetings found for {year}.")
        return 0

    print(f"Found {len(meetings)} El Mirage meeting(s)")
    session = get_session()
    total_items = 0
    meeting_count = len(meetings)
    try:
        for index, meeting in enumerate(meetings, 1):
            meeting_id = meeting["meeting_id"]
            meeting_date = meeting["meeting_date"]
            body_code = meeting.get("body_code", "el-mirage-cc")
            agenda_url = meeting.get("agenda_url", "")
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting.get("meeting_type", ""),
                "meeting_title": meeting.get("body_name", ""),
                "source_url": agenda_url,
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
                    f"  [{index}/{meeting_count}] {meeting_id} {meeting_date}: "
                    "already synced"
                )
                continue

            try:
                items = parse_agenda_items(fetch_page(agenda_url), meeting_id)
                if not items:
                    replace_meeting_data_safe(
                        session, body_code, meeting_id, meeting_dict, []
                    )
                    print(
                        f"  [{index}/{meeting_count}] {meeting_id} {meeting_date}: "
                        "no items"
                    )
                    continue

                supporting_documents: list[dict] = []
                seen_memo_urls: set[str] = set()
                for item in items:
                    memo_url = item.get("agenda_item_url", "") or item.get(
                        "source_url", ""
                    )
                    if not memo_url or memo_url in seen_memo_urls:
                        continue
                    seen_memo_urls.add(memo_url)
                    try:
                        for document in fetch_agenda_memo_docs(memo_url, timeout=15):
                            document["agenda_item_id"] = "0"
                            document["agenda_item_number"] = item.get(
                                "agenda_item_number", ""
                            )
                            supporting_documents.append(document)
                    except Exception as error:
                        log.debug(
                            "Memo docs failed for %s item %s: %s",
                            meeting_id,
                            item.get("agenda_item_number", ""),
                            error,
                        )

                agenda_items = []
                seen_item_ids: set[str] = set()
                for item in items:
                    number = item.get("agenda_item_number", "")
                    item_id = f"{body_code}-{meeting_id}_{number}"
                    if item_id in seen_item_ids:
                        continue
                    seen_item_ids.add(item_id)
                    agenda_items.append(
                        {
                            "agenda_item_id": item_id,
                            "meeting_id": meeting_id,
                            "agenda_item_number": number,
                            "agenda_item_title": item.get("agenda_item_title", ""),
                            "agenda_item_text": item.get("agenda_item_text", ""),
                            "agenda_item_url": item.get("agenda_item_url", "")
                            or item.get("source_url", ""),
                            "source_body": body_code,
                            "source_url": agenda_url,
                            "sort_order": item.get("sort_order", 0),
                        }
                    )
                replace_meeting_data_safe(
                    session,
                    body_code,
                    meeting_id,
                    meeting_dict,
                    agenda_items,
                    supporting_doc_dicts=supporting_documents,
                )
                total_items += len(agenda_items)
                document_summary = (
                    f" ({len(supporting_documents)} doc(s))"
                    if supporting_documents
                    else ""
                )
                print(
                    f"  [{index}/{meeting_count}] {meeting_id} {meeting_date}: "
                    f"{len(agenda_items)} item(s){document_summary}"
                )
            except Exception as error:
                log.error("Failed El Mirage meeting %s: %s", meeting_id, error)
    finally:
        session.close()

    print(f"Synced {total_items} El Mirage items across {meeting_count} meeting(s)")
    return 0
