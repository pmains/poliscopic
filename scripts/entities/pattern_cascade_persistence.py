"""Persistence and proposal classification for the pattern cascade.

Extracted from :mod:`scripts.entities.pattern_cascade` so that module stays
focused on scanning and orchestration.

Every function here classifies each proposal **exactly once** — insert, replay
no-op, or unresolved — and writes only what it classified as an insert.  Dry runs
classify the same proposals and write nothing, so a dry receipt describes exactly
what a live run would have written.

Classification is deliberately by *assertion identity* rather than by row id.
A dry run has no ids, so id-based replay detection would report every existing
row as a new insert and inflate the receipt.  Ids are resolved only to perform
the write.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

log = logging.getLogger("pattern_cascade")

__all__ = [
    "classify_and_write_edges",
    "classify_and_write_mentions",
    "pattern_cascade_accounting",
    "write_entity_proposals",
]

_ENTITY_INSERT = text("""
    INSERT INTO entities
        (entity_type, name, normalized_name, is_government,
         resolution_status,
         first_seen_at, last_seen_at, mention_count,
         created_at, updated_at)
    VALUES (:et, :name, :nn, False,
            'unresolved',
            now(), now(), 1, now(), now())
    ON CONFLICT (normalized_name, entity_type) DO UPDATE SET
        last_seen_at = now()
    RETURNING id
""")

_ENTITY_SELECT_ID = text(
    "SELECT id FROM entities WHERE normalized_name = :nn AND entity_type = :et"
)

_MENTION_EXISTS = text(
    "SELECT 1 FROM entity_mentions WHERE entity_id = :eid "
    "AND source_type = :st AND source_id = :sid AND role_in_context = :role"
)

_MENTION_INSERT = text("""
    INSERT INTO entity_mentions
        (entity_id, source_type, source_id, mention_text,
         context_snippet, confidence, extracted_by, role_in_context,
         created_at)
    VALUES (:eid, :st, :sid, :mt, :cs, :conf, :eb, :role, now())
