"""Canonical additive schema reconciliation for KG Stage 0 (Brief 017).

This is the executable authority for the existing entity/mention/relationship
integrity schema. It adds constraints/indexes and narrows only columns whose
contents have first been validated. It never changes graph rows.
"""

from sqlalchemy import text


DDL = (
    "ALTER TABLE entities ALTER COLUMN resolution_block_key TYPE VARCHAR(128)",
    "ALTER TABLE entities ALTER COLUMN resolution_status TYPE VARCHAR(32)",
    "ALTER TABLE entities ALTER COLUMN resolution_confidence TYPE NUMERIC(4,3)",
    "ALTER TABLE entities ALTER COLUMN resolution_method TYPE VARCHAR(64)",
    "ALTER TABLE entity_relationships ALTER COLUMN source_type TYPE VARCHAR(32)",
    "CREATE INDEX IF NOT EXISTS ix_entities_canonical ON entities(canonical_entity_id)",
    "CREATE INDEX IF NOT EXISTS ix_entities_resolution_block ON entities(resolution_block_key)",
    "CREATE INDEX IF NOT EXISTS ix_entities_resolution_status ON entities(resolution_status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_entities_unique_norm_type ON entities(normalized_name, entity_type)",
    "CREATE INDEX IF NOT EXISTS ix_entity_mentions_lookup ON entity_mentions(entity_id, source_type, source_id, role_in_context)",
    "CREATE INDEX IF NOT EXISTS ix_entity_mentions_source ON entity_mentions(source_type, source_id)",
    "CREATE INDEX IF NOT EXISTS idx_mentions_source ON entity_mentions(source_type, source_id, entity_id)",
    """DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='entities'::regclass AND contype='f'
          AND conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='entities'::regclass AND attname='canonical_entity_id')]::smallint[]) THEN
        ALTER TABLE entities ADD CONSTRAINT entities_canonical_entity_id_fkey
          FOREIGN KEY (canonical_entity_id) REFERENCES entities(id) NOT VALID;
      END IF; END $$""",
    """DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='entity_relationships'::regclass AND contype='f'
          AND conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='entity_relationships'::regclass AND attname='from_entity_id')]::smallint[]) THEN
        ALTER TABLE entity_relationships ADD CONSTRAINT entity_relationships_from_entity_id_fkey
          FOREIGN KEY (from_entity_id) REFERENCES entities(id) NOT VALID;
      END IF; END $$""",
    """DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='entity_relationships'::regclass AND contype='f'
          AND conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='entity_relationships'::regclass AND attname='to_entity_id')]::smallint[]) THEN
        ALTER TABLE entity_relationships ADD CONSTRAINT entity_relationships_to_entity_id_fkey
          FOREIGN KEY (to_entity_id) REFERENCES entities(id) NOT VALID;
      END IF; END $$""",
)


def ensure_kg_integrity_schema(engine) -> None:
    with engine.connect() as c:
        bad = c.execute(text("""SELECT
          count(*) FILTER (WHERE length(resolution_block_key)>128),
          count(*) FILTER (WHERE length(resolution_status)>32),
          count(*) FILTER (WHERE length(resolution_method)>64),
          count(*) FILTER (WHERE resolution_confidence<0 OR resolution_confidence>9.999)
          FROM entities""")).one()
        long_source = c.execute(text("SELECT count(*) FROM entity_relationships WHERE length(source_type)>32")).scalar()
    if any(int(v or 0) for v in (*bad, long_source)):
        raise RuntimeError("KG schema narrowing preflight failed")
    for statement in DDL:
        with engine.begin() as c:
            c.execute(text(statement))
    with engine.begin() as c:
        for constraint in (
            "entities_canonical_entity_id_fkey",
            "entity_relationships_from_entity_id_fkey",
            "entity_relationships_to_entity_id_fkey",
        ):
            exists = c.execute(text("SELECT 1 FROM pg_constraint WHERE conname=:n"), {"n": constraint}).scalar()
            if exists:
                c.execute(text(f'ALTER TABLE {"entities" if constraint.startswith("entities_") else "entity_relationships"} VALIDATE CONSTRAINT "{constraint}"'))


def main() -> None:
    from db import get_engine
    ensure_kg_integrity_schema(get_engine())
    print("KG Stage 0 integrity schema ready")


if __name__ == "__main__":
    main()
