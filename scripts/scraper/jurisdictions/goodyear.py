"""
City of Goodyear meeting and agenda extraction via AgendaQuick (Destiny Software).

Goodyear uses the AgendaQuick platform at ``public.destinyhosted.com`` with
organization ID 46639.  This is the same platform as Chandler (ID 24263).
"""
from __future__ import annotations

import logging
import re
import ssl
import urllib.parse
from typing import Optional

import html as html_mod
from scraper.common.io_utils import _normalize_text_date

log = logging.getLogger(__name__)

PUBLIC_BODY_CODE = "goodyear-cc"
SOURCE_SYSTEM = "agendaquick"
SOURCE_INSTANCE_URL = "https://public.destinyhosted.com"

BASE_URL = "https://public.destinyhosted.com"
GOODYEAR_ID = "46639"
AGENDA_BASE_URL = f"{BASE_URL}/{GOODYEAR_ID}/agenda/"

# BODY_MAP: meeting name keyword → (slug, body_code)
BODY_MAP: dict[str, tuple[str, str]] = {
    "city council regular meeting": ("goodyear-city-council", "goodyear-cc"),
    "city council work session": ("goodyear-city-council", "goodyear-cc"),
    "city council": ("goodyear-city-council", "goodyear-cc"),
    "planning and zoning commission": ("goodyear-planning-zoning-commission", "goodyear-pz"),
    "planning & zoning commission": ("goodyear-planning-zoning-commission", "goodyear-pz"),
    "planning & zoning": ("goodyear-planning-zoning-commission", "goodyear-pz"),
    "arts and culture commission": ("goodyear-arts-culture-commission", "goodyear-acc"),
    "arts & culture commission": ("goodyear-arts-culture-commission", "goodyear-acc"),
    "youth commission": ("goodyear-youth-commission", "goodyear-yc"),
    "citizen water advisory committee": ("goodyear-water-advisory", "goodyear-wac"),
    "citizens water advisory committee": ("goodyear-water-advisory", "goodyear-wac"),
    "water advisory committee": ("goodyear-water-advisory", "goodyear-wac"),
    "fire psprb": ("goodyear-fire-psprs", "goodyear-psprs-f"),
    "police psprb": ("goodyear-police-psprs", "goodyear-psprs-p"),
    "joint psprb": ("goodyear-joint-psprs", "goodyear-psprs-j"),
    "psprb": ("goodyear-psprs", "goodyear-psprs"),
    "audit committee": ("goodyear-audit-committee", "goodyear-audit"),
    "notice of quorum": ("goodyear-notice-of-quorum", "goodyear-quorum"),
    "notice quorum": ("goodyear-notice-of-quorum", "goodyear-quorum"),

    # Missing bodies discovered June 2026
    "industrial development authority": ("goodyear-ida", "goodyear-ida"),
    "ida": ("goodyear-ida", "goodyear-ida"),
    "parks & recreation advisory commission": ("goodyear-parks", "goodyear-parks"),
    "parks and recreation": ("goodyear-parks", "goodyear-parks"),
    "board of adjustment": ("goodyear-boa", "goodyear-boa"),
    "community facilities district": ("goodyear-cfd", "goodyear-cfd"),
    "joint community facilities district": ("goodyear-cfd", "goodyear-cfd"),
    "self-insured healthcare trust": ("goodyear-healthcare-trust", "goodyear-healthcare"),
    "volunteer & reserve firefighter": ("goodyear-firefighter-retirement", "goodyear-fr"),
    "public art subcommittee": ("goodyear-public-art", "goodyear-public-art"),
}

