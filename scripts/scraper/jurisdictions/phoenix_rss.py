"""
Phoenix Legistar scraper using RSS feeds instead of HTML parsing.

Two RSS feeds replace the brittle HTML scraping:

  1. Calendar RSS (Feed.ashx?M=Calendar) — discover meetings for a year
  2. Meeting Detail RSS (Feed.ashx?M=CalendarDetail) — structured agenda items

Supporting documents are extracted by fetching each item's
LegislationDetail.aspx page and parsing View.ashx?M=F links.

The old phoenix.py scraper is kept as a fallback for body types not
covered by the known calendar feeds.
"""

from __future__ import annotations
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ──

BASE_URL = "https://phoenix.legistar.com"
CALENDAR_URL = f"{BASE_URL}/Calendar.aspx"
PUBLIC_BODY_CODE = "phoenix-cc"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Known RSS calendar feeds (ID + GUID pairs).
# Each covers a different set of bodies.
CALENDAR_FEEDS: dict[str, dict[str, str]] = {
    "city-council": {
        "id": "40660991",
        "guid": "c9f1596b-7647-46a0-816b-c717b7f5b475",
    },
}

# Body name → (slug, code)
BODY_SLUG_MAP: dict[str, tuple[str, str]] = {
    "city council formal meeting": ("phoenix-city-council", "phoenix-cc"),
    "city council policy session": ("phoenix-city-council", "phoenix-cc"),
    "city council special meeting": ("phoenix-city-council", "phoenix-cc"),
    "city council work study session": ("phoenix-city-council", "phoenix-cc"),
    "planning commission": ("phoenix-planning-commission", "phoenix-pc"),
    "community services and education subcommittee": ("phoenix-community-services-sub", "phoenix-cs"),
    "economic development and the arts subcommittee": ("phoenix-economic-dev-sub", "phoenix-ed"),
    "public safety and justice subcommittee": ("phoenix-public-safety-sub", "phoenix-ps"),
    "transportation, infrastructure, and planning subcommittee": ("phoenix-transportation-sub", "phoenix-ti"),
    # The "General Information Packet" is a publication, not a meeting of a
    # governing body — retired 2026-09-18 (docs/briefs/035-dev-cleanup-followups-2026-09-18.md).
    # The second element is the body code: None means the record is ingested
    # with no body, which is correct — a document not tied to a body has no body.
    # "phoenix-gp" survives ONLY as the agenda_item_id prefix (see
    # _PACKET_ID_PREFIX) so existing item identities stay stable.
    "general information packet": ("phoenix-general-packet", None),
    "subcommittee general information packet": ("phoenix-sub-packet", "phoenix-sp"),
    "virtual community budget hearing": ("phoenix-budget-hearing", "phoenix-bh"),
}


# Historical agenda_item_id prefix for "General Information Packet" records.
# Packets are ingested with a NULL body (they are publications, not body
# meetings), but their item identities already exist using this prefix, so it
# is retained purely as an id component.
_PACKET_ID_PREFIX = "phoenix-gp"


# ── Helpers ──

