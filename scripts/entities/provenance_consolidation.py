#!/usr/bin/env python3
"""Consolidate stale graph provenance into proven current assertions.

This facade owns plan construction and the CLI.  Automatic plans are always
saved first and applied only through ``--apply-plan`` with an explicitly
approved SHA-256 digest and a fresh row-level backup output; the old
generate-and-immediately-apply ``--apply`` shortcut is rejected.  All
fail-closed validation and the atomic transaction live in
``provenance_consolidation_apply``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

_ENTITIES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ENTITIES_DIR.parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from db.core import get_engine
from scripts.entities.provenance_consolidation_adjudication import (
    PHOENIX_TZ,
    build_human_adjudication_plan,
)
from scripts.entities.provenance_consolidation_apply import (
    AUTOMATIC_PLAN_KIND,
    apply_consolidation,
)
from scripts.entities.provenance_consolidation_operations import (
    _operation_target_ids,
    _plan_operations,
    _rows_fingerprint,
    _write_json,
)
from scripts.entities.provenance_consolidation_plans import (
    build_consolidation_plan,
)

_PLAN_KINDS = {"human_adjudication", AUTOMATIC_PLAN_KIND}


def _read_json(path: Path) -> dict[str, Any]:
    """Load one JSON object from disk with a clear contract error."""
    with path.open(encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return document


def main() -> int:
    """Build a read-only plan or apply a previously saved plan."""
    parser = argparse.ArgumentParser(
        description="Consolidate redundant stale KG provenance"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="rejected legacy shortcut; apply through --apply-plan instead",
    )
    mode.add_argument(
        "--adjudication",
        type=Path,
        help="build a read-only plan from an explicit human-adjudication JSON file",
    )
    mode.add_argument(
        "--apply-plan",
        type=Path,
        help="atomically apply a previously persisted consolidation plan",
    )
    parser.add_argument(
        "--expect-plan-sha256",
        help="required approved digest when applying any saved plan",
    )
    parser.add_argument(
        "--backup-output",
        type=Path,
        help="required new row-level backup path for an automatic apply",
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    if arguments.apply:
        raise ValueError(
            "automatic --apply is disabled; save the plan, review its digest, "
            "then use --apply-plan with --expect-plan-sha256 and --backup-output"
        )

    timestamp = datetime.now(PHOENIX_TZ).strftime("%Y%m%d-%H%M%S")
    engine = get_engine()

    if arguments.apply_plan:
        plan_document = _read_json(arguments.apply_plan)
        plan_kind = plan_document.get("plan_kind")
        if plan_kind not in _PLAN_KINDS:
            raise ValueError("--apply-plan requires a recognized saved plan")
        if not arguments.expect_plan_sha256:
            raise ValueError("--apply-plan requires --expect-plan-sha256")
        if arguments.expect_plan_sha256 != plan_document.get("plan_sha256"):
            raise ValueError("approved plan SHA-256 does not match the plan file")
        plan = plan_document
        output_path = arguments.output or (
            _REPO_ROOT / "data" / f"kg-provenance-consolidation-applied-{timestamp}.json"
        )
        document = {**plan, "mode": "apply"}
        if plan_kind == AUTOMATIC_PLAN_KIND and arguments.backup_output is None:
            raise ValueError("automatic --apply-plan requires --backup-output")
        document["result"] = apply_consolidation(
            engine,
            plan,
            expected_plan_sha256=arguments.expect_plan_sha256,
            automatic_backup_path=arguments.backup_output,
            saved_plan_path=arguments.apply_plan,
        )
        document["completed_at"] = datetime.now(PHOENIX_TZ).isoformat()
        _write_json(document, output_path)
    else:
        if arguments.adjudication:
            adjudication = _read_json(arguments.adjudication)
            plan = build_human_adjudication_plan(engine, adjudication)
            default_name = f"kg-human-adjudication-plan-{timestamp}.json"
        else:
            plan = build_consolidation_plan(engine)
            default_name = f"kg-provenance-consolidation-{timestamp}.json"
        output_path = arguments.output or (_REPO_ROOT / "data" / default_name)
        document = {"mode": "dry_run", **plan}
        _write_json(document, output_path)

    print(json.dumps({
        "mode": document["mode"],
        "summary": document["summary"],
        "result": document.get("result"),
        "output": str(output_path),
    }, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
