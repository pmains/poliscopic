#!/usr/bin/env python3
"""Editorial sync: push articles/tags/sources from dev → prod PostgreSQL.

The main dev→prod sync (scripts/db/sync_prod.py) deliberately EXCLUDES
editorial tables (articles, tags, article_tags, article_sources). This
script is the surgical editorial push — the PostgreSQL replacement for the
deleted SQLite-era scripts/editorial_sync.py.

Usage:
    source .env   # DATABASE_URL (dev) + PROD_DATABASE_URL
    .venv/bin/python -u scripts/editorial_sync.py

Tables synced (FK-safe order):
    tags → articles → article_sources → article_tags

Full-table upsert (INSERT ... ON CONFLICT DO UPDATE), no deletes — dev is
the source of truth for editorial content; prod-only rows are preserved.
"""

import logging
import os
import sys
import time

# Make the shared database-tier authority importable however this tool is invoked.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.tier import (  # noqa: E402
    DEVELOPMENT,
    PRODUCTION,
    TierError,
    resolve_role_url,
)
from ops.production_interlock_guard import require_production_interlock  # noqa: E402
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("editorial_sync")

# Table order matters for FK constraints
EDITORIAL_TABLES = [
    "tags",
    "articles",
    "article_sources",
    "article_tags",
]

# Columns that must never be copied (generated / server-maintained)
AUTO_EXCLUDE = {
    "articles": {"search_vector"},
}


def _columns(engine, table):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
        ), {"t": table}).fetchall()
    return [r[0] for r in rows]


def _pk_cols(engine, table):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'public." + table + "'::regclass AND i.indisprimary"
        )).fetchall()
    return [r[0] for r in rows]


def upsert_table(dev, prod, table):
    dev_cols = set(_columns(dev, table))
    prod_cols = set(_columns(prod, table))
    cols = sorted(dev_cols & prod_cols - AUTO_EXCLUDE.get(table, set()))
    pks = _pk_cols(prod, table)
    if not cols or not pks:
        log.warning("  skip %s (cols=%d pks=%s)", table, len(cols), pks)
        return 0

    non_pk = [c for c in cols if c not in pks]
    conflict = ", ".join(f'"{c}"' for c in pks)
    insert_cols = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(f":{c}" for c in cols)

    if non_pk:
        update_set = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in non_pk)
        sql = (f'INSERT INTO public."{table}" ({insert_cols}) VALUES ({placeholders}) '
               f'ON CONFLICT ({conflict}) DO UPDATE SET {update_set}')
    else:
        # All-PK tables (e.g. article_tags) have nothing to update — insert-or-skip
        sql = (f'INSERT INTO public."{table}" ({insert_cols}) VALUES ({placeholders}) '
               f'ON CONFLICT ({conflict}) DO NOTHING')

    total = 0
    with dev.connect() as dc:
        rows = dc.execute(text(f'SELECT {insert_cols} FROM public."{table}"')).mappings().all()
    with prod.begin() as pc:
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            for row in chunk:
                pc.execute(text(sql), dict(row))
            total += len(chunk)
            log.info("    %s: +%d (%d total)", table, len(chunk), total)
    return total


def main():
    require_production_interlock("OP-RECON", "scripts/editorial_sync.py")
    dev_url = os.environ.get("DATABASE_URL", "")
    prod_url = os.environ.get("PROD_DATABASE_URL", "")
    if not dev_url or not prod_url:
        log.error("Set DATABASE_URL and PROD_DATABASE_URL (source .env)")
        return 1

    # Validate both roles before any engine exists, so a swapped pair can never
    # reach a connection.
    try:
        resolve_role_url(DEVELOPMENT, dev_url, label="dev")
        resolve_role_url(PRODUCTION, prod_url, label="prod")
    except TierError as exc:
        log.error("refusing to sync an unvalidated target: %s", exc)
        return 1

    dev = create_engine(dev_url, pool_size=2, connect_args={"connect_timeout": 10})
    prod = create_engine(prod_url, pool_size=2, connect_args={"connect_timeout": 10})

    t0 = time.time()
    for table in EDITORIAL_TABLES:
        log.info("Syncing %s", table)
        n = upsert_table(dev, prod, table)
        log.info("  %s: %d rows upserted", table, n)

    dev.dispose()
    prod.dispose()
    log.info("Editorial sync finished in %.1fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
