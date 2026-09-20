#!/usr/bin/env python3
"""One-time, read-only admission binding for short Stage 3 receipt batches.

Validating the immutable 65k-row dry plan before every 500-row transaction made
the safe runner needlessly slow.  This module performs that *complete* check once
and writes an immutable preflight.  A later batch accepts it only while its exact
plan, design, authorization, backup, code, target, writer, receipt-store schema,
and full planned source-identity population still match.  The batch transaction
then locks and rechecks its own exact source window; the preflight is never a
substitute for row-level drift detection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import text

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from db.core import get_engine  # noqa: E402
from scripts.kg import stage3_processing_receipt as receipt  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply_packet as authorization  # noqa: E402
from scripts.kg import stage3_processing_receipt_store_backup as backup  # noqa: E402
from scripts.kg import stage3_processing_receipt_store_packet as design  # noqa: E402
from scripts.kg import stage3_processing_receipt_store_schema as schema  # noqa: E402
from scripts.kg import stage3_processing_plan_validator as validator  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

KIND = "kg-stage3-processing-receipt-preflight"
VERSION = "1.0"
# The interval is deliberately short: this is a reusable optimization, not a
# permanent waiver of the whole-population read.  Each individual batch still
# locks/rechecks its own source rows.
MAX_AGE_SECONDS = 1800
SOURCE_CHUNK_SIZE = 1000


class PreflightRefused(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return receipt.canonical_sha256(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _target(connection: Any, expected: Mapping[str, Any]) -> dict[str, Any]:
    database, writer = connection.execute(text("SELECT current_database(), current_user")).one()
    return {"tier": expected.get("tier"), "database": str(database),
            "dialect": connection.engine.dialect.name, "host": expected.get("host"),
            "port": expected.get("port"), "writer_role": str(writer)}


def _schema_binding(connection: Any) -> dict[str, Any]:
    """Capture all locally-owned receipt DDL observables without reading receipts."""
    present = connection.execute(text("SELECT to_regclass('processing_receipts')")).scalar_one()
    if present is None:
        return {"state": "absent"}
    columns = [list(row) for row in connection.execute(text("""
        SELECT column_name, data_type, is_nullable, column_default, is_generated,
               generation_expression
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'processing_receipts'
        ORDER BY ordinal_position
    """))]
    constraints = [list(row) for row in connection.execute(text("""
        SELECT conname, pg_get_constraintdef(oid, true)
        FROM pg_constraint WHERE conrelid = to_regclass('processing_receipts')
        ORDER BY conname
    """))]
    indexes = [list(row) for row in connection.execute(text("""
        SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = current_schema() AND tablename = 'processing_receipts'
        ORDER BY indexname
    """))]
    triggers = [list(row) for row in connection.execute(text("""
        SELECT tgname, pg_get_triggerdef(oid, true)
        FROM pg_trigger WHERE tgrelid = to_regclass('processing_receipts')
          AND NOT tgisinternal ORDER BY tgname
    """))]
    functions = [list(row) for row in connection.execute(text("""
        SELECT p.proname, pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = current_schema() AND p.proname = ANY(:names)
        ORDER BY p.proname
    """), {"names": [schema.TRIGGER_FUNCTION, schema.CANONICAL_JSON_FUNCTION,
                         schema.JSON_STRING_FUNCTION, schema.CANONICAL_DIGEST_FUNCTION,
                         schema.RECORDED_AT_KEY_FUNCTION]})]
    columns_present = {str(row[0]) for row in columns}
    triggers_present = {str(row[0]) for row in triggers}
    complete = (columns_present == {name for name, _decl, _source in schema.COLUMNS}
                and {schema.TRIGGER_ROW, schema.TRIGGER_TRUNCATE} <= triggers_present)
    body = {"columns": columns, "constraints": constraints, "indexes": indexes,
            "triggers": triggers, "functions": functions}
    return {"state": "complete" if complete else "incomplete",
            "schema_sha256": _digest(body), "objects": body}


def _plan_sources(plan: Mapping[str, Any]) -> list[tuple[int, list[Any]]]:
    values: list[tuple[int, list[Any]]] = []
    seen: set[int] = set()
    for record in list(plan.get("records") or []):
        source_id = record.get("source_id")
        identity = record.get("processing_identity")
        if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
            raise PreflightRefused("plan carries a non-positive source id")
        if source_id in seen:
            raise PreflightRefused(f"plan repeats source id {source_id}")
        if not isinstance(identity, Sequence) or isinstance(identity, (str, bytes)) or len(identity) != 6:
            raise PreflightRefused(f"plan source {source_id} has a malformed identity")
        if int(identity[1]) != source_id:
            raise PreflightRefused(f"plan source {source_id} identity disagrees with source id")
        seen.add(source_id)
        values.append((source_id, list(identity)))
    if not values:
        raise PreflightRefused("preflight refuses an empty source population")
    return values


def _source_population(connection: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Read every plan source once and require its exact current identity.

    PostgreSQL computes the text SHA-256, avoiding transfer of the entire retained
    corpus merely to recreate the already-defined identity authority locally.
    """
    expected = _plan_sources(plan)
    ids = [source_id for source_id, _identity in expected]
    rows: dict[int, list[Any]] = {}
    for start in range(0, len(ids), SOURCE_CHUNK_SIZE):
        chunk = ids[start:start + SOURCE_CHUNK_SIZE]
        result = connection.execute(text("""
            SELECT id,
                   encode(digest(convert_to(coalesce(text_content, ''), 'UTF8'), 'sha256'), 'hex')
                       AS content_sha256,
                   coalesce(text_extraction_method, '') AS extraction_method
            FROM supporting_documents WHERE id = ANY(:ids) ORDER BY id
        """), {"ids": chunk})
        for row in result:
            source_id, content_sha256, extraction_method = row
            if int(source_id) in rows:
                raise PreflightRefused(f"database returned duplicate source id {source_id}")
            rows[int(source_id)] = ["supporting_document", int(source_id), str(content_sha256),
                                    str(extraction_method), receipt.EXTRACTOR,
                                    receipt.EXTRACTOR_VERSION]
    projection: list[list[Any]] = []
    for source_id, identity in expected:
        current = rows.get(source_id)
        if current is None:
            raise PreflightRefused(f"planned source {source_id} is absent")
        if current != identity:
            raise PreflightRefused(f"planned source {source_id} identity drifted")
        projection.append([source_id, identity])
    return {"records": len(expected), "source_ids_sha256": _digest(ids),
            "identities_sha256": _digest(projection), "read_method": "server_sha256_chunked",
            "chunk_size": SOURCE_CHUNK_SIZE}


