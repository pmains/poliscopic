"""Entity taxonomy — entity_types table + seed + legacy audit (Brief 015, Step 1).

Mirrors the proven meeting_event_types pattern (slug / parent_slug / leaf value /
display_name). Gives the entity side of the knowledge graph an is-a hierarchy
so queries can generalize ("all organization actors") without hardcoded unions.

Run (dev):
    PYTHONPATH=scripts python3 scripts/entities/entity_taxonomy.py

Idempotent: CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT (slug) DO NOTHING.
Safe to run any number of times. Does NOT rewrite entities.entity_type values —
entity rows keep their current leaf value; the taxonomy supplies parentage.

Audit findings (2026-09-02, dev DB — 12 distinct entity_type values, 98,539 rows):
    person 36324  organization 32739  case 18767  parcel 8310  address 1636
    meeting 655   developer 35        planning_firm 29  law_firm 20
    recommendation 19  utility 4      advocacy_group 1
  - NO role-shaped values (applicant/attorney/owner/etc.) exist in entity_type —
    roles already live on entity_relationships edges and mentions. The role
    cleanup called for in the brief is therefore already done in practice.
  - 'recommendation' (19) is outcome-shaped legacy data (targets of
    HAS_RECOMMENDATION edges); kept reachable as a documented root leaf,
    flagged for possible migration into meeting_event_types later.
"""

import logging

from sqlalchemy import text

from db import get_engine

log = logging.getLogger("entity_taxonomy")

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

# Single multi-row INSERT (parents + children together) so the self-referencing
# FK is satisfied within one statement - same pattern as meeting_event_types.
# Executed as ONE statement (never split on ';' - descriptions contain semicolons).
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
    ('recommendation',  NULL,  'recommendation', 'Recommendation', 'LEGACY outcome-shaped rows (e.g. Approve with conditions). '
                                                                   'Targets of HAS_RECOMMENDATION edges; consider migrating into '
                                                                   'meeting_event_types in a future cleanup'),
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


def audit(engine) -> dict:
    """Map every distinct entities.entity_type to a taxonomy leaf. Zero-unmapped check."""
    with engine.connect() as c:
        legacy = c.execute(
            text("SELECT entity_type, count(*) FROM entities GROUP BY entity_type ORDER BY 2 DESC")
        ).fetchall()
        leaves = {r[0] for r in c.execute(
            text("SELECT entity_type FROM entity_types")
        ).fetchall()}
    unmapped = [(r[0], r[1]) for r in legacy if r[0] not in leaves]
    log.info("Legacy entity_type values: %d distinct, %d total", len(legacy), sum(r[1] for r in legacy))
    for r in legacy:
        log.info("  %-18s %7d  %s", r[0], r[1], "" if r[0] in leaves else "⚠ UNMAPPED")
    if unmapped:
        log.error("Unmapped entity_type values (need taxonomy rows): %s", unmapped)
    else:
        log.info("Zero unmapped entity rows ✅")
    return {"distinct": len(legacy), "total": sum(r[1] for r in legacy), "unmapped": unmapped}


def ensure_entity_types(engine) -> None:
    """Create the entity_types table and seed the taxonomy (idempotent).

    Each SQL block executes as ONE statement - never split on ';' because
    description strings may contain semicolons.
    """
    with engine.begin() as c:
        c.execute(text(SCHEMA_SQL))
    with engine.begin() as c:
        c.execute(text(TAXONOMY_SQL))
    log.info("entity_types table ready; taxonomy seeded")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    engine = get_engine()
    ensure_entity_types(engine)

    # Verification: leaves under organization (is-a generalization)
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT slug, entity_type, display_name FROM entity_types
            WHERE slug = 'organization'
               OR parent_slug = 'organization'
               OR parent_slug IN (SELECT slug FROM entity_types WHERE parent_slug = 'organization')
            ORDER BY slug
        """)).fetchall()
    log.info("Subtree under organization (%d rows):", len(rows))
    for r in rows:
        log.info("  %-34s entity_type=%-14s %s", r.slug, r.entity_type, r.display_name)

    audit(engine)


if __name__ == "__main__":
    main()
