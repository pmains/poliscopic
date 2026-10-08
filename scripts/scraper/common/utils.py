#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import html
import io
import logging
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional


log = logging.getLogger("maricopa")


def setup_logger():
    """Configure logging to stdout with timestamps and immediate flushing."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    handler.flush = sys.stdout.flush
    log.addHandler(handler)
    log.setLevel(logging.INFO)

SOURCE_PAGE = "https://www.maricopa.gov/324/Board-of-Supervisors-Meeting-Information"
SEARCH_BASE = "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/Search"
REQUIRED_BODY = re.compile(r"Board of Supervisors", re.I)
REQUIRED_TYPES = re.compile(r"(Formal|Informal)", re.I)

ROOT = Path.cwd()
AGENDAS_ROOT = ROOT / "data" / "agendas"
SUPPORT_ROOT = ROOT / "data" / "supporting-materials"
AGENDA_ITEMS_ROOT = ROOT / "data" / "agenda-items"
AGENDA_ITEMS_CSV = AGENDA_ITEMS_ROOT / "agenda_items.csv"
RAW_AGENDA_ITEMS_CSV = AGENDA_ITEMS_ROOT / "raw_agenda_items.csv"
REJECTED_RAW_BLOCKS_CSV = AGENDA_ITEMS_ROOT / "rejected_raw_blocks.csv"
DISCOVERY_CSV = ROOT / "data" / "discovery_metadata.csv"
LOGS_ROOT = ROOT / "logs"


def get_async_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for browser-backed scraping commands. "
            "Install the project scraping dependencies before running those commands."
        ) from exc
    return async_playwright


async def retry_with_backoff(
    coro_factory,
    max_attempts: int = 3,
    backoff_seconds: list[int] | None = None,
    label: str = "",
):
    """Execute an async call with retry and exponential backoff.

    coro_factory: a no-arg callable that returns a coroutine
    max_attempts: max retries (default 3)
    backoff_seconds: delays between attempts (default [1, 3, 10])
    """
    if backoff_seconds is None:
        backoff_seconds = [1, 3, 10]
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts:
                delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                jitter = random.uniform(0, 0.5 * delay)
                total_delay = delay + jitter
                label_text = f" [{label}]" if label else ""
                print(f"  Retry {attempt}/{max_attempts}{label_text}: {e} (waiting {total_delay:.1f}s)")
                await asyncio.sleep(total_delay)
    raise last_exc
CASE_PATTERN = re.compile(
    r"\b(CPA[A-Z]?\d+|Z\d{5,}|Z\d+-?\d*|GPA\d+|MCP\d+|SU\d+|PD\d+|TU\d+|[A-Z]{2,5}\d{5,}|RECONSIDERATION\s+OF\s+[A-Z]+\d+|SPLIT\s+CASE\s+[A-Z]+\d+)\b", re.I
)


PZ_SEARCH_BASE = "https://www.maricopa.gov/AgendaCenter/Search/"
PZ_AGENDA_BASE = "https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/"


C_NUMBER_PATTERN = re.compile(
    r"\(?(C-\d{2}-\d{2}-\d{3}(?:-[A-Z0-9]{1,3}){1,3})\)?"
)


def _extract_c_number(text: str) -> str:
    """Extract the first C-number from item text.

    Pattern: (C-XX-XX-XXX-XXX-XX) or C-XX-XX-XXX-XXX-XX
    Returns the number without parentheses, or empty string.
    """
    if not text:
        return ""
    m = C_NUMBER_PATTERN.search(text)
    if m:
        return m.group(1)
    return ""


def parse_c_number_parts(c_number: str) -> dict[str, str]:
    """Split a C-number into base and revision parts.

    C-86-25-040-X-00 => base=C-86-25-040-X, revision=00
    C-06-25-199-02   => base=C-06-25-199,   revision=02
    """
    if not c_number:
        return {"c_number": "", "c_number_base": "", "c_number_revision": ""}
    # The revision is the last dash-separated segment
    last_dash = c_number.rfind("-")
    if last_dash < 0:
        return {"c_number": c_number, "c_number_base": c_number, "c_number_revision": ""}
    revision = c_number[last_dash + 1:]
    base = c_number[:last_dash]
    # Verify revision looks like a revision code (alphanumeric, 1-3 chars)
    if len(revision) <= 4:
        return {"c_number": c_number, "c_number_base": base, "c_number_revision": revision}
    # If the last segment is too long to be a revision, treat the whole thing as base
    return {"c_number": c_number, "c_number_base": c_number, "c_number_revision": ""}
def parse_metadata_from_page_data(page_data: dict) -> dict:
    """Parse meeting metadata from page text data.

    Pure function counterpart to extract_meeting_metadata_from_page.
    Takes the dict from page.evaluate and returns parsed metadata.
    """
    body = page_data.get("bodyText", "")
    result = {
        "meeting_date": "",
        "meeting_type": "",
        "meeting_title": "",
    }

    # Title: use header or formTitle
    result["meeting_title"] = (page_data.get("formTitle") or page_data.get("headerText") or "").strip()

    # Date: the meeting header is authoritative. Agenda bodies routinely contain
    # later dates (appointment terms, contract expirations, hearing deadlines), so
    # taking the first date from the whole page can silently move the meeting.
    # Keep body text only as a compatibility fallback for pages whose header omits
    # the date entirely.
    for date_source in (
        page_data.get("headerText", ""),
        page_data.get("formTitle", ""),
        body,
    ):
        date_m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_source or "")
        if date_m:
            result["meeting_date"] = (
                f"{date_m.group(3)}-{int(date_m.group(1)):02d}"
                f"-{int(date_m.group(2)):02d}"
            )
            break

    # Type: look for Formal Meeting / Informal Meeting in body
    type_m = re.search(r"\b(FORMAL|INFORMAL)\s+MEETING\b", body, re.I)
    if type_m:
        result["meeting_type"] = type_m.group(1).capitalize() + " Meeting"

    return result


async def extract_meeting_metadata_from_page(page, source_url: str) -> dict:
    """Extract meeting date, type, and title from the loaded agenda page.

    Grabs page body text via Playwright, then delegates to
    parse_metadata_from_page_data for structured parsing.
    """
    try:
        data = await page.evaluate(
            """() => {
                return {
                    bodyText: document.body.textContent || '',
                    headerText: (
                        document.querySelector('.view-header-area') ||
                        document.querySelector('.page-header') ||
                        document.querySelector('.meeting-header') ||
                        document.querySelector('h1') ||
                        document.querySelector('h2') ||
                        { textContent: '' }
                    ).textContent.trim(),
                    formTitle: (
                        document.getElementById('formTitle') ||
                        { textContent: '' }
                    ).textContent.trim(),
                };
            }"""
        )
    except Exception:
        return {"meeting_date": "", "meeting_type": "", "meeting_title": ""}

    return parse_metadata_from_page_data(data)


async def is_image_based_agenda(page) -> bool:
    """Check if the agenda page is image-based (scanned) and unparseable.

    Looks for:
    - Zero interactive links (href=\"#\" in #agendaView)
    - Zero numbered table items matching the agenda pattern
    - Significant base64 image data as primary content
    - Zero lnkAgendaItem_TOC links with numeric patterns
    """
    try:
        data = await page.evaluate(
            """() => {
                const av = document.getElementById('agendaView');
                if (!av) return { interactive: 0, tables: 0, images: 0, hasNumberedItems: false };
                const interactive = av.querySelectorAll('a[href="#"]').length;
                const tables = av.querySelectorAll('table').length;
                const images = av.querySelectorAll('img').length;
                const text = av.textContent || '';
                // Check for numbered item patterns like "1. " or "1)"
                const hasNumberedItems = /\\b\\d+\\.\\s+|\\b\\d+\\)\\s+/.test(text);
                // Check for image-based content (data URI images, large image content)
                const hasDataUriImages = av.innerHTML.includes('data:image');
                return { interactive, tables, images, hasNumberedItems, hasDataUriImages, textLen: text.length };
            }"""
        )
        # Image-based: no interactive links, no numbered items, has data URI images or many images + little text
        if data['interactive'] == 0 and not data['hasNumberedItems'] and data['images'] > 0:
            return True
        if data['interactive'] == 0 and not data['hasNumberedItems'] and data.get('hasDataUriImages'):
            return True
        return False
    except Exception:
        # If the evaluation fails, assume it's not image-based
        return False


async def get_page_state_summary(page) -> str:
    """Grab the page title and a short body snippet for diagnostic logging.

    Returns a one-line string like:
        'title="Meeting 4627" body_snippet="Loading... (first 150 chars)"'
    Returns empty string if page evaluation fails.
    """
    try:
        info = await page.evaluate(
            """() => {
                const title = document.title || '';
                const body = document.body ? document.body.textContent || '' : '';
                const snippet = body.replace(/\\s+/g, ' ').trim();
                return { title, snippet: snippet.slice(0, 200) };
            }"""
        )
        if info and info.get("title"):
            return f'title="{info["title"]}" body_snippet="{info.get("snippet", "")[:150]}"'
        return f'body_snippet="{info.get("snippet", "(no page body)")[:150]}"'
    except Exception:
        return "(page state unavailable)"


async def extract_agenda_items_for_meeting(page, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []
    # Wait for the page to be fully loaded and all network activity to
    # settle (including the AJAX call that populates the agenda content).
    # Using networkidle ensures we don't grab a stale JS scaffold page.
    await page.goto(source_url, wait_until="networkidle", timeout=60000)
    html = await page.content()
    normalized_meeting = {
        "meeting_id": (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting",
        "meeting_date": (meeting.get("record_date") or meeting.get("meeting_date") or "").strip(),
        "meeting_type": (meeting.get("meeting_type") or "").strip(),
    }
    from scraper.common.agenda_items import parse_agenda_items_from_html
    return parse_agenda_items_from_html(html, source_url, normalized_meeting)

