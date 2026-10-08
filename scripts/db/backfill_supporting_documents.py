#!/usr/bin/env python3
"""
Backfill supporting_documents (or any sync table) from dev → prod.

Copies rows that exist on dev but are missing on prod (by primary key).
Uses the same direct-connection pattern as sync_prod.py. Idempotent —
safe to re-run (upsert, ON CONFLICT DO UPDATE). Does NOT touch
_sync_meta checkpoints: it is a one-off repair tool, run manually.

Why this exists: the nightly prod sync permanently skipped 1,580
supporting_documents (created 2026-07-27) because _sync_meta.last_sync_at
advanced past them despite failed rows. Incremental sync will never retry
them; this script copies the missing rows so the event tables
(meeting_events → extractions → participants) can finally resolve their
FK violations on the next normal sync run.

Usage:
    source .env && python3 -u scripts/db/backfill_supporting_documents.py
    source .env && python3 -u scripts/db/backfill_supporting_documents.py --table meeting_events --dry-run

Environment:
    DATABASE_URL          Dev database (Windows via Tailscale, or local)
    PROD_DATABASE_URL     Prod database (DO Managed PostgreSQL)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Make the shared database-tier authority importable however this tool is invoked.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))

from db.tier import (  # noqa: E402
    DEVELOPMENT,
    PRODUCTION,
    TierError,
    resolve_role_url,
)
from ops.production_interlock_guard import require_production_interlock  # noqa: E402
from sqlalchemy import create_engine, inspect as sa_inspect, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")

BATCH = 500
AUTO_EXCLUDE = {"search_vector", "rowid"}


def _mask_url(url: str) -> str:
    import re
    return re.sub(r'(//[^:]+:).+?(@)', r'\1****\2', url)


def _pk_cols(engine, table: str) -> list[str]:
    pk = sa_inspect(engine).get_pk_constraint(table)
    if pk and pk.get("constrained_columns"):
        return list(pk["constrained_columns"])
    return ["id"]


def _upsert_rows(prod_engine, table: str, rows: list[dict], pk_cols: list[str]) -> int:
    """Bulk upsert one batch. Serializes dict/list values (JSONB) to JSON."""
    if not rows:
        return 0
    col_names = list(rows[0].keys())
    col_sql = ", ".join(f'"{c}"' for c in col_names)
    pk_sql = ", ".join(f'"{c}"' for c in pk_cols)
    update_set = ", ".join(
        f'"{c}" = EXCLUDED."{c}"' for c in col_names if c not in pk_cols
    )
    conflict = (
        f"ON CONFLICT ({pk_sql}) DO UPDATE SET {update_set}"
        if update_set else "ON CONFLICT DO NOTHING"
    )

    values_parts: list[str] = []
    params: dict = {}
    for i, row in enumerate(rows):
        placeholders = [f":r{i}_{c}" for c in col_names]
        values_parts.append(f"({', '.join(placeholders)})")
        for c in col_names:
            v = row[c]
            params[f"r{i}_{c}"] = json.dumps(v) if isinstance(v, (dict, list)) else v

    with prod_engine.begin() as c:
        c.execute(
            text(
                f'INSERT INTO public."{table}" ({col_sql})\n'
                f"  VALUES\n  {',\n  '.join(values_parts)}\n"
                f"  {conflict}"
            ),
            params,
        )
    return len(rows)


def _reset_sequence(prod_engine, table: str) -> None:
    try:
        with prod_engine.connect() as c:
            max_id = c.execute(
                text(f'SELECT COALESCE(MAX(id), 0) FROM public."{table}"')
            ).scalar()
            if max_id and max_id > 0:
                c.execute(
                    text(f"SELECT setval('{table}_id_seq', :max_id)"),
                    {"max_id": max_id},
                )
    except Exception as e:
        log.warning("  sequence reset skipped: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="supporting_documents")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since", default=None,
                        help="Only consider dev rows with updated_at >= this ISO ts (optional)")
    args = parser.parse_args()

    require_production_interlock(
        "OP-STATUS" if args.dry_run else "OP-RECON",
        "scripts/db/backfill_supporting_documents.py",
        mode=None if args.dry_run else "backfill",
    )

    dev_url = os.environ.get("DATABASE_URL")
    prod_url = os.environ.get("PROD_DATABASE_URL")
    if not dev_url or not prod_url:
        log.error("DATABASE_URL and PROD_DATABASE_URL must be set")
        return 1

    # Validate both roles before any engine exists, so a swapped pair can never
    # reach a connection.
    try:
        resolve_role_url(DEVELOPMENT, dev_url, label="dev")
        resolve_role_url(PRODUCTION, prod_url, label="prod")
    except TierError as exc:
        log.error("refusing to backfill an unvalidated target: %s", exc)
        return 1

    dev_engine = create_engine(dev_url, pool_size=2, connect_args={"connect_timeout": 10})
    prod_engine = create_engine(prod_url, pool_size=2, connect_args={"connect_timeout": 10})
    table = args.table

    log.info("Dev:   %s", _mask_url(dev_url))
    log.info("Prod:  %s", _mask_url(prod_url))
    log.info("Table: %s  dry_run=%s  since=%s", table, args.dry_run, args.since or "all")

    # Column intersection (mirror sync_prod.py behavior)
    dev_cols = {
        c["name"] for c in sa_inspect(dev_engine).get_columns(table)
        if c["name"] not in AUTO_EXCLUDE
    }
    prod_cols = {
        c["name"] for c in sa_inspect(prod_engine).get_columns(table)
        if c["name"] not in AUTO_EXCLUDE
    }
    cols = sorted(dev_cols & prod_cols)
    if not cols:
        log.error("No common columns for %s — aborting", table)
        return 1
    col_sql = ", ".join(f'"{c}"' for c in cols)

    pk_cols = _pk_cols(prod_engine, table)
    log.info("Columns (%d): %s", len(cols), ", ".join(cols))
    log.info("PK: %s", pk_cols)

    # Dev ids (optionally filtered by updated_at)
    with dev_engine.connect() as c:
        if args.since:
            rows = c.execute(
                text(f'SELECT id FROM public."{table}" WHERE updated_at >= :since'),
                {"since": args.since},
            ).fetchall()
        else:
            rows = c.execute(text(f'SELECT id FROM public."{table}"')).fetchall()
    dev_ids = {r[0] for r in rows}

    # Prod ids
    with prod_engine.connect() as c:
        rows = c.execute(text(f'SELECT id FROM public."{table}"')).fetchall()
    prod_ids = {r[0] for r in rows}

    missing = sorted(dev_ids - prod_ids)
    log.info(
        "dev=%d prod=%d missing=%d",
        len(dev_ids), len(prod_ids), len(missing),
    )
    if not missing:
        log.info("✅ Nothing to backfill — prod already has all rows.")
        return 0

    if args.dry_run:
        log.info("DRY RUN — would copy %d rows (first 10 ids: %s)",
                 len(missing), missing[:10])
        return 0

    copied = 0
    t0 = time.time()
    for i in range(0, len(missing), BATCH):
        batch_ids = missing[i:i + BATCH]
        with dev_engine.connect() as c:
            batch_rows = c.execute(
                text(f'SELECT {col_sql} FROM public."{table}" WHERE id = ANY(:ids)'),
                {"ids": batch_ids},
            ).mappings().fetchall()
        n = _upsert_rows(prod_engine, table, [dict(r) for r in batch_rows], pk_cols)
        copied += n
        log.info("  batch %4d..%-4d: %d rows copied  (%d/%d)",
                 batch_ids[0], batch_ids[-1], n, min(i + BATCH, len(missing)), len(missing))
        time.sleep(0.1)

    _reset_sequence(prod_engine, table)

    # Verify
    with prod_engine.connect() as c:
        prod_after = c.execute(
            text(f'SELECT COUNT(*) FROM public."{table}"')
        ).scalar()
    log.info("Done: copied %d rows in %.1fs. Prod %s count now %d (dev=%d).",
             copied, time.time() - t0, table, prod_after, len(dev_ids))
    if prod_after == len(dev_ids):
        log.info("✅ Backfill complete — prod count matches dev.")
        return 0
    log.warning("⚠ Prod count (%d) still differs from dev (%d)",
                prod_after, len(dev_ids))
    return 1


if __name__ == "__main__":
    sys.exit(main())
