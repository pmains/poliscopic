"""Registry adapter for Phoenix Planning Commission ingestion."""

from __future__ import annotations

import datetime as dt


def sync(args) -> int:
    """Run the established Phoenix planning persistence pipeline."""
    from db import get_session, init_db
    from scraper.jurisdictions.phoenix_planning import sync_all

    init_db()
    session = get_session()
    try:
        results = sync_all(session, force=getattr(args, "force", False))
    finally:
        session.close()

    events = results.get("events", {})
    staff = results.get("staff_reports", {})
    pud = results.get("pud_cases", {})
    timestamp = dt.datetime.now().strftime("%H:%M:%S")
    print(
        f"{timestamp} Phoenix planning sync complete: "
        f"{events.get('synced', 0)}/{events.get('fetched', 0)} events, "
        f"{staff.get('docs_synced', 0)}/{staff.get('fetched', 0)} staff docs, "
        f"{pud.get('docs_synced', 0)}/{pud.get('fetched', 0)} PUD docs"
    )
    return 0
