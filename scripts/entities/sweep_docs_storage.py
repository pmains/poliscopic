"""sweep_docs_storage.py — classification-state loading and planned writes.

Owns every database interaction for the document sweep: loading the existing
entity and mention assertion snapshots, writing the planned entity and mention
rows, resolving exact-identity write conflicts, and persisting document
swept-state.

Row classification and receipt accounting live elsewhere.  This module loads
state and performs only the writes the plan has already approved.
"""

from __future__ import annotations

from .sweep_docs_planning import (
    DEFAULT_SOURCE_TYPE,
    EntityAssertion,
    MentionAssertion,
)

#: Source type recorded on every mention this sweep writes.  Defined once in the
#: planning module and reused here, so the value has a single authority.
SOURCE_TYPE = DEFAULT_SOURCE_TYPE
#: Rows per multi-row INSERT.
ENTITY_CHUNK = 100
MENTION_CHUNK = 50
#: ``IN`` list chunk when loading existing mention assertions.  Chunked so every
#: source id in the batch is covered, not just a truncated prefix.
SOURCE_ID_CHUNK = 100

__all__ = [
    "ENTITY_CHUNK",
    "MENTION_CHUNK",
    "SOURCE_ID_CHUNK",
    "SOURCE_TYPE",
    "_load_existing_entity_assertions",
    "_load_existing_mention_assertions",
    "_write_entities",
    "_write_mentions",
    "mark_docs_swept",
    "record_batch_failure",
]


# ── Failure accounting ──────────────────────────────────────────────────

def record_batch_failure(validator, dry_run: bool, reason: str) -> None:
    """Account honestly for a batch that raised, then fail the receipt.

    Rollback accounting applies **only when writes were actually attempted**.  In
    dry mode no statement is ever issued, so nothing can have been rolled back:
    the planned-but-uncommitted totals are the *expected* state of a preview, not
    lost mutations.  Recording them as ``rows_rolled_back`` would claim a
    mutation that never happened, and is exactly what the canonical dry-mode
    mutation check refuses.

    In live mode the planned-but-never-committed mutations are genuinely lost to
    the rolled-back transaction, so they are recorded as ``rows_rolled_back`` —
    which explains them without reducing the cumulative proposed totals.
    """
    if dry_run:
        validator.fail(reason)
        return
    mutations = (validator.receipt.rows_would_insert
                 + validator.receipt.rows_would_update)
    lost = max(0, mutations - validator.receipt.rows_committed)
    if lost > 0:
        validator.rollback(lost, reason)
    else:
        validator.fail(reason)


# ── Classification-state loading ────────────────────────────────────────

def _load_existing_entity_assertions(entity_cache: dict) -> tuple[list[EntityAssertion], int]:
    """Entity assertions already present, from the loaded entity cache.

    ``entity_cache`` maps ``"<normalized_name>|<entity_type>"`` to the canonical
    entity id.  It is loaded before the run and extended as batches insert
    entities, so it covers everything written earlier in the same run.

    Keys whose name or type half is blank (or whitespace-only) cannot form a
    valid assertion identity; they are counted and returned instead of raising,
    for the same reason as :func:`_load_existing_mention_assertions`.  The name
    half is stripped before the blank check so a whitespace-only name is caught
    here rather than inside ``EntityAssertion``.

    Returns ``(assertions, skipped_blank_identity_keys)``.
    """
    assertions: list[EntityAssertion] = []
    skipped = 0
    for cache_key, entity_id in entity_cache.items():
        normalized_name, separator, entity_type = str(cache_key).rpartition("|")
        normalized_name = normalized_name.strip()
        entity_type = entity_type.strip()
        if not separator or not normalized_name or not entity_type:
            skipped += 1
            continue
        assertions.append(
            EntityAssertion(normalized_name, entity_type, entity_id=int(entity_id))
        )
    return assertions, skipped


def _load_existing_mention_assertions(
    conn, all_candidates: list[dict],
) -> tuple[list[MentionAssertion], int]:
    """Mention assertions already present, with joined entity identity.

    The semantic mention identity is normalized entity name + entity type +
    source type + source id + role.  ``entity_mentions`` stores only
    ``entity_id`` and ``role_in_context``, so the join to ``entities`` is
    required to reconstruct that identity exactly.

    Some legacy ``entities`` rows carry a blank ``normalized_name``.  Such a row
    cannot be expressed as a mention assertion at all, and no *candidate* can
    ever produce a blank-name identity (``EntityAssertion`` refuses it), so the
    row is unreachable by the planner: skipping it cannot turn a real replay
    no-op into a spurious insert.  Blank-identity rows are counted and returned
    rather than crashing the batch, so the caller can report them honestly.

    Returns ``(assertions, skipped_blank_identity_rows)``.
    """
    source_ids = sorted({int(c["_source_id"]) for c in all_candidates})
    mentions: list[MentionAssertion] = []
    skipped = 0
    for start in range(0, len(source_ids), SOURCE_ID_CHUNK):
        chunk = source_ids[start:start + SOURCE_ID_CHUNK]
        placeholders = ", ".join(str(s) for s in chunk)
        rows = conn.execute(
            __import__("sqlalchemy").text(f"""
                SELECT e.normalized_name, e.entity_type, em.source_type,
                       em.source_id, em.role_in_context
                FROM entity_mentions em
                JOIN entities e ON e.id = em.entity_id
                WHERE em.source_type = '{SOURCE_TYPE}'
                  AND em.source_id IN ({placeholders})
            """)
        ).fetchall()
        for r in rows:
            if not str(r[0]).strip() or not str(r[1]).strip():
                skipped += 1
                continue
            mentions.append(MentionAssertion(
                entity=EntityAssertion(str(r[0]), str(r[1])),
                source_id=int(r[3]),
                role=str(r[4]),
                source_type=str(r[2]),
            ))
    return mentions, skipped


