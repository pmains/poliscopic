#!/usr/bin/env python3
"""
Drop old tables after migration is verified in production.

Only drops tables whose new counterpart has data.  Refuses to run if
the new table is empty — prevents accidental data loss.

Usage:
    source .env && PROD_DATABASE_URL="..." python3 scripts/db/cleanup_prod_db.py
"""

import logging
import os
import re
import sys

# Make the shared database-tier authority importable however this tool is invoked.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))

from db.tier import PRODUCTION, TierError, resolve_role_url  # noqa: E402
from ops.production_interlock_guard import require_production_interlock  # noqa: E402
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy import create_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cleanup_prod_db")


def _mask_url(url: str) -> str:
    return re.sub(r"(//[^:]+:).+?(@)", r"\1****\2", url)


def _resolve_prod_url() -> str:
    """The production role, validated before any engine is constructed."""
    url = os.environ.get("PROD_DATABASE_URL")
    if not url:
        log.error("Set PROD_DATABASE_URL")
        sys.exit(1)
    try:
        target = resolve_role_url(PRODUCTION, url, label="prod")
    except TierError as exc:
        log.error("prod role refused before connecting: %s", exc)
        sys.exit(1)
    log.info("Prod: %s", target.redacted())
    return url


# ═══════════════════════════════════════════════════════════════════════════
#  Cleanup
# ═══════════════════════════════════════════════════════════════════════════


def print_status(engine):
    """Print current table state."""
    inspector = sa_inspect(engine)
    tables = inspector.get_table_names()

    print(f"\n{'=' * 60}")
    print(f"  Prod cleanup status")
    print(f"{'=' * 60}")
    print(f"  {'Table':<35s} {'Rows':>8}")
    print(f"  {'─' * 44}")

    for table in sorted(tables):
        if table.startswith("_ingest"):
            continue  # Skip internal tables in display
        try:
            with engine.connect() as c:
                cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()
            print(f"  {table:<35s} {cnt:>8}")
        except Exception:
            print(f"  {table:<35s}   ERROR")

    print(f"{'=' * 60}")

    if "meeting_supervisors" in tables and "meeting_members" in tables:
        print(f"\n  Old and new tables both present. Ready for cleanup.\n")
    elif "meeting_supervisors" in tables:
        print(f"\n  ⚠  meeting_members doesn't exist yet. Run migrate_prod_db.py first.\n")
    else:
        print(f"\n  ✅ No old tables to clean up.\n")


def drop_meeting_supervisors(engine):
    """Drop meeting_supervisors if meeting_members has data.

    Safety guard: refuses if meeting_members is empty.
    """
    inspector = sa_inspect(engine)
    if "meeting_supervisors" not in inspector.get_table_names():
        log.info("  meeting_supervisors: already removed ✓")
        return

    if "meeting_members" not in inspector.get_table_names():
        log.warning("  meeting_members doesn't exist — cannot safely drop meeting_supervisors")
        log.warning("  Run scripts/db/migrate_prod_db.py first")
        return

    with engine.connect() as conn:
        new_count = conn.execute(
            text("SELECT COUNT(*) FROM meeting_members")
        ).scalar()

        if not new_count or new_count == 0:
            log.warning("  meeting_members is empty — NOT dropping meeting_supervisors")
            log.warning("  Something went wrong with the migration. Check data before retrying.")
            return

        old_count = conn.execute(
            text("SELECT COUNT(*) FROM meeting_supervisors")
        ).scalar()

    log.info("  meeting_members: %d rows", new_count)
    log.info("  meeting_supervisors: %d rows (will be dropped)", old_count)

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS meeting_supervisors CASCADE"))
    log.info("  meeting_supervisors: dropped ✓")


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Drop old tables after migration is verified"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print table status and exit (no changes)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip confirmation prompts (for automated runs)",
    )
    args = parser.parse_args()

    require_production_interlock(
        "OP-STATUS" if args.status else "OP-SCHEMA",
        "scripts/db/cleanup_prod_db.py",
    )

    prod_url = _resolve_prod_url()
    engine = create_engine(prod_url, pool_size=2, connect_args={"connect_timeout": 10})

    with engine.connect() as c:
        pg_version = c.execute(text("SELECT version()")).scalar()
        log.info("Connected: %s", pg_version.split(",")[0])

    if args.status:
        print_status(engine)
        engine.dispose()
        return

    log.info("── Cleanup old tables ──")

    # Final safety confirmation
    if not args.force:
        print()
        print("  ⚠  This will DROP the following tables if their replacements have data:")
        print("     - meeting_supervisors")
        print()
        print("  This is IRREVERSIBLE (unless you have a DB snapshot).")
        print()

        import sys
        try:
            print("  Press Ctrl+C to cancel, or Enter to proceed...", end=" ")
            sys.stdout.flush()
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            engine.dispose()
            return

    drop_meeting_supervisors(engine)

    log.info("── Cleanup complete ──")
    print_status(engine)
    engine.dispose()


if __name__ == "__main__":
    main()
