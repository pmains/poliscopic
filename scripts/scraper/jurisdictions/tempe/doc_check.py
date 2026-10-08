#!/usr/bin/env python3
"""
DEPRECATED — Use scripts/scraper/common/doc_check.py instead.

This module is kept for backward compatibility during the migration.
The scheduler + probe logic has been moved to:
  - scripts/scraper/common/doc_check.py          (scheduler + CLI)
  - scripts/scraper/platforms/onbase.py           (OnBase probe)
  - scripts/scraper/platforms/granicus_common.py  (Granicus probe)

Will be removed after the 2026-07-24 nightly cron confirms the
new scheduler handles all existing Tempe OnBase doc checks.
"""

import warnings
warnings.warn(
    "scripts/scraper/jurisdictions/tempe/doc_check.py is deprecated. "
    "Use scripts/scraper/common/doc_check.py instead.",
    DeprecationWarning,
    stacklevel=2,
)

import logging
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("doc_check")

# ── OnBase meeting types that never have item-level supporting documents ──
SKIP_MEETING_TYPES = frozenset({
    "executive session",
    "executive",
    "cancelled",
    "canceled",
})

# ── Bodies that CAN have item-level supporting documents via OnBase ──
# Any new Tempe body added to the scraper should be added here.
CHECKABLE_BODIES = frozenset({
    "tempe-cc",
    "tempe-drc",
    "tempe-boa",
    "tempe-hpc",
    "tempe-jrc",
    "tempe-ha",
    "tempe-rio",
    "tempe-rmt",
    "tempe-aviation-commission",
})

# ── Backoff schedule (days between checks) ──
BACKOFF_DAYS = [2, 4, 8, 16]

# ── Sunset: max days past meeting date before we give up ──
SUNSET_DAYS = 30

# ── Skeleton/empty meeting detection ──
# Item titles that are just boilerplate/template text.
# If a past meeting ONLY has these, it never actually had business.
_SKELETON_TITLE_LOWERS = frozenset({
    "call to order",
    "consideration of meeting minutes",
    "meeting minutes",
    "approval of minutes",
    "public appearances",
    "reports and announcements",
    "announcements / miscellaneous",
    "committee member announcements",
    "city staff announcements",
    "commission member announcements",
    "staff announcements",
    "current events/council announcements/future agenda items",
    "adjournment",
    "consent agenda",
    "non-consent agenda",
    "development plan review appeal: none",
    "development plan review: none",
    "development plan review",
    "development plan review appeal",
    "code text amendment: none",
    "code text amendment",
    "use permits",
})

# Item titles whose presence means this meeting had actual business
_SUBSTANTIVE_KEYWORDS = frozenset({
    "ordinance", "resolution", "public hearing", "contract", "bid",
    "rezoning", "zoning map", "general plan", "development agreement",
    "use permit", "planned area development", "staff report",
    "conditional use permit", "variance", "subdivision", "plat",
    "easement", "annexation", "budget", "appropriation", "grant",
    "intergovernmental", "license agreement", "lease",
    "construction", "improvement", "project", "assessment",
    "tax levy", "bond", "fee schedule", "comprehensive plan",
})


def meeting_should_never_have_docs(meeting_type: str) -> bool:
    """Return True if this meeting type will never have item-level docs."""
    mt = (meeting_type or "").lower().strip()
    for skip in SKIP_MEETING_TYPES:
        if skip in mt:
            return True
    return False


