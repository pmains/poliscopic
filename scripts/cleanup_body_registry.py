#!/usr/bin/env python3
"""Brief 034 — development body-registry cleanup (names, codes, registration).

Dev-only.  Idempotent.  Nothing is written unless ``--apply`` is passed; the
default is a dry run that reports what would change and pre-checks for
collisions.

What it does (docs/briefs/034-dev-body-cleanup-report-2026-09-18.md §8/§11):

  0. widens body-code columns  — eight are VARCHAR(16); long codes are truncated
                                 on write (StringDataRightTruncation) and short
                                 ones silently cut
  1. code renames              — 17 forgettable abbreviations -> legible codes
  2. consolidation             — chandler-pha-comm -> chandler-pha (row deleted)
  3. orphan reparenting        — retired/sentinel codes -> canonical bodies
  4. name corrections          — 14 rows whose stored name contradicts the source
  5. registration              — create bodies named by the data but absent from
                                 the registry (phoenix-gp, phoenix-emsd)

Ordering note: renames run BEFORE name fixes, so a name fix keyed on a renamed
body (e.g. ``phoenix-cb``) targets the code that exists afterwards.

Every rewritten row has ``updated_at`` advanced *where the column exists*.
Brief 033 §1 showed that a change which does not advance ``updated_at`` is
invisible to the dev->prod sync, permanently — so a cleanup that skips it
silently fails to hold.

A backup of every affected row is written to ``data/archive/`` before any write.
Excluded: ``_pattern_cascade_watermark`` (dev-only merge-rehearsal scratch table,
``body`` is its PRIMARY KEY, absent from production) and ``articles`` (prose).
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

# ── 1+2+3. old code -> new code (renames, consolidation, reparenting) ───
CODE_RENAMES: dict[str, str] = {
    # legible-code migration (report §11.2)
    "phoenix-hr": "phoenix-human-relations",
    "phoenix-hs": "phoenix-human-services",
    "phoenix-fp": "phoenix-fire-pension",
    "phoenix-pp": "phoenix-police-pension",
    "phoenix-eq": "phoenix-environmental-quality",
    "phoenix-di": "phoenix-disability-issues",
    "phoenix-hp": "phoenix-historic-preservation",
    "phoenix-hc": "phoenix-heritage-commission",
    "phoenix-la": "phoenix-license-appeal",
    "phoenix-vpc": "phoenix-village-planning",
    "phoenix-wc": "phoenix-womens-commission",
    "phoenix-za": "phoenix-zoning-adjustment",
    "phoenix-cb": "phoenix-copers-board",
    "mesa-boa": "mesa-board-of-adjustment",
    "mesa-cc": "mesa-city-council",
    "mesa-drb": "mesa-design-review-board",
    "mesa-hpb": "mesa-historic-preservation-board",
    # consolidation (report §4) — source row is DELETED after reparenting
    "chandler-pha-comm": "chandler-pha",
    # orphan reparenting (report §7)
    "el-mirage-planning-zoning": "el-mirage-pz",
    "tempe-development-review-commission": "tempe-drc",
    "peoria-pz": "peoria-planning-zoning",
    # sentinel that is not a code: the single row is a real EMSD Advisory Board
    # notice (meetings.id=15841, "Enhanced Municipal Services District (EMSD)
    # Advisory Board - Annual Notice"), so it is reparented to a registered body
    # rather than nulled.  meetings.body is NOT NULL.
    "__skip__": "phoenix-emsd",
}

# For these sources the target code already has its own public_bodies row, so the
# source row must be DELETED after its children are reparented.  Renaming it
# would leave two rows sharing one body_code (there is no unique constraint on
# body_code, so the database would accept the duplicate silently).
CONSOLIDATE_DELETE = frozenset({"chandler-pha-comm"})

# ── 4. name corrections (report §2), keyed on the code AFTER renaming ───
NAME_FIXES: dict[str, str] = {
    "chandler-lb": "Chandler Library Board",
    "chandler-dvc": "Chandler Domestic Violence Commission",
    "chandler-prb": "Chandler Parks and Recreation Board",
    "chandler-mf": "Chandler Museum Foundation",
    "chandler-mvc": "Chandler Military and Veterans Affairs Commission",
    "chandler-hhsc": "Chandler Housing and Human Services Commission",
    "chandler-nac": "Chandler Neighborhood Advisory Committee",
    "chandler-yc": "Chandler Mayor's Youth Commission",
    "chandler-psprs-f": "Chandler PSPRS Board Fire",
    "chandler-psprs-p": "Chandler PSPRS Board Police",
    "chandler-pha": "Chandler Public Housing Authority Commission",
    "chandler-wct": (
        "Chandler Workers' Compensation and Employer Liability Trust Board"
    ),
    "chandler-hcc": "Chandler Housing and Community Services Corporation",
    "phoenix-cb": "Phoenix COPERS Board",
}

# ── 5. registration: bodies the data names but the registry lacks ───────
CREATE_ROWS: list[dict[str, str]] = [
    {
        "body_code": "phoenix-gp",
        "name": "Phoenix General Information Packet",
        "slug": "phoenix-general-information-packet",
        "jurisdiction_match": "Phoenix",
    },
    {
        "body_code": "phoenix-emsd",
        "name": "Phoenix Enhanced Municipal Services District Advisory Board",
        "slug": "phoenix-emsd-advisory-board",
        "jurisdiction_match": "Phoenix",
    },
]

PROSE_TABLES = ("articles",)

# Dev-only scratch table written by the merge rehearsal.  It is absent from
# production and holds no product data, but `body` is its PRIMARY KEY, so a
# remap collides.  Excluded deliberately and reported.
SKIP_TABLES = {"_pattern_cascade_watermark"}

# Widened to this when a body-code column is too narrow to hold a legible code.
TARGET_WIDTH = 64


def schema_map(engine):
    """Return (body-code columns, {table: has_updated_at}, {(table,col): max_length})."""
    insp = inspect(engine)
    columns: list[tuple[str, str]] = []
    stamped: dict[str, bool] = {}
    widths: dict[tuple[str, str], int | None] = {}
    for table in insp.get_table_names():
        if table in PROSE_TABLES or table in SKIP_TABLES:
            continue
        try:
            cols = insp.get_columns(table)
        except Exception:
            continue
        names = {c["name"] for c in cols}
        stamped[table] = "updated_at" in names
        for column in ("body", "body_code"):
            if column not in names:
                continue
            if table != "public_bodies" and column == "body_code":
                continue
            columns.append((table, column))
            widths[(table, column)] = next(
                (c.get("type").length for c in cols
                 if c["name"] == column and hasattr(c.get("type"), "length")),
                None,
            )
    return columns, stamped, widths


def bump(table: str, stamped: dict[str, bool]) -> str:
    """SET fragment that advances updated_at when the table supports it."""
    return "updated_at = CURRENT_TIMESTAMP, " if stamped.get(table) else ""


def keyed_body_tables(engine) -> list[tuple[str, list[str]]]:
    """Tables having a PK/unique constraint that includes a body-code column.

    Returns (table, other_key_columns).  Reparenting into an occupied key space
    raises UniqueViolation, so these are pre-checked before any write.
    """
    out: list[tuple[str, list[str]]] = []
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT cl.relname,
                   array(SELECT att.attname
                         FROM unnest(con.conkey) AS k
                         JOIN pg_attribute att
                           ON att.attrelid = con.conrelid AND att.attnum = k) AS cols
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            WHERE ns.nspname = current_schema() AND con.contype IN ('p','u')
        """)).fetchall()
    for table, cols in rows:
        if table in SKIP_TABLES or table in PROSE_TABLES or not cols:
            continue
        if "body" in cols:
            out.append((table, [c for c in cols if c != "body"]))
    return out


