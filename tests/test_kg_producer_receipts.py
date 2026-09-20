"""Focused, behaviourally-executed tests for the corrected emission lifecycle.

All database work uses isolated in-memory SQLite.  Nothing here touches dev or
production, and the pipeline is never run.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text

from scripts.kg.emission import (
    BoundaryStateError,
    EmissionBundle,
    EmissionError,
    EmissionValidator,
    STATE_SEALED,
    bundle_source_reference,
    emit_validated,
    reconcile_receipts,
)
from scripts.kg import identity
from scripts.kg.identity import (
    adjudication_identity,
    canonical_entity_identity,
    civic_context_identity,
    entity_candidate_identity,
    event_identity,
    evidence_identity,
    vote_identity,
)
from scripts.entities.event_link_storage import (
    _insert_participants,
    _upgrade_participants,
)
from scripts.entities.sweep_docs_payloads import select_write_payloads
from scripts.entities.sweep_docs_planning import (
    EntityAssertion,
    ExtractedCandidate,
    MentionAssertion,
    build_classification_plan,
)


def sqlite_engine(schema: str):
    """In-memory SQLite with a ``now()`` shim for PostgreSQL-style SQL."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_connection, _record):  # pragma: no cover - driver glue
        dbapi_connection.create_function("now", 0, lambda: "2026-01-01 00:00:00")

    with engine.begin() as connection:
        for statement in schema.strip().split(";"):
            if statement.strip():
                connection.execute(text(statement))
    return engine


PARTICIPANTS_SCHEMA = """
CREATE TABLE event_participants (
    meeting_event_id INTEGER, entity_id INTEGER,
    role_in_event TEXT, confidence REAL,
    PRIMARY KEY (meeting_event_id, entity_id, role_in_event))
"""

SWEEP_SCHEMA = """
CREATE TABLE supporting_documents (
    id INTEGER PRIMARY KEY, document_title TEXT, text_content TEXT,
    text_extraction_method TEXT DEFAULT 'pymupdf', swept_at TEXT)
;
CREATE TABLE entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT, name TEXT,
    normalized_name TEXT, is_government INTEGER, resolution_status TEXT,
    first_seen_at TEXT, last_seen_at TEXT, mention_count INTEGER,
    created_at TEXT, updated_at TEXT,
    UNIQUE (normalized_name, entity_type))
;
CREATE TABLE entity_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER, source_type TEXT,
    source_id INTEGER, mention_text TEXT, context_snippet TEXT,
    confidence INTEGER, extracted_by TEXT, role_in_context TEXT, created_at TEXT)
;
CREATE TABLE _sweep_docs_watermark (
    last_run_at TEXT, last_processed_id INTEGER, docs_processed INTEGER,
    entities_created INTEGER, mentions_created INTEGER)
"""


def participants_engine():
    return sqlite_engine(PARTICIPANTS_SCHEMA)


def sweep_engine():
    return sqlite_engine(SWEEP_SCHEMA)


def count(engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


# ===========================================================================
# 1. run_sweep_docs live-mode startup: no obsolete-method failure
# ===========================================================================


def test_run_sweep_docs_live_startup_has_no_obsolete_method_failure():
    from scripts.entities.sweep_docs import run_sweep_docs

    engine = sweep_engine()
    result = run_sweep_docs(engine, dry_run=False)
    assert result["success"] is True
    assert "validation_receipt" in result
    receipt = result["validation_receipt"]
    assert receipt["producer"] == "sweep_docs"
    assert receipt["state"] == STATE_SEALED
    # The retired writes_started() method is gone from the validator entirely.
    assert not hasattr(EmissionValidator("x", "1"), "writes_started")
    assert reconcile_receipts([receipt], expected_producers=["sweep_docs"]) == []


# ===========================================================================
# 2. run_sweep_docs dry mode validates nonempty candidates, mutates nothing
# ===========================================================================


def test_run_sweep_docs_dry_mode_validates_candidates_and_writes_nothing(monkeypatch):
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO supporting_documents (id, document_title, text_content, "
            "swept_at) VALUES (1, 'doc', 'Applicant: Dana Reyes', NULL)"
        ))

    monkeypatch.setattr(
        sweep_docs, "extract_entities_from_doc",
        lambda text_value: [
            {"name": "Dana Reyes", "normalized": "dana reyes",
             "entity_type": "person", "role": "applicant", "confidence": 80},
        ],
    )

    result = sweep_docs.run_sweep_docs(engine, dry_run=True)

    receipt = result["validation_receipt"]
    assert receipt["dry_run"] is True
    # A complete mention bundle validates five registry categories.
    assert receipt["values"]["attempted"] == 5
    assert receipt["values"]["accepted"] == 5
    # ...and nothing was mutated.
    assert receipt["rows"]["committed"] == 0
    assert receipt["rows"]["rolled_back"] == 0
    assert count(engine, "entities") == 0
    assert count(engine, "entity_mentions") == 0
    assert reconcile_receipts([receipt], expected_producers=["sweep_docs"]) == []


def test_run_sweep_docs_dry_mode_reports_invalid_candidates(monkeypatch):
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO supporting_documents (id, document_title, text_content, "
            "swept_at) VALUES (1, 'doc', 'text', NULL)"
        ))

    monkeypatch.setattr(
        sweep_docs, "extract_entities_from_doc",
        lambda text_value: [
            {"name": "X", "normalized": "x", "entity_type": "ghost_type",
             "role": "applicant", "confidence": 50},
        ],
    )

    result = sweep_docs.run_sweep_docs(engine, dry_run=True)
    receipt = result["validation_receipt"]
    assert result["success"] is False
    assert receipt["values"]["rejected"] == 1
    assert receipt["values"]["accepted"] == 0
    assert "ghost_type" in receipt["rejections"][0]["reason"]


# ===========================================================================
# 3. run_sweep_docs batch exception fails the phase
# ===========================================================================


def test_run_sweep_docs_batch_exception_returns_failed_receipt(monkeypatch):
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO supporting_documents (id, document_title, text_content, "
            "swept_at) VALUES (1, 'doc', 'text', NULL)"
        ))

    def boom(*args, **kwargs):
        raise RuntimeError("batch exploded")

    monkeypatch.setattr(sweep_docs, "process_batch", boom)

    result = sweep_docs.run_sweep_docs(engine, dry_run=False)

    assert result["success"] is False
    assert "batch exploded" in result["error"]
    receipt = result["validation_receipt"]
    assert receipt["failure"] is not None
    assert "batch exploded" in receipt["failure"]
    problems = reconcile_receipts([receipt], expected_producers=["sweep_docs"])
    assert any("run failed" in problem for problem in problems)


# ===========================================================================
# 4. event zero-write replay: proposed == replay_noop and reconciles
# ===========================================================================


def test_event_zero_write_replay_classifies_as_replay_noop():
    engine = participants_engine()
    # First run inserts the row.
    with engine.begin() as connection:
        assert _insert_participants(connection, [(1, 10, "presenter", 0.9)]) == 1

    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    proposals = [(1, 10, "presenter", 0.9)]
    emit_validated(validator, "role", proposals,
                   to_value=lambda candidate: candidate[2])
    validator.complete_validation()
    # The replay mutates nothing: every proposed row is a replay no-op.
    validator.classify_rows(replay_noop=len(proposals))
    validator.begin_writes()
    validator.commit(0)
    receipt = validator.seal()

    assert receipt.rows_proposed == 1
    assert receipt.rows_replay_noop == 1
    assert receipt.rows_would_insert == 0
    assert receipt.rows_committed == 0
    assert receipt.classification_reconciles
    assert receipt.mutation_reconciles
    assert count(engine, "event_participants") == 1
    assert reconcile_receipts([receipt], expected_producers=["event_pipeline"]) == []


def test_event_write_then_replay_reconciles_across_batches():
    engine = participants_engine()
    validator = EmissionValidator("event_pipeline", "1")

    # Batch 1: a real insert.
    validator.start_batch()
    emit_validated(validator, "role", [(1, 10, "presenter", 0.9)],
                   to_value=lambda candidate: candidate[2])
    validator.complete_validation()
    validator.classify_rows(would_insert=1)
    validator.begin_writes()
    with engine.begin() as connection:
        _insert_participants(connection, [(1, 10, "presenter", 0.9)])
    validator.commit(1)

    # Batch 2: the same row again — a replay no-op.
    validator.start_batch()
    emit_validated(validator, "role", [(1, 10, "presenter", 0.9)],
                   to_value=lambda candidate: candidate[2])
    validator.complete_validation()
    validator.classify_rows(replay_noop=1)
    validator.begin_writes()
    validator.commit(0)

    receipt = validator.seal()
    assert receipt.rows_proposed == 2
    assert receipt.rows_would_insert == 1
    assert receipt.rows_replay_noop == 1
    assert receipt.rows_committed == 1
    assert receipt.classification_reconciles and receipt.mutation_reconciles
    assert reconcile_receipts([receipt], expected_producers=["event_pipeline"]) == []


