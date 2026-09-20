#!/usr/bin/env python3
"""Conservatively repair graph-builder provenance after source-row churn.

The default mode is read-only. Current structured source rows are passed
through the authoritative graph-builder producers, then stale provenance is
matched by canonical edge identity. A source reference is repairable only when
all matching edges identify exactly one current source row.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

_ENTITIES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ENTITIES_DIR.parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from db.core import get_engine
from scripts.entities.detect_entities import _integrity_snapshot
from scripts.entities.graph_builder import (
    MeetingAttendanceSource,
    PZItemDetailsSource,
    Source,
)

PHOENIX_TZ = ZoneInfo("America/Phoenix")
SUPPORTED_PROVENANCE_TYPES = ("meeting_member", "pz_item_detail")
EdgeIdentity = tuple[int, str, int]
SourceReference = tuple[str, int]


@dataclass(frozen=True)
class UnresolvedRelationship:
    """Minimum relationship evidence needed to locate a current source row."""

    relationship_id: int
    provenance_type: str
    stale_provenance_id: int
    from_entity_id: int
    relationship: str
    to_entity_id: int

    @property
    def edge_identity(self) -> EdgeIdentity:
        """Return graph identity without the unstable provenance row ID."""
        return (self.from_entity_id, self.relationship, self.to_entity_id)

    @property
    def source_reference(self) -> SourceReference:
        """Return the stale polymorphic source reference."""
        return (self.provenance_type, self.stale_provenance_id)


@dataclass(frozen=True)
class RepairDecision:
    """Classification of one stale source reference."""

    provenance_type: str
    stale_provenance_id: int
    replacement_provenance_id: int | None
    status: str
    relationship_ids: tuple[int, ...]
    candidate_ids: tuple[int, ...]


def classify_repairs(
    relationships: Sequence[UnresolvedRelationship],
    candidates: Mapping[tuple[str, EdgeIdentity], set[int]],
) -> list[RepairDecision]:
    """Classify stale references using every edge in their evidence bundle.

    A current source row is a valid replacement when all surviving producer
    signals intersect at one candidate. A multi-edge bundle requires at least
    two surviving signals; obsolete parser variants with no current candidate
    do not veto otherwise-convergent evidence. This prevents one generic edge
    from making an unrelated P&Z item look repairable.
    """
    relationships_by_reference: dict[
        SourceReference, list[UnresolvedRelationship]
    ] = defaultdict(list)
    for relationship in relationships:
        relationships_by_reference[relationship.source_reference].append(relationship)

    decisions: list[RepairDecision] = []
    for source_reference, related_edges in sorted(relationships_by_reference.items()):
        provenance_type, stale_id = source_reference
        candidate_sets: list[set[int]] = []
        for relationship in related_edges:
            candidate_sets.append(set(candidates.get(
                (provenance_type, relationship.edge_identity), set()
            )))
        supported_candidate_sets = [values for values in candidate_sets if values]
        enough_evidence = (
            len(related_edges) == 1 or len(supported_candidate_sets) >= 2
        )
        if enough_evidence and supported_candidate_sets:
            candidate_ids = set.intersection(*supported_candidate_sets)
        elif supported_candidate_sets:
            candidate_ids = set.union(*supported_candidate_sets)
        else:
            candidate_ids = set()

        if not enough_evidence and supported_candidate_sets:
            replacement_id = None
            status = "ambiguous"
        elif len(candidate_ids) == 1:
            replacement_id = next(iter(candidate_ids))
            status = "repairable"
        elif candidate_ids:
            replacement_id = None
            status = "ambiguous"
        else:
            replacement_id = None
            status = "unmatched"
        decisions.append(RepairDecision(
            provenance_type=provenance_type,
            stale_provenance_id=stale_id,
            replacement_provenance_id=replacement_id,
            status=status,
            relationship_ids=tuple(sorted(edge.relationship_id for edge in related_edges)),
            candidate_ids=tuple(sorted(candidate_ids)),
        ))
    return decisions


def _load_entity_ids(connection) -> dict[tuple[str, str], int]:
    rows = connection.execute(text(
        "SELECT normalized_name, entity_type, id FROM entities"
    )).fetchall()
    return {(str(row[0]), str(row[1])): int(row[2]) for row in rows}


def _candidate_edges(
    connection, sources: Iterable[Source]
) -> dict[tuple[str, EdgeIdentity], set[int]]:
    """Index current structured evidence by its emitted canonical edge."""
    entity_ids = _load_entity_ids(connection)
    candidates: dict[tuple[str, EdgeIdentity], set[int]] = defaultdict(set)
    for source in sources:
        rows = connection.execute(text(source.query)).fetchall()
        for row in rows:
            for _, edge, _ in source.produce([dict(row._mapping)]):
                if edge is None:
                    continue
                from_id = entity_ids.get((edge.from_entity_norm, edge.from_type))
                to_id = entity_ids.get((edge.to_entity_norm, edge.to_type))
                if from_id is None or to_id is None:
                    continue
                identity = (from_id, edge.relationship, to_id)
                candidates[(edge.source_type, identity)].add(int(edge.source_id))
    return candidates


def _unresolved_relationships(connection) -> list[UnresolvedRelationship]:
    rows = connection.execute(text("""
        SELECT r.id, r.provenance_type, r.provenance_id,
               r.from_entity_id, r.relationship, r.to_entity_id
        FROM entity_relationships r
        WHERE (r.provenance_type = 'meeting_member' AND NOT EXISTS (
                 SELECT 1 FROM meeting_members s WHERE s.id = r.provenance_id
              ))
           OR (r.provenance_type = 'pz_item_detail' AND NOT EXISTS (
                 SELECT 1 FROM pz_item_details s WHERE s.id = r.provenance_id
              ))
        ORDER BY r.provenance_type, r.provenance_id, r.id
    """)).fetchall()
    return [UnresolvedRelationship(
        relationship_id=int(row[0]),
        provenance_type=str(row[1]),
        stale_provenance_id=int(row[2]),
        from_entity_id=int(row[3]),
        relationship=str(row[4]),
        to_entity_id=int(row[5]),
    ) for row in rows]


def _affected_mentions(connection, decision: RepairDecision) -> int:
    return int(connection.execute(text("""
        SELECT COUNT(*) FROM entity_mentions
        WHERE source_type=:source_type AND source_id=:source_id
    """), {
        "source_type": decision.provenance_type,
        "source_id": decision.stale_provenance_id,
    }).scalar() or 0)


def _replacement_collisions(
    connection, decisions: Sequence[RepairDecision]
) -> dict[int, list[int]]:
    """Find current or jointly proposed edges that an update would duplicate."""
    collisions: dict[int, list[int]] = {}
    proposed_relationships: dict[
        tuple[int, str, int, str, int], list[int]
    ] = defaultdict(list)
    for decision in decisions:
        if decision.status != "repairable":
            continue
        for relationship_id in decision.relationship_ids:
            row = connection.execute(text("""
                SELECT stale.from_entity_id, stale.relationship,
                       stale.to_entity_id, stale.provenance_type
                FROM entity_relationships stale
                WHERE stale.id = :relationship_id
            """), {"relationship_id": relationship_id}).one()
            proposed_identity = (
                int(row[0]), str(row[1]), int(row[2]), str(row[3]),
                int(decision.replacement_provenance_id),
            )
            proposed_relationships[proposed_identity].append(relationship_id)

            existing_ids = connection.execute(text("""
                SELECT current.id
                FROM entity_relationships stale
                JOIN entity_relationships current
                  ON current.from_entity_id = stale.from_entity_id
                 AND current.relationship = stale.relationship
                 AND current.to_entity_id = stale.to_entity_id
                 AND current.provenance_type = stale.provenance_type
                 AND current.provenance_id = :replacement_id
                 AND current.id <> stale.id
                WHERE stale.id = :relationship_id
                ORDER BY current.id
            """), {
                "replacement_id": decision.replacement_provenance_id,
                "relationship_id": relationship_id,
            }).scalars().all()
            if existing_ids:
                collisions[relationship_id] = [int(value) for value in existing_ids]

    # Two stale rows can map to the same replacement identity even when that
    # identity did not exist before the transaction. Treat those jointly
    # proposed updates exactly like collisions with a current row.
    for relationship_ids in proposed_relationships.values():
        if len(relationship_ids) < 2:
            continue
        for relationship_id in relationship_ids:
            peers = [value for value in relationship_ids if value != relationship_id]
            collisions.setdefault(relationship_id, []).extend(peers)
    return collisions


def _apply_repairs(engine: Engine, decisions: Sequence[RepairDecision]) -> dict[str, int]:
    """Apply only pre-classified one-to-one source-reference repairs."""
    relationships_updated = 0
    mentions_updated = 0
    relationships_skipped_collision = 0
    mentions_skipped_collision = 0
    with engine.begin() as connection:
        collisions = _replacement_collisions(connection, decisions)
        for decision in decisions:
            if decision.status != "repairable":
                continue
            if any(identifier in collisions for identifier in decision.relationship_ids):
                relationships_skipped_collision += len(decision.relationship_ids)
                continue
            parameters = {
                "source_type": decision.provenance_type,
                "stale_id": decision.stale_provenance_id,
                "replacement_id": decision.replacement_provenance_id,
            }
            relationship_result = connection.execute(text("""
                UPDATE entity_relationships SET
                    provenance_id=:replacement_id,
                    updated_at=CURRENT_TIMESTAMP
                WHERE provenance_type=:source_type AND provenance_id=:stale_id
            """), parameters)
            relationships_updated += int(relationship_result.rowcount or 0)
            stale_mentions = connection.execute(text("""
                SELECT id, entity_id, NULLIF(role_in_context, ''), extracted_by
                FROM entity_mentions
                WHERE source_type=:source_type AND source_id=:stale_id
                ORDER BY id
            """), parameters).fetchall()
            for mention in stale_mentions:
                duplicate_exists = connection.execute(text("""
                    SELECT 1 FROM entity_mentions
                    WHERE entity_id=:entity_id
                      AND source_type=:source_type
                      AND source_id=:replacement_id
                      AND NULLIF(role_in_context, '') IS NOT DISTINCT FROM :role
                      AND extracted_by=:extracted_by
                    LIMIT 1
                """), {
                    **parameters,
                    "entity_id": int(mention[1]),
                    "role": mention[2],
                    "extracted_by": str(mention[3]),
                }).scalar()
                if duplicate_exists:
                    mentions_skipped_collision += 1
                    continue
                mention_result = connection.execute(text("""
                    UPDATE entity_mentions SET
                        source_id=:replacement_id,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=:mention_id
                """), {
                    "replacement_id": decision.replacement_provenance_id,
                    "mention_id": int(mention[0]),
                })
                mentions_updated += int(mention_result.rowcount or 0)
    return {
        "relationships_updated": relationships_updated,
        "mentions_updated": mentions_updated,
        "relationships_skipped_collision": relationships_skipped_collision,
        "mentions_skipped_collision": mentions_skipped_collision,
    }


def audit_and_repair(engine: Engine, *, apply: bool = False) -> dict[str, Any]:
    """Audit stale provenance and optionally apply unambiguous repairs."""
    before_integrity = _integrity_snapshot(engine)
    with engine.connect() as connection:
        unresolved = _unresolved_relationships(connection)
        candidates = _candidate_edges(
            connection, (MeetingAttendanceSource(), PZItemDetailsSource())
        )
        decisions = classify_repairs(unresolved, candidates)
        mention_counts = {
            f"{decision.provenance_type}:{decision.stale_provenance_id}":
                _affected_mentions(connection, decision)
            for decision in decisions
        }
        replacement_collisions = _replacement_collisions(connection, decisions)

    applied = _apply_repairs(engine, decisions) if apply else {
        "relationships_updated": 0,
        "mentions_updated": 0,
        "relationships_skipped_collision": 0,
        "mentions_skipped_collision": 0,
    }
    after_integrity = _integrity_snapshot(engine)
    status_counts = {
        status: sum(decision.status == status for decision in decisions)
        for status in ("repairable", "ambiguous", "unmatched")
    }
    return {
        "mode": "apply" if apply else "dry_run",
        "generated_at": datetime.now(PHOENIX_TZ).isoformat(),
        "before_integrity": before_integrity,
        "after_integrity": after_integrity,
        "relationship_rows_audited": len(unresolved),
        "source_references": len(decisions),
        "status_counts": status_counts,
        "affected_mentions": sum(mention_counts.values()),
        "replacement_collision_rows": len(replacement_collisions),
        "replacement_collisions": replacement_collisions,
        "mention_counts_by_source_reference": mention_counts,
        "applied": applied,
        "decisions": [asdict(decision) for decision in decisions],
    }


def _write_report(report: Mapping[str, Any], output_path: Path | None) -> Path:
    if output_path is None:
        timestamp = datetime.now(PHOENIX_TZ).strftime("%Y%m%d-%H%M%S")
        output_path = _REPO_ROOT / "data" / f"kg-provenance-repair-{timestamp}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary_path.replace(output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit or conservatively repair unstable KG provenance IDs"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply one-to-one repairs (default is read-only audit)",
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    report = audit_and_repair(get_engine(), apply=arguments.apply)
    report_path = _write_report(report, arguments.output)
    summary = {
        "mode": report["mode"],
        "relationship_rows_audited": report["relationship_rows_audited"],
        "source_references": report["source_references"],
        "status_counts": report["status_counts"],
        "applied": report["applied"],
        "report": str(report_path),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
