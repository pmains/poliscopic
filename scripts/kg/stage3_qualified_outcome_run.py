#!/usr/bin/env python3
"""Dev-only CLI for immutable qualified-outcome plans and explicit applies."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for candidate in (str(REPO), str(REPO / "scripts")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from sqlalchemy import inspect, text  # noqa: E402
from scripts.db import get_engine  # noqa: E402
from scripts.kg.stage2_artifacts import compute_digest, load_verified, write_immutable  # noqa: E402
from scripts.kg.stage2_backup_verify import capture_target  # noqa: E402
from scripts.kg.stage3_qualified_outcome_apply import (  # noqa: E402
    AUTHORIZATION, apply, code_digest, live_schema_digest, validate_backup,
)
from scripts.kg.stage3_qualified_outcome_apply_packet import build_apply_packet  # noqa: E402
from scripts.kg.stage3_qualified_outcome_migration import (  # noqa: E402
    LEGACY_PROJECTION_SQL, REPLAY_PROJECTION_SQL, group_projection_rows, write_plan,
)
from scripts.kg.stage3_qualified_outcome_schema_packet import build_packet  # noqa: E402


def _engine():
    engine = get_engine()
    target = capture_target(engine)
    if target.get("tier") != "development" or target.get("database") != "poliscopic_dev":
        raise RuntimeError(f"refusing non-development target: {target}")
    return engine


def _rows(engine):
    columns = {column["name"] for column in inspect(engine).get_columns("meeting_events")}
    query = REPLAY_PROJECTION_SQL if {"outcome_base", "outcome_qualifier"} <= columns \
        else LEGACY_PROJECTION_SQL
    with engine.connect() as connection:
        return group_projection_rows([dict(row) for row in connection.execute(text(query)).mappings()])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", type=Path)
    mode.add_argument("--authorize", type=Path, metavar="AUTHORIZED_APPLY_PACKET_OUT")
    mode.add_argument("--apply", type=Path, metavar="AUTHORIZED_APPLY_PACKET")
    parser.add_argument("--design", type=Path, required=True)
    parser.add_argument("--plan-artifact", type=Path)
    parser.add_argument("--backup-receipt", type=Path)
    parser.add_argument("--receipt-out", type=Path)
    parser.add_argument("--authorization-token")
    args = parser.parse_args(argv)
    engine = _engine()
    schema = live_schema_digest(engine)
    code = code_digest()
    if args.plan:
        rows = _rows(engine)
        plan, _ = write_plan(args.plan, rows, target="poliscopic_dev",
                             schema_digest=schema, code_digest=code)
        write_immutable(args.design, build_packet(plan))
        return 0
    if args.authorize:
        if not all((args.plan_artifact, args.backup_receipt)):
            parser.error("--authorize requires --plan-artifact and --backup-receipt")
        token = args.authorization_token or os.environ.get("POLISCOPIC_QUALIFIED_OUTCOME_TOKEN")
        if token != AUTHORIZATION:
            raise RuntimeError("explicit qualified-outcome authorization token required")
        plan = load_verified(args.plan_artifact)
        design = load_verified(args.design)
        backup = load_verified(args.backup_receipt)
        if schema != plan.get("schema_digest") or code != plan.get("code_digest"):
            raise RuntimeError("live schema or code no longer matches the reviewed plan")
        provisional = build_apply_packet(
            design_packet=design, plan=plan,
            backup_receipt_digest=compute_digest(backup),
            code_digest=code, schema_digest=schema)
        if validate_backup(backup, plan, provisional):
            raise RuntimeError("backup receipt does not satisfy reviewed Stage 2 contract")
        write_immutable(args.authorize, provisional)
        return 0
    if not all((args.plan_artifact, args.backup_receipt, args.receipt_out)):
        parser.error("--apply requires --plan-artifact, --backup-receipt, and --receipt-out")
    token = args.authorization_token or os.environ.get("POLISCOPIC_QUALIFIED_OUTCOME_TOKEN")
    if token != AUTHORIZATION:
        raise RuntimeError("explicit qualified-outcome apply token required")
    plan = load_verified(args.plan_artifact)
    design = load_verified(args.design)
    authorized = load_verified(args.apply)
    backup = load_verified(args.backup_receipt)
    rows = _rows(engine)
    result = apply(engine, design_packet=design, apply_packet=authorized, plan=plan,
                   source_rows=rows, backup_receipt=backup, authorization=token,
                   target="poliscopic_dev", schema_digest=schema,
                   current_code_digest=code)
    receipt = {"kind": "kg-stage3-qualified-outcome-apply-receipt", "version": "1.0",
               "created_at": datetime.now(timezone.utc).isoformat(),
               "target": "poliscopic_dev", "plan_digest": plan["digest"],
               "apply_packet_digest": authorized["digest"], "result": result}
    write_immutable(args.receipt_out, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
