"""Entity routes — search and detail pages for the entity graph."""

from __future__ import annotations

import logging
from sqlalchemy import text
from flask import Blueprint, render_template, request
from db.core import get_engine

log = logging.getLogger(__name__)
entities_bp = Blueprint("entities", __name__)


@entities_bp.route("/entities")
def entity_search():
    """Search entities by name, type, and jurisdiction."""
    q = request.args.get("q", "").strip()
    etype = request.args.get("type", "").strip()
    sort = request.args.get("sort", "mentions").strip()
    page = int(request.args.get("page", "1"))
    per_page = 50

    engine = get_engine()
    results = []
    total = 0

    # Don't surface parcels/addresses in entity search (kept for geospatial use)
    if q or etype:
        params = {}
        where_clauses = ["e.entity_type NOT IN ('parcel', 'address')"]
        if q:
            where_clauses.append("LOWER(e.normalized_name) LIKE LOWER(:q)")
            params["q"] = f"%{q}%"
        if etype and etype != "all":
            where_clauses.append("e.entity_type = :etype")
            params["etype"] = etype
        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

        # Sorting
        sort_clause = "e.mention_count DESC, e.name"
        if sort == "name":
            sort_clause = "e.normalized_name ASC"
        elif sort == "recent":
            sort_clause = "mrange.last_meeting DESC NULLS LAST, e.mention_count DESC"
        elif sort == "oldest":
            sort_clause = "mrange.first_meeting ASC NULLS LAST, e.mention_count DESC"

        with engine.connect() as c:
            total = c.execute(
                text(f"SELECT COUNT(*) FROM entities e WHERE {where_sql}"),
                params,
            ).scalar()

            rows = c.execute(
                text(
                    f"SELECT e.id, e.entity_type, e.name, e.normalized_name, "
                    f"e.mention_count, "
                    f"mrange.first_meeting, mrange.last_meeting, "
                    f"j.name AS jurisdiction_name "
                    f"FROM entities e "
                    f"LEFT JOIN jurisdictions j ON j.id = e.jurisdiction_id "
                    f"LEFT JOIN ("
                    f"  SELECT em.entity_id, "
                    f"         MIN(m.meeting_date) AS first_meeting, "
                    f"         MAX(m.meeting_date) AS last_meeting "
                    f"  FROM entity_mentions em "
                    f"  JOIN agenda_items ai ON ai.id = em.source_id AND em.source_type = 'agenda_item' "
                    f"  JOIN meetings m ON m.id = ai.meeting_db_id "
                    f"  GROUP BY em.entity_id"
                    f") mrange ON mrange.entity_id = e.id "
                    f"WHERE {where_sql} "
                    f"ORDER BY {sort_clause} "
                    f"LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": per_page, "offset": (page - 1) * per_page},
            ).fetchall()

    # Entity type counts for filter sidebar (exclude parcels/addresses — kept for geospatial use)
    with engine.connect() as c:
        type_counts = c.execute(
            text("""
                SELECT entity_type, COUNT(*)
                FROM entities
                WHERE entity_type NOT IN ('parcel', 'address')
                GROUP BY entity_type
                ORDER BY COUNT(*) DESC
            """)
        ).fetchall()

        # Top 5 entities per type by mention count (for landing page cards)
        top_per_type = {}
        # Only show cards for types with 3+ entities (skip test, single-advocacy, etc.)
        card_types = [t for t in type_counts if t[1] >= 3]
        for et, _ in card_types:
            top_rows = c.execute(
                text("""
                    SELECT e.id, e.name, e.mention_count
                    FROM entities e
                    WHERE e.entity_type = :et
                    ORDER BY e.mention_count DESC
                    LIMIT 5
                """),
                {"et": et},
            ).fetchall()
            top_per_type[et] = top_rows

    return render_template(
        "entity_search.html",
        results=results if not (q or etype) else rows,
        query=q,
        current_type=etype,
        current_sort=sort,
        type_counts=type_counts,
        top_per_type=top_per_type,
        card_types=card_types,
        total=total,
        page=page,
        per_page=per_page,
    )