def collision_check(engine, remap: dict[str, str]) -> list[str]:
    """Count rows that would violate a keyed constraint during reparenting."""
    problems: list[str] = []
    tables = keyed_body_tables(engine)
    with engine.connect() as conn:
        for table, others in tables:
            for old, new in remap.items():
                if others:
                    join = " AND ".join(f'a."{c}" = b."{c}"' for c in others)
                else:
                    join = "TRUE"
                sql = (f'SELECT count(*) FROM "{table}" a JOIN "{table}" b '
                       f'ON {join} WHERE a.body = :old AND b.body = :new')
                try:
                    n = conn.execute(text(sql), {"old": old, "new": new}).scalar()
                except Exception as exc:
                    problems.append(f"{table}: collision check errored: {exc}")
                    continue
                if n:
                    problems.append(
                        f"{table}: {n} row(s) would collide renaming "
                        f"{old!r} -> {new!r} (key includes {others or ['body']})")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry run)")
    parser.add_argument("--backup-dir", default="data/archive")
    args = parser.parse_args()

    from db.core import get_engine

    engine = get_engine()
    columns, stamped, widths = schema_map(engine)
    narrow = [(t, c) for t, c in columns
              if widths.get((t, c)) is not None and widths[(t, c)] < TARGET_WIDTH]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    no_stamp = [t for t, c in columns if not stamped.get(t)]

    print("=" * 76)
    print("BRIEF 034 — DEV BODY-REGISTRY CLEANUP"
          + ("" if args.apply else "  [DRY RUN]"))
    print("=" * 76)
    print(f"target        : {engine.url.render_as_string(hide_password=True)}")
    print(f"remaps        : {len(CODE_RENAMES)}")
    print(f"name fixes    : {len(NAME_FIXES)}")
    print(f"registrations : {len(CREATE_ROWS)}")
    print(f"child columns : {len(columns)}")
    if no_stamp:
        print(f"  ! no updated_at (cannot stamp): {no_stamp}")
    print(f"  narrow columns to widen to varchar({TARGET_WIDTH}): {len(narrow)}")
    for table, column in narrow:
        print(f"      {table}.{column} (varchar({widths[(table, column)]}))")
    if SKIP_TABLES:
        print(f"  excluded tables (scratch, not product data): {sorted(SKIP_TABLES)}")

    collisions = collision_check(engine, CODE_RENAMES)
    if collisions:
        print("  ! KEY COLLISIONS:")
        for problem in collisions:
            print(f"      {problem}")
    else:
        print("  key-collision pre-check: clean")
    print()
    if collisions and args.apply:
        raise SystemExit("refusing to apply — keyed reparenting would collide")

    if not args.apply:
        with engine.connect() as conn:
            print("would re-key:")
            for old, new in CODE_RENAMES.items():
                body = conn.execute(
                    text("SELECT count(*) FROM public_bodies WHERE body_code=:v"),
                    {"v": old}).scalar()
                kids = 0
                for table, column in columns:
                    if table == "public_bodies":
                        continue
                    kids += conn.execute(
                        text(f'SELECT count(*) FROM "{table}" WHERE "{column}"=:v'),
                        {"v": old}).scalar()
                print(f"  {old:<38} -> {new:<38} body={body} children={kids}")
            print()
            print("would rename:")
            for code, name in NAME_FIXES.items():
                target = CODE_RENAMES.get(code, code)
                cur = conn.execute(
                    text("SELECT name FROM public_bodies WHERE body_code=:v"),
                    {"v": target}).fetchone()
                print(f"  {target:<24} {cur[0] if cur else '(absent)'!r}")
                print(f"    {'':<24} -> {name!r}")
            print()
            for row in CREATE_ROWS:
                exists = conn.execute(
                    text("SELECT 1 FROM public_bodies WHERE body_code=:v"),
                    {"v": row["body_code"]}).fetchone()
                print(f"would create  : {row['body_code']} -> {row['name']!r} "
                      f"({'already present' if exists else 'new row'})")
            print("\nDRY RUN — no writes. Re-run with --apply to execute.")
            return 0

    # ── backup every affected row ──
    affected = list(CODE_RENAMES)
    with engine.connect() as conn:
        insp = inspect(engine)
        backup: dict = {"created_at": stamp, "kind": "body-registry-cleanup",
                        "target": engine.url.render_as_string(hide_password=True),
                        "remap": CODE_RENAMES, "name_fixes": NAME_FIXES,
                        "rows": {}}
        for table, column in columns:
            names = {c["name"] for c in insp.get_columns(table)}
            if "id" not in names:
                continue
            placeholders = ", ".join(f":v{i}" for i in range(len(affected)))
            params = {f"v{i}": v for i, v in enumerate(affected)}
            rows = conn.execute(
                text(f'SELECT id, "{column}" AS body FROM "{table}" '
                     f'WHERE "{column}" IN ({placeholders})'),
                params).mappings().all()
            if rows:
                backup["rows"][f"{table}.{column}"] = [dict(r) for r in rows]
    backup_path = Path(args.backup_dir) / f"body-registry-cleanup-{stamp}.json"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(json.dumps(backup, indent=2, default=str))
    os.chmod(backup_path, 0o600)
    print(f"backup        : {backup_path} "
          f"({sum(len(v) for v in backup['rows'].values())} rows)")

    # ── apply in one transaction ──
    written = 0
    with engine.begin() as conn:
        # 0. widen body-code columns too narrow to hold a legible code
        for table, column in narrow:
            conn.execute(text(
                f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
                f'TYPE varchar({TARGET_WIDTH})'))
            print(f"  widen {table}.{column} -> varchar({TARGET_WIDTH})")

        # 1-3. re-key children, then the registry row
        for old, new in CODE_RENAMES.items():
            kids = 0
            for table, column in columns:
                if table == "public_bodies":
                    continue
                rr = conn.execute(
                    text(f'UPDATE "{table}" SET {bump(table, stamped)}'
                         f'"{column}" = :new WHERE "{column}" = :old'),
                    {"old": old, "new": new})
                kids += rr.rowcount
            written += kids
            if old in CONSOLIDATE_DELETE:
                r = conn.execute(
                    text("DELETE FROM public_bodies WHERE body_code = :old"),
                    {"old": old})
                written += r.rowcount
                print(f"  consol {old:<34} -> {new:<34} children={kids} "
                      f"deleted_row={r.rowcount}")
            else:
                r = conn.execute(
                    text(f'UPDATE public_bodies SET {bump("public_bodies", stamped)}'
                         f'body_code = :new WHERE body_code = :old'),
                    {"old": old, "new": new})
                written += r.rowcount
                print(f"  re-key {old:<34} -> {new:<34} body={r.rowcount} "
                      f"children={kids}")

        # 4. name corrections (after renames, so codes exist)
        for code, name in NAME_FIXES.items():
            target = CODE_RENAMES.get(code, code)
            r = conn.execute(
                text(f'UPDATE public_bodies SET {bump("public_bodies", stamped)}'
                     f'name = :n WHERE body_code = :c'),
                {"n": name, "c": target})
            written += r.rowcount
            print(f"  name   {target:<34} -> {name}")

        # 5. registrations
        for row in CREATE_ROWS:
            if conn.execute(text("SELECT 1 FROM public_bodies WHERE body_code=:c"),
                            {"c": row["body_code"]}).fetchone():
                print(f"  create {row['body_code']}: already present, skipped")
                continue
            jur = conn.execute(
                text("SELECT id FROM jurisdictions WHERE name ILIKE :p LIMIT 1"),
                {"p": f"%{row['jurisdiction_match']}%"}).fetchone()
            if jur is None:
                raise SystemExit(
                    f"cannot register {row['body_code']}: jurisdiction "
                    f"{row['jurisdiction_match']!r} not found")
            conn.execute(
                text("INSERT INTO public_bodies (jurisdiction_id, name, slug, "
                     "body_code, created_at, updated_at) "
                     "VALUES (:j, :n, :s, :c, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                {"j": jur[0], "n": row["name"], "s": row["slug"],
                 "c": row["body_code"]})
            written += 1
            print(f"  create {row['body_code']} -> {row['name']!r}")

        # ── postconditions: fail closed ──
        problems: list[str] = []
        for old in CODE_RENAMES:
            for table, column in columns:
                n = conn.execute(
                    text(f'SELECT count(*) FROM "{table}" WHERE "{column}" = :v'),
                    {"v": old}).scalar()
                if n:
                    problems.append(f"{table}.{column} still holds {old!r}: {n}")
        duplicates = conn.execute(text(
            "SELECT body_code, count(*) FROM public_bodies GROUP BY body_code "
            "HAVING count(*) > 1")).fetchall()
        for value, n in duplicates:
            problems.append(
                f"public_bodies.body_code {value!r} is duplicated ({n} rows)")
        dangling = conn.execute(text(
            "SELECT m.body, count(*) FROM meetings m LEFT JOIN public_bodies pb "
            "ON pb.body_code = m.body WHERE pb.id IS NULL GROUP BY m.body")).fetchall()
        for value, n in dangling:
            problems.append(
                f"meetings.body {value!r} has no public_bodies row ({n})")

        print()
        if problems:
            for problem in problems:
                print(f"  POSTCONDITION FAIL: {problem}")
            raise SystemExit("rolling back — postconditions not met")
        print("  postconditions: clean")

    print(f"\nrows written  : {written}")
    print(f"backup        : data/archive/body-registry-cleanup-{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
