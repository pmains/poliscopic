#!/usr/bin/env python3
"""Read-only preflight: does the live schema already satisfy every `_migrate_col`?

Brief 032 review finding 6, corrected by re-review finding 4.

`_migrate_col` swallows **all** exceptions:

    try:
        ALTER TABLE ... ADD COLUMN ...
    except Exception:
        pass   # "race: parallel worker may have added it first"

On a table whose attribute numbers are exhausted, PostgreSQL refuses with
"tables can have at most 1600 columns" — and that failure is swallowed too.  So
a genuinely missing required column would look exactly like a harmless race.

This preflight proves, WITHOUT WRITING ANYTHING, that every column the migration
code expects is already present.  It parses the `_migrate_col(...)` calls out of
`scripts/db/migrations.py` rather than keeping a hand-maintained list, so it
cannot drift from the code it checks.

## Conditional / retired markers

Not every `_migrate_col` call represents a column that should exist in steady
state.  Some sit behind an earlier `return`, so they are only reachable while an
older schema shape still exists.  Those are declared in `CONDITIONAL_MARKERS`
and reported **separately** from required markers, so they are visible without
being blockers.

Each exemption carries two safety properties so it cannot go stale silently:

  * `guard_source` must still appear verbatim in `migrations.py`; if the guard is
    removed, the exemption is void and the marker is required again;
  * `required_when` names the column whose PRESENCE makes the marker required
    again — checked live, so the exemption self-cancels the moment the older
    shape returns.

Exit codes:  0 = nothing required is missing;  1 = a required marker is missing;
2 = error.

This script never writes: it issues catalog SELECTs only.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS = PROJECT_ROOT / "scripts" / "db" / "migrations.py"

# PostgreSQL's hard limit; exceeding it is what makes ADD COLUMN fail.
PG_MAX_COLUMNS = 1600


@dataclass(frozen=True)
class ConditionalMarker:
    """A `_migrate_col` expectation that is only live under an older schema."""

    guard_source: str
    """Verbatim guard text that must still exist in migrations.py."""

    required_when: tuple[str, str]
    """(table, column) whose presence makes this marker required again."""

    reason: str


# Keyed by (table, column) as they appear in the `_migrate_col` calls.
CONDITIONAL_MARKERS: dict[tuple[str, str], ConditionalMarker] = {
    ("persons", "_membership_migrated"): ConditionalMarker(
        guard_source='if "active_from" not in existing_cols:',
        required_when=("persons", "active_from"),
        reason=(
            "_migrate_membership_model() returns before reaching its _migrate_col "
            "call when persons.active_from is absent, and "
            "_drop_deprecated_person_columns() drops _membership_migrated anyway. "
            "Its absence is intentional steady state, NOT a missing column."
        ),
    ),
}


def expected_columns(path: Path = MIGRATIONS) -> list[tuple[str, str]]:
    """(table, column) pairs from every literal `_migrate_col(...)` call."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_migrate_col"):
            continue
        args = node.args
        if len(args) < 3:
            continue
        table, column = args[1], args[2]
        if isinstance(table, ast.Constant) and isinstance(column, ast.Constant):
            found.append((str(table.value), str(column.value)))
    return sorted(set(found))


def main() -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from sqlalchemy import text
    from db.core import get_engine

    source = MIGRATIONS.read_text(encoding="utf-8")
    expected = expected_columns()
    if not expected:
        print("ERROR: no _migrate_col calls found — did the helper get renamed?")
        return 2

    engine = get_engine()
    with engine.connect() as conn:
        ident = {
            "database": conn.execute(text("select current_database()")).scalar(),
            "schema": conn.execute(text("select current_schema()")).scalar(),
        }
        print(f"target: {ident['database']} / schema {ident['schema']}")
        print(f"expected markers from _migrate_col: {len(expected)}")

        rows = conn.execute(text("""
            select c.relname, a.attname,
                   count(*) filter (where a.attnum > 0 and not a.attisdropped)
                       over (partition by c.relname) as visible,
                   max(a.attnum) over (partition by c.relname) as max_attnum
            from pg_attribute a join pg_class c on c.oid = a.attrelid
            where c.relnamespace = current_schema()::regnamespace
        """)).fetchall()
        present = {(r[0], r[1]) for r in rows}
        stress = {r[0]: (r[2], r[3]) for r in rows}
        present_tables = {r[0] for r in rows}

        required: list[tuple[str, str]] = []
        conditional: list[tuple[tuple[str, str], ConditionalMarker, str]] = []
        stale_exemptions: list[tuple[tuple[str, str], ConditionalMarker]] = []

        for pair in expected:
            if pair in present:
                continue
            rule = CONDITIONAL_MARKERS.get(pair)
            if rule is None:
                required.append(pair)
                continue
            # guard must still exist verbatim, else the exemption is void
            if rule.guard_source not in source:
                stale_exemptions.append((pair, rule))
                required.append(pair)
                continue
            # self-cancel: if the older shape is back, the marker is required
            gate_table, gate_column = rule.required_when
            if (gate_table, gate_column) in present:
                conditional.append((pair, rule, "PRECONDITION PRESENT"))
                required.append(pair)
            elif gate_table not in present_tables:
                conditional.append((pair, rule, "gate table absent"))
            else:
                conditional.append((pair, rule, "intentional steady state"))

        print()
        if stale_exemptions:
            print("STALE EXEMPTIONS — the guard that justified these is gone:")
            for pair, rule in stale_exemptions:
                print(f"  - {pair[0]}.{pair[1]}  (missing guard: "
                      f"{rule.guard_source!r})")
            print()

        if required:
            print("MISSING REQUIRED COLUMNS (ADD COLUMN would have failed and been")
            print("swallowed by _migrate_col):")
            for table, column in required:
                stat = stress.get(table)
                extra = f"  (visible={stat[0]} max_attnum={stat[1]})" if stat else ""
                print(f"  - {table}.{column}{extra}")
        else:
            print("OK: every required column expected by _migrate_col is present.")

        if conditional:
            print()
            print("Conditional / retired markers (reported separately, NOT blockers):")
            for (table, column), rule, why in conditional:
                print(f"  - {table}.{column}  [{why}]")
                print(f"      {rule.reason}")

        near = {t: s for t, s in stress.items() if s[1] and s[1] >= 1500}
        if near:
            print()
            print("Tables approaching the PostgreSQL attribute ceiling "
                  f"(max {PG_MAX_COLUMNS}); ADD COLUMN would fail here:")
            for table, (visible, max_attnum) in sorted(near.items()):
                print(f"  - {table}: visible={visible} max_attnum={max_attnum}")

    return 1 if required else 0


if __name__ == "__main__":
    raise SystemExit(main())
