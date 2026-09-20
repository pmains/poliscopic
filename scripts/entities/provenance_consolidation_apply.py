"""Fail-closed validation and atomic execution for consolidation plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.engine import Engine

from scripts.entities.detect_entities import _integrity_snapshot
from scripts.entities.provenance_consolidation_operations import (
    _apply_operations,
    _operation_target_ids,
    _plan_fingerprint,
    _rows_as_dicts,
    _rows_fingerprint,
    _write_json,
)
from scripts.entities.provenance_repair import _unresolved_relationships

AUTOMATIC_PLAN_KIND = "automatic_consolidation"
_AUTOMATIC_SOURCE_TABLES = ("meeting_members", "pz_item_details")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROTECTED_FULL_DUMP = (
    _REPO_ROOT / "data" / "backups" / "poliscopic-dev-pre-kg-cleanup-20260909.dump"
)


def _section_fingerprint(section: Any) -> str:
    """Return a canonical digest for one durable backup-artifact section."""
    serialized = json.dumps(
        section, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _restore_instructions(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive explicit per-table restore steps from the saved operations.

    Every deletion is reversed by re-inserting its backed-up original row;
    every promoted (non-current) survivor is reset to its original stale
    provenance value. Current survivors are never touched by a restore.
    """
    instructions: list[dict[str, Any]] = []
    for table, operation_key, source_column in (
        ("entity_relationships", "relationship_operations", "provenance_id"),
        ("entity_mentions", "mention_operations", "source_id"),
    ):
        rows_by_id = {
            int(row["id"]): row for row in plan["backup"][table]
        }
        reinsert_ids: list[int] = []
        reset_survivors: list[dict[str, Any]] = []
        for operation in plan[operation_key]:
            reinsert_ids.extend(int(value) for value in operation["delete_ids"])
            if operation["survivor_is_current"]:
                continue
            survivor_id = int(operation["survivor_id"])
            original_row = rows_by_id.get(survivor_id)
            if original_row is None:
                raise RuntimeError(
                    f"{table} backup lacks the promoted survivor {survivor_id}"
                )
            reset_survivors.append({
                "id": survivor_id,
                "original_source_value": int(original_row[source_column]),
            })
        instructions.append({
            "table": table,
            "reinsert_original_rows": sorted(set(reinsert_ids)),
            "reset_promoted_survivors": sorted(
                reset_survivors, key=lambda item: int(item["id"])
            ),
        })
    return instructions


def _write_automatic_backup_artifact(
    plan: Mapping[str, Any], backup_path: Path,
) -> None:
    """Persist a fsynced restoration bundle before an automatic transaction."""
    backup = plan["backup"]
    restoration_manifest = {
        "entity_relationships": {
            "restore_rows": backup["entity_relationships"],
            "restore_sha256": plan["backup_sha256"]["entity_relationships"],
        },
        "entity_mentions": {
            "restore_rows": backup["entity_mentions"],
            "restore_sha256": plan["backup_sha256"]["entity_mentions"],
        },
    }
    artifact = {
        "artifact_kind": "automatic_consolidation_preapply_backup",
        "plan_sha256": plan["plan_sha256"],
        "backup": backup,
        "backup_sha256": plan["backup_sha256"],
        "restoration_manifest": restoration_manifest,
        "restore_instructions": _restore_instructions(plan),
        "section_sha256": {
            "backup": _section_fingerprint(backup),
            "restoration_manifest": _section_fingerprint(restoration_manifest),
        },
    }
    _write_json(artifact, backup_path)


