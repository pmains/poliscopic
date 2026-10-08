#!/usr/bin/env python3
"""
Doc Availability Checker — lightweight scheduler for post-sync
supporting document discovery.

Checks whether item-level supporting documents have been published for
meetings that were synced before their documents were available.

Design:
  - Queries meetings where next_doc_check_at <= now across ALL platforms.
  - Dispatches to the correct platform probe based on meeting body / source.
  - Each probe does one lightweight HTTP GET/HEAD to check for documents.
  - If docs found → trigger targeted re-sync.
  - If not found → exponential backoff (2d → 4d → 8d → 16d).
  - Sunset after 30 days past meeting date.
  - Skeleton meeting detection — if only boilerplate items, mark no_agenda.

Usage:
  # As a standalone CLI (debugging / manual runs):
  python3 scripts/scraper/common/doc_check.py --apply
  python3 scripts/scraper/common/doc_check.py --dry-run --limit 10

  # From run_pipeline.py:
  from scraper.common.doc_check import run_doc_check
  run_doc_check()
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("doc_check")

# ── Skeleton/empty meeting detection ──────────────────────────────────────
# Item titles that are just boilerplate/template text.
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

# Item keywords whose presence means this meeting had actual business
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

# ── Backoff and sunset ───────────────────────────────────────────────────
BACKOFF_DAYS = [2, 4, 8, 16]
SUNSET_DAYS = 30

# ── Meeting types that never have item-level supporting documents ─────────
SKIP_MEETING_TYPES = frozenset({
    "executive session",
    "executive",
    "cancelled",
    "canceled",
})

# ── Platform probe registry ───────────────────────────────────────────────
# Maps body prefix → (probe_function, platform_name)
# Probe function signature: check_fn(meeting) -> DocCheckResult
# Returns None if the probe is not available for this body.


def _get_probe(body: str):
    """Return (probe_function, platform_name) for a body code, or None."""
    body_lower = body.lower()

    # OnBase — Tempe is the only OnBase jurisdiction
    if body_lower.startswith("tempe"):
        from scraper.platforms.onbase import check_meeting_docs_onbase
        return check_meeting_docs_onbase, "onbase"

    # Granicus — bodies served by Granicus
    granicus_bodies = {
        "buckeye", "surprise", "goodyear", "avondale",
    }
    for prefix in granicus_bodies:
        if body_lower.startswith(prefix):
            from scraper.platforms.granicus_common import check_meeting_docs_granicus
            return check_meeting_docs_granicus, "granicus"

    return None


def meeting_should_never_have_docs(meeting_type: str | None) -> bool:
    """Return True if this meeting type will never have item-level docs."""
    mt = (meeting_type or "").lower().strip()
    for skip in SKIP_MEETING_TYPES:
        if skip in mt:
            return True
    return False


# ── Result type ───────────────────────────────────────────────────────────


class DocCheckResult:
    """Result from a platform doc check probe."""
    __slots__ = ("docs_available", "is_skeleton", "error")

    def __init__(self, docs_available: bool = False,
                 is_skeleton: bool = False,
                 error: str | None = None):
        self.docs_available = docs_available
        self.is_skeleton = is_skeleton
        self.error = error

    def __repr__(self) -> str:
        if self.error:
            return f"DocCheckResult(error={self.error!r})"
        return f"DocCheckResult(docs_available={self.docs_available}, is_skeleton={self.is_skeleton})"


# ── Skeleton detection helpers (kept here since they're CMS-agnostic) ─────


def _extract_item_titles(html: str) -> list[str]:
    """Extract item and section titles from accessible agenda HTML."""
    import re
    titles = []
    for m in re.finditer(
        r'<span\s+class="accessible-header-text">\s*([^<]+?)\s*</span>',
        html, re.I
    ):
        titles.append(m.group(1).strip().lower())
    for m in re.finditer(
        r'<span\s+class="accessible-item-text">\s*([^<]+?)\s*</span>',
        html, re.I
    ):
        titles.append(m.group(1).strip().lower())
    return titles


def is_skeleton_meeting(html: str) -> bool:
    """Check if a past meeting's agenda has ONLY boilerplate/empty items.

    Returns True if the meeting should be marked no_agenda.
    """
    titles = _extract_item_titles(html)
    if not titles:
        return False  # No items at all — handled elsewhere in the caller

    for t in titles:
        for kw in _SUBSTANTIVE_KEYWORDS:
            if kw in t:
                return False  # Found a real item

    for t in titles:
        if t not in _SKELETON_TITLE_LOWERS:
            return False  # Unknown title — err on side of not marking skeleton

    return True


# ── Re-sync dispatch ─────────────────────────────────────────────────────


def _resync_meeting(body_code: str, meeting_id: str,
                    meeting_date: str, meeting_type: str) -> None:
    """Re-sync a single meeting to pick up newly-published supporting docs.

    Dispatches to the correct re-sync handler based on the body code.
    This is a targeted re-sync of a single meeting, not a full scrape.
    """
    body_lower = body_code.lower()

    if body_lower.startswith("tempe"):
        _resync_tempe_onbase(body_code, meeting_id, meeting_date, meeting_type)
    elif body_lower.startswith(("buckeye", "surprise", "goodyear", "avondale")):
        _resync_granicus(body_code, meeting_id, meeting_date, meeting_type)
    else:
        raise NotImplementedError(f"No re-sync handler for {body_code}")


def _resync_tempe_onbase(body_code: str, meeting_id: str,
                          meeting_date: str, meeting_type: str) -> None:
    """Re-sync a single Tempe OnBase meeting to pick up item-level docs."""
    from db import get_session
    from db.persist import replace_meeting_data_safe
    from scraper.platforms.onbase import (
        TEMPE_CONFIG,
        fetch_agenda_sync,
        parse_agenda_html,
        fetch_item_details_sync,
        parse_item_details,
    )
    from scraper.jurisdictions.tempe import _assign_tempe_categories

    session = get_session()
    try:
        meeting_id_int = int(meeting_id)
    except (ValueError, TypeError):
        meeting_id_int = 0

    try:
        html = fetch_agenda_sync(TEMPE_CONFIG, meeting_id_int)
        items = parse_agenda_html(html, str(meeting_id), body_code)
        source_url = (
            f"https://tempe.hylandcloud.com/Agendaonline/Meetings/"
            f"ViewMeeting?id={meeting_id}&doctype=1"
        )
        for item in items:
            item["source_url"] = source_url
            item["body"] = body_code
        _assign_tempe_categories(items)

        # Build supporting docs
        supp_docs = []
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

        for item in items:
            oid = item.get("onbase_item_id")
            if not oid:
                continue
            detail_html = fetch_item_details_sync(TEMPE_CONFIG, meeting_id_int, oid)
            if detail_html:
                item_docs, _ = parse_item_details(
                    detail_html, str(meeting_id), body_code,
                    item.get("agenda_item_number", ""),
                    base_url=TEMPE_CONFIG.base_url,
                )
                for doc in item_docs:
                    doc["agenda_item_id"] = item.get("agenda_item_id", "0")
                supp_docs.extend(item_docs)

        meeting_dict = {
            "meeting_id": str(meeting_id),
            "meeting_date": meeting_date,
            "meeting_type": meeting_type,
            "meeting_title": meeting_type,
            "source_url": source_url,
        }

        replace_meeting_data_safe(
            session, body_code, str(meeting_id), meeting_dict, items,
            supporting_doc_dicts=supp_docs,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _resync_granicus(body_code: str, meeting_id: str,
                      meeting_date: str, meeting_type: str) -> None:
    """Re-sync a single Granicus meeting.

    For Granicus, re-scraping the meeting is handled by running
    scrape_agendas.py with --meeting-id.  This triggers the full
    scraper pipeline which picks up any newly-published documents.
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent.parent
    cmd = [
        _sys.executable, "scripts/scrape_agendas.py",
        body_code, "--sync",
        f"--meeting-id={meeting_id}",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=300,
        cwd=str(root), env={**_sys.environ, "PYTHONPATH": "scripts"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Granicus re-sync failed: {result.stderr[:500]}")


# ── Main scheduler ───────────────────────────────────────────────────────


def seed_next_doc_check(session, since_days: int = 30) -> int:
    """Seed next_doc_check_at for recently-synced meetings that lack docs.

    Looks for meetings across all platforms that:
      - have items extracted
      - do NOT have supporting docs extracted
      - don't already have next_doc_check_at set
      - are from the last N days
      - have a platform probe registered

    Returns the number of meetings seeded.
    """
    from db.models import Meeting
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    today = date.today()
    cutoff = (today - timedelta(days=since_days)).isoformat()

    rows = session.execute(
        select(Meeting).where(
            Meeting.items_extracted == True,
            Meeting.supporting_docs_extracted == False,
            Meeting.next_doc_check_at.is_(None),
            Meeting.meeting_date >= cutoff,
            Meeting.sync_status.in_(["complete", "pending"]),
        )
    ).scalars().all()

    seeded = 0
    for m in rows:
        if meeting_should_never_have_docs(m.meeting_type):
            continue
        if _get_probe(m.body or "") is None:
            continue

        try:
            md = date.fromisoformat(m.meeting_date) if m.meeting_date else today
        except (ValueError, TypeError):
            md = today

        if md >= today:
            m.next_doc_check_at = datetime(md.year, md.month, md.day, tzinfo=timezone.utc)
        else:
            m.next_doc_check_at = now + timedelta(days=2)
        seeded += 1

    session.flush()
    return seeded


def run_doc_check(dry_run: bool = True, limit: int = 0) -> dict[str, int]:
    """Main check loop — called by run_pipeline.py or as CLI.

    Parameters
    ----------
    dry_run : bool
        If True, report findings but do NOT re-sync or update check dates.
    limit : int
        Max meetings to check. 0 = unlimited.

    Returns a dict with summary stats.
    """
    from db import get_session
    from db.models import Meeting
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    today = date.today()
    session = get_session()

    rows = session.execute(
        select(Meeting).where(
            Meeting.next_doc_check_at.isnot(None),
            Meeting.next_doc_check_at <= now,
            Meeting.items_extracted == True,
            Meeting.supporting_docs_extracted == False,
            Meeting.sync_status.in_(["complete", "pending"]),
        ).order_by(Meeting.next_doc_check_at).limit(limit or None)
    ).scalars().all()

    if not rows:
        log.info("No meetings due for doc check.")
        session.close()
        return {"checked": 0, "docs_available": 0, "docs_still_missing": 0,
                "sunsets": 0, "skipped_type": 0, "errors": 0}

    log.info("Checking %d meeting(s) for document availability...", len(rows))

    stats: dict[str, int] = {
        "checked": 0, "docs_available": 0, "docs_still_missing": 0,
        "sunsets": 0, "skipped_type": 0, "errors": 0,
    }

    for meeting in rows:
        meeting_id = meeting.meeting_id
        body_code = meeting.body or ""
        meeting_date_str = meeting.meeting_date or ""
        meeting_type = meeting.meeting_type or ""

        # Skip meeting types that never have docs
        if meeting_should_never_have_docs(meeting_type):
            log.info("  %s/%s: skipping (type=%s)", body_code, meeting_id, meeting_type)
            if not dry_run:
                meeting.next_doc_check_at = None
                session.flush()
            stats["skipped_type"] += 1
            continue

        # Sunset
        try:
            meeting_date = date.fromisoformat(meeting_date_str) if meeting_date_str else today
        except (ValueError, TypeError):
            meeting_date = today
        days_since = (today - meeting_date).days

        if days_since > SUNSET_DAYS:
            log.info("  %s/%s: sunset (%d days old)", body_code, meeting_id, days_since)
            if not dry_run:
                meeting.next_doc_check_at = None
                session.flush()
            stats["sunsets"] += 1
            continue

        # Get platform probe
        probe = _get_probe(body_code)
        if probe is None:
            log.info("  %s/%s: no probe for body", body_code, meeting_id)
            if not dry_run:
                meeting.next_doc_check_at = None
                session.flush()
            stats["sunsets"] += 1
            continue

        probe_fn, platform = probe
        stats["checked"] += 1

        try:
            result = probe_fn(meeting)
        except Exception as e:
            log.info("  %s/%s: probe error: %s", body_code, meeting_id, e)
            stats["errors"] += 1
            continue

        if result.error:
            log.info("  %s/%s: probe returned error: %s", body_code, meeting_id, result.error)
            stats["errors"] += 1
            # Don't clear next_doc_check_at — try again
            continue

        # Skeleton meeting (only boilerplate items, past meeting)
        if result.is_skeleton and days_since >= 0:
            log.info("  %s/%s: skeleton agenda, marking no_agenda", body_code, meeting_id)
            stats["sunsets"] += 1
            if not dry_run:
                from db import update_sync_status
                meeting.sync_status = "no_agenda"
                meeting.last_error = "Agenda had no substantive items (likely cancelled)"
                meeting.next_doc_check_at = None
                session.flush()
            continue

        if result.docs_available and days_since >= 0:
            # Docs are available! Trigger re-sync
            stats["docs_available"] += 1
            log.info("  %s/%s: docs available via %s! Re-syncing...",
                      body_code, meeting_id, platform)
            if not dry_run:
                try:
                    _resync_meeting(body_code, meeting_id,
                                    meeting_date_str, meeting_type)
                    meeting.next_doc_check_at = None
                    session.flush()
                    log.info("     Re-sync complete")
                except Exception as e:
                    log.warning("     Re-sync failed: %s", e)
                    stats["errors"] += 1
        else:
            # Docs not yet available — exponential backoff
            backoff_index = min(days_since // 4, len(BACKOFF_DAYS) - 1)
            backoff_days = BACKOFF_DAYS[backoff_index]
            next_check = now + timedelta(days=backoff_days)
            stats["docs_still_missing"] += 1
            log.info("  %s/%s: no docs yet (backoff %dd → %s)",
                      body_code, meeting_id, backoff_days,
                      next_check.strftime("%Y-%m-%d"))
            if not dry_run:
                meeting.next_doc_check_at = next_check
                session.flush()

    if not dry_run:
        session.commit()
    session.close()

    log.info("Doc check complete: %d checked, %d available, %d still missing, "
              "%d sunsets, %d errors",
              stats["checked"], stats["docs_available"],
              stats["docs_still_missing"], stats["sunsets"], stats["errors"])
    return stats


# ── CLI entry point ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Check meetings for newly-published supporting documents"
    )
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report only (default)")
    parser.add_argument("--apply", action="store_false", dest="dry_run",
                        help="Actually re-sync meetings and update check dates")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max meetings to check (0 = unlimited)")
    parser.add_argument("--seed", action="store_true",
                        help="Seed next_doc_check_at for meetings needing checking")
    parser.add_argument("--since-days", type=int, default=30,
                        help="Days back to seed (default: 30, used with --seed)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.seed:
        from db import get_session
        session = get_session()
        seeded = seed_next_doc_check(session, since_days=args.since_days)
        session.commit()
        session.close()
        print(f"Seeded {seeded} meeting(s) for doc check")
        if args.dry_run:
            print("  (dry run — rolled back)")

    stats = run_doc_check(dry_run=args.dry_run, limit=args.limit)

    print(f"\n=== Doc Check Summary ===")
    print(f"  Checked:         {stats['checked']}")
    print(f"  Docs available:  {stats['docs_available']}")
    print(f"  Still missing:   {stats['docs_still_missing']}")
    print(f"  Sunset:          {stats['sunsets']}")
    print(f"  Skipped (type):  {stats['skipped_type']}")
    print(f"  Errors:          {stats['errors']}")
    if args.dry_run:
        print(f"\n  (dry run — no changes applied)")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