# ===========================================================================
# 5. deliberately inflated replay_noop fails reconciliation
# ===========================================================================


def _payload(**row_overrides):
    """A hand-built sealed receipt, for cases a validator cannot even produce."""
    rows = {"proposed": 1, "would_insert": 1, "would_update": 0,
            "replay_noop": 0, "unresolved": 0,
            "committed": 1, "rolled_back": 0}
    rows.update(row_overrides)
    return {
        "producer": "event_pipeline", "producer_version": "1",
        "model_version": "kg-model/1.0", "registry_snapshot": None,
        "state": "sealed", "dry_run": False, "failure": None,
        "values": {"attempted": 1, "accepted": 1, "rejected": 0},
        "rows": rows, "observed": {}, "rejections": [],
        "derived_excluded": 0, "derived_exclusion_reasons": [],
    }


def test_inflated_replay_noop_fails_reconciliation():
    """An overcounted classification must fail, not merely an undercount."""
    from scripts.kg import registries as r

    payload = _payload(proposed=1, would_insert=0, replay_noop=5, committed=0)
    payload["registry_snapshot"] = r.snapshot_sha256()
    problems = reconcile_receipts([payload], expected_producers=["event_pipeline"])
    assert any("classification does not reconcile exactly" in p for p in problems)


def test_undercount_of_proposed_rows_fails_reconciliation():
    from scripts.kg import registries as r

    payload = _payload(proposed=1, would_insert=3, committed=1)
    payload["registry_snapshot"] = r.snapshot_sha256()
    problems = reconcile_receipts([payload], expected_producers=["event_pipeline"])
    assert any("mutation does not reconcile exactly" in p for p in problems)


# ===========================================================================
# 6. rollback keeps cumulative totals stable and explains the mutation
# ===========================================================================


def test_rollback_keeps_cumulative_proposed_stable():
    validator = EmissionValidator("event_pipeline", "1")

    # Batch 1 commits.
    validator.start_batch()
    emit_validated(validator, "role", [(1, 10, "presenter", 0.9)],
                   to_value=lambda candidate: candidate[2])
    validator.complete_validation()
    validator.classify_rows(would_insert=1)
    validator.begin_writes()
    validator.commit(1)
    proposed_after_batch_one = validator.receipt.rows_proposed

    # Batch 2 is attempted then rolled back.
    validator.start_batch()
    emit_validated(validator, "role", [(2, 20, "staff", 0.8)],
                   to_value=lambda candidate: candidate[2])
    validator.complete_validation()
    validator.classify_rows(would_insert=1)
    validator.begin_writes()
    validator.rollback(1, "OperationalError: boom")

    receipt = validator.seal()
    # Cumulative proposed total is never rewritten by a rollback.
    assert receipt.rows_proposed == proposed_after_batch_one + 1
    assert receipt.rows_would_insert == 2
    assert receipt.rows_committed == 1
    assert receipt.rows_rolled_back == 1
    assert receipt.classification_reconciles
    assert receipt.mutation_reconciles
    assert receipt.failure == "OperationalError: boom"
    assert any(
        "run failed" in problem
        for problem in reconcile_receipts([receipt], expected_producers=["event_pipeline"])
    )


def test_rollback_leaves_no_rows_in_database():
    engine = participants_engine()
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    emit_validated(validator, "role", [(1, 10, "presenter", 0.9)],
                   to_value=lambda candidate: candidate[2])
    validator.complete_validation()
    validator.classify_rows(would_insert=1)
    try:
        with engine.begin() as connection:
            validator.begin_writes()
            _insert_participants(connection, [(1, 10, "presenter", 0.9)])
            raise RuntimeError("boom")
    except RuntimeError as error:
        validator.rollback(1, f"RuntimeError: {error}")
    receipt = validator.seal()
    assert count(engine, "event_participants") == 0
    assert receipt.rows_committed == 0
    assert receipt.rows_rolled_back == 1
    assert receipt.mutation_reconciles


# ===========================================================================
# Lifecycle guards and storage behaviour
# ===========================================================================


def test_commit_before_validation_completes_raises():
    with pytest.raises(BoundaryStateError):
        EmissionValidator("p", "1").commit(1)


def test_validation_after_completion_raises():
    validator = EmissionValidator("p", "1")
    validator.complete_validation()
    with pytest.raises(BoundaryStateError):
        validator.validate("role", "presenter")


def test_storage_returns_actual_row_counts():
    engine = participants_engine()
    with engine.begin() as connection:
        assert _insert_participants(connection, [(1, 10, "presenter", 0.9)]) == 1
    with engine.begin() as connection:
        assert _insert_participants(connection, [(1, 10, "presenter", 0.9)]) == 0
    with engine.begin() as connection:
        assert _upgrade_participants(connection, [(1, 10, "presenter", 0.99)]) == 1
    assert count(engine, "event_participants") == 1


def test_link_events_returns_one_sealed_receipt(monkeypatch):
    import scripts.entities.event_link as event_link

    monkeypatch.setattr(event_link, "_event_id_batch", lambda engine, cursor, size: [])
    stats = event_link.link_events(
        engine=None, entity_lookup=[], meeting_entity_lookup={}
    )
    receipt = stats["validation_receipt"]
    assert receipt["producer"] == "event_pipeline"
    assert receipt["state"] == STATE_SEALED
    assert reconcile_receipts([receipt], expected_producers=["event_pipeline"]) == []


def test_invalid_role_is_refused_before_any_write():
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    with pytest.raises(EmissionError):
        emit_validated(validator, "role", [(1, 10, "ghost_role", 0.9)],
                       to_value=lambda candidate: candidate[2])
    receipt = validator.seal()
    assert receipt.values_rejected == 1
    assert receipt.rows_proposed == 0


# ===========================================================================
# 1. would_update counted exactly once
# ===========================================================================


def test_would_update_is_counted_exactly_once():
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    validator.validate("role", "staff")
    validator.complete_validation()
    validator.classify_rows(would_update=4)
    validator.begin_writes()
    validator.commit(4)
    receipt = validator.seal()

    # Counted once in the field...
    assert receipt.rows_would_update == 4
    # ...and once as a component of the proposed total (not twice).
    assert receipt.rows_proposed == 4
    assert receipt.rows_proposed == (
        receipt.rows_would_insert
        + receipt.rows_would_update
        + receipt.rows_replay_noop
        + receipt.rows_unresolved
    )
    assert receipt.classification_reconciles
    assert receipt.mutation_reconciles
    assert reconcile_receipts([receipt], expected_producers=["event_pipeline"]) == []


def test_would_update_accumulates_across_batches_without_doubling():
    validator = EmissionValidator("event_pipeline", "1")
    for expected_total in (3, 7):
        validator.start_batch()
        validator.validate("role", "staff")
        validator.complete_validation()
        validator.classify_rows(would_update=3 if expected_total == 3 else 4)
        validator.begin_writes()
        validator.commit(3 if expected_total == 3 else 4)
    receipt = validator.seal()
    assert receipt.rows_would_update == 7  # 3 + 4, never 14
    assert receipt.rows_proposed == 7


# ===========================================================================
# 2. start_batch cannot abandon an unreconciled batch
# ===========================================================================


def test_start_batch_rejects_unreconciled_classification():
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    validator.complete_validation()
    validator.classify_rows(would_insert=2)
    validator.begin_writes()
    validator.commit(2)
    # Simulate an unclassified row sneaking into the totals.
    validator.receipt.rows_unresolved += 5
    with pytest.raises(BoundaryStateError) as error:
        validator.start_batch()
    assert "unreconciled" in str(error.value)


def test_start_batch_rejects_classified_but_uncommitted_live_batch():
    """A classified live batch that mutated nothing cannot be abandoned."""
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    validator.validate("role", "staff")
    validator.complete_validation()
    validator.classify_rows(would_insert=2)
    validator.begin_writes()
    with pytest.raises(BoundaryStateError) as error:
        validator.start_batch()
    message = str(error.value)
    assert "classified but unaccounted mutations" in message
    assert "committed 0" in message


def test_start_batch_rejects_dry_batch_that_committed_rows():
    validator = EmissionValidator("event_pipeline", "1", dry_run=True)
    validator.start_batch()
    validator.complete_validation()
    validator.classify_rows(would_insert=2)
    validator.receipt.rows_committed += 2  # simulate an illegal dry mutation
    with pytest.raises(BoundaryStateError) as error:
        validator.start_batch()
    assert "dry run mutated rows" in str(error.value)


