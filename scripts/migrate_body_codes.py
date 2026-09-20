#!/usr/bin/env python3
"""LEGACY ANALYZER — its original apply direction has been permanently disabled.

Background (Pete 2026-09-17)
---------------------------
The same public body exists in ``meetings`` under TWO codes, because the
scraper's short code and the registry's long code were never reconciled:

    chandler-pz                          -> chandler-planning-zoning-commission
    mesa-pz                              -> mesa-planning-zoning

The long code is the one registered in ``public_bodies.body_code``, so the
short-code rows are orphaned: they have no display name, and articles that
cite them render a raw slug.

Pete's decision: merge onto the canonical (registered) code rather than
keep aliases — "We can't have multiple bodies sharing names."  Old public
URLs (``/meetings/{body}/{meeting_id}``) may break; the app forwards them.

Usage
-----
    .venv/bin/python scripts/migrate_body_codes.py --analyze
    .venv/bin/python scripts/body_code_merge.py --rehearse

``--analyze`` is read-only and always safe to run.
``--apply`` always refuses.  The registered short codes are the producer-facing
canonical identities; use the digest-bound ``body_code_merge.py`` runner.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sqlalchemy import text  # noqa: E402

from db.core import get_engine  # noqa: E402

# old (orphaned) code -> canonical (registered) code
REKEY = {
    "chandler-pz": "chandler-planning-zoning-commission",
    "mesa-pz": "mesa-planning-zoning",
}

BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "archive"


def _tables_with_body(conn) -> list[str]:
    """Tables that carry a ``body`` column, in the app's search path only."""
    rows = conn.execute(text("""
        select table_schema, table_name
        from information_schema.columns
        where column_name = 'body'
          and table_schema = current_schema()
        order by table_name
    """)).fetchall()
    return [r[1] for r in rows]


def _count(conn, table: str, code: str) -> int:
    try:
        return conn.execute(
            text(f'select count(*) from "{table}" where body = :b'), {"b": code}
        ).scalar() or 0
    except Exception:
        conn.rollback()
        return -1


