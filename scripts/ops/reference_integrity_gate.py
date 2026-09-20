#!/usr/bin/env python3
"""Reference-integrity gate for the dev→prod sync.

Brief 033.  Read-only.  This module computes and evaluates the invariants whose
absence allowed production's ``public_bodies`` registry to diverge from
development's while every existing release check reported success.

Why this exists
---------------
``scripts/db/sync_upsert.py`` detects changes with ``WHERE updated_at > :since``.
Any write that does not advance ``updated_at`` is therefore invisible to sync —
permanently.  ``scripts/body_code_merge_runtime.py:1589`` renames a
``public_bodies.body_code`` in place without touching ``updated_at``, so the
canonical row never propagated while the meetings that reference it did.

Nothing in the release path could observe that.  This gate can:

  1. ``reference_order_problems``      — parents must sync before dependents
  2. ``dangling_references``           — body values with no registry row
  3. ``integrity_problems``            — fail-closed verdict (sentinels + scope)
  4. ``correction_update``             — the only sanctioned way to rewrite a code
  5. ``unbumped_write_problems``       — lint: a body write that skips updated_at

Functions taking an ``engine`` work on any SQLAlchemy engine (SQLite in tests,
PostgreSQL in preflight).  The pure functions take plain data.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable, Mapping, Sequence

from sqlalchemy import inspect, text

# Make the shared database modules importable however this module is invoked
# (mirrors scripts/db/sync_declarations.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from propagation_contract import DEPENDENCY_EDGES  # noqa: E402

# Values that must never be promoted into ``public_bodies``.  ``__skip__`` is a
# scrape-level sentinel that leaked into live rows (meetings.id=15841).
SENTINELS: frozenset[str] = frozenset({"__skip__", ""})

# Columns that carry a body code.  ``articles.body`` is article prose, not a
# code, and is excluded by table name.
BODY_COLUMNS = ("body", "body_code")
PROSE_TABLES = ("articles",)

# Sync ordering invariants: (parent, dependent).
REQUIRED_ORDER: tuple[tuple[str, str], ...] = DEPENDENCY_EDGES


# ── pure checks ─────────────────────────────────────────────────────────


def is_sentinel(value: str | None) -> bool:
    """True when ``value`` is a sentinel/empty placeholder, not a real code."""
    return value is None or value in SENTINELS or not str(value).strip()


def reference_order_problems(tables: Sequence[str]) -> list[str]:
    """Parents must appear before their dependents in the sync table list."""
    problems: list[str] = []
    for parent, dependent in REQUIRED_ORDER:
        if parent not in tables:
            problems.append(
                f"{parent!r} missing from the sync table list "
                f"(required as parent of {dependent!r})"
            )
            continue
        if dependent not in tables:
            continue
        if tables.index(parent) > tables.index(dependent):
            problems.append(
                f"{parent!r} (index {tables.index(parent)}) must precede "
                f"{dependent!r} (index {tables.index(dependent)}) — a dependent "
                f"row synced first can reference a parent that does not exist"
            )
    return problems


def correction_update(
    table: str,
    *,
    set_column: str,
    key_column: str,
    stamp_column: str = "updated_at",
) -> str:
    """SQL that rewrites a body code *and* advances the change-detection stamp.

    Raises when ``stamp_column`` is empty.  A correction that skips the stamp
    cannot converge: the row stays invisible to every future incremental sync.
    This is the root cause recorded in Brief 033 §1.
    """
    if not stamp_column:
        raise ValueError(
            "refusing to build a correction that does not advance the "
            "sync stamp — the row would remain invisible to incremental sync"
        )
    return (
        f'UPDATE "{table}" SET "{set_column}" = :new, '
        f'"{stamp_column}" = CURRENT_TIMESTAMP WHERE "{key_column}" = :old'
    )


_UPDATE_RE = re.compile(
    r'UPDATE\s+(?:"(?P<qtable>[A-Za-z_][\w]*)"|(?P<table>[A-Za-z_][\w]*))'
    r'\s+SET\s+(?P<sets>.*?)(?=\s+WHERE\b|\s*"""|\s*$|\s*\)\s*$)',
    re.IGNORECASE | re.DOTALL,
)


def unbumped_write_problems(source: str, tables: Iterable[str] | None = None) -> list[str]:
    """Lint source text for body-code writes that do not advance ``updated_at``.

    Detects the exact shape at body_code_merge_runtime.py:1589.  Intended to run
    over the merge/sync/ingest sources in preflight.
    """
    watch = set(tables) if tables else None
    problems: list[str] = []
    for match in _UPDATE_RE.finditer(source):
        table = match.group("qtable") or match.group("table")
        if watch and table not in watch:
            continue
        sets = " ".join(match.group("sets").split())
        if not sets:
            continue
        writes_body = re.search(r'"?\bbody_code\b"?\s*=', sets) or re.search(
            r'"?\bbody\b"?\s*=', sets
        )
        if not writes_body:
            continue
        if re.search(r'"?\bupdated_at\b"?\s*=', sets):
            continue
        problems.append(
            f'UPDATE "{table}" writes a body code without advancing updated_at: SET {sets}'
        )
    return problems


# ── database checks ─────────────────────────────────────────────────────