def test_start_batch_accepts_a_fully_accounted_batch():
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    validator.validate("role", "staff")
    validator.complete_validation()
    validator.classify_rows(would_insert=2)
    validator.begin_writes()
    validator.commit(2)
    validator.start_batch()  # must not raise
    assert validator.state == "collecting"


# ===========================================================================
# 3. exact-identity reclassification (no count-based variant exists)
# ===========================================================================


def test_reclassify_assertion_requires_a_named_assertion():
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    validator.validate("role", "staff")
    validator.complete_validation()
    validator.begin_writes()
    with pytest.raises(TypeError):
        validator.reclassify_assertion(2)


def test_reclassify_assertion_moves_exactly_one_named_row():
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    validator.validate("role", "staff")
    validator.complete_validation()
    validator.classify_rows(would_insert=3)
    validator.begin_writes()
    validator.reclassify_assertion(EntityAssertion("acme llc", "organization"))
    validator.commit(2)
    receipt = validator.seal()
    assert receipt.rows_would_insert == 2
    assert receipt.rows_replay_noop == 1
    assert receipt.rows_proposed == 3
    assert receipt.classification_reconciles and receipt.mutation_reconciles
    assert receipt.reclassified_conflicts == [{
        "identity": ["acme llc", "organization"],
        "kind": "EntityAssertion",
        "reason": "",
    }]


def test_reclassify_assertion_without_a_pending_insert_raises():
    validator = EmissionValidator("event_pipeline", "1")
    validator.start_batch()
    validator.validate("role", "staff")
    validator.complete_validation()
    validator.classify_rows(replay_noop=1)
    validator.begin_writes()
    with pytest.raises(BoundaryStateError) as error:
        validator.reclassify_assertion(EntityAssertion("acme llc", "organization"))
    assert "classified no inserts" in str(error.value)
    assert validator.receipt.rows_would_insert == 0
    assert validator.receipt.rows_replay_noop == 1


# ===========================================================================
# 4/5/6. DB-backed sweep_docs matrix, dry/live agreement
# ===========================================================================


def _seed_docs(engine, texts):
    with engine.begin() as connection:
        for index, body in enumerate(texts, start=1):
            connection.execute(text(
                "INSERT INTO supporting_documents (id, document_title, "
                "text_content, text_extraction_method, swept_at) "
                "VALUES (:i, :t, :b, :m, NULL)"
            ), {"i": index, "t": f"doc{index}", "b": body, "m": "pymupdf"})


def _extractor(mapping):
    """Return an extractor keyed on document text."""
    def extract(body):
        return [dict(candidate) for candidate in mapping.get(body, [])]
    return extract


ONE_DOC_TWO_ROLES = {
    "alpha": [
        {"name": "Dana Reyes", "normalized": "dana reyes",
         "entity_type": "person", "role": "applicant", "confidence": 80},
    ],
    "beta": [
        {"name": "Dana Reyes", "normalized": "dana reyes",
         "entity_type": "person", "role": "staff", "confidence": 70},
    ],
}


def test_sweep_docs_live_insert_commits_rows(monkeypatch):
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    _seed_docs(engine, ["alpha"])
    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc",
                        _extractor(ONE_DOC_TWO_ROLES))

    result = sweep_docs.run_sweep_docs(engine, dry_run=False)
    receipt = result["validation_receipt"]
    assert result["success"] is True
    assert count(engine, "entities") == 1
    assert count(engine, "entity_mentions") == 1
    assert receipt["rows"]["would_insert"] == 2   # 1 entity + 1 mention
    assert receipt["rows"]["committed"] == 2
    assert receipt["rows"]["replay_noop"] == 0
    assert receipt["rows"]["classification_reconciles"] is True
    assert receipt["rows"]["mutation_reconciles"] is True
    assert reconcile_receipts([receipt], expected_producers=["sweep_docs"]) == []


def test_sweep_docs_unchanged_replay_writes_nothing(monkeypatch):
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    _seed_docs(engine, ["alpha"])
    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc",
                        _extractor(ONE_DOC_TWO_ROLES))

    first = sweep_docs.run_sweep_docs(engine, dry_run=False)["validation_receipt"]
    assert first["rows"]["committed"] == 2

    # Re-open the document so the second run sees it again.
    with engine.begin() as connection:
        connection.execute(text("UPDATE supporting_documents SET swept_at = NULL"))

    second = sweep_docs.run_sweep_docs(engine, dry_run=False)["validation_receipt"]
    assert count(engine, "entities") == 1
    assert count(engine, "entity_mentions") == 1
    # The replay proposes nothing: sweep_docs filters rows already present in the
    # entity cache and existing mentions *before* classification, so there is
    # nothing to classify and nothing to reconcile as a replay no-op.
    assert second["rows"]["proposed"] == 0
    assert second["rows"]["would_insert"] == 0
    assert second["rows"]["replay_noop"] == 0
    assert second["rows"]["committed"] == 0
    assert second["rows"]["rolled_back"] == 0
    assert second["rows"]["classification_reconciles"] is True
    assert second["rows"]["mutation_reconciles"] is True
    assert reconcile_receipts([second], expected_producers=["sweep_docs"]) == []


def test_sweep_docs_transaction_failure_after_classification_rolls_back(monkeypatch):
    """Failure after classification must roll back with exact accounting."""
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    _seed_docs(engine, ["alpha"])
    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc",
                        _extractor(ONE_DOC_TWO_ROLES))

    real_process_batch = sweep_docs.process_batch

    def failing_batch(conn, wm, entity_cache, dry_run=False, verbose=False,
                      validator=None):
        stats = real_process_batch(conn, wm, entity_cache, dry_run=dry_run,
                                   verbose=verbose, validator=validator)
        raise RuntimeError("write failed")

    monkeypatch.setattr(sweep_docs, "process_batch", failing_batch)
    result = sweep_docs.run_sweep_docs(engine, dry_run=False)

    receipt = result["validation_receipt"]
    assert result["success"] is False
    assert receipt["failure"] is not None
    rows = receipt["rows"]
    assert rows["would_insert"] == 2   # classified before the write
    assert rows["committed"] == 0      # the transaction rolled back
    assert rows["rolled_back"] == 2
    assert rows["classification_reconciles"] is True
    assert rows["mutation_reconciles"] is True
    assert count(engine, "entities") == 0
    assert count(engine, "entity_mentions") == 0


def test_sweep_docs_prohibited_role_fails_before_any_write(monkeypatch):
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    _seed_docs(engine, ["alpha"])
    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc", _extractor({
        "alpha": [{"name": "X", "normalized": "x", "entity_type": "person",
                   "role": "known_org", "confidence": 50}],
    }))

    result = sweep_docs.run_sweep_docs(engine, dry_run=False)
    receipt = result["validation_receipt"]
    assert result["success"] is False
    assert receipt["values"]["rejected"] == 1
    assert "known_org" in receipt["rejections"][0]["reason"]
    assert count(engine, "entities") == 0
    assert count(engine, "entity_mentions") == 0


def test_sweep_docs_dry_and_live_agree_on_mentions(monkeypatch):
    """One entity in two documents and two roles: dry == live proposed."""
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    _seed_docs(engine, ["alpha", "beta"])
    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc",
                        _extractor(ONE_DOC_TWO_ROLES))

    dry = sweep_docs.run_sweep_docs(engine, dry_run=True)["validation_receipt"]
    live = sweep_docs.run_sweep_docs(engine, dry_run=False)["validation_receipt"]

    # 1 entity + 2 mentions (same entity, different source and role).
    assert dry["rows"]["would_insert"] == 3
    assert live["rows"]["would_insert"] == 3
    assert dry["rows"]["proposed"] == live["rows"]["proposed"] == 3
    assert dry["rows"]["committed"] == 0
    assert live["rows"]["committed"] == 3
    assert count(engine, "entities") == 1
    assert count(engine, "entity_mentions") == 2


def test_dry_and_live_receipts_match_except_mutation(monkeypatch):
    """Identical fixtures: values and classification must match exactly."""
    import scripts.entities.sweep_docs as sweep_docs

    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc",
                        _extractor(ONE_DOC_TWO_ROLES))

    dry_engine = sweep_engine()
    _seed_docs(dry_engine, ["alpha", "beta"])
    dry = sweep_docs.run_sweep_docs(dry_engine, dry_run=True)["validation_receipt"]

    live_engine = sweep_engine()
    _seed_docs(live_engine, ["alpha", "beta"])
    live = sweep_docs.run_sweep_docs(live_engine, dry_run=False)["validation_receipt"]

    assert dry["values"] == live["values"]
    assert dry["observed"] == live["observed"]
    classification_keys = ("proposed", "would_insert", "would_update",
                           "replay_noop", "unresolved")
    for key in classification_keys:
        assert dry["rows"][key] == live["rows"][key], key

    # Only mutation and its applicability may differ.
    assert dry["rows"]["committed"] == 0
    assert live["rows"]["committed"] > 0
    assert dry["rows"]["mutation_reconciles"] is None
    assert live["rows"]["mutation_reconciles"] is True
    assert dry["rows"]["classification_reconciles"] is True
    assert live["rows"]["classification_reconciles"] is True