@entities_bp.route("/entities/<int:entity_id>")
def entity_detail(entity_id: int):
    """Show an entity's profile: timeline, relationships, documents."""
    from flask import request
    active_jurisdiction = request.args.get("jurisdiction", "").strip()
    engine = get_engine()

    with engine.connect() as c:
        # Entity
        entity = c.execute(
            text("""
                SELECT e.id, e.entity_type, e.name, e.normalized_name,
                       e.is_government, e.mention_count,
                       e.first_seen_at, e.last_seen_at,
                       j.name AS jurisdiction_name
                FROM entities e
                LEFT JOIN jurisdictions j ON j.id = e.jurisdiction_id
                WHERE e.id = :eid
            """),
            {"eid": entity_id},
        ).fetchone()

        if not entity:
            return render_template("404.html"), 404

        # Withdrawal count
        withdrawal_count = c.execute(
            text("""
                SELECT COUNT(*) FROM entity_mentions
                WHERE entity_id = :eid AND is_withdrawn = true
            """),
            {"eid": entity_id},
        ).scalar()

        # Actual date range of mentions (meeting dates, not entity record dates)
        mention_dates = c.execute(
            text("""
                SELECT MIN(m.meeting_date) AS first_seen,
                       MAX(m.meeting_date) AS last_seen
                FROM entity_mentions em
                JOIN agenda_items ai ON ai.id = em.source_id AND em.source_type = 'agenda_item'
                JOIN meetings m ON m.id = ai.meeting_db_id
                WHERE em.entity_id = :eid
            """),
            {"eid": entity_id},
        ).fetchone()

        # Jurisdictions where entity has appeared
        jurisdictions = c.execute(
            text("""
                SELECT DISTINCT j.name, j.slug
                FROM entity_mentions em
                JOIN agenda_items ai ON ai.id = em.source_id AND em.source_type = 'agenda_item'
                JOIN meetings m ON m.id = ai.meeting_db_id
                JOIN jurisdictions j ON j.id = m.jurisdiction_id
                WHERE em.entity_id = :eid
                ORDER BY j.name
            """),
            {"eid": entity_id},
        ).fetchall()

        # Attorneys/representatives (via entity_relationships)
        representatives = c.execute(
            text("""
                SELECT e_to.id, e_to.name, e_to.entity_type
                FROM entity_relationships er
                JOIN entities e_to ON e_to.id = er.to_entity_id
                WHERE er.from_entity_id = :eid AND er.relationship = 'represents'
                UNION
                SELECT e_from.id, e_from.name, e_from.entity_type
                FROM entity_relationships er
                JOIN entities e_from ON e_from.id = er.from_entity_id
                WHERE er.to_entity_id = :eid AND er.relationship = 'represents'
                ORDER BY name
            """),
            {"eid": entity_id},
        ).fetchall()

        # Mention timeline (meetings → agenda items)
        jur_filter = ""
        params = {"eid": entity_id}
        if active_jurisdiction:
            jur_filter = " AND j.name = :jur"
            params["jur"] = active_jurisdiction

        timeline = c.execute(
            text(f"""
                SELECT meeting_db_id, meeting_date, meeting_id, body,
                       body_name, jurisdiction_name, agenda_item_title,
                       agenda_item_number, role_in_context, mention_text,
                       confidence, is_withdrawn, flag_reason, meeting_type
                FROM (
                    SELECT m.id AS meeting_db_id,
                           m.meeting_date,
                           m.meeting_id,
                           m.body,
                           pb.name AS body_name,
                           j.name AS jurisdiction_name,
                           ai.agenda_item_title,
                           ai.agenda_item_number,
                           em.role_in_context,
                           em.mention_text,
                           em.confidence,
                           em.is_withdrawn,
                           em.flag_reason,
                           m.meeting_type,
                           ROW_NUMBER() OVER (
                               PARTITION BY m.id
                               ORDER BY ai.id, em.id
                           ) AS row_rank
                    FROM entity_mentions em
                    JOIN agenda_items ai ON ai.id = em.source_id AND em.source_type = 'agenda_item'
                    JOIN meetings m ON m.id = ai.meeting_db_id
                    JOIN public_bodies pb ON pb.body_code = m.body
                    JOIN jurisdictions j ON j.id = m.jurisdiction_id
                    WHERE em.entity_id = :eid{jur_filter}
                ) ranked
                WHERE row_rank = 1
                ORDER BY meeting_date DESC, meeting_db_id DESC
                LIMIT 100
            """),
            params,
        ).fetchall()

        # ── Actual distinct meeting count ──
        meeting_count = c.execute(
            text("""
                SELECT COUNT(DISTINCT m.id)
                FROM entity_mentions em
                JOIN agenda_items ai ON ai.id = em.source_id AND em.source_type = 'agenda_item'
                JOIN meetings m ON m.id = ai.meeting_db_id
                JOIN jurisdictions j ON j.id = m.jurisdiction_id
                WHERE em.entity_id = :eid""" + (" AND j.name = :jur" if active_jurisdiction else "")),
            params,
        ).scalar() or 0

        # ── Raw mention_details grouped by meeting (for expandable timeline) ──
        mention_details_raw = c.execute(
            text(f"""
                SELECT em.id, em.mention_text, em.role_in_context, em.confidence,
                       em.is_withdrawn, em.flag_reason,
                       em.context_snippet,
                       ai.agenda_item_number, ai.agenda_item_title, ai.id AS agenda_item_db_id,
                       ai.agenda_item_text,
                       m.id AS meeting_db_id
                FROM entity_mentions em
                JOIN agenda_items ai ON ai.id = em.source_id AND em.source_type = 'agenda_item'
                JOIN meetings m ON m.id = ai.meeting_db_id
                JOIN jurisdictions j ON j.id = m.jurisdiction_id
                WHERE em.entity_id = :eid{jur_filter}
                ORDER BY m.meeting_date DESC, ai.agenda_item_number
            """),
            params,
        ).fetchall()

        mention_details = {}
        for md in mention_details_raw:
            mid = md.meeting_db_id
            if mid not in mention_details:
                mention_details[mid] = []
            mention_details[mid].append(dict(md._mapping))

        # ── Related entities (via entity_relationships) ──
        related = c.execute(
            text("""
                SELECT e_from.id AS from_id, e_from.name AS from_name, e_from.entity_type AS from_type,
                       er.relationship,
                       e_to.id AS to_id, e_to.name AS to_name, e_to.entity_type AS to_type
                FROM entity_relationships er
                JOIN entities e_from ON e_from.id = er.from_entity_id
                JOIN entities e_to ON e_to.id = er.to_entity_id
                WHERE er.from_entity_id = :eid OR er.to_entity_id = :eid
                ORDER BY er.relationship
            """),
            {"eid": entity_id},
        ).fetchall()

        # Recent meetings mentioning this entity
        recent_meetings = c.execute(
            text("""
                SELECT m.meeting_date, m.meeting_id, m.body, pb.name AS body_name,
                       j.name AS jurisdiction_name, m.meeting_type,
                       em.mention_text
                FROM entity_mentions em
                JOIN meetings m ON m.id = em.source_id AND em.source_type = 'meeting'
                JOIN public_bodies pb ON pb.body_code = m.body
                JOIN jurisdictions j ON j.id = m.jurisdiction_id
                WHERE em.entity_id = :eid
                ORDER BY m.meeting_date DESC
                LIMIT 20
            """),
            {"eid": entity_id},
        ).fetchall()

        # Documents mentioning this entity
        docs = c.execute(
            text("""
                SELECT em.mention_text, em.context_snippet,
                       sd.id AS doc_id, sd.document_title, sd.document_url,
                       sd.text_content,
                       m.meeting_date, m.meeting_id, m.body
                FROM entity_mentions em
                JOIN supporting_documents sd ON sd.id = em.source_id AND em.source_type = 'supporting_doc'
                JOIN meetings m ON m.id = sd.meeting_db_id
                WHERE em.entity_id = :eid
                ORDER BY m.meeting_date DESC
                LIMIT 20
            """),
            {"eid": entity_id},
        ).fetchall()

        # Jurisdiction breakdown: appearances per jurisdiction
        jur_breakdown = c.execute(
            text("""
                SELECT j.name, j.slug, COUNT(DISTINCT m.id) AS meeting_count,
                       COUNT(DISTINCT ai.id) AS item_count,
                       SUM(CASE WHEN em.is_withdrawn THEN 1 ELSE 0 END) AS withdrawal_count
                FROM entity_mentions em
                JOIN agenda_items ai ON ai.id = em.source_id AND em.source_type = 'agenda_item'
                JOIN meetings m ON m.id = ai.meeting_db_id
                JOIN jurisdictions j ON j.id = m.jurisdiction_id
                WHERE em.entity_id = :eid
                GROUP BY j.name, j.slug
                ORDER BY meeting_count DESC
            """),
            {"eid": entity_id},
        ).fetchall()

        # Clients represented (for law firms / attorneys — entities they represent)
        clients = c.execute(
            text("""
                SELECT e.id, e.name, e.entity_type, e.mention_count
                FROM entity_relationships er
                JOIN entities e ON e.id = er.to_entity_id
                WHERE er.from_entity_id = :eid AND er.relationship = 'represents'
                ORDER BY e.mention_count DESC
            """),
            {"eid": entity_id},
        ).fetchall()

        # Legal representatives (for developers — entities representing them)
        legal_reps = c.execute(
            text("""
                SELECT e.id, e.name, e.entity_type, e.mention_count
                FROM entity_relationships er
                JOIN entities e ON e.id = er.from_entity_id
                WHERE er.to_entity_id = :eid AND er.relationship = 'represents'
                ORDER BY e.mention_count DESC
            """),
            {"eid": entity_id},
        ).fetchall()

    return render_template(
        "entity_detail.html",
        entity=entity,
        timeline=timeline,
        related=related,
        recent_meetings=recent_meetings,
        docs=docs,
        jur_breakdown=jur_breakdown,
        clients=clients,
        legal_reps=legal_reps,
        withdrawal_count=withdrawal_count,
        jurisdictions=jurisdictions,
        representatives=representatives,
        active_jurisdiction=active_jurisdiction,
        entity_id=entity_id,
        mention_dates=mention_dates,
        meeting_count=meeting_count,
        mention_details=mention_details,
    )
