"""Persistence for the entity resolver: merges and composite splits.

Extracted from :mod:`scripts.entities.resolver` so the orchestration module stays
focused on building proposals, classifying them, and aggregating accounting.

Every function here is *pure persistence*: it applies exactly the changes the
caller already classified.  It does no proposal classification of its own, and it
returns the counts the caller needs to reconcile committed rows against the
proposals it decided on.  Nothing here writes unless ``dry_run`` is false.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from scripts.entities.resolver_proposals import (
    SPLIT_ORG_ROLE,
    organization_mention_bundle,
)

__all__ = ["apply_composite_splits", "merge_entities"]

_BATCH_SIZE = 50


def merge_entities(conn, victim_id: int, survivor_id: int,
                   method: str, confidence: float) -> None:
    """Re-point all references from victim to survivor, then mark victim."""
    now = datetime.now(timezone.utc)

    # Re-point entity_mentions
    conn.execute(
        text("""
            UPDATE entity_mentions
            SET entity_id = :survivor
            WHERE entity_id = :victim
              AND NOT EXISTS (
                  SELECT 1 FROM entity_mentions em2
                  WHERE em2.entity_id = :survivor
                    AND em2.source_type = entity_mentions.source_type
                    AND em2.source_id = entity_mentions.source_id
                    AND em2.role_in_context IS NOT DISTINCT FROM entity_mentions.role_in_context
              )
        """),
        {"survivor": survivor_id, "victim": victim_id},
    )

    # Re-point entity_relationships (from_entity_id)
    conn.execute(
        text("""
            UPDATE entity_relationships
            SET from_entity_id = :survivor
            WHERE from_entity_id = :victim
              AND NOT EXISTS (
                  SELECT 1 FROM entity_relationships er2
                  WHERE er2.from_entity_id = :survivor
                    AND er2.relationship = entity_relationships.relationship
                    AND er2.to_entity_id = entity_relationships.to_entity_id
                    AND er2.provenance_type IS NOT DISTINCT FROM entity_relationships.provenance_type
                    AND er2.provenance_id IS NOT DISTINCT FROM entity_relationships.provenance_id
              )
        """),
        {"survivor": survivor_id, "victim": victim_id},
    )

    # Re-point entity_relationships (to_entity_id)
    conn.execute(
        text("""
            UPDATE entity_relationships
            SET to_entity_id = :survivor
            WHERE to_entity_id = :victim
              AND NOT EXISTS (
                  SELECT 1 FROM entity_relationships er2
                  WHERE er2.from_entity_id = entity_relationships.from_entity_id
                    AND er2.relationship = entity_relationships.relationship
                    AND er2.to_entity_id = :survivor
                    AND er2.provenance_type IS NOT DISTINCT FROM entity_relationships.provenance_type
                    AND er2.provenance_id IS NOT DISTINCT FROM entity_relationships.provenance_id
              )
        """),
        {"survivor": survivor_id, "victim": victim_id},
    )

    # Accumulate mention_count on survivor
    victim_cnt = conn.execute(
        text("SELECT mention_count FROM entities WHERE id = :vid"),
        {"vid": victim_id},
    ).scalar() or 0
    conn.execute(
        text("""
            UPDATE entities
            SET mention_count = mention_count + :vcnt,
                last_seen_at = GREATEST(last_seen_at, (
                    SELECT last_seen_at FROM entities WHERE id = :vid
                )),
                updated_at = :now
            WHERE id = :sid
        """),
        {"vcnt": victim_cnt, "vid": victim_id, "sid": survivor_id, "now": now},
    )

    # Mark victim as merged
    conn.execute(
        text("""
            UPDATE entities
            SET canonical_entity_id = :sid,
                resolution_status = 'merged',
                resolution_confidence = :conf,
                resolution_method = :method,
                resolved_at = :now,
                updated_at = :now
            WHERE id = :vid
        """),
        {"sid": survivor_id, "vid": victim_id, "conf": confidence,
         "method": method, "now": now},
    )

    # Mark survivor as resolved
    conn.execute(
        text("""
            UPDATE entities
            SET resolution_status = 'canonical',
                resolution_method = :method,
                resolved_at = :now,
                updated_at = :now
            WHERE id = :sid
              AND resolution_status = 'unresolved'
        """),
        {"sid": survivor_id, "method": method, "now": now},
    )


def _insert_new_entities(conn, new_entities: list, entity_cache: dict, now) -> int:
    """Bulk-insert entities created by composite splits.  Returns rows created."""
    created = 0
    for batch_start in range(0, len(new_entities), _BATCH_SIZE):
        batch = new_entities[batch_start:batch_start + _BATCH_SIZE]
        val_parts = []
        params: dict = {}
        for bi, (etype, ename, enorm) in enumerate(batch):
            i = batch_start + bi
            val_parts.append(f"(:et{i}, :name{i}, :nn{i})")
            params[f"et{i}"] = etype
            params[f"name{i}"] = ename
            params[f"nn{i}"] = enorm
        val_clause = ", ".join(val_parts)
        result_rows = conn.execute(
            text(f"""
                INSERT INTO entities
                    (entity_type, name, normalized_name, is_government,
                     first_seen_at, last_seen_at, mention_count,
                     resolution_status, created_at, updated_at)
                SELECT v.et, v.name, v.nn, False,
                       :now, :now, 1, 'canonical', :now, :now
                FROM (VALUES {val_clause}) AS v(et, name, nn)
                RETURNING normalized_name, entity_type, id
            """),
            {**params, "now": now},
        ).fetchall()
        for row in result_rows:
            entity_cache[(str(row[0]), str(row[1]))] = int(row[2])
            created += 1
    return created


def _execute_mention_updates(conn, mention_updates: list) -> None:
    for batch_start in range(0, len(mention_updates), _BATCH_SIZE):
        batch = mention_updates[batch_start:batch_start + _BATCH_SIZE]
        val_parts = []
        params: dict = {}
        for bi, row in enumerate(batch):
            i = batch_start + bi
            val_parts.append(f"(:pid{i}, :cid{i})")
            params[f"pid{i}"] = row["pid"]
            params[f"cid{i}"] = row["cid"]
        val_clause = ", ".join(val_parts)
        conn.execute(
            text(f"""
                UPDATE entity_mentions em
                SET entity_id = v.pid
                FROM (VALUES {val_clause}) AS v(pid, cid)
                WHERE em.entity_id = v.cid
                  AND NOT EXISTS (
                      SELECT 1 FROM entity_mentions em2
                      WHERE em2.entity_id = v.pid
                        AND em2.source_type = em.source_type
                        AND em2.source_id = em.source_id
                        AND em2.role_in_context IS NOT DISTINCT FROM em.role_in_context
                  )
            """), params,
        )


_COMPOSITE_SOURCE_MENTIONS = text("""
    SELECT source_type, source_id, mention_text
    FROM entity_mentions
    WHERE entity_id = :cid AND source_type = 'agenda_item'