# ===========================================================================
# Typed emission bundles
# ===========================================================================


def _evidence_key(**overrides):
    """A Stage 3 evidence identity, not a second identity model."""
    kwargs = dict(source_type="supporting_document", source_id="1",
                  content_hash="hash-v1", extraction_method="source_pdf_text",
                  span_start=0, span_end=40)
    kwargs.update(overrides)
    return evidence_identity(**kwargs)


def _meeting_context_key():
    return civic_context_identity(
        context_class="meeting", source_system="poliscopic",
        context_id="2026-01-06-bos",
    )


def _context_key():
    """An agenda-item civic context, disambiguated by its parent meeting."""
    return civic_context_identity(
        context_class="agenda_item", source_system="poliscopic",
        context_id="item-1", parent=_meeting_context_key(),
    )


def _actor_key():
    return entity_candidate_identity(entity_type="person", surface_form="Dana Reyes")


def _case_key():
    return canonical_entity_identity(entity_id=42, entity_type="case")


def _adjudication_key():
    return adjudication_identity(
        adjudicator="pete", decision_id="adj-1",
        decided_at="2026-09-10T12:00:00-07:00",
    )


def _vote_key():
    """A vote identity: neither an evidence nor a context identity kind."""
    return vote_identity(
        context=_context_key(), actor=_actor_key(), motion="approve C-1",
        occurrence="1",
    )


def _mention_bundle(**overrides):
    base = dict(
        kind="mention", entity_type="person", role="mentioned",
        evidence_class="source_pdf_text", assertion_class="source_supported",
        context_class="agenda_item", context_identity=_context_key(),
        evidence_identity=_evidence_key(),
    )
    base.update(overrides)
    return EmissionBundle(**base)


# -- 1. reuse of the Stage 3 identity system --------------------------------


def test_bundle_reuses_stage3_evidence_identity():
    """There is one evidence-identity model, and bundles consume it."""
    import scripts.kg.emission_bundles as bundles

    assert not hasattr(bundles, "EvidenceIdentity")
    key = _evidence_key()
    assert key.kind == "evidence"
    bundle = _mention_bundle(evidence_identity=key)
    assert bundle.evidence_identity is key
    # A bundle's source presentation is derived from the authoritative key.
    assert bundle_source_reference(bundle) == key.canonical


def test_complete_mention_bundle_validates_every_component():
    validator = EmissionValidator("sweep_docs", "1")
    validated = validator.validate_bundle(_mention_bundle())
    assert validated["entity_type"] == "person"
    assert validated["role"] == "mentioned"
    assert validated["evidence_class"] == "source_pdf_text"
    assert validated["assertion_class"] == "source_supported"
    assert validated["model_version"] == "kg-model/1.0"
    receipt = validator.seal()
    assert receipt.values_accepted == 5
    assert receipt.values_attempted == receipt.values_accepted + receipt.values_rejected


# -- 2. wrong identity kind in each identity field ---------------------------


def test_wrong_identity_kind_is_refused_in_every_field():
    cases = (
        ("evidence_identity", _actor_key()),
        ("context_identity", _vote_key()),
        ("actor_identity", _evidence_key()),
    )
    for field_name, wrong_key in cases:
        validator = EmissionValidator("sweep_docs", "1")
        with pytest.raises(EmissionError) as error:
            validator.validate_bundle(_mention_bundle(**{field_name: wrong_key}))
        assert "identity" in str(error.value)
        receipt = validator.seal()
        assert receipt.values_accepted == 0
        assert receipt.values_rejected == 1


def test_context_class_must_agree_with_identity_kind():
    """An evidence key with context_class="agenda_item" must fail."""
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(_mention_bundle(
            context_class="agenda_item",
            context_identity=_evidence_key(),
        ))
    assert "requires a context identity" in str(error.value)
    receipt = validator.seal()
    assert receipt.values_accepted == 0
    assert receipt.values_rejected == 1
    assert receipt.observed == {}


def test_evidence_key_with_evidence_context_class_passes():
    validator = EmissionValidator("sweep_docs", "1")
    validated = validator.validate_bundle(_mention_bundle(
        role="mentioned", context_class="evidence",
        context_identity=_evidence_key(),
    ))
    assert validated["role"] == "mentioned"


def test_civic_context_key_with_agenda_item_class_passes():
    validator = EmissionValidator("sweep_docs", "1")
    validated = validator.validate_bundle(_mention_bundle(
        context_class="agenda_item", context_identity=_context_key(),
    ))
    assert validated["role"] == "mentioned"


def test_case_context_accepts_a_canonical_entity_identity():
    validator = EmissionValidator("sweep_docs", "1")
    validated = validator.validate_bundle(_mention_bundle(
        role="owner", context_class="case",
        context_identity=canonical_entity_identity(entity_id=42, entity_type="case"),
    ))
    assert validated["role"] == "owner"


def test_wrong_identity_kind_for_relationship_endpoints_is_refused():
    validator = EmissionValidator("graph_builder", "1")
    with pytest.raises(EmissionError):
        validator.validate_bundle(EmissionBundle(
            kind="relationship", relationship="APPLIED_FOR",
            evidence_class="structured_record", from_class="person",
            to_class="case", from_identity=_evidence_key(),  # wrong kind
            to_identity=_case_key(), evidence_identity=_evidence_key(),
        ))
    assert validator.seal().values_accepted == 0


# -- 3. derived requires model version too ----------------------------------


def test_derived_bundle_requires_model_version():
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(_mention_bundle(
            assertion_class="derived", evidence_identity=None,
            evidence_class=None, model_version="",
            derived_inputs=(_context_key(),),
        ))
    assert "model_version" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_derived_bundle_validates_with_typed_inputs():
    validator = EmissionValidator("event_pipeline", "1")
    validated = validator.validate_bundle(_mention_bundle(
        assertion_class="derived", evidence_identity=None, evidence_class=None,
        derived_inputs=(_context_key(), _actor_key()),
    ))
    assert validated["assertion_class"] == "derived"
    assert validated["model_version"] == "kg-model/1.0"


# -- 9. derived inputs must be typed ----------------------------------------


def test_derived_inputs_must_be_typed_identity_keys():
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(_mention_bundle(
            assertion_class="derived", evidence_identity=None,
            evidence_class=None, derived_inputs=("meeting:2026-01-06",),
        ))
    assert "must be a typed identity key" in str(error.value)
    assert validator.seal().values_accepted == 0


# -- 4. human validation needs decided_at -----------------------------------


def test_human_validation_requires_decided_at():
    """The builder itself refuses an incomplete adjudication."""
    with pytest.raises(identity.IdentityError):
        adjudication_identity(adjudicator="pete", decision_id="adj-1", decided_at="")
    with pytest.raises(identity.IdentityError):
        adjudication_identity(adjudicator="", decision_id="adj-1",
                              decided_at="2026-09-10T12:00:00-07:00")


def test_human_validation_rejects_a_non_adjudication_key():
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(_mention_bundle(
            assertion_class="human_validated",
            adjudication_identity=_actor_key(),
        ))
    assert "adjudication identity" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_human_validation_validates_when_complete():
    validator = EmissionValidator("sweep_docs", "1")
    validated = validator.validate_bundle(_mention_bundle(
        assertion_class="human_validated",
        adjudication_identity=_adjudication_key(),
    ))
    assert validated["assertion_class"] == "human_validated"


# -- 5. quarantine reason must be registered ---------------------------------


def test_unregistered_quarantine_reason_is_refused():
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(_mention_bundle(
            assertion_class="quarantined",
            quarantine_reason="because I said so",
        ))
    assert "not registered" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_registered_quarantine_reason_validates():
    validator = EmissionValidator("sweep_docs", "1")
    validated = validator.validate_bundle(_mention_bundle(
        assertion_class="quarantined", quarantine_reason="unregistered_value",
    ))
    assert validated["assertion_class"] == "quarantined"


def test_quarantined_bundle_retains_evidence_identity():
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(_mention_bundle(
            assertion_class="quarantined", evidence_identity=None,
            evidence_class=None, quarantine_reason="unregistered_value",
        ))
    assert "requires an evidence identity" in str(error.value)


# -- 6. cross-field rules ----------------------------------------------------


