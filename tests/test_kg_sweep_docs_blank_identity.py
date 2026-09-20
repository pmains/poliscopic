"""Regressions for the blank-identity legacy data that broke the sweep.

The failing batch (watermark ``117268``) raised
``ValueError: EntityAssertion.normalized_name must be non-empty``.  The cause was
not document text at all: two legacy ``entities`` rows (ids ``28850`` and
``46173``) carry ``normalized_name = ''``, and the mention loader reconstructed
assertions from those rows through an unguarded join.

These tests pin the exact input shape with isolated in-memory SQLite, and pin the
dry-mode failure accounting so a preview can never claim a rolled-back mutation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text

from scripts.entities.sweep_docs_batch import run_batch
from scripts.entities.sweep_docs_storage import (
    SOURCE_TYPE,
    _load_existing_entity_assertions,
    _load_existing_mention_assertions,
    record_batch_failure,
)
from scripts.kg import registries as r
from scripts.kg.emission import EmissionValidator

#: The exact watermark the failing batch reported, and the source_id that
#: reaches legacy entity 46173 in the development database.
FAILING_WATERMARK = 117268
LEGACY_OFFENDER_SOURCE_ID = 117156


def _conn():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _register_sqlite_now(dbapi_connection, _record):
        """SQLite has no ``now();`` the write path uses it for timestamps."""
        dbapi_connection.create_function("now", 0, lambda: "2026-09-11 00:00:00+00")

    conn = engine.connect()
    conn.execute(text("""
        CREATE TABLE supporting_documents (
            id INTEGER PRIMARY KEY,
            document_title TEXT,
            text_content TEXT,
            text_extraction_method TEXT,
            swept_at TEXT
        )
    """))
    conn.execute(text("""
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            entity_type TEXT,
            name TEXT,
            normalized_name TEXT,
            is_government BOOLEAN,
            resolution_status TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            mention_count INTEGER,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE (normalized_name, entity_type)
        )
    """))
    conn.execute(text("""
        CREATE TABLE entity_mentions (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER,
            source_type TEXT,
            source_id INTEGER,
            role_in_context TEXT
        )
    """))
    return conn


def _add_mention(conn, mention_id: int, entity_id: int, source_id: int,
                 role: str = "mentioned") -> None:
    conn.execute(
        text("INSERT INTO entity_mentions (id, entity_id, source_type, source_id,"
             " role_in_context) VALUES (:i, :e, :st, :s, :r)"),
        {"i": mention_id, "e": entity_id, "st": SOURCE_TYPE, "s": source_id,
         "r": role},
    )


def _add_entity(conn, entity_id: int, normalized_name: str, entity_type: str) -> None:
    conn.execute(
        text("INSERT INTO entities (id, normalized_name, entity_type)"
             " VALUES (:i, :n, :t)"),
        {"i": entity_id, "n": normalized_name, "t": entity_type},
    )


# ── the exact failing shape ─────────────────────────────────────────────


def test_mention_loader_skips_blank_normalized_name_instead_of_raising():
    """Legacy entity 46173's shape: blank name, reached by a real mention."""
    conn = _conn()
    _add_entity(conn, 46173, "", "person")
    _add_mention(conn, 1, 46173, LEGACY_OFFENDER_SOURCE_ID)

    mentions, skipped = _load_existing_mention_assertions(
        conn, [{"_source_id": LEGACY_OFFENDER_SOURCE_ID}]
    )

    assert mentions == []
    assert skipped == 1


def test_mention_loader_skips_whitespace_only_normalized_name():
    conn = _conn()
    _add_entity(conn, 28850, "   ", "organization")
    _add_mention(conn, 1, 28850, LEGACY_OFFENDER_SOURCE_ID)

    mentions, skipped = _load_existing_mention_assertions(
        conn, [{"_source_id": LEGACY_OFFENDER_SOURCE_ID}]
    )

    assert mentions == []
    assert skipped == 1


def test_mention_loader_keeps_valid_rows_alongside_skipped_ones():
    conn = _conn()
    _add_entity(conn, 46173, "", "person")
    _add_entity(conn, 900, "acme corp", "developer")
    _add_mention(conn, 1, 46173, LEGACY_OFFENDER_SOURCE_ID)
    _add_mention(conn, 2, 900, LEGACY_OFFENDER_SOURCE_ID)

    mentions, skipped = _load_existing_mention_assertions(
        conn, [{"_source_id": LEGACY_OFFENDER_SOURCE_ID}]
    )

    assert skipped == 1
    assert [m.entity.identity for m in mentions] == [("acme corp", "developer")]


def test_mention_loader_ignores_other_source_types():
    conn = _conn()
    _add_entity(conn, 46173, "", "person")
    _add_mention(conn, 1, 46173, LEGACY_OFFENDER_SOURCE_ID)
    conn.execute(text("UPDATE entity_mentions SET source_type = 'other'"))

    mentions, skipped = _load_existing_mention_assertions(
        conn, [{"_source_id": LEGACY_OFFENDER_SOURCE_ID}]
    )

    assert (mentions, skipped) == ([], 0)


# ── entity cache keys ───────────────────────────────────────────────────


