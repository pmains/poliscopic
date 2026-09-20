"""Tests for the read-only Stage 1 compatibility audit (Brief 018 Step 2).

The audit runs against an isolated in-memory SQLite fixture shaped like the
production tables it reads.  Tests prove classification coverage, direction
and leaf-emission detection, lineage reporting, and that the audit never
mutates rows.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from scripts.kg.compatibility_audit import audit


_CLEAN_ROLES = ("applicant", "staff", "presenter")


def _engine(*, dirty: bool) -> "object":
    """Build a small audit fixture; ``dirty`` adds unmappable values."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE entities (id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL)",
            """CREATE TABLE entity_types (
                   slug TEXT PRIMARY KEY, parent_slug TEXT, entity_type TEXT NOT NULL)""",
            """CREATE TABLE entity_relationships (
                   id INTEGER PRIMARY KEY, relationship TEXT NOT NULL, edge_kind TEXT,
                   provenance_type TEXT, provenance_id INTEGER,
                   from_entity_id INTEGER, to_entity_id INTEGER)""",
            """CREATE TABLE entity_mentions (
                   id INTEGER PRIMARY KEY, entity_id INTEGER, source_type TEXT,
                   source_id INTEGER, role_in_context TEXT, extracted_by TEXT)""",
            """CREATE TABLE meeting_events (
                   id INTEGER PRIMARY KEY, event_type_id INTEGER, outcome TEXT)""",
            """CREATE TABLE meeting_event_types (
                   id INTEGER PRIMARY KEY, slug TEXT, parent_slug TEXT, event_type TEXT)""",
            """CREATE TABLE meeting_event_extractions (
                   id INTEGER PRIMARY KEY, extractor TEXT, extractor_version TEXT,
                   text_offset_start INTEGER, text_offset_end INTEGER,
                   supporting_doc_id INTEGER)""",
            """CREATE TABLE event_participants (
                   meeting_event_id INTEGER, entity_id INTEGER,
                   role_in_event TEXT, confidence REAL)""",
            """CREATE TABLE agenda_items (
                   id INTEGER PRIMARY KEY, agenda_item_text TEXT, agenda_item_url TEXT)""",
            """CREATE TABLE supporting_documents (
                   id INTEGER PRIMARY KEY, text_extraction_method TEXT,
                   text_content TEXT, content_hash TEXT)""",
        ):
            connection.execute(text(statement))

        connection.execute(text(
            "INSERT INTO entities (id, entity_type) VALUES "
            "(1,'person'),(2,'organization'),(3,'case'),(4,'meeting'),"
            "(5,'jurisdiction'),(6,'firm')"
        ))
        connection.execute(text(
            "INSERT INTO entity_types (slug, parent_slug, entity_type) VALUES "
            "('person',NULL,'person'),('firm','organization','firm'),"
            "('organization',NULL,'organization')"
        ))
        connection.execute(text("""
            INSERT INTO entity_relationships
                (id, relationship, edge_kind, provenance_type, provenance_id,
                 from_entity_id, to_entity_id)
            VALUES
                (1,'APPLIED_FOR','relational','agenda_item',1,1,3),
                (2,'APPLIED_FOR','relational','agenda_item',1,3,1),
                (3,'HAS_STAFF','relational','pz_item_detail',7,1,3),
                (4,'PART_OF','attributional','meetings',4,4,5)
        """))
        connection.execute(text("""
            INSERT INTO entity_mentions
                (id, entity_id, source_type, source_id, role_in_context, extracted_by)
            VALUES
                (1,1,'agenda_item',1,'applicant','pattern_cascade'),
                (2,1,'agenda_item',1,'known_org','sweep_docs'),
                (3,1,'agenda_item',1,'staff','graph_builder')
                %s
        """ % (",(4,1,'agenda_item',1,'MysteryRole','regex')" if dirty else "")))
        connection.execute(text(
            "INSERT INTO meeting_event_types (id, slug, parent_slug, event_type) VALUES "
            "(1,'approval','decision','approval'),(2,'decision',NULL,'decision')"
        ))
        connection.execute(text(
            "INSERT INTO meeting_events (id, event_type_id, outcome) VALUES "
            "(1,1,'approved_with_conditions'),(2,1,'approved')%s"
            % (",(3,1,'mystery_outcome')" if dirty else "")
        ))
        connection.execute(text(
            "INSERT INTO meeting_event_extractions "
            "(id, extractor, extractor_version, text_offset_start, text_offset_end, "
            "supporting_doc_id) VALUES (1,'pattern','2026-07-27.1',0,10,1),"
            "(2,'pattern','2026-07-27.1',NULL,NULL,2)"
        ))
        connection.execute(text(
            "INSERT INTO event_participants (meeting_event_id, entity_id, "
            "role_in_event, confidence) VALUES (1,1,'presenter',0.9)"
        ))
        connection.execute(text(
            "INSERT INTO agenda_items (id, agenda_item_text, agenda_item_url) VALUES "
            "(1,'Approve case C-1','http://example.test/a'),(2,NULL,NULL)"
        ))
        connection.execute(text(
            "INSERT INTO supporting_documents "
            "(id, text_extraction_method, text_content, content_hash) VALUES "
            "(1,'pdf_text','body','abc'),(2,NULL,NULL,NULL)"
        ))
    return engine


