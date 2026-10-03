#!/usr/bin/env python3
"""Runtime reference enforcement for the dev→prod sync.

The authority the sync path actually calls. Batch 3's isolated contract was NOT
enforced by the runtime; tests asserting list membership and order do not make a
per-row invariant true. This module is the enforcement layer.

ESTABLISHED REFERENCE SEMANTICS (traced from schema and sync code)
    Two live representations exist and both are covered:

      1. CODE STRING — ``<table>.body`` / ``<table>.body_code`` -> ``public_bodies.body_code``
      2. INTEGER FK  — ``<table>.public_body_id``               -> ``public_bodies.id``

    No FK constraint is declared for ``public_body_id`` in the models, so the
    database does not enforce (2). This module does.

SENTINEL POLICY
    ``"__skip__"`` and ``""`` are scrape placeholders. They are explicitly INVALID
    and UNRESOLVED: they are never promoted into ``public_bodies``, are never
    counted as valid references, and never count as a satisfied parent.

SINGLE DEPENDENCY AUTHORITY
    ``DEPENDENCY_EDGES`` is imported from ``scripts/ops/propagation_contract.py``;
    the reconcile order is owned by ``db.sync_declarations``. ``parity_problems()``
    asserts both stay consistent, so the runtime cannot drift from the contract.

TRANSACTION SCOPE — STATED HONESTLY
    Ordinary sync commits per chunk and per table; there is NO single transaction
    spanning a parent and its dependents, and providing one needs a larger
    redesign. This module therefore does not claim atomicity. It makes the failure
    DIRECTION safe: parents are applied and checked BEFORE any dependent is
    written, and a parent skip ABORTS before dependent writes. A mid-sync failure
    can therefore leave parent-present / dependent-absent, which dangles nothing.
    The dangerous direction — a dependent written without its parent — is what is
    eliminated. That is fail-closed ordering, not a transactional guarantee.

Read-only apart from the caller's own writes. No network, no credentials.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import bindparam, inspect, text

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts"),
              os.path.join(_REPO_ROOT, "scripts", "ops")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from propagation_contract import (  # noqa: E402
    BODY_CODE_COLUMNS,
    DEPENDENCY_EDGES,
    PUBLIC_BODY_DEPENDENTS,
    PUBLIC_BODY_ID_COLUMN,
    SENTINELS,
)

PARENT_TABLES: tuple[str, ...] = tuple(sorted({p for p, _ in DEPENDENCY_EDGES}))
DEPENDENT_TABLES: tuple[str, ...] = tuple(sorted(PUBLIC_BODY_DEPENDENTS))
PARENT_TABLE = "public_bodies"

SENTINEL_VALUES: frozenset[str] = SENTINELS


class ReferenceGuardError(RuntimeError):
    """A reference invariant cannot be satisfied — fail closed."""


def is_sentinel(value: object) -> bool:
    """True when a value is an invalid/unresolved placeholder, never a reference."""
    if value is None:
        return True
    return str(value) in SENTINEL_VALUES or not str(value).strip()


# ── parity: one authority ────────────────────────────────────────────────


def parity_problems() -> list[str]:
    """The runtime declarations must stay consistent with the contract."""
    from db.sync_declarations import ALL_SYNC_TABLES
    from db.sync_declarations import RECONCILE_ORDER as reconcile
    import propagation_contract as contract
    contract_reconcile = contract._reconcile_order()

    problems: list[str] = []
    tables = list(ALL_SYNC_TABLES)
    for parent, dependent in DEPENDENCY_EDGES:
        for name in (parent, dependent):
            if name not in tables:
                problems.append(f"{name!r} missing from ALL_SYNC_TABLES")
        if parent in tables and dependent in tables:
            if tables.index(parent) > tables.index(dependent):
                problems.append(
                    f"{parent!r} must precede {dependent!r} in ALL_SYNC_TABLES"
                )

    for table, columns in PUBLIC_BODY_DEPENDENTS.items():
        if table not in tables:
            problems.append(f"public-body dependent {table!r} missing from ALL_SYNC_TABLES")
        if not columns:
            problems.append(f"public-body dependent {table!r} declares no columns")

    # reconcile must delete dependents before parents
    for parent, dependent in DEPENDENCY_EDGES:
        if parent in reconcile and dependent in reconcile:
            if reconcile.index(parent) < reconcile.index(dependent):
                problems.append(
                    f"reconcile must delete {dependent!r} before {parent!r}"
                )

    # the contract's order must remain a consistent PROJECTION of the authority
    shared = [t for t in contract_reconcile if t in reconcile]
    projection = [t for t in reconcile if t in set(contract_reconcile)]
    if shared != projection:
        problems.append(
            "propagation_contract.RECONCILE_ORDER is no longer a consistent "
            f"projection of db.sync_declarations.RECONCILE_ORDER: {shared} != {projection}"
        )
    return problems


def assert_parity() -> None:
    problems = parity_problems()
    if problems:
        raise ReferenceGuardError("declaration drift: " + "; ".join(problems))


# ── parent coverage for an exact dependent batch ─────────────────────────


def required_parent_codes(dependent_rows) -> set[str]:
    """Body codes an exact dependent batch references. Sentinels excluded."""
    codes: set[str] = set()
    for row in dependent_rows:
        if not hasattr(row, "get"):
            continue
        value = row.get("body")
        if value is None:
            value = row.get("body_code")
        if not is_sentinel(value):
            codes.add(str(value).strip())
    return codes


def required_parent_ids(dependent_rows) -> set[int]:
    """Integer-FK parents an exact dependent batch references."""
    ids: set[int] = set()
    for row in dependent_rows:
        if not hasattr(row, "get"):
            continue
        value = row.get(PUBLIC_BODY_ID_COLUMN)
        if isinstance(value, int):
            ids.add(value)
    return ids


def _has(engine, table: str) -> bool:
    try:
        return table in set(inspect(engine).get_table_names())
    except Exception:
        return False


def force_include_parent_keys(dev_engine, dependent_rows, *,
                              parent_table: str = PARENT_TABLE) -> list:
    """Primary keys of parents that MUST be upserted for this dependent batch.

    Called before a dependent table is synced. A parent absent from the SOURCE is
    a hard failure — no sync can supply it. Parents that exist on dev but are
    absent on the target are included here, which is what makes them re-sent even
    when their timestamp predates the checkpoint.
    """
    codes = required_parent_codes(dependent_rows)
    ids = required_parent_ids(dependent_rows)
    if not codes and not ids:
        return []
    if not _has(dev_engine, parent_table):
        raise ReferenceGuardError(
            f"required parent table {parent_table!r} is absent from the source"
        )

    keys: set = set()
    absent: list[str] = []
    with dev_engine.connect() as conn:
        for code in sorted(codes):
            row = conn.execute(
                text(f'SELECT id FROM "{parent_table}" WHERE body_code = :c LIMIT 1'),
                {"c": code},
            ).first()
            if row is None:
                absent.append(f"body_code={code!r}")
            else:
                keys.add(row[0])
        for pid in sorted(ids):
            row = conn.execute(
                text(f'SELECT id FROM "{parent_table}" WHERE id = :i LIMIT 1'),
                {"i": pid},
            ).first()
            if row is None:
                absent.append(f"id={pid}")
            else:
                keys.add(row[0])
    if absent:
        raise ReferenceGuardError(
            "required parent(s) absent from the SOURCE; sync cannot supply them: "
            + ", ".join(sorted(absent))
        )
    return sorted(keys)


def missing_target_parents(prod_engine, *, codes, ids,
                           parent_table: str = PARENT_TABLE) -> dict[str, list]:
    """Required parents absent on the TARGET. Read-only."""
    missing_codes: list[str] = []
    missing_ids: list[int] = []
    with prod_engine.connect() as conn:
        for code in sorted(codes):
            if conn.execute(
                text(f'SELECT 1 FROM "{parent_table}" WHERE body_code = :c LIMIT 1'),
                {"c": code},
            ).first() is None:
                missing_codes.append(code)
        for pid in sorted(ids):
            if conn.execute(
                text(f'SELECT 1 FROM "{parent_table}" WHERE id = :i LIMIT 1'),
                {"i": pid},
            ).first() is None:
                missing_ids.append(pid)
    return {"codes": missing_codes, "ids": missing_ids}


def assert_parents_synced(table: str, skipped_by_table) -> None:
    """Abort BEFORE writing a dependent when a required parent skipped rows."""
    for parent in PARENT_TABLES:
        skipped = skipped_by_table.get(parent, 0)
        if skipped:
            raise ReferenceGuardError(
                f"refusing to write {table!r}: required parent {parent!r} skipped "
                f"{skipped} row(s) — no dependent write across an unsatisfied "
                f"reference boundary"
            )


def _parent_rows_by_value(
    engine, *, column: str, values: list[object], chunk_size: int = 500
) -> dict[object, list[dict]]:
    """Return reviewed parent identity fields grouped by lookup value.

    The selected fields are restricted to columns present on both fixture and real
    schemas. ``id`` and ``body_code`` are mandatory because they are the two live
    reference representations. Values are fetched in bounded batches so validating
    a dependent table takes a handful of database round trips rather than one query
    and one schema inspection per distinct reference.
    """
    columns = {c["name"] for c in inspect(engine).get_columns(PARENT_TABLE)}
    required = {"id", "body_code"}
    if not required <= columns:
        raise ReferenceGuardError(
            f"{PARENT_TABLE} lacks required identity columns: "
            f"{sorted(required - columns)}"
        )
    identity_columns = [
        name for name in ("id", "body_code", "name", "jurisdiction_id", "slug")
        if name in columns
    ]
    if column not in columns:
        raise ReferenceGuardError(
            f"{PARENT_TABLE} lacks lookup column {column!r}"
        )
    if column not in identity_columns:
        identity_columns.append(column)
    selected = ", ".join(f'"{name}"' for name in identity_columns)
    grouped = {value: [] for value in values}
    statement = text(
        f'SELECT {selected} FROM "{PARENT_TABLE}" WHERE "{column}" IN :values'
    ).bindparams(bindparam("values", expanding=True))
    with engine.connect() as conn:
        for start in range(0, len(values), chunk_size):
            chunk = values[start:start + chunk_size]
            for row in conn.execute(statement, {"values": chunk}).mappings():
                result = dict(row)
                grouped.setdefault(result[column], []).append(result)
    return grouped


def assert_target_parent_coverage(
    dev_engine, prod_engine, table: str, *, since=None
) -> None:
    """Refuse before a dependent write unless every exact parent is on target.

    For a full sync this examines all source rows. For an incremental sync,
    ``since`` restricts the check to the exact changed-row population eligible for
    this run. Historical rows that are not being written cannot create a new target
    reference violation; newly changed rows still fail closed.
    """
    declared = PUBLIC_BODY_DEPENDENTS.get(table)
    if declared is None:
        return

    source_columns = {c["name"] for c in inspect(dev_engine).get_columns(table)}
    absent_columns = set(declared) - source_columns
    if absent_columns:
        raise ReferenceGuardError(
            f"{table!r} is missing declared reference columns "
            f"{sorted(absent_columns)}"
        )

    codes: set[str] = set()
    ids: set[int] = set()
    with dev_engine.connect() as conn:
        for column in declared:
            changed = (
                ' AND "updated_at" > :since' if since is not None else ""
            )
            rows = conn.execute(
                text(f'SELECT DISTINCT "{column}" FROM "{table}" '
                     f'WHERE "{column}" IS NOT NULL{changed}'),
                {"since": since} if since is not None else {},
            ).fetchall()
            for (value,) in rows:
                if column == PUBLIC_BODY_ID_COLUMN:
                    if not isinstance(value, int):
                        raise ReferenceGuardError(
                            f"{table}.{column} has non-integer reference {value!r}"
                        )
                    ids.add(value)
                elif is_sentinel(value):
                    raise ReferenceGuardError(
                        f"{table}.{column} has invalid/unresolved sentinel {value!r}"
                    )
                else:
                    codes.add(str(value).strip())

    for column, values in (("body_code", sorted(codes)), ("id", sorted(ids))):
        source_by_value = _parent_rows_by_value(
            dev_engine, column=column, values=values
        )
        target_by_value = _parent_rows_by_value(
            prod_engine, column=column, values=values
        )
        for value in values:
            source_rows = source_by_value.get(value, [])
            target_rows = target_by_value.get(value, [])
            if len(source_rows) != 1:
                raise ReferenceGuardError(
                    f"ambiguous/missing source parent for {column}={value!r}: "
                    f"{len(source_rows)} row(s)"
                )
            if len(target_rows) != 1:
                raise ReferenceGuardError(
                    f"ambiguous/missing target parent for {column}={value!r}: "
                    f"{len(target_rows)} row(s)"
                )
            shared = sorted(set(source_rows[0]) & set(target_rows[0]))
            source_identity = {key: source_rows[0][key] for key in shared}
            target_identity = {key: target_rows[0][key] for key in shared}
            if source_identity != target_identity:
                raise ReferenceGuardError(
                    f"conflicting target parent for {column}={value!r}: "
                    f"source={source_identity!r} target={target_identity!r}"
                )


# ── postconditions: both representations ─────────────────────────────────


def scoped_dangling_problems(engine, scope_codes, *,
                             dependent_tables=None) -> list[str]:
    """Scoped postconditions for BOTH live representations.

    For a repair scope this requires ZERO remaining scoped dangling references (not
    merely "no new" ones). A query failure IS a failure — never swallowed.

    Sentinel values are reported as invalid/unresolved references rather than being
    silently accepted.
    """
    from db.sync_declarations import ALL_SYNC_TABLES

    tables = list(dependent_tables or ALL_SYNC_TABLES)
    scope = {str(c).strip() for c in scope_codes if not is_sentinel(c)}
    problems: list[str] = []
    # NOTE: deliberately NO early return on an empty scope. Unresolved sentinels are
    # invalid references regardless of scope and must always be reported; only the
    # "scoped dangling" check depends on the scope being non-empty.

    try:
        present = set(inspect(engine).get_table_names())
    except Exception as exc:
        return [f"postcondition introspection failed: {exc}"]

    for table in tables:
        if table not in present or table == PARENT_TABLE:
            continue
        try:
            columns = {c["name"] for c in inspect(engine).get_columns(table)}
        except Exception as exc:
            problems.append(f"postcondition column lookup failed for {table}: {exc}")
            continue

        with engine.connect() as conn:
            for column in BODY_CODE_COLUMNS:
                if column not in columns:
                    continue
                try:
                    rows = conn.execute(
                        text(
                            f'SELECT x."{column}" AS v, COUNT(*) AS n FROM "{table}" x '
                            f'LEFT JOIN {PARENT_TABLE} pb ON pb.body_code = x."{column}" '
                            f'WHERE x."{column}" IS NOT NULL AND pb.id IS NULL '
                            f'GROUP BY x."{column}"'
                        )
                    ).fetchall()
                except Exception as exc:
                    problems.append(
                        f"postcondition query failed on {table}.{column}: {exc}")
                    continue
                for value, count in rows:
                    if is_sentinel(value):
                        problems.append(
                            f"invalid/unresolved reference: {table}.{column}={value!r} "
                            f"({count} row(s))"
                        )
                    elif str(value).strip() in scope:
                        problems.append(
                            f"scoped dangling reference: {table}.{column}={value!r} "
                            f"({count} row(s))"
                        )

            if PUBLIC_BODY_ID_COLUMN in columns:
                try:
                    rows = conn.execute(
                        text(
                            f'SELECT x."{PUBLIC_BODY_ID_COLUMN}" AS v, COUNT(*) AS n '
                            f'FROM "{table}" x LEFT JOIN {PARENT_TABLE} pb '
                            f'ON pb.id = x."{PUBLIC_BODY_ID_COLUMN}" '
                            f'WHERE x."{PUBLIC_BODY_ID_COLUMN}" IS NOT NULL '
                            f'AND pb.id IS NULL GROUP BY x."{PUBLIC_BODY_ID_COLUMN}"'
                        )
                    ).fetchall()
                except Exception as exc:
                    problems.append(
                        f"postcondition query failed on "
                        f"{table}.{PUBLIC_BODY_ID_COLUMN}: {exc}")
                    continue
                for value, count in rows:
                    problems.append(
                        f"scoped dangling FK: {table}.{PUBLIC_BODY_ID_COLUMN}={value} "
                        f"({count} row(s))"
                    )
    return problems


def newly_dangling_problems(engine, baseline: dict[str, int], *,
                            dependent_tables=None) -> list[str]:
    """Ordinary-sync postcondition: reject NEWLY introduced dangling rows.

    Pre-existing dangling rows are reported in ``baseline`` and are not treated as
    new failures, but they are never hidden: callers should log the baseline set.
    """
    current = dangling_counts(engine, dependent_tables=dependent_tables)
    problems: list[str] = []
    for key, count in current.items():
        if count > baseline.get(key, 0):
            problems.append(
                f"newly dangling reference {key}: {count} (baseline "
                f"{baseline.get(key, 0)})"
            )
    return problems


def dangling_counts(engine, *, dependent_tables=None) -> dict[str, int]:
    """Count dangling body-code references; every inspection/query error raises."""
    from db.sync_declarations import ALL_SYNC_TABLES

    tables = list(dependent_tables or ALL_SYNC_TABLES)
    out: dict[str, int] = {}
    present = set(inspect(engine).get_table_names())
    for table in tables:
        if table not in present or table == PARENT_TABLE:
            continue
        columns = {c["name"] for c in inspect(engine).get_columns(table)}
        for column in BODY_CODE_COLUMNS:
            if column not in columns:
                continue
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        f'SELECT x."{column}" AS v, COUNT(*) AS n FROM "{table}" x '
                        f'LEFT JOIN {PARENT_TABLE} pb ON pb.body_code = x."{column}" '
                        f'WHERE x."{column}" IS NOT NULL AND pb.id IS NULL '
                        f'GROUP BY x."{column}"'
                    )
                ).fetchall()
            for value, count in rows:
                out[f"{table}.{column}={value}"] = int(count)
    return out


__all__ = [
    "DEPENDENCY_EDGES", "DEPENDENT_TABLES", "PARENT_TABLES", "PARENT_TABLE",
    "ReferenceGuardError", "assert_parents_synced", "assert_parity",
    "assert_target_parent_coverage",
    "dangling_counts", "force_include_parent_keys", "is_sentinel",
    "missing_target_parents", "newly_dangling_problems", "parity_problems",
    "required_parent_codes", "required_parent_ids", "scoped_dangling_problems",
]
