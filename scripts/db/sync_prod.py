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
from db.sync_runtime import _bootstrap_prod_schema, run_sync


# ── Guarded production entry point ──

def _interlock_verdict(reconcile_dry_run: bool = False):
    """Run the fail-closed production interlock. Returns (allowed, verdict).

    Imported lazily so the module stays importable in contexts without scripts/ops
    on the path, and FAILS CLOSED (refuses) if the interlock cannot be loaded.
    """
    from pathlib import Path as _Path
    _ops_dir = _Path(__file__).resolve().parents[1] / "ops"
    if str(_ops_dir) not in sys.path:
        sys.path.insert(0, str(_ops_dir))
    try:
        from production_interlock import check as _interlock_check
    except Exception as exc:  # fail closed: no interlock means no production
        return False, {"status": "REFUSED", "code": "INTERLOCK_UNAVAILABLE",
                       "reason": f"production interlock unavailable ({exc})"}
    op = "OP-STATUS" if reconcile_dry_run else "OP-RECON"
    verdict = _interlock_check(op, entry_point="scripts/db/sync_prod.py")
    return verdict.get("status") == "ALLOWED", verdict


def main(reconcile: bool = False, reconcile_only: bool = False,
         reconcile_dry_run: bool = False, schema_only: bool = False,
         bootstrap_schema: bool = False) -> int:
    # ── Production interlock (FAIL CLOSED) ───────────────────────────────────
    # MUST run before URL resolution and before any engine is created, so no
    # production connection is opened unless the interlock allows it.
    # --reconcile-dry-run is the only read-only mode in this family; every other
    # path through main() can write to production.
    allowed, verdict = _interlock_verdict(reconcile_dry_run)
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
    ))
