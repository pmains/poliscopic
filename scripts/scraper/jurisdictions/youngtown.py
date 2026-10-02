"""Town of Youngtown agendas and minutes (Revize document center)."""

from __future__ import annotations

import re
import os
import subprocess
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime

from bs4 import BeautifulSoup

LISTING_URL = "https://www.youngtownaz.org/departments/town_clerk/agendas_minutes.php"
ROOT_URL = "https://www.youngtownaz.org/"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/pdf"}


def _stable_url(href: str) -> str:
    url = urllib.parse.urljoin(ROOT_URL, href)
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            urllib.parse.quote(urllib.parse.unquote(parsed.path)),
            "",
            "",
        )
    )


def _body(title: str) -> tuple[str, str]:
    lower = title.lower()
    if "agua fria" in lower or " cfd" in lower or "community facilities district" in lower:
        return "youngtown-afr-cfd", "Agua Fria Ranch Community Facilities District"
    if "psprs" in lower or "retirement system" in lower:
        return "youngtown-psprs", "PSPRS Local Board"
    if "board of adjustment" in lower:
        return "youngtown-boa", "Board of Adjustment"
    return "youngtown-cc", "Town Council"


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    meetings = []
    for table in soup.select("table"):
        cells = table.select("tr > td")
        if not cells:
            continue
        heading = " ".join(cells[0].get_text(" ", strip=True).split())
        match = re.match(r"^(\d{2}/\d{2}/\d{2})\s+(.+)$", heading)
        if not match:
            continue
        date_text, title = match.groups()
        docs = []
        record_id = None
        detail_url = LISTING_URL
        for link in table.select("a[href]"):
            label = " ".join(link.get_text(" ", strip=True).split())
            href = link.get("href", "")
            rid = re.search(r"agenda_detail_T\d+_R(\d+)\.php", href, re.I)
            if rid:
                record_id = rid.group(1)
                detail_url = urllib.parse.urljoin(LISTING_URL, href)
            elif label in {"Agenda", "Packet", "Minutes", "Action Item"}:
                docs.append(
                    {
                        "document_type": label,
                        "document_title": label,
                        "document_url": _stable_url(href),
                    }
                )
        if not record_id:
            # Cancelled rows sometimes omit More; date/title remains deterministic.
            record_id = (
                datetime.strptime(date_text, "%m/%d/%y").strftime("%Y%m%d")
                + "-"
                + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
            )
        body_code, meeting_type = _body(title)
        meetings.append(
            {
                "meeting_id": record_id,
                "meeting_date": datetime.strptime(date_text, "%m/%d/%y").strftime("%Y-%m-%d"),
                "meeting_title": title,
                "meeting_type": meeting_type,
                "body_code": body_code,
                "source_url": detail_url,
                "documents": docs,
                "agenda_url": next(
                    (d["document_url"] for d in docs if d["document_type"] == "Agenda"), ""
                ),
                "minutes_url": next(
                    (d["document_url"] for d in docs if d["document_type"] == "Minutes"), ""
                ),
            }
        )
    return meetings


def _get(url: str) -> bytes:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=HEADERS), timeout=30
    ) as response:
        return response.read()


