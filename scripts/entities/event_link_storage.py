"""Database lookup, classification, and persistence for event participants."""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

from sqlalchemy import bindparam, text

ParticipantKey: TypeAlias = tuple[int, int, str]
ParticipantCandidate: TypeAlias = tuple[int, int, str, float]
ParticipantCounts: TypeAlias = dict[str, int]
ParticipantClassification: TypeAlias = tuple[
    ParticipantCounts,
    list[ParticipantCandidate],
    list[ParticipantCandidate],
]
LinkStats: TypeAlias = dict[str, int]


def load_meeting_entity_lookup(engine: Any) -> dict[str, dict[int, dict[str, Any]]]:
    """Build the meeting-to-entity lookup from source-supported mentions."""
    lookup: dict[str, dict[int, dict[str, Any]]] = {}
    queries = (
        """SELECT ai.meeting_id, em.entity_id, e.name, e.normalized_name,
                  em.role_in_context
           FROM entity_mentions em
           JOIN entities e ON e.id = em.entity_id
           JOIN agenda_items ai ON ai.id = em.source_id
           WHERE em.source_type = 'agenda_item'
             AND (e.resolution_status IS NULL
                  OR e.resolution_status = 'canonical')
           ORDER BY ai.meeting_id, em.entity_id, em.id""",
        """SELECT sd.meeting_id, em.entity_id, e.name, e.normalized_name,
                  em.role_in_context
           FROM entity_mentions em
           JOIN entities e ON e.id = em.entity_id
           JOIN supporting_documents sd ON sd.id = em.source_id
           WHERE em.source_type = 'supporting_document'
             AND sd.meeting_id IS NOT NULL
             AND (e.resolution_status IS NULL
                  OR e.resolution_status = 'canonical')
           ORDER BY sd.meeting_id, em.entity_id, em.id""",
    )
    for query in queries:
        with engine.connect() as connection:
            rows = connection.execute(text(query)).fetchall()
        for row in rows:
            meeting_id = str(row[0] or "")
            if not meeting_id:
                continue
            entity_id = int(row[1])
            meeting_entities = lookup.setdefault(meeting_id, {})
            meeting_entities.setdefault(entity_id, {
                "id": entity_id,
                "name": str(row[2] or ""),
                "normalized_name": str(row[3] or ""),
                "role": str(row[4] or "") or "participant",
            })
    return lookup


def _deduplicate_candidates(
    candidates: Sequence[ParticipantCandidate],
) -> list[ParticipantCandidate]:
    """Return unique participant keys, retaining the strongest evidence."""
    best: dict[ParticipantKey, ParticipantCandidate] = {}
    for event_id, entity_id, role, confidence in candidates:
        key = (int(event_id), int(entity_id), str(role))
        confidence = float(confidence)
        if key not in best or confidence > best[key][3]:
            best[key] = (key[0], key[1], key[2], confidence)
    return list(best.values())


def _storage_confidence(value: float) -> float:
    """Return the IEEE-754 float32 value stored by PostgreSQL ``real``."""
    return struct.unpack("!f", struct.pack("!f", float(value)))[0]


def storage_confidence(value: float) -> float:
    """Canonical float4 normalization of a participant confidence.

    Public name for :func:`_storage_confidence`, the single float-conversion
    implementation in this codebase.  Callers that model participant writes must
    use this rather than converting to float32 themselves.
    """
    return _storage_confidence(value)


def confidence_increases(new: float, existing: float) -> bool:
    """Whether ``new`` should overwrite ``existing`` under storage semantics.

    Mirrors :func:`_classify_candidate_rows`: both sides are normalized to the
    stored ``real`` value first, and the update happens only when the new value is
    **strictly greater**.  An unchanged float4 value is therefore a replay (a
    no-op), never an update.
    """
    return _storage_confidence(new) > _storage_confidence(existing)


def _classify_candidate_rows(
    existing: Mapping[ParticipantKey, float],
    candidates: Sequence[ParticipantCandidate],
) -> ParticipantClassification:
    """Classify candidates after canonicalizing confidence for storage."""
    counts: ParticipantCounts = {
        "participants_inserted": 0,
        "participants_updated": 0,
        "participant_replay_collisions": 0,
    }
    insert_rows: list[ParticipantCandidate] = []
    update_rows: list[ParticipantCandidate] = []
    for event_id, entity_id, role, confidence in _deduplicate_candidates(candidates):
        key = (int(event_id), int(entity_id), str(role))
        confidence = _storage_confidence(confidence)
        if key not in existing:
            counts["participants_inserted"] += 1
            insert_rows.append((key[0], key[1], key[2], confidence))
        elif confidence > _storage_confidence(existing[key]):
            counts["participants_updated"] += 1
            update_rows.append((key[0], key[1], key[2], confidence))
        else:
            counts["participant_replay_collisions"] += 1
    return counts, insert_rows, update_rows


