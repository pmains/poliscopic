#!/usr/bin/env python3
"""Internal sync runtime — the sync MECHANISM, directly testable.

Extracted from ``scripts/db/sync_prod.py`` so the mechanism can be exercised by
tests WITHOUT going through the guarded production entry point, and without
bypassing it either. This mirrors the deploy architecture: a guarded CLI boundary
plus a directly testable internal runtime.

WHAT THIS MODULE DELIBERATELY DOES NOT HAVE
    * no CLI / ``if __name__ == "__main__"`` block — it is not independently
      executable and is not an operational command,
    * no production defaults,
    * no URL resolution (``_resolve_dev_url`` / ``_resolve_prod_url``),
    * no engine construction, no credentials, no environment reads,
    * no production interlock — that lives in the guarded entry point.

    Callers supply already-constructed engines. ``scripts/db/sync_prod.py`` is the
    only production caller: it runs the interlock, resolves URLs, builds the
    engines, then delegates here.

TRANSACTION SCOPE (stated honestly)
    This runtime commits per chunk inside ``_upsert_table`` and per table; there is
    NO single transaction spanning a parent and its dependents. Providing one needs
    a larger redesign. Referentially safe staged execution is what is implemented
    and proven here, not atomicity.
"""

from __future__ import annotations

import logging
import os
import sys
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from sqlalchemy import text  # noqa: E402

from db.sync_declarations import (  # noqa: E402
    ALL_SYNC_TABLES,
    AUTO_COLUMNS,
    BATCH_SIZE,
    BATCH_SLEEP_S,
    EXCLUDED_TABLES,
    FULL_SYNC_TABLES,
    LOCK_ID,
    SYNC_MODE,
)
from db.sync_reconcile import _reconcile  # noqa: E402
from db.sync_schema import _column_intersection  # noqa: E402
from db.sync_upsert import _upsert_table  # noqa: E402
from db.sync_validate import _validate  # noqa: E402
from db.sync_meta import _get_last_sync, _set_last_sync  # noqa: E402
from db.sync_reference import (  # noqa: E402
    BODY_CODE_COLUMNS,
    DEPENDENT_TABLES,
    PARENT_TABLES,
    assert_parity,
    assert_parents_synced,
    assert_target_parent_coverage,
    dangling_counts,
    is_sentinel,
    newly_dangling_problems,
    scoped_dangling_problems,
)

log = logging.getLogger("sync")


def _scoped_reference_codes(dev_engine) -> list[str]:
    """Non-sentinel body codes referenced by dependent rows on dev.

    This is the repair SCOPE for the reference invariant. Sentinels ('', '__skip__')
    are excluded — they are invalid/unresolved, never valid references and never
    promoted. A query failure PROPAGATES: it must become a validation failure, not
    a silent pass.
    """
    from sqlalchemy import inspect as _inspect

    present = set(_inspect(dev_engine).get_table_names())
    codes: set[str] = set()
    with dev_engine.connect() as conn:
        for table in DEPENDENT_TABLES:
            if table not in present:
                continue
            columns = {c["name"] for c in _inspect(dev_engine).get_columns(table)}
            for column in BODY_CODE_COLUMNS:
                if column not in columns:
                    continue
                rows = conn.execute(
                    text(
                        f'SELECT DISTINCT "{column}" FROM "{table}" '
                        f'WHERE "{column}" IS NOT NULL'
                    )
                ).fetchall()
                for (value,) in rows:
                    if not is_sentinel(value):
                        codes.add(str(value).strip())
    return sorted(codes)


def _reference_postconditions(dev_engine, prod_engine, baseline=None) -> list[str]:
    """Post-sync integrity checks for BOTH live representations.

    Representation 1: ``<table>.body``/``body_code`` -> ``public_bodies.body_code``
    Representation 2: ``<table>.public_body_id``       -> ``public_bodies.id``

    This exists because table-count validation alone does not prove referential
    integrity. Query failures propagate as problems.
    """
    if baseline is not None:
        return newly_dangling_problems(prod_engine, baseline)
    scope = _scoped_reference_codes(dev_engine)
    return scoped_dangling_problems(prod_engine, scope)


