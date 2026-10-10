#!/usr/bin/env python3
"""Incremental batched upsert/copy of changed rows from dev to prod.

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


import time
import json
from sqlalchemy import text
from db.sync_declarations import (BATCH_SIZE, BATCH_SLEEP_S)
from db.sync_meta import (_get_last_sync, _set_last_sync)
from db.sync_schema import (_pk_cols, _quoted_cols)
from db.sync_uniques import (_cleanup_secondary_conflicts, _detect_secondary_uniques)
log = logging.getLogger("sync")




# ── Table sync (incremental) ──


def _upsert_table(
    dev_engine, prod_engine, table: str, cols: list[str],
    *, is_full_sync: bool = False, advance_checkpoint: bool = True,
):
    """Upsert changed rows from dev → prod for one table.

    When is_full_sync is True (or no checkpoint exists), syncs ALL rows.
    Otherwise only syncs rows where updated_at > last_sync_at.
    """
    pk_cols = _pk_cols(prod_engine, table)
    pk_sql = _quoted_cols(pk_cols)
    col_sql = _quoted_cols(cols)

    secondary_uniques = _detect_secondary_uniques(prod_engine, table)

    update_set = ", ".join(
        f'"{c}" = EXCLUDED."{c}"' for c in cols if c not in pk_cols
    )
    conflict_clause = (
        f"ON CONFLICT ({pk_sql}) DO UPDATE SET {update_set}"
        if update_set else "ON CONFLICT DO NOTHING"
    )

    # Determine sync range
    last_sync = _get_last_sync(prod_engine, table) if not is_full_sync else None
    incremental = last_sync is not None and not is_full_sync

    # Count rows to sync
    with dev_engine.connect() as c:
        if incremental:
            count_sql = text(
                f'SELECT COUNT(*) FROM public."{table}" WHERE updated_at > :since'
            )
            total = c.execute(count_sql, {"since": last_sync}).scalar()
        else:
            total = c.execute(
                text(f'SELECT COUNT(*) FROM public."{table}"')
            ).scalar()

    if total == 0:
        log.info("  %-35s  no new rows (last_sync=%s)", table,
                 last_sync.isoformat() if last_sync else "never")
        # Advance the checkpoint even when nothing was copied. Near-static
        # reference tables (public_bodies, jurisdictions, persons, …) would
        # otherwise keep an old last_sync_at forever and trip the digest's
        # prod-staleness check despite dev == prod (false alarm). Safe: the
        # daily sync runs after the scrape finishes, so no concurrent dev
        # writes race the checkpoint; anything updated later is > now and is
        # picked up on the next run.
        if incremental and advance_checkpoint:
            _set_last_sync(prod_engine, table)
            log.info("    checkpoint advanced (no changes to copy)")
        return 0

    # Count rows on prod (for logging delta)
    with prod_engine.connect() as c:
        prod_before = c.execute(
            text(f'SELECT COUNT(*) FROM public."{table}"')
        ).scalar()

    if incremental:
        log.info("  %-35s  dev=%d new since %s  prod=%d  (upsert %d at a time)",
                 table, total, last_sync.strftime("%Y-%m-%d %H:%M:%S"),
                 prod_before, BATCH_SIZE)
    else:
        log.info("  %-35s  dev=%d  prod=%d  (full sync, %d at a time)",
                 table, total, prod_before, BATCH_SIZE)

    # Build the SELECT query with optional filter
    if incremental:
        select_sql = text(
            f'SELECT {col_sql} FROM public."{table}"\n'
            f'  WHERE updated_at > :since\n'
            f'  ORDER BY {pk_sql}\n'
            f'  LIMIT :limit OFFSET :offset'
        )
    else:
        select_sql = text(
            f'SELECT {col_sql} FROM public."{table}"\n'
            f'  ORDER BY {pk_sql}\n'
            f'  LIMIT :limit OFFSET :offset'
        )

    offset = 0
    chunk_count = 0
    skipped_total = 0
    while offset < total:
        try:
            # Read chunk from dev
            with dev_engine.connect() as c:
                params = {"limit": BATCH_SIZE, "offset": offset}
                if incremental:
                    params["since"] = last_sync
                chunk = c.execute(select_sql, params).mappings().fetchall()

            if not chunk:
                break

            # Clean secondary unique conflicts in prod for this chunk
            if secondary_uniques:
                _cleanup_secondary_conflicts(
                    prod_engine, table, chunk, secondary_uniques, pk_cols
                )

            # Bulk INSERT
            col_names = list(chunk[0].keys())
            values_clause_parts = []
            params = {}
            for i, row in enumerate(chunk):
                placeholders = [f":r{i}_{c}" for c in col_names]
                values_clause_parts.append(f"({', '.join(placeholders)})")
                for c in col_names:
                    v = row[c]
                    params[f"r{i}_{c}"] = json.dumps(v) if isinstance(v, (dict, list)) else v

            values_clause = ",\n  ".join(values_clause_parts)

            with prod_engine.begin() as c:
                c.execute(
                    text(
                        f'INSERT INTO public."{table}" ({col_sql})\n'
                        f'  VALUES\n  {values_clause}\n'
                        f'  {conflict_clause}'
                    ),
                    params,
                )

            offset += BATCH_SIZE
            chunk_count += 1
            if chunk_count % 5 == 0:
                log.info("    chunk %3d: %6d / %d", chunk_count, min(offset, total), total)
            time.sleep(BATCH_SLEEP_S)

        except Exception as e:
            log.error("    FAILED at offset %d: %s", offset, e)
            skipped = 0
            for row in chunk:
                try:
                    with prod_engine.begin() as c:
                        c.execute(
                            text(
                                f'INSERT INTO public."{table}" ({col_sql})\n'
                                f'  VALUES ({", ".join(f":{k}" for k in row.keys())})\n'
                                f"  {conflict_clause}"
                            ),
                            {k: json.dumps(v) if isinstance(v, dict) else v for k, v in dict(row).items()},
                        )
                except Exception as e2:
                    row_id = row.get("id", "?")
                    log.warning("    Skipped row id=%s: %s", row_id, e2)
                    skipped += 1
            if skipped:
                log.warning("    Row-by-row: %d row(s) skipped", skipped)
            skipped_total += skipped
            offset += BATCH_SIZE
            chunk_count += 1
            time.sleep(BATCH_SLEEP_S * 2)

    # Reset sequence — only for tables that actually have an id column.
    # (Join tables like event_participants have a composite PK and no id;
    #  MAX(id) would fail and abort the transaction, killing the count below.)
    if "id" in pk_cols:
        with prod_engine.connect() as c:
            try:
                seq_name = f"{table}_id_seq"
                max_id = c.execute(
                    text(f'SELECT COALESCE(MAX(id), 0) FROM public."{table}"')
                ).scalar()
                if max_id and max_id > 0:
                    c.execute(text(f"SELECT setval('{seq_name}', :max_id)"), {"max_id": max_id})
                c.commit()
            except Exception as e:
                c.rollback()
                log.warning("  sequence reset skipped: %s", e)

    with prod_engine.connect() as c:
        prod_after = c.execute(
            text(f'SELECT COUNT(*) FROM public."{table}"')
        ).scalar()
        skip_note = f", {skipped_total} skipped" if skipped_total else ""
        log.info("    done: prod now %d rows (%+d)%s",
                 prod_after, prod_after - prod_before, skip_note)

    # Record sync checkpoint — ONLY when nothing was skipped, so failed rows
    # are retried on the next run instead of being permanently bypassed.
    if skipped_total == 0 and advance_checkpoint:
        _set_last_sync(prod_engine, table)
    elif skipped_total == 0:
        log.info("  Checkpoint deferred for %s until reference validation", table)
    else:
        log.warning("  Checkpoint NOT advanced for %s — %d row(s) skipped (will retry)",
                    table, skipped_total)

    return skipped_total
