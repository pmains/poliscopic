#!/usr/bin/env python3
"""
Legacy OnBase image OCR compatibility path.

New stored-document extraction must use ``scripts/docs/extract.py`` so retained
text and the ``document-layout/1.0`` artifact are produced together. This
script remains for the older OnBase page-image workflow and emits plain text;
it is not a replacement for the governed PDF extraction cascade.

These are meetings where the agenda is a scanned image embedded in the
OnBase page (not parseable HTML).  The pipeline:

  1. Render the page with Playwright (executes JS, captures base64 image)
  2. Decode the image and SCP it to the Windows machine (has Tesseract)
  3. Run Tesseract OCR via SSH over Tailscale
  4. Retrieve the OCR text
  5. Optionally persist as agenda items in the database

Usage:
    # Show OCR text only
    python scripts/scraper/common/ocr_pipeline.py bos 4657 --show

    # Persist agenda items
    python scripts/scraper/common/ocr_pipeline.py bos 4657 --persist

    # Via the main scraper
    python scripts/scrape_agendas.py bos --sync --meeting-id=4657 --ocr
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ocr")

# ── Windows SSH / Tesseract config ──────────────────────────────────
WINDOWS_HOST = os.environ.get("POLISCOPIC_DEV_SSH_HOST", "development-host")
WINDOWS_TEMP = os.environ.get("POLISCOPIC_OCR_TEMP", r"C:\ocr_temp")
TESSERACT_PATH = os.environ.get(
    "POLISCOPIC_TESSERACT_PATH", r'"C:\Program Files\Tesseract-OCR\tesseract.exe"')

# ── OnBase config ───────────────────────────────────────────────────
ONBASE_HOST = "mccobagenda.databankcloud.com"
ONBASE_BASE = f"https://{ONBASE_HOST}/AgendaOnline"


def _make_temp_name(mid: str) -> tuple[str, str, str]:
    """Return (img_path, txt_path) on Windows."""
    stamp = str(int(time.time()))
    return (
        f"{WINDOWS_TEMP}\\img_{mid}_{stamp}.png",
        f"{WINDOWS_TEMP}\\img_{mid}_{stamp}",
    )


def capture_image(mid: str) -> bytes:
    """Render the OnBase ViewMeeting page with Playwright and extract
    the embedded base64 image.  Returns the raw PNG bytes."""
    from playwright.sync_api import sync_playwright

    url = f"{ONBASE_BASE}/Meetings/ViewMeeting?id={mid}&doctype=1"
    log.info("Loading %s", url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1024})
        page.goto(url, wait_until="networkidle", timeout=45_000)
        page.wait_for_timeout(3000)

        imgs = page.query_selector_all("img")
        for img in imgs:
            src = img.get_attribute("src") or ""
            if "base64" in src:
                _, b64data = src.split("base64,", 1)
                browser.close()
                return base64.b64decode(b64data)

        # Fallback: full-page screenshot
        log.info("No base64 image found — taking screenshot")
        png = page.screenshot(full_page=True)
        browser.close()
        return png


def ocr_on_windows(img_bytes: bytes, mid: str) -> str:
    """SCP the image to Windows, run Tesseract, retrieve text."""
    win_img, win_base = _make_temp_name(mid)

    # Ensure temp dir exists
    subprocess.run(
        ["ssh", WINDOWS_HOST, f"if not exist {WINDOWS_TEMP} mkdir {WINDOWS_TEMP}"],
        capture_output=True,
        timeout=10,
    )

    # Write image to local temp
    local_img = Path(tempfile.gettempdir()) / f"ocr_{mid}.png"
    local_img.write_bytes(img_bytes)

    # SCP
    log.info("SCP image (%d bytes) to Windows…", len(img_bytes))
    subprocess.run(
        ["scp", str(local_img), f"{WINDOWS_HOST}:{win_img.replace(chr(92), '/')}"],
        check=True, timeout=30,
    )

    # Run Tesseract
    cmd = f"{TESSERACT_PATH} {win_img} {win_base} --psm 6"
    log.info("Running Tesseract…")
    subprocess.run(
        ["ssh", WINDOWS_HOST, cmd],
        capture_output=True, timeout=60,
    )

    # Retrieve text
    txt_path = win_base + ".txt"
    local_out = Path(tempfile.gettempdir()) / f"ocr_{mid}.txt"
    subprocess.run(
        ["scp", f"{WINDOWS_HOST}:{txt_path.replace(chr(92), '/')}", str(local_out)],
        capture_output=True, timeout=30,
    )

    text = local_out.read_text(encoding="utf-8", errors="replace")

    # Cleanup remote files
    subprocess.run(
        ["ssh", WINDOWS_HOST, f"del {win_img} {win_base}.txt"],
        capture_output=True, timeout=10,
    )
    local_img.unlink(missing_ok=True)
    local_out.unlink(missing_ok=True)

    return text


def _clean_ocr(text: str) -> str:
    """Clean up common Tesseract artifacts."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        alpha = sum(1 for c in stripped if c.isalpha())
        total = sum(1 for c in stripped if c.isprintable())
        # Skip garbled OCR header lines:
        #   - Mostly symbols/numbers
        #   - Start with lowercase (after stripping leading symbols)
        #   - Contain pipe characters (OCR artifacts)
        #   - Have more parens/brackets than letters
        if total > 0 and alpha / total < 0.4:
            continue
        if "|" in stripped:
            continue
        if stripped and stripped[0].islower() and not stripped[0].isdigit():
            continue
        # Lines like "BICC) :" or "] & ( Board" — has upper+parens but garbled
        paren_count = stripped.count("(") + stripped.count(")") + stripped.count("[") + stripped.count("]")
        if paren_count > 0 and alpha < 10 and len(stripped) > 3:
            continue
        # Skip lines that start with punctuation (OCR header artifacts)
        if stripped and not stripped[0].isalnum():
            continue
        # Skip OCR garbage: "Axa aricopa ounty" — mixed case short fragments
        words = stripped.split()
        if len(words) >= 2 and len(stripped) < 50:
            # First word uppercase, second word starts lowercase = garbled
            if words[0][0].isupper() and any(w[0].islower() for w in words[1:]):
                continue
            # Contains lowercase after uppercase mid-word
        lines.append(stripped)
    return "\n".join(lines)