def _bootstrap_prod_schema(prod_engine) -> None:
    """Run explicitly requested production schema/bootstrap work only."""
    from db.sync_schema import (
        _ensure_entity_taxonomy,
        _ensure_event_tables,
        _ensure_updated_at_on_prod,
    )
    from db.sync_meta import _ensure_sync_meta_table

    _ensure_sync_meta_table(prod_engine)
    _ensure_updated_at_on_prod(prod_engine)
    _ensure_event_tables(prod_engine)
    _ensure_entity_taxonomy(prod_engine)
    from db.kg_integrity_schema import ensure_kg_integrity_schema

    ensure_kg_integrity_schema(prod_engine)


def select_sync_tables(restriction=None) -> list[str]:
    """The exact table set a run will WRITE.

    The production interlock scope declaration and the sync loop must BOTH derive
    from this function, so a caller's declared scope can never understate what the
    run touches. An unknown table name is refused rather than ignored, and the
    declared order is preserved.
    """
    if restriction is None:
        return list(ALL_SYNC_TABLES)
    unknown = sorted({t for t in restriction if t not in set(ALL_SYNC_TABLES)})
    if unknown:
        raise ValueError(f"unknown sync table(s): {unknown}")
    wanted = set(restriction)
    return [t for t in ALL_SYNC_TABLES if t in wanted]