def test_approval_event_paired_with_denied_outcome_is_refused():
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="event", event_type="decision.approval",
            outcome="denied", evidence_class="minutes_or_summary",
            context_identity=_context_key(), evidence_identity=_evidence_key(),
        ))
    assert "not compatible with event type" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_event_bundle_validates_type_and_outcome_when_compatible():
    validator = EmissionValidator("event_pipeline", "1")
    validated = validator.validate_bundle(EmissionBundle(
        kind="event", event_type="decision.approval",
        outcome="approved_with_conditions",
        evidence_class="minutes_or_summary",
        context_identity=_context_key(), evidence_identity=_evidence_key(),
    ))
    assert validated["event_type"] == "approval"
    # The canonical base and the qualifier are recorded separately; the raw
    # qualified form is never certified as the canonical observed outcome.
    assert validated["outcome"] == "approved"
    assert validated["outcome_qualifier"] == "with_conditions"


def test_event_bundle_registers_base_and_qualifier_as_separate_observations():
    validator = EmissionValidator("event_pipeline", "1")
    validator.validate_bundle(EmissionBundle(
        kind="event", event_type="decision.approval",
        outcome="approved_with_conditions",
        evidence_class="minutes_or_summary",
        context_identity=_context_key(), evidence_identity=_evidence_key(),
    ))
    receipt = validator.seal()
    assert receipt.observed["outcome"] == {"approved": 1}
    assert receipt.observed["outcome_qualifier"] == {"with_conditions": 1}
    # The raw qualified form must never be certified as the outcome.
    assert "approved_with_conditions" not in receipt.observed["outcome"]


def test_event_bundle_accepts_an_explicit_base_and_qualifier_pair():
    validator = EmissionValidator("event_pipeline", "1")
    validated = validator.validate_bundle(EmissionBundle(
        kind="event", event_type="decision.denial",
        outcome="denied", outcome_qualifier="without_prejudice",
        evidence_class="minutes_or_summary",
        context_identity=_context_key(), evidence_identity=_evidence_key(),
    ))
    assert validated["outcome"] == "denied"
    assert validated["outcome_qualifier"] == "without_prejudice"
    assert validated["event_type"] == "denial"


def test_event_type_compatibility_uses_the_canonical_base():
    # approved_with_conditions canonicalises to approved, which decision.approval
    # permits; compatibility is decided by the base, not the raw form.
    validator = EmissionValidator("event_pipeline", "1")
    validated = validator.validate_bundle(EmissionBundle(
        kind="event", event_type="decision.approval",
        outcome="approved_with_conditions",
        evidence_class="minutes_or_summary",
        context_identity=_context_key(), evidence_identity=_evidence_key(),
    ))
    assert validated["event_type"] == "approval"

    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="event", event_type="decision.denial",
            outcome="approved_with_conditions",
            evidence_class="minutes_or_summary",
            context_identity=_context_key(), evidence_identity=_evidence_key(),
        ))
    assert "not compatible with event type" in str(error.value)


def test_event_bundle_rejects_approved_with_without_prejudice():
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="event", event_type="decision.approval",
            outcome="approved", outcome_qualifier="without_prejudice",
            evidence_class="minutes_or_summary",
            context_identity=_context_key(), evidence_identity=_evidence_key(),
        ))
    assert "does not permit qualifier" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_event_bundle_rejects_a_qualifier_without_a_base():
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="event", event_type="decision.approval",
            outcome_qualifier="with_conditions",
            evidence_class="minutes_or_summary",
            context_identity=_context_key(), evidence_identity=_evidence_key(),
        ))
    assert "without a base outcome" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_event_bundle_rejects_an_unknown_outcome_form():
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError):
        validator.validate_bundle(EmissionBundle(
            kind="event", event_type="decision.approval",
            outcome="ghost_outcome",
            evidence_class="minutes_or_summary",
            context_identity=_context_key(), evidence_identity=_evidence_key(),
        ))
    assert validator.seal().values_accepted == 0


def test_event_bundle_rejects_a_contradictory_qualifier():
    # The raw form carries its own qualifier; declaring a different one conflicts.
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="event", event_type="decision.approval",
            outcome="approved_with_conditions",
            outcome_qualifier="with_stipulations",
            evidence_class="minutes_or_summary",
            context_identity=_context_key(), evidence_identity=_evidence_key(),
        ))
    assert "contradicts the declared qualifier" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_outcome_bundle_failure_is_atomic():
    """One bundle rejection and zero accepted observations."""
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError):
        validator.validate_bundle(EmissionBundle(
            kind="event", event_type="decision.approval",
            outcome="approved", outcome_qualifier="without_prejudice",
            evidence_class="minutes_or_summary",
            context_identity=_context_key(), evidence_identity=_evidence_key(),
        ))
    receipt = validator.seal()
    assert receipt.values_accepted == 0
    assert receipt.observed == {}
    assert len(receipt.rejections) == 1
    assert receipt.rejections[0].category == "bundle:event"


def test_relationship_with_disallowed_evidence_class_is_refused():
    validator = EmissionValidator("graph_builder", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="relationship", relationship="APPLIED_FOR",
            evidence_class="minutes_or_summary",  # not allowed for APPLIED_FOR
            from_class="person", to_class="case",
            from_identity=_actor_key(), to_identity=_case_key(),
            evidence_identity=_evidence_key(),
        ))
    assert "does not allow evidence class" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_missing_context_class_despite_context_identity_is_refused():
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(_mention_bundle(context_class=None))
    assert "context class" in str(error.value)
    assert validator.seal().values_accepted == 0


def test_relationship_requires_both_endpoint_identities_and_classes():
    validator = EmissionValidator("graph_builder", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="relationship", relationship="APPLIED_FOR",
            evidence_class="structured_record",
            from_identity=_actor_key(), to_identity=_case_key(),
            evidence_identity=_evidence_key(),
        ))
    assert "from_class and to_class" in str(error.value)


def test_relationship_bundle_validates_when_complete():
    validator = EmissionValidator("graph_builder", "1")
    validated = validator.validate_bundle(EmissionBundle(
        kind="relationship", relationship="APPLIED_FOR",
        evidence_class="structured_record", assertion_class="source_supported",
        from_class="person", to_class="case",
        from_identity=_actor_key(), to_identity=_case_key(),
        evidence_identity=_evidence_key(),
    ))
    assert validated["relationship"] == "APPLIED_FOR"


def test_relationship_bundle_cannot_bypass_domain_range():
    validator = EmissionValidator("graph_builder", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="relationship", relationship="APPLIED_FOR",
            evidence_class="structured_record", from_class="case",
            to_class="person", from_identity=_case_key(),
            to_identity=_actor_key(), evidence_identity=_evidence_key(),
        ))
    assert "does not allow" in str(error.value)


# -- 7. atomicity ------------------------------------------------------------


def test_bundle_atomic_late_failure_commits_nothing():
    """An early valid component followed by a late invalid one accepts nothing."""
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(_mention_bundle(
            entity_type="person",             # valid, validated first
            role="mentioned",                  # valid
            evidence_class="ghost_evidence",   # invalid, validated late
        ))
    assert "evidence_class" in str(error.value)
    receipt = validator.seal()
    assert receipt.values_accepted == 0
    assert receipt.values_rejected == 1
    assert len(receipt.rejections) == 1
    assert receipt.rejections[0].category == "bundle:mention"
    assert receipt.observed == {}
    assert receipt.values_reconcile


def test_bundle_structural_failure_records_one_rejection_and_no_vocabulary():
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError):
        validator.validate_bundle(_mention_bundle(evidence_identity=None))
    receipt = validator.seal()
    assert receipt.values_accepted == 0
    assert receipt.values_rejected == 1
    assert receipt.observed == {}


def test_valid_role_does_not_imply_a_valid_assertion():
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError):
        validator.validate_bundle(_mention_bundle(
            role="mentioned", evidence_identity=None,
        ))
    receipt = validator.seal()
    assert receipt.values_accepted == 0
    assert receipt.values_rejected == 1


def test_bundle_with_prohibited_value_is_refused_whole():
    validator = EmissionValidator("sweep_docs", "1")
    with pytest.raises(EmissionError):
        validator.validate_bundle(_mention_bundle(role="known_org"))
    receipt = validator.seal()
    assert receipt.values_accepted == 0
    assert receipt.values_rejected == 1
    assert "known_org" in receipt.rejections[0].reason


def test_participation_bundle_rejects_unsupported_promotion():
    validator = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        validator.validate_bundle(EmissionBundle(
            kind="participation", role="participant",
            participation_basis="agenda_listing", promotion="attendance",
            evidence_class="structured_record", context_class="event",
            actor_identity=_actor_key(), context_identity=_context_key(),
            evidence_identity=_evidence_key(),
        ))
    assert "cannot support promotion attendance" in str(error.value)


