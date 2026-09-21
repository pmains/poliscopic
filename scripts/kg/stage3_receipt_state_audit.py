#!/usr/bin/env python3
"""Read-only audit of the Stage 3 processing-receipt state.

This answers recovery questions and nothing else.  The session is opened read
only, every statement is recorded, and the audit refuses to finish if any
mutating statement was issued.  It never creates, updates, or deletes rows or
schema, and it never runs the receipt backfill.

Coverage is measured against the authoritative dry plan's own selected
identities, so the eligible set is never inferred from the receipts it is being
compared against.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from db.core import get_engine  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

KIND = "kg-stage3-processing-receipt-state-audit"
VERSION = "1.0"
TABLE = "public.processing_receipts"
DEFAULT_PLAN = REPO / "data/kg-plans/kg-stage3-processing-dry-plan-20260920T174924Z.json"
IDENTITY_FIELDS = ("source_kind", "source_id", "content_sha256",
                   "extraction_method", "extractor", "extractor_version")
DISTRIBUTION_FIELDS = ("status", "extractor", "extractor_version", "extraction_method")
MUTATING = ("insert ", "update ", "delete ", "create ", "drop ", "alter ", "truncate ", "grant ")


class AuditRefused(RuntimeError):
    pass


def _record(executed: list[str], statement: Any) -> None:
    executed.append(" ".join(str(statement).split()).lower())


def _rows(connection: Any, statement: str, executed: list[str], **params: Any) -> list[dict[str, Any]]:
    _record(executed, statement)
    return [dict(row) for row in connection.execute(text(statement), params).mappings().all()]


def _scalar(connection: Any, statement: str, executed: list[str], **params: Any) -> Any:
    _record(executed, statement)
    return connection.execute(text(statement), params).scalar()


def _relation_exists(connection: Any, executed: list[str]) -> bool:
    return bool(_scalar(connection, "SELECT to_regclass(:table) IS NOT NULL",
                        executed, table=TABLE))


def _schema_facts(connection: Any, executed: list[str]) -> dict[str, Any]:
    columns = _rows(connection,
        "SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS data_type, "
        "a.attnotnull AS not_null, a.attgenerated AS generated, "
        "pg_get_expr(ad.adbin, ad.adrelid) AS generation_expression "
        "FROM pg_attribute a LEFT JOIN pg_attrdef ad "
        "  ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
        "WHERE a.attrelid = to_regclass(:table) AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum", executed, table=TABLE)
    functions = _rows(connection,
        "SELECT DISTINCT p.proname AS name, "
        "pg_get_function_identity_arguments(p.oid) AS arguments, l.lanname AS language "
        "FROM pg_depend d JOIN pg_attrdef ad ON ad.oid = d.objid "
        "JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum "
        "JOIN pg_proc p ON p.oid = d.refobjid "
        "JOIN pg_language l ON l.oid = p.prolang "
        "WHERE a.attrelid = to_regclass(:table) AND a.attgenerated <> '' "
        "  AND d.refclassid = 'pg_proc'::regclass ORDER BY 1, 2", executed, table=TABLE)
    return {
        "columns": columns,
        "generated_columns": [row for row in columns if row.get("generated")],
        "functions_used_by_generated_columns": functions,
        "constraints": _rows(connection,
            "SELECT conname AS name, contype AS type, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint WHERE conrelid = to_regclass(:table) ORDER BY conname",
            executed, table=TABLE),
        "indexes": _rows(connection,
            "SELECT indexname AS name, indexdef AS definition FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = :bare ORDER BY indexname",
            executed, bare=TABLE.split(".", 1)[1]),
        "triggers": _rows(connection,
            "SELECT tgname AS name, pg_get_triggerdef(oid) AS definition FROM pg_trigger "
            "WHERE tgrelid = to_regclass(:table) AND NOT tgisinternal ORDER BY tgname",
            executed, table=TABLE),
    }


def _plan_identities(plan_path: Path) -> dict[str, Any]:
    plan = load_verified(plan_path)
    selected: Sequence[Mapping[str, Any]] = ()
    for key, value in plan.items():
        if isinstance(value, list) and value and isinstance(value[0], Mapping) \
                and "processing_identity" in value[0]:
            selected = value
            break
    if not selected:
        raise AuditRefused("the authoritative plan exposes no selected identities")
    planned = [row for row in selected if row.get("outcome") == "planned"]
    eligible = {tuple(str(part) for part in row["processing_identity"]) for row in planned}
    outcomes = Counter(str(row.get("outcome")) for row in selected)
    return {"plan_digest": plan.get("digest"), "selected": len(selected),
            "planned": len(planned), "eligible_identities": eligible,
            "outcomes": dict(sorted(outcomes.items()))}


def _stored_identities(connection: Any, executed: list[str]) -> list[tuple[str, ...]]:
    available = {row["name"] for row in _rows(connection,
        "SELECT a.attname AS name FROM pg_attribute a "
        "WHERE a.attrelid = to_regclass(:table) AND a.attnum > 0 AND NOT a.attisdropped",
        executed, table=TABLE)}
    missing = [field for field in IDENTITY_FIELDS if field not in available]
    if missing:
        raise AuditRefused(
            f"the receipt table exposes no typed identity columns; missing {missing}. "
            f"Identity coverage cannot be asserted from this shape.")
    projection = ", ".join(IDENTITY_FIELDS)
    rows = _rows(connection,
        f"SELECT {projection} FROM {TABLE}", executed)
    return [tuple(str(row[field]) for field in IDENTITY_FIELDS) for row in rows]


def audit(plan_path: Path) -> dict[str, Any]:
    executed: list[str] = []
    engine = get_engine()
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        executed.append("set transaction read only")
        if not _relation_exists(connection, executed):
            raise AuditRefused(f"{TABLE} does not exist in the audited database")
        facts = _schema_facts(connection, executed)
        total = _scalar(connection, f"SELECT count(*) FROM {TABLE}", executed)
        available = {row["name"] for row in facts["columns"]}
        distributions = {
            field: _rows(connection,
                f"SELECT {field}::text AS value, count(*) AS rows FROM {TABLE} "
                f"GROUP BY 1 ORDER BY 2 DESC, 1", executed)
            for field in DISTRIBUTION_FIELDS if field in available}
        recorded_range = None
        if "recorded_at" in available:
            recorded_range = _rows(connection,
                f"SELECT min(recorded_at) AS earliest, max(recorded_at) AS latest FROM {TABLE}",
                executed)[0]
        stored = _stored_identities(connection, executed)
        connection.rollback()

    plan = _plan_identities(plan_path)
    eligible = plan["eligible_identities"]
    stored_unique = set(stored)
    covered = eligible & stored_unique
    duplicates = len(stored) - len(stored_unique)
    orphaned = stored_unique - eligible
    report = {
        "kind": KIND, "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "table": TABLE,
        "schema": facts,
        "rows": {"total": int(total or 0), "distinct_identities": len(stored_unique),
                 "duplicate_identities": duplicates},
        "distributions": distributions,
        "recorded_at_range": recorded_range,
        "coverage": {
            "basis": "authoritative dry plan selected identities",
            "plan_path": str(plan_path.resolve()),
            "plan_digest": plan["plan_digest"],
            "selected_rows": plan["selected"],
            "planned_rows": plan["planned"],
            "outcomes": plan["outcomes"],
            "eligible_identities": len(eligible),
            "covered_identities": len(covered),
            "missing_identities": len(eligible - stored_unique),
            "orphaned_identities": len(orphaned),
            "stale_or_superseded": len(orphaned),
        },
        "mutating_statements": [statement for statement in executed
                                if any(token in statement for token in MUTATING)],
        "select_only": True,
        "statements_executed": len(executed),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = audit(args.plan)
    except Exception as exc:  # a refusal is a result, not a crash
        print(json.dumps({"outcome": "refused", "error": f"{type(exc).__name__}: {exc}"},
                         sort_keys=True))
        return 1
    if report["mutating_statements"]:
        print(json.dumps({"outcome": "refused", "error": "mutating statements were issued"},
                         sort_keys=True))
        return 1
    write_immutable(args.out, report)
    print(json.dumps({"outcome": "audited", "out": str(args.out), "table": report["table"],
                      "total_rows": report["rows"]["total"],
                      "covered": report["coverage"]["covered_identities"],
                      "eligible": report["coverage"]["eligible_identities"],
                      "missing": report["coverage"]["missing_identities"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
