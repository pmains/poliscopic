"""
City of Mesa meeting and agenda extraction via Legistar (Granicus).

Mesa uses the Legistar agenda management system at ``mesa.legistar.com``.

The Calendar page (Calendar.aspx) lists upcoming/recent meetings for all
public bodies in a Telerik RadGrid HTML table.  Each row includes:
  - Body name (link to DepartmentDetail.aspx)
  - Meeting date
  - Meeting time
  - Meeting location
  - Meeting Details (link to MeetingDetail.aspx)
  - Agenda (View.ashx?M=A)
  - Accessible Agenda (View.ashx?M=AADA)
  - Minutes (View.ashx?M=M)
  - Video link

MeetingDetail.aspx shows agenda items in an HTML table with columns:
  File#, Agenda#, Type, Title, Action Result, Action Details.

LegislationDetail.aspx shows individual item details with attachments.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

from scraper.common.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.common.io_utils import normalize_meeting_date
from scraper.common.models import _HtmlNode

log = logging.getLogger(__name__)

# ── Jurisdiction / body constants ──

PUBLIC_BODY_CODE = "mesa-city-council"
SOURCE_SYSTEM = "legistar"
SOURCE_INSTANCE_URL = "https://mesa.legistar.com"

# ── Public body slug mapping ──
# Matches body names in the Legistar Calendar.aspx and DepartmentDetail.aspx
# to our public body slugs.

BODY_SLUG_MAP = {
    "city council": "mesa-city-council",
    "city council study session": "mesa-city-council",
    "city council strategic planning session": "mesa-city-council",
    "planning and zoning board": "mesa-planning-zoning",
    "planning and zoning board - public hearing": "mesa-planning-zoning",
    "planning and zoning board - study session": "mesa-planning-zoning",
    "design review board": "mesa-design-review-board",
    "board of adjustment": "mesa-board-of-adjustment",
    "board of adjustment public hearing": "mesa-board-of-adjustment",
    "board of adjustment study session": "mesa-board-of-adjustment",
    "historic preservation board": "mesa-historic-preservation-board",
    "cadence community facilities district board": "mesa-cadence-cfd",
    "eastmark community facilities district no. 1 board": "mesa-eastmark-cfd-1",
    "eastmark community facilities district no. 2 board": "mesa-eastmark-cfd-2",
    "eastmark community facilities district board": "mesa-eastmark-cfd-1",
}

# Body code → code slug for the ``body`` column on meetings/agenda_items
#
# The four abbreviated codes (mesa-cc, mesa-boa, mesa-drb, mesa-hpb) were
# renamed to their descriptive forms in the 2026-09-18 dev body-code cleanup
# (docs/briefs/034-dev-body-cleanup-changes-2026-09-18.md), so most entries are
# now identity.  ``mesa-planning-zoning`` → ``mesa-pz`` is still a real
# translation: the slug differs from the canonical code.
BODY_CODE_MAP = {
    "mesa-city-council": "mesa-city-council",
    "mesa-planning-zoning": "mesa-pz",
    "mesa-design-review-board": "mesa-design-review-board",
    "mesa-board-of-adjustment": "mesa-board-of-adjustment",
    "mesa-historic-preservation-board": "mesa-historic-preservation-board",
    "mesa-cadence-cfd": "mesa-cadence",
    "mesa-eastmark-cfd-1": "mesa-eastmark1",
    "mesa-eastmark-cfd-2": "mesa-eastmark2",
}

# Default body slugs — include CFD boards for full coverage
DEFAULT_BODY_SLUGS = [
    "mesa-city-council",
    "mesa-planning-zoning",
    "mesa-design-review-board",
    "mesa-historic-preservation-board",
    "mesa-board-of-adjustment",
    "mesa-cadence-cfd",
    "mesa-eastmark-cfd-1",
    "mesa-eastmark-cfd-2",
]


# ── URL patterns ──

BASE_URL = "https://mesa.legistar.com"
CALENDAR_URL = f"{BASE_URL}/Calendar.aspx"

# ── Parsing helpers ──

def _attr(node: _HtmlNode, key: str) -> str:
    """Get an attribute value from an HTML node, or empty string."""
    return (node.attrs.get(key) or "").strip()


def _text(node: _HtmlNode | None) -> str:
    """Get cleaned text from an HTML node."""
    if node is None:
        return ""
    return _clean_html_text(_node_text(node))


def _find_link(node: _HtmlNode, id_contains: str = "") -> Optional[_HtmlNode]:
    """Find an anchor tag within *node* whose id contains *id_contains*."""
    for a in _find_all(node, "a"):
        aid = (a.attrs.get("id") or "")
        if id_contains in aid:
            return a
    return None


# ── Meeting list extraction ──

def parse_meetings_from_html(html: str) -> list[dict]:
    """Parse the RadGrid meeting table from Calendar.aspx or DepartmentDetail.aspx.

    Returns a list of meeting dicts with keys:
      - body_name       : full body name from the Name column
      - body_slug       : normalized body slug
      - body_code       : short body code
      - meeting_date    : MM/DD/YYYY
      - meeting_time    : HH:MM AM/PM
      - meeting_location: venue name
      - meeting_id      : Legistar meeting ID
      - meeting_guid    : Legistar meeting GUID
      - meeting_detail_url : MeetingDetail.aspx URL
      - agenda_url      : View.ashx?M=A URL
      - accessible_agenda_url : View.ashx?M=AADA URL
      - minutes_url     : View.ashx?M=M URL
      - agenda_packet_url : View.ashx?M=AP URL
      - video_url       : video link URL (YouTube or Mesa 11)
      - source_url      : URL of the page this was parsed from
    """
    root = _parse_html(html)
    meetings: list[dict] = []

    # Find the RadGrid master table
    table = _find_radgrid_table(root)
    if table is None:
        log.warning("No RadGrid table found in HTML")
        return meetings

    # Find data rows (skip header rows)
    rows = [tr for tr in _find_all(table, "tr")
            if "rgRow" in (tr.attrs.get("class") or "")
            or "rgAltRow" in (tr.attrs.get("class") or "")]

    log.debug("Found %d meeting rows in RadGrid", len(rows))

    for row in rows:
        cells = [td for td in _find_all(row, "td") if td.tag == "td"]
        if len(cells) < 6:
            continue

        # Column 0: Body Name (with link to DepartmentDetail)
        body_name = ""
        body_dept_id = ""
        body_dept_guid = ""
        body_link = _find_link(cells[0], "hypBody")
        if body_link:
            body_name = _text(body_link)
            href = _attr(body_link, "href")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            body_dept_id = params.get("ID", [""])[0]
            body_dept_guid = params.get("GUID", [""])[0]

        # Column 1: Meeting Date
        meeting_date = _text(cells[1])

        # Column 3: Meeting Time (column 2 is the iCal icon)
        meeting_time = _text(cells[3]) if len(cells) > 3 else ""

        # Column 4: Meeting Location
        meeting_location = _text(cells[4]) if len(cells) > 4 else ""

        # Column 5: Meeting Details link
        meeting_id = ""
        meeting_guid = ""
        meeting_detail_url = ""
        detail_link = _find_link(cells[5], "hypMeetingDetail")
        if detail_link:
            href = _attr(detail_link, "href")
            if href and "MeetingDetail.aspx" in href:
                meeting_detail_url = urllib.parse.urljoin(BASE_URL, href)
                params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                meeting_id = params.get("ID", [""])[0]
                meeting_guid = params.get("GUID", [""])[0]

        if not meeting_id:
            log.debug("Skipping row without meeting ID: %s", body_name)
            continue

        # Column 6: Agenda link
        agenda_url = ""
        agenda_link = _find_link(cells[6], "hypAgenda") if len(cells) > 6 else None
        if agenda_link:
            href = _attr(agenda_link, "href")
            if href:
                agenda_url = urllib.parse.urljoin(BASE_URL, href)

        # Column 7: Accessible Agenda
        accessible_agenda_url = ""
        aa_link = _find_link(cells[7], "hypAccessibleAgendaHTML") if len(cells) > 7 else None
        if aa_link:
            href = _attr(aa_link, "href")
            if href:
                accessible_agenda_url = urllib.parse.urljoin(BASE_URL, href)

        # Column 8: Agenda Packet
        agenda_packet_url = ""
        ap_link = _find_link(cells[8], "hypAgendaPacket") if len(cells) > 8 else None
        if ap_link:
            href = _attr(ap_link, "href")
            if href and "Not" not in href:
                agenda_packet_url = urllib.parse.urljoin(BASE_URL, href)

        # Column 9: Minutes
        minutes_url = ""
        minutes_link = _find_link(cells[9], "hypMinutes") if len(cells) > 9 else None
        if minutes_link:
            href = _attr(minutes_link, "href")
            if href and "Not" not in href:
                minutes_url = urllib.parse.urljoin(BASE_URL, href)

        # Column 11: Video
        video_url = ""
        video_link = _find_link(cells[11], "hypVideo") if len(cells) > 11 else None
        if video_link:
            href = _attr(video_link, "href")
            if href and "Not" not in href and href != "#":
                video_url = urllib.parse.urljoin(BASE_URL, href)

        body_slug = _resolve_body_slug(body_name)
        body_code = BODY_CODE_MAP.get(body_slug, "mesa-city-council")

        m = {
            "body_name": body_name,
            "body_slug": body_slug,
            "body_code": body_code,
            "body_dept_id": body_dept_id,
            "body_dept_guid": body_dept_guid,
            "meeting_date": normalize_meeting_date(meeting_date) or meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": meeting_location,
            "meeting_id": meeting_id,
            "meeting_guid": meeting_guid,
            "meeting_detail_url": meeting_detail_url,
            "agenda_url": agenda_url,
            "accessible_agenda_url": accessible_agenda_url,
            "minutes_url": minutes_url,
            "agenda_packet_url": agenda_packet_url,
            "video_url": video_url,
        }
        meetings.append(m)

    return meetings


def _find_radgrid_table(root: _HtmlNode) -> Optional[_HtmlNode]:
    """Find the RadGrid master table in the parsed HTML tree.

    Legistar RadGrid has class ``rgMasterTable`` and id containing
    ``gridCalendar`` or ``gridMeetings``.
    """
    for table in _find_all(root, "table"):
        cls = table.attrs.get("class") or ""
        tbl_id = table.attrs.get("id") or ""
        if "rgMasterTable" in cls and ("gridCalendar" in tbl_id or "gridMeetings" in tbl_id):
            return table
    # Fallback: find any table with rgMasterTable class
    for table in _find_all(root, "table"):
        cls = table.attrs.get("class") or ""
        if "rgMasterTable" in cls:
            return table
    return None


def _resolve_body_slug(body_name: str) -> str:
    """Normalize a Legistar body name to our slug."""
    key = body_name.lower().strip()
    # Direct match
    if key in BODY_SLUG_MAP:
        return BODY_SLUG_MAP[key]
    # Partial match by prefix
    for pattern, slug in sorted(BODY_SLUG_MAP.items(), key=lambda x: -len(x[0])):
        if key.startswith(pattern):
            return slug
    log.debug("Unknown Mesa body name: '%s', defaulting to mesa-city-council", body_name)
    return "mesa-city-council"


def meetings_for_body(meetings: list[dict], body_slug: str) -> list[dict]:
    """Filter meeting list to only those belonging to a specific body."""
    return [m for m in meetings if m.get("body_slug") == body_slug]


# ── Agenda item extraction ──

def parse_agenda_items_from_html(html: str, meeting_id: str,
                                  body_code: str = "mesa-city-council") -> list[dict]:
    """Parse agenda items from a MeetingDetail.aspx HTML page.

    The MeetingDetail page shows items in an HTML table with columns:
      File#, Agenda#, Type, Title, Action Result, Action Details.

    Returns a list of item dicts with keys:
      - meeting_id
      - agenda_item_number  : e.g. "2", "3-a", "4-a"
      - file_number         : e.g. "26-0034"
      - item_type           : e.g. "Minutes", "Contract", "Resolution", "Ordinance"
      - agenda_item_title
      - agenda_item_text    : description text
      - action_result       : e.g. "Not available", "Approved"
      - legislation_id      : Legistar legislation ID
      - legislation_guid    : Legistar legislation GUID
      - legislation_url     : LegislationDetail.aspx URL
      - item_type_category  : "section" for headers, "item" for action items
      - section_level       : nested depth
    """
    root = _parse_html(html)
    items: list[dict] = []

    # The MeetingDetail page renders agenda items in <table id="..._gridLegislation_ctl00">
    # or within a RadGrid with id containing "gridLegislation"
    grid = _find_legislation_grid(root)
    if grid is None:
        log.debug("No legislation grid found for meeting %s", meeting_id)
        return items

    rows = [tr for tr in _find_all(grid, "tr")
            if "rgRow" in (tr.attrs.get("class") or "")
            or "rgAltRow" in (tr.attrs.get("class") or "")]

    log.debug("Found %d agenda item rows for meeting %s", len(rows), meeting_id)

    sort_order = 0
    for row in rows:
        cells = [td for td in _find_all(row, "td") if td.tag == "td"]
        if len(cells) < 4:
            continue

        # Column 0: File# (link to LegislationDetail)
        file_number = _text(cells[0])
        legislation_id = ""
        legislation_guid = ""
        legislation_url = ""
        file_link = _find_link(cells[0])
        if file_link:
            href = _attr(file_link, "href")
            if href and "LegislationDetail.aspx" in href:
                legislation_url = urllib.parse.urljoin(BASE_URL, href)
                params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                legislation_id = params.get("ID", [""])[0]
                legislation_guid = params.get("GUID", [""])[0]

        # Column 1: Agenda#
        agenda_number = _text(cells[1])

        # Column 2: Type (Minutes, Liquor License, Contract, Resolution, Ordinance, etc.)
        item_type = _text(cells[2])

        # Column 3: Title
        title = _text(cells[3])

        # Column 6 (last): Action Details / Result
        action_result = ""
        if len(cells) > 6:
            action_link = _find_link(cells[6])
            if action_link:
                action_result = _text(action_link)

        sort_order += 1

        item = {
            "meeting_id": meeting_id,
            "agenda_item_number": agenda_number,
            "file_number": file_number,
            "item_type": item_type,
            "agenda_item_title": title,
            "agenda_item_text": "",
            "action_result": action_result,
            "legislation_id": legislation_id,
            "legislation_guid": legislation_guid,
            "legislation_url": legislation_url,
            "item_type_category": "item",
            "section_level": 0,
            "sort_order": sort_order,
        }
        items.append(item)

    return items


def _find_legislation_grid(root: _HtmlNode) -> Optional[_HtmlNode]:
    """Find the legislation RadGrid table in the MeetingDetail HTML.

    The grid is typically named ``gridMain`` or ``gridLegislation``,
    both of which have the ``rgMasterTable`` class.
    """
    for table in _find_all(root, "table"):
        cls = table.attrs.get("class") or ""
        tbl_id = table.attrs.get("id") or ""
        if "rgMasterTable" in cls and ("gridMain" in tbl_id or "gridLegislation" in tbl_id):
            return table
    return None


def _split_title_and_action(text: str) -> tuple[str, str]:
    """Split agenda item cell text into title and action result.

    In the Legistar MeetingDetail table, the Title column may contain
    both the item title and the action result (on a new line).
    """
    text = text.strip()
    # If there's a visible break in the text (multiple spaces, newlines)
    parts = re.split(r"\s{3,}|\n|\r", text, maxsplit=1)
    if len(parts) == 2:
        title = parts[0].strip()
        action = parts[1].strip()
        return title, action
    return text, ""


# ── Legislation detail extraction ──

def parse_legislation_detail_from_html(html: str) -> dict:
    """Parse a LegislationDetail.aspx page for item details and attachments.

    Returns a dict with keys:
      - file_number
      - item_type
      - status
      - title
      - description  : full legislation body/description text
      - attachments : list of dicts [{title, url}]
    """
    root = _parse_html(html)
    result: dict = {
        "file_number": "",
        "item_type": "",
        "status": "",
        "title": "",
        "description": "",
        "attachments": [],
    }

    # Find the detail fields
    # Legistar uses label-value pairs like "File #:", "Type:", "Status:", "Title:"
    all_spans = _find_all(root, "span")
    for i, span in enumerate(all_spans):
        span_text = _clean_html_text(_node_text(span)).strip()

        if span_text.startswith("File #") or span_text.startswith("File #:"):
            # The value is in the next sibling or the same container
            parent = span.parent
            if parent:
                full_text = _clean_html_text(_node_text(parent))
                # Extract value after the label
                m = re.search(r"File\s*#:\s*(.+)", full_text)
                if m:
                    result["file_number"] = m.group(1).strip()

        elif span_text.startswith("Type:"):
            parts = span_text.split(":", 1)
            if len(parts) > 1:
                result["item_type"] = parts[1].strip()
            # Also look at what follows in siblings
            parent = span.parent
            if parent:
                for child in parent.children:
                    if isinstance(child, _HtmlNode) and child.tag == "a":
                        result["item_type"] = _text(child)

        elif span_text.startswith("Status:"):
            result["status"] = span_text.split(":", 1)[1].strip() if ":" in span_text else ""

        elif span_text.startswith("Title:"):
            result["title"] = span_text.split(":", 1)[1].strip() if ":" in span_text else ""

    # Find attachments under "Attachments:" header
    # Attachments are <a> tags with href containing View.ashx?M=F
    for a in _find_all(root, "a"):
        href = _attr(a, "href")
        if "View.ashx?M=F" in href:
            title = _text(a)
            if title and title not in ("Not available", "Not available "):
                result["attachments"].append({
                    "title": title,
                    "url": urllib.parse.urljoin(BASE_URL, href),
                })

    # Extract description text from the legislation body
    # The description lives in #ctl00_ContentPlaceHolder1_divText (or pageText
    # if the tab is not 'Details').  The text structure is:
    #   Title{title text}end{description sections...}
    # We strip the "Title" prefix (the field label) and the title text up to
    # the "end" marker, keeping everything after as the description.
    for div_id in ("ctl00_ContentPlaceHolder1_divText", "ctl00_ContentPlaceHolder1_pageText"):
        text_div = None
        for div in _find_all(root, "div"):
            if (div.attrs.get("id") or "") == div_id:
                text_div = div
                break
        if text_div is not None:
            raw_text = _clean_html_text(_node_text(text_div)).strip()
            # Skip empty divs
            if not raw_text:
                continue
            # Remove the "Title" label prefix (could be "Title" alone or
            # "Title:" depending on rendering) and everything up to "end"
            stripped = raw_text
            if stripped.startswith("Title:"):
                stripped = stripped[6:].lstrip()
            elif stripped.startswith("Title"):
                stripped = stripped[5:].lstrip()
            # Find the "end" marker that separates header from body
            end_idx = stripped.find("end")
            if end_idx >= 0:
                # Skip past "end" plus any following whitespace/newlines
                stripped = stripped[end_idx + 3:].lstrip()
            result["description"] = stripped
            break

    return result


# ── Meeting title normalization ──

def normalize_meeting_title(raw_title: str) -> str:
    """Normalize a Mesa meeting title."""
    title = raw_title.replace("\u2013", "-").strip()
    # Strip cancel/reschedule prefixes
    title = re.sub(r"^CANCEL(?:LED|ED)?\s*-\s*", "", title, flags=re.IGNORECASE)
    return title.strip()


def extract_meeting_type(title: str) -> str:
    """Derive a meeting type from the meeting title."""
    tl = title.lower()
    if "study session" in tl:
        return "Study Session"
    if "strategic planning" in tl:
        return "Strategic Planning Session"
    if "executive session" in tl:
        return "Executive Session"
    if "special" in tl:
        return "Special Meeting"
    if "public hearing" in tl:
        return "Public Hearing"
    if "regular" in tl or tl in ("city council", "planning and zoning board"):
        return "Regular Meeting"
    return "Regular Meeting"


# ── Year-based search via POST ──

def _extract_aspnet_form_fields(html: str) -> dict[str, str]:
    """Extract ASP.NET WebForms hidden fields from a page."""
    fields = {}
    for name in (
        "__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR",
        "__PREVIOUSFOCUSED", "__CT100", "__LASTFOCUS",
    ):
        m = re.search(
            rf'(?:id|name)="{name}"[^>]*value="([^"]*)"', html
        )
        if m:
            fields[name] = m.group(1)
        else:
            fields[name] = ""
    return fields


def _build_year_client_state(year_label: str) -> str:
    """Build a Telerik RadComboBox ClientState JSON string for a year selection."""
    import json
    state = {
        "logEntries": [],
        "value": year_label,
        "text": year_label,
        "enabled": True,
        "checkedIndices": [],
        "checkedItemsTextOverflows": False,
    }
    return json.dumps(state)


def search_mesa_meetings_by_year(year: int) -> list[dict]:
    """Search Mesa Legistar for all meetings in a given year using ASP.NET POST.

    Fetches the Calendar.aspx page, extracts the ASP.NET form fields, then
    POSTs back with the RadComboBox year dropdown set to the requested year.
    The Legistar server returns meetings for that year (max 100 per page).

    Parameters
    ----------
    year : int
        The year to search for (e.g., 2025).

    Returns
    -------
    list[dict]
        Meeting dicts as returned by ``parse_meetings_from_html()``.
    """
    import urllib.parse

    year_label = str(year)

    # Step 1: GET the Calendar page
    html = fetch_page(CALENDAR_URL)
    fields = _extract_aspnet_form_fields(html)

    client_state = _build_year_client_state(year_label)

    # Step 2: POST with the year selection
    form_data = [
        ("__VIEWSTATE", fields.get("__VIEWSTATE", "")),
        ("__VIEWSTATEGENERATOR", fields.get("__VIEWSTATEGENERATOR", "")),
        ("__EVENTVALIDATION", fields.get("__EVENTVALIDATION", "")),
        ("ctl00_ContentPlaceHolder1_lstYears_ClientState", client_state),
        ("ctl00_ContentPlaceHolder1_lstYears_Input", year_label),
        ("ctl00_ContentPlaceHolder1_txtSearch", ""),
        ("__EVENTTARGET", "ctl00$ContentPlaceHolder1$lstYears"),
        ("__EVENTARGUMENT", ""),
        ("__PREVIOUSFOCUSED", fields.get("__PREVIOUSFOCUSED", "")),
        ("__CT100", fields.get("__CT100", "")),
    ]

    data = urllib.parse.urlencode(form_data).encode("utf-8")

    req = urllib.request.Request(
        CALENDAR_URL,
        data=data,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to POST year search for %s: %s", year_label, e)
        raise

    meetings = parse_meetings_from_html(result_html)
    log.info(
        "Found %d Mesa meeting(s) for year %s",
        len(meetings), year_label,
    )
    return meetings


# ── HTTP helpers ──

def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch an HTML page from Mesa Legistar.

    Uses urllib with a reasonable User-Agent header.
    """
    import urllib.request
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


