"""Build and validate human-adjudicated provenance consolidation plans."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from scripts.entities.provenance_consolidation_operations import (
    _delete_operation,
    _plan_fingerprint,
    _plan_operations,
    _rows_as_dicts,
    _rows_fingerprint,
)
from scripts.entities.provenance_repair import _unresolved_relationships

PHOENIX_TZ = ZoneInfo("America/Phoenix")


def _adjudication_entries_by_id(
    entries: Sequence[Mapping[str, Any]],
    *,
    id_field: str,
    entry_name: str,
) -> dict[int, Mapping[str, Any]]:
    """Index explicit adjudication entries and reject duplicate row decisions."""
    indexed: dict[int, Mapping[str, Any]] = {}
    for entry in entries:
        reason = str(entry.get("reason", "")).strip()
        if not reason:
            raise ValueError(f"every {entry_name} requires a reason")
        raw_identifiers = entry.get(id_field)
        if raw_identifiers is None and id_field.endswith("s"):
            singular_identifier = entry.get(id_field[:-1])
            raw_identifiers = [] if singular_identifier is None else [singular_identifier]
        for raw_identifier in raw_identifiers or []:
            identifier = int(raw_identifier)
            if identifier in indexed:
                raise ValueError(f"row {identifier} has more than one adjudication decision")
            indexed[identifier] = entry
    return indexed


def build_human_adjudication_plan(
    engine: Engine,
    adjudication: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a bounded plan covering the exact unresolved relationship set.

    Human input selects source replacements or explicitly rejects unsupported
    rows. Database operations, collision handling, mention reconciliation, and
    complete backups are derived from current state rather than hand-authored.
    """
    if int(adjudication.get("version", 0)) != 1:
        raise ValueError("human adjudication version must be 1")
    adjudicator = adjudication.get("adjudicated_by", adjudication.get("approved_by", ""))
    if not str(adjudicator).strip():
        raise ValueError("human adjudication requires adjudicated_by")
    if not str(adjudication.get("rule", "")).strip():
        raise ValueError("human adjudication requires a decision rule")

    assignments = list(adjudication.get("source_assignments", []))
    rejections = list(adjudication.get("rejected_relationships", []))
    rejected_mentions = list(adjudication.get("rejected_mentions", []))
    assigned_by_id = _adjudication_entries_by_id(
        assignments, id_field="relationship_ids", entry_name="source assignment"
    )
    rejected_by_id = _adjudication_entries_by_id(
        rejections, id_field="relationship_ids", entry_name="relationship rejection"
    )
    rejected_mentions_by_id = _adjudication_entries_by_id(
        rejected_mentions, id_field="mention_ids", entry_name="mention rejection"
    )
    overlap = set(assigned_by_id) & set(rejected_by_id)
    if overlap:
        raise ValueError(f"rows cannot be assigned and rejected: {sorted(overlap)}")

    with engine.connect() as connection:
        unresolved_rows = _unresolved_relationships(connection)
        unresolved_by_id = {
            decision.relationship_id: {
                "id": decision.relationship_id,
                "provenance_type": decision.provenance_type,
                "provenance_id": decision.stale_provenance_id,
            }
            for decision in unresolved_rows
        }
        decided_ids = set(assigned_by_id) | set(rejected_by_id)
        if decided_ids != set(unresolved_by_id):
            missing = sorted(set(unresolved_by_id) - decided_ids)
            unexpected = sorted(decided_ids - set(unresolved_by_id))
            raise ValueError(
                "adjudication must cover the exact unresolved relationship set; "
                f"missing={missing}, unexpected={unexpected}"
            )

        expected_relationship_rows = int(adjudication.get("scope", {}).get(
            "expected_relationship_rows", len(unresolved_by_id)
        ))
        if expected_relationship_rows != len(unresolved_by_id):
            raise ValueError("adjudication relationship bound does not match current unresolved rows")

        for entry in assignments:
            relationship_ids = {int(identifier) for identifier in entry.get("relationship_ids", [])}
            actual_stale_ids = {
                int(unresolved_by_id[identifier]["provenance_id"])
                for identifier in relationship_ids
            }
            declared_stale_ids = {int(identifier) for identifier in entry.get("stale_source_ids", [])}
            if actual_stale_ids != declared_stale_ids:
                raise ValueError(
                    "source assignment stale_source_ids do not exactly match its rows: "
                    f"declared={sorted(declared_stale_ids)}, actual={sorted(actual_stale_ids)}"
                )

        replacement_by_source: dict[tuple[str, int], int] = {}
        relationship_replacements: dict[tuple[str, int], int] = {}
        for relationship_id, entry in assigned_by_id.items():
            row = unresolved_by_id[relationship_id]
            source_reference = (str(row["provenance_type"]), int(row["provenance_id"]))
            if source_reference[0] != "pz_item_detail":
                raise ValueError("human adjudication mode currently supports only pz_item_detail")
            replacement_id = int(entry["replacement_source_id"])
            existing_replacement = replacement_by_source.setdefault(source_reference, replacement_id)
            if existing_replacement != replacement_id:
                raise ValueError(f"source reference {source_reference} has conflicting replacements")
            relationship_replacements[source_reference] = replacement_id

        replacement_ids = sorted(set(replacement_by_source.values()))
        replacement_source_rows: list[dict[str, Any]] = []
        if replacement_ids:
            statement = text("SELECT * FROM pz_item_details WHERE id IN :ids ORDER BY id").bindparams(
                bindparam("ids", expanding=True)
            )
            replacement_source_rows = [
                dict(row._mapping)
                for row in connection.execute(statement, {"ids": replacement_ids})
            ]
        existing_sources = {int(row["id"]) for row in replacement_source_rows}
        if existing_sources != set(replacement_ids):
            raise ValueError(
                "replacement P&Z sources do not exist: "
                f"{sorted(set(replacement_ids) - existing_sources)}"
            )

        stale_relationships = _rows_as_dicts(
            connection,
            "SELECT * FROM entity_relationships WHERE id IN :ids ORDER BY id",
            sorted(unresolved_by_id),
        )
        source_references = sorted({
            (str(row["provenance_type"]), int(row["provenance_id"]))
            for row in stale_relationships
        })
        expected_source_references = int(adjudication.get("scope", {}).get(
            "expected_source_references", len(source_references)
        ))
        if expected_source_references != len(source_references):
            raise ValueError("adjudication source-reference bound does not match current rows")
        stale_mentions = [dict(row._mapping) for row in connection.execute(text("""
            SELECT DISTINCT m.* FROM entity_mentions m
            JOIN entity_relationships r
              ON r.provenance_type = m.source_type
             AND r.provenance_id = m.source_id
            WHERE r.id IN :ids
            ORDER BY m.id
        """).bindparams(bindparam("ids", expanding=True)), {
            "ids": sorted(unresolved_by_id),
        }).fetchall()]
        stale_mention_ids = {int(row["id"]) for row in stale_mentions}
        unexpected_mentions = sorted(set(rejected_mentions_by_id) - stale_mention_ids)
        if unexpected_mentions:
            raise ValueError(
                f"rejected mentions are outside the adjudicated sources: {unexpected_mentions}"
            )

        assigned_relationships = [
            row for row in stale_relationships if int(row["id"]) in assigned_by_id
        ]
        current_relationships = _rows_as_dicts(
            connection,
            """SELECT * FROM entity_relationships
               WHERE provenance_type='pz_item_detail'
                 AND provenance_id IN :ids ORDER BY id""",
            replacement_ids,
        )
        current_mentions = _rows_as_dicts(
            connection,
            """SELECT * FROM entity_mentions
               WHERE source_type='pz_item_detail'
                 AND source_id IN :ids ORDER BY id""",
            replacement_ids,
        )

    actionable_mentions = [
        row for row in stale_mentions
        if (str(row["source_type"]), int(row["source_id"])) in replacement_by_source
        or int(row["id"]) in rejected_mentions_by_id
    ]
    normal_mentions = [
        row for row in actionable_mentions if int(row["id"]) not in rejected_mentions_by_id
    ]
    undisposed_mentions = sorted(
        int(row["id"]) for row in stale_mentions
        if (str(row["source_type"]), int(row["source_id"])) not in replacement_by_source
        and int(row["id"]) not in rejected_mentions_by_id
    )
    if undisposed_mentions:
        raise ValueError(
            "mentions from rejected-only sources require explicit disposition: "
            f"{undisposed_mentions}"
        )
    missing_mention_replacements = sorted({
        (str(row["source_type"]), int(row["source_id"])) for row in normal_mentions
        if (str(row["source_type"]), int(row["source_id"])) not in replacement_by_source
    })
    if missing_mention_replacements:
        raise ValueError(
            "mentions cannot be reconciled without a source replacement: "
            f"{missing_mention_replacements}"
        )

    relationship_operations = _plan_operations(
        assigned_relationships, current_relationships, relationship_replacements,
        row_kind="relationship",
    )
    if rejected_by_id:
        relationship_operations.append(_delete_operation("relationship", rejected_by_id))
    mention_operations = _plan_operations(
        normal_mentions, current_mentions, replacement_by_source, row_kind="mention"
    )
    if rejected_mentions_by_id:
        mention_operations.append(_delete_operation("mention", rejected_mentions_by_id))

    backup = {
        "entity_relationships": stale_relationships,
        "entity_mentions": actionable_mentions,
    }
    relationship_operations.sort(key=lambda operation: (
        operation.row_kind, operation.survivor_id or -1, operation.delete_ids
    ))
    mention_operations.sort(key=lambda operation: (
        operation.row_kind, operation.survivor_id or -1, operation.delete_ids
    ))
    plan = {
        "plan_id": uuid.uuid4().hex,
        "generated_at": datetime.now(PHOENIX_TZ).isoformat(),
        "plan_kind": "human_adjudication",
        "adjudication": dict(adjudication),
        "source_references": [list(reference) for reference in source_references],
        "replacement_sources": {"pz_item_details": replacement_source_rows},
        "replacement_sources_sha256": {"pz_item_details": _rows_fingerprint(replacement_source_rows)},
        "backup": backup,
        "backup_sha256": {table: _rows_fingerprint(rows) for table, rows in backup.items()},
        "relationship_operations": [asdict(operation) for operation in relationship_operations],
        "mention_operations": [asdict(operation) for operation in mention_operations],
        "summary": {
            "relationship_rows_backed_up": len(stale_relationships),
            "relationship_rows_rejected": len(rejected_by_id),
            "relationship_rows_to_delete": sum(len(operation.delete_ids) for operation in relationship_operations),
            "relationship_rows_to_repoint": sum(not operation.survivor_is_current for operation in relationship_operations),
            "mention_rows_backed_up": len(actionable_mentions),
            "mention_rows_rejected": len(rejected_mentions_by_id),
            "mention_rows_to_delete": sum(len(operation.delete_ids) for operation in mention_operations),
            "mention_rows_to_repoint": sum(not operation.survivor_is_current for operation in mention_operations),
        },
    }
    plan["plan_sha256"] = _plan_fingerprint(plan)
    return plan
