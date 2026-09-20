"""
City of Buckeye meeting extraction via Granicus ViewPublisher.

Buckeye uses the Granicus platform at ``buckeyeaz.granicus.com``.
All meeting materials (agendas, minutes, packets) are PDF-based.
Agenda items are extracted from the agenda packet PDFs via pdftotext.

Sources:
  https://buckeyeaz.granicus.com/ViewPublisher.php?view_id=1  (HTML meeting list, full history)
  https://buckeyeaz.granicus.com/ViewPublisherRSS.php?view_id=1  (RSS feeds)
  https://buckeyeaz.granicus.com/AgendaViewer.php?view_id=1&event_id=X  (PDF agenda page)
"""

from __future__ import annotations
import logging
import re
import subprocess
import tempfile
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from scraper.jurisdictions.buckeye_agenda_parse import (
    parse_agenda_blocks,
    parse_agenda_pdf_items,
)

log = logging.getLogger(__name__)

# ── Constants ──

PUBLIC_BODY_CODE = "buckeye-cc"
DEFAULT_BODY_SLUGS = ["buckeye-cc"]

BASE_URL = "https://buckeyeaz.granicus.com"
SOURCE_INSTANCE_URL = BASE_URL
SOURCE_SYSTEM = "granicus"

VIEW_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

_BODY_MAP: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"regular\s+council\s+meeting", re.I), "buckeye-city-council", "buckeye-cc", "Regular Council Meeting"),
    (re.compile(r"council\s+workshop", re.I), "buckeye-city-council", "buckeye-cc", "Council Workshop"),
    (re.compile(r"special\s+council\s+meeting\s+and\s+council\s+executive\s+session", re.I), "buckeye-city-council", "buckeye-cc", "Special Council & Executive Session"),
    (re.compile(r"special\s+council\s+meeting", re.I), "buckeye-city-council", "buckeye-cc", "Special Council Meeting"),
    (re.compile(r"regular\s+and\s+special\s+council\s+meeting", re.I), "buckeye-city-council", "buckeye-cc", "Regular & Special Council Meeting"),
    (re.compile(r"council\s+executive\s+session", re.I), "buckeye-city-council", "buckeye-cc", "Council Executive Session"),
    (re.compile(r"planning\s+and\s+zoning\s+commission", re.I), "buckeye-pz", "buckeye-pz", "Planning & Zoning Commission"),
    (re.compile(r"joint\s+community\s+facilities\s+districts?", re.I), "buckeye-cfd", "buckeye-cfd", "Joint Community Facilities Districts"),
    (re.compile(r"arts\s+and\s+culture\s+subcommittee", re.I), "buckeye-arts-culture", "buckeye-arts-culture", "Arts & Culture Subcommittee"),
    (re.compile(r"community\s+services\s+advisory\s+board", re.I), "buckeye-community-services", "buckeye-community-services", "Community Services Advisory Board"),
    (re.compile(r"buckeye\s+youth\s+council", re.I), "buckeye-youth", "buckeye-youth", "Youth Council"),
    (re.compile(r"public\s+safety\s+retirement\s+board\s*\(police\)", re.I), "buckeye-psprs-police", "buckeye-psprs-police", "Public Safety Retirement Board (Police)"),
    (re.compile(r"public\s+safety\s+retirement\s+board\s*\(fire\)", re.I), "buckeye-psprs-fire", "buckeye-psprs-fire", "Public Safety Retirement Board (Fire)"),
    (re.compile(r"public\s+safety\s+retirement", re.I), "buckeye-psprs", "buckeye-psprs", "Public Safety Retirement Board"),
    (re.compile(r"pollution\s+control", re.I), "buckeye-pollution-control", "buckeye-pollution-control", "Pollution Control Corporation"),
    (re.compile(r"airport\s+advisory", re.I), "buckeye-airport", "buckeye-airport", "Airport Advisory Board"),
    (re.compile(r"library\s+advisory", re.I), "buckeye-library", "buckeye-library", "Library Advisory Board"),
    (re.compile(r"citizen\s+water\s+and\s+wastewater", re.I), "buckeye-water-rate", "buckeye-water-rate", "Citizen Water & Wastewater Rate Committee"),
]

