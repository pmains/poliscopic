"""Build deterministic automatic provenance consolidation plans."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Connection, Engine

from scripts.entities.graph_builder import MeetingAttendanceSource, PZItemDetailsSource
from scripts.entities.provenance_consolidation_adjudication import PHOENIX_TZ
from scripts.entities.provenance_consolidation_operations import (
    _operation_target_ids,
    _plan_fingerprint,
    _plan_operations,
    _rows_as_dicts,
    _rows_fingerprint,
)
from scripts.entities.detect_entities import _integrity_snapshot
from scripts.entities.provenance_repair import (
    RepairDecision,
    _candidate_edges,
    _unresolved_relationships,
    classify_repairs,
)


def _repairable_decisions(connection: Connection) -> list[RepairDecision]:
    """Return only repairs whose replacement provenance is proven current."""
    return [
        decision for decision in _all_repair_decisions(connection)
        if decision.status == "repairable"
    ]


def _all_repair_decisions(connection: Connection) -> list[RepairDecision]:
    """Classify every unresolved source reference for immutable plan evidence."""
    unresolved = _unresolved_relationships(connection)
    candidates = _candidate_edges(
        connection, (MeetingAttendanceSource(), PZItemDetailsSource())
    )
    return classify_repairs(unresolved, candidates)


_SOURCE_TABLES = {
    "meeting_member": "meeting_members",
    "pz_item_detail": "pz_item_details",
}


def _classification_summary(
    decisions: list[RepairDecision],
) -> dict[str, dict[str, object]]:
    """Record every non-repairable decision instead of silently omitting it."""
    summary: dict[str, dict[str, object]] = {}
    for status in ("repairable", "ambiguous", "unmatched"):
        selected = [decision for decision in decisions if decision.status == status]
        summary[status] = {
            "source_references": [
                [decision.provenance_type, decision.stale_provenance_id]
                for decision in selected
            ],
            "relationship_ids": sorted({
                relationship_id
                for decision in selected
                for relationship_id in decision.relationship_ids
            }),
            "count": len(selected),
        }
    return summary


def _operation_counts(operations: list[dict[str, object]]) -> dict[str, int]:
    """Return exact update/delete expectations for one plan operation class."""
    return {
        "updated": sum(not bool(operation["survivor_is_current"])
                       for operation in operations),
        "deleted": sum(len(operation["delete_ids"]) for operation in operations),
    }


def build_consolidation_plan(engine: Engine) -> dict[str, Any]:
    """Build an automatic plan only from complete, current source evidence.

    The plan is saved as an immutable evidence bundle.  Ambiguous or unmatched
    sources remain visible in the bundle and make it ineligible for apply.
    """
    with engine.connect() as connection:
        pre_operation_integrity = _integrity_snapshot(engine)
        unresolved_rows = _unresolved_relationships(connection)
        unresolved_relationship_ids = sorted(
            row.relationship_id for row in unresolved_rows
        )
        decisions = _all_repair_decisions(connection)
        repairable_decisions = [
            decision for decision in decisions if decision.status == "repairable"
        ]
        replacement_ids = {
            (decision.provenance_type, decision.stale_provenance_id):
            int(decision.replacement_provenance_id)
            for decision in repairable_decisions
        }
        stale_relationship_ids = unresolved_relationship_ids
        stale_relationships = _rows_as_dicts(
            connection,
            "SELECT * FROM entity_relationships WHERE id IN :ids ORDER BY id",
            stale_relationship_ids,
        )
        stale_mentions = _rows_as_dicts(
            connection,
            """SELECT * FROM entity_mentions
               WHERE (source_type, source_id) IN (
                 SELECT provenance_type, provenance_id
                 FROM entity_relationships WHERE id IN :ids
               ) ORDER BY id""",
            stale_relationship_ids,
        )
        replacement_source_ids_by_type: dict[str, list[int]] = {}
        for provenance_type in _SOURCE_TABLES:
            replacement_source_ids_by_type[provenance_type] = sorted({
                replacement_id
                for (source_type, _), replacement_id in replacement_ids.items()
                if source_type == provenance_type
            })
        replacement_source_ids = sorted({
            replacement_id for replacement_ids_for_type
            in replacement_source_ids_by_type.values()
            for replacement_id in replacement_ids_for_type
        })
        current_relationships = _rows_as_dicts(
            connection,
            """SELECT * FROM entity_relationships
               WHERE provenance_type IN ('meeting_member', 'pz_item_detail')
                 AND provenance_id IN :ids ORDER BY id""",
            replacement_source_ids,
        )
        current_mentions = _rows_as_dicts(
            connection,
            """SELECT * FROM entity_mentions
               WHERE source_type IN ('meeting_member', 'pz_item_detail')
                 AND source_id IN :ids ORDER BY id""",
            replacement_source_ids,
        )

        replacement_sources = {
            "meeting_members": _rows_as_dicts(
                connection,
                "SELECT * FROM meeting_members WHERE id IN :ids ORDER BY id",
                replacement_source_ids_by_type["meeting_member"],
            ),
            "pz_item_details": _rows_as_dicts(
                connection,
                "SELECT * FROM pz_item_details WHERE id IN :ids ORDER BY id",
                replacement_source_ids_by_type["pz_item_detail"],
            ),
        }

    actionable_relationship_ids = {
        relationship_id
        for decision in repairable_decisions
        for relationship_id in decision.relationship_ids
    }
    actionable_relationships = [
        row for row in stale_relationships
        if int(row["id"]) in actionable_relationship_ids
    ]
    actionable_mentions = [
        row for row in stale_mentions
        if (str(row["source_type"]), int(row["source_id"])) in replacement_ids
    ]
    relationship_operations = _plan_operations(
        actionable_relationships, current_relationships, replacement_ids,
        row_kind="relationship",
    )
    mention_operations = _plan_operations(
        actionable_mentions, current_mentions, replacement_ids, row_kind="mention"
    )
    relationship_operation_dicts = [
        asdict(operation) for operation in relationship_operations
    ]
    mention_operation_dicts = [asdict(operation) for operation in mention_operations]
    current_survivor_ids = {
        "entity_relationships": sorted({
            int(operation["survivor_id"])
            for operation in relationship_operation_dicts
            if operation["survivor_is_current"] and operation["survivor_id"] is not None
        }),
        "entity_mentions": sorted({
            int(operation["survivor_id"])
            for operation in mention_operation_dicts
            if operation["survivor_is_current"] and operation["survivor_id"] is not None
        }),
    }
    current_survivors = {
        "entity_relationships": [
            row for row in current_relationships
            if int(row["id"]) in current_survivor_ids["entity_relationships"]
        ],
        "entity_mentions": [
            row for row in current_mentions
            if int(row["id"]) in current_survivor_ids["entity_mentions"]
        ],
    }
    backup = {
        "entity_relationships": stale_relationships,
        "entity_mentions": stale_mentions,
    }
    preserved_row_ids = {
        "entity_relationships": sorted(
            int(row["id"]) for row in stale_relationships
            if int(row["id"]) not in _operation_target_ids(
                relationship_operation_dicts
            )
        ),
        "entity_mentions": sorted(
            int(row["id"]) for row in stale_mentions
            if int(row["id"]) not in _operation_target_ids(mention_operation_dicts)
        ),
    }
    classification = _classification_summary(decisions)
    operation_counts = {
        "relationships": _operation_counts(relationship_operation_dicts),
        "mentions": _operation_counts(mention_operation_dicts),
    }
    plan = {
        "plan_id": uuid.uuid4().hex,
        "generated_at": datetime.now(PHOENIX_TZ).isoformat(),
        "plan_kind": "automatic_consolidation",
        "pre_operation_integrity": pre_operation_integrity,
        "unresolved_relationship_ids": unresolved_relationship_ids,
        "classification": classification,
        "repairable_source_references": len(repairable_decisions),
        "decisions": [asdict(decision) for decision in decisions],
        "backup": backup,
        "backup_sha256": {table: _rows_fingerprint(rows) for table, rows in backup.items()},
        "current_survivors": current_survivors,
        "current_survivors_sha256": {
            table: _rows_fingerprint(rows)
            for table, rows in current_survivors.items()
        },
        "replacement_sources": replacement_sources,
        "replacement_sources_sha256": {
            table: _rows_fingerprint(rows)
            for table, rows in replacement_sources.items()
        },
        "preserved_row_ids": preserved_row_ids,
        "relationship_operations": relationship_operation_dicts,
        "mention_operations": mention_operation_dicts,
        "expected_operation_counts": operation_counts,
        "expected_unresolved_relationship_ids_after": sorted(
            set(unresolved_relationship_ids) - actionable_relationship_ids
        ),
        "summary": {
            "relationship_rows_backed_up": len(stale_relationships),
            "relationship_rows_to_delete": operation_counts["relationships"]["deleted"],
            "relationship_rows_to_repoint": operation_counts["relationships"]["updated"],
            "mention_rows_backed_up": len(stale_mentions),
            "mention_rows_to_delete": operation_counts["mentions"]["deleted"],
            "mention_rows_to_repoint": operation_counts["mentions"]["updated"],
        },
    }
    plan["plan_sha256"] = _plan_fingerprint(plan)
    return plan