# -- civic context identity (item 1) -----------------------------------------


def test_civic_context_identity_shape():
    meeting = _meeting_context_key()
    assert meeting.kind == "context"
    parts = dict(meeting.parts)
    assert parts["context_class"] == "meeting"
    assert parts["source_system"] == "poliscopic"
    assert parts["context_id"] == "2026-01-06-bos"
    # A subitem is disambiguated by its parent context.
    item = _context_key()
    assert item.kind == "context"
    assert dict(item.parts)["parent"] == meeting.digest


def test_civic_context_identity_rejects_unregistered_class():
    with pytest.raises(identity.IdentityError):
        civic_context_identity(
            context_class="spaceship", source_system="poliscopic", context_id="1",
        )


def test_civic_context_parent_must_be_a_context_key():
    with pytest.raises(identity.IdentityError):
        civic_context_identity(
            context_class="agenda_item", source_system="poliscopic",
            context_id="item-1", parent=_evidence_key(),
        )


def test_civic_context_identity_requires_source_and_id():
    with pytest.raises(identity.IdentityError):
        civic_context_identity(
            context_class="meeting", source_system="", context_id="1",
        )


# -- canonical entity identity (item 2) --------------------------------------


def test_canonical_entity_identity_is_its_own_kind():
    key = canonical_entity_identity(entity_id=42, entity_type="case")
    assert key.kind == "canonical_entity"
    assert dict(key.parts)["entity_id"] == "42"


def test_canonical_entity_identity_usable_as_actor_without_masquerading():
    validator = EmissionValidator("event_pipeline", "1")
    validated = validator.validate_bundle(EmissionBundle(
        kind="participation", role="staff",
        participation_basis="observed_attendance",
        evidence_class="structured_record", context_class="meeting",
        actor_identity=canonical_entity_identity(entity_id=7, entity_type="person"),
        context_identity=_meeting_context_key(),
        evidence_identity=_evidence_key(),
    ))
    assert validated["role"] == "staff"


def test_canonical_entity_identity_requires_a_registered_type():
    with pytest.raises(identity.IdentityError):
        canonical_entity_identity(entity_id=1, entity_type="recommendation")


# -- adjudication identity (item 3) ------------------------------------------


def test_adjudication_identity_is_its_own_kind():
    key = _adjudication_key()
    assert key.kind == "adjudication"
    assert dict(key.parts)["decided_at"] == "2026-09-10T12:00:00-07:00"


def test_bundle_module_has_no_second_adjudication_model():
    import scripts.kg.emission_bundles as bundles

    assert not hasattr(bundles, "AdjudicationIdentity")


# -- entity endpoints (item 5) -----------------------------------------------


def test_evidence_event_and_vote_keys_are_rejected_as_entity_endpoints():
    event_key = event_identity(
        event_type="approval", context=_context_key(), occurrence="1",
    )
    for wrong in (_evidence_key(), event_key, _vote_key()):
        validator = EmissionValidator("graph_builder", "1")
        with pytest.raises(EmissionError) as error:
            validator.validate_bundle(EmissionBundle(
                kind="relationship", relationship="APPLIED_FOR",
                evidence_class="structured_record", from_class="person",
                to_class="case", from_identity=wrong, to_identity=_case_key(),
                evidence_identity=_evidence_key(),
            ))
        assert "must be a canonical_entity or entity_candidate identity" in str(
            error.value
        )
        receipt = validator.seal()
        assert receipt.values_accepted == 0
        assert receipt.values_rejected == 1
        assert receipt.observed == {}


def test_entity_candidate_is_still_accepted_as_an_endpoint():
    validator = EmissionValidator("graph_builder", "1")
    validated = validator.validate_bundle(EmissionBundle(
        kind="relationship", relationship="APPLIED_FOR",
        evidence_class="structured_record", from_class="person",
        to_class="case", from_identity=_actor_key(), to_identity=_case_key(),
        evidence_identity=_evidence_key(),
    ))
    assert validated["relationship"] == "APPLIED_FOR"


# -- changed-document identity (item 8) --------------------------------------


def test_changed_document_at_same_source_yields_a_new_evidence_identity():
    before = _evidence_key(content_hash="hash-v1")
    after = _evidence_key(content_hash="hash-v2")
    assert before.digest != after.digest
    before_parts, after_parts = dict(before.parts), dict(after.parts)
    assert before_parts["source_id"] == after_parts["source_id"]
    assert before_parts["content_hash"] != after_parts["content_hash"]

    validator = EmissionValidator("sweep_docs", "1")
    first = _mention_bundle(evidence_identity=before)
    second = _mention_bundle(evidence_identity=after)
    assert bundle_source_reference(first) != bundle_source_reference(second)
    assert validator.validate_bundle(first)["role"] == "mentioned"
    assert validator.validate_bundle(second)["role"] == "mentioned"


def test_extraction_method_alone_is_not_a_content_version():
    """The builder refuses before the bundle check ever runs."""
    with pytest.raises(identity.IdentityError) as error:
        evidence_identity(
            source_type="supporting_document", source_id="1",
            extraction_method="source_ocr",
        )
    assert "content_hash" in str(error.value)


def test_url_without_content_hash_fails():
    with pytest.raises(identity.IdentityError) as error:
        evidence_identity(
            source_type="supporting_document", source_id="1",
            url="https://example.test/a.pdf",
        )
    assert "content_hash" in str(error.value)


def test_url_plus_extraction_method_without_content_hash_fails():
    with pytest.raises(identity.IdentityError) as error:
        evidence_identity(
            source_type="supporting_document", source_id="1",
            url="https://example.test/a.pdf", extraction_method="pdftotext",
        )
    assert "content_hash" in str(error.value)


# -- extraction-method inventory (repository-derived) -------------------------


def _repo_source(relative: str) -> str:
    """Read a repository source file as text (no imports, no side effects)."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / relative).read_text(encoding="utf-8")


def _emitted_success_methods() -> set[str]:
    """Successful methods the extraction cascade actually returns."""
    import re

    source = _repo_source("scripts/docs/extract.py")
    return set(re.findall(r'return\s+[^,\n]+,\s*"([^"]+)"', source))


def _declared_failure_methods() -> set[str]:
    """Failure methods declared by the repository's canonical constant."""
    import re

    source = _repo_source("scripts/docs/doc_constants.py")
    match = re.search(r"FAILURE_METHODS\s*=\s*\(([^)]*)\)", source, re.S)
    assert match is not None, "FAILURE_METHODS not found in doc_constants.py"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_successful_methods_emitted_by_extract_py_all_map_correctly():
    """Derived from the cascade itself, never from a copied expected list."""
    from scripts.kg.registries.evidence import (
        EXTRACTION_METHOD_EVIDENCE_CLASSES,
        evidence_class_for_extraction_method,
    )

    emitted = _emitted_success_methods()
    assert emitted, "extract.py must expose its successful method literals"
    for method in sorted(emitted):
        assert method in EXTRACTION_METHOD_EVIDENCE_CLASSES, (
            f"extract.py emits {method!r} with no mapping"
        )
        assert (
            evidence_class_for_extraction_method(method)
            == EXTRACTION_METHOD_EVIDENCE_CLASSES[method]
        )
    # The cascade's PDF readers are PDF text; its OCR paths are OCR.
    assert EXTRACTION_METHOD_EVIDENCE_CLASSES["pymupdf"] == "source_pdf_text"
    assert EXTRACTION_METHOD_EVIDENCE_CLASSES["pdftotext"] == "source_pdf_text"
    for method in ("ocr_local", "ocr_windows", "ocr_windows_paddle"):
        assert EXTRACTION_METHOD_EVIDENCE_CLASSES[method] == "source_ocr"


def test_declared_failure_methods_remain_unmappable():
    from scripts.kg.registries.evidence import (
        FAILED_EXTRACTION_METHODS,
        evidence_class_for_extraction_method,
    )

    declared = _declared_failure_methods()
    assert declared, "doc_constants.FAILURE_METHODS must be discoverable"
    for method in sorted(declared | set(FAILED_EXTRACTION_METHODS)):
        assert evidence_class_for_extraction_method(method) is None, method
    assert evidence_class_for_extraction_method("pdftotext-failed") is None


def test_quarantine_and_reject_prefixes_remain_unmappable():
    from scripts.kg.registries.evidence import (
        QUARANTINE_METHOD_PREFIX,
        REJECT_METHOD_PREFIX,
        evidence_class_for_extraction_method,
    )

    for suffix in (
        "oversized:123", "unknown_domain:example.test", "high_page_count:9",
        "pdf_parse_error:bad", "no_domain", "empty",
    ):
        assert evidence_class_for_extraction_method(
            QUARANTINE_METHOD_PREFIX + suffix
        ) is None
        assert evidence_class_for_extraction_method(
            REJECT_METHOD_PREFIX + suffix
        ) is None
    # Empty and absent methods are unmappable too.
    assert evidence_class_for_extraction_method("") is None
    assert evidence_class_for_extraction_method(None) is None