def analyze() -> int:
    eng = get_engine()
    with eng.connect() as conn:
        tables = _tables_with_body(conn)
        print(f"=== app schema tables with a 'body' column ({len(tables)}) ===")
        print("   ", ", ".join(tables))

        touched: dict[str, dict[str, int]] = {}
        for old, new in REKEY.items():
            print(f"\n=== {old}  ->  {new} ===")
            per_table = {}
            for t in tables:
                n = _count(conn, t, old)
                if n > 0:
                    per_table[t] = n
                    print(f"   {t:34} {n:>8}")
            touched[old] = per_table
            if not per_table:
                print("   (no rows)")

        print("\n=== COLLISION CHECK (same meeting_id under both codes) ===")
        for old, new in REKEY.items():
            n = conn.execute(text("""
                select count(*) from meetings a
                join meetings b on a.meeting_id = b.meeting_id
                where a.body = :o and b.body = :n
            """), {"o": old, "n": new}).scalar() or 0
            flag = "  <-- MUST RESOLVE BEFORE APPLY" if n else ""
            print(f"   {old:14} vs {new:38} collisions: {n}{flag}")

        print("\n=== COLLISION DETAIL (which side is authoritative?) ===")
        for old, new in REKEY.items():
            row = conn.execute(text("""
                select
                  count(*)                                                   as overlap,
                  count(*) filter (where a.meeting_date <> b.meeting_date)   as dates_differ,
                  count(*) filter (where coalesce(a.source_url,'') <> coalesce(b.source_url,'')) as urls_differ,
                  min(a.meeting_date) as short_min, max(a.meeting_date) as short_max,
                  min(b.meeting_date) as canon_min, max(b.meeting_date) as canon_max
                from meetings a
                join meetings b on a.meeting_id = b.meeting_id
                where a.body = :o and b.body = :n
            """), {"o": old, "n": new}).mappings().first()
            print(f"   {old} -> {new}")
            for k, v in dict(row).items():
                print(f"      {k:14} {v}")
            # agenda_items attached to the overlapping meetings, per code
            for label, code in (("short", old), ("canonical", new)):
                n = conn.execute(text("""
                    select count(*) from agenda_items ai
                    where ai.body = :c and ai.meeting_id in (
                        select a.meeting_id from meetings a
                        join meetings b on a.meeting_id = b.meeting_id
                        where a.body = :o and b.body = :n)
                """), {"c": code, "o": old, "n": new}).scalar() or 0
                print(f"      agenda_items under {label:10} {n}")

        print("\n=== PER-MEETING CHILD COMPLETENESS (would deleting the short copy lose data?) ===")
        child_tables = [r[0] for r in conn.execute(text("""
            select table_name from information_schema.columns
            where column_name = 'meeting_id' and table_schema = current_schema()
            intersect
            select table_name from information_schema.columns
            where column_name = 'body' and table_schema = current_schema()
            order by table_name
        """)).fetchall()]
        print("   child tables keyed by (body, meeting_id):")
        print("     ", ", ".join(child_tables))

        any_loss = False
        for old, new in REKEY.items():
            # Restrict to meeting_ids present under BOTH codes — only those
            # are candidates for deletion.  A meeting that exists solely on
            # the short side gets RE-KEYED, so its rows are never at risk.
            overlap_sql = text("""
                select a.meeting_id from meetings a
                join meetings b on a.meeting_id = b.meeting_id
                where a.body = :o and b.body = :n
            """)
            overlap_ids = [r[0] for r in conn.execute(
                overlap_sql, {"o": old, "n": new}).fetchall()]
            print(f"\n   --- {old}: {len(overlap_ids)} overlapping meeting(s) "
                  "(the only deletion candidates) ---")
            if not overlap_ids:
                print("       no overlap — nothing to delete")
                continue

            for t in child_tables:
                rows = conn.execute(text(f"""
                    select coalesce(a.meeting_id, b.meeting_id) as mid,
                           coalesce(a.n, 0) as short_n,
                           coalesce(b.n, 0) as canon_n
                    from (select meeting_id, count(*) n from "{t}"
                          where body = :o group by meeting_id) a
                    full outer join (select meeting_id, count(*) n from "{t}"
                          where body = :n group by meeting_id) b
                      on a.meeting_id = b.meeting_id
                    where coalesce(a.n, 0) <> coalesce(b.n, 0)
                      and coalesce(a.meeting_id, b.meeting_id) = any(:ids)
                """), {"o": old, "n": new, "ids": overlap_ids}).fetchall()
                if not rows:
                    continue
                worse = [r for r in rows if r[1] > r[2]]
                if worse:
                    any_loss = True
                    print(f"   {t}: {len(worse)} overlapping meeting(s) where "
                          "SHORT has MORE — deleting loses data")
                    for r in worse[:6]:
                        print(f"        meeting {r[0]}: short={r[1]} "
                              f"canonical={r[2]}  <-- would lose {r[1]-r[2]}")

        if not any_loss:
            print("\n   RESULT: within the overlap, no child row exists only on "
                  "the short side — deleting short duplicates is SAFE")
        else:
            print("\n   RESULT: NOT SAFE to delete — merge, don't discard. "
                  "Rows listed above exist only on the short side.")

        print("\n=== unique/primary constraints that could block the update ===")
        for tbl in ("meetings", "agenda_items"):
            rows = conn.execute(text("""
                select conname, pg_get_constraintdef(oid)
                from pg_constraint
                where conrelid = to_regclass(:t) and contype in ('u', 'p')
            """), {"t": tbl}).fetchall()
            print(f"   {tbl}:")
            for r in rows:
                print(f"      {r[0]}: {r[1]}")

        print("\n=== remaining rows still on the orphaned codes ===")
        for old, new in REKEY.items():
            total = sum(v for v in touched[old].values() if v > 0)
            print(f"   {old:14} {total:>8} rows across "
                  f"{len(touched[old])} table(s)")
    return 0


def apply(backup: bool) -> int:
    print("refusing: this legacy runner used the wrong canonical direction; "
          "use scripts/body_code_merge.py with a reviewed digest and verified backup")
    return 2

    # Historical implementation retained below as audit evidence; unreachable.
    if not backup:
        print("refusing to write without --backup")
        return 2

    eng = get_engine()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    with eng.begin() as conn:
        tables = _tables_with_body(conn)

        # 1. Refuse on collisions — merging them needs a human decision.
        for old, new in REKEY.items():
            n = conn.execute(text("""
                select count(*) from meetings a
                join meetings b on a.meeting_id = b.meeting_id
                where a.body = :o and b.body = :n
            """), {"o": old, "n": new}).scalar() or 0
            if n:
                print(f"ABORT: {old} has {n} meeting_id collision(s) with {new}")
                return 3

        # 2. Back up every affected row before touching anything.
        backup_path = BACKUP_DIR / f"body-rekey-{stamp}.json"
        dump: dict[str, list] = {}
        for old in REKEY:
            for t in tables:
                if _count(conn, t, old) <= 0:
                    continue
                rows = conn.execute(
                    text(f'select * from "{t}" where body = :b'), {"b": old}
                ).mappings().all()
                dump[f"{t}|{old}"] = [dict(r) for r in rows]
        backup_path.write_text(json.dumps(dump, default=str, indent=2))
        print(f"backup written: {backup_path} "
              f"({sum(len(v) for v in dump.values())} rows)")

        # 3. Re-key.
        for old, new in REKEY.items():
            for t in tables:
                n = _count(conn, t, old)
                if n <= 0:
                    continue
                conn.execute(
                    text(f'update "{t}" set body = :n where body = :o'),
                    {"n": new, "o": old},
                )
                print(f"   {t:34} {n:>8} rows  {old} -> {new}")

    print("re-key complete")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", action="store_true")
    a = ap.parse_args()
    if a.apply:
        return apply(a.backup)
    return analyze()


if __name__ == "__main__":
    raise SystemExit(main())
