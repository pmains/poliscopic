"""
Gilbert Planning Commission / Board of Adjustment scraper.

Sources draft and action minutes from the CivicPlus Document Folder
(gilbertaz.gov), bypassing Akamai via a Node.js helper.

The city's Planning Commission, Board of Adjustment, and Zoning Hearing
Officer-Variance all share one folder (folder 654).  Once draft minutes
are posted, the corresponding agenda is removed — so we pull minutes
rather than live agenda items.

Usage:
    python -m scripts.scraper.gilbert_planning [--limit N]
    python scrape_agendas.py gilbert-planning --sync [--start-date=...] [--end-date=...]
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

PUBLIC_BODY_SLUG = "gilbert-planning-commission"
PUBLIC_BODY_CODE = "gilbert-pc"
PUBLIC_BODY_NAME = "Gilbert Planning Commission"

FOLDER_URL = (
    "https://www.gilbertaz.gov/departments/clerk-s-office/"
    "draft-final-meeting-minutes/draft-minutes-and-meeting-actions/-folder-654"
)

# How many documents per page in the folder listing
ITEMS_PER_PAGE = 20

# ── Paths ────────────────────────────────────────────────────────────────────

_HELPER = Path(__file__).parent / "gilbert_planning_helper.mjs"
_PROJECT_ROOT = _HELPER.parent.parent

# ── Date parsing ─────────────────────────────────────────────────────────────

# Titles look like:  "6-3-26 Planning Commission Meeting ACTION Minutes"
#                     "5-6-26 Planning Commission Meeting DRAFT Minutes"
#                     "6-19-25 Variance Hearing DRAFT Minutes"
_TITLE_DATE_RE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{2})\s+")

# Minutes type from description
_ACTION_RE = re.compile(r"ACTION", re.I)
_DRAFT_RE = re.compile(r"DRAFT", re.I)


def _parse_date_from_title(title: str) -> str:
    """Extract YYYY-MM-DD from a document title like '6-3-26 ...'."""
    m = _TITLE_DATE_RE.search(title)
    if not m:
        return ""
    month, day, short_year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = 2000 + short_year if short_year < 50 else 1900 + short_year
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _minutes_type_from_title(title: str) -> str:
    """Return 'action', 'draft', or 'minutes'."""
    if _ACTION_RE.search(title):
        return "action"
    elif _DRAFT_RE.search(title):
        return "draft"
    return "minutes"


def _meeting_id_from_title(title: str, date_str: str) -> str:
    """Build a unique meeting ID from the date."""
    if date_str:
        return f"gilbert-pc-{date_str}"
    return f"gilbert-pc-{abs(hash(title)) % 10**6}"


# ── Akamai-bypass helpers ────────────────────────────────────────────────────

def _node_fetch(url: str) -> Optional[str]:
    """Fetch HTML via Node.js helper (bypasses Akamai)."""
    try:
        result = subprocess.run(
            ["node", str(_HELPER), "fetch", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("Helper fetch failed (exit %d): %s",
                        result.returncode, result.stderr.strip())
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        log.warning("Helper fetch timed out: %s", url)
        return None
    except FileNotFoundError:
        log.error("Node.js not found; cannot bypass Akamai for Gilbert PC")
        return None


def _node_download(url: str, output_path: str) -> bool:
    """Download a file via Node.js helper."""
    try:
        result = subprocess.run(
            ["node", str(_HELPER), "download", url, output_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning("Helper download failed (exit %d): %s",
                        result.returncode, result.stderr.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        log.warning("Helper download timed out: %s", url)
        return False
    except FileNotFoundError:
        log.error("Node.js not found")
        return False


# ── Folder page fetching ─────────────────────────────────────────────────────

def _fetch_documents_from_page(page_url: str) -> list[dict]:
    """Fetch a single folder page and extract document metadata."""
    output = _node_fetch(page_url)
    if not output:
        return []

    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        log.warning("Failed to parse helper output: %s", e)
        return []

    return data.get("documents", [])


def search_documents(start_date: str = "", end_date: str = "",
                     limit: int = 0) -> list[dict]:
    """Fetch all Planning Commission documents from the folder.

    Paginates through all pages and returns documents sorted by date
    (newest first).  Optionally filters by date range.

    Each result dict:
        doc_id          unique document reference
        meeting_id      stable meeting identifier
        meeting_date    YYYY-MM-DD
        meeting_type    minutes type (draft/action/minutes)
        body_slug       public body slug
        body_code       public body code
        minutes_title   human-readable title
        minutes_url     full PDF download URL
    """
    all_docs: list[dict] = []
    page = 1

    while True:
        page_url = FOLDER_URL if page == 1 else f"{FOLDER_URL}/-npage-{page}"
        docs = _fetch_documents_from_page(page_url)

        if not docs:
            break  # no more pages or fetch failed

        for d in docs:
            title = d.get("title", "")
            pdf_url = d.get("url", "")
            meeting_date = _parse_date_from_title(title)
            minutes_type = _minutes_type_from_title(title)
            meeting_id = _meeting_id_from_title(title, meeting_date)

            all_docs.append({
                "doc_id": pdf_url.split("/")[-2] if "/" in pdf_url else pdf_url,
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": minutes_type,
                "body_slug": PUBLIC_BODY_SLUG,
                "body_code": PUBLIC_BODY_CODE,
                "body_name": PUBLIC_BODY_NAME,
                "minutes_title": title,
                "minutes_url": pdf_url,
            })

        # Stop if this is the last page
        if len(docs) < ITEMS_PER_PAGE:
            break

        page += 1
        if limit and len(all_docs) >= limit:
            break

    # Sort by date, newest first
    all_docs.sort(key=lambda d: d["meeting_date"], reverse=True)

    # Filter by date range
    if start_date:
        all_docs = [d for d in all_docs if d["meeting_date"] >= start_date]
    if end_date:
        all_docs = [d for d in all_docs if d["meeting_date"] <= end_date]

    if limit:
        all_docs = all_docs[:limit]

    return all_docs


# ── PDF extraction ───────────────────────────────────────────────────────────

_MINUTES_TEXT_LINE_RE = re.compile(r"^\s*(\d+)\.\s+(.*)")


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from a PDF using pdftotext."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            p = f.name
        r = subprocess.run(
            ["pdftotext", "-layout", p, "-"],
            capture_output=True, text=True, timeout=30,
        )
        Path(p).unlink(missing_ok=True)
        return r.stdout.strip() if r.stdout.strip() else None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed: %s", e)
        return None


def download_pdf(url: str) -> Optional[bytes]:
    """Download a PDF via the Node.js helper."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp_path = f.name
        ok = _node_download(url, tmp_path)
        if not ok:
            Path(tmp_path).unlink(missing_ok=True)
            return None
        with open(tmp_path, "rb") as f:
            data = f.read()
        Path(tmp_path).unlink(missing_ok=True)
        return data
    except Exception as e:
        log.warning("Failed to download PDF %s: %s", url, e)
        return None