# ── Planned writes ──────────────────────────────────────────────────────

def _write_entities(conn, plan, entity_payloads, entity_cache, validator):
    """Insert the planned entity assertions.  Returns ``(inserted, plan)``.

    ``ON CONFLICT DO NOTHING`` detects a concurrent creator instead of
    overwriting it.  A planned insert that did not land is a real concurrency
    conflict, so the exact conflicting assertion is reclassified as a replay
    no-op in both the plan and the receipt.
    """
    inserted = 0
    assertions = list(plan.entity_inserts)
    for start in range(0, len(assertions), ENTITY_CHUNK):
        chunk = assertions[start:start + ENTITY_CHUNK]
        value_rows = []
        row_params = {}
        for j, assertion in enumerate(chunk):
            payload = entity_payloads[assertion.identity]
            suffix = f"_{j}"
            value_rows.append(
                f"(:et{suffix}, :name{suffix}, :nn{suffix}, False, "
                f"'unresolved', now(), now(), 1, now(), now())"
            )
            row_params.update({
                f"et{suffix}": assertion.entity_type,
                f"name{suffix}": payload.name or assertion.normalized_name,
                f"nn{suffix}": assertion.normalized_name,
            })
        sql = (
            "INSERT INTO entities "
            "(entity_type, name, normalized_name, is_government, "
            "resolution_status, first_seen_at, last_seen_at, mention_count, "
            "created_at, updated_at) "
            f"VALUES {', '.join(value_rows)} "
            "ON CONFLICT (normalized_name, entity_type) DO NOTHING "
            "RETURNING id, normalized_name, entity_type"
        )
        rows = conn.execute(
            __import__("sqlalchemy").text(sql), row_params
        ).fetchall()
        landed = set()
        for r in rows:
            eid, nn, etype_val = int(r[0]), str(r[1]), str(r[2])
            entity_cache[f"{nn}|{etype_val}"] = eid
            landed.add((nn, etype_val))
            inserted += 1
        for assertion in chunk:
            if assertion.identity in landed:
                continue
            plan = plan.reclassify_as_replay(assertion)
            if validator is not None:
                validator.reclassify_assertion(
                    assertion, reason="entity existed at write time",
                )
            existing = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT id FROM entities WHERE normalized_name = :nn "
                    "AND entity_type = :et"
                ),
                {"nn": assertion.normalized_name, "et": assertion.entity_type},
            ).fetchone()
            if existing is None:
                raise RuntimeError(
                    f"sweep_docs: entity {assertion.identity!r} neither inserted "
                    "nor found after a write conflict"
                )
            entity_cache[
                f"{assertion.normalized_name}|{assertion.entity_type}"
            ] = int(existing[0])
    return inserted, plan


def _write_mentions(conn, plan, mention_payloads, entity_cache) -> int:
    """Insert the planned mention assertions.  Returns the number written.

    Entity identity is resolved from ``entity_cache``, which by now holds the
    canonical id returned by the entity insert earlier in this batch.  The
    assertion identity itself is unchanged: only the resolved id is attached.
    """
    rows_to_write: list[dict] = []
    for assertion in plan.mention_inserts:
        cache_key = (f"{assertion.entity.normalized_name}|"
                     f"{assertion.entity.entity_type}")
        entity_id = entity_cache.get(cache_key)
        if entity_id is None:
            raise RuntimeError(
                f"sweep_docs: planned mention {assertion.identity!r} has no "
                "resolved entity id; refusing to write an unresolvable row"
            )
        payload = mention_payloads[assertion.identity]
        text = payload.name or assertion.entity.normalized_name
        rows_to_write.append({
            "eid": entity_id, "sid": assertion.source_id,
            "mt": text[:500], "cs": text[:300],
            "conf": payload.confidence, "role": assertion.role,
        })

    for start in range(0, len(rows_to_write), MENTION_CHUNK):
        chunk = rows_to_write[start:start + MENTION_CHUNK]
        value_rows = []
        row_params = {}
        for j, p in enumerate(chunk):
            suffix = f"_{j}"
            value_rows.append(
                f"(:eid{suffix}, '{SOURCE_TYPE}', :sid{suffix}, "
                f":mt{suffix}, :cs{suffix}, :conf{suffix}, "
                f"'sweep_docs', :role{suffix}, now())"
            )
            row_params.update({
                f"eid{suffix}": p["eid"],
                f"sid{suffix}": p["sid"],
                f"mt{suffix}": p["mt"],
                f"cs{suffix}": p["cs"],
                f"conf{suffix}": p["conf"],
                f"role{suffix}": p["role"],
            })
        sql = (
            "INSERT INTO entity_mentions "
            "(entity_id, source_type, source_id, mention_text, "
            "context_snippet, confidence, extracted_by, "
            "role_in_context, created_at) "
            f"VALUES {', '.join(value_rows)}"
        )
        conn.execute(__import__("sqlalchemy").text(sql), row_params)
    return len(rows_to_write)


def mark_docs_swept(conn, rows) -> None:
    """Persist swept-state for every document fetched in this batch."""
    batch_ids = [int(r[0]) for r in rows]
    for chunk_start in range(0, len(batch_ids), 100):
        chunk = batch_ids[chunk_start:chunk_start + 100]
        id_list = ", ".join(str(i) for i in chunk)
        conn.execute(
            __import__("sqlalchemy").text(
                f"UPDATE supporting_documents SET swept_at = now() "
                f"WHERE id IN ({id_list})"
            )
        )