def test_entity_loader_skips_blank_and_whitespace_keys():
    assertions, skipped = _load_existing_entity_assertions({
        "|person": 46173,             # blank name half
        "   |organization": 28850,    # whitespace-only name half
        "acme|": 7,                   # blank type half
        "acme corp|developer": 900,
    })

    assert skipped == 3
    assert [(a.identity, a.entity_id) for a in assertions] == [
        (("acme corp", "developer"), 900)
    ]


def test_entity_loader_strips_surrounding_whitespace():
    assertions, skipped = _load_existing_entity_assertions({" acme | developer ": 5})

    assert skipped == 0
    assert assertions[0].identity == ("acme", "developer")


# ── dry-mode failure accounting ─────────────────────────────────────────


def test_dry_mode_failure_records_no_rollback():
    """A dry preview must never claim a mutation it never attempted."""
    validator = EmissionValidator("sweep_docs", dry_run=True)
    validator.start_batch()
    validator.complete_validation()
    validator.classify_rows(would_insert=941)

    record_batch_failure(validator, True, "ValueError: boom")

    assert validator.receipt.rows_rolled_back == 0
    assert validator.receipt.rows_committed == 0
    assert validator.receipt.failure == "ValueError: boom"


def test_live_failure_records_uncommitted_mutations_as_rolled_back():
    validator = EmissionValidator("sweep_docs", dry_run=False)
    validator.start_batch()
    validator.complete_validation()
    validator.begin_writes()
    validator.classify_rows(would_insert=5)
    validator.commit(2)

    record_batch_failure(validator, False, "ValueError: boom")

    assert validator.receipt.rows_rolled_back == 3
    assert validator.receipt.rows_committed == 2


def test_live_failure_with_full_commit_records_no_rollback():
    validator = EmissionValidator("sweep_docs", dry_run=False)
    validator.start_batch()
    validator.complete_validation()
    validator.begin_writes()
    validator.classify_rows(would_insert=2)
    validator.commit(2)

    record_batch_failure(validator, False, "ValueError: boom")

    assert validator.receipt.rows_rolled_back == 0
    assert validator.receipt.failure == "ValueError: boom"


# ── end to end: the failing batch shape ─────────────────────────────────


def _extractor(_text: str) -> list[dict]:
    return [{
        "name": "Acme Corp",
        "normalized": "acme corp",
        "entity_type": "developer",
        "role": "mentioned",
        "confidence": 95,
    }]


def test_dry_batch_survives_the_failing_shape():
    """The batch at the failing watermark now completes and reports honestly."""
    conn = _conn()
    conn.execute(
        text("INSERT INTO supporting_documents (id, document_title, text_content,"
             " text_extraction_method, swept_at)"
             " VALUES (:i, :t, :c, :m, NULL)"),
        {"i": FAILING_WATERMARK + 1, "t": "Doc", "c": "case text",
         "m": "pdftotext"},
    )
    # The exact offender: a blank-name legacy entity mentioned by a document in
    # this batch.
    _add_entity(conn, 46173, "", "person")
    _add_mention(conn, 1, 46173, FAILING_WATERMARK + 1)

    validator = EmissionValidator("sweep_docs", dry_run=True)
    stats = run_batch(conn, FAILING_WATERMARK, {}, batch_size=10,
                      extractor=_extractor, mention_writer=None,
                      dry_run=True, validator=validator)

    assert stats["unrepresentable_existing_mentions"] == 1
    # One entity insert plus one mention insert: the plan counts both row kinds,
    # and the skipped blank-identity row contributes to neither.
    assert stats["would_insert"] == 2
    assert stats["unrepresentable_existing_entities"] == 0
    assert validator.receipt.rows_rolled_back == 0
    assert validator.receipt.rows_committed == 0
    assert validator.receipt.failure is None
    # Every document row must be untouched by a dry run.
    swept = conn.execute(text(
        "SELECT count(*) FROM supporting_documents WHERE swept_at IS NOT NULL"
    )).scalar()
    assert swept == 0


def test_live_batch_still_writes_and_reports_no_skips():
    """The live path is unchanged when there is no blank-identity data."""
    conn = _conn()
    conn.execute(
        text("INSERT INTO supporting_documents (id, document_title, text_content,"
             " text_extraction_method, swept_at)"
             " VALUES (:i, :t, :c, :m, NULL)"),
        {"i": 1, "t": "Doc", "c": "case text", "m": "pdftotext"},
    )
    cache: dict[str, int] = {}

    def _writer(_conn, plan, _payloads, entity_cache):
        return len(plan.mention_inserts)

    validator = EmissionValidator("sweep_docs", dry_run=False)
    stats = run_batch(conn, 0, cache, batch_size=10, extractor=_extractor,
                      mention_writer=_writer, dry_run=False, validator=validator)

    assert stats["entities"] == 1
    assert stats["unrepresentable_existing_mentions"] == 0
    assert validator.receipt.rows_committed == 0  # facade commits, not the batch


def test_role_and_type_used_by_the_regression_are_canonical():
    assert "developer" in r.ENTITY_TYPES
    assert "mentioned" in r.CANONICAL_ROLES