def test_no_speculative_mapping_survives_without_a_cited_writer():
    from scripts.kg.registries.evidence import (
        EXTRACTION_METHOD_EVIDENCE_CLASSES,
        EXTRACTION_METHOD_WRITERS,
    )

    assert set(EXTRACTION_METHOD_EVIDENCE_CLASSES) == set(EXTRACTION_METHOD_WRITERS)
    for method, citation in EXTRACTION_METHOD_WRITERS.items():
        assert ".py:" in citation, f"{method} must cite a file:line writer"


def test_speculative_aliases_are_removed():
    from scripts.kg.registries.evidence import EXTRACTION_METHOD_EVIDENCE_CLASSES

    for alias in ("pdf_text", "text_layer", "ocr", "tesseract", "html",
                  "api", "structured", "structured_record"):
        assert alias not in EXTRACTION_METHOD_EVIDENCE_CLASSES, alias


def test_mapping_values_are_registered_evidence_classes():
    from scripts.kg.registries.evidence import (
        EVIDENCE_CLASSES,
        EXTRACTION_METHOD_EVIDENCE_CLASSES,
    )

    for method, evidence_class in EXTRACTION_METHOD_EVIDENCE_CLASSES.items():
        assert evidence_class in EVIDENCE_CLASSES, (method, evidence_class)


# -- complete civic context chain --------------------------------------------


def test_complete_civic_context_chain():
    jurisdiction = civic_context_identity(
        context_class="jurisdiction", source_system="poliscopic",
        context_id="maricopa-county",
    )
    body = civic_context_identity(
        context_class="body", source_system="poliscopic",
        context_id="bos", parent=jurisdiction,
    )
    meeting = civic_context_identity(
        context_class="meeting", source_system="poliscopic",
        context_id="2026-01-06", parent=body,
    )
    item = civic_context_identity(
        context_class="agenda_item", source_system="poliscopic",
        context_id="item-1", parent=meeting,
    )
    subitem = civic_context_identity(
        context_class="agenda_subitem", source_system="poliscopic",
        context_id="item-1a", parent=item,
    )
    chain = [jurisdiction, body, meeting, item, subitem]
    assert all(key.kind == "context" for key in chain)
    assert len({key.digest for key in chain}) == 5
    for child, parent in zip(chain[1:], chain):
        assert dict(child.parts)["parent"] == parent.digest
    # The same subitem id under a different parent is a different context.
    other_subitem = civic_context_identity(
        context_class="agenda_subitem", source_system="poliscopic",
        context_id="item-1a", parent=meeting,
    )
    assert other_subitem.digest != subitem.digest


# -- mention permission must not widen participation -------------------------


def test_role_valid_as_evidence_mention_is_rejected_for_participation():
    """``applicant`` may label a mention in evidence context, nothing more."""
    mentions = EmissionValidator("sweep_docs", "1")
    validated = mentions.validate_bundle(
        _mention_bundle(
            role="applicant", context_class="evidence",
            context_identity=_evidence_key(),
        )
    )
    assert validated["role"] == "applicant"

    participation = EmissionValidator("event_pipeline", "1")
    with pytest.raises(EmissionError) as error:
        participation.validate_bundle(EmissionBundle(
            kind="participation", role="applicant",
            participation_basis="agenda_listing",
            evidence_class="structured_record", context_class="body",
            actor_identity=_actor_key(),
            context_identity=civic_context_identity(
                context_class="body", source_system="poliscopic", context_id="bos",
            ),
            evidence_identity=_evidence_key(),
        ))
    assert "not permitted in context body" in str(error.value)
    receipt = participation.seal()
    assert receipt.values_accepted == 0
    assert receipt.observed == {}


# -- mentions create no participation and no relationships -------------------