def _validate_automatic_backup_path(
    backup_path: Path,
    saved_plan_path: Path | None = None,
) -> None:
    """Fail closed unless the row-level backup is a brand-new JSON file.

    The backup must never reuse an existing file, the saved plan file, or
    the protected full PostgreSQL dump, and must carry a ``.json`` suffix so
    a later restore can never mistake it for a raw dump.
    """
    resolved_backup = backup_path.resolve()
    if resolved_backup == _PROTECTED_FULL_DUMP.resolve():
        raise RuntimeError(
            "automatic backup output must not point at the protected PostgreSQL dump"
        )
    if backup_path.suffix != ".json":
        raise RuntimeError("automatic backup output must use a .json suffix")
    if saved_plan_path is not None and resolved_backup == saved_plan_path.resolve():
        raise RuntimeError(
            "automatic backup output must not equal the saved plan path"
        )
    if backup_path.exists():
        raise RuntimeError("automatic backup output must be a new file")


def _validate_automatic_plan(
    plan: Mapping[str, Any],
    before_integrity: Mapping[str, int],
    expected_plan_sha256: str,
) -> None:
    """Require a complete, non-ambiguous, digest-bound automatic plan."""
    if plan.get("plan_kind") != AUTOMATIC_PLAN_KIND:
        raise RuntimeError("expected an automatic-consolidation plan")
    if plan.get("plan_sha256") != expected_plan_sha256:
        raise RuntimeError("approved plan SHA-256 does not match the automatic plan")
    if _plan_fingerprint(plan) != expected_plan_sha256:
        raise RuntimeError("automatic-consolidation plan fingerprint is invalid")
    if before_integrity != plan.get("pre_operation_integrity"):
        raise RuntimeError("integrity baseline changed after the automatic plan was created")
    classification = plan.get("classification")
    if not isinstance(classification, Mapping):
        raise RuntimeError("automatic plan is missing its classification summary")
    expected_ids = {int(value) for value in plan.get("unresolved_relationship_ids", [])}
    classified_ids: set[int] = set()
    for status in ("repairable", "ambiguous", "unmatched"):
        entry = classification.get(status)
        if not isinstance(entry, Mapping):
            raise RuntimeError("automatic plan has an incomplete classification summary")
        identifiers = {int(value) for value in entry.get("relationship_ids", [])}
        if classified_ids & identifiers:
            raise RuntimeError("automatic plan classification overlaps relationship IDs")
        classified_ids.update(identifiers)
        if status != "repairable" and int(entry.get("count", 0)) != 0:
            raise RuntimeError(
                "automatic plan cannot apply while ambiguity or unmatched sources remain"
            )
    if classified_ids != expected_ids:
        raise RuntimeError("automatic plan classification does not cover its unresolved set")


def _validate_rows(
    connection,
    *,
    table: str,
    rows: list[Mapping[str, Any]],
    expected_hash: str,
    label: str,
) -> None:
    """Verify saved rows and their current database counterparts match exactly."""
    if _rows_fingerprint(rows) != expected_hash:
        raise RuntimeError(f"{label} backup fingerprint is invalid")
    identifiers = sorted(int(row["id"]) for row in rows)
    current_rows = _rows_as_dicts(
        connection, f"SELECT * FROM {table} WHERE id IN :ids ORDER BY id", identifiers
    )
    if _rows_fingerprint(current_rows) != expected_hash:
        raise RuntimeError(f"{label} changed after the consolidation plan was created")


def _validate_automatic_evidence(connection, plan: Mapping[str, Any]) -> None:
    """Revalidate replacement sources and unchanged current survivors."""
    sources = plan.get("replacement_sources")
    source_hashes = plan.get("replacement_sources_sha256")
    survivors = plan.get("current_survivors")
    survivor_hashes = plan.get("current_survivors_sha256")
    if not all(isinstance(value, Mapping) for value in (
        sources, source_hashes, survivors, survivor_hashes,
    )):
        raise RuntimeError("automatic plan is missing source or survivor evidence")
    for table in _AUTOMATIC_SOURCE_TABLES:
        expected_hash = source_hashes.get(table)
        if not isinstance(expected_hash, str):
            raise RuntimeError("automatic plan has an incomplete source fingerprint")
        _validate_rows(
            connection,
            table=table,
            rows=list(sources.get(table, [])),
            expected_hash=expected_hash,
            label=f"replacement {table}",
        )
    for table in ("entity_relationships", "entity_mentions"):
        expected_hash = survivor_hashes.get(table)
        if not isinstance(expected_hash, str):
            raise RuntimeError("automatic plan has an incomplete survivor fingerprint")
        _validate_rows(
            connection,
            table=table,
            rows=list(survivors.get(table, [])),
            expected_hash=expected_hash,
            label=f"current {table} survivor",
        )