def fetch_page(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def fetch_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Failed to download %s: %s", url, e)
        return None


def _resolve_body(body_name: str) -> tuple[str, str, str]:
    lower = body_name.lower().strip()
    for pattern, (slug, code) in BODY_SLUG_MAP.items():
        if lower == pattern or lower.startswith(pattern):
            return slug, code, body_name.strip()
    return "phoenix-city-council", "phoenix-cc", body_name.strip()


def _parse_date_from_title(title: str) -> str:
    """Extract YYYY-MM-DD from a meeting title like 'City Council Formal Meeting - 6/17/2026 - 2:30 PM'."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", title)
    if m:
        month, day, year = m.group(1), m.group(2), m.group(3)
        try:
            return f"{year}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            pass
    return ""


def _extract_id_guid_from_link(link: str) -> tuple[str, str]:
    """Extract ID and GUID from a Gateway.aspx link."""
    params = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
    return (
        (params.get("ID", [""])[0] if "ID" in params else params.get("id", [""])[0]),
        (params.get("GUID", [""])[0] if "GUID" in params else params.get("guid", [""])[0]),
    )


# ── Meeting discovery via Calendar RSS ──

def _calendar_rss_url(year: int, feed_key: str = "city-council") -> str:
    feed = CALENDAR_FEEDS.get(feed_key)
    if not feed:
        raise ValueError(f"Unknown calendar feed: {feed_key}")
    params = urllib.parse.urlencode({
        "M": "Calendar",
        "ID": feed["id"],
        "GUID": feed["guid"],
        "Mode": str(year),
    })
    return f"{BASE_URL}/Feed.ashx?{params}"


def search_meetings_via_rss(year: int) -> list[dict]:
    """Discover Phoenix meetings using the Calendar RSS feed.

    Returns a list of meeting dicts with the same schema as
    ``phoenix.search_phoenix_meetings()``.
    """
    meetings: list[dict] = []
    rss_url = _calendar_rss_url(year)

    try:
        rss_xml = fetch_page(rss_url)
    except Exception as e:
        log.warning("Calendar RSS fetch failed for %s: %s", year, e)
        return meetings

    try:
        root = ET.fromstring(rss_xml)
    except ET.ParseError as e:
        log.warning("Calendar RSS parse failed for %s: %s", year, e)
        return meetings

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    channel = root.find("channel")
    if channel is None:
        return meetings

    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        cat_el = item.find("category")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        cat = cat_el.text.strip() if cat_el is not None and cat_el.text else ""

        if not title or not link:
            continue

        meeting_id, meeting_guid = _extract_id_guid_from_link(link)
        if not meeting_id:
            continue

        meeting_date = _parse_date_from_title(title)
        slug, code, mtype = _resolve_body(cat or title)

        meetings.append({
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "meeting_type": mtype,
            "meeting_title": cat or title.split(" - ")[0] if " - " in title else title,
            "body_slug": slug,
            "body_code": code,
            "meeting_guid": meeting_guid,
            "meeting_detail_url": f"{BASE_URL}/MeetingDetail.aspx?ID={meeting_id}&GUID={meeting_guid}&Options=info|",
            "source_url": link,
            "_rss": True,
        })

    return meetings


def _extract_aspnet_fields(html: str) -> dict[str, str]:
    """Extract ASP.NET form fields (VIEWSTATE, etc.) from Calendar.aspx."""
    fields: dict[str, str] = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]:
        m = re.search(r'name=["\']' + name + r'["\'][^>]*value=["\']([^"\']*)["\']', html)
        if m:
            fields[name] = m.group(1)
        m2 = re.search(r'id=["\']' + name + r'["\'][^>]*value=["\']([^"\']*)["\']', html)
        if m2 and name not in fields:
            fields[name] = m2.group(1)
    return fields


def search_meetings_via_html(year: int = 0) -> list[dict]:
    """Discover Phoenix meetings via the Calendar.aspx HTML page.

    Unlike the old phoenix.py scraper which relied on iCal buttons
    for ID extraction, this function extracts real Legistar meeting IDs
    and GUIDs directly from the MeetingDetail links in the grid.

    Covers ALL body types published on the calendar.
    """
    meetings: list[dict] = []

    # Determine years to scrape
    if year > 0:
        years = [year]
    else:
        from datetime import date as _d
        years = [_d.today().year, _d.today().year - 1]

    for yr in years:
        # Fetch calendar page
        html = fetch_page(CALENDAR_URL)
        fields = _extract_aspnet_fields(html)

        if not fields:
            log.warning("No ASP.NET form fields found on Calendar.aspx")
            continue

        # Build form data to select year
        client_state = (
            '{"logEntries":[],"value":"","text":"' + str(yr) + '",'
            '"enabled":true,"checkedIndices":[],"checkedItemsTextOverflows":false}'
        )
        form_data = [
            ("__VIEWSTATE", fields.get("__VIEWSTATE", "")),
            ("__VIEWSTATEGENERATOR", fields.get("__VIEWSTATEGENERATOR", "")),
            ("__EVENTVALIDATION", fields.get("__EVENTVALIDATION", "")),
            ("ctl00_ContentPlaceHolder1_lstYears_ClientState", client_state),
            ("ctl00_ContentPlaceHolder1_lstYears_Input", str(yr)),
            ("ctl00_ContentPlaceHolder1_txtSearch", ""),
            ("__EVENTTARGET", "ctl00$ContentPlaceHolder1$lstYears"),
            ("__EVENTARGUMENT", ""),
        ]

        data = urllib.parse.urlencode(form_data).encode("utf-8")
        req = urllib.request.Request(
            CALENDAR_URL, data=data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result_html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.warning("Failed to POST year search for %s: %s", yr, e)
            continue

        # Parse meetings from the HTML result
        # Extract grid rows containing meeting data
        rows = re.findall(
            r'<tr[^>]*class="rgRow[^"]*"[^>]*>(.*?)</tr>',
            result_html, re.DOTALL
        )
        alt_rows = re.findall(
            r'<tr[^>]*class="rgAltRow[^"]*"[^>]*>(.*?)</tr>',
            result_html, re.DOTALL
        )
        rows = rows + alt_rows

        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) < 5:
                continue

            # Column 0: Body name
            body_name = re.sub(r"<[^>]+>", " ", cells[0]).strip()
            # Column 1: Date
            date_raw = re.sub(r"<[^>]+>", " ", cells[1]).strip()
            # Column 4: Details column — contains MeetingDetail link
            details_html = cells[4] if len(cells) > 4 else ""

            if not body_name or not date_raw:
                continue

            slug, code, mtype = _resolve_body(body_name)

            # Parse date
            meeting_date = ""
            for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"]:
                try:
                    meeting_date = datetime.strptime(date_raw, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            if not meeting_date:
                meeting_date = date_raw

            # Extract meeting ID and GUID from MeetingDetail link
            meeting_id = ""
            meeting_guid = ""
            meeting_detail_link = re.search(
                r'href="([^"]*MeetingDetail\.aspx[^"]*)"',
                details_html
            )
            if meeting_detail_link:
                qs = meeting_detail_link.group(1).replace("&amp;", "&")
                params = urllib.parse.parse_qs(urllib.parse.urlparse(qs).query)
                meeting_id = params.get("ID", [""])[0]
                meeting_guid = params.get("GUID", [""])[0]

            # Fallback: try iCal link for ID
            if not meeting_id or not meeting_guid:
                ical_link = re.search(r'href="([^"]*)"[^>]*id="[^"]*hypiCal', cells[2] if len(cells) > 2 else "", re.I)
                if ical_link:
                    qs = ical_link.group(1).replace("&amp;", "&")
                    params = urllib.parse.parse_qs(urllib.parse.urlparse(qs).query)
                    meeting_id = params.get("ID", [""])[0] if "ID" in params else params.get("id", [""])[0]
                    meeting_guid = params.get("GUID", [""])[0] if "GUID" in params else params.get("guid", [""])[0]

            if not meeting_id:
                continue

            detail_url = f"{BASE_URL}/MeetingDetail.aspx?ID={meeting_id}&GUID={meeting_guid}&Options=info|"

            meetings.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": mtype,
                "meeting_title": body_name,
                "body_slug": slug,
                "body_code": code,
                "meeting_guid": meeting_guid,
                "meeting_detail_url": detail_url,
                "source_url": detail_url,
                "_html": True,
            })

    return meetings


# ── Item extraction via CalendarDetail RSS ──

def _meeting_detail_rss_url(meeting_id: str, meeting_guid: str) -> str:
    params = urllib.parse.urlencode({
        "M": "CalendarDetail",
        "ID": meeting_id,
        "GUID": meeting_guid,
    })
    return f"{BASE_URL}/Feed.ashx?{params}"


def parse_items_from_rss(rss_xml: str, body_code: str = "phoenix-cc",
                         meeting_id: str = "") -> list[dict]:
    """Parse agenda items from a CalendarDetail RSS feed.

    The feed items contain structured descriptions with:
      File #, Agenda #, Type, Title, Action, Result

    Returns a list of item dicts matching the agenda_items schema.
    """
    items: list[dict] = []

    try:
        root = ET.fromstring(rss_xml)
    except ET.ParseError as e:
        log.warning("CalendarDetail RSS parse failed: %s", e)
        return items

    channel = root.find("channel")
    if channel is None:
        return items

    sort_order = 0
    for item in channel.findall("item"):
        title_el = item.find("title")
        desc_el = item.find("description")
        cat_el = item.find("category")
        link_el = item.find("link")

        if title_el is None or title_el.text is None:
            continue

        file_number = title_el.text.strip()
        category = cat_el.text.strip() if cat_el is not None and cat_el.text else ""

        # Parse structured fields from description
        agenda_number = ""
        item_type = category
        item_title = ""
        action_result = ""
        legislation_id = ""
        legislation_guid = ""
        legislation_url = ""

        # Extract legislation GUID from the link
        if link_el is not None and link_el.text:
            leg_id, leg_guid = _extract_id_guid_from_link(link_el.text)
            legislation_id = leg_id
            legislation_guid = leg_guid
            legislation_url = link_el.text

        # Parse description HTML
        if desc_el is not None and desc_el.text:
            desc_text = desc_el.text
            # Extract fields from <br />-separated key: value pairs
            sections = re.split(r"<br\s*/?>", desc_text, flags=re.IGNORECASE)
            for sec in sections:
                sec = re.sub(r"<[^>]+>", " ", sec).strip()
                if sec.startswith("Agenda #:"):
                    agenda_number = sec.replace("Agenda #:", "").strip()
                elif sec.startswith("Title:"):
                    item_title = sec.replace("Title:", "").strip()
                elif sec.startswith("Type:") and not item_type:
                    item_type = sec.replace("Type:", "").strip()
                elif sec.startswith("Action:"):
                    action_val = sec.replace("Action:", "").strip()
                    if action_val:
                        action_result = action_val
                elif sec.startswith("Result:"):
                    result_val = sec.replace("Result:", "").strip()
                    if result_val:
                        action_result = (action_result + " - " + result_val if action_result else result_val)

        if not item_title:
            continue

        sort_order += 1
        an = agenda_number or str(sort_order)
        # Packets are ingested with body_code = None.  Keep the historical
        # prefix in agenda_item_id so item identities do not change.
        aid = f"{body_code or _PACKET_ID_PREFIX}-{meeting_id}_{an}"

        items.append({
            "meeting_id": meeting_id,
            "agenda_item_id": aid,
            "agenda_item_number": an,
            "file_number": file_number,
            "item_type": item_type,
            "item_type_category": "item",
            "agenda_item_title": item_title,
            "agenda_item_text": item_title,
            "action_result": action_result,
            "legislation_id": legislation_id,
            "legislation_guid": legislation_guid,
            "legislation_url": legislation_url,
            "source_body": body_code,
            "source_url": legislation_url or f"{BASE_URL}/MeetingDetail.aspx?ID={meeting_id}",
            "section_level": 0,
            "sort_order": sort_order,
        })

    return items


def fetch_meeting_items_via_rss(meeting_id: str, meeting_guid: str,
                                body_code: str = "phoenix-cc",
                                leg_limit: int = 0) -> tuple[list[dict], list[dict]]:
    """Fetch agenda items and meeting-level docs for a Phoenix meeting.

    1. Fetch the CalendarDetail RSS feed for structured items
    2. Fetch MeetingDetail.aspx HTML for the agenda PDF link
    3. Fetch each item's LegislationDetail.aspx page for document attachments

    Parameters
    ----------
    leg_limit : int
        Max legislation detail pages to fetch for documents (0 = all).
        Use a small number for testing; 0 for production.

    Returns
    -------
    (agenda_items, supporting_docs)
    """
    items: list[dict] = []
    supp_docs: list[dict] = []

    # -- Step 1: Fetch CalendarDetail RSS feed for items --
    rss_url = _meeting_detail_rss_url(meeting_id, meeting_guid)
    try:
        rss_xml = fetch_page(rss_url)
        items = parse_items_from_rss(rss_xml, body_code, meeting_id)
    except Exception as e:
        log.warning("Failed to fetch CalendarDetail RSS for %s: %s", meeting_id, e)

    # -- Step 2: Fetch MeetingDetail page for agenda PDF --
    detail_url = f"{BASE_URL}/MeetingDetail.aspx?ID={meeting_id}&GUID={meeting_guid}&Options=info|"
    try:
        html = fetch_page(detail_url)
        # Extract agenda PDF (View.ashx?M=A)
        for m in re.finditer(
            r'<a\s+[^>]*href="(View\.ashx\?M=A[^"]*)"[^>]*>([^<]+)</a>',
            html
        ):
            href = m.group(1)
            title = m.group(2).strip()
            supp_docs.append({
                "document_url": urllib.parse.urljoin(BASE_URL, href),
                "document_title": title or "Agenda",
                "document_type": "Agenda",
                "agenda_item_number": "",
                "agenda_item_id": "0",
                "body": body_code,
                "meeting_id": meeting_id,
            })
            break
    except Exception as e:
        log.debug("MeetingDetail page fetch failed for %s: %s", meeting_id, e)

    # -- Step 3: Fetch per-item legislation pages for document attachments --
    for idx, item in enumerate(items):
        if leg_limit > 0 and idx >= leg_limit:
            break
        
        leg_url = item.get("legislation_url", "")
        leg_id = item.get("legislation_id", "")
        leg_guid = item.get("legislation_guid", "")
        if not leg_url and leg_id and leg_guid:
            leg_url = f"{BASE_URL}/LegislationDetail.aspx?ID={leg_id}&GUID={leg_guid}&Options=&Search="

        if leg_url:
            try:
                leg_html = fetch_page(leg_url, timeout=15)
                # Find attachments: <a href="View.ashx?M=F...">
                for m in re.finditer(
                    r'<a\s+href="(View\.ashx\?M=F[^"]*)"[^>]*>([^<]+)</a>',
                    leg_html
                ):
                    href = m.group(1)
                    title = m.group(2).strip()
                    doc_url = urllib.parse.urljoin(BASE_URL, href)
                    supp_docs.append({
                        "document_url": doc_url,
                        "document_title": title,
                        "document_type": "Attachment",
                        "agenda_item_number": item.get("agenda_item_number", ""),
                        "agenda_item_id": "0",
                        "body": body_code,
                        "meeting_id": meeting_id,
                    })
            except Exception as e:
                log.debug("Legislation detail fetch failed for %s item %s: %s",
                          meeting_id, item.get("agenda_item_number"), e)

    return items, supp_docs


# ── Cleanup ──

if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)

    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        meeting_id = sys.argv[2] if len(sys.argv) > 2 else "1364170"
        meeting_guid = sys.argv[3] if len(sys.argv) > 3 else "81251288-E012-44AC-A33C-909216824710"

        items, docs = fetch_meeting_items_via_rss(meeting_id, meeting_guid)
        print(f"Items: {len(items)}")
        for it in items[:10]:
            print(f"  #{it['agenda_item_number']:4s} {it['item_type'][:30]:30s} {it['agenda_item_title'][:60]}")
            if it.get("legislation_guid"):
                print(f"       leg_guid={it['legislation_guid'][:20]}...")
        print(f"\nDocs: {len(docs)}")
        for d in docs:
            print(f"  {d['document_type']:10s} {d['document_title'][:50]}")