def fetch_agenda_page(meeting_id: int) -> str:
    """Fetch the OnBase ViewMeetingAgenda page (one HTTP GET).

    Returns the HTML text, or empty string on failure.
    """
    url = (
        "https://tempe.hylandcloud.com/Agendaonline/Meetings/ViewMeetingAgenda"
        f"?meetingId={meeting_id}&type=agenda"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        log.debug("Failed to fetch agenda page for meeting %d: %s", meeting_id, e)
        return ""


def has_item_detail_handlers(html: str) -> bool:
    """Check if the agenda page has loadAgendaItem(NNN) handlers.

    These handlers are rendered by OnBase when item-level supporting
    documents are published. Their presence means docs are available.
    """
    return bool(re.search(r"loadAgendaItem\(\d+\)", html))


def has_accessible_items(html: str) -> bool:
    """Check if the agenda page has accessible-item elements at all."""
    return "accessible-item" in html


def _extract_item_titles(html: str) -> list[str]:
    """Extract all item and section titles from the accessible agenda HTML.

    Returns a list of lowercase title strings.  Captures both
    section headings (accessible-header-text) and individual
    agenda items (accessible-item-text), since either could
    indicate whether the meeting has actual business.
    """
    titles = []
    # Section headings: <span class="accessible-header-text">TITLE</span>
    for m in re.finditer(
        r'<span\s+class="accessible-header-text">\s*([^<]+?)\s*</span>',
        html, re.I
    ):
        titles.append(m.group(1).strip().lower())
    # Individual item titles: <span class="accessible-item-text">TITLE</span>
    for m in re.finditer(
        r'<span\s+class="accessible-item-text">\s*([^<]+?)\s*</span>',
        html, re.I
    ):
        titles.append(m.group(1).strip().lower())
    return titles


def _is_skeleton_meeting(html: str) -> bool:
    """Check if a past meeting's agenda has NO substantive business items.

    A skeleton meeting is one where the ONLY items are boilerplate
    (Call to Order, Minutes, Announcements, Adjournment).  These
    indicate a meeting that was effectively cancelled or never had
    business published, even though OnBase didn't put CANCELED in
    the title.

    Returns True if the meeting should be marked no_agenda.
    """
    titles = _extract_item_titles(html)
    if not titles:
        return False  # No items at all — already handled elsewhere

    # Fast check: if ANY title contains a substantive keyword, this
    # meeting had real business.
    for t in titles:
        for kw in _SUBSTANTIVE_KEYWORDS:
            if kw in t:
                return False

    # Check if EVERY item is a known skeleton title
    for t in titles:
        if t not in _SKELETON_TITLE_LOWERS:
            # Unknown item title that doesn't look like a substantive keyword
            # either.  But it could be something like "Action Item" or a
            # generic section name.  Err on the side of NOT marking as
            # skeleton if there's any title we don't recognize.
            log.debug("Unknown non-substantive item title: %r", t)
            return False

    return True


def check_meetings(dry_run: bool = True, limit: int = 0) -> dict:
    """Main check loop.

    Parameters
    ----------
    dry_run : bool
        If True, report findings but do NOT re-sync or update check dates.
    limit : int
        Max meetings to check. 0 = unlimited.

    Returns a dict with summary stats.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    from db import get_session
    from db.models import Meeting as MeetingModel
    from sqlalchemy import select, func

    now = datetime.now(timezone.utc)
    today = date.today()
    session = get_session()

    # Find meetings due for a check
    rows = session.execute(
        select(MeetingModel)
        .where(
            MeetingModel.body.in_(CHECKABLE_BODIES),
            MeetingModel.next_doc_check_at.isnot(None),
            MeetingModel.next_doc_check_at <= now,
            MeetingModel.items_extracted == True,  # noqa: E712
            MeetingModel.supporting_docs_extracted == False,  # noqa: E712
            MeetingModel.sync_status.in_(["complete", "pending"]),
        )
        .order_by(MeetingModel.next_doc_check_at)
        .limit(limit or None)
    ).scalars().all()

    if not rows:
        log.info("No meetings due for doc check.")
        return {
            "checked": 0,
            "docs_available": 0,
            "docs_still_missing": 0,
            "sunsets": 0,
            "skipped_meeting_type": 0,
            "errors": 0,
        }

    log.info("Checking %d meeting(s) for document availability...", len(rows))

    stats = {
        "checked": 0,
        "docs_available": 0,
        "docs_still_missing": 0,
        "sunsets": 0,
        "skipped_meeting_type": 0,
        "errors": 0,
    }

    for meeting in rows:
        meeting_id = meeting.meeting_id
        meeting_date_str = meeting.meeting_date or ""
        body_code = meeting.body
        meeting_type = meeting.meeting_type or ""

        # Skip meeting types that never have docs
        if meeting_should_never_have_docs(meeting_type):
            log.info("  %s/%s: skipping (type=%s)", body_code, meeting_id, meeting_type)
            if not dry_run:
                meeting.next_doc_check_at = None
                session.flush()
            stats["skipped_meeting_type"] += 1
            continue

        # Sunset check: if meeting is too old, give up
        try:
            meeting_date = date.fromisoformat(meeting_date_str) if meeting_date_str else today
        except (ValueError, TypeError):
            meeting_date = today
        days_since_meeting = (today - meeting_date).days

        if days_since_meeting > SUNSET_DAYS:
            log.info("  %s/%s (%s): sunset (%d days old)",
                      body_code, meeting_id, meeting_date_str, days_since_meeting)
            if not dry_run:
                meeting.next_doc_check_at = None
                session.flush()
            stats["sunsets"] += 1
            continue

        stats["checked"] += 1

        # Lightweight check: fetch agenda page
        html = fetch_agenda_page(int(meeting_id))
        if not html:
            log.info("  %s/%s: fetch failed", body_code, meeting_id)
            stats["errors"] += 1
            continue

        # ── Skeleton/empty meeting detection ──────────────────────────
        # If the meeting is in the past and has ONLY boilerplate items
        # (Call to Order, Minutes, Announcements, Adjournment), it never
        # had actual business — treat as cancelled.
        if days_since_meeting >= 0 and _is_skeleton_meeting(html):
            log.info("  🟡 %s/%s (%s): skeleton agenda, marking no_agenda",
                      body_code, meeting_id, meeting_date_str)
            stats["sunsets"] += 1
            if not dry_run:
                from db import update_sync_status
                meeting.sync_status = "no_agenda"
                meeting.last_error = "Agenda had no substantive items (likely cancelled)"
                meeting.next_doc_check_at = None
                session.flush()
            continue

        # Check for accessible items + loadAgendaItem handlers
        items_present = has_accessible_items(html)
        handlers_present = has_item_detail_handlers(html)

        if items_present and handlers_present and days_since_meeting >= 0:
            # Docs are available! Trigger re-sync
            stats["docs_available"] += 1
            log.info("  ✅ %s/%s (%s): docs available! Re-syncing...",
                      body_code, meeting_id, meeting_date_str)

            if not dry_run:
                try:
                    _resync_meeting(body_code, meeting_id, meeting_date_str, meeting_type)
                    meeting.next_doc_check_at = None
                    session.flush()
                    log.info("     Re-sync complete")
                except Exception as e:
                    log.warning("     Re-sync failed: %s", e)
                    # Don't clear check date — try again later
                    stats["errors"] += 1
        else:
            # Docs not yet available — apply exponential backoff
            # Count how many times we've checked (approximate via check date history)
            # For simplicity, use days since meeting to determine backoff tier
            backoff_index = min(days_since_meeting // 4, len(BACKOFF_DAYS) - 1)
            backoff_days = BACKOFF_DAYS[backoff_index]
            next_check = now + timedelta(days=backoff_days)

            stats["docs_still_missing"] += 1
            log.info("  ❌ %s/%s (%s): no handlers yet (backoff %dd → %s)",
                      body_code, meeting_id, meeting_date_str,
                      backoff_days, next_check.strftime("%Y-%m-%d"))

            if not dry_run:
                meeting.next_doc_check_at = next_check
                session.flush()

    if not dry_run:
        session.commit()
    session.close()

    log.info("Doc check complete: %d checked, %d available, %d still missing, %d sunsets, %d errors",
              stats["checked"], stats["docs_available"],
              stats["docs_still_missing"], stats["sunsets"], stats["errors"])
    return stats


def _resync_meeting(body_code: str, meeting_id: str,
                     meeting_date: str, meeting_type: str) -> None:
    """Trigger a targeted re-sync of a single meeting to pick up docs."""
    from db import get_session
    from db.persist import replace_meeting_data_safe
    from scraper.platforms.onbase import (
        fetch_agenda_sync, parse_agenda_html,
        fetch_item_details_sync, parse_item_details,
        TEMPE_CONFIG,
    )
    from scraper.jurisdictions.tempe import _assign_tempe_categories

    session = get_session()
    try:
        html = fetch_agenda_sync(TEMPE_CONFIG, int(meeting_id))
        items = parse_agenda_html(html, meeting_id, body_code)
        source_url = f"https://tempe.hylandcloud.com/Agendaonline/Meetings/ViewMeeting?id={meeting_id}&doctype=1"
        for item in items:
            item["source_url"] = source_url
            item["body"] = body_code
        _assign_tempe_categories(items)

        # Build supporting docs
        supp_docs = []
        # Meeting-level: Agenda Packet
        packet_url = (
            f"https://tempe.hylandcloud.com/Agendaonline/Documents/Downloadfile/"
            f"{meeting_id}_packet.pdf?documentType=5&meetingId={meeting_id}"
        )
        supp_docs.append({
            "agenda_item_id": 0,
            "agenda_item_number": "0",
            "document_title": "Agenda Packet",
            "document_url": packet_url,
            "document_type": "Packet",
            "file_name": f"{meeting_id}_packet.pdf",
            "file_extension": ".pdf",
        })

        # Item-level docs
        for item in items:
            oid = item.get("onbase_item_id")
            if not oid:
                continue
            detail_html = fetch_item_details_sync(TEMPE_CONFIG, int(meeting_id), oid)
            if detail_html:
                item_docs, _ = parse_item_details(
                    detail_html, meeting_id, body_code,
                    item.get("agenda_item_number", ""),
                    base_url=TEMPE_CONFIG.base_url,
                )
                for doc in item_docs:
                    doc["agenda_item_id"] = item.get("agenda_item_id", "0")
                supp_docs.extend(item_docs)

        meeting_dict = {
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "meeting_type": meeting_type,
            "meeting_title": meeting_type,
            "source_url": source_url,
        }

        replace_meeting_data_safe(
            session, body_code, meeting_id, meeting_dict, items,
            supporting_doc_dicts=supp_docs,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Check Tempe OnBase meetings for newly-published supporting documents"
    )
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report only, don't re-sync or update check dates (default)")
    parser.add_argument("--apply", action="store_false", dest="dry_run",
                        help="Actually re-sync meetings and update check dates")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max meetings to check (0 = unlimited)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stats = check_meetings(dry_run=args.dry_run, limit=args.limit)

    print(f"\n=== Doc Check Summary ===")
    print(f"  Checked:        {stats['checked']}")
    print(f"  Docs available: {stats['docs_available']}")
    print(f"  Still missing:  {stats['docs_still_missing']}")
    print(f"  Sunset (no more checks): {stats['sunsets']}")
    print(f"  Skipped (type):  {stats['skipped_meeting_type']}")
    print(f"  Errors:         {stats['errors']}")
    if args.dry_run:
        print(f"\n  (dry run — no changes applied)")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
