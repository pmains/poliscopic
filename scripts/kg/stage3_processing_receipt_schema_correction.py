#!/usr/bin/env python3
"""Plan or apply the proved two-function receipt schema correction on development."""

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
for candidate in (str(REPO), str(REPO / "scripts")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from db.core import get_engine  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_backup_verify as backup_verify  # noqa: E402
from scripts.kg import stage3_processing_receipt_backup_run as backup  # noqa: E402

KIND = "kg-stage3-processing-receipt-schema-correction"
VERSION = "1.0"
FUNCTIONS = (
    "processing_receipts_canonical_json(jsonb)",
    "processing_receipts_canonical_receipt_digest(jsonb)",
)
LOCK_KEY = 0x4B475333


def _digest(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _target(engine: Any) -> dict[str, Any]:
    target = backup_verify.capture_target(engine)
    if target.get("tier") != "development" or target.get("database") != "poliscopic_dev":
        raise RuntimeError(f"refusing non-canonical development target {target!r}")
    return target


def _definitions(connection: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for signature in FUNCTIONS:
        definition = connection.execute(text(
            "SELECT pg_get_functiondef(to_regprocedure(:signature))"),
            {"signature": f"public.{signature}"}).scalar()
        if not definition:
            raise RuntimeError(f"required function absent: public.{signature}")
        result[signature] = str(definition)
    return result


def _receipt_snapshot(connection: Any) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT count(*) AS rows, count(DISTINCT receipt_id) AS receipt_ids, "
        "count(DISTINCT (source_kind, source_id, content_sha256, extraction_method, "
        "extractor, extractor_version)) AS identities, "
        "md5(coalesce(string_agg(receipt_id::text || ':' || receipt_digest::text, "
        "',' ORDER BY receipt_id), '')) AS fingerprint FROM public.processing_receipts"
    )).mappings().one()
    return {key: (int(value) if key != "fingerprint" else str(value))
            for key, value in row.items()}


def build_plan(engine: Any) -> dict[str, Any]:
    target = _target(engine)
    with engine.connect() as connection:
        preimage = _definitions(connection)
        snapshot = _receipt_snapshot(connection)
    statements = list(backup._schema_correction_statements())
    plan = {
        "kind": KIND,
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "scope": {"functions_replaced": list(FUNCTIONS), "table_rows_updated": 0,
                  "production_allowed": False},
        "preimage": preimage,
        "receipt_snapshot": snapshot,
        "statements": statements,
        "correction_sha256": hashlib.sha256("\n".join(statements).encode()).hexdigest(),
        "rollback": {"statements": list(preimage.values())},
        "proof": {"path": "data/kg-plans/kg-stage3-processing-receipt-schema-correction-proof-20260921T140000Z.json",
                  "digest": "f515ee3b22ec48ee3ec993689f6117fd447f9aacc232bd6e7f4e5f385521f7f9"},
    }
    plan["digest"] = _digest(plan)
    return plan


def apply_plan(engine: Any, plan: Mapping[str, Any], *, expected_digest: str) -> dict[str, Any]:
    if plan.get("kind") != KIND or plan.get("version") != VERSION:
        raise RuntimeError("wrong correction-plan contract")
    actual = _digest(plan)
    if plan.get("digest") != actual or expected_digest != actual:
        raise RuntimeError("correction-plan digest mismatch")
    if plan.get("target") != _target(engine):
        raise RuntimeError("correction-plan target drift")
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
            if _definitions(connection) != plan.get("preimage"):
                raise RuntimeError("function-definition preimage drift")
            before = _receipt_snapshot(connection)
            if before != plan.get("receipt_snapshot"):
                raise RuntimeError("processing-receipt rows drifted since planning")
            backup._apply_schema_correction(connection)
            connection.execute(text("SET LOCAL search_path = ''"))
            mismatch = connection.execute(text(
                "SELECT count(*) FROM public.processing_receipts "
                "WHERE receipt_digest <> public.processing_receipts_canonical_receipt_digest(receipt_body)"
            )).scalar_one()
            if int(mismatch) != 0:
                raise RuntimeError(f"corrected digest function disagrees with {mismatch} receipts")
            after = _receipt_snapshot(connection)
            if after != before:
                raise RuntimeError("receipt rows changed during function correction")
            postimage = _definitions(connection)
            transaction.commit()
        except BaseException:
            transaction.rollback()
            raise
    receipt = {
        "kind": f"{KIND}-receipt",
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan_digest": actual,
        "target": plan["target"],
        "functions_replaced": list(FUNCTIONS),
        "receipt_snapshot_before": before,
        "receipt_snapshot_after": after,
        "digest_mismatches": 0,
        "table_rows_updated": 0,
        "transaction": "committed",
        "postimage_sha256": hashlib.sha256(json.dumps(postimage, sort_keys=True).encode()).hexdigest(),
    }
    receipt["digest"] = _digest(receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-out", type=Path)
    mode.add_argument("--apply-plan", type=Path)
    parser.add_argument("--digest")
    parser.add_argument("--receipt-out", type=Path)
    args = parser.parse_args(argv)
    engine = get_engine()
    try:
        if args.plan_out:
            plan = build_plan(engine)
            artifacts.write_immutable(args.plan_out, plan)
            print(json.dumps({"outcome": "planned", "path": str(args.plan_out),
                              "digest": plan["digest"]}, sort_keys=True))
            return 0
        if not args.digest or not args.receipt_out:
            parser.error("--apply-plan requires --digest and --receipt-out")
        plan = artifacts.load_verified(args.apply_plan)
        receipt = apply_plan(engine, plan, expected_digest=args.digest)
        artifacts.write_immutable(args.receipt_out, receipt)
        print(json.dumps({"outcome": "applied", "path": str(args.receipt_out),
                          "digest": receipt["digest"]}, sort_keys=True))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
