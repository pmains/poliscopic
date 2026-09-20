#!/usr/bin/env python3
"""Schema introspection and column contracts for the dev→prod sync.

Extracted from ``scripts/db/sync_prod.py`` as a behavior-preserving split; the
code below is unchanged.  ``scripts/db/sync_prod.py`` remains the CLI facade.
"""

from __future__ import annotations

import logging
import os
import sys

# Make the shared modules importable however this module is invoked.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


from sqlalchemy import text, inspect as sa_inspect
try:
    from kg import stage2_parentage_contract as parentage_contract
except ImportError:  # direct execution
    from scripts.kg import stage2_parentage_contract as parentage_contract
log = logging.getLogger("sync")




# ── Schema helpers ──


def _pk_cols(engine, table: str) -> list[str]:
    inspector = sa_inspect(engine)
    pk = inspector.get_pk_constraint(table)
    if pk and pk.get("constrained_columns"):
        return list(pk["constrained_columns"])
    # Fallback to "id"
    return ["id"]




def _quoted_cols(cols: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)




def _column_intersection(dev_engine, prod_engine, table: str) -> list[str]:
    dev_cols = {
        c["name"] for c in sa_inspect(dev_engine).get_columns(table)
        if c["name"] != "rowid"
    }
    prod_cols = {
        c["name"] for c in sa_inspect(prod_engine).get_columns(table)
        if c["name"] != "rowid"
    }
    if table == parentage_contract.PARENTAGE_TABLE:
        # The intersection rule silently drops any column the target has not
        # gained yet.  For parentage that would mean shipping every other change
        # while quietly leaving meetings without a parent, so refuse instead.
        problems = parentage_contract.readiness_problems(
            parentage_contract.sync_readiness(dev_cols, prod_cols, table)
        )
        if problems:
            raise RuntimeError(
                "parentage columns would not be carried by this sync: "
                + "; ".join(problems)
            )
    return sorted(dev_cols & prod_cols)




def _table_has_updated_at(engine, table: str) -> bool:
    """Check if a table has an 'updated_at' column."""
    cols = {c["name"] for c in sa_inspect(engine).get_columns(table)}
    return "updated_at" in cols