def _empty_link_stats() -> LinkStats:
    """Keep legacy counters while exposing explicit accounting counters."""
    return {
        "events_processed": 0,
        "names_from_text": 0,
        "matched_via_text": 0,
        "names_via_meeting": 0,
        "matched_via_meeting": 0,
        "participants_written": 0,
        "participants_mutated": 0,
        "errors": 0,
        "events_attempted": 0,
        "participant_attempts": 0,
        "participants_inserted": 0,
        "participants_updated": 0,
        "participant_replay_collisions": 0,
        "unresolved_names": 0,
        "participants_planned_insert": 0,
        "participants_planned_update": 0,
    }


def _event_id_batch(engine: Any, cursor_id: int, batch_size: int) -> list[int]:
    """Fetch logical event IDs only; extraction rows are loaded separately."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT DISTINCT e.id
            FROM meeting_events e
            JOIN meeting_event_types et ON et.id = e.event_type_id
            JOIN meeting_event_extractions ee ON ee.meeting_event_id = e.id
            WHERE e.id > :cursor
            ORDER BY e.id
            LIMIT :limit
        """), {"cursor": cursor_id, "limit": batch_size}).fetchall()
    return [int(row[0]) for row in rows]


def _event_rows(engine: Any, event_ids: Sequence[int]) -> list[Any]:
    """Load every extraction row for a logical event batch."""
    if not event_ids:
        return []
    query = text("""
        SELECT e.id, e.meeting_id, e.outcome, e.case_number,
               ee.raw_text, et.slug AS event_type_slug,
               e.supporting_doc_id
        FROM meeting_events e
        JOIN meeting_event_types et ON et.id = e.event_type_id
        LEFT JOIN meeting_event_extractions ee ON ee.meeting_event_id = e.id
        WHERE e.id IN :event_ids
        ORDER BY e.id, ee.id
    """).bindparams(bindparam("event_ids", expanding=True))
    with engine.connect() as connection:
        return connection.execute(query, {"event_ids": event_ids}).fetchall()


def _existing_participants(
    connection: Any,
    candidates: Sequence[ParticipantCandidate],
) -> dict[ParticipantKey, float]:
    """Read existing keys/confidences in the transaction used for mutation."""
    if not candidates:
        return {}
    event_ids = sorted({int(row[0]) for row in candidates})
    entity_ids = sorted({int(row[1]) for row in candidates})
    query = text("""
        SELECT meeting_event_id, entity_id, role_in_event, confidence
        FROM event_participants
        WHERE meeting_event_id IN :event_ids
          AND entity_id IN :entity_ids
    """).bindparams(
        bindparam("event_ids", expanding=True),
        bindparam("entity_ids", expanding=True),
    )
    rows = connection.execute(query, {
        "event_ids": event_ids,
        "entity_ids": entity_ids,
    }).fetchall()
    return {
        (int(row[0]), int(row[1]), str(row[2])): float(row[3] or 0)
        for row in rows
    }


def _mutation_count(result: Any, operation: str) -> int:
    """Return a trustworthy affected-row count or fail the phase closed."""
    rowcount = result.rowcount
    if not isinstance(rowcount, int) or rowcount < 0:
        raise RuntimeError(f"{operation} did not return a valid SQL row count")
    return rowcount


def _participant_parameters(
    candidates: Sequence[ParticipantCandidate],
) -> list[dict[str, int | str | float]]:
    """Convert typed candidates to canonical SQL parameters."""
    return [
        {
            "meeting_event_id": int(event_id),
            "entity_id": int(entity_id),
            "role_in_event": str(role),
            "confidence": _storage_confidence(confidence),
        }
        for event_id, entity_id, role, confidence in candidates
    ]


def _insert_participants(
    connection: Any,
    candidates: Sequence[ParticipantCandidate],
) -> int:
    """Insert absent participant keys and return actual inserted rows.

    Validation happens in the caller *before* mutation classification, so this
    function only ever writes already-validated canonical roles.
    """
    if not candidates:
        return 0
    result = connection.execute(
        text("""
            INSERT INTO event_participants
                (meeting_event_id, entity_id, role_in_event, confidence)
            VALUES
                (:meeting_event_id, :entity_id, :role_in_event, :confidence)
            ON CONFLICT (meeting_event_id, entity_id, role_in_event)
            DO NOTHING
        """),
        _participant_parameters(candidates),
    )
    inserted = _mutation_count(result, "participant insert")
    return inserted


def _upgrade_participants(
    connection: Any,
    candidates: Sequence[ParticipantCandidate],
) -> int:
    """Apply guarded confidence upgrades and return actual updated rows."""
    if not candidates:
        return 0
    result = connection.execute(
        text("""
            UPDATE event_participants
            SET confidence = :confidence
            WHERE meeting_event_id = :meeting_event_id
              AND entity_id = :entity_id
              AND role_in_event = :role_in_event
              AND confidence < :confidence
        """),
        _participant_parameters(candidates),
    )
    updated = _mutation_count(result, "participant confidence update")
    return updated