def _backup_binding(path: Path) -> dict[str, Any]:
    document = load_verified(path)
    return {"path": str(path.resolve()), "digest": document.get("digest"),
            "size": path.stat().st_size, "mode": stat.S_IMODE(path.stat().st_mode)}


def build(*, engine: Any, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
          apply_packet: Mapping[str, Any], backup_path: Path, current_code_digest: str,
          created_at: str | None = None) -> dict[str, Any]:
    """Perform every expensive admission check once, then bind its exact evidence."""
    problems = validator.validate_plan(plan, target=plan.get("target"), hashes=validator.code_hashes())
    problems += design.validate_packet(design_packet)
    problems += authorization.validate(apply_packet, plan=plan, design_packet=design_packet,
                                       current_code_digest=current_code_digest)
    problems += backup.backup_problems(backup_path, target=plan.get("target") or {})
    if problems:
        raise PreflightRefused("; ".join(problems))
    with engine.connect() as connection:
        target = _target(connection, plan.get("target") or {})
        expected_target = plan.get("target") or {}
        if {name: target.get(name) for name in backup.TARGET_FIELDS} != {
                name: expected_target.get(name) for name in backup.TARGET_FIELDS}:
            raise PreflightRefused("live target differs from immutable plan")
        if target["writer_role"] != apply_packet.get("writer_role"):
            raise PreflightRefused("live writer differs from authorized packet")
        store = _schema_binding(connection)
        if store.get("state") != "complete":
            raise PreflightRefused("receipt store is absent, partial, or drifted")
        population = _source_population(connection, plan)
    body = {"kind": KIND, "version": VERSION,
            "created_at": created_at or _utc_now().isoformat(), "mode": "read-only",
            "applied": False, "write_path": "absent by design",
            "plan_digest": plan.get("digest"), "design_packet_digest": design_packet.get("digest"),
            "apply_packet_digest": apply_packet.get("digest"), "code_digest": current_code_digest,
            "target": {name: target.get(name) for name in backup.TARGET_FIELDS},
            "writer_role": target["writer_role"], "backup": _backup_binding(backup_path),
            "receipt_store": store, "source_population": population,
            "validation": {"plan": "complete", "design": "complete", "authorization": "complete",
                           "backup": "complete", "source_population": "complete"}}
    return {**body, "digest": _digest(body)}


