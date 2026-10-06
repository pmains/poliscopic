#!/usr/bin/env python3
"""Refuse daily success unless development meeting rows reached production."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from sqlalchemy import Engine, create_engine, text

REPO = Path(__file__).resolve().parents[2]
FIELDS = (
    "meeting_date", "meeting_type", "meeting_title", "source_url", "sync_status",
    "item_count_expected", "item_count_actual", "supporting_doc_count",
    "items_extracted", "supporting_docs_extracted", "minutes_url", "updated_at",
)
PHOENIX = ZoneInfo("America/Phoenix")


def rows(engine: Engine, *, updated_since: datetime | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    columns = ", ".join(("body", "meeting_id", *FIELDS))
    statement = f"SELECT {columns} FROM meetings"
    parameters: dict[str, Any] = {}
    if updated_since is not None:
        statement += " WHERE updated_at >= :updated_since"
        parameters["updated_since"] = updated_since
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        found = connection.execute(text(statement), parameters).mappings()
        return {(str(row["body"]), str(row["meeting_id"])): dict(row) for row in found}


def compare(dev: dict[tuple[str, str], dict[str, Any]],
            prod: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    missing = sorted(set(dev) - set(prod))
    mismatches: list[dict[str, Any]] = []
    for key in sorted(set(dev) & set(prod)):
        changed = [field for field in FIELDS if dev[key].get(field) != prod[key].get(field)]
        if changed:
            mismatches.append({"body": key[0], "meeting_id": key[1], "fields": changed})
    return {
        "status": "succeeded" if not missing and not mismatches else "failed",
        "development_meetings": len(dev), "production_meetings": len(prod),
        "missing_count": len(missing), "mismatch_count": len(mismatches),
        "missing_examples": [{"body": body, "meeting_id": meeting_id}
                             for body, meeting_id in missing[:20]],
        "mismatch_examples": mismatches[:20],
        "policy": "every development meeting key and synchronized field must match production; production-only rows are allowed by upsert-only policy",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        run_date = date.fromisoformat(arguments.run_date)
    except ValueError:
        print("REFUSED: --run-date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    if arguments.lookback_days < 0:
        print("REFUSED: --lookback-days must be non-negative", file=sys.stderr)
        return 2
    updated_since = datetime.combine(
        run_date - timedelta(days=arguments.lookback_days), time.min, PHOENIX)
    env = dotenv_values(REPO / ".env")
    dev_url, prod_url = env.get("DATABASE_URL"), env.get("PROD_DATABASE_URL")
    if not dev_url or not prod_url:
        print("REFUSED: DATABASE_URL and PROD_DATABASE_URL are required", file=sys.stderr)
        return 2
    try:
        result = compare(
            rows(create_engine(str(dev_url), future=True, pool_pre_ping=True),
                 updated_since=updated_since),
            rows(create_engine(str(prod_url), future=True, pool_pre_ping=True)))
    except Exception as exc:
        print(f"REFUSED: meeting parity query failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    result["window"] = {"run_date": arguments.run_date,
                        "lookback_days": arguments.lookback_days,
                        "updated_since": updated_since.isoformat()}
    rendered = json.dumps(result, indent=2, default=str, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered)
    print(rendered, end="")
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