def _row_counts(engine) -> dict[str, int]:
    tables = (
        "entities", "entity_relationships", "entity_mentions", "meeting_events",
        "agenda_items", "supporting_documents", "event_participants",
    )
    with engine.connect() as connection:
        return {
            table: int(connection.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one())
            for table in tables
        }


def test_clean_fixture_passes_with_no_unmapped_values():
    document = audit(_engine(dirty=False), sample_limit=3)
    assert document["audit_passed_no_unmapped"] is True
    assert document["unmapped"] == {}
    assert document["model_version"] == "kg-model/1.0"
    assert len(document["registry_snapshot_sha256"]) == 64


def test_unmapped_role_and_outcome_are_reported():
    document = audit(_engine(dirty=True), sample_limit=3)
    assert document["audit_passed_no_unmapped"] is False
    assert "MysteryRole" in document["unmapped"]["role"]
    assert "mystery_outcome" in document["unmapped"]["event.unmapped_outcomes"]


def test_roles_are_classified_by_producer_and_source():
    document = audit(_engine(dirty=False), sample_limit=3)
    records = {item["value"]: item for item in document["sections"]["role"]["mention_roles"]}
    assert records["applicant"]["status"] == "canonical"
    assert records["known_org"]["status"] == "quarantined"
    assert records["staff"]["producer"] == "graph_builder"
    participants = document["sections"]["role"]["event_participant_roles"]
    assert participants[0]["value"] == "presenter"


def test_relationship_direction_violation_detected():
    document = audit(_engine(dirty=False), sample_limit=5)
    section = document["sections"]["relationship"]
    assert section["direction_violation_count"] == 1
    violation = section["direction_violations"][0]
    assert violation["predicate"] == "APPLIED_FOR"
    assert violation["from_type"] == "case" and violation["to_type"] == "person"


def test_historical_predicate_is_compatibility_mapped():
    document = audit(_engine(dirty=False), sample_limit=5)
    predicates = {item["value"]: item for item in document["sections"]["relationship"]["predicates"]}
    assert predicates["APPLIED_FOR"]["status"] == "canonical"
    assert predicates["HAS_STAFF"]["status"] == "compatibility_mapped"


def test_leaf_emission_violation_detected_for_parent_type():
    document = audit(_engine(dirty=False), sample_limit=5)
    assert "firm" in document["sections"]["entity_type"]["leaf_emission_violations"]


def test_event_root_emission_and_qualifier_split():
    document = audit(_engine(dirty=False), sample_limit=5)
    events = document["sections"]["event"]
    outcomes = {item["value"]: item for item in events["outcomes"]}
    assert outcomes["approved_with_conditions"]["base"] == "approved"
    assert outcomes["approved_with_conditions"]["qualifier"] == "with_conditions"
    assert events["leaf_emission_violations"] == []
    assert "approved_with_conditions" in events["inclusive_query_check"]["approved"]


def test_agenda_lineage_reports_unidentifiable_text():
    document = audit(_engine(dirty=False), sample_limit=5)
    lineage = document["sections"]["agenda_evidence_lineage"]
    assert lineage["agenda_items"]["with_text"] == 1
    assert lineage["agenda_items"]["text_extraction_method_recorded"] is False
    assert lineage["agenda_items"]["rows_unable_to_identify_extraction_method"] == 1
    assert lineage["event_extractions_missing_offsets"] == 1


def test_provenance_section_flags_missing_assertion_class_columns():
    document = audit(_engine(dirty=False), sample_limit=5)
    provenance = document["sections"]["provenance"]
    assert provenance["assertion_class_columns_present"] is False
    assert provenance["relationships_missing_assertion_class"] == 4
    assert provenance["mentions_missing_assertion_class"] == 3


def test_direction_violation_breakdown_reconciles_to_total():
    """The reported total must always equal its parts (regression guard)."""
    section = audit(_engine(dirty=False), sample_limit=5)["sections"]["relationship"]
    assert section["direction_violation_count"] == section[
        "direction_violation_breakdown_total"
    ]
    assert section["direction_violation_breakdown_total"] == sum(
        group["count"] for group in section["direction_violation_breakdown"]
    )
    assert sum(section["direction_violation_by_predicate"].values()) == section[
        "direction_violation_count"
    ]


def test_breakdown_is_complete_regardless_of_sample_limit():
    """Samples are capped for readability; the breakdown never is."""
    small = audit(_engine(dirty=False), sample_limit=1)["sections"]["relationship"]
    large = audit(_engine(dirty=False), sample_limit=50)["sections"]["relationship"]
    assert small["direction_violation_breakdown"] == large[
        "direction_violation_breakdown"
    ]
    assert small["direction_violation_breakdown_total"] == large[
        "direction_violation_breakdown_total"
    ]
    assert len(small["direction_violations"]) <= len(large["direction_violations"])


def test_direction_violation_breakdown_names_endpoint_classes():
    section = audit(_engine(dirty=False), sample_limit=5)["sections"]["relationship"]
    groups = {(g["predicate"], g["from_type"], g["to_type"]): g["count"]
              for g in section["direction_violation_breakdown"]}
    assert groups[("APPLIED_FOR", "case", "person")] == 1


def test_audit_is_read_only():
    engine = _engine(dirty=False)
    before = _row_counts(engine)
    audit(engine, sample_limit=2)
    assert _row_counts(engine) == before
