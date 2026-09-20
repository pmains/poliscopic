"""Pure accounting tests for the event participant linker."""

import struct

import pytest
from sqlalchemy import create_engine, text

import scripts.entities.event_link as event_link
from scripts.entities.event_link import (
    _classify_candidate_rows,
    _deduplicate_candidates,
    link_events,
    load_meeting_entity_lookup,
)


def test_same_event_candidates_keep_max_confidence():
    candidates = [
        (7, 11, "applicant", 0.5),
        (7, 11, "applicant", 0.95),
        (7, 11, "staff", 0.8),
    ]

    assert _deduplicate_candidates(candidates) == [
        (7, 11, "applicant", 0.95),
        (7, 11, "staff", 0.8),
    ]


def test_existing_equal_or_higher_confidence_is_replay_collision():
    existing = {(7, 11, "applicant"): 0.95, (7, 12, "staff"): 0.99}
    candidates = [(7, 11, "applicant", 0.95), (7, 12, "staff", 0.8)]

    counts, insert_rows, update_rows = _classify_candidate_rows(
        existing, candidates
    )

    assert counts == {
        "participants_inserted": 0,
        "participants_updated": 0,
        "participant_replay_collisions": 2,
    }
    assert insert_rows == []
    assert update_rows == []


def test_lower_existing_confidence_is_upgrade():
    existing = {(7, 11, "applicant"): 0.5}

    counts, insert_rows, update_rows = _classify_candidate_rows(
        existing, [(7, 11, "applicant", 0.95)]
    )

    assert counts == {
        "participants_inserted": 0,
        "participants_updated": 1,
        "participant_replay_collisions": 0,
    }
    assert insert_rows == []
    assert update_rows == [(
        7,
        11,
        "applicant",
        event_link._storage_confidence(0.95),
    )]


def test_float32_equivalent_confidence_is_a_replay_collision():
    candidate = 3 / 35
    stored = struct.unpack("!f", struct.pack("!f", candidate))[0]

    counts, insert_rows, update_rows = _classify_candidate_rows(
        {(7, 11, "applicant"): stored},
        [(7, 11, "applicant", candidate)],
    )

    assert counts == {
        "participants_inserted": 0,
        "participants_updated": 0,
        "participant_replay_collisions": 1,
    }
    assert insert_rows == []
    assert update_rows == []