_DEFAULT_SLUG = "buckeye-city-council"
_DEFAULT_CODE = "buckeye-cc"


def resolve_body(title: str) -> tuple[str, str, str]:
    for pattern, slug, code, mtype in _BODY_MAP:
        if pattern.search(title):
            return slug, code, mtype
    return _DEFAULT_SLUG, _DEFAULT_CODE, title


def _parse_granicus_date(date_str: str) -> str:
    date_str = date_str.strip().replace("&nbsp;", " ")
    date_str = re.sub(r"\s+", " ", date_str)
    for fmt in ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def fetch_page(url: str, timeout: int = 30) -> str:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def fetch_bytes(url: str, timeout: int = 60) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Failed to download %s: %s", url, e)
        return None


# ── Meeting discovery ──

def search_buckeye_meetings_from_rss(max_items: int = 200) -> list[dict]:
    seen: set[str] = set()
    meetings: list[dict] = []

    for mode in ("agendas", "minutes"):
        url = f"{BASE_URL}/ViewPublisherRSS.php?view_id={VIEW_ID}&mode={mode}"
        try:
            rss_xml = fetch_page(url)
        except Exception:
            continue

        for item_xml in re.findall(r"<item>(.*?)</item>", rss_xml, re.DOTALL):
            title_m = re.search(r"<title>(.*?)</title>", item_xml)
            if not title_m:
                continue
            title = title_m.group(1).strip()

            meeting_name = title
            meeting_date = ""
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                meeting_name = parts[0].strip()
                try:
                    dt = datetime.strptime(parts[1].strip(), "%B %d, %Y")
                    meeting_date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    meeting_date = parts[1].strip()

            link_m = re.search(r"<link>(.*?)</link>", item_xml)
            link_url = link_m.group(1).strip() if link_m else ""

            event_id = ""
            agenda_url = ""
            minutes_url = link_url
            m = re.search(r"clip_id=(\d+)", link_url)
            if m:
                event_id = m.group(1)
                agenda_url = f"{BASE_URL}/AgendaViewer.php?view_id={VIEW_ID}&event_id={event_id}"

            slug, code, mtype = resolve_body(meeting_name)

            key = event_id or f"{meeting_date}-{meeting_name}"
            if key in seen:
                continue
            seen.add(key)

            meetings.append({
                "meeting_id": event_id or f"buckeye-{meeting_date}",
                "meeting_date": meeting_date,
                "meeting_type": mtype,
                "meeting_title": meeting_name,
                "body_slug": slug,
                "body_code": code,
                "event_id": event_id,
                "agenda_url": agenda_url,
                "minutes_url": minutes_url if mode == "minutes" else "",
                "packet_url": "",
                "source_url": url,
            })

    meetings.sort(key=lambda m: m["meeting_date"], reverse=True)
    return meetings[:max_items]


