"""PART_OF container materialization for the knowledge graph (Brief 015, Step 2).

Creates jurisdiction + body container entity rows from the structured
registries (jurisdictions, public_bodies) and seeds composition edges:

    body        PART_OF  jurisdiction   (exhaustive — every registry body)
    meeting     PART_OF  body           (only for meeting entities that ALREADY
                                        exist; no mass materialization of ~15.5k
                                        meetings yet)

Design for later extension: meeting entities are named
"{body_code} Meeting {meeting_id}" with normalized_name "{body_code}/{meeting_id}"
(graph_builder convention), and body entities here get normalized_name =
body_code. Full meeting materialization later is therefore a simple join on
that key — no redesign.

Idempotent: entity rows are looked up by (entity_type, normalized_name) and
reused; edges use INSERT ... ON CONFLICT DO NOTHING on the existing unique
index (from_entity_id, relationship, to_entity_id, provenance_type,
provenance_id). Safe to run any number of times.

Run (dev):
    PYTHONPATH=scripts python3 scripts/entities/partof_seed.py
"""

import logging
import re

from sqlalchemy import text

from db import get_engine

log = logging.getLogger("partof_seed")

MEETING_NAME_RE = re.compile(r"^(?P<body_code>.+?) Meeting (?P<meeting_id>\S+)$")

# Provenance tags (idempotency keys per registry row)
PROV_BODY = "public_bodies"      # provenance_id = public_bodies.id
PROV_MEETING = "meetings"        # provenance_id = meetings.id (meeting entity's source row)


def _get_or_create_entity(c, entity_type, name, normalized_name, jurisdiction_id,
                          is_government=True):
    """Return (entity_id, 'created'|'reused'). Idempotent on (type, normalized)."""
    row = c.execute(
        text("SELECT id FROM entities WHERE entity_type = :t AND normalized_name = :n"),
        {"t": entity_type, "n": normalized_name},
    ).fetchone()
    if row:
        return int(row[0]), "reused"
    new_id = c.execute(
        text("""
            INSERT INTO entities
                (entity_type, name, normalized_name, jurisdiction_id, is_government,
                 resolution_status, mention_count,
                 first_seen_at, last_seen_at, created_at, updated_at)
            VALUES
                (:t, :name, :n, :jid, :gov,
                 'unresolved', 0,
                 now(), now(), now(), now())
            RETURNING id
        """),
        {"t": entity_type, "name": name, "n": normalized_name,
         "jid": jurisdiction_id, "gov": is_government},
    ).scalar()
    return int(new_id), "created"


