#!/usr/bin/env python3
"""Re-scrape a Tempe BOA meeting to pull item text with the fixed parser."""
import asyncio, json, os, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

async def main():
    from scraper.platforms.onbase import TEMPE_CONFIG, parse_agenda_html, fetch_agenda_html
    from scraper.common.html_utils import _parse_html, _find_all

    # Tempe BOA Regular Meeting July 29 — meeting_id = 1914
    meeting_id = 1914
    public_body_code = "tempe-boa"

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            html = await fetch_agenda_html(page, TEMPE_CONFIG, meeting_id)
        except Exception as e:
            print(f"Failed to fetch agenda: {e}", file=sys.stderr)
            # Fallback: try direct URL
            url = f"https://tempe.hylandcloud.com/Agendaonline/Meetings/ViewMeetingAgenda?meetingId={meeting_id}"
            await page.goto(url)
            # Switch to accessible view
            await page.wait_for_timeout(3000)
            accessible_btn = page.locator("text=Switch to Accessible View")
            if await accessible_btn.is_visible():
                await accessible_btn.click()
                await page.wait_for_timeout(2000)
            html = await page.content()

        await browser.close()

    # Parse with the updated parser (now extracts item text)
    items = parse_agenda_html(html, str(meeting_id), public_body_code)

    print(f"Parsed {len(items)} items from BOA meeting {meeting_id}")
    for item in items:
        title = item.get("agenda_item_title", "")
        text = item.get("agenda_item_text", "")
        itype = item.get("item_type", "")
        print(f"  [{itype:7s}] {title[:60]:60s} | text_len={len(text)}")
        if text:
            print(f"           {text[:100]}")
        print()

    # Update the database.  Resolution goes through the authoritative tier
    # authority, which loads .env, selects the development target and refuses a
    # production-like result; this script no longer parses .env by hand.
    from sqlalchemy import create_engine, text as sa_text
    from db.config import DATABASE_URL as _db_url

    engine = create_engine(_db_url)
    updated = 0
    with engine.connect() as conn:
        trans = conn.begin()
        for item in items:
            item_text = item.get("agenda_item_text", "")
            item_num = item.get("agenda_item_number", "")
            item_title = item.get("agenda_item_title", "")
            if item_text and item_num:
                result = conn.execute(
                    sa_text("""
                        UPDATE agenda_items
                        SET agenda_item_text = :text
                        WHERE body = :body
                        AND meeting_id = :mid
                        AND agenda_item_number = :num
                        AND (agenda_item_text IS NULL OR agenda_item_text = '')
                        RETURNING agenda_item_id
                    """),
                    {"text": item_text, "body": public_body_code,
                     "mid": str(meeting_id), "num": item_num},
                )
                if result.rowcount > 0:
                    updated += result.rowcount
                    print(f"    Updated item #{item_num}: {item_title[:50]}")
        trans.commit()

    print(f"\nUpdated {updated} items in database")

if __name__ == "__main__":
    asyncio.run(main())