# ── Sync entry point ─────────────────────────────────────────────────────────

def fetch_meetings(
    start_date: str = "",
    end_date: str = "",
    limit: int = 0,
) -> list[dict]:
    """Fetch documents and return records ready for meeting persistence."""
    docs = search_documents(start_date=start_date, end_date=end_date, limit=limit)
    log.info("Found %d Gilbert PC document(s)", len(docs))

    # Group documents by meeting_id (there can be draft + action minutes
    # for the same meeting)
    meetings: dict[str, dict] = {}
    for d in docs:
        mid = d["meeting_id"]
        if mid not in meetings:
            meetings[mid] = {
                "meeting_id": d["meeting_id"],
                "meeting_date": d["meeting_date"],
                "meeting_type": "Regular Meeting",
                "meeting_title": d["body_name"],
                "body_slug": d["body_slug"],
                "body_code": d["body_code"],
                "minutes_url": d["minutes_url"],
                "minutes_title": d["minutes_title"],
                "documents": [],
            }
        else:
            # Prefer action minutes URL over draft for the primary link
            if d["meeting_type"] == "action":
                meetings[mid]["minutes_url"] = d["minutes_url"]
            meetings[mid]["documents"].append(d)

    result = []
    for mid in sorted(meetings.keys(), reverse=True):
        m = meetings[mid]
        result.append(m)

    return result


def sync(args) -> int:
    """Fetch and persist Gilbert Planning Commission meeting records."""
    from sqlalchemy import select

    from db import Meeting, get_session, init_db, replace_meeting_data_safe

    init_db()
    meetings = fetch_meetings(
        start_date=getattr(args, "start_date", None) or "",
        end_date=getattr(args, "end_date", None) or "",
        limit=getattr(args, "limit", 0) or 0,
    )
    if not meetings:
        print("No Gilbert Planning Commission meetings found.")
        return 0

    session = get_session()
    total = 0
    try:
        for meeting_data in meetings:
            meeting_id = meeting_data["meeting_id"]
            meeting_date = meeting_data["meeting_date"]
            body_code = meeting_data["body_code"]
            minutes_url = meeting_data.get("minutes_url", "")
            if not meeting_date:
                continue

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
                continue

            meeting = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_data.get(
                    "meeting_type", "Regular Meeting"
                ),
                "meeting_title": meeting_data.get(
                    "meeting_title", PUBLIC_BODY_NAME
                ),
                "minutes_url": minutes_url,
                "source_url": minutes_url or "",
            }
            try:
                replace_meeting_data_safe(
                    session,
                    body_code,
                    meeting_id,
                    meeting,
                    [],
                )
                total += 1
                log.info(
                    "%s %s %s minutes=%s",
                    datetime.now().strftime("%H:%M:%S"),
                    meeting_date,
                    meeting_id[:35],
                    minutes_url[:50],
                )
            except Exception as exc:
                log.debug(
                    "Failed to sync Gilbert PC meeting %s: %s",
                    meeting_id,
                    exc,
                )
    finally:
        session.close()

    log.info(
        "%s Synced %d Gilbert Planning Commission meetings",
        datetime.now().strftime("%H:%M:%S"),
        total,
    )
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Gilbert PC scraper")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    docs = fetch_meetings(
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
    )
    print(f"Found {len(docs)} Gilbert PC meeting(s) with minutes")
    for d in docs:
        print(f"  {d['meeting_date']} {d['meeting_id'][:30]} minutes_url={d['minutes_url'][:50]}")
