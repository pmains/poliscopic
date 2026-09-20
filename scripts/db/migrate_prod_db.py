#!/usr/bin/env python3
"""
Create new tables and copy data from old tables (no cleanup).

Strategy: CREATE NEW → COPY DATA (both tables coexist after this).

The old table stays untouched — cleanup is a separate step run after
verification.

Usage:
    source .env && PROD_DATABASE_URL="..." python3 scripts/db/migrate_prod_db.py
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
log = logging.getLogger("migrate_prod_db")


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
#  Migration: meeting_supervisors → meeting_members
# ═══════════════════════════════════════════════════════════════════════════

_CREATE_MEETING_MEMBERS_SQL = """
CREATE TABLE IF NOT EXISTS meeting_members (
    id            SERIAL PRIMARY KEY,
    body          VARCHAR(16) NOT NULL DEFAULT '',
    meeting_id    VARCHAR(32) NOT NULL,
    meeting_db_id INTEGER NOT NULL DEFAULT 0,
    member_id     INTEGER NOT NULL,
    role          VARCHAR(64) DEFAULT NULL,
    present       BOOLEAN DEFAULT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_meeting_member UNIQUE (body, meeting_id, member_id)
);

CREATE INDEX IF NOT EXISTS ix_meeting_members_body
    ON meeting_members (body);
CREATE INDEX IF NOT EXISTS ix_meeting_members_meeting_id
    ON meeting_members (meeting_id);
CREATE INDEX IF NOT EXISTS ix_meeting_members_meeting_db_id
    ON meeting_members (meeting_db_id);
CREATE INDEX IF NOT EXISTS ix_meeting_members_member_id
    ON meeting_members (member_id);
CREATE INDEX IF NOT EXISTS ix_meeting_members_updated_at
    ON meeting_members (updated_at);