def test_meeting_lookup_selects_role_deterministically_by_source_and_mention_id():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE entities(
                id INTEGER PRIMARY KEY,
                name TEXT,
                normalized_name TEXT,
                resolution_status TEXT
            )
        """))
        connection.execute(text("""
            CREATE TABLE agenda_items(id INTEGER PRIMARY KEY, meeting_id TEXT)
        """))
        connection.execute(text("""
            CREATE TABLE supporting_documents(
                id INTEGER PRIMARY KEY,
                meeting_id TEXT
            )
        """))
        connection.execute(text("""
            CREATE TABLE entity_mentions(
                id INTEGER PRIMARY KEY,
                entity_id INTEGER,
                source_type TEXT,
                source_id INTEGER,
                role_in_context TEXT
            )
        """))
        connection.execute(text("""
            INSERT INTO entities(id, name, normalized_name, resolution_status)
            VALUES (9, 'Alice Smith', 'alice smith', 'canonical')
        """))
        connection.execute(text("""
            INSERT INTO agenda_items(id, meeting_id) VALUES (1, 'meeting-1')
        """))
        connection.execute(text("""
            INSERT INTO supporting_documents(id, meeting_id)
            VALUES (2, 'meeting-1')
        """))
        connection.execute(text("""
            INSERT INTO entity_mentions(
                id, entity_id, source_type, source_id, role_in_context
            ) VALUES
                (30, 9, 'agenda_item', 1, 'owner'),
                (10, 9, 'supporting_document', 2, 'staff'),
                (20, 9, 'agenda_item', 1, 'applicant')
        """))

    first = load_meeting_entity_lookup(engine)
    second = load_meeting_entity_lookup(engine)

    assert first == second
    assert first["meeting-1"][9]["role"] == "applicant"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, event_ids, extraction_rows=1):
        self.event_ids = event_ids
        self.extraction_rows = extraction_rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        sql = str(query)
        if "LEFT JOIN meeting_event_extractions" in sql:
            ids = params["event_ids"]
            return _Result([
                (event_id, f"meeting-{event_id}", "approved", None,
                 "Applicant: Alice Smith", "decision.approval", None)
                for event_id in ids
                for _ in range(self.extraction_rows)
            ])
        if "SELECT DISTINCT e.id" in sql:
            cursor = params["cursor"]
            limit = params["limit"]
            return _Result([(event_id,) for event_id in self.event_ids
                            if event_id > cursor][:limit])
        if "FROM event_participants" in sql:
            return _Result([])
        raise AssertionError(f"unexpected SQL in isolated fake: {sql}")


class _Engine:
    def __init__(self, event_ids, extraction_rows=1):
        self.event_ids = event_ids
        self.extraction_rows = extraction_rows

    def connect(self):
        return _Connection(self.event_ids, self.extraction_rows)

    def begin(self):
        return _Connection(self.event_ids, self.extraction_rows)


def test_multiple_extractions_for_one_event_are_preserved_then_deduplicated():
    engine = _Engine([1], extraction_rows=2)
    stats = link_events(
        engine,
        [{"id": 9, "name": "Alice Smith", "normalized_name": "alice smith",
          "entity_type": "person"}],
        {},
        limit=1,
        dry_run=True,
    )

    assert stats["events_attempted"] == 1
    assert stats["names_from_text"] == 2
    assert stats["participant_attempts"] == 1


def test_exact_limit_and_dry_run_report_planned_outcome_without_writes():
    engine = _Engine([1, 2, 3, 4, 5])
    stats = link_events(
        engine,
        [{"id": 9, "name": "Alice Smith", "normalized_name": "alice smith",
          "entity_type": "person"}],
        {},
        limit=3,
        dry_run=True,
    )

    assert stats["events_attempted"] == 3
    assert stats["events_processed"] == 3
    assert stats["participant_attempts"] == 3
    assert stats["participants_inserted"] == 0
    assert stats["participants_updated"] == 0
    assert stats["participants_planned_insert"] == 3
    assert stats["participants_planned_update"] == 0
    assert stats["participant_replay_collisions"] == 0
    assert stats["participants_written"] == 0


def test_unresolved_name_is_counted_without_a_participant_attempt():
    engine = _Engine([1])
    stats = link_events(
        engine,
        [],
        {},
        limit=1,
        dry_run=True,
    )

    assert stats["unresolved_names"] == 1
    assert stats["participant_attempts"] == 0


def _sqlite_link_engine(existing_confidence=None):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as c:
        c.execute(text("""
            CREATE TABLE meeting_event_types(
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL
            )
        """))
        c.execute(text("""
            CREATE TABLE meeting_events(
                id INTEGER PRIMARY KEY,
                meeting_id TEXT,
                outcome TEXT,
                case_number TEXT,
                event_type_id INTEGER NOT NULL,
                supporting_doc_id INTEGER
            )
        """))
        c.execute(text("""
            CREATE TABLE meeting_event_extractions(
                id INTEGER PRIMARY KEY,
                meeting_event_id INTEGER NOT NULL,
                raw_text TEXT NOT NULL
            )
        """))
        c.execute(text("""
            CREATE TABLE event_participants(
                meeting_event_id INTEGER NOT NULL,
                entity_id INTEGER NOT NULL,
                role_in_event TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (meeting_event_id, entity_id, role_in_event)
            )
        """))
        c.execute(text(
            "INSERT INTO meeting_event_types(id, slug) VALUES (1, 'decision.approval')"
        ))
        c.execute(text("""
            INSERT INTO meeting_events(
                id, meeting_id, outcome, case_number, event_type_id,
                supporting_doc_id
            )
            VALUES (1, 'meeting-1', 'approved', NULL, 1, 44)
        """))
        c.execute(text("""
            INSERT INTO meeting_event_extractions(
                id, meeting_event_id, raw_text
            )
            VALUES (
                1, 1,
                'APPROVED 1. Project case materials. Applicant: Alice Smith, Owner'
            )
        """))
        if existing_confidence is not None:
            c.execute(text("""
                INSERT INTO event_participants(
                    meeting_event_id, entity_id, role_in_event, confidence
                )
                VALUES (1, 9, 'applicant', :confidence)
            """), {"confidence": existing_confidence})
    return engine


def _alice_lookup():
    return [{
        "id": 9,
        "name": "Alice Smith",
        "normalized_name": "alice smith",
        "entity_type": "person",
    }]


def _participant_confidence(engine):
    with engine.connect() as c:
        return c.execute(text("""
            SELECT confidence FROM event_participants
            WHERE meeting_event_id = 1
              AND entity_id = 9
              AND role_in_event = 'applicant'
        """)).scalar()


def test_first_live_link_inserts_participant_once():
    engine = _sqlite_link_engine()

    stats = event_link.link_events(
        engine, _alice_lookup(), {}, limit=1, dry_run=False
    )

    assert stats["participant_attempts"] == 1
    assert stats["participants_planned_insert"] == 1
    assert stats["participants_planned_update"] == 0
    assert stats["participants_inserted"] == 1
    assert stats["participants_updated"] == 0
    assert stats["participant_replay_collisions"] == 0
    assert stats["participants_written"] == 1
    assert stats["participants_mutated"] == 1
    assert _participant_confidence(engine) == pytest.approx(
        event_link._storage_confidence(0.95)
    )


def test_confidence_upgrade_updates_once_then_exact_replay_is_noop():
    engine = _sqlite_link_engine(existing_confidence=0.5)

    upgraded = event_link.link_events(
        engine, _alice_lookup(), {}, limit=1, dry_run=False
    )
    replayed = event_link.link_events(
        engine, _alice_lookup(), {}, limit=1, dry_run=False
    )

    assert upgraded["participants_planned_insert"] == 0
    assert upgraded["participants_planned_update"] == 1
    assert upgraded["participants_inserted"] == 0
    assert upgraded["participants_updated"] == 1
    assert upgraded["participants_written"] == 1
    assert upgraded["participants_mutated"] == 1
    assert _participant_confidence(engine) == pytest.approx(
        event_link._storage_confidence(0.95)
    )

    assert replayed["participant_attempts"] == 1
    assert replayed["participants_planned_insert"] == 0
    assert replayed["participants_planned_update"] == 0
    assert replayed["participants_inserted"] == 0
    assert replayed["participants_updated"] == 0
    assert replayed["participant_replay_collisions"] == 1
    assert replayed["participants_written"] == 0
    assert replayed["participants_mutated"] == 0


def test_unchanged_collision_does_not_enter_write_path(monkeypatch):
    engine = _sqlite_link_engine(existing_confidence=0.95)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("unchanged replay collision reached write path")

    monkeypatch.setattr(event_link, "_insert_participants", fail_if_called)
    monkeypatch.setattr(event_link, "_upgrade_participants", fail_if_called)
    stats = event_link.link_events(
        engine, _alice_lookup(), {}, limit=1, dry_run=False
    )

    assert stats["participant_attempts"] == 1
    assert stats["participant_replay_collisions"] == 1
    assert stats["participants_written"] == 0
    assert stats["participants_mutated"] == 0


def test_guarded_equal_update_reports_zero_actual_mutations():
    stored = event_link._storage_confidence(3 / 35)
    engine = _sqlite_link_engine(existing_confidence=stored)

    with engine.begin() as connection:
        updated = event_link._upgrade_participants(
            connection,
            [(1, 9, "applicant", 3 / 35)],
        )

    assert updated == 0
    assert _participant_confidence(engine) == pytest.approx(stored)