def search_buckeye_meetings_from_html(max_meetings: int = 500) -> list[dict]:
    url = f"{BASE_URL}/ViewPublisher.php?view_id={VIEW_ID}"
    html = fetch_page(url)
    meetings: list[dict] = []

    rows = re.findall(r'<tr\s+class="listingRow">(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        name_match = re.search(r'<td[^>]*headers="Name"[^>]*>(.*?)</td>', row, re.DOTALL)
        if not name_match:
            continue
        name = re.sub(r"<[^>]+>", " ", name_match.group(1)).strip()
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            continue

        date_match = re.search(r'<td[^>]*headers="[^"]*Date[^"]*"[^>]*>(.*?)</td>', row, re.DOTALL)
        date_raw = re.sub(r"<[^>]+>", " ", date_match.group(1)).strip() if date_match else ""
        meeting_date = _parse_granicus_date(date_raw)

        event_id = ""
        agenda_link = re.search(r'href="[^"]*event_id=(\d+)"', row)
        if agenda_link:
            event_id = agenda_link.group(1)
        if not event_id:
            continue

        agenda_url = f"{BASE_URL}/AgendaViewer.php?view_id={VIEW_ID}&event_id={event_id}"

        packet_url = ""
        pkt = re.search(r'href="(https://[^"]*\.pdf)"[^>]*>Agenda\s*Packet', row)
        if pkt:
            packet_url = pkt.group(1)

        minutes_url = ""
        min_m = re.search(r'href="(https://[^"]*minute[^"]*)"', row, re.IGNORECASE)
        if min_m:
            minutes_url = min_m.group(1)

        slug, code, mtype = resolve_body(name)

        meetings.append({
            "meeting_id": event_id,
            "meeting_date": meeting_date,
            "meeting_type": mtype,
            "meeting_title": name,
            "body_slug": slug,
            "body_code": code,
            "event_id": event_id,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "packet_url": packet_url,
            "source_url": url,
        })

    meetings.sort(key=lambda m: m["meeting_date"], reverse=True)
    return meetings[:max_meetings]


def search_buckeye_meetings(year=None, body_slugs=None, use_html=False, max_meetings=500):
    if use_html:
        meetings = search_buckeye_meetings_from_html(max_meetings=max_meetings)
    else:
        meetings = search_buckeye_meetings_from_rss(max_items=max_meetings)
    if year:
        year_str = str(year)
        meetings = [m for m in meetings if m["meeting_date"].startswith(year_str)]
    if body_slugs:
        meetings = [m for m in meetings if m["body_slug"] in body_slugs or m["body_code"] in body_slugs]
    return meetings


# ── Agenda item extraction from PDF ──

def fetch_pdf_bytes(url: str) -> Optional[bytes]:
    return fetch_bytes(url)


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(["pdftotext", pdf_path, "-"], capture_output=True, text=True, timeout=120)
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed: %s", e)
        return None
    finally:
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


def fetch_and_parse_agenda(meeting: dict) -> list[dict]:
    meeting_id = meeting["meeting_id"]
    packet_url = meeting.get("packet_url", "")
    if packet_url:
        pdf_bytes = fetch_pdf_bytes(packet_url)
        if pdf_bytes:
            text = extract_pdf_text(pdf_bytes)
            if text and len(text) > 100:
                items = parse_agenda_pdf_items(text, meeting_id)
                if items:
                    return items
    return []


def extract_agenda_pdf_url(agenda_viewer_url: str) -> Optional[str]:
    """Extract the S3 agenda PDF URL from a Granicus AgendaViewer page.

    The AgendaViewer page loads the agenda PDF via Google Docs viewer.
    The S3 URL is embedded in the page's JavaScript initialization.
    """
    try:
        html = fetch_page(agenda_viewer_url)
        m = re.search(r'buckeyeaz/([a-f0-9]{30,40})\.pdf', html, re.IGNORECASE)
        if m:
            s3_key = m.group(1)
            return (
                "https://granicus_production_attachments.s3.amazonaws.com"
                f"/buckeyeaz/{s3_key}.pdf"
            )
    except Exception as e:
        log.debug("extract_agenda_pdf_url failed for %s: %s", agenda_viewer_url, e)
    return None


def _download_pdf_with_ssl_bypass(url: str) -> Optional[bytes]:
    """Download a PDF, bypassing SSL verification for S3 hostname mismatches."""
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Failed to download with SSL bypass %s: %s", url, e)
        return None


def extract_supporting_docs(agenda_viewer_url: str) -> list[dict]:
    """Download the agenda PDF and extract hyperlinked supporting documents.

    Buckeye's Granicus agenda PDFs embed hyperlinks on each item description
    that link to individual item report PDFs on CloudFront. This function
    extracts those links and matches them to their agenda item numbers.

    Returns a list of dicts with keys:
      - agenda_item_number : item number e.g. "4.A"
      - document_title     : first ~120 chars of the linked item text
      - document_url       : the CloudFront PDF URL
      - document_type      : "Supporting Document"
    """
    if not agenda_viewer_url:
        return []

    pdf_url = extract_agenda_pdf_url(agenda_viewer_url)
    if not pdf_url:
        log.debug("No agenda PDF URL found from %s", agenda_viewer_url)
        return []

    pdf_bytes = _download_pdf_with_ssl_bypass(pdf_url)
    if not pdf_bytes:
        log.debug("Failed to download agenda PDF: %s", pdf_url)
        return []

    docs: list[dict] = []
    seen_urls: set[str] = set()

    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page_num in range(len(doc)):
            page = doc[page_num]
            links = page.get_links()
            for link in links:
                uri = link.get("uri", "")
                if not uri or not uri.endswith(".pdf"):
                    continue
                if uri in seen_urls:
                    continue
                seen_urls.add(uri)

                # Find text overlapping the link rectangle to determine
                # which agenda item this link belongs to.
                rect = link.get("from")
                linked_text = ""
                if rect:
                    blocks = page.get_text("blocks")
                    for b in blocks:
                        bx0, by0, bx1, by1 = b[0:4]
                        if (rect.x0 < bx1 and rect.x1 > bx0
                                and rect.y0 < by1 and rect.y1 > by0):
                            linked_text = b[4].strip()
                            break

                if not linked_text:
                    continue

                # Extract agenda item number from the beginning of linked text
                # e.g. "*4.A\nCouncil to take action..." or "5.A\nCouncil to hold..."
                item_num = ""
                lines = linked_text.split("\n")
                first_line = lines[0].strip() if lines else ""
                item_m = re.match(r"^\*?(\d+(?:\.\w)?)\b", first_line)
                if item_m:
                    item_num = item_m.group(1)

                # Build a clean title: skip the item number prefix, use the
                # rest of the first line plus the second line if available.
                title_parts = []
                for line in lines:
                    cleaned = re.sub(r"^\*?\d+(?:\.\w)?\s*", "", line, count=1).strip()
                    if cleaned:
                        title_parts.append(cleaned)
                title = " ".join(title_parts)[:200] if title_parts else first_line[:120]

                # Add the item report document
                docs.append({
                    "agenda_item_id": "0",
                    "agenda_item_number": item_num,
                    "document_title": title[:200],
                    "document_url": uri,
                    "document_type": "Item Report",
                    "file_extension": ".pdf",
                })

                # --- Second layer: extract attachments inside the item report PDF ---
                try:
                    attachment_bytes = _download_pdf_with_ssl_bypass(uri)
                    if attachment_bytes:
                        inner_doc = fitz.open(stream=attachment_bytes, filetype="pdf")
                        for inner_page_num in range(len(inner_doc)):
                            inner_page = inner_doc[inner_page_num]
                            inner_links = inner_page.get_links()
                            for il in inner_links:
                                iuri = il.get("uri", "")
                                if not iuri or not iuri.endswith(".pdf"):
                                    continue
                                if iuri in seen_urls:
                                    continue
                                seen_urls.add(iuri)

                                # Extract the attachment title from text near the link
                                irect = il.get("from")
                                att_title = ""
                                if irect:
                                    att_text = inner_page.get_text("text", clip=irect).strip()
                                    if att_text:
                                        att_title = att_text[:200]

                                docs.append({
                                    "agenda_item_id": "0",
                                    "agenda_item_number": item_num,
                                    "document_title": att_title or f"Attachment for Item {item_num}",
                                    "document_url": iuri,
                                    "document_type": "Attachment",
                                    "file_extension": ".pdf",
                                })
                        inner_doc.close()
                except Exception as ie:
                    log.debug("Failed to extract attachments from %s: %s", uri, ie)

        doc.close()
    except ImportError:
        log.warning("PyMuPDF (fitz) not available; cannot extract supporting docs")
    except Exception as e:
        log.warning("Failed to extract supporting docs from agenda PDF: %s", e)

    return docs