def _validate_exact_unresolved_set(connection, plan: Mapping[str, Any]) -> set[int]:
    """Require the live unresolved relationship IDs to equal the saved plan."""
    current_ids = {
        row.relationship_id for row in _unresolved_relationships(connection)
    }
    expected_ids = {
        int(value) for value in plan.get("unresolved_relationship_ids", [])
    }
    if current_ids != expected_ids:
        raise RuntimeError(
            "current unresolved relationship set differs from the automatic plan"
        )
    return current_ids


def _validate_operation_counts(
    plan: Mapping[str, Any],
    *,
    relationships_updated: int,
    relationships_deleted: int,
    mentions_updated: int,
    mentions_deleted: int,
) -> None:
    """Require mutation row counts to match the saved plan exactly."""
    actual = {
        "relationships": {
            "updated": relationships_updated,
            "deleted": relationships_deleted,
        },
        "mentions": {
            "updated": mentions_updated,
            "deleted": mentions_deleted,
        },
    }
    if plan.get("expected_operation_counts") != actual:
        raise RuntimeError(
            "consolidation operation counts differ: "
            f"expected {plan.get('expected_operation_counts')}, found {actual}"
        )


def _validate_operation_postconditions(
    connection,
    *,
    table: str,
    source_id_column: str,
    operations: list[Mapping[str, Any]],
) -> None:
    """Verify every deletion and source repoint before committing."""
    deleted_ids = sorted({
        int(identifier)
        for operation in operations
        for identifier in operation["delete_ids"]
    })
    if _rows_as_dicts(
        connection,
        f"SELECT * FROM {table} WHERE id IN :ids ORDER BY id",
        deleted_ids,
    ):
        raise RuntimeError(f"{table} delete postcondition failed")
    for operation in operations:
        if operation["survivor_is_current"]:
            continue
        survivor_id = int(operation["survivor_id"])
        rows = _rows_as_dicts(
            connection,
            f"SELECT * FROM {table} WHERE id IN :ids ORDER BY id",
            [survivor_id],
        )
        if (
            len(rows) != 1
            or int(rows[0][source_id_column])
            != int(operation["replacement_source_id"])
        ):
            raise RuntimeError(f"{table} repoint postcondition failed")


def _validate_backed_up_targets(connection, plan: Mapping[str, Any]) -> None:
    """Require backup coverage and live target rows to match the saved plan."""
    table_specs = (
        ("entity_relationships", "relationship_operations"),
        ("entity_mentions", "mention_operations"),
    )
    for table, operation_key in table_specs:
        backed_up_rows = plan["backup"][table]
        backed_up_ids = {int(row["id"]) for row in backed_up_rows}
        preserved_ids = {
            int(row_id)
            for row_id in plan.get("preserved_row_ids", {}).get(table, [])
        }
        operation_ids = _operation_target_ids(plan[operation_key])
        if operation_ids & preserved_ids:
            raise RuntimeError(f"{table} rows cannot be both preserved and changed")
        if operation_ids | preserved_ids != backed_up_ids:
            raise RuntimeError(f"{table} backup does not exactly cover operation targets")
        _validate_rows(
            connection,
            table=table,
            rows=list(backed_up_rows),
            expected_hash=plan["backup_sha256"][table],
            label=table,
        )


def _validate_pz_replacement_sources(connection, plan: Mapping[str, Any]) -> None:
    """Preserve the established human-adjudication P&Z source check."""
    replacement_sources = plan.get("replacement_sources", {}).get(
        "pz_item_details", []
    )
    if not replacement_sources:
        return
    expected_hash = plan.get("replacement_sources_sha256", {}).get(
        "pz_item_details"
    )
    _validate_rows(
        connection,
        table="pz_item_details",
        rows=list(replacement_sources),
        expected_hash=expected_hash,
        label="replacement P&Z source",
    )


