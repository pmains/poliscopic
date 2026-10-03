#!/usr/bin/env python3
"""Post-sync validation and the sync lag/status report.

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


from sqlalchemy import text
from db.sync_declarations import (ALL_SYNC_TABLES, BATCH_SIZE, BATCH_SLEEP_S, EXCLUDED_TABLES)
from db.sync_meta import (_get_last_sync)
from db.sync_schema import (_table_has_updated_at)
from db.sync_targets import (_mask_url)
log = logging.getLogger("sync")




# ── Validation ──


def _validate(dev_engine, prod_engine):
    """Post-sync sanity checks for an upsert-only propagation lane.

    Production may legitimately contain historical rows no longer present in dev:
    this lane is explicitly forbidden from deleting them. A production deficit is
    a failure; a production surplus is reported but is not an upsert failure.
    """
    log.info("── Post-sync validation ──")
    ok = True

    for table in ALL_SYNC_TABLES:
        try:
            with dev_engine.connect() as c:
                dev_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()
            with prod_engine.connect() as c:
                prod_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()
            status = "✅" if dev_cnt == prod_cnt else ("ℹ" if prod_cnt > dev_cnt else "⚠")
            if prod_cnt < dev_cnt:
                ok = False
            log.info("  %s %-35s  dev=%7d  prod=%7d", status, table, dev_cnt, prod_cnt)
        except Exception as e:
            log.warning("  ⚠ %-35s  error: %s", table, e)
            ok = False

    if ok:
        log.info("  ✅ Production contains every dev row by count")
    else:
        log.warning("  ⚠ Some production tables have fewer rows than dev")
    return ok




# ── Main ──


def _sync_status(dev_engine, prod_engine):
    """Print a status report showing sync lag per table."""
    print(f"\n{'=' * 72}")
    print(f"  Dev → Prod Sync Status")
    print(f"  DEV:  {_mask_url(str(dev_engine.url))}")
    print(f"  PROD: {_mask_url(str(prod_engine.url))}")
    print(f"{'=' * 72}")

    header = (
        f"  {'Table':<32s} {'Dev':>7} {'Prod':>7} {'Delta':>7}"
        f"  {'Pending':>7}  {'Last Synced'}"
    )
    print(header)
    print(f"  {'-' * len(header)}")

    total_pending = 0
    total_dev = 0
    total_prod = 0

    for table in ALL_SYNC_TABLES:
        if table in EXCLUDED_TABLES:
            continue

        try:
            with dev_engine.connect() as c:
                dev_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()

            with prod_engine.connect() as c:
                prod_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()

            # Rows changed since last sync
            last_sync = _get_last_sync(prod_engine, table)
            if last_sync and _table_has_updated_at(dev_engine, table):
                with dev_engine.connect() as c:
                    pending = c.execute(
                        text(
                            f'SELECT COUNT(*) FROM public."{table}"'
                            f' WHERE updated_at > :since'
                        ),
                        {"since": last_sync},
                    ).scalar()
            else:
                pending = dev_cnt  # full sync needed

            total_pending += pending
            total_dev += dev_cnt
            total_prod += prod_cnt

            delta = dev_cnt - prod_cnt
            last_sync_str = (
                last_sync.strftime("%Y-%m-%d %H:%M") if last_sync else "(never)"
            )

            flag = " ⚠" if pending > 1000 else ""
            print(
                f"  {table:<32s} {dev_cnt:>7} {prod_cnt:>7} {delta:+>7}"
                f"  {pending:>7}{flag}  {last_sync_str}"
            )

        except Exception as e:
            brief = e.args[0] if e.args else str(e)
            print(f"  {table:<32s}  {'ERROR':>7}  {brief[:80]}")

    print(f"  {'─' * len(header)}")
    print(
        f"  {'TOTAL':<32s} {total_dev:>7} {total_prod:>7}"
        f"  {total_pending:>7}  {'':12s}"
    )
    print(f"{'=' * 72}\n")

    if total_pending == 0:
        print("  ✅ Dev and prod are in sync.")
    else:
        tail_count = min(total_pending, 99999)
        print(f"  📦 {total_pending} row(s) pending sync.")
        print(f"     BATCH_SIZE={BATCH_SIZE}  BATCH_SLEEP_MS={int(BATCH_SLEEP_S * 1000)}")
        print(f"     Estimated chunks: {(total_pending + BATCH_SIZE - 1) // BATCH_SIZE}")
        print(f"     Estimated time:  ~{(total_pending // BATCH_SIZE) * BATCH_SLEEP_S + 1}s")
    print()
