"""Guarded development-only apply/rollback runner. Never invoked on import."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from scripts.kg.stage3_qualified_outcome_migration import (
    LEGACY_PROJECTION_SQL, group_projection_rows, validate_plan,
)
from scripts.kg.stage3_qualified_outcome_schema_packet import STATEMENTS, validate_packet
from scripts.kg.stage3_qualified_outcome_apply_packet import validate_apply_packet
from scripts.kg.stage2_artifacts import compute_digest
from scripts.kg.stage2_backup_verify import (
    capture_schema_signature, validate_stage2_receipt,
)

AUTHORIZATION = "kg-stage3-qualified-outcome-apply/v1"
LOCK_ID = 730320260920
CODE_FILES = (
    "scripts/kg/stage3_qualified_outcome_migration.py",
    "scripts/kg/stage3_qualified_outcome_schema_packet.py",
    "scripts/kg/stage3_qualified_outcome_apply_packet.py",
    "scripts/kg/stage3_qualified_outcome_apply.py",
    "scripts/kg/registries/events.py",
)


class ApplyRefused(RuntimeError):
    pass


def code_digest(root: Path | None = None) -> str:
    base = root or Path(__file__).resolve().parents[2]
    hashes = {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
              for name in CODE_FILES}
    return hashlib.sha256(__import__("json").dumps(
        hashes, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def live_schema_digest(engine: Any) -> str:
    return str(capture_schema_signature(engine)["schema_sha256"])


def validate_backup(receipt: dict[str, Any], plan: dict[str, Any],
                    apply_packet: dict[str, Any]) -> list[str]:
    problems = list(validate_stage2_receipt(receipt))
    target = receipt.get("target") or {}
    if target.get("tier") != "development" or "dev" not in str(target.get("database") or ""):
        problems.append("backup target mismatch")
    if (receipt.get("signatures") or {}).get("schema_sha256") != plan.get("schema_digest"):
        problems.append("backup schema binding mismatch")
    if compute_digest(receipt) != apply_packet.get("backup_receipt_digest"):
        problems.append("backup receipt digest binding mismatch")
    return problems


def validate_authority(*, design_packet: dict[str, Any], apply_packet: dict[str, Any],
                       plan: dict[str, Any], backup_receipt: dict[str, Any],
                       authorization: str, target: str,
                       current_code_digest: str) -> list[str]:
    problems = validate_packet(design_packet, plan)
    problems += validate_apply_packet(apply_packet, design_packet=design_packet, plan=plan)
    problems += validate_backup(backup_receipt, plan, apply_packet)
    if authorization != AUTHORIZATION:
        problems.append("authorization token mismatch")
    if target != "poliscopic_dev" or plan.get("target") != target:
        problems.append("development target mismatch")
    if not current_code_digest or current_code_digest != plan.get("code_digest") \
            or current_code_digest != apply_packet.get("code_digest"):
        problems.append("code binding drift")
    return problems


def verified_replay(engine: Any, *, plan: dict[str, Any]) -> dict[str, Any] | None:
    """Verify exact applied post-state using SELECTs only; return ``None`` pre-schema."""
    with engine.connect() as connection:
        objects = connection.execute(text("""
            SELECT
              to_regclass('meeting_event_outcome_migration_receipts') IS NOT NULL AS has_receipts,
              EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='meeting_events' AND column_name='outcome_base') AS has_base,
              EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='meeting_events' AND column_name='outcome_qualifier') AS has_qualifier
        """)).mappings().one()
        if not all(objects[key] for key in ("has_receipts", "has_base", "has_qualifier")):
            return None
        rows = [dict(row) for row in connection.execute(text("""
            SELECT r.event_id, r.disposition, r.legacy_outcome, r.normalized_outcome,
                   r.outcome_base, r.outcome_qualifier, r.evidence,
                   e.outcome AS current_outcome, e.outcome_base AS current_base,
                   e.outcome_qualifier AS current_qualifier,
                   d.text_content AS current_document_text
            FROM meeting_event_outcome_migration_receipts r
            JOIN meeting_events e ON e.id=r.event_id
            JOIN supporting_documents d ON d.id=e.supporting_doc_id
            WHERE r.plan_digest=:digest ORDER BY r.event_id, r.id
        """), {"digest": plan["digest"]}).mappings()]
    planned = {record["event_id"]: record for record in plan["records"]
               if record["disposition"] == "planned"}
    if len(rows) != len(planned):
        raise ApplyRefused("replay receipt population mismatch")
    seen = set()
    for row in rows:
        event_id = int(row["event_id"])
        record = planned.get(event_id)
        if record is None or event_id in seen or row["disposition"] != "applied":
            raise ApplyRefused("replay has foreign, duplicate, or rolled-back receipt")
        seen.add(event_id)
        evidence = row["evidence"]
        if isinstance(evidence, str):
            evidence = __import__("json").loads(evidence)
        expected_evidence = {"document_text_sha256": record["document_text_sha256"],
                             "attestations": record["evidence"]}
        actual = (row["legacy_outcome"], row["normalized_outcome"], row["outcome_base"],
                  row["outcome_qualifier"], row["current_outcome"], row["current_base"],
                  row["current_qualifier"], evidence)
        expected = (record["legacy_outcome"], record["normalized_legacy_outcome"],
                    record["outcome_base"], record["outcome_qualifier"],
                    record["normalized_legacy_outcome"], record["outcome_base"],
                    record["outcome_qualifier"], expected_evidence)
        if actual != expected:
            raise ApplyRefused(f"replay post-state mismatch for event {event_id}")
        current_text_digest = hashlib.sha256(
            str(row.get("current_document_text") or "").encode()).hexdigest()
        if current_text_digest != record["document_text_sha256"]:
            raise ApplyRefused(f"replay source evidence drift for event {event_id}")
    return {"outcome": "replay", "writes": 0, "verified_receipts": len(rows),
            "plan_digest": plan["digest"]}


def gate_apply(*, design_packet: dict[str, Any], apply_packet: dict[str, Any],
               plan: dict[str, Any], source_rows: Iterable[dict[str, Any]],
               backup_receipt: dict[str, Any], authorization: str, target: str,
               schema_digest: str, current_code_digest: str) -> list[str]:
    rows = list(source_rows)
    problems = validate_plan(plan, rows)
    problems += validate_authority(
        design_packet=design_packet, apply_packet=apply_packet, plan=plan,
        backup_receipt=backup_receipt, authorization=authorization, target=target,
        current_code_digest=current_code_digest)
    if schema_digest != plan.get("schema_digest"):
        problems.append("live schema drift")
    if schema_digest != apply_packet.get("schema_digest"):
        problems.append("apply packet schema binding drift")
    return problems


def apply(engine: Any, *, design_packet: dict[str, Any], apply_packet: dict[str, Any],
          plan: dict[str, Any], source_rows: Iterable[dict[str, Any]],
          backup_receipt: dict[str, Any], authorization: str, target: str,
          schema_digest: str, current_code_digest: str) -> dict[str, Any]:
    rows = list(source_rows)
    authority_problems = validate_authority(
        design_packet=design_packet, apply_packet=apply_packet, plan=plan,
        backup_receipt=backup_receipt, authorization=authorization, target=target,
        current_code_digest=current_code_digest)
    if authority_problems:
        raise ApplyRefused("; ".join(authority_problems))
    replay = verified_replay(engine, plan=plan)
    if replay is not None:
        return replay
    problems = gate_apply(design_packet=design_packet, apply_packet=apply_packet,
                          plan=plan, source_rows=rows,
                          backup_receipt=backup_receipt, authorization=authorization,
                          target=target, schema_digest=schema_digest,
                          current_code_digest=current_code_digest,
                          )
    if problems:
        raise ApplyRefused("; ".join(problems))
    planned = [record for record in plan["records"] if record["disposition"] == "planned"]
    if not planned:
        raise ApplyRefused("empty pre-schema plan has no applied replay state")
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": LOCK_ID})
        live_rows = group_projection_rows(
            [dict(row) for row in connection.execute(text(LEGACY_PROJECTION_SQL)).mappings()]
        )
        live_problems = validate_plan(plan, live_rows)
        if live_problems:
            raise ApplyRefused("live source revalidation failed: " + "; ".join(live_problems))
        for statement in STATEMENTS:
            connection.execute(text(statement))
        for record in planned:
            result = connection.execute(text("""
                UPDATE meeting_events SET outcome=:normalized, outcome_base=:base,
                       outcome_qualifier=:qualifier
                WHERE id=:event_id AND outcome=:legacy
                  AND outcome_base IS NULL AND outcome_qualifier IS NULL
            """), {"normalized": record["normalized_legacy_outcome"],
                    "base": record["outcome_base"], "qualifier": record["outcome_qualifier"],
                    "event_id": record["event_id"], "legacy": record["legacy_outcome"]})
            if result.rowcount != 1:
                raise ApplyRefused(f"event {record['event_id']} changed after planning")
            connection.execute(text("""
                INSERT INTO meeting_event_outcome_migration_receipts
                  (event_id, plan_digest, disposition, legacy_outcome, normalized_outcome, outcome_base,
                   outcome_qualifier, evidence, reason)
                VALUES (:event_id,:plan_digest,'applied',:legacy,:normalized,:base,:qualifier,
                        CAST(:evidence AS JSONB),:reason)
            """), {"event_id": record["event_id"], "plan_digest": plan["digest"],
                    "legacy": record["legacy_outcome"], "base": record["outcome_base"],
                    "normalized": record["normalized_legacy_outcome"],
                    "qualifier": record["outcome_qualifier"],
                    "evidence": __import__("json").dumps({
                        "document_text_sha256": record["document_text_sha256"],
                        "attestations": record["evidence"],
                    }, sort_keys=True),
                    "reason": record["reason"]})
        count = connection.execute(text("""
            SELECT count(*) FROM meeting_events e
            JOIN meeting_event_outcome_migration_receipts r ON r.event_id=e.id
            WHERE r.plan_digest=:digest AND r.disposition='applied'
              AND e.outcome_base=r.outcome_base AND e.outcome_qualifier=r.outcome_qualifier
              AND e.outcome=r.normalized_outcome
        """), {"digest": plan["digest"]}).scalar_one()
        if int(count) != len(planned):
            raise ApplyRefused("postcondition count mismatch")
    return {"outcome": "applied", "writes": len(planned) * 2, "plan_digest": plan["digest"]}


def rollback(engine: Any, *, plan: dict[str, Any], authorization: str, target: str) -> dict[str, Any]:
    if authorization != AUTHORIZATION or target != "poliscopic_dev":
        raise ApplyRefused("rollback authorization or target mismatch")
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": LOCK_ID})
        owned = connection.execute(text("""
            SELECT count(*) FROM meeting_event_outcome_migration_receipts
            WHERE plan_digest=:digest AND disposition='applied'
        """), {"digest": plan["digest"]}).scalar_one()
        expected = sum(record["disposition"] == "planned" for record in plan["records"])
        if int(owned) != expected:
            raise ApplyRefused("rollback receipt ownership is incomplete")
        for record in plan["records"]:
            if record["disposition"] != "planned":
                continue
            result = connection.execute(text("""
                UPDATE meeting_events SET outcome=:legacy, outcome_base=NULL,
                       outcome_qualifier=NULL
                WHERE id=:event_id AND outcome=:normalized AND outcome_base=:base
                  AND outcome_qualifier=:qualifier
            """), {"legacy": record["legacy_outcome"],
                    "normalized": record["normalized_legacy_outcome"],
                    "base": record["outcome_base"], "qualifier": record["outcome_qualifier"],
                    "event_id": record["event_id"]})
            if result.rowcount != 1:
                raise ApplyRefused(f"rollback post-state mismatch for {record['event_id']}")
            connection.execute(text("""
                INSERT INTO meeting_event_outcome_migration_receipts
                  (event_id, plan_digest, disposition, legacy_outcome, normalized_outcome,
                   outcome_base, outcome_qualifier, evidence, reason)
                VALUES (:event_id,:plan_digest,'rolled_back',:legacy,:normalized,:base,
                        :qualifier,CAST(:evidence AS JSONB),'authorized_compensating_rollback')
            """), {"event_id": record["event_id"], "plan_digest": plan["digest"],
                    "legacy": record["legacy_outcome"],
                    "normalized": record["normalized_legacy_outcome"],
                    "base": record["outcome_base"], "qualifier": record["outcome_qualifier"],
                    "evidence": __import__("json").dumps(record["evidence"], sort_keys=True)})
    return {"outcome": "rolled_back", "schema_retained": True,
            "receipts_retained": True, "plan_digest": plan["digest"]}