def validate(document: Any, *, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
             apply_packet: Mapping[str, Any], backup_path: Path,
             current_code_digest: str, now: datetime | None = None) -> list[str]:
    """Fast, offline verification for every bounded apply invocation.

    The live target/writer/store check is intentionally performed again inside the
    serializable transaction by :func:`apply_batch`.
    """
    if not isinstance(document, Mapping):
        return ["preflight must be an object"]
    body = {key: value for key, value in document.items() if key != "digest"}
    problems: list[str] = []
    if document.get("kind") != KIND or document.get("version") != VERSION:
        problems.append("preflight kind or version is wrong")
    if document.get("digest") != _digest(body):
        problems.append("preflight digest does not match")
    if document.get("mode") != "read-only" or document.get("applied") is not False \
            or document.get("write_path") != "absent by design":
        problems.append("preflight must remain a read-only artifact")
    created = _parse_instant(document.get("created_at"))
    age = ((_utc_now() if now is None else now) - created).total_seconds() if created else None
    if age is None or age < 0 or age > MAX_AGE_SECONDS:
        problems.append("preflight is missing, future-dated, or stale")
    if document.get("plan_digest") != plan.get("digest"):
        problems.append("preflight plan binding drift")
    if document.get("design_packet_digest") != design_packet.get("digest"):
        problems.append("preflight design binding drift")
    if document.get("apply_packet_digest") != apply_packet.get("digest"):
        problems.append("preflight authorization binding drift")
    if document.get("code_digest") != current_code_digest:
        problems.append("preflight code binding drift")
    canonical_target = {name: (plan.get("target") or {}).get(name) for name in backup.TARGET_FIELDS}
    if document.get("target") != canonical_target:
        problems.append("preflight target binding drift")
    if document.get("writer_role") != apply_packet.get("writer_role"):
        problems.append("preflight writer binding drift")
    expected_backup = _backup_binding(backup_path)
    if document.get("backup") != expected_backup:
        problems.append("preflight backup binding drift")
    store = document.get("receipt_store") or {}
    if store.get("state") != "complete" or not isinstance(store.get("schema_sha256"), str):
        problems.append("preflight receipt-store binding is incomplete")
    population = document.get("source_population") or {}
    if population.get("records") != len(list(plan.get("records") or [])):
        problems.append("preflight source population count drift")
    if population.get("source_ids_sha256") != _digest([record.get("source_id") for record in plan.get("records") or []]):
        problems.append("preflight source population id binding drift")
    if document.get("validation") != {"plan": "complete", "design": "complete",
                                       "authorization": "complete", "backup": "complete",
                                       "source_population": "complete"}:
        problems.append("preflight validation record is incomplete")
    return problems


def schema_matches(connection: Any, document: Mapping[str, Any]) -> bool:
    """Small in-transaction schema recheck; receipt rows are deliberately excluded."""
    return _schema_binding(connection) == document.get("receipt_store")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--design", type=Path, required=True)
    parser.add_argument("--apply", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        raise PreflightRefused("preflight output path already exists")
    value = build(engine=get_engine(), plan=load_verified(args.plan),
                  design_packet=load_verified(args.design), apply_packet=load_verified(args.apply),
                  backup_path=args.backup, current_code_digest=_code_digest())
    digest = write_immutable(args.out, value)
    print(json.dumps({"outcome": "preflight_complete", "path": str(args.out), "digest": digest,
                      "source_population": value["source_population"]}, sort_keys=True))
    return 0


def _code_digest() -> str:
    # Late import avoids an apply/preflight import cycle while retaining one code authority.
    from scripts.kg import stage3_processing_receipt_apply as apply
    return apply.code_digest()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
