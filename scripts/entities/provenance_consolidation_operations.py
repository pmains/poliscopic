"""Shared row, fingerprint, planning-operation, and SQL mutation helpers.

The functions here deliberately have no dependency on the consolidation CLI or
repair-discovery code.  Keeping them independent makes the deterministic
operation rules usable by both automatic and human-adjudicated plans.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

RelationshipIdentity = tuple[int, str, int, str, int]
MentionIdentity = tuple[int, str, int, str | None, str]


@dataclass(frozen=True)
class ConsolidationOperation:
    """One canonical survivor and its redundant stale rows."""

    row_kind: str
    survivor_id: int | None
    survivor_is_current: bool
    replacement_source_id: int
    delete_ids: tuple[int, ...]


def _rows_as_dicts(
    connection: Connection, sql: str, identifiers: Sequence[int]
) -> list[dict[str, Any]]:
    """Load complete rows for a durable pre-change backup."""
    if not identifiers:
        return []
    statement = text(sql).bindparams(bindparam("ids", expanding=True))
    rows = connection.execute(statement, {"ids": list(identifiers)}).fetchall()
    return [dict(row._mapping) for row in rows]


def _rows_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    """Return a stable digest binding a plan to its complete backed-up rows."""
    serialized = json.dumps(
        sorted((dict(row) for row in rows), key=lambda row: int(row["id"])),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    """Return a stable digest of the immutable, approval-relevant plan."""
    # ``plan_id`` and ``generated_at`` identify a saved artifact but do not
    # change its proposed evidence or operations. Excluding them makes the
    # approval digest reproducible for equivalent plans.
    excluded_fields = {
        "plan_sha256", "mode", "result", "completed_at", "plan_id",
        "generated_at",
    }
    payload = {key: value for key, value in plan.items() if key not in excluded_fields}
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _write_json(document: Mapping[str, Any], path: Path) -> None:
    """Atomically persist and fsync a JSON document and its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    temporary_path.replace(path)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _operation_target_ids(operations: Iterable[Mapping[str, Any]]) -> set[int]:
    """Return stale row IDs that a set of operations will mutate or delete."""
    targets: set[int] = set()
    for operation in operations:
        targets.update(int(value) for value in operation["delete_ids"])
        if not operation["survivor_is_current"]:
            survivor_id = operation["survivor_id"]
            if survivor_id is None:
                raise ValueError("a repoint operation requires a survivor ID")
            targets.add(int(survivor_id))
    return targets


def _relationship_identity(
    row: Mapping[str, Any], source_id: int
) -> RelationshipIdentity:
    """Return the canonical identity for one relationship row."""
    return (
        int(row["from_entity_id"]),
        str(row["relationship"]),
        int(row["to_entity_id"]),
        str(row["provenance_type"]),
        source_id,
    )


def _mention_identity(row: Mapping[str, Any], source_id: int) -> MentionIdentity:
    """Return the canonical identity for one entity-mention row."""
    role = str(row["role_in_context"]) if row["role_in_context"] not in (None, "") else None
    return (
        int(row["entity_id"]),
        str(row["source_type"]),
        source_id,
        role,
        str(row["extracted_by"]),
    )


def _plan_operations(
    stale_rows: Sequence[Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
    replacement_ids: Mapping[tuple[str, int], int],
    *,
    row_kind: str,
) -> list[ConsolidationOperation]:
    """Group stale rows by proposed identity and choose one canonical survivor."""
    identity_function = (
        _relationship_identity if row_kind == "relationship" else _mention_identity
    )
    source_type_field = "provenance_type" if row_kind == "relationship" else "source_type"
    source_id_field = "provenance_id" if row_kind == "relationship" else "source_id"

    current_by_identity: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for row in current_rows:
        identity = identity_function(row, int(row[source_id_field]))
        current_by_identity[identity].append(int(row["id"]))

    stale_by_identity: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    replacement_by_identity: dict[tuple[Any, ...], int] = {}
    for row in stale_rows:
        source_reference = (str(row[source_type_field]), int(row[source_id_field]))
        replacement_id = replacement_ids[source_reference]
        identity = identity_function(row, replacement_id)
        stale_by_identity[identity].append(int(row["id"]))
        replacement_by_identity[identity] = replacement_id

    operations: list[ConsolidationOperation] = []
    for identity, stale_ids in sorted(stale_by_identity.items(), key=lambda item: repr(item[0])):
        current_ids = sorted(current_by_identity.get(identity, []))
        sorted_stale_ids = sorted(stale_ids)
        if current_ids:
            survivor_id = current_ids[0]
            survivor_is_current = True
            delete_ids = tuple(sorted_stale_ids)
        else:
            survivor_id = sorted_stale_ids[0]
            survivor_is_current = False
            delete_ids = tuple(sorted_stale_ids[1:])
        operations.append(ConsolidationOperation(
            row_kind=row_kind,
            survivor_id=survivor_id,
            survivor_is_current=survivor_is_current,
            replacement_source_id=replacement_by_identity[identity],
            delete_ids=delete_ids,
        ))
    return operations


def _delete_operation(row_kind: str, row_ids: Iterable[int]) -> ConsolidationOperation:
    """Build an explicit delete-only operation for human-rejected rows."""
    identifiers = tuple(sorted({int(row_id) for row_id in row_ids}))
    if not identifiers:
        raise ValueError("a delete-only operation requires at least one row ID")
    return ConsolidationOperation(
        row_kind=row_kind,
        survivor_id=None,
        survivor_is_current=True,
        replacement_source_id=0,
        delete_ids=identifiers,
    )


def _apply_operations(
    connection: Connection,
    operations: Iterable[Mapping[str, Any]],
    *,
    table: str,
    source_id_column: str,
) -> tuple[int, int]:
    """Apply the bounded updates and deletes represented by plan operations."""
    updated = 0
    deleted = 0
    for operation in operations:
        if not operation["survivor_is_current"]:
            result = connection.execute(text(f"""
                UPDATE {table}
                SET {source_id_column}=:source_id, updated_at=CURRENT_TIMESTAMP
                WHERE id=:survivor_id
            """), {
                "source_id": operation["replacement_source_id"],
                "survivor_id": operation["survivor_id"],
            })
            updated += int(result.rowcount or 0)
        delete_ids = list(operation["delete_ids"])
        if delete_ids:
            statement = text(f"DELETE FROM {table} WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            )
            result = connection.execute(statement, {"ids": delete_ids})
            deleted += int(result.rowcount or 0)
    return updated, deleted