DEFAULT_BODY_SLUGS = [
    "goodyear-city-council",
    "goodyear-planning-zoning-commission",
    "goodyear-arts-culture-commission",
    "goodyear-youth-commission",
    "goodyear-water-advisory",
    "goodyear-psprs",
    "goodyear-audit-committee",
    "goodyear-notice-of-quorum",
    "goodyear-ida",
    "goodyear-parks",
    "goodyear-boa",
    "goodyear-cfd",
    "goodyear-healthcare-trust",
    "goodyear-firefighter-retirement",
    "goodyear-public-art",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _resolve_body(body_name: str) -> tuple[str, str]:
    """Match a meeting body name from the AgendaQuick listing to our slug and code."""
    key = body_name.lower().strip()
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "goodyear-city-council", "goodyear-cc"


def extract_meeting_type(body_name: str) -> str:
    """Extract the meeting type from the body_name."""
    tl = body_name.lower()
    if "cancellation" in tl or "canceled" in tl or "cancelled" in tl:
        return "Cancelled"
    if "quorum notice" in tl or "quorum notices" in tl:
        return "Quorum Notice"
    if "study session" in tl:
        return "Study Session"
    if "work session" in tl:
        return "Work Session"
    if "executive session" in tl:
        return "Executive Session"
    if "special meeting" in tl:
        return "Special Meeting"
    if "retreat" in tl:
        return "Retreat"
    if "regular meeting" in tl:
        return "Regular Meeting"
    if "regular" in tl:
        return "Regular Meeting"
    if "amended" in tl:
        return "Regular Meeting"
    return "Regular Meeting"


def search_goodyear_meetings(year: int, body_slugs: Optional[list[str]] = None) -> list[dict]:
    """Search Goodyear meetings for a given year using AgendaQuick month-by-month.

    Returns a list of meeting dicts with keys:
      - meeting_id       : Destiny seq number (string)
      - meeting_date     : YYYY-MM-DD
      - meeting_type     : e.g. "Regular Meeting", "Work Session"
      - body_name        : Full meeting title from listing
      - body_slug        : Normalized body slug
      - body_code        : Short body code (e.g. goodyear-cc)
      - agenda_url       : URL to the agenda HTML page
      - minutes_url      : URL to minutes page
      - video_url        : URL to video recording
      - canceled         : True/False
    """
    import urllib.request
    import time

    if body_slugs is None:
        body_slugs = DEFAULT_BODY_SLUGS

    meetings: list[dict] = []
    seen_seqs: set[str] = set()

    for month in range(1, 13):
        # AgendaQuick's 2026 UI moved from agenda_publish.cfm to monthly
        # ``default.cfm`` cards.  The old endpoint silently returns an
        # unrelated page, which made the scraper report zero meetings.
        url = f"{AGENDA_BASE_URL}default.cfm?mt=ALL&month={month}&year={year}"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            time.sleep(0.5)
            try:
                import certifi
                tls_context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                tls_context = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=20, context=tls_context) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.warning(f"Failed to fetch Goodyear {year}-{month:02d}: {e}")
            continue

        month_meetings = _parse_month_page(html, year, month)
        for m in month_meetings:
            seq = m["meeting_id"]
            if seq not in seen_seqs:
                seen_seqs.add(seq)
                # Filter by body_slug if specified
                if body_slugs is None or m["body_slug"] in body_slugs:
                    meetings.append(m)

    log.info(f"Found {len(meetings)} Goodyear meeting(s) for year {year}")
    return meetings


