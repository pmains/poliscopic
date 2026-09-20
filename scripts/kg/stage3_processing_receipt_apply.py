#!/usr/bin/env python3
"""Guarded, resumable development-only apply for current processing receipts.

The executable mechanics are intentionally hard-disabled.  No CLI is exposed and
``EXECUTION_ENABLED`` is false.  Once separately authorized, each bounded call
opens one SERIALIZABLE transaction, takes an advisory transaction lock, rechecks
the exact retained source identity under row locks, and appends only canonical
success/failure receipts.  Held plan rows are reported in the immutable terminal
receipt, never falsely represented as processed.  ``swept_at`` is never updated.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import text

from scripts.kg import stage3_processing_receipt as receipt
from scripts.kg import stage3_processing_receipt_apply_packet as authorization
from scripts.kg import stage3_processing_receipt_store_backup as backup
from scripts.kg import stage3_processing_receipt_store_packet as design
from scripts.kg import stage3_processing_receipt_store_rows as rows
from scripts.kg import stage3_processing_receipt_store_schema as schema
from scripts.kg import stage3_processing_plan_validator as validator
from scripts.kg.stage2_artifacts import load_verified, write_immutable

REPO = Path(__file__).resolve().parents[2]
EXECUTION_ENABLED = False
AUTHORIZATION_TOKEN = "stage3-processing-receipt-development-apply/v1"
LOCK_ID = 7303202609201
CODE_FILES = ("scripts/kg/stage3_processing_receipt_apply.py",
              "scripts/kg/stage3_processing_receipt_apply_packet.py",
              "scripts/kg/stage3_processing_receipt_store_schema.py",
              "scripts/kg/stage3_processing_receipt_store_rows.py",
              "scripts/kg/stage3_processing_receipt_store_backup.py",
              "scripts/kg/stage3_processing_receipt.py",
              "scripts/kg/stage3_processing_plan_validator.py")


class ApplyRefused(RuntimeError):
    pass


def code_digest(repo: Path = REPO) -> str:
    hashes = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in CODE_FILES}
    return receipt.canonical_sha256(hashes)


def _target(engine: Any) -> dict[str, Any]:
    url = engine.url
    return {"tier": "development", "database": str(url.database or ""),
            "dialect": str(url.get_backend_name()), "host": str(url.host or ""),
            "port": int(url.port or 0)}


def _source_record(connection: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    row = connection.execute(text("""
        SELECT id, text_content, text_extraction_method, swept_at, document_url,
               text_extracted_at, scraped_at
        FROM supporting_documents WHERE id=:id FOR UPDATE
    """), {"id": record["source_id"]}).mappings().one_or_none()
    if row is None:
        raise ApplyRefused(f"source {record['source_id']} disappeared after planning")
    value = dict(row)
    if list(receipt.processing_identity(value)) != list(record["processing_identity"]):
        raise ApplyRefused(f"source {record['source_id']} current identity drifted")
    return value


def _stored(connection: Any, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = connection.execute(text("""
        SELECT receipt_body FROM processing_receipts
        WHERE source_kind=:source_kind AND source_id=:source_id
          AND content_sha256=:content_sha256 AND extraction_method=:extraction_method
          AND extractor=:extractor AND extractor_version=:extractor_version
        ORDER BY recorded_at_sort_key DESC, receipt_digest DESC
    """), {
        "source_kind": record["processing_identity"][0], "source_id": record["source_id"],
        "content_sha256": record["processing_identity"][2],
        "extraction_method": record["processing_identity"][3],
        "extractor": record["processing_identity"][4],
        "extractor_version": record["processing_identity"][5]}).scalars()
    result: list[dict[str, Any]] = []
    for value in values:
        result.append(json.loads(value) if isinstance(value, str) else dict(value))
    return result


def _receipt_row(connection: Any, body: Mapping[str, Any]) -> None:
    connection.execute(text("INSERT INTO processing_receipts (receipt_body) VALUES (CAST(:body AS JSONB))"),
                       {"body": json.dumps(body, sort_keys=True)})


def _recorded_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema_is_complete(connection: Any) -> bool:
    """A pre-existing partial DDL set is unexplained and is never repaired."""
    columns = {row[0] for row in connection.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='processing_receipts'
    """))}
    expected = {name for name, _declaration, _source in schema.COLUMNS}
    triggers = {row[0] for row in connection.execute(text("""
        SELECT tgname FROM pg_trigger WHERE tgrelid=to_regclass('processing_receipts')
        AND NOT tgisinternal
    """))}
    return columns == expected and {schema.TRIGGER_ROW, schema.TRIGGER_TRUNCATE} <= triggers


def _terminal(*, packet: Mapping[str, Any], plan: Mapping[str, Any], offset: int,
              selected: int, success: int, failed: int, held: int, replay: int) -> dict[str, Any]:
    body = {"kind": "kg-stage3-processing-receipt-apply-terminal", "version": "1.0",
            "authorized_packet_digest": packet["digest"], "plan_digest": plan["digest"],
            "offset": offset, "selected": selected, "success": success, "failed": failed,
            "held": held, "replay": replay, "swept_at_updates": 0,
            "outcome": "replay" if selected == replay else "applied"}
    return {**body, "digest": receipt.canonical_sha256(body)}


