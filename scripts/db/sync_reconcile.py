#!/usr/bin/env python3
"""Atomic delete propagation for production rows absent from development.

The caller holds the process-wide production advisory lock.  This module reads
the complete stale-ID plan first, then applies every live deletion in one
transaction so a failure cannot leave a partially reconciled production tree.
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


from sqlalchemy import bindparam, text
from db.sync_declarations import (EXCLUDED_TABLES, RECONCILE_ORDER)
from db.sync_schema import (_pk_cols)
log = logging.getLogger("sync")




_DELETE_BATCH_SIZE = 500


def _plan_reconcile_table(dev_engine, prod_engine, table: str) -> tuple[str, list] | None:
    """Read the exact stale IDs for one table without mutating production.

    Planning every table before opening the deletion transaction means an
    unsupported table or a read failure cannot leave earlier tables partially
    reconciled.  The caller owns the production advisory lock for this whole
    operation (``sync_prod.main`` keeps that lock session alive).
    """
    pk_cols = _pk_cols(prod_engine, table)
    if len(pk_cols) != 1:
        log.info("  %-35s  skip reconcile (composite PK)", table)
        return None
    pk = pk_cols[0]

    with dev_engine.connect() as c:
        dev_ids = {r[0] for r in c.execute(
            text(f'SELECT "{pk}" FROM public."{table}"'))}
    with prod_engine.connect() as c:
        prod_ids = {r[0] for r in c.execute(
            text(f'SELECT "{pk}" FROM public."{table}"'))}

    stale = sorted(prod_ids - dev_ids)
    if not stale:
        return None
    log.info("  %-35s  %d stale prod row(s) to delete", table, len(stale))
    return pk, stale


def _delete_planned_ids(connection, table: str, pk: str, stale_ids: list) -> int:
    """Delete a precomputed table plan on the caller's transaction.

    A changed row count is a concurrent-drift signal.  Raise rather than
    accepting a partial result so the surrounding transaction rolls back every
    prior table/chunk deletion as well.
    """
    statement = text(
        f'DELETE FROM public."{table}" WHERE "{pk}" IN :ids'
    ).bindparams(bindparam("ids", expanding=True))
    deleted = 0
    for offset in range(0, len(stale_ids), _DELETE_BATCH_SIZE):
        chunk = stale_ids[offset:offset + _DELETE_BATCH_SIZE]
        result = connection.execute(statement, {"ids": chunk})
        if result.rowcount != len(chunk):
            raise RuntimeError(
                f"reconcile delete drift for {table}: expected {len(chunk)} rows, "
                f"deleted {result.rowcount}"
            )
        deleted += result.rowcount
    return deleted


def _reconcile_table(dev_engine, prod_engine, table: str, dry_run: bool = False) -> int:
    """Reconcile one table atomically (compatibility helper).

    Returns number of rows deleted (or that WOULD be deleted in dry-run).
    Tables with composite PKs are skipped (logged) — none of the stale
    tables identified so far use one.
    """
    planned = _plan_reconcile_table(dev_engine, prod_engine, table)
    if planned is None:
        return 0
    pk, stale_ids = planned
    if dry_run:
        return len(stale_ids)
    with prod_engine.begin() as connection:
        deleted = _delete_planned_ids(connection, table, pk, stale_ids)
    if deleted:
        log.info("    ─ deleted %d stale prod row(s) from %s", deleted, table)
    return deleted




def _reconcile(dev_engine, prod_engine, dry_run: bool = False) -> int:
    """Plan all stale rows, then delete all of them in one transaction.

    Dry-run retains its old read-only behavior.  Live reconciliation plans the
    entire child-first scope before the first delete and never catches deletion
    errors, so a failure rolls back every production deletion and propagates to
    the sync caller.
    """
    plans: list[tuple[str, str, list]] = []
    for table in RECONCILE_ORDER:
        if table in EXCLUDED_TABLES:
            continue
        planned = _plan_reconcile_table(dev_engine, prod_engine, table)
        if planned is not None:
            pk, stale_ids = planned
            plans.append((table, pk, stale_ids))

    total = sum(len(stale_ids) for _table, _pk, stale_ids in plans)
    if dry_run:
        log.info("  Reconcile (dry run): %d stale row(s) would be deleted", total)
        return total

    deleted = 0
    with prod_engine.begin() as connection:
        for table, pk, stale_ids in plans:
            deleted += _delete_planned_ids(connection, table, pk, stale_ids)
    if deleted != total:
        raise RuntimeError(
            f"reconcile accounting mismatch: planned {total} stale row(s), deleted {deleted}"
        )
    log.info("  Reconcile %s: %d stale row(s) %s",
             "", deleted, "deleted")
    return deleted
