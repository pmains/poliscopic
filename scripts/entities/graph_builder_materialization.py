"""Resolution and persistence for structured graph specifications."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Connection, text

from scripts.entities.graph_builder_models import (
    EdgeSpec,
    EntitySpec,
    MentionSpec,
    Source,
    SourceStats,
)

log = logging.getLogger("graph_builder")
EntityCache = dict[tuple[str, str], int]


def load_all_entity_ids(connection: Connection) -> EntityCache:
    """Load every stable entity identity into the shared in-memory cache."""
    rows = connection.execute(
        text("SELECT normalized_name, entity_type, id FROM entities")
    ).fetchall()
    cache = {(str(row[0]), str(row[1])): int(row[2]) for row in rows}
    log.info("  Loaded %d existing entity mappings", len(cache))
    return cache


def run_source(
    source: Source,
    connection: Connection,
    entity_cache: EntityCache,
    dry_run: bool = False,
    verbose: bool = False,
) -> SourceStats:
    """Run one source as entities, relationships, then provenance mentions.

    The caller owns the transaction.  New ids are returned in ``stats`` and
    must only be copied into the shared cache after its transaction commits.
    """
    row_count, entity_specs, edge_specs, mention_specs = _collect_specs(source, connection)
    stats = SourceStats(
        edges_attempted=len(edge_specs),
        mentions_attempted=len(mention_specs),
    )
    if verbose:
        log.info(
            "  Query returned %d rows → %d entity specs, %d edge specs, "
            "%d mention specs",
            row_count,
            len(entity_specs),
            len(edge_specs),
            len(mention_specs),
        )

    unique_specs = _unique_entities(entity_specs)
    stats.entities_attempted = len(unique_specs)
    existing_specs, new_specs = _partition_entities(unique_specs, entity_cache)
    stats.entities_planned = len(new_specs)
    new_ids = _persist_entities(
        connection,
        entity_cache,
        existing_specs,
        new_specs,
        dry_run,
    )
    if not dry_run:
        stats.entities_inserted = len(new_specs)
    if verbose:
        log.info(
            "  → %d existing, %d new entities",
            len(existing_specs),
            len(new_specs),
        )

    lookup = {**entity_cache, **new_ids}
    planned_keys = {(spec.normalized_name, spec.entity_type) for spec in new_specs}
    _persist_edges(
        connection,
        edge_specs,
        entity_cache,
        planned_keys,
        lookup,
        stats,
        dry_run,
    )
    _persist_mentions(
        connection,
        mention_specs,
        entity_cache,
        planned_keys,
        lookup,
        stats,
        dry_run,
    )
    stats.new_ids = new_ids
    # The ontology-bearing values this source actually emitted, in emission
    # order, for bundle validation before the phase is sealed.  Counts alone
    # cannot be validated; these are the real values.
    stats.emitted_values.extend(
        ("entity_type", spec.entity_type) for spec in unique_specs
    )
    stats.emitted_values.extend(
        ("relationship", spec.relationship) for spec in edge_specs
    )
    stats.emitted_values.extend(
        ("role", spec.role) for spec in mention_specs if spec.role
    )
    if verbose:
        log.info(
            "  → %d edges: %d inserted, %d replay collisions, %d unresolved",
            stats.edges_attempted,
            stats.edges_inserted,
            stats.edge_replay_collisions,
            stats.edges_unresolved_endpoint,
        )
        log.info(
            "  → %d mentions: %d inserted, %d replay collisions, %d unresolved",
            stats.mentions_attempted,
            stats.mentions_inserted,
            stats.mention_replay_collisions,
            stats.mentions_unresolved_entity,
        )
    return stats


def _collect_specs(
    source: Source,
    connection: Connection,
) -> tuple[int, list[EntitySpec], list[EdgeSpec], list[MentionSpec]]:
    """Query one source and collect its emitted specifications."""
    entities: list[EntitySpec] = []
    edges: list[EdgeSpec] = []
    mentions: list[MentionSpec] = []
    source_rows = connection.execute(text(source.query)).fetchall()
    for row in source_rows:
        for entity, edge, mention in source.produce([dict(row._mapping)]):
            if entity is not None:
                entities.append(entity)
            if edge is not None:
                edges.append(edge)
            if mention is not None:
                mentions.append(mention)
    return len(source_rows), entities, edges, mentions


def _unique_entities(specs: list[EntitySpec]) -> list[EntitySpec]:
    """Keep the first spec for every stable entity identity."""
    by_identity: dict[tuple[str, str], EntitySpec] = {}
    for spec in specs:
        by_identity.setdefault((spec.normalized_name, spec.entity_type), spec)
    return list(by_identity.values())


def _partition_entities(
    specs: list[EntitySpec],
    cache: EntityCache,
) -> tuple[list[EntitySpec], list[EntitySpec]]:
    """Partition unique specs into cache hits and new database entities."""
    existing, new = [], []
    for spec in specs:
        (existing if (spec.normalized_name, spec.entity_type) in cache else new).append(spec)
    return existing, new


def _persist_entities(
    connection: Connection,
    cache: EntityCache,
    existing: list[EntitySpec],
    new: list[EntitySpec],
    dry_run: bool,
) -> EntityCache:
    """Refresh existing entities and bulk-insert new entities when not dry-running."""
    if dry_run:
        return {}
    for spec in existing:
        connection.execute(
            text(
                "UPDATE entities SET last_seen_at = CURRENT_TIMESTAMP "
                "WHERE id = :id"
            ),
            {"id": cache[(spec.normalized_name, spec.entity_type)]},
        )
    if not new:
        return {}
    values, params = [], {}
    for index, spec in enumerate(new):
        values.append(
            f"(:et{index}, :name{index}, :norm{index}, :government{index}, "
            f":jurisdiction{index}, 'unresolved', CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        params.update(
            {
                f"et{index}": spec.entity_type,
                f"name{index}": spec.name,
                f"norm{index}": spec.normalized_name,
                f"government{index}": spec.is_government,
                f"jurisdiction{index}": spec.jurisdiction_id,
            }
        )
    rows = connection.execute(text(f"""
        INSERT INTO entities (entity_type, name, normalized_name, is_government,
            jurisdiction_id, resolution_status, first_seen_at, last_seen_at,
            mention_count, created_at, updated_at)
        VALUES {', '.join(values)}
        RETURNING normalized_name, entity_type, id
    """), params).fetchall()
    return {(str(row[0]), str(row[1])): int(row[2]) for row in rows}


def _persist_edges(
    connection: Connection,
    specs: list[EdgeSpec],
    cache: EntityCache,
    planned_keys: set[tuple[str, str]],
    lookup: EntityCache,
    stats: SourceStats,
    dry_run: bool,
) -> None:
    """Classify edge specs and persist only resolved, non-replayed identities."""
    existing_keys = _existing_edge_keys(connection, specs)
    seen_keys: set[tuple[Any, ...]] = set()
    rows: list[tuple[int, int, EdgeSpec]] = []
    for spec in specs:
        from_key = spec.from_entity_norm, spec.from_type
        to_key = spec.to_entity_norm, spec.to_type
        from_is_unresolved = from_key not in cache and from_key not in planned_keys
        to_is_unresolved = to_key not in cache and to_key not in planned_keys
        if from_is_unresolved or to_is_unresolved:
            stats.edges_unresolved_endpoint += 1
            continue
        from_id, to_id = lookup.get(from_key), lookup.get(to_key)
        if from_id is not None and to_id is not None:
            key: tuple[Any, ...] = (
                from_id,
                spec.relationship,
                to_id,
                spec.source_type,
                spec.source_id,
            )
            is_existing = key in existing_keys
        else:
            key = (
                from_key,
                spec.relationship,
                to_key,
                spec.source_type,
                spec.source_id,
            )
            is_existing = False
        if key in seen_keys or is_existing:
            stats.edge_replay_collisions += 1
            continue
        seen_keys.add(key)
        stats.edges_planned += 1
        if from_id is not None and to_id is not None:
            rows.append((from_id, to_id, spec))
        else:
            # A planned edge whose endpoints have no id yet was not written.
            # Counting it as an insert would overstate the mutation; it is
            # classified as unresolved for the phase receipt.
            stats.edges_planned_unwritable += 1
    if not dry_run and rows:
        _insert_edges(connection, rows)
        stats.edges_inserted = len(rows)


def _existing_edge_keys(
    connection: Connection,
    specs: list[EdgeSpec],
) -> set[tuple[int, str, int, str, int]]:
    """Load all persisted edge identities for provenance types emitted this run."""
    if not specs:
        return set()
    types = sorted({spec.source_type for spec in specs})
    placeholders = ", ".join(f":type{index}" for index in range(len(types)))
    rows = connection.execute(
        text(f"""
            SELECT from_entity_id, relationship, to_entity_id,
                   provenance_type, provenance_id
            FROM entity_relationships
            WHERE provenance_type IN ({placeholders})
        """),
        {f"type{index}": value for index, value in enumerate(types)},
    ).fetchall()
    return {
        (int(row[0]), str(row[1]), int(row[2]), str(row[3]), int(row[4]))
        for row in rows
    }


def _insert_edges(connection: Connection, rows: list[tuple[int, int, EdgeSpec]]) -> None:
    """Bulk-insert relationship rows using SQL portable to SQLite and PostgreSQL."""
    values, params = [], {}
    for index, (from_id, to_id, spec) in enumerate(rows):
        values.append(
            f"(:from{index}, :to{index}, :relationship{index}, "
            f":type{index}, :source{index}, :label{index}, "
            "'relational', 1.0, CURRENT_TIMESTAMP)"
        )
        params.update(
            {
                f"from{index}": from_id,
                f"to{index}": to_id,
                f"relationship{index}": spec.relationship,
                f"type{index}": spec.source_type,
                f"source{index}": spec.source_id,
                f"label{index}": f"Structured: {spec.relationship}",
            }
        )
    connection.execute(
        text(f"""
            INSERT INTO entity_relationships
                (from_entity_id, to_entity_id, relationship, provenance_type,
                 provenance_id, source_label, edge_kind, confidence, created_at)
            VALUES {', '.join(values)}
        """),
        params,
    )


def _persist_mentions(
    connection: Connection,
    specs: list[MentionSpec],
    cache: EntityCache,
    planned_keys: set[tuple[str, str]],
    lookup: EntityCache,
    stats: SourceStats,
    dry_run: bool,
) -> None:
    """Classify mention specs and persist only resolved, non-replayed identities."""
    existing_keys = _existing_mention_keys(connection) if specs else set()
    seen_keys: set[tuple[Any, ...]] = set()
    rows: list[tuple[int, MentionSpec]] = []
    for spec in specs:
        entity_key = (spec.entity_norm, spec.entity_type)
        if entity_key not in cache and entity_key not in planned_keys:
            stats.mentions_unresolved_entity += 1
            continue
        entity_id, role = lookup.get(entity_key), spec.role or None
        if entity_id is not None:
            key: tuple[Any, ...] = (
                entity_id,
                spec.source_type,
                spec.source_id,
                role,
            )
            is_existing = key in existing_keys
        else:
            key = (entity_key, spec.source_type, spec.source_id, role)
            is_existing = False
        if key in seen_keys or is_existing:
            stats.mention_replay_collisions += 1
            continue
        seen_keys.add(key)
        stats.mentions_planned += 1
        if entity_id is not None:
            rows.append((entity_id, spec))
        else:
            # Same reasoning as planned-but-unwritable edges.
            stats.mentions_planned_unwritable += 1
    if not dry_run and rows:
        _insert_mentions(connection, rows)
        stats.mentions_inserted = len(rows)


def _existing_mention_keys(connection: Connection) -> set[tuple[int, str, int, str | None]]:
    """Load graph-builder mention identities, normalized for empty roles."""
    rows = connection.execute(text("""
        SELECT entity_id, source_type, source_id, NULLIF(role_in_context, '')
        FROM entity_mentions
        WHERE extracted_by = 'graph_builder'
    """)).fetchall()
    return {
        (int(row[0]), str(row[1]), int(row[2]), row[3])
        for row in rows
    }


def _insert_mentions(connection: Connection, rows: list[tuple[int, MentionSpec]]) -> None:
    """Bulk-insert graph-builder provenance with portable bound defaults."""
    values, params = [], {}
    for index, (entity_id, spec) in enumerate(rows):
        values.append(
            f"(:entity{index}, :type{index}, :source{index}, "
            f":mention{index}, :role{index}, :confidence{index}, "
            f"'graph_builder', :withdrawn{index}, CURRENT_TIMESTAMP)"
        )
        params.update(
            {
                f"entity{index}": entity_id,
                f"type{index}": spec.source_type,
                f"source{index}": spec.source_id,
                f"mention{index}": spec.mention_text,
                f"role{index}": spec.role or "",
                f"confidence{index}": spec.confidence,
                # PostgreSQL rejects SQLite's integer representation of false.
                f"withdrawn{index}": False,
            }
        )
    connection.execute(
        text(f"""
            INSERT INTO entity_mentions
                (entity_id, source_type, source_id, mention_text,
                 role_in_context, confidence, extracted_by, is_withdrawn,
                 created_at)
            VALUES {', '.join(values)}
        """),
        params,
    )