""")

_ORG_MENTION_EXISTS = text("""
    SELECT 1 FROM entity_mentions
    WHERE entity_id = :oid AND source_type = :st AND source_id = :sid
""")

_ORG_MENTION_INSERT = text("""
    INSERT INTO entity_mentions
        (entity_id, source_type, source_id, mention_text,
         context_snippet, confidence, extracted_by, role_in_context, created_at)
    VALUES (:oid, :st, :sid, :oname, :cs, 70, 'resolver', :role, :now)
""")


def write_split_organization_mentions(conn, split_orgs: list, *,
                                      validator, dry_run: bool,
                                      model_version: str, now) -> dict:
    """Create the canonical organisation mention for each split source.

    The organisation was merely *named* in the same occurrence, so the only
    truthful role is ``mentioned``.  A mention is written only when the
    organisation has no mention for that source at all — an existing stronger
    role is never replaced by a weaker one — and every emitted mention bundle is
    validated through the canonical emission boundary, carrying the exact
    evidence identity of the occurrence that was read, before anything is
    written.  A bundle that cannot be validated is retained as ``unresolved``
    and nothing is written for it.
    """
    would_insert = 0
    replay_noop = 0
    unresolved = 0

    if validator is None:
        # Fail closed: without the canonical boundary nothing may be emitted.
        return {"would_insert": 0, "replay_noop": 0, "unresolved": len(split_orgs)}

    for row in split_orgs:
        org_id = row.get("oid")
        sources = conn.execute(
            _COMPOSITE_SOURCE_MENTIONS, {"cid": row["cid"]}
        ).fetchall()
        for source_type, source_id, source_text in sources:
            if org_id is not None and conn.execute(_ORG_MENTION_EXISTS, {
                "oid": org_id, "st": source_type, "sid": source_id,
            }).fetchone():
                # Already present for this source: nothing to write, and an
                # existing role is never replaced by a weaker one.
                replay_noop += 1
                continue

            bundle = organization_mention_bundle(
                source_type=str(source_type),
                source_id=source_id,
                source_text=str(source_text or ""),
                org_name=row["oname"],
                model_version=model_version,
            )
            try:
                validator.validate_bundle(
                    bundle, source=f"{source_type}:{source_id}"
                )
            except Exception:
                unresolved += 1
                continue

            if dry_run:
                would_insert += 1
                continue
            conn.execute(_ORG_MENTION_INSERT, {
                "oid": org_id, "st": source_type, "sid": source_id,
                "oname": row["oname"][:500],
                "cs": str(source_text or "")[:300],
                "role": SPLIT_ORG_ROLE, "now": now,
            })
            would_insert += 1

    return {"would_insert": would_insert, "replay_noop": replay_noop,
            "unresolved": unresolved}


def _execute_entity_merges(conn, entity_merges: list, now) -> None:
    if not entity_merges:
        return
    val_parts = []
    params: dict = {}
    for i, row in enumerate(entity_merges):
        val_parts.append(f"(:pid{i}, :cid{i})")
        params[f"pid{i}"] = row["pid"]
        params[f"cid{i}"] = row["cid"]
    val_clause = ", ".join(val_parts)
    conn.execute(
        text(f"""
            UPDATE entities
            SET canonical_entity_id = v.pid,
                resolution_status = 'merged',
                resolution_confidence = 0.95,
                resolution_method = 'composite_split',
                resolved_at = :now,
                updated_at = :now
            FROM (VALUES {val_clause}) AS v(pid, cid)
            WHERE entities.id = v.cid
        """),
        {**params, "now": now},
    )


def apply_composite_splits(conn, ops: list, entity_cache: dict, *,
                           dry_run: bool, validator=None,
                           model_version: str) -> dict:
    """Materialise composite splits and classify each split exactly once.

    A split whose person/organisation half already existed only re-points
    existing rows, so its dominant effect is an **update**.  A split that
    materialised at least one new canonical entity is an **insert**.  A split
    whose halves cannot be identified is **unresolved** and writes nothing.

    Returns ``created_entities``, ``merged_entities``, ``would_insert``,
    ``would_update`` and ``unresolved``.  In dry mode nothing is written.
    """
    now = datetime.now(timezone.utc)

    # Phase B: decide which entity keys are missing, and which ops materialise.
    new_entities: list = []
    creates: list[bool] = []
    for op in ops:
        pkey = (op.person_norm, "person")
        okey = (op.org_norm, "organization")
        created = False
        if pkey not in entity_cache:
            new_entities.append(("person", op.person_name, op.person_norm))
            # Placeholder in cache so we don't create duplicate
            entity_cache[pkey] = -(len(new_entities))
            created = True
        if okey not in entity_cache:
            # Also check if org exists under any type
            found = False
            for cached_key in entity_cache:
                if cached_key[0] == op.org_norm:
                    found = True
                    break
            if not found:
                new_entities.append(("organization", op.org_name, op.org_norm))
                entity_cache[okey] = -(len(new_entities))
                created = True
        creates.append(created)

    split_orgs = [
        {
            "oid": entity_cache.get((op.org_norm, "organization")),
            "oname": op.org_name,
            "cid": op.entity_id,
        }
        for op in ops
    ]

    if dry_run:
        # Identical classification to the live path: the emitted mention set is
        # the same, only the writes are skipped.  Bundles are still validated.
        emission = write_split_organization_mentions(
            conn, split_orgs, validator=validator, dry_run=True,
            model_version=model_version, now=now,
        )
        return {
            "created_entities": 0,
            "merged_entities": 0,
            "would_insert": sum(1 for flag in creates if flag),
            "would_update": sum(1 for flag in creates if not flag),
            "unresolved": emission["unresolved"],
            "organization_mentions_insert": emission["would_insert"],
            "organization_mentions_replay": emission["replay_noop"],
        }

    created_entities = _insert_new_entities(conn, new_entities, entity_cache, now)

    # Phase C: build the per-op row sets, classifying each op exactly once.
    mention_updates: list = []
    entity_merges: list = []
    unresolved = 0
    would_insert = 0
    would_update = 0

    for index, op in enumerate(ops):
        pkey = (op.person_norm, "person")
        person_id = entity_cache.get(pkey)
        # Try any type for org
        okey = (op.org_norm, "organization")
        org_id = entity_cache.get(okey)
        if not org_id:
            # Check other types
            for ck, cid in entity_cache.items():
                if ck[0] == op.org_norm:
                    org_id = cid
                    okey = ck
                    break

        if not person_id or not org_id:
            unresolved += 1
            continue

        # Classified exactly once from the same decision the write path takes.
        if creates[index]:
            would_insert += 1
        else:
            would_update += 1

        # The composite's original mentions move to the person entity with
        # their contextual roles untouched: only the resolved target changes.
        mention_updates.append({"pid": person_id, "cid": op.entity_id})
        entity_merges.append({"pid": person_id, "cid": op.entity_id})

    # No person-to-organisation relationship is written here.  A comma is
    # punctuation, not affiliation evidence, and co-occurrence in one
    # sentence is not a relationship.  AFFILIATED_WITH is reserved for
    # explicit with/of affiliation language and is never inferred here.
    _execute_mention_updates(conn, mention_updates)
    emission = write_split_organization_mentions(
        conn, split_orgs, validator=validator, dry_run=False,
        model_version=model_version, now=now,
    )
    _execute_entity_merges(conn, entity_merges, now)

    return {
        "created_entities": created_entities,
        "merged_entities": len(entity_merges),
        "would_insert": would_insert,
        "would_update": would_update,
        "unresolved": unresolved + emission["unresolved"],
        "organization_mentions_insert": emission["would_insert"],
        "organization_mentions_replay": emission["replay_noop"],
    }
