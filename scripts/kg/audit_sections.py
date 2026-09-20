"""Read-only database queries for the Stage 1 compatibility audit (Brief 018 Step 2).

Every function here issues SELECTs only and returns plain dictionaries.  The
orchestration, producer introspection, and artifact writing live in
``scripts.kg.compatibility_audit``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import registries as r

#: entity_type -> canonical node class used for direction/domain/range checks.
ENTITY_TYPE_NODE_CLASS: Mapping[str, str] = {
    "person": "person",
    "organization": "organization", "firm": "organization", "law_firm": "organization",
    "planning_firm": "organization", "developer": "organization", "agency": "organization",
    "utility": "organization", "vendor": "organization", "department": "organization",
    "advocacy_group": "organization",
    "case": "case", "parcel": "parcel", "address": "address", "meeting": "meeting",
    "body": "body", "jurisdiction": "jurisdiction",
}


def _fetch(engine: Engine, sql: str, **params: Any) -> list[tuple]:
    """Run one read-only statement."""
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql), params).fetchall()]


def _samples(engine: Engine, sql: str, limit: int, **params: Any) -> list[Any]:
    """Return up to ``limit`` sample identifiers for one value."""
    return [row[0] for row in _fetch(engine, sql, **params)][:limit]


def _classified_section(
    engine: Engine,
    *,
    category: str,
    observed_sql: str,
    sample_sql: str,
    sample_limit: int,
) -> dict[str, Any]:
    """Classify one observed value category with counts and samples."""
    rows = _fetch(engine, observed_sql)
    buckets: dict[str, list[dict[str, Any]]] = {
        "canonical": [], "compatibility_mapped": [], "quarantined": [], "unmapped": [],
    }
    total = 0
    for row in rows:
        value, count = str(row[0]), int(row[1])
        total += count
        status = r.classify_value(category, value)
        buckets[status].append({
            "value": value,
            "count": count,
            "samples": _samples(engine, sample_sql, sample_limit, value=value),
        })
    return {
        "category": category,
        "total_rows": total,
        "counts": {status: len(items) for status, items in buckets.items()},
        "distinct": len(rows),
        "values": {status: items for status, items in buckets.items() if items},
    }


def audit_entity_types(engine: Engine, sample_limit: int) -> dict[str, Any]:
    """Classify emitted entity types and check taxonomy leaf policy."""
    section = _classified_section(
        engine,
        category="entity_type",
        observed_sql=(
            "SELECT entity_type, count(*) FROM entities GROUP BY 1 ORDER BY 2 DESC"
        ),
        sample_sql="SELECT id FROM entities WHERE entity_type=:value ORDER BY id LIMIT 5",
        sample_limit=sample_limit,
    )
    taxonomy = _fetch(
        engine, "SELECT slug, parent_slug, entity_type FROM entity_types ORDER BY slug"
    )
    membership, leaf_violations = [], []
    for slug, parent, value in taxonomy:
        membership.append({
            "slug": str(slug),
            "parent_slug": None if parent is None else str(parent),
            "entity_type": str(value),
            "registered": value in r.ENTITY_TYPES,
        })
    emitted = [item["value"] for item in section["values"].get("canonical", [])]
    for value in emitted:
        if value in r.ENTITY_TYPES and not r.is_leaf_compliant(value):
            leaf_violations.append(value)
    section["taxonomy_rows"] = membership
    section["leaf_emission_violations"] = sorted(set(leaf_violations))
    return section


def audit_roles(engine: Engine, sample_limit: int) -> dict[str, Any]:
    """Classify mention roles (with producer pairs) and event participant roles."""
    by_producer = _fetch(engine, """
        SELECT role_in_context, source_type, extracted_by, count(*)
        FROM entity_mentions
        WHERE role_in_context IS NOT NULL AND role_in_context <> ''
        GROUP BY 1, 2, 3 ORDER BY 4 DESC
    """)
    total = sum(int(row[3]) for row in by_producer)
    records = []
    for role, source_type, extracted_by, count in by_producer:
        role = str(role)
        records.append({
            "value": role,
            "source_type": str(source_type),
            "producer": str(extracted_by),
            "count": int(count),
            "status": r.classify_value("role", role),
            "samples": _samples(
                engine,
                "SELECT id FROM entity_mentions WHERE role_in_context=:value "
                "ORDER BY id LIMIT 5",
                sample_limit, value=role,
            ),
        })
    participants = _fetch(
        engine, "SELECT role_in_event, count(*) FROM event_participants "
                "GROUP BY 1 ORDER BY 2 DESC"
    )
    participant_records = [
        {
            "value": str(role), "count": int(count),
            "status": r.classify_value("role", str(role)),
        }
        for role, count in participants
    ]
    return {
        "category": "role",
        "total_rows": total,
        "mention_roles": records,
        "event_participant_roles": participant_records,
        "unmapped": sorted({
            record["value"] for record in records if record["status"] == "unmapped"
        } | {
            record["value"] for record in participant_records
            if record["status"] == "unmapped"
        }),
    }


def audit_relationships(engine: Engine, sample_limit: int) -> dict[str, Any]:
    """Classify relationship predicates, edge kinds, and check direction/range."""
    rows = _fetch(engine, """
        SELECT r.relationship, r.edge_kind, count(*)
        FROM entity_relationships r GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    predicates = []
    for predicate, edge_kind, count in rows:
        predicate = str(predicate)
        predicates.append({
            "value": predicate,
            "edge_kind": None if edge_kind is None else str(edge_kind),
            "count": int(count),
            "status": r.classify_value("relationship", predicate),
            "samples": _samples(
                engine,
                "SELECT id FROM entity_relationships WHERE relationship=:value "
                "ORDER BY id LIMIT 5",
                sample_limit, value=predicate,
            ),
        })
    violations = _fetch(engine, """
        SELECT r.id, r.relationship, f.entity_type AS from_type, t.entity_type AS to_type
        FROM entity_relationships r
        LEFT JOIN entities f ON f.id = r.from_entity_id
        LEFT JOIN entities t ON t.id = r.to_entity_id
    """)
    direction_problems: list[dict[str, Any]] = []
    breakdown: dict[tuple[str, str, Any, Any], int] = {}
    for rel_id, predicate, from_type, to_type in violations:
        predicate = str(predicate)
        if predicate not in r.PREDICATES:
            continue
        domain_class = ENTITY_TYPE_NODE_CLASS.get(str(from_type))
        range_class = ENTITY_TYPE_NODE_CLASS.get(str(to_type))
        if domain_class is None or range_class is None:
            reason = "unmapped_endpoint_type"
        elif not r.direction_allows(predicate, domain_class, range_class):
            reason = "direction_or_domain_range"
        else:
            continue
        direction_problems.append({
            "relationship_id": int(rel_id), "predicate": predicate,
            "reason": reason, "from_type": from_type, "to_type": to_type,
        })
        key = (predicate, reason, from_type, to_type)
        breakdown[key] = breakdown.get(key, 0) + 1
    grouped = [
        {
            "predicate": predicate, "reason": reason,
            "from_type": from_type, "to_type": to_type, "count": count,
        }
        for (predicate, reason, from_type, to_type), count in sorted(
            breakdown.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    by_predicate: dict[str, int] = {}
    for group in grouped:
        by_predicate[group["predicate"]] = (
            by_predicate.get(group["predicate"], 0) + group["count"]
        )
    return {
        "category": "relationship",
        "total_rows": sum(item["count"] for item in predicates),
        "predicates": predicates,
        "edge_kinds": [
            {"value": str(kind), "count": int(count),
             "status": r.classify_value("edge_kind", str(kind))}
            for kind, count in _fetch(
                engine, "SELECT edge_kind, count(*) FROM entity_relationships "
                        "GROUP BY 1 ORDER BY 2 DESC"
            )
        ],
        "direction_violations": direction_problems[:sample_limit * 4],
        "direction_violation_count": len(direction_problems),
        # Complete, uncapped grouping so reported totals reconcile to their parts.
        "direction_violation_breakdown": grouped,
        "direction_violation_by_predicate": by_predicate,
        "direction_violation_breakdown_total": sum(
            group["count"] for group in grouped
        ),
        "direction_violation_predicates_checked": sorted(r.PREDICATES),
        "unmapped": sorted({
            item["value"] for item in predicates if item["status"] == "unmapped"
        }),
    }


def audit_events(engine: Engine, sample_limit: int) -> dict[str, Any]:
    """Classify event types and outcomes, including qualifier splits."""
    event_rows = _fetch(engine, """
        SELECT t.slug, t.parent_slug, count(e.id)
        FROM meeting_events e
        LEFT JOIN meeting_event_types t ON t.id = e.event_type_id
        GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    events = []
    for slug, parent, count in event_rows:
        value = None if slug is None else str(slug)
        canonical = r.normalize_event_slug(value) if value else None
        events.append({
            "value": value,
            "canonical_value": canonical,
            "parent_slug": None if parent is None else str(parent),
            "count": int(count),
            "registered": bool(canonical and canonical in r.EVENT_TYPES),
            "leaf_emission_violation": bool(canonical and canonical in r.EVENT_ROOTS),
        })
    outcome_rows = _fetch(engine, """
        SELECT outcome, count(*) FROM meeting_events
        WHERE outcome IS NOT NULL AND outcome <> '' GROUP BY 1 ORDER BY 2 DESC
    """)
    outcomes = []
    for outcome, count in outcome_rows:
        value = str(outcome)
        base, qualifier = r.split_outcome(value)
        outcomes.append({
            "value": value, "count": int(count),
            "status": r.classify_value("outcome", value),
            "base": base, "qualifier": qualifier,
            "samples": _samples(
                engine,
                "SELECT id FROM meeting_events WHERE outcome=:value ORDER BY id LIMIT 5",
                sample_limit, value=value,
            ),
        })
    return {
        "category": "event",
        "events": events,
        "outcomes": outcomes,
        "unregistered_event_types": sorted({
            str(item["value"]) for item in events if not item["registered"]
        }),
        "unmapped_outcomes": sorted({
            item["value"] for item in outcomes if item["status"] == "unmapped"
        }),
        "leaf_emission_violations": sorted({
            str(item["value"]) for item in events if item["leaf_emission_violation"]
        }),
        "inclusive_query_check": {
            base: list(r.accepted_outcome_forms(base))
            for base in ("approved", "denied")
        },
    }


def audit_provenance(engine: Engine, sample_limit: int) -> dict[str, Any]:
    """Inventory provenance/source reference types and extractor versions."""
    provenance = _fetch(engine, """
        SELECT provenance_type, count(*) FROM entity_relationships
        GROUP BY 1 ORDER BY 2 DESC
    """)
    pairs = _fetch(engine, """
        SELECT source_type, extracted_by, count(*) FROM entity_mentions
        GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    extractors = _fetch(engine, """
        SELECT extractor, extractor_version, count(*) FROM meeting_event_extractions
        GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    return {
        "category": "provenance",
        "provenance_types": [
            {"value": str(kind), "count": int(count),
             "status": r.classify_value("source_reference_type", str(kind))}
            for kind, count in provenance
        ],
        "mention_source_extractor_pairs": [
            {"source_type": str(source), "producer": str(producer), "count": int(count),
             "registered": [str(source), str(producer)] in [
                 list(pair) for pair in r.MENTION_SOURCE_EXTRACTOR_PAIRS
             ]}
            for source, producer, count in pairs
        ],
        "event_extractors": [
            {"extractor": str(name), "version": None if version is None else str(version),
             "count": int(count)}
            for name, version, count in extractors
        ],
        "assertion_class_columns_present": False,
        "model_version_columns_present": False,
        "relationships_missing_assertion_class": int(_fetch(
            engine, "SELECT count(*) FROM entity_relationships"
        )[0][0]),
        "mentions_missing_assertion_class": int(_fetch(
            engine, "SELECT count(*) FROM entity_mentions"
        )[0][0]),
    }


def audit_agenda_lineage(engine: Engine, sample_limit: int) -> dict[str, Any]:
    """Inventory agenda evidence lineage and identify unidentifiable rows."""
    agenda = _fetch(engine, """
        SELECT count(*)                              AS total,
               count(*) FILTER (WHERE agenda_item_text IS NOT NULL
                                  AND agenda_item_text <> '') AS with_text,
               count(*) FILTER (WHERE agenda_item_url IS NOT NULL) AS with_url
        FROM agenda_items
    """)[0]
    documents = _fetch(engine, """
        SELECT coalesce(text_extraction_method, '(none)') AS method,
               count(*) AS n,
               count(*) FILTER (WHERE text_content IS NULL) AS without_text,
               count(*) FILTER (WHERE content_hash IS NULL) AS without_hash
        FROM supporting_documents GROUP BY 1 ORDER BY 2 DESC
    """)
    offset_rows = _fetch(engine, """
        SELECT count(*) FROM meeting_event_extractions
        WHERE text_offset_start IS NULL OR text_offset_end IS NULL
    """)[0][0]
    return {
        "category": "agenda_evidence_lineage",
        "lineage_steps_registered": list(r.AGENDA_LINEAGE_STEPS),
        "agenda_items": {
            "total": int(agenda[0]),
            "with_text": int(agenda[1]),
            "with_url": int(agenda[2]),
            "text_extraction_method_recorded": False,
            "source_version_recorded": False,
            "rows_unable_to_identify_extraction_method": int(agenda[1]),
            "rows_unable_to_identify_source_version": int(agenda[1]),
        },
        "supporting_documents": [
            {"text_extraction_method": str(method), "count": int(count),
             "without_text": int(without_text), "without_content_hash": int(without_hash)}
            for method, count, without_text, without_hash in documents
        ],
        "event_extractions_missing_offsets": int(offset_rows),
        "distinct_observations_required": list(r.DISTINCT_AGENDA_OBSERVATIONS),
    }
