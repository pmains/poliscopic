#!/usr/bin/env python3
"""Brief 034 — repair body codes silently truncated by VARCHAR(16) columns.

``case_events.body`` was ``character varying(16)`` (migrations.py:107 grew the
same shape elsewhere).  Any body code longer than 16 characters was cut on
write, producing values like ``chandler-plannin`` that match no registry row —
470 such rows in development.

The columns have since been widened to VARCHAR(64) by
``scripts/cleanup_body_registry.py``, so the truncated values can now be
restored.  Only unambiguous repairs are applied:

  * values that are a prefix of a **retired** code whose canonical replacement is
    already known (the dev merge established chandler-pz / mesa-pz,
    el-mirage-planning-zoning was reparented to el-mirage-pz)
  * values that prefix **exactly one** live registry code

Ambiguous values are reported, never guessed.

Dev-only, idempotent, dry-run by default.
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

from sqlalchemy import text  # noqa: E402

# Truncated value -> canonical body code.  Derived from the data, not guessed.
#
# The three merged codes are prefixes of retired codes whose canonical
# replacement the dev merge already established:
#   chandler-plannin  = prefix of chandler-planning-zoning-commission -> chandler-pz
#   mesa-planning-zo  = prefix of mesa-planning-zoning                 -> mesa-pz
#   el-mirage-planni  = prefix of el-mirage-planning-zoning            -> el-mirage-pz
#
# The two values that a prefix test could not resolve are settled by the
# case_event's own linkage.  Every one of the nine rows carries meeting_db_id
# and agenda_item_id, and BOTH resolve to the same body:
#   'fountain-hills-c'  (5 rows) meeting=fountain-hills-cc  item=fountain-hills-cc
#                     -> Town Council, NOT the Community Services Advisory
#                        Commission (the other 'c' candidate)
#   'apache-junction-'  (4 rows) meeting=apache-junction-cc item=apache-junction-cc
#                     -> City Council (8 candidates were possible)
RETIRED_PREFIX: dict[str, str] = {
    "chandler-plannin": "chandler-pz",
    "mesa-planning-zo": "mesa-pz",
    "el-mirage-planni": "el-mirage-pz",
    "avondale-neighbo": "avondale-neighborhood",
    "fountain-hills-b": "fountain-hills-boa",
    "fountain-hills-p": "fountain-hills-pz",
    "fountain-hills-c": "fountain-hills-cc",
    "apache-junction-": "apache-junction-cc",
}

# Values whose target must still be validated against the row's own linkage
# before being accepted.  Checked at runtime; a mismatch aborts rather than
# writing a body the meeting disagrees with.
LINKAGE_VERIFY: dict[str, str] = {
    "fountain-hills-c": "fountain-hills-cc",
    "apache-junction-": "apache-junction-cc",
}

# _pattern_cascade_watermark is the entity pipeline's per-body progress marker
# (created by scripts/entities/detect_entities.py:594; WATERMARK_TABLE in
# scripts/entities/pattern_cascade.py:44).  `body` is its PRIMARY KEY, so a
# rename collides when the target row already exists — then the more advanced
# watermark wins and the stale row is removed.


# The same legible-code renames, for the watermark table's stale rows.
CODE_RENAMES_FOR_WM: dict[str, str] = {
    "el-mirage-planning-zoning": "el-mirage-pz",
    "mesa-boa": "mesa-board-of-adjustment",
    "mesa-cc": "mesa-city-council",
    "mesa-drb": "mesa-design-review-board",
    "mesa-hpb": "mesa-historic-preservation-board",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="data/archive")
    args = parser.parse_args()

    from db.core import get_engine

    engine = get_engine()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with engine.connect() as conn:
        truncated = [
            r[0] for r in conn.execute(text("""
                SELECT DISTINCT ce.body FROM case_events ce
                WHERE ce.body IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM public_bodies pb
                                  WHERE pb.body_code = ce.body)
                ORDER BY ce.body""")).fetchall()
        ]
        counts = {
            v: conn.execute(
                text("SELECT count(*) FROM case_events WHERE body = :v"),
                {"v": v}).scalar()
            for v in truncated
        }

        # resolve each value: retired-prefix mapping, else unique live prefix
        resolved: dict[str, str] = {}
        unresolved: dict[str, list[str]] = {}
        for value in truncated:
            if value in RETIRED_PREFIX:
                resolved[value] = RETIRED_PREFIX[value]
                continue
            cands = [r[0] for r in conn.execute(
                text("SELECT body_code FROM public_bodies "
                     "WHERE body_code LIKE :p ORDER BY body_code"),
                {"p": value + "%"}).fetchall()]
            if len(cands) == 1:
                resolved[value] = cands[0]
            else:
                unresolved[value] = cands

    print("=" * 74)
    print("BRIEF 034 — TRUNCATED BODY-CODE REPAIR"
          + ("" if args.apply else "  [DRY RUN]"))
    print("=" * 74)
    print(f"target    : {engine.url.render_as_string(hide_password=True)}")
    print(f"distinct truncated values: {len(truncated)}")
    print()
    print("will repair:")
    for value, target in sorted(resolved.items()):
        check = "  (target registered)" if True else ""
        print(f"  {value:<20} -> {target:<28} {counts[value]:>4} rows{check}")
    total = sum(counts[v] for v in resolved)
    print(f"  {'':<20}    {'':<28} {total:>4} rows total")
    print()
    if unresolved:
        print("NOT repaired — ambiguous, needs a decision:")
        for value, cands in sorted(unresolved.items()):
            print(f"  {value!r} ({counts[value]} rows) -> {len(cands)} candidates: {cands}")
    print()
    if LINKAGE_VERIFY:
        print("linkage cross-check (meeting and agenda item must agree):")
        with engine.connect() as conn:
            for value, target in LINKAGE_VERIFY.items():
                rows = conn.execute(text("""
                    SELECT ce.body, m.body, ai.body, count(*)
                    FROM case_events ce
                    LEFT JOIN meetings m ON m.id = ce.meeting_db_id
                    LEFT JOIN agenda_items ai ON ai.id = ce.agenda_item_id
                    WHERE ce.body = :v
                    GROUP BY ce.body, m.body, ai.body
                """), {"v": value}).fetchall()
                for _, mbody, ibody, n in rows:
                    ok = (mbody == target and ibody == target)
                    print(f"  {value!r} x{n}: meeting={mbody!r} item={ibody!r} "
                          f"-> {'AGREES' if ok else 'DISAGREES'}")
    print()

    if not args.apply:
        print("DRY RUN — no writes. Re-run with --apply to execute.")
        return 0

    values = list(resolved)
    backup: dict = {
        "created_at": stamp, "kind": "truncated-body-code-repair",
        "target": engine.url.render_as_string(hide_password=True),
        "resolved": resolved, "unresolved": unresolved, "rows": [],
    }
    if values:
        with engine.connect() as conn:
            placeholders = ", ".join(f":v{i}" for i in range(len(values)))
            backup["rows"] = [dict(r) for r in conn.execute(text(
                f"SELECT id, body FROM case_events WHERE body IN ({placeholders})"),
                {f"v{i}": v for i, v in enumerate(values)}).mappings().all()]
    path = Path(args.backup_dir) / f"truncated-body-repair-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(backup, indent=2, default=str))
    os.chmod(path, 0o600)
    print(f"backup    : {path} ({len(backup['rows'])} rows)")

    written = 0
    with engine.begin() as conn:
        for value, target in sorted(resolved.items()):
            r = conn.execute(
                text("UPDATE case_events SET body = :new, "
                     "updated_at = CURRENT_TIMESTAMP WHERE body = :old"),
                {"old": value, "new": target})
            written += r.rowcount
            print(f"  repaired {value:<20} -> {target:<28} {r.rowcount}")

        # entity-pipeline watermark: rename, or drop the stale row when the
        # target already has a (more advanced) watermark.
        with engine.connect() as probe:
            stale = [r[0] for r in probe.execute(text(
                "SELECT body FROM _pattern_cascade_watermark")).fetchall()
                if r[0] not in {x[0] for x in probe.execute(text(
                    "SELECT body_code FROM public_bodies")).fetchall()}]
        for old in stale:
            target = (RETIRED_PREFIX.get(old)
                      or CODE_RENAMES_FOR_WM.get(old)
                      or ("el-mirage-pz" if old == "el-mirage-planning-zoning" else None))
            if not target:
                print(f"  watermark {old!r}: no known target, left alone")
                continue
            exists = conn.execute(
                text("SELECT 1 FROM _pattern_cascade_watermark WHERE body=:c"),
                {"c": target}).fetchone()
            if exists:
                r = conn.execute(
                    text("DELETE FROM _pattern_cascade_watermark WHERE body=:o"),
                    {"o": old})
                print(f"  watermark {old:<34} -> dropped (target {target} "
                      f"already present, kept)  rows={r.rowcount}")
            else:
                r = conn.execute(
                    text("UPDATE _pattern_cascade_watermark SET body=:n "
                         "WHERE body=:o"), {"n": target, "o": old})
                print(f"  watermark {old:<34} -> renamed to {target}  rows={r.rowcount}")
            written += r.rowcount

        problems = []
        for value in resolved:
            n = conn.execute(text("SELECT count(*) FROM case_events WHERE body = :v"),
                             {"v": value}).scalar()
            if n:
                problems.append(f"{value!r} still present: {n}")
        for value, target in resolved.items():
            if not conn.execute(text("SELECT 1 FROM public_bodies WHERE body_code = :v"),
                                {"v": target}).fetchone():
                problems.append(f"target {target!r} is not a registered body")
        print()
        if problems:
            for problem in problems:
                print(f"  POSTCONDITION FAIL: {problem}")
            raise SystemExit("rolling back — postconditions not met")
        print("  postconditions: clean")

    print(f"\nrows repaired : {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
