#!/usr/bin/env python3
"""Build a read-only contextual dossier for unresolved P&Z provenance."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text

_ENTITIES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ENTITIES_DIR.parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from db.core import get_engine
from scripts.entities.graph_builder import PZItemDetailsSource
from scripts.entities.provenance_repair import (
    _candidate_edges,
    _unresolved_relationships,
)

PHOENIX_TZ = ZoneInfo("America/Phoenix")


def build_context_dossier(engine) -> dict[str, Any]:
    """Return source and graph context without assigning replacements."""
    with engine.connect() as connection:
        relationships = [
            relationship for relationship in _unresolved_relationships(connection)
            if relationship.provenance_type == "pz_item_detail"
        ]
        candidates = _candidate_edges(connection, (PZItemDetailsSource(),))
        relationships_by_source: dict[int, list] = defaultdict(list)
        candidate_ids: set[int] = set()
        for relationship in relationships:
            relationships_by_source[relationship.stale_provenance_id].append(relationship)
            candidate_ids.update(candidates.get(
                (relationship.provenance_type, relationship.edge_identity), set()
            ))

        entity_ids = sorted({
            identifier
            for relationship in relationships
            for identifier in (relationship.from_entity_id, relationship.to_entity_id)
        })
        entity_statement = text("""
            SELECT id, name, normalized_name, entity_type
            FROM entities WHERE id IN :ids ORDER BY id
        """).bindparams(bindparam("ids", expanding=True))
        entities = {
            int(row.id): dict(row._mapping)
            for row in connection.execute(entity_statement, {"ids": entity_ids})
        }

        detail_statement = text("""
            SELECT pz.id, pz.body, pz.meeting_id, pz.meeting_db_id,
                   pz.agenda_item_number, pz.case_number, pz.project_name,
                   pz.applicant, pz.recommendation, pz.presented_by,
                   m.meeting_date, m.meeting_title,
                   ai.agenda_item_title
            FROM pz_item_details pz
            LEFT JOIN meetings m ON m.id=pz.meeting_db_id
            LEFT JOIN agenda_items ai ON ai.id=pz.agenda_item_id
            WHERE pz.id IN :ids ORDER BY pz.id
        """).bindparams(bindparam("ids", expanding=True))
        details = {
            int(row.id): dict(row._mapping)
            for row in connection.execute(detail_statement, {"ids": sorted(candidate_ids)})
        } if candidate_ids else {}

    source_groups = []
    for stale_id, source_relationships in sorted(relationships_by_source.items()):
        edge_records = []
        common_candidates: set[int] | None = None
        for relationship in source_relationships:
            edge_candidates = set(candidates.get(
                (relationship.provenance_type, relationship.edge_identity), set()
            ))
            common_candidates = (
                edge_candidates if common_candidates is None
                else common_candidates & edge_candidates
            )
            edge_records.append({
                "relationship_id": relationship.relationship_id,
                "relationship": relationship.relationship,
                "from_entity": entities[relationship.from_entity_id],
                "to_entity": entities[relationship.to_entity_id],
                "candidate_ids": sorted(edge_candidates),
            })
        all_candidate_ids = sorted({
            candidate_id for edge in edge_records for candidate_id in edge["candidate_ids"]
        })
        source_groups.append({
            "stale_provenance_id": stale_id,
            "common_candidate_ids": sorted(common_candidates or set()),
            "edges": edge_records,
            "candidate_details": [details[value] for value in all_candidate_ids],
        })

    return {
        "generated_at": datetime.now(PHOENIX_TZ).isoformat(),
        "source_reference_count": len(source_groups),
        "relationship_count": len(relationships),
        "source_references": source_groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    dossier = build_context_dossier(get_engine())
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = arguments.output.with_suffix(f"{arguments.output.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as stream:
        json.dump(dossier, stream, indent=2, sort_keys=True, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    temporary_path.replace(arguments.output)
    print(json.dumps({
        "output": str(arguments.output),
        "source_reference_count": dossier["source_reference_count"],
        "relationship_count": dossier["relationship_count"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