def _parse_month_page(html: str, year: int, month: int) -> list[dict]:
    """Parse a single month's AgendaQuick view into meeting dicts."""
    import re as _re

    # Current AgendaQuick markup uses one bordered card per meeting instead of
    # the legacy table/agenda_publish layout.
    if 'class="p-2 border-top"' in html and 'minutes.cfm?meetingId=' in html:
        meetings = []
        blocks = _re.split(r'<div class="p-2 border-top">', html)[1:]
        for block in blocks:
            heading = _re.search(r'<h3[^>]*>(.*?)</h3>', block, _re.I | _re.S)
            agenda = _re.search(r'href="([^"]*agenda\.cfm\?seq=(\d+)[^"]*)"', block, _re.I)
            if not heading or not agenda:
                continue
            heading_text = html_mod.unescape(_re.sub(r'<[^>]+>', '', heading.group(1))).strip()
            parsed = _re.match(r'([A-Za-z]+\s+\d{1,2},\s*\d{4}):\s*(.+)', heading_text)
            if not parsed:
                continue
            meeting_date = _normalize_text_date(parsed.group(1)) or ""
            title = parsed.group(2).strip()
            slug, code = _resolve_body(title)
            if title.lower().startswith(('cancelled ', 'canceled ')):
                meeting_type = "Cancelled"
                canceled = True
            else:
                meeting_type = extract_meeting_type(title)
                canceled = False
            minutes = _re.search(r'href="([^"]*minutes\.cfm\?meetingId=\d+[^"]*)"', block, _re.I)
            video = _re.search(r'href="([^"]*open\.media[^"]*)"', block, _re.I)
            meetings.append({
                "meeting_id": agenda.group(2),
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": title,
                "body_name": title,
                "body_slug": slug,
                "body_code": code,
                "agenda_url": urllib.parse.urljoin(AGENDA_BASE_URL, html_mod.unescape(agenda.group(1))),
                "minutes_url": urllib.parse.urljoin(AGENDA_BASE_URL, html_mod.unescape(minutes.group(1))) if minutes else "",
                "video_url": html_mod.unescape(video.group(1)) if video else "",
                "canceled": canceled,
            })
        return meetings

    meetings: list[dict] = []
    current_date = ""

    # The AgendaQuick page has a simple structure:
    # [May 26, 2026](/agenda_publish.cfm?...&seq=2377)
    # Notice of Quorum
    #
    # [May 20, 2026](/agenda_publish.cfm?...&seq=2375)
    # Citizen Water Advisory Committee
    #
    # First find all seq links with their dates, then the next non-empty
    # text after each link is the meeting name.

    lines = html.split("\n")
    pending_seq = None  # (url, seq, link_text)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Check for date in text (e.g., "May 26, 2026")
        date_match = _re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s*(\d{4})?',
            stripped,
        )
        if date_match:
            month_name = date_match.group(1)
            day = int(date_match.group(2))
            yr = int(date_match.group(3)) if date_match.group(3) else year
            current_date = f"{yr:04d}-{_month_number(month_name):02d}-{day:02d}"

        # Find meeting seq links (the date is the link text)
        seq_match = _re.search(
            r'href="([^"]*seq=(\d+))"[^>]*>([^<]*)</a>',
            line, _re.I
        )
        if seq_match:
            href = seq_match.group(1)
            seq = seq_match.group(2)
            link_text = seq_match.group(3).strip()
            full_url = urllib.parse.urljoin(BASE_URL, href)

            # The meeting name is usually on this line after the </a> or on the next line
            # Check if there's text after the </a> on the same line
            after_tag = line.split('</a>', 1)[1].strip() if '</a>' in line else ''

            if after_tag and after_tag != '&nbsp;' and not after_tag.startswith('&nbsp;'):
                # Meeting name is on the same line as the link
                title = _re.sub(r'<[^>]+>', '', after_tag).strip()
                if title:
                    meetings.append(_make_meeting(seq, href, full_url, title, current_date))
                    continue

            # Meeting name is on the next line (or within a few lines)
            pending_seq = (href, seq, link_text, current_date, i)

        elif pending_seq:
            href, seq, link_text, pdate, pline = pending_seq
            # Check if this line has a meeting name (no seq link)
            href, seq, link_text, pdate, pline = pending_seq

            # Skip lines that are just &amp;nbsp; or empty
            if stripped in ('&nbsp;', '', '&amp;nbsp;', '\u00a0'):
                continue

            # Check if this is a minutes/video link (starts with [Minutes] or [Video])
            if 'dsp=min' in stripped or 'open.media' in stripped or stripped.startswith('['):
                continue

            # Check if this is a CANCELLED notice
            cancel_match = _re.search(r'\*?\s*CANCELLED\s+(.+)', stripped, _re.I)
            if cancel_match:
                title = cancel_match.group(1).strip()
                slug, code = _resolve_body(title)
                meetings.append({
                    "meeting_id": f"{seq}",
                    "meeting_date": pdate,
                    "meeting_type": "Cancelled",
                    "meeting_title": f"CANCELLED {title}",
                    "body_name": title,
                    "body_slug": slug,
                    "body_code": code,
                    "agenda_url": "",
                    "minutes_url": "",
                    "video_url": "",
                    "canceled": True,
                })
                pending_seq = None
                continue

            # This line is the meeting name
            title = _re.sub(r'<[^>]+>', '', stripped).strip()
            # Decode HTML entities (e.g. &amp; → &)
            title = html_mod.unescape(title)
            # Clean up any "*" prefix (future meeting indicator)
            title = _re.sub(r'^\*\s*', '', title)
            if title:
                # Build agenda_url from href
                full_url = urllib.parse.urljoin(BASE_URL, html_mod.unescape(href)) if href else ''
                meetings.append(_make_meeting(seq, href, full_url, title, pdate))

            pending_seq = None

    return meetings


def _make_meeting(seq, href, full_url, title, date_str):
    """Create a meeting dict from parsed fields."""
    slug, code = _resolve_body(title)
    meeting_type = extract_meeting_type(title)

    if href and full_url:
        agenda_url = full_url
    else:
        agenda_url = ""

    return {
        "meeting_id": seq,
        "meeting_date": date_str,
        "meeting_type": meeting_type,
        "meeting_title": title,
        "body_name": title,
        "body_slug": slug,
        "body_code": code,
        "agenda_url": agenda_url,
        "minutes_url": "",
        "video_url": "",
        "canceled": False,
    }


