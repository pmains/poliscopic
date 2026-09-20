#!/usr/bin/env python3
"""Brief 036 — backfill empty ``body`` values from their parent row.

Both tables carry a denormalized body code that duplicates what the parent row
already knows, so an empty value is recoverable rather than lost:

  article_sources.body  <- meetings.body              via meeting_id
  member_votes.body     <- agenda_item_votes.body     via agenda_item_vote_id

Only rows whose parent exists and itself carries a body are filled.  Rows whose
parent row is *gone* cannot be derived and are reported, never guessed — in
development that is 197 ``member_votes`` rows pointing at ``agenda_item_votes``
ids that no longer exist (a separate dangling-FK defect).

Defaults to development.  Pass ``--target production`` to operate on
production; the target is resolved through ``db.sync_targets`` and is printed
(masked) before anything is written.  Dry-run by default either way.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import inspect, text  # noqa: E402
from ops.production_interlock_guard import require_production_interlock  # noqa: E402

# (target table, target column, parent table, join condition, parent body expr)
BACKFILLS = (
    (
        "article_sources", "body", "meetings",
        "m.meeting_id = a.meeting_id",
        "m.body",
    ),
    (
        "member_votes", "body", "agenda_item_votes",
        "p.id = mv.agenda_item_vote_id",
        "p.body",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="data/archive")
    parser.add_argument(
        "--target", choices=("development", "production"),
        default="development",
        help="database to operate on (default: development)",
    )
    args = parser.parse_args()

    if args.target == "production":
        require_production_interlock(
            "OP-REPAIR" if args.apply else "OP-STATUS",
            "scripts/backfill_empty_body.py",
        )

    if args.target == "production":
        from sqlalchemy import create_engine
        from db.sync_targets import _resolve_prod_url

        engine = create_engine(_resolve_prod_url(), future=True)
    else:
        from db.core import get_engine

        engine = get_engine()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aliases = {"article_sources": "a", "member_votes": "mv",
               "meetings": "m", "agenda_item_votes": "p"}
    parent_alias = {"meetings": "m", "agenda_item_votes": "p"}

    # Not every target table has updated_at (article_sources does not), so a
    # change-detection stamp is only advanced where the column exists.
    insp = inspect(engine)
    has_stamp = {}
    for table, *_ in BACKFILLS:
        try:
            has_stamp[table] = "updated_at" in {
                c["name"] for c in insp.get_columns(table)}
        except Exception:
            has_stamp[table] = False

    print("=" * 76)
    print("BACKFILL EMPTY BODY VALUES" + ("" if args.apply else "   [DRY RUN]"))
    print("=" * 76)
    print(f"tier:   {args.target}")
    print(f"target: {engine.url.render_as_string(hide_password=True)}")
    print()

    planned: dict[str, dict] = {}
    with engine.connect() as conn:
        for table, column, parent, join, expr in BACKFILLS:
            alias = aliases[table]
            palias = parent_alias[parent]
            total = conn.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE "{column}" = \'\'')
            ).scalar()
            fillable = conn.execute(text(
                f'SELECT count(*) FROM "{table}" {alias} '
                f'JOIN "{parent}" {palias} ON {join} '
                f"WHERE {alias}.\"{column}\" = '' "
                f"AND {expr} IS NOT NULL AND {expr} <> ''")).scalar()
            orphaned = total - fillable
            planned[table] = {"total_empty": total, "fillable": fillable,
                              "orphaned": orphaned}
            print(f"  {table}.{column}")
            print(f"      empty={total}  fillable={fillable}  "
                  f"parent-missing={orphaned}")

        print()
        # what values would be written
        for table, column, parent, join, expr in BACKFILLS:
            alias = aliases[table]
            palias = parent_alias[parent]
            rows = conn.execute(text(
                f'SELECT {expr} AS v, count(*) n FROM "{table}" {alias} '
                f'JOIN "{parent}" {palias} ON {join} '
                f"WHERE {alias}.\"{column}\" = '' "
                f"AND {expr} IS NOT NULL AND {expr} <> '' "
                f"GROUP BY {expr} ORDER BY n DESC")).fetchall()
            if rows:
                print(f"  would set {table}.{column} to:")
                for v, n in rows:
                    print(f"      {v!r:<28} {n} rows")
        print()

        if not args.apply:
            print("DRY RUN — no writes. Re-run with --apply to execute.")
            return 0

        # backup
        backup: dict = {"created_at": stamp, "kind": "backfill-empty-body",
                        "planned": planned, "rows": {}}
        for table, column, parent, join, expr in BACKFILLS:
            alias = aliases[table]
            palias = parent_alias[parent]
            backup["rows"][table] = [dict(r) for r in conn.execute(text(
                f'SELECT {alias}.id, {alias}."{column}" AS body, {expr} AS derived '
                f'FROM "{table}" {alias} JOIN "{parent}" {palias} ON {join} '
                f"WHERE {alias}.\"{column}\" = '' "
                f"AND {expr} IS NOT NULL AND {expr} <> ''")).mappings().all()]

    path = Path(args.backup_dir) / f"backfill-empty-body-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(backup, indent=2, default=str))
    os.chmod(path, 0o600)
    print(f"backup: {path} "
          f"({sum(len(v) for v in backup['rows'].values())} rows)")

    written = 0
    with engine.begin() as conn:
        for table, column, parent, join, expr in BACKFILLS:
            alias = aliases[table]
            palias = parent_alias[parent]
            r = conn.execute(text(
                f'UPDATE "{table}" {alias} SET '
                + ("updated_at = CURRENT_TIMESTAMP, " if has_stamp[table] else "")
                + f'"{column}" = {expr} '
                f'FROM "{parent}" {palias} WHERE {join} '
                f"AND {alias}.\"{column}\" = '' "
                f"AND {expr} IS NOT NULL AND {expr} <> ''"))
            written += r.rowcount
            print(f"  backfilled {table}.{column}: {r.rowcount} rows")

        problems = []
        for table, column, parent, join, expr in BACKFILLS:
            alias = aliases[table]
            palias = parent_alias[parent]
            left = conn.execute(text(
                f'SELECT count(*) FROM "{table}" {alias} '
                f'JOIN "{parent}" {palias} ON {join} '
                f"WHERE {alias}.\"{column}\" = '' "
                f"AND {expr} IS NOT NULL AND {expr} <> ''")).scalar()
            if left:
                problems.append(f"{table}.{column} still has {left} fillable empty rows")
        # nothing may be left claiming an empty body
        for table, column, _, _, _ in BACKFILLS:
            empty = conn.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE "{column}" = \'\'')
            ).scalar()
            if empty:
                print(f"  note: {table}.{column} still has {empty} empty row(s) "
                      f"(parent missing — reported, not guessed)")
        print()
        if problems:
            for p in problems:
                print(f"  POSTCONDITION FAIL: {p}")
            raise SystemExit("rolling back — postconditions not met")
        print("  postconditions: clean")

    print(f"\nrows backfilled: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