def extract_agenda_text(pdf_bytes: bytes) -> str:
    """Extract text, OCRing Youngtown's image-only scanned agendas when needed."""
    from scraper.platforms.civicclerk import extract_pdf_text

    text = extract_pdf_text(pdf_bytes) or ""
    if len(text.strip()) >= 100:
        return text
    with tempfile.TemporaryDirectory(prefix="youngtown-ocr-") as tmp:
        pdf_path = os.path.join(tmp, "agenda.pdf")
        image_prefix = os.path.join(tmp, "page")
        with open(pdf_path, "wb") as pdf_file:
            pdf_file.write(pdf_bytes)
        try:
            subprocess.run(
                ["pdftoppm", "-jpeg", "-r", "200", pdf_path, image_prefix],
                check=True,
                capture_output=True,
                timeout=90,
            )
            pages = []
            for image_path in sorted(
                (
                    os.path.join(tmp, name)
                    for name in os.listdir(tmp)
                    if name.startswith("page-") and name.endswith(".jpg")
                )
            ):
                result = subprocess.run(
                    ["tesseract", image_path, "stdout", "--psm", "6"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                pages.append(result.stdout)
            return "\n".join(pages)
        except (FileNotFoundError, subprocess.SubprocessError):
            return text


def parse_youngtown_items(text: str, meeting_id: str) -> list[dict]:
    """Parse numbered sections and their lettered agenda items from OCR text."""
    blocks: list[dict] = []
    current_section = ""
    current = None
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        numbered = re.match(r"^(\d{1,2})\.\s+(.+)$", line)
        lettered = re.match(r"^([A-Z])\.\s+(.+)$", line)
        if numbered:
            current_section = numbered.group(1)
            current = {
                "number": current_section,
                "title": numbered.group(2),
                "text": line,
                "has_children": False,
            }
            blocks.append(current)
        elif lettered and current_section:
            # A lettered entry belongs to the most recent numbered section.
            for block in reversed(blocks):
                if block["number"] == current_section:
                    block["has_children"] = True
                    break
            current = {
                "number": current_section + lettered.group(1),
                "title": lettered.group(2),
                "text": line,
                "has_children": False,
            }
            blocks.append(current)
        elif current is not None:
            current["text"] += "\n" + line

    chosen = []
    seen_numbers = set()
    for block in blocks:
        if block["has_children"] or block["number"] in seen_numbers:
            continue
        seen_numbers.add(block["number"])
        chosen.append(block)
    return [
        {
            "meeting_id": meeting_id,
            "agenda_item_number": block["number"],
            "item_type_category": "item",
            "agenda_item_title": block["title"],
            "agenda_item_text": block["text"],
            "sort_order": index,
        }
        for index, block in enumerate(chosen, 1)
    ]


def _agenda_items(meeting: dict) -> list[dict]:
    if not meeting["agenda_url"] or "cancelled" in meeting["meeting_title"].lower():
        return []
    text = extract_agenda_text(_get(meeting["agenda_url"]))
    items = parse_youngtown_items(text, meeting["meeting_id"])
    for item in items:
        number = item["agenda_item_number"]
        item["agenda_item_id"] = f"{meeting['body_code']}-{meeting['meeting_id']}_{number}"
        item["source_body"] = meeting["body_code"]
        item["source_url"] = meeting["agenda_url"]
    return items


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
    meetings = parse_listing(_get(LISTING_URL).decode("utf-8", "replace"))
    start, end = getattr(args, "start_date", None), getattr(args, "end_date", None)
    if start:
        meetings = [m for m in meetings if m["meeting_date"] >= start]
    if end:
        meetings = [m for m in meetings if m["meeting_date"] <= end]
    if getattr(args, "meeting_id", None):
        meetings = [m for m in meetings if m["meeting_id"] == args.meeting_id]
    if getattr(args, "limit", None):
        meetings = meetings[: args.limit]
    print(f"Found {len(meetings)} Youngtown meeting(s)")
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
            items = _agenda_items(meeting)
            docs = [
                {
                    "agenda_item_id": "0",
                    "agenda_item_number": "",
                    "body": body,
                    "meeting_id": mid,
                    **doc,
                }
                for doc in meeting["documents"]
            ]
            replace_meeting_data_safe(session, body, mid, meeting, items, supporting_doc_dicts=docs)
            pb_id = session.execute(
                select(PublicBody.id).where(PublicBody.body_code == body)
            ).scalar_one_or_none()
            stored = session.execute(
                select(Meeting).where(Meeting.body == body, Meeting.meeting_id == mid)
            ).scalar_one()
            stored.public_body_id = pb_id
            status = "complete" if items else "no_agenda"
            update_sync_status(session, body, mid, status)
            session.commit()
            print(
                f"  [{index}/{len(meetings)}] {mid} {meeting['meeting_date']}: {len(items)} items, {len(docs)} documents"
            )
    finally:
        session.close()
    return 0