def _insert_partof(c, from_id, to_id, relationship, prov_type, prov_id, source_label):
    """Idempotent PART_OF edge insert. Returns 'inserted' | 'exists'."""
    res = c.execute(
        text("""
            INSERT INTO entity_relationships
                (from_entity_id, relationship, to_entity_id,
                 provenance_type, provenance_id, source_label,
                 edge_kind, confidence, created_at, updated_at)
            VALUES
                (:f, :rel, :t, :pt, :pid, :sl, 'attributional', 1.0, now(), now())
            ON CONFLICT (from_entity_id, relationship, to_entity_id,
                         provenance_type, provenance_id) DO NOTHING
            RETURNING id
        """),
        {"f": from_id, "rel": relationship, "t": to_id,
         "pt": prov_type, "pid": prov_id, "sl": source_label},
    ).fetchone()
    return "inserted" if res else "exists"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    engine = get_engine()

    stats = {
        "jurisdiction_created": 0, "jurisdiction_reused": 0,
        "body_created": 0, "body_reused": 0,
        "body_partof_inserted": 0, "body_partof_exists": 0,
        "meeting_partof_inserted": 0, "meeting_partof_exists": 0,
        "meetings_unmapped_body": 0, "meetings_orphan_row": 0,
    }
    unmapped_bodies = []      # meeting prefixes with no body entity
    orphan_meetings = []      # meeting entities whose meetings row is missing

    with engine.begin() as c:
        # ── 1. Jurisdiction container entities (from jurisdictions registry) ──
        jurs = c.execute(text("SELECT id, name, slug FROM jurisdictions ORDER BY id")).fetchall()
        jur_entity_ids = {}
        for j in jurs:
            eid, kind = _get_or_create_entity(
                c, "jurisdiction", j.name, j.slug, j.id, is_government=True)
            jur_entity_ids[j.id] = eid
            stats[f"jurisdiction_{kind}"] += 1
        log.info("Jurisdiction entities: %d created, %d reused",
                 stats["jurisdiction_created"], stats["jurisdiction_reused"])

        # ── 2. Body container entities (from public_bodies registry) ──
        bodies = c.execute(text("""
            SELECT id, body_code, name, jurisdiction_id
            FROM public_bodies ORDER BY id
        """)).fetchall()
        body_entity_ids = {}   # body_code -> entity id
        for b in bodies:
            eid, kind = _get_or_create_entity(
                c, "body", b.name, b.body_code, b.jurisdiction_id, is_government=True)
            body_entity_ids[b.body_code] = eid
            stats[f"body_{kind}"] += 1
        log.info("Body entities: %d created, %d reused",
                 stats["body_created"], stats["body_reused"])

        # ── 3. body PART_OF jurisdiction (exhaustive) ──
        for b in bodies:
            jur_eid = jur_entity_ids.get(b.jurisdiction_id)
            if jur_eid is None:
                log.warning("  body %s (%s): jurisdiction %s has no jurisdiction entity",
                            b.body_code, b.name[:30], b.jurisdiction_id)
                continue
            kind = _insert_partof(c, body_entity_ids[b.body_code], jur_eid,
                                  "PART_OF", PROV_BODY, b.id,
                                  f"body:{b.body_code}")
            stats[f"body_partof_{kind}"] += 1
        log.info("body PART_OF jurisdiction: %d inserted, %d already existed",
                 stats["body_partof_inserted"], stats["body_partof_exists"])

        # ── 4. meeting PART_OF body (existing meeting entities only) ──
        meetings = c.execute(text("SELECT id, name FROM entities WHERE entity_type='meeting' ORDER BY id")).fetchall()
        for m in meetings:
            mt = MEETING_NAME_RE.match(m.name or "")
            if not mt:
                log.warning("  meeting entity #%s unparseable name: %r", m.id, m.name)
                continue
            body_code = mt.group("body_code")
            meeting_id = mt.group("meeting_id")
            body_eid = body_entity_ids.get(body_code)
            if body_eid is None:
                stats["meetings_unmapped_body"] += 1
                unmapped_bodies.append((m.id, body_code, meeting_id))
                continue
            # provenance_id = the meetings registry row for this meeting
            prow = c.execute(
                text("SELECT id FROM meetings WHERE body = :b AND meeting_id = :mid"),
                {"b": body_code, "mid": meeting_id},
            ).fetchone()
            if prow is None:
                stats["meetings_orphan_row"] += 1
                orphan_meetings.append((m.id, body_code, meeting_id))
                continue
            kind = _insert_partof(c, m.id, body_eid, "PART_OF", PROV_MEETING,
                                  int(prow[0]), f"meeting:{body_code}/{meeting_id}")
            stats[f"meeting_partof_{kind}"] += 1
        log.info("meeting PART_OF body: %d inserted, %d already existed",
                 stats["meeting_partof_inserted"], stats["meeting_partof_exists"])

        if unmapped_bodies:
            log.warning("  %d meeting entity(ies) with no matching body in registry (first 10):",
                        stats["meetings_unmapped_body"])
            for m_id, bc, mid in unmapped_bodies[:10]:
                log.warning("    meeting entity #%s: prefix %r (meeting %s)", m_id, bc, mid)
        if orphan_meetings:
            log.warning("  %d meeting entity(ies) with no meetings row (first 10):",
                        stats["meetings_orphan_row"])
            for m_id, bc, mid in orphan_meetings[:10]:
                log.warning("    meeting entity #%s: %s/%s", m_id, bc, mid)

    # ── Report summary ──
    print("\n=== Step 2 PART_OF materialization report ===")
    print(f"jurisdiction entities: {stats['jurisdiction_created']} created, "
          f"{stats['jurisdiction_reused']} reused")
    print(f"body entities:         {stats['body_created']} created, "
          f"{stats['body_reused']} reused")
    print(f"body PART_OF jurisdiction: {stats['body_partof_inserted']} inserted, "
          f"{stats['body_partof_exists']} pre-existing")
    print(f"meeting PART_OF body:   {stats['meeting_partof_inserted']} inserted, "
          f"{stats['meeting_partof_exists']} pre-existing")
    print(f"meeting entities w/o body mapping: {stats['meetings_unmapped_body']}")
    print(f"meeting entities w/o meetings row: {stats['meetings_orphan_row']}")

    # ── Coverage verification queries ──
    with engine.connect() as c:
        total_bodies = c.execute(text("SELECT count(*) FROM public_bodies")).scalar()
        total_meeting_entities = c.execute(text(
            "SELECT count(*) FROM entities WHERE entity_type='meeting'")).scalar()
        bodies_covered = c.execute(text("""
            SELECT count(DISTINCT source_label) FROM entity_relationships
            WHERE relationship='PART_OF' AND provenance_type='public_bodies'
        """)).scalar()
        meetings_covered = c.execute(text("""
            SELECT count(DISTINCT source_label) FROM entity_relationships
            WHERE relationship='PART_OF' AND provenance_type='meetings'
        """)).scalar()
    print(f"\ncoverage bodies→jurisdiction: {bodies_covered}/{total_bodies} registry bodies")
    print(f"coverage meeting entities→body: {meetings_covered}/{total_meeting_entities} existing meeting entities")


if __name__ == "__main__":
    main()