def test_sweep_docs_mentions_create_no_participants_or_relationships(monkeypatch):
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    _seed_docs(engine, ["alpha"])
    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc",
                        _extractor(ONE_DOC_TWO_ROLES))
    result = sweep_docs.run_sweep_docs(engine, dry_run=False)
    receipt = result["validation_receipt"]

    assert result["success"] is True
    # No participation or relationship vocabulary was ever observed.
    assert "participation_basis" not in receipt["observed"]
    assert "relationship" not in receipt["observed"]
    assert set(receipt["observed"]) <= {
        "entity_type", "role", "assertion_class", "evidence_class",
        "model_version",
    }
    with engine.connect() as connection:
        tables = {
            row[0] for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "event_participants" not in tables
    assert "entity_relationships" not in tables


# ===========================================================================
# sweep_docs emits a canonical role for recognized organizations
# ===========================================================================


def test_sweep_docs_known_organization_emits_mentioned_not_known_org(monkeypatch):
    """Recognizing an organization establishes a mention, not participation."""
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    _seed_docs(engine, ["Taylor Morrison is the applicant on this case"])
    monkeypatch.setattr(
        sweep_docs, "extract_entities_from_doc",
        lambda body: [{"name": "Taylor Morrison", "normalized": "taylor morrison",
                       "entity_type": "developer", "role": "mentioned",
                       "confidence": 95}],
    )

    result = sweep_docs.run_sweep_docs(engine, dry_run=False)
    receipt = result["validation_receipt"]
    assert result["success"] is True
    assert receipt["values"]["rejected"] == 0
    assert receipt["observed"]["role"] == {"mentioned": 1}
    assert "known_org" not in receipt["observed"].get("role", {})
    assert reconcile_receipts([receipt], expected_producers=["sweep_docs"]) == []


# ===========================================================================
# sweep_docs_planning integration: shared plan for dry and live
# ===========================================================================


def _hit(name, normalized, entity_type, role, confidence):
    # ``_source_id`` is replaced by the sweep with the real document id; it is
    # present here so these mappings are directly loadable as candidates.
    return {"name": name, "normalized": normalized, "entity_type": entity_type,
            "role": role, "confidence": confidence, "_source_id": 1}


def _one_applicant():
    return [_hit("Acme LLC", "acme llc", "organization", "applicant", 80)]


def _reset_sweep(engine):
    """Re-present already-swept documents so replay can be exercised."""
    with engine.begin() as connection:
        connection.execute(text("UPDATE supporting_documents SET swept_at = NULL"))
        connection.execute(text("DELETE FROM _sweep_docs_watermark"))


def _run(engine, monkeypatch, extractor, dry_run):
    import scripts.entities.sweep_docs as sweep_docs

    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc", extractor)
    return sweep_docs.run_sweep_docs(engine, dry_run=dry_run)


def test_first_live_insert_is_two_inserts_and_two_commits(monkeypatch):
    engine = sweep_engine()
    _seed_docs(engine, ["Applicant: Acme LLC"])

    result = _run(engine, monkeypatch, lambda body: _one_applicant(), False)
    rows = result["validation_receipt"]["rows"]

    assert result["success"] is True
    assert rows["proposed"] == 2
    assert rows["would_insert"] == 2
    assert rows["replay_noop"] == 0
    assert rows["committed"] == 2
    assert rows["classification_reconciles"] and rows["mutation_reconciles"]
    assert count(engine, "entities") == 1
    assert count(engine, "entity_mentions") == 1


def test_unchanged_live_replay_is_two_replay_noops(monkeypatch):
    engine = sweep_engine()
    _seed_docs(engine, ["Applicant: Acme LLC"])
    extractor = lambda body: _one_applicant()  # noqa: E731

    first = _run(engine, monkeypatch, extractor, False)
    assert first["validation_receipt"]["rows"]["committed"] == 2

    _reset_sweep(engine)
    second = _run(engine, monkeypatch, extractor, False)
    rows = second["validation_receipt"]["rows"]

    assert second["success"] is True
    assert rows["proposed"] == 2
    assert rows["would_insert"] == 0
    assert rows["replay_noop"] == 2
    assert rows["committed"] == 0
    # Nothing was duplicated.
    assert count(engine, "entities") == 1
    assert count(engine, "entity_mentions") == 1


def test_empty_dry_run_classifies_inserts_and_writes_nothing(monkeypatch):
    engine = sweep_engine()
    _seed_docs(engine, ["Applicant: Acme LLC"])

    result = _run(engine, monkeypatch, lambda body: _one_applicant(), True)
    rows = result["validation_receipt"]["rows"]

    assert rows["proposed"] == 2
    assert rows["would_insert"] == 2
    assert rows["replay_noop"] == 0
    assert rows["committed"] == 0
    assert rows["mutation_reconciles"] is None
    assert result["entities_created"] == 0
    assert result["mentions_created"] == 0
    assert count(engine, "entities") == 0
    assert count(engine, "entity_mentions") == 0


def test_populated_dry_run_classifies_replay_noops(monkeypatch):
    engine = sweep_engine()
    _seed_docs(engine, ["Applicant: Acme LLC"])
    extractor = lambda body: _one_applicant()  # noqa: E731

    _run(engine, monkeypatch, extractor, False)
    _reset_sweep(engine)
    result = _run(engine, monkeypatch, extractor, True)
    rows = result["validation_receipt"]["rows"]

    assert rows["proposed"] == 2
    assert rows["would_insert"] == 0
    assert rows["replay_noop"] == 2
    assert rows["committed"] == 0
    assert rows["mutation_reconciles"] is None
    assert count(engine, "entity_mentions") == 1


def test_validation_still_runs_on_unchanged_replay(monkeypatch):
    engine = sweep_engine()
    _seed_docs(engine, ["Applicant: Acme LLC"])
    extractor = lambda body: _one_applicant()  # noqa: E731

    _run(engine, monkeypatch, extractor, False)
    _reset_sweep(engine)
    second = _run(engine, monkeypatch, extractor, False)
    receipt = second["validation_receipt"]

    # Every bundle value was validated again on the replay path.
    assert receipt["values"]["attempted"] == 5
    assert receipt["values"]["accepted"] == 5
    assert receipt["values"]["rejected"] == 0
    assert receipt["observed"]["role"] == {"applicant": 1}
    assert receipt["observed"]["evidence_class"] == {"source_pdf_text": 1}
    assert receipt["rows"]["replay_noop"] == 2


def test_new_entity_id_reaches_its_mention_insert(monkeypatch):
    engine = sweep_engine()
    _seed_docs(engine, ["Applicant: Acme LLC"])

    _run(engine, monkeypatch, lambda body: _one_applicant(), False)

    with engine.connect() as connection:
        entity_id = connection.execute(text("SELECT id FROM entities")).scalar_one()
        mention_entity_id = connection.execute(
            text("SELECT entity_id FROM entity_mentions")
        ).scalar_one()
    assert mention_entity_id == entity_id
    assert count(engine, "entity_mentions") == 1


def test_duplicate_candidates_write_one_entity_and_one_mention(monkeypatch):
    engine = sweep_engine()
    _seed_docs(engine, ["Applicant: Acme LLC"])
    duplicates = [
        _hit("Acme LLC", "acme llc", "organization", "applicant", 80),
        _hit("Acme LLC", "acme llc", "organization", "applicant", 80),
        _hit("Acme LLC", "acme llc", "organization", "applicant", 80),
    ]

    result = _run(engine, monkeypatch, lambda body: duplicates, False)
    rows = result["validation_receipt"]["rows"]

    # Three raw duplicates collapse to one entity assertion and one mention.
    assert rows["proposed"] == 2
    assert rows["would_insert"] == 2
    assert count(engine, "entities") == 1
    assert count(engine, "entity_mentions") == 1


def test_payload_selection_is_deterministic_and_order_independent():
    higher = _hit("Alpha LLC", "alpha llc", "organization", "applicant", 60)
    lower = _hit("Beta LLC", "beta llc", "organization", "applicant", 90)
    tie_a = _hit("Alpha LLC", "tie llc", "organization", "applicant", 70)
    tie_b = _hit("Beta LLC", "tie llc", "organization", "applicant", 70)

    def select(hits):
        candidates = [ExtractedCandidate.from_mapping(h) for h in hits]
        entities, _ = select_write_payloads(candidates)
        return entities

    # Highest confidence wins, regardless of order.
    assert select([higher, lower])[("beta llc", "organization")].name == "Beta LLC"
    assert select([lower, higher])[("beta llc", "organization")].name == "Beta LLC"
    # Equal confidence falls back to the lexicographically smallest name.
    assert select([tie_b, tie_a])[("tie llc", "organization")].name == "Alpha LLC"
    assert select([tie_a, tie_b])[("tie llc", "organization")].name == "Alpha LLC"
    # Exactly one payload per assertion identity.
    assert len(select([tie_a, tie_b])) == 1


def test_entity_conflict_reclassifies_exactly_the_conflicting_assertion():
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO entities (id, entity_type, name, normalized_name, "
            "is_government, resolution_status, first_seen_at, last_seen_at, "
            "mention_count, created_at, updated_at) "
            "VALUES (1, 'organization', 'Acme LLC', 'acme llc', 0, 'unresolved', "
            "'2026-01-01', '2026-01-01', 1, '2026-01-01', '2026-01-01')"))

    candidate = ExtractedCandidate(
        "acme llc", "organization", "applicant", 1,
        name="Acme LLC", confidence=80,
    )
    payloads, _ = select_write_payloads([candidate])
    # The row exists in the database but is absent from the cache the plan was
    # built from, so the plan wrongly believes it is an insert.
    plan = build_classification_plan([candidate], existing_entities=[])
    assert plan.total_would_insert == 2

    validator = EmissionValidator("sweep_docs", "1")
    validator.start_batch()
    validator.complete_validation()
    validator.begin_writes()
    validator.classify_rows(
        would_insert=plan.total_would_insert, replay_noop=plan.total_replay_noop,
    )

    entity_cache: dict = {}
    with engine.begin() as connection:
        inserted, revised = sweep_docs._write_entities(
            connection, plan, payloads, entity_cache, validator)

    # The write revealed the conflict: exactly that assertion was reclassified.
    assert inserted == 0
    assert revised.entity_inserts == ()
    assert len(revised.entity_replay_noops) == 1
    assert entity_cache["acme llc|organization"] == 1
    conflicts = validator.seal().reclassified_conflicts
    assert len(conflicts) == 1
    assert conflicts[0]["identity"] == ["acme llc", "organization"]
    assert conflicts[0]["kind"] == "EntityAssertion"
    assert count(engine, "entities") == 1


def test_mention_conflict_reclassifies_exactly_the_mention_assertion():
    validator = EmissionValidator("sweep_docs", "1")
    validator.start_batch()
    validator.complete_validation()
    validator.begin_writes()
    validator.classify_rows(would_insert=2)

    assertion = MentionAssertion(
        entity=EntityAssertion("acme llc", "organization"),
        source_id=1,
        role="applicant",
    )
    validator.reclassify_assertion(assertion, reason="mention existed at write time")
    receipt = validator.seal()

    assert receipt.rows_would_insert == 1
    assert receipt.rows_replay_noop == 1
    assert receipt.rows_proposed == 2
    assert receipt.classification_reconciles
    assert receipt.reclassified_conflicts[0]["identity"] == [
        "acme llc", "organization", "supporting_document", 1, "applicant",
    ]
    assert receipt.reclassified_conflicts[0]["kind"] == "MentionAssertion"


def test_transaction_failure_rolls_back_every_would_mutate_row(monkeypatch):
    import scripts.entities.sweep_docs as sweep_docs

    engine = sweep_engine()
    _seed_docs(engine, ["Applicant: Acme LLC"])
    monkeypatch.setattr(
        sweep_docs, "extract_entities_from_doc", lambda body: _one_applicant())

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(sweep_docs, "_write_mentions", explode)

    result = sweep_docs.run_sweep_docs(engine, dry_run=False)
    rows = result["validation_receipt"]["rows"]

    assert result["success"] is False
    assert rows["would_insert"] == 2
    assert rows["committed"] == 0
    assert rows["rolled_back"] == 2
    assert rows["classification_reconciles"] is True
    assert rows["mutation_reconciles"] is True
    # Nothing persisted.
    assert count(engine, "entities") == 0
    assert count(engine, "entity_mentions") == 0


def test_no_aggregate_shortfall_reconciliation_remains():
    from pathlib import Path

    import scripts.entities.sweep_docs as sweep_docs

    # The count-based reclassification API is gone entirely.
    assert not hasattr(EmissionValidator("x", "1"), "reclassify_replay")
    family = sorted(Path(sweep_docs.__file__).parent.glob("sweep_docs*.py"))
    assert family, "expected to find the sweep_docs module family"
    sources = {path.name: path.read_text(encoding="utf-8") for path in family}

    for name, source in sources.items():
        # The count-based API is gone from every module in the family.
        assert "reclassify_replay" not in source, name
        # The removed path derived a reclassification count from the difference
        # between proposed and committed rows.  That expression must not exist
        # anywhere.  (A docstring explaining why it is absent is fine.)
        assert 'max(0, int(stats.get("proposed"' not in source, name
        assert "shortfall =" not in source, name

    # The replacement is exact-identity based, and lives where conflicts are
    # actually resolved: the storage module that performs the writes.
    assert "reclassify_assertion" in sources["sweep_docs_storage.py"]