def _month_number(name: str) -> int:
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    return months.get(name.lower().strip(), 1)


def fetch_page(url: str, timeout: int = 20) -> str:
    """Fetch an HTML page from the AgendaQuick server."""
    import urllib.request
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        try:
            import certifi
            tls_context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            tls_context = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=tls_context) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


# ── Agenda item parsing from detail page ──


def parse_agenda_items(html: str, meeting_id: str) -> list[dict]:
    """Parse agenda items from a Goodyear AgendaQuick meeting detail page.

    The HTML has items in the format:
        N.
        TITLE (ALL CAPS)
        RECOMMENDATION
        Description text

    Returns a list of item dicts with keys:
      - meeting_id
      - agenda_item_number
      - agenda_item_title
      - agenda_item_text
      - item_type (section or item)
      - sort_order
    """
    import re as _re
    items: list[dict] = []
    sort_order = 0

    # Strip HTML to plain text with line breaks
    text = html.replace('\xa0', ' ')
    text = _re.sub(r'<script[^>]*>.*?</script>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r'<style[^>]*>.*?</style>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r'<br\\s*/?>', '\n', text, flags=_re.IGNORECASE)
    text = _re.sub(r'<[^>]+>', '\n', text)
    text = _re.sub(r'&nbsp;', ' ', text)
    text = _re.sub(r'\n{3,}', '\n\n', text)

    # Clean up lines
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    full_text = '\n'.join(lines)

    # Known section headers
    section_keywords = [
        'CALL TO ORDER', 'ROLL CALL', 'PLEDGE OF ALLEGIANCE',
        'COMMUNICATIONS', 'PUBLIC COMMENTS', 'CONSENT',
        'PUBLIC HEARINGS', 'BUSINESS', 'INFORMATION ITEMS',
        'FUTURE MEETINGS', 'ADJOURNMENT',
    ]

    seen_numbers: set[str] = set()

    # Find item blocks: N. followed by content until next N.
    for m in _re.finditer(
        r'(?:^|\n)\s*(\d+)\.\s*\n(.*?)(?=\n\s*\d+\.\s*\n|$)',
        full_text, _re.DOTALL
    ):
        num = m.group(1).strip()
        block = m.group(2).strip()
        if num in seen_numbers:
            continue
        seen_numbers.add(num)

        # Split block into lines to extract title and recommendation
        block_lines = block.split('\n')
        title = ''
        rec_text = ''
        in_rec = False

        for bline in block_lines:
            bline = bline.strip()
            if not bline:
                continue
            if bline == 'RECOMMENDATION':
                in_rec = True
                continue
            if in_rec:
                rec_text = (rec_text + ' ' + bline).strip() if rec_text else bline
            elif not title:
                title = bline

        if not title:
            continue

        sort_order += 1

        # Check if this is a section header
        is_section = any(title.upper() == kw for kw in section_keywords)

        items.append({
            "meeting_id": meeting_id,
            "agenda_item_number": num,
            "agenda_item_title": title,
            "agenda_item_text": rec_text,
            "item_type": "section" if is_section else "item",
            "sort_order": sort_order,
        })

    return items


def main() -> None:
    """CLI entry point for testing."""
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s - %(name)s - %(message)s",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "meetings":
        year = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
        body = sys.argv[3] if len(sys.argv) > 3 else "goodyear-city-council"
        meetings = search_goodyear_meetings(year, body_slugs=[body])
        print(f"\nFound {len(meetings)} Goodyear meeting(s) for {year}:")
        for m in meetings:
            print(f"  {m['meeting_date']:12s} | {m['body_name']:50s} | seq={m['meeting_id']}")
        print()

    elif len(sys.argv) > 1 and sys.argv[1] == "items":
        meeting_seq = sys.argv[2]
        url = f"{BASE_URL}/agenda_publish.cfm?id={GOODYEAR_ID}&dsp=ag&seq={meeting_seq}"
        html = fetch_page(url)
        items = parse_agenda_items(html, meeting_seq)
        print(f"\nFound {len(items)} agenda items for seq={meeting_seq}:")
        for item in items:
            print(f"  {item['agenda_item_number']:4s} | {item['agenda_item_title'][:70]}")

    else:
        print("Usage:")
        print("  python -m scraper.goodyear meetings [year] [body_slug]")
        print("  python -m scraper.goodyear items <seq>")


if __name__ == "__main__":
    main()