def apply_consolidation(
    engine: Engine,
    plan: Mapping[str, Any],
    *,
    expected_plan_sha256: str | None = None,
    automatic_backup_path: Path | None = None,
    saved_plan_path: Path | None = None,
) -> dict[str, Any]:
    """Apply a saved plan atomically after fail-closed evidence validation."""
    is_automatic = plan.get("plan_kind") == AUTOMATIC_PLAN_KIND
    if plan.get("plan_kind") == "human_adjudication":
        if _plan_fingerprint(plan) != plan.get("plan_sha256"):
            raise RuntimeError("human-adjudication plan fingerprint is invalid")
    before_integrity = _integrity_snapshot(engine)
    if is_automatic:
        if expected_plan_sha256 is None:
            raise RuntimeError("automatic apply requires an approved plan SHA-256")
        if automatic_backup_path is None:
            raise RuntimeError("automatic apply requires a pre-operation backup path")
        _validate_automatic_plan(plan, before_integrity, expected_plan_sha256)
        _validate_automatic_backup_path(
            automatic_backup_path, saved_plan_path=saved_plan_path
        )
        with engine.connect() as connection:
            _validate_exact_unresolved_set(connection, plan)
            _validate_automatic_evidence(connection, plan)
            _validate_backed_up_targets(connection, plan)
        _write_automatic_backup_artifact(plan, automatic_backup_path)

    with engine.begin() as connection:
        if is_automatic:
            _validate_exact_unresolved_set(connection, plan)
            _validate_automatic_evidence(connection, plan)
        _validate_pz_replacement_sources(connection, plan)
        _validate_backed_up_targets(connection, plan)

        unresolved_before_ids = {
            row.relationship_id for row in _unresolved_relationships(connection)
        }
        backed_up_relationship_ids = {
            int(row["id"]) for row in plan["backup"]["entity_relationships"]
        }
        if (
            plan.get("plan_kind") == "human_adjudication"
            and unresolved_before_ids != backed_up_relationship_ids
        ):
            raise RuntimeError(
                "current unresolved relationship set differs from the adjudicated plan"
            )

        relationships_updated, relationships_deleted = _apply_operations(
            connection,
            plan["relationship_operations"],
            table="entity_relationships",
            source_id_column="provenance_id",
        )
        mentions_updated, mentions_deleted = _apply_operations(
            connection,
            plan["mention_operations"],
            table="entity_mentions",
            source_id_column="source_id",
        )
        if is_automatic:
            _validate_operation_counts(
                plan,
                relationships_updated=relationships_updated,
                relationships_deleted=relationships_deleted,
                mentions_updated=mentions_updated,
                mentions_deleted=mentions_deleted,
            )
            _validate_operation_postconditions(
                connection,
                table="entity_relationships",
                source_id_column="provenance_id",
                operations=list(plan["relationship_operations"]),
            )
            _validate_operation_postconditions(
                connection,
                table="entity_mentions",
                source_id_column="source_id",
                operations=list(plan["mention_operations"]),
            )
        remaining_ids = {
            row.relationship_id for row in _unresolved_relationships(connection)
        }
        expected_remaining_ids = (
            {
                int(value)
                for value in plan["expected_unresolved_relationship_ids_after"]
            }
            if is_automatic
            else unresolved_before_ids - backed_up_relationship_ids
        )
        if remaining_ids != expected_remaining_ids:
            raise RuntimeError(
                "consolidation verification failed: expected unresolved IDs "
                f"{sorted(expected_remaining_ids)}, found {sorted(remaining_ids)}"
            )

    return {
        "relationships_updated": relationships_updated,
        "relationships_deleted": relationships_deleted,
        "mentions_updated": mentions_updated,
        "mentions_deleted": mentions_deleted,
        "before_integrity": before_integrity,
        "after_integrity": _integrity_snapshot(engine),
    }