"""


def _create_meeting_members_table(engine) -> bool:
    """Create the meeting_members table (idempotent).

    Returns True if the table was created just now, False if it already
    existed.
    """
    inspector = sa_inspect(engine)
    if "meeting_members" in inspector.get_table_names():
        log.info("  meeting_members: already exists ✓")
        return False

    log.info("  Creating meeting_members table...")
    with engine.begin() as conn:
        conn.execute(text(_CREATE_MEETING_MEMBERS_SQL))
    log.info("  meeting_members: created ✓")
    return True


def _copy_meeting_supervisors_to_meeting_members(engine) -> int:
    """Copy data from meeting_supervisors → meeting_members.

    Maps supervisor_id → member_id.  Batched 5,000 at a time to keep
    individual transactions short.  Uses ON CONFLICT DO NOTHING so it's
    safe to re-run.

    Returns the number of source rows.
    """
    inspector = sa_inspect(engine)
    if "meeting_supervisors" not in inspector.get_table_names():
        log.info("  meeting_supervisors: table doesn't exist, nothing to copy")
        return 0

    with engine.begin() as conn:
        source_count = conn.execute(
            text("SELECT COUNT(*) FROM meeting_supervisors")
        ).scalar()

        if source_count == 0:
            log.info("  meeting_supervisors: empty, nothing to copy")
            return 0

        log.info("  Copying %d rows from meeting_supervisors → meeting_members ...", source_count)

        BATCH_SIZE = 5000
        offset = 0

        while offset < source_count:
            result = conn.execute(text(
                "INSERT INTO meeting_members "
                "(body, meeting_id, meeting_db_id, member_id, role, present, created_at, updated_at) "
                "SELECT body, meeting_id, meeting_db_id, supervisor_id, role, present, created_at, updated_at "
                "FROM meeting_supervisors "
                "ORDER BY id "
                "LIMIT :limit OFFSET :offset "
                "ON CONFLICT (body, meeting_id, member_id) DO NOTHING"
            ), {"limit": BATCH_SIZE, "offset": offset})
            offset += BATCH_SIZE
            if offset % 10000 == 0 or offset >= source_count:
                log.info("    progress: %d / %d", min(offset, source_count), source_count)

        target_count = conn.execute(
            text("SELECT COUNT(*) FROM meeting_members")
        ).scalar()
        log.info("  Copied: %d rows into meeting_members", target_count)

        # Reset sequence
        max_id = conn.execute(
            text("SELECT COALESCE(MAX(id), 0) FROM meeting_members")
        ).scalar()
        if max_id and max_id > 0:
            conn.execute(text("SELECT setval('meeting_members_id_seq', :max_id)"), {"max_id": max_id})

    return source_count


def migrate_meeting_members(engine):
    """Create meeting_members and copy data from meeting_supervisors.

    Safe to run multiple times.  Preserves the old table.
    """
    created = _create_meeting_members_table(engine)
    if not created:
        # Table already existed — still check if we need a data copy.
        # This catches interruption mid-copy on a previous run.
        with engine.connect() as conn:
            existing = conn.execute(
                text("SELECT COUNT(*) FROM meeting_members")
            ).scalar()
        if existing == 0:
            created = True

    _copy_meeting_supervisors_to_meeting_members(engine)


# ═══════════════════════════════════════════════════════════════════════════
#  Migration: _ingest_failures
# ═══════════════════════════════════════════════════════════════════════════


def create_ingest_failures(engine):
    """Create _ingest_failures table if it doesn't exist."""
    inspector = sa_inspect(engine)
    if "_ingest_failures" in inspector.get_table_names():
        log.info("  _ingest_failures: already exists ✓")
        return

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS _ingest_failures (
                id              SERIAL PRIMARY KEY,
                error_category  VARCHAR(32) NOT NULL,
                source          VARCHAR(64) NOT NULL,
                body            VARCHAR(16),
                -- External scraper/source ID, never a meetings.id reference.
                meeting_id      VARCHAR(32),
                meeting_date    VARCHAR(16),
                error           TEXT NOT NULL,
                context         TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
    log.info("  _ingest_failures: created ✓")


# ═══════════════════════════════════════════════════════════════════════════
#  Status
# ═══════════════════════════════════════════════════════════════════════════


def print_status(engine):
    """Print current table state."""
    inspector = sa_inspect(engine)
    tables = inspector.get_table_names()

    print(f"\n{'=' * 60}")
    print(f"  Prod schema status")
    print(f"{'=' * 60}")

    for table in sorted(tables):
        with engine.connect() as c:
            cnt = c.execute(
                text(f'SELECT COUNT(*) FROM public."{table}"')
            ).scalar()
        print(f"  {table:<35s}  {cnt:>8} rows")

    print(f"{'=' * 60}")

    if "meeting_supervisors" in tables and "meeting_members" in tables:
        print(f"\n  ✅ Both tables present. Cleanup is the next step.")
    elif "meeting_members" in tables and "meeting_supervisors" not in tables:
        print(f"\n  ✅ Migration complete. Old table already cleaned up.\n")


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════


def main():
    require_production_interlock("OP-SCHEMA", "scripts/db/migrate_prod_db.py")
    prod_url = _resolve_prod_url()
    engine = create_engine(prod_url, pool_size=2, connect_args={"connect_timeout": 10})

    with engine.connect() as c:
        pg_version = c.execute(text("SELECT version()")).scalar()
        log.info("Connected: %s", pg_version.split(",")[0])

    log.info("── Prod schema migration ──")

    migrate_meeting_members(engine)
    create_ingest_failures(engine)

    log.info("── Migration complete ──")

    print_status(engine)

    print()
    print("  Next steps:")
    print("    1. Sync:    BATCH_SIZE=5000 BATCH_SLEEP_MS=100 \\")
    print("                  python3 scripts/db/sync_prod.py")
    print("    2. Deploy:  ./scripts/deploy_code.sh --execute")
    print("    3. Reload:  ./scripts/reload_gunicorn.sh")
    print("    4. Verify using the configured production deployment host")
    print("                  'cd /opt/poliscopic && python3 scripts/verify_deploy.py'")
    print("    5. Cleanup: PROD_DATABASE_URL=\"...\" \\")
    print("                  python3 scripts/db/cleanup_prod_db.py")
    print()

    engine.dispose()


if __name__ == "__main__":
    main()
