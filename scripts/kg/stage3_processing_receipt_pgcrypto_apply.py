#!/usr/bin/env python3
"""Narrow, separately authorized development-only pgcrypto admission.

This tool owns exactly one schema operation: ``CREATE EXTENSION pgcrypto`` on
the exact current development target.  It is deliberately separate from the
receipt-store apply so that an extension authorization cannot be mistaken for
authorization to create the receipt table or process documents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from db.core import get_engine  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply as receipt_apply  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply_packet as receipt_packet  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

KIND = "kg-stage3-processing-receipt-pgcrypto-extension-plan"
VERSION = "1.0"
AUTHORIZATION = "stage3-processing-receipt-pgcrypto-development-schema/v1"
LOCK_ID = 7303202609202
CODE_FILES = ("scripts/kg/stage3_processing_receipt_pgcrypto_apply.py",)


class Refused(RuntimeError):
    pass


def code_digest(repo: Path = REPO) -> str:
    return hashlib.sha256((repo / CODE_FILES[0]).read_bytes()).hexdigest()


def digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def build(*, processing_plan: Mapping[str, Any], design_packet: Mapping[str, Any],
          approver: str, writer_role: str) -> dict[str, Any]:
    if processing_plan.get("digest") != receipt_packet.CURRENT_PLAN_DIGEST:
        raise Refused("pgcrypto extension must bind the current processing dry plan")
    if design_packet.get("digest") != receipt_packet.CURRENT_DESIGN_PACKET_DIGEST:
        raise Refused("pgcrypto extension must bind the current disabled design packet")
    if not str(approver).strip() or not str(writer_role).strip():
        raise Refused("approver and exact writer role are required")
    body = {"kind": KIND, "version": VERSION, "state": "authorized",
            "target": dict(processing_plan["target"]),
            "processing_plan_digest": processing_plan["digest"],
            "design_packet_digest": design_packet["digest"],
            "operation": "CREATE EXTENSION pgcrypto",
            "forbidden_operations": ["CREATE TABLE", "ALTER TABLE", "INSERT", "UPDATE",
                                     "DELETE", "DROP", "swept_at"],
            "required_capability": "public.digest(bytea, text)",
            "approver": approver, "writer_role": writer_role,
            "code_digest": code_digest()}
    return {**body, "digest": digest(body)}


def validate(plan: Mapping[str, Any], *, processing_plan: Mapping[str, Any],
             design_packet: Mapping[str, Any]) -> list[str]:
    body = {key: value for key, value in plan.items() if key != "digest"}
    expected = build(processing_plan=processing_plan, design_packet=design_packet,
                     approver=str(plan.get("approver") or ""),
                     writer_role=str(plan.get("writer_role") or ""))
    problems: list[str] = []
    if plan.get("digest") != digest(body):
        problems.append("pgcrypto plan digest mismatch")
    if plan != expected:
        problems.append("pgcrypto plan differs from canonical authorized construction")
    return problems


def _capability(connection: Any) -> dict[str, Any]:
    row = connection.execute(text("""
        SELECT current_database() AS database, current_user AS writer,
               n.nspname AS extension_schema,
               to_regprocedure('public.digest(bytea,text)')::text AS digest_function
        FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
        WHERE e.extname='pgcrypto'
    """)).mappings().one_or_none()
    return dict(row or {})


def apply(engine: Any, *, extension_plan: Mapping[str, Any],
          processing_plan: Mapping[str, Any], design_packet: Mapping[str, Any],
          token: str, receipt_out: Path) -> dict[str, Any]:
    problems = validate(extension_plan, processing_plan=processing_plan, design_packet=design_packet)
    if token != AUTHORIZATION:
        problems.append("explicit pgcrypto schema authorization token mismatch")
    if receipt_apply._target(engine) != receipt_apply._canonical_target(processing_plan.get("target") or {}):
        problems.append("live engine is not the exact development target")
    if receipt_out.exists():
        problems.append("extension terminal receipt path already exists")
    if problems:
        raise Refused("; ".join(problems))
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": LOCK_ID})
            live_database, live_writer = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
            if (str(live_database) != extension_plan["target"]["database"]
                    or str(live_writer) != extension_plan["writer_role"]):
                raise Refused("authenticated writer or live database differs from extension plan")
            before = _capability(connection)
            if before:
                if before.get("extension_schema") != "public" or before.get("digest_function") != "digest(bytea,text)":
                    raise Refused("existing pgcrypto extension does not provide the required public digest function")
                created = False
            else:
                connection.execute(text("CREATE EXTENSION pgcrypto"))
                created = True
            after = _capability(connection)
            if (after.get("extension_schema") != "public"
                    or after.get("digest_function") != "digest(bytea,text)"):
                raise Refused("pgcrypto install did not provide public.digest(bytea,text)")
            unexpected_table = connection.execute(
                text("SELECT to_regclass('public.processing_receipts')")
            ).scalar_one()
            if unexpected_table is not None:
                raise Refused("receipt store appeared during pgcrypto-only schema operation")
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
    body = {"kind": "kg-stage3-processing-receipt-pgcrypto-extension-receipt", "version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(), "extension_plan_digest": extension_plan["digest"],
            "target": dict(extension_plan["target"]), "operation": extension_plan["operation"],
            "created": created, "capability": after, "processing_receipts_table": None,
            "unrelated_changes": 0}
    document = {**body, "digest": digest(body)}
    write_immutable(receipt_out, document)
    return {**document, "receipt_path": str(receipt_out)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-out", type=Path)
    mode.add_argument("--apply", type=Path)
    parser.add_argument("--processing-plan", type=Path, required=True)
    parser.add_argument("--design", type=Path, required=True)
    parser.add_argument("--approver")
    parser.add_argument("--writer-role")
    parser.add_argument("--authorization-token")
    parser.add_argument("--receipt-out", type=Path)
    args = parser.parse_args(argv)
    processing_plan, design_packet = load_verified(args.processing_plan), load_verified(args.design)
    if args.plan_out:
        document = build(processing_plan=processing_plan, design_packet=design_packet,
                         approver=args.approver or "", writer_role=args.writer_role or "")
        print(json.dumps({"outcome": "authorized", "digest": write_immutable(args.plan_out, document),
                          "plan": str(args.plan_out)}, sort_keys=True))
        return 0
    if not args.receipt_out:
        parser.error("--apply requires --receipt-out")
    document = load_verified(args.apply)
    result = apply(get_engine(), extension_plan=document, processing_plan=processing_plan,
                   design_packet=design_packet, token=args.authorization_token or "", receipt_out=args.receipt_out)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