def _split_into_items(ocr_text: str) -> list[str]:
    """Split OCR text into individual agenda items.

    Strategy:
      - Double-newlines are natural section breaks
      - Lines starting with uppercase date-like patterns
      - Lines starting with common section markers like 'At the', 'The emergency'
    """
    import re

    # Try blank-line paragraphs first
    paragraphs = [p.strip() for p in ocr_text.split("\n\n") if p.strip()]
    if len(paragraphs) >= 3:
        return paragraphs

    # Fallback: single item with the full text
    return [ocr_text]


def persist_ocr_as_items(mid: str, body: str, ocr_text: str) -> int:
    """Store OCR text as agenda items in the database.

    Creates a single agenda item with the full OCR text as the description.
    Returns the number of items created.
    """
    # Same path setup as scrape_agendas.py
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(os.path.join(_script_dir, "..", ".."))
    sys.path.insert(0, "scripts")
    from db.core import get_session
    from sqlalchemy import text as _text

    session = get_session()
    meeting = session.execute(
        _text("SELECT id, meeting_date, meeting_type FROM meetings WHERE body=:body AND meeting_id=:mid"),
        {"body": body, "mid": mid},
    ).fetchone()

    if not meeting:
        log.error("Meeting not found: %s / %s", body, mid)
        return 0

    db_id = meeting[0]
    meeting_date = meeting[1]
    meeting_type = meeting[2]

    # Delete existing items
    session.execute(_text("DELETE FROM agenda_items WHERE meeting_db_id = :db_id"), {"db_id": db_id})
    session.execute(_text("DELETE FROM supporting_documents WHERE meeting_db_id = :db_id"), {"db_id": db_id})

    # Parse the OCR text into sections
    paragraphs = _split_into_items(ocr_text)
    now = datetime.now()

    for i, para in enumerate(paragraphs):
        # Choose a clean title — skip OCR header artifacts
        para_lines = para.split("\n")
        title = ""
        for line in para_lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip garbled OCR header lines (mostly non-alpha)
            alpha = sum(1 for c in stripped if c.isalpha())
            total = len(stripped)
            if total > 0 and alpha / total < 0.4:
                continue
            title = stripped[:200]
            break
        if not title:
            title = "Meeting Notice" if len(paragraphs) == 1 else f"Item {i+1}"
        text_body = para[:2000]

        session.execute(
            _text("""INSERT INTO agenda_items
                (meeting_db_id, body, meeting_id, agenda_item_number, agenda_item_id,
                 agenda_item_title, agenda_item_text, agenda_item_url, vote_or_action,
                 source_body, source_url, c_number, c_number_base, case_number,
                 agenda_category, section_level, item_type, sort_order, created_at)
                VALUES (:db_id, :body, :mid, '', :uid, :title, :text, '', '',
                        :body, '', '', '', '', '', 0, 'item', :sort, :now)"""),
            {
                "db_id": db_id, "body": body, "mid": mid,
                "uid": f"ocr-{mid}-{i:03d}", "title": title,
                "text": text_body, "sort": i, "now": now,
            },
        )

    # Update meeting status
    total = len(paragraphs)
    session.execute(
        _text("UPDATE meetings SET sync_status='complete', item_count_actual=:cnt, updated_at=NOW() WHERE id=:db_id"),
        {"cnt": total, "db_id": db_id},
    )
    session.commit()
    session.close()
    return total


def main():
    parser = argparse.ArgumentParser(
        description="OCR an image-scanned OnBase meeting agenda",
    )
    parser.add_argument("body", help="Body code (e.g. 'bos', 'tempe-cc')")
    parser.add_argument("meeting_id", help="OnBase meeting ID")
    parser.add_argument("--show", action="store_true", help="Print OCR text and exit")
    parser.add_argument("--persist", action="store_true", help="Store OCR text as agenda items")
    args = parser.parse_args()

    # Path setup (same pattern as scrape_agendas.py)
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(os.path.join(_script_dir, "..", ".."))
    sys.path.insert(0, "scripts")

    if not args.show and not args.persist:
        parser.error("Specify --show and/or --persist")

    mid = args.meeting_id
    body = args.body

    # Step 1: Capture image from page
    log.info("Step 1/4: Capturing page image for meeting %s…", mid)
    img = capture_image(mid)
    log.info("  Captured %d bytes", len(img))

    # Step 2: OCR on Windows
    log.info("Step 2/4: Running OCR on Windows…")
    raw_text = ocr_on_windows(img, mid)
    clean = _clean_ocr(raw_text)
    log.info("  OCR returned %d chars", len(clean))

    if args.show:
        print("\n" + "=" * 60)
        print(f"OCR TEXT — {body}/{mid}")
        print("=" * 60)
        print(clean)
        print("=" * 60)

    if args.persist:
        log.info("Step 3/4: Persisting to database…")
        count = persist_ocr_as_items(mid, body, clean)
        log.info("  ✅ %d items created for meeting %s", count, mid)

    log.info("Done")


if __name__ == "__main__":
    main()