def gate(*, engine: Any, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
         apply_packet: Mapping[str, Any], backup_path: str | Path | None,
         authorization_token: str) -> list[str]:
    problems = validator.validate_plan(plan, target=plan.get("target"), hashes=validator.code_hashes())
    problems += design.validate_packet(design_packet)
    problems += authorization.validate(apply_packet, plan=plan, design_packet=design_packet,
                                       current_code_digest=code_digest())
    problems += backup.backup_problems(backup_path, target=plan.get("target") or {})
    if authorization_token != AUTHORIZATION_TOKEN:
        problems.append("explicit apply authorization token mismatch")
    if _target(engine) != dict(plan.get("target") or {}):
        problems.append("live engine is not the exact development target")
    if not EXECUTION_ENABLED:
        problems.append("receipt apply execution is disabled")
    return problems


def apply_batch(engine: Any, *, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
                apply_packet: Mapping[str, Any], backup_path: str | Path,
                authorization_token: str, processor: Callable[[Mapping[str, Any]], None],
                offset: int = 0, terminal_dir: Path | None = None) -> dict[str, Any]:
    """Execute one exact plan window after every admission check; disabled by default."""
    if terminal_dir is None:
        raise ApplyRefused("an existing terminal-receipt directory is required before any write")
    problems = gate(engine=engine, plan=plan, design_packet=design_packet, apply_packet=apply_packet,
                    backup_path=backup_path, authorization_token=authorization_token)
    if problems:
        raise ApplyRefused("; ".join(problems))
    records = list(plan["records"])[offset:offset + apply_packet["batch_size"]]
    if not records:
        raise ApplyRefused("requested batch is outside the immutable plan")
    success = failed = held = replay = 0
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": LOCK_ID})
            # The schema is additive and is created only once, under the same lock.
            present = connection.execute(text("SELECT to_regclass('processing_receipts')")).scalar_one()
            if present is None:
                for statement in schema.render_ddl(writer_role=apply_packet["writer_role"]):
                    connection.execute(text(statement))
            elif not _schema_is_complete(connection):
                raise ApplyRefused("pre-existing receipt store is partial or drifted; restore required")
            for record in records:
                if record["outcome"] == "held":
                    held += 1
                    continue
                if record["outcome"] != "planned":
                    raise ApplyRefused("only an unprocessed planned row may enter a new apply batch")
                source = _source_record(connection, record)
                history = _stored(connection, record)
                if history:
                    folded = receipt.fold_receipts(history)
                    state = folded["current"].get(receipt.identity_key(record["processing_identity"]))
                    if state and state.get("status") == "success":
                        replay += 1
                        continue
                    raise ApplyRefused(f"source {record['source_id']} has non-replay receipt history")
                try:
                    processor(source)  # exact extractor invocation supplied by the authorized caller
                    body = receipt.build_receipt(source, status="success", reason="", recorded_at=_recorded_at())
                    success += 1
                except Exception as exc:  # a failure is durable, never misreported as success
                    body = receipt.build_receipt(source, status="failed", reason=f"processor:{type(exc).__name__}", recorded_at=_recorded_at())
                    failed += 1
                _receipt_row(connection, body)
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
    terminal = _terminal(packet=apply_packet, plan=plan, offset=offset, selected=len(records),
                         success=success, failed=failed, held=held, replay=replay)
    terminal_path = terminal_dir / f"kg-stage3-processing-receipt-apply-{terminal['digest']}.json"
    write_immutable(terminal_path, terminal)
    terminal["terminal_receipt_path"] = str(terminal_path)
    return terminal


def compensating_rollback(engine: Any, *, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
                          apply_packet: Mapping[str, Any], backup_path: str | Path,
                          authorization_token: str, offset: int = 0) -> dict[str, Any]:
    """Append failure compensation for prior successes; receipt history is never deleted."""
    problems = gate(engine=engine, plan=plan, design_packet=design_packet, apply_packet=apply_packet,
                    backup_path=backup_path, authorization_token=authorization_token)
    if problems:
        raise ApplyRefused("; ".join(problems))
    records = list(plan["records"])[offset:offset + apply_packet["batch_size"]]
    appended = replay = 0
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": LOCK_ID})
            if connection.execute(text("SELECT to_regclass('processing_receipts')")).scalar_one() is None or not _schema_is_complete(connection):
                raise ApplyRefused("receipt store is absent or partial; compensation is refused")
            for record in records:
                if record["outcome"] != "planned":
                    continue
                source = _source_record(connection, record)
                history = _stored(connection, record)
                folded = receipt.fold_receipts(history)
                state = folded["current"].get(receipt.identity_key(record["processing_identity"]))
                if state is None or state.get("status") != "success":
                    replay += 1
                    continue
                body = receipt.build_receipt(source, status="failed",
                    reason="authorized_compensating_rollback", recorded_at=_recorded_at())
                _receipt_row(connection, body)
                appended += 1
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
    return {"outcome": "compensated", "receipts_appended": appended, "replay": replay,
            "receipts_deleted": 0, "swept_at_updates": 0, "plan_digest": plan["digest"]}
