"""City of Yuma council meetings through the official Legistar Web API."""

from __future__ import annotations

import json
import urllib.request
from datetime import date

API = "https://webapi.legistar.com/v1/yuma-az"
PORTAL = "https://yuma-az.legistar.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _json(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.loads(response.read())


def parse_events(events: list[dict], year: int) -> list[dict]:
    meetings = []
    for event in events:
        event_date = (event.get("EventDate") or "")[:10]
        if not event_date.startswith(f"{year:04d}-"):
            continue
        body_name = event.get("EventBodyName") or "City Council"
        if body_name not in {
            "City Council Meeting",
            "City Council Worksession",
            "City Council Citizen's Forum",
        }:
            continue
        event_id = str(event["EventId"])
        comment = (event.get("EventComment") or "").strip()
        meeting_type = "Work Session" if "worksession" in body_name.lower() else "Regular Meeting"
        if "special" in comment.lower():
            meeting_type = "Special Meeting"
        meetings.append(
            {
                "meeting_id": event_id,
                "meeting_date": event_date,
                "meeting_title": comment or body_name,
                "meeting_type": meeting_type,
                "body_code": "yuma-cc",
                "source_url": event.get("EventInSiteURL")
                or f"{PORTAL}/MeetingDetail.aspx?LEGID={event_id}",
                "agenda_url": event.get("EventAgendaFile") or "",
                "minutes_url": event.get("EventMinutesFile") or "",
                "event_guid": event.get("EventGuid") or "",
            }
        )
    return sorted(
        meetings, key=lambda meeting: (meeting["meeting_date"], meeting["meeting_id"]), reverse=True
    )


def parse_items(rows: list[dict], meeting: dict) -> tuple[list[dict], list[dict]]:
    items, docs = [], []
    for row in rows:
        matter_id = row.get("EventItemMatterId")
        if not matter_id:
            continue
        number = (
            row.get("EventItemAgendaNumber")
            or row.get("EventItemMatterFile")
            or str(row.get("EventItemAgendaSequence") or len(items) + 1)
        )
        title = (row.get("EventItemTitle") or row.get("EventItemMatterName") or "").strip()
        item_id = f"yuma-cc-{meeting['meeting_id']}_{matter_id}"
        details = [row.get("EventItemAgendaNote"), row.get("EventItemActionText")]
        items.append(
            {
                "agenda_item_id": item_id,
                "meeting_id": meeting["meeting_id"],
                "agenda_item_number": str(number),
                "agenda_item_title": title,
                "agenda_item_text": "\n".join(
                    value.strip() for value in details if value and value.strip()
                ),
                "agenda_item_url": f"{PORTAL}/LegislationDetail.aspx?ID={matter_id}",
                "source_body": "yuma-cc",
                "source_url": meeting["source_url"],
                "sort_order": len(items) + 1,
            }
        )
        for attachment in row.get("EventItemMatterAttachments") or []:
            url = (
                attachment.get("MatterAttachmentHyperlink")
                or attachment.get("MatterAttachmentFileName")
                or ""
            )
            if not url:
                continue
            docs.append(
                {
                    "agenda_item_id": item_id,
                    "agenda_item_number": str(number),
                    "document_title": attachment.get("MatterAttachmentName") or "Attachment",
                    "document_url": url,
                    "document_type": "Attachment",
                }
            )
    if not items:
        skip_phrases = ("notice is hereby given", "page break", "americans with disabilities")
        for row in sorted(rows, key=lambda value: value.get("EventItemAgendaSequence") or 9999):
            sequence = row.get("EventItemAgendaSequence")
            title = (row.get("EventItemTitle") or "").strip()
            if not sequence or not title or title.lower().startswith(skip_phrases):
                continue
            number = str(sequence)
            items.append(
                {
                    "agenda_item_id": f"yuma-cc-{meeting['meeting_id']}_seq-{number}",
                    "meeting_id": meeting["meeting_id"],
                    "agenda_item_number": number,
                    "agenda_item_title": title[:500],
                    "agenda_item_text": title,
                    "agenda_item_url": meeting["source_url"],
                    "source_body": "yuma-cc",
                    "source_url": meeting["source_url"],
                    "sort_order": len(items) + 1,
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
    meetings = parse_events(_json(f"{API}/events"), year)
    start, end = getattr(args, "start_date", None), getattr(args, "end_date", None)
    if start:
        meetings = [meeting for meeting in meetings if meeting["meeting_date"] >= start]
    if end:
        meetings = [meeting for meeting in meetings if meeting["meeting_date"] <= end]
    if getattr(args, "meeting_id", None):
        meetings = [meeting for meeting in meetings if meeting["meeting_id"] == args.meeting_id]
    if getattr(args, "limit", None):
        meetings = meetings[: args.limit]
    print(f"Found {len(meetings)} Yuma meeting(s)")
    session = get_session()
    try:
        body_id = session.execute(
            select(PublicBody.id).where(PublicBody.body_code == "yuma-cc")
        ).scalar_one_or_none()
        for index, meeting in enumerate(meetings, 1):
            mid = meeting["meeting_id"]
            existing = session.execute(
                select(Meeting).where(Meeting.body == "yuma-cc", Meeting.meeting_id == mid)
            ).scalar_one_or_none()
            if (
                existing
                and existing.sync_status == "complete"
                and existing.item_count_actual
                and not getattr(args, "force", False)
            ):
                print(f"  [{index}/{len(meetings)}] {mid}: already synced")
                continue
            items, docs = parse_items(_json(f"{API}/events/{mid}/EventItems"), meeting)
            for item in items:
                matter_id = item["agenda_item_id"].rsplit("_", 1)[-1]
                if matter_id.startswith("seq-"):
                    continue
                try:
                    attachments = _json(f"{API}/matters/{matter_id}/attachments")
                except Exception:
                    attachments = []
                for attachment in attachments:
                    url = (
                        attachment.get("MatterAttachmentHyperlink")
                        or attachment.get("MatterAttachmentFileName")
                        or ""
                    )
                    if url:
                        docs.append(
                            {
                                "agenda_item_id": item["agenda_item_id"],
                                "agenda_item_number": item["agenda_item_number"],
                                "document_title": attachment.get("MatterAttachmentName")
                                or "Attachment",
                                "document_url": url,
                                "document_type": "Attachment",
                            }
                        )
            for kind, url in (
                ("Agenda", meeting["agenda_url"]),
                ("Minutes", meeting["minutes_url"]),
            ):
                if url:
                    docs.append(
                        {
                            "agenda_item_id": "0",
                            "agenda_item_number": "",
                            "document_title": kind,
                            "document_url": url,
                            "document_type": kind,
                        }
                    )
            replace_meeting_data_safe(
                session, "yuma-cc", mid, meeting, items, supporting_doc_dicts=docs
            )
            stored = session.execute(
                select(Meeting).where(Meeting.body == "yuma-cc", Meeting.meeting_id == mid)
            ).scalar_one()
            stored.public_body_id = body_id
            update_sync_status(session, "yuma-cc", mid, "complete" if items else "no_agenda")
            session.commit()
            print(
                f"  [{index}/{len(meetings)}] {mid} {meeting['meeting_date']}: {len(items)} items, {len(docs)} documents"
            )
    finally:
        session.close()
    return 0
