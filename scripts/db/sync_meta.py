#!/usr/bin/env python3
"""Sync checkpoint metadata stored in the ``_sync_meta`` table on prod.

Extracted from ``scripts/db/sync_prod.py`` as a behavior-preserving split; the
code below is unchanged.  ``scripts/db/sync_prod.py`` remains the CLI facade.
"""

from __future__ import annotations

import logging
import os
import sys

# Make the shared modules importable however this module is invoked.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


from datetime import datetime, timezone
from sqlalchemy import text, inspect as sa_inspect
log = logging.getLogger("sync")




# ── Sync metadata (_sync_meta table on prod) ──


def _ensure_sync_meta_table(prod_engine):
    """Create _sync_meta table on prod if it doesn't exist."""
    inspector = sa_inspect(prod_engine)
    if "_sync_meta" in inspector.get_table_names():
        return
    log.info("  Creating _sync_meta table on prod...")
    with prod_engine.begin() as c:
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS _sync_meta (
                table_name   TEXT PRIMARY KEY,
                last_sync_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
    log.info("    done")




def _get_last_sync(prod_engine, table: str) -> datetime | None:
    """Return the last sync timestamp for a table, or None if never synced."""
    with prod_engine.connect() as c:
        row = c.execute(
            text("SELECT last_sync_at FROM _sync_meta WHERE table_name = :t"),
            {"t": table},
        ).fetchone()
    return row[0] if row else None




def _set_last_sync(prod_engine, table: str, when: datetime | None = None):
    """Update (or insert) the last-sync timestamp for a table."""
    when = when or datetime.now(timezone.utc)
    with prod_engine.begin() as c:
        c.execute(
            text("""
                INSERT INTO _sync_meta (table_name, last_sync_at, updated_at)
                VALUES (:t, :w, NOW())
                ON CONFLICT (table_name) DO UPDATE
                SET last_sync_at = :w, updated_at = NOW()
            """),
            {"t": table, "w": when},
        )