""")


def write_entity_proposals(conn, new_entities: dict, entity_cache: dict,
                           *, dry_run: bool) -> None:
    """Insert planned entities, refreshing the cache with the ids returned.

    A dry run writes nothing, so no id is available and the cache is untouched;
    the caller resolves planned identities from ``planned_keys`` instead.
    """
    for (norm, etype), name in new_entities.items():
        if (norm, etype) in entity_cache:
            continue
        if dry_run:
            continue
        inserted = conn.execute(
            _ENTITY_INSERT, {"et": etype, "name": name, "nn": norm}
        ).fetchone()
        if inserted:
            entity_cache[(norm, etype)] = inserted[0]
            continue
        # Race: entity was created between our check and the INSERT.
        existing = conn.execute(
            _ENTITY_SELECT_ID, {"nn": norm, "et": etype}
        ).fetchone()
        if existing:
            entity_cache[(norm, etype)] = existing[0]


def classify_and_write_mentions(conn, rows: list[dict], entity_cache: dict,
                                planned_keys: set, *,
                                dry_run: bool) -> tuple[int, int]:
    """Classify and write mention proposals.

    Returns ``(replay_noop, unresolved)``; inserts are the remainder.
    """
    from scripts.entities.pattern_cascade import is_probable_person, normalize_name

    replay = 0
    unresolved = 0
    for row in rows:
        etype = ("person" if is_probable_person(str(row["mention_text"]))
                 else "organization")
        norm = normalize_name(str(row["mention_text"]), etype)
        entity_key = (norm, etype)
        if entity_key not in entity_cache and entity_key not in planned_keys:
            unresolved += 1
            continue
        entity_id = entity_cache.get(entity_key)
        duplicate = None
        if entity_id is not None:
            duplicate = conn.execute(_MENTION_EXISTS, {
                "eid": entity_id, "st": row["source_type"],
                "sid": row["source_id"], "role": row["role_in_context"],
            }).fetchone()
        if duplicate:
            replay += 1
            continue
        if dry_run:
            continue
        if entity_id is None:
            # Resolved identity but no id to write: never silently dropped.
            unresolved += 1
            continue
        conn.execute(_MENTION_INSERT, {
            "eid": entity_id, "st": row["source_type"], "sid": row["source_id"],
            "mt": row["mention_text"], "cs": row["context_snippet"],
            "conf": 90, "eb": "pattern_cascade",
            "role": row["role_in_context"],
        })
    return replay, unresolved


def _existing_edge_identities(conn, rows: list[dict]) -> set[tuple]:
    """Load already-stored agenda-item edges as assertion identities."""
    identities: set[tuple] = set()
    item_ids = list({row["provenance_id"] for row in rows})
    for start in range(0, len(item_ids), 100):
        chunk = item_ids[start:start + 100]
        id_list = ", ".join(str(i) for i in chunk)
        try:
            existing = conn.execute(text(f"""
                SELECT e1.normalized_name, e1.entity_type, r.relationship,
                       e2.normalized_name, e2.entity_type,
                       r.provenance_type, r.provenance_id
                FROM entity_relationships r
                JOIN entities e1 ON e1.id = r.from_entity_id
                JOIN entities e2 ON e2.id = r.to_entity_id
                WHERE r.provenance_type = 'agenda_item'
                  AND r.provenance_id IN ({id_list})
            """)).fetchall()
        except Exception:
            continue  # Table may not exist yet on a fresh database.
        for row in existing:
            identities.add((
                str(row[0]), str(row[1]), str(row[2]), str(row[3]),
                str(row[4]), str(row[5]), int(row[6]),
            ))
    return identities


def classify_and_write_edges(conn, rows: list[dict], entity_cache: dict,
                             planned_keys: set, *,
                             dry_run: bool) -> tuple[int, int, int]:
    """Classify and write relationship proposals.

    Returns ``(inserted, replay_noop, unresolved)``.
    """
    if not rows:
        return 0, 0, 0
    existing = _existing_edge_identities(conn, rows)
    resolved: list[tuple[int, int, dict]] = []
    replay = 0
    unresolved = 0
    for row in rows:
        from_key = (row["from_norm"], row["from_type"])
        to_key = (row["to_norm"], row["to_type"])
        if not (from_key in entity_cache or from_key in planned_keys) or \
                not (to_key in entity_cache or to_key in planned_keys):
            unresolved += 1
            continue
        identity = (
            row["from_norm"], row["from_type"], row["relationship"],
            row["to_norm"], row["to_type"],
            row["provenance_type"], row["provenance_id"],
        )
        if identity in existing:
            replay += 1
            continue
        if dry_run:
            continue
        from_id = entity_cache.get(from_key)
        to_id = entity_cache.get(to_key)
        if from_id is None or to_id is None:
            unresolved += 1
            continue
        resolved.append((from_id, to_id, row))
    if resolved:
        _insert_edges(conn, resolved)
    return len(resolved), replay, unresolved


def _insert_edges(conn, resolved: list[tuple[int, int, dict]]) -> None:
    """Bulk-insert resolved relationship rows in one statement."""
    value_parts = []
    params: dict = {}
    for i, (from_id, to_id, row) in enumerate(resolved):
        value_parts.append(
            f"(:feid{i}, :teid{i}, :rel{i}, :pt{i}, :pid{i}, :sl{i}, :ek{i})"
        )
        params.update({
            f"feid{i}": from_id, f"teid{i}": to_id,
            f"rel{i}": row["relationship"],
            f"pt{i}": row["provenance_type"], f"pid{i}": row["provenance_id"],
            f"sl{i}": row["source_label"], f"ek{i}": row["edge_kind"],
        })
    conn.execute(text(f"""
        INSERT INTO entity_relationships
            (from_entity_id, to_entity_id, relationship,
             provenance_type, provenance_id, source_label,
             edge_kind, confidence, created_at)
        SELECT v.feid, v.teid, v.rel, v.pt, v.pid, v.sl,
               v.ek, 0.9, now()
        FROM (VALUES {', '.join(value_parts)})
        AS v(feid, teid, rel, pt, pid, sl, ek)
    """), params)


def pattern_cascade_accounting(total: dict, *, dry_run: bool):
    """Classify every pattern-cascade proposal exactly once for the receipt.

    ``would_insert`` is exactly the proposals that were (live) or would have been
    (dry) written, which keeps the live mutation equation
    ``would_insert + would_update == committed + rolled_back`` honest.
    """
    from scripts.entities.phase_receipt import RowAccounting

    entities_planned = int(total["entities"])
    mentions_planned = int(total["mentions_planned"])
    edges_planned = int(total["edges_planned"])
    entity_replay = int(total["entity_replay_collisions"])
    mention_replay = int(total["mention_replay_collisions"])
    mention_unresolved = int(total["mentions_unresolved_entity"])
    edge_replay = int(total["edge_replay_collisions"])
    edge_unresolved = int(total["edges_unresolved_endpoint"])

    mention_inserts = mentions_planned - mention_replay - mention_unresolved
    edge_inserts = edges_planned - edge_replay - edge_unresolved
    would_insert = entities_planned + mention_inserts + edge_inserts
    return RowAccounting(
        proposed=(entities_planned + entity_replay) + mentions_planned + edges_planned,
        would_insert=would_insert,
        would_update=0,
        replay_noop=entity_replay + mention_replay + edge_replay,
        unresolved=mention_unresolved + edge_unresolved,
        committed=0 if dry_run else would_insert,
    )
