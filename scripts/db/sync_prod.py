#!/usr/bin/env python3
"""
Dev → prod incremental sync via direct SQLAlchemy connection (no FDW).

Only copies rows that have changed since the last sync, using updated_at
timestamps.  Checkpoints are stored in a _sync_meta table on prod.

First run performs a full sync for all tables.  Subsequent runs only copy
rows where updated_at > last_sync_at for that table.

Usage:
    source .env && python3 -u scripts/db/sync_prod.py
    source .env && python3 -u scripts/db/sync_prod.py --reconcile      # + delete stale prod rows
    source .env && python3 -u scripts/db/sync_prod.py --reconcile-dry-run
    source .env && python3 -u scripts/db/sync_prod.py --reconcile-only --reconcile-dry-run

Environment:
    DATABASE_URL          Dev database  (Windows via Tailscale, or local)
    PROD_DATABASE_URL     Prod database (DO Managed PostgreSQL)
    BATCH_SIZE            Rows per chunk (default: 2000)
    BATCH_SLEEP_MS        Sleep between chunks, milliseconds (default: 100)
    SYNC_MODE             "incremental" (default) or "full" (force full resync)

ARCHITECTURE
    This module is a THIN FACADE: the guarded production entry point. It runs the
    fail-closed production interlock, resolves URLs, constructs engines, and then
    delegates the mechanism to ``db.sync_runtime.run_sync``, which takes explicit
    engines and has no CLI, no URL resolution and no credentials.

    The mechanism is therefore directly testable without going through — or
    bypassing — this guarded boundary.

    TRANSACTION SCOPE: commits happen per chunk and per table inside
    ``_upsert_table``. There is NO single transaction spanning a parent and its
    dependents; referentially safe staged execution is what is provided, not
    atomicity.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

# Make the shared database-tier authority and the extracted sync modules importable
# however this tool is invoked (module import or direct script execution).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from db.tier import (  # noqa: E402
    DEVELOPMENT,
    PRODUCTION,
    TierError,
    resolve_role_url,
)
from sqlalchemy import Engine, Connection, create_engine, inspect as sa_inspect, text
try:
    from kg import stage2_parentage_contract as parentage_contract
except ImportError:  # direct execution via a different sys.path
    from scripts.kg import stage2_parentage_contract as parentage_contract

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sync")

# ── Extracted definitions, re-exported for import compatibility ──
# The implementation now lives in focused modules; this facade keeps every
# historical name importable from db.sync_prod unchanged.
from db.sync_declarations import (
    ALL_SYNC_TABLES, AUTO_COLUMNS, BATCH_SIZE, BATCH_SLEEP_S, EXCLUDED_TABLES,
    FULL_SYNC_TABLES, LOCK_ID, RECONCILE_ORDER, SYNC_MODE, _ENTITY_TAXONOMY_TABLES,
    _EVENT_TABLES,
)
from db.sync_targets import _mask_url, _resolve_dev_url, _resolve_prod_url
from db.sync_schema import (
    _column_intersection, _ensure_entity_taxonomy, _ensure_event_tables,
    _ensure_updated_at_on_prod, _pk_cols, _quoted_cols, _table_has_updated_at,
)
from db.sync_meta import (
    _ensure_sync_meta_table, _get_last_sync, _set_last_sync,
)
from db.sync_uniques import (
    _cleanup_multi_column_unique, _cleanup_secondary_conflicts,
    _cleanup_single_column_unique, _detect_secondary_uniques,
)
from db.sync_reconcile import _reconcile, _reconcile_table
from db.sync_upsert import _upsert_table
from db.sync_validate import _sync_status, _validate
from db.sync_runtime import _bootstrap_prod_schema, run_sync, select_sync_tables


# ── Guarded production entry point ──

def _parse_tables(value: str | None) -> list[str] | None:
    """Parse --tables into a list, or None when unset."""
    if not value:
        return None
    return [name.strip() for name in value.split(",") if name.strip()]


def _interlock_verdict(reconcile_dry_run: bool = False, tables=None, mode=None,
                       authorization_id: str | None = None):
    """Run the fail-closed production interlock. Returns (allowed, verdict).

    Imported lazily so the module stays importable in contexts without scripts/ops
    on the path, and FAILS CLOSED (refuses) if the interlock cannot be loaded.

    The declared SCOPE is the exact WRITE SET this run will touch, derived from
    the same `select_sync_tables` the sync loop uses — so the request can never
    declare less than the run writes. A scope that cannot be computed is itself a
    refusal, never a silent default.

    ``mode`` is the execution mode this run will ACTUALLY use (`upsert`,
    `reconcile`, `reconcile-only`, `schema-only`, `bootstrap-schema`). It is
    declared for every production-mutating path, because a table scope alone cannot
    separate an upsert from a delete or a schema change at this entry point.
    """
    from pathlib import Path as _Path
    _ops_dir = _Path(__file__).resolve().parents[1] / "ops"
    if str(_ops_dir) not in sys.path:
        sys.path.insert(0, str(_ops_dir))
    op = "OP-STATUS" if reconcile_dry_run else "OP-RECON"
    try:
        declared = select_sync_tables(tables)
    except ValueError as exc:
        return False, {"status": "REFUSED", "code": "SCOPE_INVALID",
                       "reason": f"invalid sync table scope: {exc}",
                       "entry_point": "scripts/db/sync_prod.py"}
    try:
        from production_interlock import check as _interlock_check
    except Exception as exc:  # fail closed: no interlock means no production
        return False, {"status": "REFUSED", "code": "INTERLOCK_UNAVAILABLE",
                       "reason": f"production interlock unavailable ({exc})"}
    if authorization_id is None:
        verdict = _interlock_check(
            op, entry_point="scripts/db/sync_prod.py", scope=declared, mode=mode)
    else:
        verdict = _interlock_check(
            op, entry_point="scripts/db/sync_prod.py", scope=declared, mode=mode,
            authorization_id=authorization_id)
    return verdict.get("status") == "ALLOWED", verdict


def execution_mode_for(reconcile: bool = False, reconcile_only: bool = False,
                       schema_only: bool = False, bootstrap_schema: bool = False,
                       reconcile_dry_run: bool = False) -> str | None:
    """The execution mode this invocation will ACTUALLY use.

    Declared truthfully to the interlock: a table scope alone cannot distinguish an
    insert-or-update from a delete or a schema change at this entry point. Mutually
    exclusive flags are refused rather than silently resolved by precedence, so the
    declared mode is never ambiguous. A read-only preview declares no mutation mode
    (the interlock classifies OP-STATUS as non-mutating).
    """
    requested = [name for name, flag in (
        ("reconcile", reconcile), ("reconcile-only", reconcile_only),
        ("schema-only", schema_only), ("bootstrap-schema", bootstrap_schema))
        if flag]
    if len(requested) > 1:
        raise ValueError(f"conflicting execution modes {requested}")
    if reconcile_dry_run:
        return None
    return requested[0] if requested else "upsert"


def main(reconcile: bool = False, reconcile_only: bool = False,
         reconcile_dry_run: bool = False, schema_only: bool = False,
         bootstrap_schema: bool = False, tables=None,
         authorization_id: str | None = None) -> int:
    # ── --tables narrows the WRITE SET, before anything else ─────────────────
    # Refused for modes that touch tables beyond the requested set: reconcile
    # walks its own fixed children-first order (db/sync_reconcile.RECONCILE_ORDER)
    # and the schema modes perform bootstrap/DDL work outside the sync set. A
    # narrower declaration there would UNDERSTATE what the run writes, which is
    # exactly what the scope check exists to prevent.
    if tables is not None and (reconcile or reconcile_only or schema_only
                               or bootstrap_schema):
        sys.stderr.write(
            "REFUSED: --tables cannot be combined with --reconcile, "
            "--reconcile-only, --schema-only or --bootstrap-schema; those modes "
            "touch tables outside the requested set.\n"
        )
        return 3
    try:
        select_sync_tables(tables)
    except ValueError as exc:
        sys.stderr.write(f"REFUSED: invalid --tables: {exc}\n")
        return 3

    # ── the execution MODE this run will actually use ────────────────────────
    # Declared truthfully and fail-closed; see execution_mode_for().
    try:
        mode = execution_mode_for(reconcile, reconcile_only, schema_only,
                                  bootstrap_schema, reconcile_dry_run)
    except ValueError as exc:
        sys.stderr.write(
            f"REFUSED: {exc}; pass exactly one mode flag so the declared mode is "
            "unambiguous.\n")
        return 3

    # ── Production interlock (FAIL CLOSED) ───────────────────────────────────
    # MUST run before URL resolution and before any engine is created, so no
    # production connection is opened unless the interlock allows it.
    # --reconcile-dry-run is the only read-only mode in this family; every other
    # path through main() can write to production.
    allowed, verdict = _interlock_verdict(
        reconcile_dry_run, tables, mode, authorization_id)
    if not allowed:
        sys.stderr.write(json.dumps(verdict, sort_keys=True) + "\n")
        sys.stderr.write("REFUSED: production interlock blocked this sync.\n")
        return 3

    dev_url = _resolve_dev_url()
    prod_url = _resolve_prod_url()

    dev_engine = create_engine(dev_url, pool_size=2, connect_args={"connect_timeout": 10})
    prod_engine = create_engine(prod_url, pool_size=2, connect_args={"connect_timeout": 10})

    return run_sync(
        dev_engine, prod_engine,
        reconcile=reconcile,
        reconcile_only=reconcile_only,
        reconcile_dry_run=reconcile_dry_run,
        schema_only=schema_only,
        bootstrap_schema=bootstrap_schema,
        tables=tables,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dev → prod incremental database sync"
    )
    parser.add_argument(
        "--schema-only", action="store_true",
        help="Run idempotent production schema bootstrap and validation; do not upsert or reconcile data",
    )
    parser.add_argument(
        "--bootstrap-schema", action="store_true",
        help="Explicitly run production schema/bootstrap work before data sync",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print sync lag report and exit (no data transfer)",
    )
    parser.add_argument(
        "--reconcile", action="store_true",
        help="After upserts, delete prod rows whose PK is absent from dev "
             "(delete propagation for rows removed on dev)",
    )
    parser.add_argument(
        "--reconcile-only", action="store_true",
        help="Skip upserts; only reconcile (delete stale prod rows) + validate",
    )
    parser.add_argument(
        "--reconcile-dry-run", action="store_true",
        help="Preview which prod rows would be deleted, without deleting",
    )
    parser.add_argument(
        "--tables", default=None, metavar="T1,T2,...",
        help="Restrict the sync WRITE SET to these tables (comma-separated). The "
             "interlock scope declared to production is exactly this set, so the "
             "request can never declare less than the run writes. Not available "
             "with --reconcile/--reconcile-only/--schema-only/--bootstrap-schema.",
    )
    parser.add_argument(
        "--authorization-id", default=None,
        help="Select one exact operation authorization. Required by managed daily "
             "and maintenance wrappers to avoid matching an unintended grant.",
    )
    args = parser.parse_args()

    if args.status:
        allowed, verdict = _interlock_verdict(reconcile_dry_run=True)
        if not allowed:
            sys.stderr.write(json.dumps(verdict, sort_keys=True) + "\n")
            sys.stderr.write("REFUSED: production interlock blocked this status read.\n")
            sys.exit(3)
        dev_url = _resolve_dev_url()
        prod_url = _resolve_prod_url()
        dev_engine = create_engine(
            dev_url, pool_size=2, connect_args={"connect_timeout": 10}
        )
        prod_engine = create_engine(
            prod_url, pool_size=2, connect_args={"connect_timeout": 10}
        )
        _sync_status(dev_engine, prod_engine)
        dev_engine.dispose()
        prod_engine.dispose()
        sys.exit(0)

    raise SystemExit(main(
        reconcile=args.reconcile,
        reconcile_only=args.reconcile_only,
        reconcile_dry_run=args.reconcile_dry_run,
        schema_only=args.schema_only,
        bootstrap_schema=args.bootstrap_schema,
        tables=_parse_tables(args.tables),
        authorization_id=args.authorization_id,
    ))