def _ensure_updated_at_on_prod(prod_engine):
    """Add updated_at to tables that are missing it on prod."""
    needs_updated_at = [
        "agenda_items",
        "case_events",
        "entity_mentions",
    ]
    for table in needs_updated_at:
        if _table_has_updated_at(prod_engine, table):
            continue
        log.info("  Adding updated_at to %s on prod...", table)
        with prod_engine.begin() as c:
            c.execute(text(
                f'ALTER TABLE "{table}" '
                "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ))
            c.execute(text(
                f'UPDATE "{table}" SET updated_at = created_at '
                "WHERE updated_at != created_at"
            ))
        log.info("    done")




def _ensure_event_tables(prod_engine):
    """Create Phase 5/6 event tables on prod if they don't exist."""
    inspector = sa_inspect(prod_engine)
    existing = set(inspector.get_table_names())
    needed = {"meeting_event_types", "meeting_events",
              "meeting_event_extractions", "event_participants"}
    if needed <= existing:
        return

    log.info("  Creating event tables on prod...")
    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS meeting_event_types (
            id              SERIAL PRIMARY KEY,
            slug            VARCHAR(64) NOT NULL UNIQUE,
            parent_slug     VARCHAR(64) REFERENCES meeting_event_types(slug),
            event_type      VARCHAR(64) NOT NULL,
            display_name    VARCHAR(128) NOT NULL,
            description     TEXT
        );
        CREATE TABLE IF NOT EXISTS meeting_events (
            id                  SERIAL PRIMARY KEY,
            meeting_id          VARCHAR(64) NOT NULL,
            supporting_doc_id   INTEGER REFERENCES supporting_documents(id),
            agenda_item_id      INTEGER REFERENCES agenda_items(id),
            event_type_id       INTEGER NOT NULL REFERENCES meeting_event_types(id),
            outcome             VARCHAR(64) NOT NULL,
            action_verb         VARCHAR(256) NOT NULL,
            text_offset_start   INTEGER,
            text_offset_end     INTEGER,
            case_number         VARCHAR(32),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_meeting_events_meeting ON meeting_events(meeting_id);
        CREATE INDEX IF NOT EXISTS ix_meeting_events_type ON meeting_events(event_type_id);
        CREATE INDEX IF NOT EXISTS ix_meeting_events_case ON meeting_events(case_number);
        CREATE TABLE IF NOT EXISTS meeting_event_extractions (
            id                  SERIAL PRIMARY KEY,
            meeting_event_id    INTEGER REFERENCES meeting_events(id) ON DELETE SET NULL,
            extractor           VARCHAR(16) NOT NULL DEFAULT 'pattern',
            extractor_version   VARCHAR(32),
            raw_text            TEXT NOT NULL,
            confidence          REAL NOT NULL DEFAULT 0.0,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            supporting_doc_id   INTEGER REFERENCES supporting_documents(id),
            action_verb         VARCHAR(256),
            text_offset_start   INTEGER,
            text_offset_end     INTEGER,
            case_number         VARCHAR(32)
        );
        CREATE INDEX IF NOT EXISTS ix_meeting_event_extractions_event
            ON meeting_event_extractions(meeting_event_id);
        CREATE INDEX IF NOT EXISTS ix_meeting_event_extractions_extractor
            ON meeting_event_extractions(extractor);
        CREATE TABLE IF NOT EXISTS event_participants (
            meeting_event_id    INTEGER NOT NULL REFERENCES meeting_events(id) ON DELETE CASCADE,
            entity_id           INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            role_in_event       VARCHAR(64) NOT NULL,
            confidence          REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (meeting_event_id, entity_id, role_in_event)
        );
        CREATE INDEX IF NOT EXISTS ix_event_participants_entity
            ON event_participants(entity_id);
    """
    with prod_engine.begin() as c:
        for stmt in SCHEMA_SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                c.execute(text(stmt))

    # Insert taxonomy if empty
    with prod_engine.connect() as c:
        count = c.execute(text("SELECT COUNT(*) FROM meeting_event_types")).scalar()
    if count == 0:
        log.info("  Inserting event taxonomy...")
        TAXONOMY_SQL = """
            INSERT INTO meeting_event_types (slug, parent_slug, event_type, display_name, description)
            VALUES
                ('decision',        NULL,  'decision',     'Decision',     'Any decision'),
                ('legislation',     NULL,  'legislation',  'Legislation',  'Legislative actions'),
                ('administration',  NULL,  'administration','Administration','Admin actions'),
                ('procedure',       NULL,  'procedure',    'Procedure',    'Procedural actions'),
                ('decision.approval',       'decision', 'approval',     'Approval',         ''),
                ('decision.denial',         'decision', 'denial',       'Denial',           ''),
                ('decision.continuation',   'decision', 'continuation', 'Continuation',     ''),
                ('legislation.adoption',    'legislation', 'adoption',  'Adoption',         ''),
                ('legislation.introduction','legislation', 'introduction','Introduction',    ''),
                ('legislation.amendment',   'legislation', 'amendment',  'Amendment',        ''),
                ('administration.appointment','administration','appointment','Appointment',   ''),
                ('administration.removal',  'administration', 'removal',   'Removal',        ''),
                ('administration.resignation','administration','resignation','Resignation',   ''),
                ('procedure.discussion',         'procedure', 'discussion',   'Discussion',   ''),
                ('procedure.public_hearing',     'procedure', 'public_hearing','Public Hearing',''),
                ('procedure.executive_session',  'procedure', 'executive_session','Exec Session',''),
                ('procedure.receipt',            'procedure', 'receipt',     'Received',     '')
            ON CONFLICT (slug) DO NOTHING;
        """
        with prod_engine.begin() as c:
            for stmt in TAXONOMY_SQL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    c.execute(text(stmt))
    log.info("  Event tables ready.")




def _ensure_entity_taxonomy(prod_engine):
    """Create the entity_types taxonomy table on prod if it doesn't exist (Brief 015).

    Mirrors _ensure_event_tables: idempotent CREATE + seed-if-empty. Keep the
    seed rows in sync with scripts/entities/entity_taxonomy.py.
    """
    inspector = sa_inspect(prod_engine)
    existing = set(inspector.get_table_names())
    if "entity_types" in existing:
        return

    log.info("  Creating entity_types table on prod...")
    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS entity_types (
            id           SERIAL PRIMARY KEY,
            slug         VARCHAR(64) NOT NULL UNIQUE,
            parent_slug  VARCHAR(64) REFERENCES entity_types(slug),
            entity_type  VARCHAR(64) NOT NULL,
            display_name VARCHAR(128) NOT NULL,
            description  TEXT
        )
    """
    with prod_engine.begin() as c:
        c.execute(text(SCHEMA_SQL))

    with prod_engine.connect() as c:
        count = c.execute(text("SELECT COUNT(*) FROM entity_types")).scalar()
    if count == 0:
        log.info("  Inserting entity taxonomy...")
        TAXONOMY_SQL = """
            INSERT INTO entity_types (slug, parent_slug, entity_type, display_name, description)
            VALUES
                ('person',          NULL,  'person',          'Person',       'Natural person'),
                ('organization',    NULL,  'organization',   'Organization', 'Any organization (parent of subtypes below)'),
                ('case',            NULL,  'case',           'Case',         'A case before a body (zoning, variance, etc.)'),
                ('parcel',          NULL,  'parcel',         'Parcel',       'A parcel of land'),
                ('address',         NULL,  'address',        'Address',      'A street address'),
                ('meeting',         NULL,  'meeting',        'Meeting',      'A meeting event container'),
                ('body',            NULL,  'body',           'Body',         'A hearing body / board / council'),
                ('jurisdiction',    NULL,  'jurisdiction',   'Jurisdiction', 'A government jurisdiction (county, city, town)'),
                ('recommendation',  NULL,  'recommendation', 'Recommendation', 'LEGACY outcome-shaped rows; targets of HAS_RECOMMENDATION edges'),
                ('organization.firm',            'organization', 'firm',           'Firm',         'A professional firm (parent of law/planning firms)'),
                ('organization.firm.law_firm',   'organization.firm', 'law_firm',  'Law Firm',     'A law firm'),
                ('organization.firm.planning_firm', 'organization.firm', 'planning_firm', 'Planning Firm', 'A planning/engineering/design firm'),
                ('organization.developer',       'organization', 'developer',      'Developer',    'A real-estate developer'),
                ('organization.agency',          'organization', 'agency',         'Agency',       'A government agency'),
                ('organization.utility',         'organization', 'utility',        'Utility',      'A utility provider'),
                ('organization.vendor',          'organization', 'vendor',         'Vendor',       'A vendor/contractor'),
                ('organization.department',      'organization', 'department',     'Department',   'A government department'),
                ('organization.advocacy_group',  'organization', 'advocacy_group', 'Advocacy Group','An advocacy / community group')
            ON CONFLICT (slug) DO NOTHING
        """
        with prod_engine.begin() as c:
            c.execute(text(TAXONOMY_SQL))
    log.info("  entity_types ready.")