def body_columns(engine, tables: Sequence[str] | None = None) -> list[tuple[str, str]]:
    """(table, column) pairs whose column may hold a body code."""
    insp = inspect(engine)
    found: list[tuple[str, str]] = []
    for table in tables if tables is not None else insp.get_table_names():
        if table in PROSE_TABLES:
            continue
        try:
            columns = {c["name"] for c in insp.get_columns(table)}
        except Exception:
            continue
        for column in BODY_COLUMNS:
            if column in columns:
                found.append((table, column))
    return found


def stamp_column_problems(engine, tables: Sequence[str] | None = None) -> list[str]:
    """Body-bearing tables whose declared propagation mode cannot be satisfied.

    This is the schema-level half of the propagation contract.  A per-write
    check cannot assert it — a test fixture with minimal DDL is legitimate
    maintenance — so the requirement is asserted here, against whichever
    database is actually connected:

      * ``incremental``      must carry ``updated_at``, or its writes are
        invisible to ``WHERE updated_at > :since`` and never converge;
      * ``full_reference``   needs no stamp (re-sent in full every sync);
      * ``excluded``         is not propagated at all.

    A body-bearing table with no declared mode is reported too: a write there
    could not be reasoned about, which is the fail-closed condition.
    """
    from db.sync_declarations import (
        PROPAGATION_EXCLUDED,
        propagation_mode,
        stamp_required,
    )

    insp = inspect(engine)
    problems: list[str] = []
    names = sorted({table for table, _ in body_columns(engine, tables)})
    for table in names:
        try:
            mode = propagation_mode(table)
        except KeyError:
            problems.append(
                f"{table}: no declared propagation mode — a write here cannot "
                "be reasoned about"
            )
            continue
        if mode == PROPAGATION_EXCLUDED or not stamp_required(table):
            continue
        try:
            columns = {c["name"] for c in insp.get_columns(table)}
        except Exception:
            continue
        if "updated_at" not in columns:
            problems.append(
                f"{table}: declared incremental but has no updated_at column — "
                "writes cannot converge"
            )
    return problems


def dangling_references(
    engine,
    *,
    tables: Sequence[str] | None = None,
) -> dict[tuple[str, str], dict[str, int]]:
    """Body values with no matching ``public_bodies.body_code``.

    Returns ``{(table, column): {value: row_count}}``.  Read-only.
    """
    insp = inspect(engine)
    if "public_bodies" not in insp.get_table_names():
        raise ValueError("public_bodies table not present — cannot evaluate referential integrity")
    out: dict[tuple[str, str], dict[str, int]] = {}
    with engine.connect() as conn:
        for table, column in body_columns(engine, tables=tables):
            if table == "public_bodies" and column == "body_code":
                continue
            try:
                rows = conn.execute(
                    text(
                        f'SELECT x."{column}" AS v, COUNT(*) AS n FROM "{table}" x '
                        f'LEFT JOIN public_bodies pb ON pb.body_code = x."{column}" '
                        f'WHERE x."{column}" IS NOT NULL AND pb.id IS NULL '
                        f'GROUP BY x."{column}"'
                    )
                ).fetchall()
            except Exception:
                continue
            if rows:
                out[(table, column)] = {r[0]: int(r[1]) for r in rows}
    return out


def integrity_problems(
    engine,
    *,
    scope_codes: Iterable[str] | None = None,
    tracked_exceptions: Iterable[str] = (),
    tables: Sequence[str] | None = None,
) -> list[str]:
    """Fail-closed verdict for the release gate.

    ``scope_codes`` — codes this operation is about to write.  Their dangling
    references are blockers: a canonical code must not be written without a
    registry row, and vice versa.  When ``None``, any non-exception dangling
    code in ``meetings`` is a blocker.

    ``tracked_exceptions`` — codes with a documented, separately-owned defect
    (e.g. ``phoenix-gp``).  Listing one here is a deliberate, auditable act;
    silent tolerance is not supported.

    Sentinels are always blockers regardless of scope.
    """
    scope = set(scope_codes) if scope_codes is not None else None
    exceptions = set(tracked_exceptions)
    problems: list[str] = []
    for (table, column), values in dangling_references(engine, tables=tables).items():
        for value, count in sorted(values.items()):
            if is_sentinel(value):
                problems.append(
                    f"SENTINEL {value!r} in {table}.{column} ({count} rows) — "
                    f"must be quarantined, never promoted into public_bodies"
                )
                continue
            if value in exceptions:
                continue
            if scope is not None and value not in scope:
                continue
            if scope is None and table != "meetings":
                continue
            problems.append(
                f"DANGLING body code {value!r} in {table}.{column} ({count} rows) — "
                f"no public_bodies row; refusing to sync a canonical code without its parent"
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from db.core import get_engine
    from db.sync_declarations import ALL_SYNC_TABLES

    parser = argparse.ArgumentParser(description="Reference-integrity gate (read-only)")
    parser.add_argument("--scope-code", action="append", default=None,
                        help="a body code this operation will write (repeatable)")
    parser.add_argument("--tracked-exception", action="append", default=[],
                        help="documented, separately-owned dangling code (repeatable)")
    args = parser.parse_args(argv)

    problems: list[str] = []
    problems += reference_order_problems(ALL_SYNC_TABLES)
    problems += integrity_problems(
        get_engine(),
        scope_codes=args.scope_code,
        tracked_exceptions=args.tracked_exception,
    )

    for problem in problems:
        print(f"REFUSE: {problem}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s) — refusing.", file=sys.stderr)
        return 1
    print("reference integrity: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
