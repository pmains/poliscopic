#!/usr/bin/env python3
"""Brief 035 — retire the fictional ``phoenix-gp`` body.

``phoenix-gp`` is not a governing body.  The 218 records are Legistar
"General Information Packet" publications — citywide informational reports with
no case numbers and no linkage to any body (569 agenda items, all with an empty
``c_number_base``; no shared case numbers with any other body).

Pete's model (2026-09-18): such documents are retained, and a document that is
not linked to a body simply has no body — a NULL reference is correct.  So this
script:

  1. drops NOT NULL on the body-code columns that reference the fictional code
     (they must be able to say "no body" — the NOT NULL string is why a
     fictional registry row had to exist in the first place)
  2. clears the fictional code from every referencing table, so no row claims a
     body that does not exist
  3. deletes the ``public_bodies`` row
  4. verifies no reference remains and no body is left without a row

Rows whose ``body`` is part of a PRIMARY KEY cannot hold NULL, so those are
deleted instead (they are progress markers, not content).

Dev-only.  Idempotent.  Dry-run by default; applies in one transaction and rolls
back if a postcondition is unmet.
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

FICTIONAL_CODE = "phoenix-gp"
PROSE_TABLES = ("articles",)


def _has_stamp(insp, table: str) -> bool:
    """True when ``table`` carries the sync's change-detection column.

    Dropping NOT NULL does not add or remove columns, so an inspector taken
    before the ALTERs is still accurate for this question.
    """
    try:
        return "updated_at" in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="data/archive")
    args = parser.parse_args()

    from db.core import get_engine

    engine = get_engine()
    insp = inspect(engine)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # every column that could hold the code, and whether it is in a PRIMARY KEY
    # (only a PK forbids NULL; a UNIQUE constraint permits it in PostgreSQL, so
    #  meetings.body — UNIQUE (body, meeting_id) — can hold NULL)
    targets: list[tuple[str, str, bool]] = []   # (table, column, is_pk)
    with engine.connect() as conn:
        keyed = set()
        for r in conn.execute(text("""
            SELECT cl.relname, att.attname
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            JOIN LATERAL unnest(con.conkey) AS k ON TRUE
            JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k
            WHERE ns.nspname = current_schema() AND con.contype = 'p'
        """)).fetchall():
            keyed.add((r[0], r[1]))
        for table in insp.get_table_names():
            if table in PROSE_TABLES or table == "public_bodies":
                # public_bodies.body_code is the registry's own column; the
                # DELETE of the row handles it and it must never be nulled.
                continue
            try:
                cols = {c["name"]: c for c in insp.get_columns(table)}
            except Exception:
                continue
            for column in ("body",):
                if column in cols:
                    targets.append((table, column, (table, column) in keyed))

        present = conn.execute(text(
            "SELECT 1 FROM public_bodies WHERE body_code = :c"),
            {"c": FICTIONAL_CODE}).fetchone()
        counts = {}
        for table, column, _ in targets:
            n = conn.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE "{column}" = :c'),
                {"c": FICTIONAL_CODE}).scalar()
            if n:
                counts[f"{table}.{column}"] = n

    print("=" * 76)
    print("RETIRE FICTIONAL BODY " + repr(FICTIONAL_CODE)
          + ("" if args.apply else "   [DRY RUN]"))
    print("=" * 76)
    print(f"target              : {engine.url.render_as_string(hide_password=True)}")
    print(f"public_bodies row   : {'present -> will be deleted' if present else 'already absent'}")
    print()
    print("references to clear:")
    for key, n in sorted(counts.items()):
        table, column = key.split(".")
        is_pk = (table, column) in {(t, c) for t, c, k in targets if k}
        mode = ("DELETE row (PRIMARY KEY column cannot be NULL)" if is_pk
                else "SET NULL (drop NOT NULL first)")
        print(f"  {key:<40} {n:>5} rows   {mode}")
    if not counts:
        print("  none")
    print()
    print("NOT NULL will be dropped on:")
    for table, column, is_pk in targets:
        if not is_pk and f"{table}.{column}" in counts:
            print(f"  {table}.{column}")
    print()

    if not args.apply:
        print("DRY RUN — no writes. Re-run with --apply to execute.")
        return 0

    # backup
    backup: dict = {"created_at": stamp, "kind": "retire-fictional-body",
                    "code": FICTIONAL_CODE, "rows": {}}
    with engine.connect() as conn:
        for key, n in counts.items():
            table, column = key.split(".")
            cols = {c["name"] for c in insp.get_columns(table)}
            if "id" not in cols:
                continue
            rows = conn.execute(
                text(f'SELECT id, "{column}" AS body FROM "{table}" '
                     f'WHERE "{column}" = :c'),
                {"c": FICTIONAL_CODE}).mappings().all()
            backup["rows"][key] = [dict(r) for r in rows]
    path = Path(args.backup_dir) / f"retire-phoenix-gp-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(backup, indent=2, default=str))
    os.chmod(path, 0o600)
    print(f"backup: {path} ({sum(len(v) for v in backup['rows'].values())} rows)")

    written = 0
    with engine.begin() as conn:
        for table, column, is_pk in targets:
            n = conn.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE "{column}" = :c'),
                {"c": FICTIONAL_CODE}).scalar()
            if not n:
                continue
            if is_pk:
                r = conn.execute(
                    text(f'DELETE FROM "{table}" WHERE "{column}" = :c'),
                    {"c": FICTIONAL_CODE})
                print(f"  deleted {table}.{column}: {r.rowcount}")
            else:
                conn.execute(text(
                    f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP NOT NULL'))
                # The cleared rows must advance their change-detection stamp, or
                # incremental sync never sees the change: it selects work with
                # ``WHERE updated_at > :since``.  Omitting this is exactly what
                # left 218 phoenix-gp meetings on production after a sync that
                # reported success (Brief 033 §1 / Brief 037 §2).
                stamp_sql = ("updated_at = CURRENT_TIMESTAMP, "
                             if _has_stamp(insp, table) else "")
                r = conn.execute(
                    text(f'UPDATE "{table}" SET {stamp_sql}"{column}" = NULL '
                         f'WHERE "{column}" = :c'),
                    {"c": FICTIONAL_CODE})
                print(f"  cleared {table}.{column} -> NULL (NOT NULL dropped): {r.rowcount}")
            written += r.rowcount

        r = conn.execute(text("DELETE FROM public_bodies WHERE body_code = :c"),
                         {"c": FICTIONAL_CODE})
        written += r.rowcount
        print(f"  deleted public_bodies row: {r.rowcount}")

        # ── postconditions ──
        problems = []
        for table, column, _ in targets:
            n = conn.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE "{column}" = :c'),
                {"c": FICTIONAL_CODE}).scalar()
            if n:
                problems.append(f"{table}.{column} still holds {FICTIONAL_CODE!r}: {n}")
        if conn.execute(text("SELECT 1 FROM public_bodies WHERE body_code = :c"),
                        {"c": FICTIONAL_CODE}).fetchone():
            problems.append("public_bodies still has the row")
        # the packets themselves must survive — deleting them was never intended
        kept = conn.execute(text(
            "SELECT count(*) FROM meetings WHERE public_body_id IS NULL")).scalar()
        if kept < 220:
            problems.append(
                f"body-less meetings dropped to {kept} (expected >= 220) — "
                f"the packet records were not preserved")
        dangling = conn.execute(text(
            "SELECT m.body, count(*) FROM meetings m LEFT JOIN public_bodies pb "
            "ON pb.body_code = m.body WHERE m.body IS NOT NULL AND pb.id IS NULL "
            "GROUP BY m.body")).fetchall()
        for value, n in dangling:
            problems.append(f"meetings.body {value!r} has no public_bodies row ({n})")
        print()
        if problems:
            for p in problems:
                print(f"  POSTCONDITION FAIL: {p}")
            raise SystemExit("rolling back — postconditions not met")
        print("  postconditions: clean")

    print(f"\nrows changed: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