# ── Top-level search / extract operations ──

def search_mesa_meetings(
    body_slugs: Optional[list[str]] = None,
    year: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Search for Mesa meetings on the Legistar Calendar page.

    Fetches the Calendar.aspx page and parses all meeting rows.
    Optionally filters by body slug(s).

    If *year* is given, uses ASP.NET POST to set the date-range combo box
    to that year, returning meetings from that year (max 100).

    If *start_date* and *end_date* are given (ISO-8601 format), fetches the
    relevant year(s) and returns only meetings within that date range.

    When both *year* and date range are omitted, returns the current default
    view (typically recent meetings).

    Parameters
    ----------
    body_slugs : list[str], optional
        Only return meetings for these body slugs.  Defaults to ``["mesa-city-council"]``.
    year : int, optional
        Year to search for (e.g. 2025). Uses POST-based date range filtering.
    start_date : str, optional
        ISO-8601 start date (e.g. "2026-06-30").  Requires *end_date*.
    end_date : str, optional
        ISO-8601 end date (e.g. "2026-07-03").  Requires *start_date*.

    Returns
    -------
    list[dict]
        Meeting dicts as returned by ``parse_meetings_from_html()``.
    """
    from datetime import date as _Date

    if body_slugs is None:
        body_slugs = DEFAULT_BODY_SLUGS

    # ── Date-range mode: fetch needed year(s), then filter ──
    if start_date and end_date:
        # Parse the ISO-8601 bounds from the CLI args
        sd_str = start_date.replace("-", "")  # YYYYMMDD for lex comparison
        ed_str = end_date.replace("-", "")
        sd_year = int(start_date[:4])
        ed_year = int(end_date[:4])
        years_to_fetch = set(range(sd_year, ed_year + 1))

        all_meetings: list[dict] = []
        for y in sorted(years_to_fetch):
            all_meetings.extend(search_mesa_meetings_by_year(y))

        def _meeting_key(md: str) -> str:
            """Turn a Legistar meeting date (MM/DD/YYYY) into YYYYMMDD for
            string comparison."""
            parts = md.split("/")
            if len(parts) == 3:
                return f"{parts[2]}{int(parts[0]):02d}{int(parts[1]):02d}"
            return md.replace("-", "")

        # Filter by body slug AND date range
        filtered = [
            m for m in all_meetings
            if m.get("body_slug") in body_slugs
            and sd_str <= _meeting_key(m.get("meeting_date", "")) <= ed_str
        ]

        log.info(
            "Found %d Mesa meeting(s) in date range %s – %s (%d fetched, %d within bodies)",
            len(filtered), start_date, end_date, len(all_meetings),
            len(filtered),
        )
        return filtered

    # ── Year mode ──
    if year is not None:
        all_meetings = search_mesa_meetings_by_year(year)
    else:
        html = fetch_page(CALENDAR_URL)
        all_meetings = parse_meetings_from_html(html)

    # Filter to requested bodies
    filtered = [m for m in all_meetings if m.get("body_slug") in body_slugs]

    log.info(
        "Found %d Mesa meeting(s) (%d total for %s)",
        len(filtered), len(all_meetings),
        str(year) if year else "current view",
    )
    return filtered


async def search_mesa_meetings_with_playwright(
    page, body_slug: Optional[str] = None
) -> list[dict]:
    """Search for Mesa meetings using Playwright (handles JS-rendered content).

    Uses the Calendar.aspx page but also attempts to change the date range
    to ``All Years`` to get as many meetings as possible.

    Parameters
    ----------
    page : playwright.async_api.Page
    body_slug : str, optional
        Filter to a specific body slug.

    Returns
    -------
    list[dict]
        Meeting dicts.
    """
    import asyncio

    await page.goto(CALENDAR_URL, wait_until="networkidle", timeout=30000)

    # Try to select "All Years" from the date range RadComboBox
    # The combo box has id ctl00_ContentPlaceHolder1_lstYears
    try:
        # Click the dropdown arrow
        arrow = await page.query_selector("#ctl00_ContentPlaceHolder1_lstYears_Arrow")
        if arrow:
            await arrow.click()
            await asyncio.sleep(1)
            # Click "All Years" option
            all_years = await page.query_selector("li.rcbItem:has-text('All Years')")
            if all_years:
                await all_years.click()
                await asyncio.sleep(2)  # Wait for postback
                await page.wait_for_load_state("networkidle", timeout=10000)
                log.info("Set date range to 'All Years'")
    except Exception as e:
        log.warning("Could not change date range: %s", e)

    html = await page.content()
    all_meetings = parse_meetings_from_html(html)

    if body_slug:
        filtered = [m for m in all_meetings if m.get("body_slug") == body_slug]
    else:
        filtered = all_meetings

    log.info(
        "Found %d Mesa meeting(s) via Playwright (%d total)",
        len(filtered), len(all_meetings)
    )
    for m in filtered:
        # Derive meeting_type from title
        m["meeting_type"] = extract_meeting_type(m.get("body_name", ""))
        m["meeting_title"] = normalize_meeting_title(m.get("body_name", ""))

    return filtered


async def fetch_agenda_items_with_playwright(
    page, meeting_detail_url: str, meeting_id: str, body_code: str = "mesa-city-council"
) -> list[dict]:
    """Fetch and parse agenda items from a MeetingDetail page via Playwright.

    Parameters
    ----------
    page : playwright.async_api.Page
    meeting_detail_url : str
        Full URL to the MeetingDetail.aspx page.
    meeting_id : str
        Legistar meeting ID.
    body_code : str
        Body code for the meeting.

    Returns
    -------
    list[dict]
        Agenda item dicts.
    """
    await page.goto(meeting_detail_url, wait_until="networkidle", timeout=30000)
    html = await page.content()
    return parse_agenda_items_from_html(html, meeting_id, body_code)


def _enrich_agenda_item_descriptions(
    items: list[dict],
    max_concurrent: int = 3,
) -> None:
    """Fetch LegislationDetail pages for items with a legislation_url and
    populate their ``agenda_item_text`` with the description text.

    Items without a ``legislation_url`` or whose detail page fails are left
    with an empty ``agenda_item_text``.

    Parameters
    ----------
    items : list[dict]
        Agenda item dicts from ``parse_agenda_items_from_html()``.  Modified
        in-place with ``agenda_item_text`` updated.
    max_concurrent : int
        Maximum concurrent fetches (not implemented yet; runs sequentially).
    """
    import urllib.request as _ur

    for item in items:
        leg_url = item.get("legislation_url", "")
        if not leg_url:
            continue

        # Use FullText mode to get the complete legislation text
        full_text_url = leg_url
        if "?" in full_text_url:
            full_text_url += "&FullText=1"
        else:
            full_text_url += "?FullText=1"

        try:
            req = _ur.Request(
                full_text_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            with _ur.urlopen(req, timeout=15) as resp:
                leg_html = resp.read().decode("utf-8", errors="replace")

            detail = parse_legislation_detail_from_html(leg_html)
            desc = detail.get("description", "")
            if desc:
                item["agenda_item_text"] = desc
        except Exception as e:
            log.debug(
                "Failed to fetch legislation detail for %s: %s",
                leg_url, e,
            )
            # Leave agenda_item_text empty on failure


async def fetch_agenda_items_async(
    meeting_detail_url: str, meeting_id: str, body_code: str = "mesa-city-council"
) -> list[dict]:
    """Fetch and parse agenda items from a MeetingDetail page via plain HTTP.

    Returns an empty list on 410/404 (meeting not available on Legistar anymore).

    Parameters
    ----------
    meeting_detail_url : str
        Full URL to the MeetingDetail.aspx page.
    meeting_id : str
        Legistar meeting ID.
    body_code : str
        Body code for the meeting.

    Returns
    -------
    list[dict]
        Agenda item dicts with ``agenda_item_text`` populated from each
        item's LegislationDetail.aspx page when available.
    """
    try:
        html = fetch_page(meeting_detail_url)
        items = parse_agenda_items_from_html(html, meeting_id, body_code)
        # Enrich with descriptions from legislation detail pages
        _enrich_agenda_item_descriptions(items)
        return items
    except Exception as e:
        err_str = str(e)
        if "410" in err_str or "404" in err_str or "Gone" in err_str:
            log.warning("Meeting detail page not available (410/404) for %s: %s", meeting_id, meeting_detail_url)
            return []
        raise

# ── Minutes PDF vote extraction ──

def fetch_minutes_pdf_bytes(minutes_url: str) -> Optional[bytes]:
    """Download a Mesa Minutes PDF from a View.ashx?M=M URL.

    Returns raw PDF bytes, or None on failure (including 410 Gone).
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            minutes_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Minutes PDF not available for %s: %s", minutes_url, e)
        return None


def extract_minutes_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from a Mesa Minutes PDF using pdftotext."""
    import subprocess
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        return None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.warning("pdftotext failed for Mesa minutes: %s", e)
        return None
    finally:
        import os
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


def parse_mesa_minutes_votes(text: str) -> dict:
    """Parse Mesa City Council vote data from Minutes PDF text.

    Mesa minutes follow a consistent format:

        It was moved by Councilmember [name], seconded by Councilmember [name],
        that [item description]
        Upon tabulation of votes, it showed:
        AYES \u2013 Name\u2013Name\u2013Name
        NAYS \u2013 Name
        Carried unanimously.

    Returns a dict with keys:
      - supervisors: list of dicts {name, normalized_name, present}
      - votes: list of dicts {agenda_item_number, ayes, nays, motion_result}
    """
    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_supervisors: set[str] = set()

    # Pattern for council member names: "FirstName LastName" or "FirstName Middle LastName"
    _NAME_RE = re.compile(r"^[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,2}\*?\s*$")

    _STOP_WORDS = {"regular", "moved", "upon", "items", "page", "mayor", "s",
                   "meeting", "adjournment", "introduction", "discuss",
                   "family", "study", "review", "update", "report",
                   "summary", "background", "project", "overview",
                   "consideration", "direction"}

    def _is_name(txt: str) -> Optional[str]:
        """Return cleaned name if *txt* looks like a council member name, else None."""
        # Split on 2+ spaces to handle PDF columnar layout
        parts = [p for p in re.split(r"\s{2,}", txt.strip("* \t").strip()) if p]
        if not parts:
            return None
        first = parts[0].strip("* \t").strip()
        if first.lower() in ("none",):
            return None
        if first.startswith("(") or first.startswith("*"):
            return None
        # Reject ALL-CAPS names (signature blocks, section headers)
        if first.upper() == first and len(first) > 3:
            return None
        if _NAME_RE.match(first):
            # Reject if any word is a stop word (section headers, not names)
            words = set(first.lower().split())
            if words & _STOP_WORDS:
                return None
            return first
        return None

    # Parse attendance from COUNCIL PRESENT / COUNCIL ABSENT sections
    # PDF headers often share a single line:
    #   COUNCIL PRESENT     COUNCIL ABSENT      OFFICERS PRESENT
    # We enter present mode on that line and let the section terminate
    # naturally when we hit a numbered agenda item (e.g. "1.") or
    # when the _is_name filter rejects lines below.
    current_section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()

        if "COUNCIL PRESENT" in upper:
            current_section = "present"
            continue
        if "COUNCIL ABSENT" in upper:
            current_section = "absent"
            continue
        if "OFFICERS PRESENT" in upper and "COUNCIL PRESENT" not in upper:
            current_section = None
            continue

        # Terminate sections when we hit a numbered agenda item or roll-call note
        if current_section and re.match(r'^\d+\.', stripped):
            current_section = None
            continue

        if current_section == "present":
            name = _is_name(stripped)
            if name:
                nkey = name.lower()
                if nkey not in seen_supervisors:
                    seen_supervisors.add(nkey)
                    supervisors.append({
                        "name": name,
                        "normalized_name": nkey,
                        "present": True,
                    })
        elif current_section == "absent":
            name = _is_name(stripped)
            if name:
                nkey = name.lower()
                if nkey not in seen_supervisors:
                    seen_supervisors.add(nkey)
                    supervisors.append({
                        "name": name,
                        "normalized_name": nkey,
                        "present": False,
                    })

    # Known Mesa council member last names for splitting merged names
    _KNOWN_LAST_NAMES = {
        "freeman", "somers", "adams", "duff", "goforth",
        "heredia", "taylor", "giles", "spilsbury",
        "woods", "thompson", "cook", "santos",
    }

    # Fuzzy-matching for corrupted PDF text
    from difflib import SequenceMatcher as _SM

    def _fuzzy_match_name(candidate: str) -> Optional[str]:
        """Try to fuzzy-match a garbled name fragment against known names."""
        lower = candidate.lower().strip()
        if not lower or lower in ("none", ""):
            return None
        # Exact or simple match
        if lower in _KNOWN_LAST_NAMES:
            return candidate
        # Try fuzzy matching
        best = None
        best_ratio = 0.5
        for known in _KNOWN_LAST_NAMES:
            ratio = _SM(None, lower, known).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = known
        if best:
            return best if best_ratio >= 0.7 else candidate
        return candidate

    def _extract_names_from_line(text: str) -> list[str]:
        """Extract council member names from an AYES/NAYS line.

        Handles:
        - Normal hyphen/en-dash separation
        - Corrupted tildes (Freema~oforth -> Freeman, Goforth)
        - Single-character corruption (Ouff -> Duff)
        - Missing names due to PDF artifacts
        """
        # Remove prefix
        clean = text.strip()
        for prefix in ("AYES", "NAYS", "AYES-", "NAYS-", "AYES–", "NAYS–"):
            if clean.upper().startswith(prefix.upper()):
                clean = clean[len(prefix):]
                break
        clean = clean.strip().strip("-\u2013\u2014 ").strip()

        # First pass: scan character by character looking for known names
        lower = clean.lower()
        sorted_known = sorted(_KNOWN_LAST_NAMES, key=len, reverse=True)

        result = []
        i = 0
        while i < len(lower):
            # Skip non-alphabetic chars
            if not lower[i].isalpha():
                i += 1
                continue
            # Try to match a known name starting at position i
            matched = None
            for known in sorted_known:
                if lower[i:i+len(known)] == known:
                    matched = known
                    break
            if matched:
                result.append(matched)
                i += len(matched)
                continue

            # No exact match at this position - try fuzzy or partial
            # Check if this character starts something close to a known name
            best = None
            best_len = 0
            for known in sorted_known:
                # Try matching first N chars of known name
                for n in range(len(known), 2, -1):
                    prefix = known[:n]
                    chunk = lower[i:i+n]
                    ratio = _SM(None, chunk, prefix).ratio()
                    if ratio >= 0.75:
                        if n > best_len:
                            best = known
                            best_len = n
                            break  # Take the longest good match
            if best:
                result.append(best)
                i += best_len
            else:
                i += 1  # Skip this character

        return [r for r in result if r.lower() not in ("none", "")]

    # Parse vote blocks
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("It was moved by"):
            # Collect description
            desc = line
            j = i + 1
            while j < len(lines) and "Upon tabulation" not in lines[j]:
                desc += " " + lines[j].strip()
                j += 1

            # Skip "Upon tabulation" line
            if j < len(lines) and "Upon tabulation" in lines[j]:
                j += 1

            # Skip blanks
            while j < len(lines) and not lines[j].strip():
                j += 1

            # AYES line
            ayes: list[str] = []
            if j < len(lines) and lines[j].strip().startswith("AYES"):
                ayes_text = lines[j].strip()
                ayes = _extract_names_from_line(ayes_text)
                j += 1

            # Skip blanks
            while j < len(lines) and not lines[j].strip():
                j += 1

            # NAYS line
            nays: list[str] = []
            if j < len(lines) and lines[j].strip().startswith("NAYS"):
                nays_text = lines[j].strip()
                nays = _extract_names_from_line(nays_text)
                j += 1

            # Result line
            result = ""
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                rline = lines[j].strip()
                if "unanimously" in rline.lower():
                    result = "Carried Unanimously"
                elif "carried" in rline.lower() or "Carried" in rline:
                    result = "Carried"
                elif "failed" in rline.lower():
                    result = "Failed"
                elif "denied" in rline.lower():
                    result = "Denied"
                elif "approved" in rline.lower():
                    result = "Approved"
                elif "adopted" in rline.lower():
                    result = "Adopted"
                else:
                    result = rline

            # Find the preceding agenda item number
            agenda_item_number = ""
            for k in range(i - 1, max(0, i - 30), -1):
                item_match = re.match(
                    r"^\**\s*(\d[\w-]*)\s*\.", lines[k].strip()
                )
                if item_match:
                    agenda_item_number = item_match.group(1).strip("*")
                    break
            # Fallback: try to derive from description
            if not agenda_item_number:
                # Consent agenda items are usually numbered 1
                if "consent agenda" in desc.lower():
                    agenda_item_number = "1"
                elif i > 0:
                    # Look even further back for item number
                    for k in range(i - 1, max(0, i - 100), -1):
                        item_match = re.match(
                            r"^\**\s*(\d[\w-]*)\s*\.", lines[k].strip()
                        )
                        if item_match:
                            agenda_item_number = item_match.group(1).strip("*")
                            break

            # Build individual supervisor_votes from ayes/nays lists
            # Use "yes"/"no" to match _detect_vote_attributes expectations
            # Map last-only names to full names from the attendance list
            supervisor_votes = []
            _seen_supervisor_names: set[str] = set()
            _name_map = {}
            for sup in supervisors:
                full = sup["name"]
                _name_map[full.lower()] = full
                parts = full.split()
                for p in parts:
                    if len(p) > 2:
                        _name_map[p.lower()] = full
            # Also add a hardcoded map of all known Mesa council member last names
            # so we don't create duplicate records even when attendance is missing
            _HARDCODED_NAME_MAP = {
                "freeman": "Mark Freeman", "somers": "Scott Somers",
                "adams": "Rich Adams", "duff": "Jennifer Duff",
                "goforth": "Alicia Goforth", "heredia": "Francisco Heredia",
                "taylor": "Dorean Taylor",
                "giles": "John Giles", "spilsbury": "Julie Spilsbury",
            }
            for ln, full in _HARDCODED_NAME_MAP.items():
                if ln not in _name_map:
                    _name_map[ln] = full
            for name in ayes:
                # Filter out non-name artifacts
                clean = name.strip("* \t~.")
                if not clean or clean.lower() in ("none", ""):
                    continue
                if any(kw in clean.lower() for kw in ["family home", "memorial", "committee"]):
                    continue
                if clean.upper() == clean and len(clean) > 3 and len(clean.split()) == 1:
                    continue
                full_name = _name_map.get(clean.lower(), clean)
                # Deduplicate
                norm_key = full_name.lower()
                if norm_key in _seen_supervisor_names:
                    continue
                _seen_supervisor_names.add(norm_key)
                supervisor_votes.append({
                    "name": full_name,
                    "vote": "yes",
                    "raw_vote_text": name,
                })
            for name in nays:
                clean = name.strip("* \t~.")
                if not clean or clean.lower() in ("none", ""):
                    continue
                if any(kw in clean.lower() for kw in ["family home", "memorial", "committee"]):
                    continue
                if clean.upper() == clean and len(clean) > 3 and len(clean.split()) == 1:
                    continue
                full_name = _name_map.get(clean.lower(), clean)
                norm_key = full_name.lower()
                if norm_key in _seen_supervisor_names:
                    continue
                _seen_supervisor_names.add(norm_key)
                supervisor_votes.append({
                    "name": full_name,
                    "vote": "no",
                    "raw_vote_text": name,
                })

            votes.append({
                "agenda_item_number": agenda_item_number,
                "ayes": ayes,
                "nays": nays,
                "motion_result": result,
                "vote_text": f"Ayes: {', '.join(ayes)}; Nays: {', '.join(nays) if nays else 'None'}",
                "supervisor_votes": supervisor_votes,
            })

            i = j
        else:
            i += 1

    return {
        "supervisors": supervisors,
        "votes": votes,
    }