def run_sync(dev_engine, prod_engine, *,
             reconcile: bool = False,
             reconcile_only: bool = False,
             reconcile_dry_run: bool = False,
             schema_only: bool = False,
             bootstrap_schema: bool = False,
             is_full_sync: bool | None = None,
             tables=None) -> int:
    """Run the dev→prod sync against already-constructed engines.

    Takes explicit dependencies: no URL resolution, no engine construction, no
    credentials, no environment reads. Returns the process exit code.

    ``tables`` restricts the WRITE SET to that subset (see ``select_sync_tables``);
    ``None`` means every declared table. It is resolved BEFORE the lock is taken,
    so an invalid restriction refuses before any database work. Reference-parent
    violations still abort (``assert_parents_synced``) rather than widening the
    set — the restriction never weakens a referential gate.
    """
    selected_tables = select_sync_tables(tables)
    if is_full_sync is None:
        is_full_sync = SYNC_MODE == "full"

    # Pure declaration check, deliberately before the lock or any DB mutation.
    assert_parity()

    log.info("Acquiring sync lock on prod...")
    lock_connection = prod_engine.connect()
    acquired = lock_connection.execute(
        text(f"SELECT pg_try_advisory_lock({LOCK_ID})")
    ).scalar()
    if not acquired:
        lock_connection.close()
        log.error("Another sync is already running (lock held)")
        return 1

    total_skipped = 0
    ok = True
    deferred_parent_checkpoints: list[str] = []
    try:
        # Ordinary upserts must introduce zero NEW dangling references. Preserve
        # the historical baseline so unrelated legacy defects neither disappear
        # from observability nor make every future incremental run impossible.
        reference_baseline = (
            dangling_counts(prod_engine)
            if not bootstrap_schema and not schema_only
            else None
        )
        # ── 2. Explicit schema bootstrap only ──
        if bootstrap_schema or schema_only:
            log.info("── Explicit schema bootstrap ──")
            _bootstrap_prod_schema(prod_engine)
        else:
            log.info("── Schema bootstrap skipped (data-only sync) ──")

        # ── 3. Upsert each table ──
        if schema_only:
            log.info("── Skipping data writes (--schema-only) ──")
        elif reconcile_only:
            log.info("── Skipping upserts (--reconcile-only) ──")
        else:
            run_type = "full" if is_full_sync else "incremental"
            log.info("── Syncing tables (%s) ──", run_type)

            skipped_by_table: dict[str, int] = {}
            for table in selected_tables:
                if table in EXCLUDED_TABLES:
                    continue

                # ── Reference boundary (FAIL CLOSED) ────────────────────────
                # Before writing any dependent, every required parent must have
                # been applied WITHOUT skipped rows. Ordinary sync commits per
                # table, so this is an ordering guarantee, not a transaction: it
                # removes the dangerous direction (dependent written without its
                # parent) at the cost of allowing parent-present/dependent-absent,
                # which dangles nothing. Aborting also leaves the parent
                # checkpoint unadvanced, so the skip is retried rather than
                # silently bypassed.
                if table in DEPENDENT_TABLES:
                    assert_parents_synced(table, skipped_by_table)
                    reference_since = (
                        None if is_full_sync or table in FULL_SYNC_TABLES
                        else _get_last_sync(prod_engine, table)
                    )
                    assert_target_parent_coverage(
                        dev_engine, prod_engine, table, since=reference_since
                    )

                cols = _column_intersection(dev_engine, prod_engine, table)
                auto_exclude = AUTO_COLUMNS.get(table, set())
                if auto_exclude:
                    cols = [c for c in cols if c not in auto_exclude]
                if not cols:
                    log.warning("  Skipping %s — no common columns between dev and prod",
                                table)
                    continue

                do_full = is_full_sync or table in FULL_SYNC_TABLES

                t0 = time.time()
                table_skipped = _upsert_table(
                    dev_engine, prod_engine, table, cols, is_full_sync=do_full,
                    advance_checkpoint=table not in PARENT_TABLES,
                )
                if table in PARENT_TABLES:
                    deferred_parent_checkpoints.append(table)
                skipped_by_table[table] = table_skipped
                total_skipped += table_skipped
                elapsed = time.time() - t0
                log.info("    ─ took %.1fs\n", elapsed)

        # ── 3.5 Reconcile: delete stale prod rows (delete propagation) ──
        if not schema_only and (reconcile or reconcile_only):
            mode = "DRY RUN" if reconcile_dry_run else "delete"
            log.info("── Reconcile stale prod rows (%s) ──", mode)
            stale_total = _reconcile(dev_engine, prod_engine, dry_run=reconcile_dry_run)
            if reconcile_dry_run and stale_total:
                log.warning("  Dry run: %d stale row(s) would be deleted", stale_total)
            elif reconcile_dry_run:
                log.info("  Dry run: no stale rows — prod is clean")

        # ── 4. Validate ──
        if reconcile_dry_run:
            _validate(dev_engine, prod_engine)
            log.info("Dry run complete — no changes made")
            return 0
        ok = _validate(dev_engine, prod_engine)

        # ── 4.5 Reference postconditions (both representations) ─────────────
        # Table counts alone do not prove referential integrity. A query failure
        # here is a FAILURE, never swallowed.
        try:
            post_problems = _reference_postconditions(
                dev_engine, prod_engine, reference_baseline
            )
        except Exception as e:
            post_problems = [f"reference postcondition check failed: {e}"]
        if post_problems:
            ok = False
            for problem in post_problems:
                log.error("  ✖ %s", problem)
        else:
            log.info("  ✅ reference postconditions hold (both representations)")

        # Parent checkpoints are evidence that the complete guarded unit passed,
        # not merely that one parent table finished copying.
        if ok and not total_skipped:
            for table in deferred_parent_checkpoints:
                _set_last_sync(prod_engine, table)

    except BaseException as e:
        log.error("Sync failed: %s", e)
        raise
    finally:
        try:
            lock_connection.execute(text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
            log.info("Lock released")
        finally:
            lock_connection.close()

    if not ok:
        log.error("Validation failed — prod row counts differ from dev")
        return 1
    if total_skipped:
        log.error("%d row(s) skipped during sync — inspect log", total_skipped)
        return 1
    return 0


__all__ = ["run_sync", "_bootstrap_prod_schema", "ALL_SYNC_TABLES", "FULL_SYNC_TABLES",
           "EXCLUDED_TABLES", "AUTO_COLUMNS", "LOCK_ID", "BATCH_SIZE", "BATCH_SLEEP_S"]
