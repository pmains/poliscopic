#!/usr/bin/env python3
"""Secondary unique-constraint detection and conflict cleanup for sync upserts.

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


import re
from sqlalchemy import Connection, Engine, text, inspect as sa_inspect
from db.sync_schema import (_pk_cols)
log = logging.getLogger("sync")




# ── Secondary unique constraint handling ──


def _detect_secondary_uniques(engine, table: str) -> list[list[str]]:
    inspector = sa_inspect(engine)
    pk_cols = set(_pk_cols(engine, table))
    uniques = []
    for ix in inspector.get_indexes(table):
        cols = list(ix["column_names"])
        if ix.get("unique") and set(cols) != pk_cols:
            uniques.append(cols)
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT conname, pg_get_constraintdef(oid) "
            f"FROM pg_constraint WHERE conrelid = '{table}'::regclass "
            f"AND contype = 'u'"
        )).fetchall()
        for conname, defn in rows:
            m = re.search(r'UNIQUE\s*\(([^)]+)\)', defn)
            if m:
                cols = [c.strip().strip('"') for c in m.group(1).split(',')]
                if set(cols) != pk_cols and cols not in uniques:
                    uniques.append(cols)
    return uniques




def _cleanup_secondary_conflicts(
    prod_engine: Engine,
    table: str,
    chunk_rows: list[dict],
    secondary_uniques: list[list[str]],
    pk_cols: list[str],
) -> None:
    """Delete prod rows that conflict with incoming chunk rows on secondary unique constraints.

    Secondary uniques are indexes like UNIQUE(body, meeting_id) that aren't the
    primary key. When a chunk row has the same unique value(s) as an existing
    prod row but a different PK, the prod row must be removed so the upsert
    succeeds without a unique-violation error.

    Single-column uniques are batched into one DELETE with IN (...).
    Multi-column uniques use a VALUES subquery for a single DELETE pass.
    """
    if not secondary_uniques or not chunk_rows:
        return

    with prod_engine.begin() as connection:
        for unique_columns in secondary_uniques:
            if len(unique_columns) == 1:
                _cleanup_single_column_unique(
                    connection, table, chunk_rows, unique_columns, pk_cols,
                )
            else:
                _cleanup_multi_column_unique(
                    connection, table, chunk_rows, unique_columns, pk_cols,
                )




def _cleanup_single_column_unique(
    connection: Connection,
    table: str,
    chunk_rows: list[dict],
    unique_columns: list[str],
    pk_cols: list[str],
) -> None:
    """Delete prod rows conflicting on a single-column UNIQUE constraint.

    Builds one DELETE: WHERE unique_col IN (..., chunk_values)
      AND pk NOT IN (..., chunk_pks)
    """
    unique_col = unique_columns[0]
    pk_col = pk_cols[0]

    values = [row[unique_col] for row in chunk_rows if row.get(unique_col) is not None]
    pks = [row[pk_col] for row in chunk_rows if row.get(unique_col) is not None]
    if not values:
        return

    value_placeholders = ", ".join(f":v{idx}" for idx in range(len(values)))
    pk_placeholders = ", ".join(f":p{idx}" for idx in range(len(pks)))

    params: dict = {}
    for idx, val in enumerate(values):
        params[f"v{idx}"] = val
    for idx, pk in enumerate(pks):
        params[f"p{idx}"] = pk

    deleted = connection.execute(
        text(
            f'DELETE FROM public."{table}"'
            f' WHERE "{unique_col}" IN ({value_placeholders})'
            f'   AND "{pk_col}" NOT IN ({pk_placeholders})'
        ),
        params,
    ).rowcount

    if deleted:
        log.info("    ─ cleaned %d prod row(s) for %s", deleted, unique_columns)




def _cleanup_multi_column_unique(
    connection: Connection,
    table: str,
    chunk_rows: list[dict],
    unique_columns: list[str],
    pk_cols: list[str],
) -> None:
    """Delete prod rows conflicting on a multi-column UNIQUE constraint.

    Builds one DELETE using a VALUES subquery for the chunk's unique-key tuples.
    Original code deleted one row at a time (O(n) round-trips). This batch
    version does one round-trip per chunk.

    Produces SQL like:
      DELETE FROM t
      USING (VALUES (:v0, :v1), (:v2, :v3)) AS v(uq_col1, uq_col2, pk_col)
      WHERE t.uq_col1 = v.uq_col1 AND t.uq_col2 = v.uq_col2
        AND NOT (t.pk_col = v.pk_col)
    """
    # Filter out rows with NULL in any unique column — can't conflict
    valid_rows = [
        row for row in chunk_rows
        if all(row.get(column) is not None for column in unique_columns)
    ]
    if not valid_rows:
        return

    # Build column names for the VALUES subquery
    uq_param_names = [f"uq_{column}" for column in unique_columns]
    pk_param_names = [f"pk_{column}" for column in pk_cols]
    all_param_names = uq_param_names + pk_param_names

    # Build VALUES clause: (:uq_col1_0, :uq_col2_0, :pk_id_0), (..._1), ...
    values_rows = ", ".join(
        "(" + ", ".join(f":{name}_{row_idx}" for name in all_param_names) + ")"
        for row_idx in range(len(valid_rows))
    )
    subquery_columns = ", ".join(all_param_names)

    # Build WHERE clause: table.unique_col = subquery.unique_col
    match_conditions = " AND ".join(
        f't."{column}" = v.{uq_param_names[col_idx]}'
        for col_idx, column in enumerate(unique_columns)
    )
    exclude_conditions = " AND ".join(
        f't."{column}" = v.{pk_param_names[col_idx]}'
        for col_idx, column in enumerate(pk_cols)
    )

    params: dict = {}
    for row_idx, row in enumerate(valid_rows):
        for col_idx, column in enumerate(unique_columns):
            params[f"uq_{column}_{row_idx}"] = row[column]
        for col_idx, column in enumerate(pk_cols):
            params[f"pk_{column}_{row_idx}"] = row[column]

    sql = (
        f'DELETE FROM public."{table}" t'
        f' USING (VALUES {values_rows}) AS v({subquery_columns})'
        f' WHERE {match_conditions}'
        f'   AND NOT ({exclude_conditions})'
    )

    deleted = connection.execute(text(sql), params).rowcount
    if deleted:
        log.info("    ─ cleaned %d prod row(s) for %s", deleted, unique_columns)
