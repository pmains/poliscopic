#!/usr/bin/env python3
"""sweep_docs_preflight.py — canonical read-only preflight for a dry gate.

Records the baseline a bounded ``sweep_docs`` dry run is judged against, and
optionally fails closed when the eligible unswept population no longer matches
an expected bound.

Every statement is a ``SELECT``.  This module never writes to the database.

It is **not** part of the sweep_docs phase's executable behaviour, so it is
deliberately excluded from the phase's ``code_modules`` fingerprint manifest.

Usage:
  .venv/bin/python -u scripts/entities/sweep_docs_preflight.py
  .venv/bin/python -u scripts/entities/sweep_docs_preflight.py --expect-eligible 1122
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ENTITIES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ENTITIES_DIR.parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import text

from db.core import get_engine
from scripts.entities.bounded_verification import (
    GATE_COUNT_TABLES,
    _graph_counts,
    _integrity_snapshot,
)

#: A document is eligible for the sweep when it has text and was never swept.
ELIGIBLE_CLAUSE = (
    "swept_at IS NULL AND text_content IS NOT NULL AND text_content != ''"
)


def eligible_unswept_count(engine) -> int:
    """Return how many documents a full sweep_docs run would process."""
    with engine.connect() as connection:
        return int(connection.execute(text(
            f"SELECT COUNT(*) FROM supporting_documents WHERE {ELIGIBLE_CLAUSE}"
        )).scalar())


def extraction_method_distribution(engine) -> dict[str, int]:
    """Return stored extraction method -> eligible document count."""
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT COALESCE(text_extraction_method, '<null>'), COUNT(*) "
            f"FROM supporting_documents WHERE {ELIGIBLE_CLAUSE} "
            "GROUP BY 1 ORDER BY 2 DESC"
        )).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def record_baseline(engine) -> dict:
    """Record the complete read-only baseline for a sweep_docs dry gate."""
    with engine.connect() as connection:
        with_text = int(connection.execute(text(
            "SELECT COUNT(*) FROM supporting_documents "
            "WHERE text_content IS NOT NULL AND text_content != ''"
        )).scalar())
    return {
        "gate_tables": list(GATE_COUNT_TABLES),
        "graph_counts": _graph_counts(engine),
        "integrity": _integrity_snapshot(engine),
        "eligible_unswept": eligible_unswept_count(engine),
        "with_text_total": with_text,
        "extraction_method_of_eligible": extraction_method_distribution(engine),
    }


def check_eligible(engine, expected: int) -> tuple[bool, int]:
    """Return ``(matches_expected, observed_count)``."""
    observed = eligible_unswept_count(engine)
    return observed == int(expected), observed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only preflight baseline for a sweep_docs dry gate")
    parser.add_argument(
        "--expect-eligible", type=int, default=None,
        help="Fail closed unless the eligible unswept count equals this value",
    )
    arguments = parser.parse_args()

    engine = get_engine()
    baseline = record_baseline(engine)

    if arguments.expect_eligible is not None:
        matches, observed = check_eligible(engine, arguments.expect_eligible)
        baseline["expected_eligible"] = int(arguments.expect_eligible)
        baseline["eligible_matches_expected"] = matches
        if not matches:
            print(json.dumps(baseline, indent=2, default=str))
            print(
                f"PREFLIGHT FAILED: eligible unswept is {observed}, not the "
                f"expected {arguments.expect_eligible}; do not reuse the "
                "recorded bound",
                file=sys.stderr,
            )
            return 1

    print(json.dumps(baseline, indent=2, default=str))
    print("PREFLIGHT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
