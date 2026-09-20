"""Disabled additive schema packet for qualified-outcome migration."""

from __future__ import annotations

import hashlib
import json
from typing import Any

ENABLED = False
PACKET_VERSION = "kg-stage3-qualified-outcome-schema/1.0"
DESIGN_KIND = "kg-stage3-qualified-outcome-schema-design"

STATEMENTS = (
    "ALTER TABLE meeting_events ADD COLUMN IF NOT EXISTS outcome_base TEXT",
    "ALTER TABLE meeting_events ADD COLUMN IF NOT EXISTS outcome_qualifier TEXT",
    "ALTER TABLE meeting_events ADD CONSTRAINT ck_meeting_events_outcome_pair "
    "CHECK ((outcome_base IS NULL AND outcome_qualifier IS NULL) OR "
    "(outcome_base IS NOT NULL AND (outcome_qualifier IS NULL OR "
    "(outcome_base = 'approved' AND outcome_qualifier IN "
    "('with_conditions','with_stipulations','subject_to','as_amended')) OR "
    "(outcome_base = 'denied' AND outcome_qualifier = 'without_prejudice'))))",
    "CREATE TABLE meeting_event_outcome_migration_receipts ("
    "id BIGSERIAL PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES meeting_events(id) ON DELETE RESTRICT, "
    "plan_digest TEXT NOT NULL, disposition TEXT NOT NULL CHECK (disposition IN "
    "('applied','replay','quarantine','rolled_back')), "
    "legacy_outcome TEXT NOT NULL, normalized_outcome TEXT NOT NULL, "
    "outcome_base TEXT, outcome_qualifier TEXT, evidence JSONB NOT NULL, "
    "reason TEXT NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
    "UNIQUE(event_id, plan_digest, disposition))",
    "CREATE INDEX ix_event_outcome_receipts_event ON meeting_event_outcome_migration_receipts(event_id)",
    "CREATE FUNCTION refuse_event_outcome_receipt_mutation() RETURNS trigger LANGUAGE plpgsql "
    "AS $$ BEGIN RAISE EXCEPTION 'outcome migration receipts are append-only'; END $$",
    "CREATE TRIGGER trg_event_outcome_receipts_immutable BEFORE UPDATE OR DELETE ON "
    "meeting_event_outcome_migration_receipts FOR EACH ROW EXECUTE FUNCTION "
    "refuse_event_outcome_receipt_mutation()",
    "REVOKE UPDATE, DELETE, TRUNCATE ON meeting_event_outcome_migration_receipts FROM PUBLIC",
)


def build_packet(plan: dict[str, Any]) -> dict[str, Any]:
    body = {"kind": DESIGN_KIND, "version": PACKET_VERSION, "mode": "design-only", "enabled": False,
            "applied": False, "target": "poliscopic_dev", "plan_digest": plan.get("digest"),
            "schema_statements": list(STATEMENTS), "data_operations": [],
            "write_path": "guarded-development-runner-disabled", "rollback": [
                "DROP TABLE meeting_event_outcome_migration_receipts",
                "DROP FUNCTION refuse_event_outcome_receipt_mutation()",
                "ALTER TABLE meeting_events DROP CONSTRAINT ck_meeting_events_outcome_pair",
                "ALTER TABLE meeting_events DROP COLUMN outcome_qualifier",
                "ALTER TABLE meeting_events DROP COLUMN outcome_base",
            ]}
    digest = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    return {**body, "digest": digest}


def validate_packet(packet: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    problems = []
    body = {key: value for key, value in packet.items() if key != "digest"}
    digest = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    if packet.get("digest") != digest:
        problems.append("packet digest mismatch")
    if packet.get("mode") != "design-only" or packet.get("enabled") is not False \
            or packet.get("applied") is not False \
            or packet.get("write_path") != "guarded-development-runner-disabled":
        problems.append("packet is not disabled design-only")
    if packet.get("target") != "poliscopic_dev" or packet.get("plan_digest") != plan.get("digest"):
        problems.append("packet target or plan binding mismatch")
    if packet.get("schema_statements") != list(STATEMENTS) or packet.get("data_operations") != []:
        problems.append("packet declaration drift")
    return problems


def execute_packet(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("qualified-outcome schema packet has no executable write path")
