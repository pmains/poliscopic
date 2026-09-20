"""Write-adapter execution tests against isolated SQLite.

No dev database, no production database, no pipeline.  Each test builds a
throwaway in-memory SQLite database via the shared helper and lets it die.  Faults
and races are injected by overriding one narrow storage method -- never by mocking
the adapter or the transaction.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import text

from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.entities.event_normalize_planning import build_classification_plan
from scripts.entities.event_normalize_storage import fetch_normalization_page
from scripts.entities.event_normalize_write_storage import (
    EventTypeLookupError, ExtractionLinkState, SqlAlchemyWriteStorage,
)
from scripts.entities.event_normalize_write_contract import (
    ConcurrentConflictError, MissingExtractionError, WriteRolledBackError,
)
from scripts.entities.event_normalize_writes import apply_classification_plan
from scripts.entities.event_normalize_work_items import build_plan_from_work_items
from scripts.kg.registries import canonicalize_outcome

from _kg_event_normalize_sqlite import (
    DEFAULT_MEETING_SOURCE, add_event, add_extraction, build_engine, seed,
)


@pytest.fixture()
def engine():
    return build_engine()


def read_plan(engine, *, force=False):
    """A real plan, built from the fixture through the read contract."""
    page = fetch_normalization_page(engine, force=force)
    return build_plan_from_work_items(page.work_items)


def make_candidate(**overrides):
    kwargs = dict(
        extraction_id=999, supporting_document_id=1, meeting_db_id=1,
        meeting_source_id=DEFAULT_MEETING_SOURCE, public_body_id=1,
        jurisdiction_id=1, action_verb="approved", content_hash="h",
        extraction_method="pdftotext",
    )
    kwargs.update(overrides)
    return NormalizationCandidate.create(**kwargs)


def count_rows(engine, table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def extraction_link(engine, extraction_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT meeting_event_id FROM meeting_event_extractions"
                 " WHERE id = :x"),
            {"x": extraction_id},
        ).fetchone()
    return None if row is None else row[0]


def set_extraction_link(engine, extraction_id, event_id):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE meeting_event_extractions SET meeting_event_id = :e"
                 " WHERE id = :x"),
            {"e": event_id, "x": extraction_id},
        )


class _InsertFails(SqlAlchemyWriteStorage):
    def insert_event(self, conn, values):
        raise RuntimeError("insert rejected")


class _LinkFails(SqlAlchemyWriteStorage):
    def link_extraction(self, conn, extraction_id, event_id):
        raise RuntimeError("link rejected")


class _RecordingLocks(SqlAlchemyWriteStorage):
    """Records the order in which extraction rows are claimed."""

    def __init__(self):
        self.locks = []

    def lock_extraction(self, conn, extraction_id):
        self.locks.append(int(extraction_id))
        return super().lock_extraction(conn, extraction_id)


class _StaleLock(SqlAlchemyWriteStorage):
    """Reports a row as unlinked even though a competing link has committed.

    This models a read that lost the race: the adapter believes the row is
    unlinked, and the real guarded update then affects zero rows.
    """

    def lock_extraction(self, conn, extraction_id):
        state = super().lock_extraction(conn, extraction_id)
        if state.exists:
            return ExtractionLinkState(exists=True, event_id=None)
        return state


# -- empty and dry ------------------------------------------------------------


def test_empty_plan_is_a_no_op(engine):
    plan = build_classification_plan([])
    result = apply_classification_plan(engine, plan)

    assert result.would_mutate == 0
    assert result.rows_committed == 0
    assert result.dry_run is False
    result.check_reconciliation(plan)
    assert count_rows(engine, "meeting_events") == 0


def test_dry_run_accounts_without_writing(engine):
    seed(engine)
    plan = read_plan(engine)

    result = apply_classification_plan(engine, plan, dry_run=True)

    assert result.dry_run is True
    assert result.events_planned == 1
    assert result.events_inserted == 0
    assert result.extraction_links_updated == 0
    assert result.rows_committed == 0
    assert result.rows_rolled_back == 0
    result.check_reconciliation(plan)

    assert count_rows(engine, "meeting_events") == 0
    assert extraction_link(engine, 1) is None


# -- live writes --------------------------------------------------------------


def test_live_insert_links_and_commits(engine):
    seed(engine)
    plan = read_plan(engine)

    result = apply_classification_plan(engine, plan)

    assert result.events_inserted == 1
    assert result.extraction_links_updated == 1
    assert result.rows_committed == 2
    assert result.rows_rolled_back == 0
    result.check_reconciliation(plan)

    assert count_rows(engine, "meeting_events") == 1
    assert extraction_link(engine, 1) is not None


def test_live_insert_persists_schema_fields(engine):
    seed(engine, span_start=10, span_end=40, case_number="CV-1")
    plan = read_plan(engine)
    apply_classification_plan(engine, plan)

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT meeting_id, supporting_doc_id, event_type_id, outcome,"
            " action_verb, text_offset_start, text_offset_end, case_number"
            " FROM meeting_events"
        )).fetchone()

    assert row[0] == DEFAULT_MEETING_SOURCE
    assert row[1] == 1
    # Resolved through the registry's dotted-slug compatibility contract.
    assert row[2] == 1
    assert row[3] == "approved"
    assert row[4] == "approved"
    assert (row[5], row[6]) == (10, 40)
    assert row[7] == "CV-1"


def test_qualified_outcome_round_trips_through_storage(engine):
    seed(engine, action_verb="approved_with_conditions")
    plan = read_plan(engine)
    apply_classification_plan(engine, plan)

    with engine.connect() as conn:
        outcome = conn.execute(text("SELECT outcome FROM meeting_events")).scalar()

    assert outcome == "approved_with_conditions"
    canonical = canonicalize_outcome(outcome)
    expected = plan.event_inserts[0].payload
    assert canonical.base == expected.outcome_base
    assert canonical.qualifier == expected.outcome_qualifier


def test_successful_result_accounting(engine):
    seed(engine, xid=1)
    seed(engine, xid=2, linked=True, did=2, mid=2, bid=2, jid=2)
    plan = read_plan(engine, force=True)

    result = apply_classification_plan(engine, plan)

    assert result.events_planned == 1
    assert result.events_inserted == 1
    assert result.event_replay_noops == 1
    assert result.extraction_links_planned == 1
    assert result.extraction_links_updated == 1
    assert result.extraction_link_replay_noops == 1
    assert result.rows_committed == 2
    assert result.rows_rolled_back == 0
    assert result.rows_reclassified_as_replay == 0
    result.check_reconciliation(plan)


# -- identity: distinct extraction rows stay distinct ------------------------


def test_distinct_extractions_with_identical_coarse_key_stay_distinct(engine):
    """Two rows sharing meeting/document/type/span are two distinct events."""
    seed(engine, xid=1)
    add_extraction(engine, xid=2)  # same document, meeting, type, and span

    plan = read_plan(engine)
    first, second = plan.event_inserts

    # The coarse tuple is identical ...
    assert first.payload.meeting_source_id == second.payload.meeting_source_id
    assert first.payload.supporting_document_id == second.payload.supporting_document_id
    assert first.payload.span_start == second.payload.span_start
    assert first.payload.span_end == second.payload.span_end
    assert first.payload.event_type == second.payload.event_type
    # ... but the canonical identities are not.
    assert first.identity.digest != second.identity.digest
    assert first.payload.occurrence != second.payload.occurrence

    result = apply_classification_plan(engine, plan)

    assert result.events_inserted == 2
    assert count_rows(engine, "meeting_events") == 2
    assert extraction_link(engine, 1) != extraction_link(engine, 2)


def test_reapplying_the_same_plan_creates_no_second_event(engine):
    seed(engine)
    plan = read_plan(engine)

    first = apply_classification_plan(engine, plan)
    assert first.events_inserted == 1

    second = apply_classification_plan(engine, plan)

    assert second.events_inserted == 0
    assert second.event_replay_noops == 1
    assert second.extraction_links_updated == 0
    assert second.extraction_link_replay_noops == 1
    assert second.rows_committed == 0
    second.check_reconciliation(plan)
    assert count_rows(engine, "meeting_events") == 1


def test_already_linked_semantic_conflict_rolls_back(engine):
    seed(engine)
    plan = read_plan(engine)
    add_event(engine, eid=50, outcome="denied")
    set_extraction_link(engine, 1, 50)

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert isinstance(exc.value.__cause__, ConcurrentConflictError)
    assert exc.value.result.rows_committed == 0
    assert count_rows(engine, "meeting_events") == 1
    assert extraction_link(engine, 1) == 50  # the competing link survives


# -- guarded compare-and-set --------------------------------------------------


def test_guarded_link_update_only_fills_a_null_link(engine):
    seed(engine)
    storage = SqlAlchemyWriteStorage()

    with engine.begin() as conn:
        assert storage.link_extraction(conn, 1, 42) == 1
        # A second attempt must not overwrite the link that is already there.
        assert storage.link_extraction(conn, 1, 99) == 0
        assert storage.load_extraction_link(conn, 1).event_id == 42

    assert extraction_link(engine, 1) == 42


def test_guarded_update_lost_race_exact_replay(engine):
    seed(engine)
    plan = read_plan(engine)
    # A competing write linked the row to an exactly equivalent event.
    add_event(engine, eid=77)
    set_extraction_link(engine, 1, 77)

    result = apply_classification_plan(engine, plan, storage=_StaleLock())

    assert result.events_inserted == 0
    assert result.event_replay_noops == 1
    assert result.extraction_links_updated == 0
    assert result.extraction_link_replay_noops == 1
    assert result.rows_committed == 0
    result.check_reconciliation(plan)
    # The redundant insert was discarded, so no orphan event remains.
    assert count_rows(engine, "meeting_events") == 1
    assert extraction_link(engine, 1) == 77


def test_guarded_update_lost_race_conflict(engine):
    seed(engine)
    plan = read_plan(engine)
    add_event(engine, eid=88, outcome="denied")
    set_extraction_link(engine, 1, 88)

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan, storage=_StaleLock())

    assert isinstance(exc.value.__cause__, ConcurrentConflictError)
    assert exc.value.result.rows_committed == 0
    assert exc.value.result.rows_rolled_back == exc.value.result.would_mutate
    exc.value.result.check_reconciliation(plan)
    assert count_rows(engine, "meeting_events") == 1
    assert extraction_link(engine, 1) == 88  # never overwritten


def test_lost_race_conflict_rolls_back_preceding_insert(engine):
    seed(engine, xid=1)
    add_extraction(engine, xid=2)
    plan = read_plan(engine)
    # Extraction 2 loses its race to a conflicting link; extraction 1 is clean.
    add_event(engine, eid=88, outcome="denied")
    set_extraction_link(engine, 2, 88)

    with pytest.raises(WriteRolledBackError):
        apply_classification_plan(engine, plan, storage=_StaleLock())

    # Extraction 1's successful insert is gone with the failed transaction.
    assert count_rows(engine, "meeting_events") == 1
    assert extraction_link(engine, 1) is None
    assert extraction_link(engine, 2) == 88


def test_missing_extraction_row_raises(engine):
    seed(engine)
    plan = build_classification_plan([make_candidate(extraction_id=999)])

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert isinstance(exc.value.__cause__, MissingExtractionError)
    assert exc.value.result.rows_committed == 0
    assert count_rows(engine, "meeting_events") == 0


# -- lock ordering ------------------------------------------------------------


def test_lock_order_is_sorted_by_extraction_id(engine):
    seed(engine, xid=1)
    add_extraction(engine, xid=2)
    add_extraction(engine, xid=3)
    plan = read_plan(engine)
    scrambled = dataclasses.replace(
        plan,
        event_inserts=tuple(reversed(plan.event_inserts)),
        link_updates=tuple(reversed(plan.link_updates)),
    )
    assert [l.extraction_id for l in scrambled.link_updates] == [3, 2, 1]

    recorder = _RecordingLocks()
    apply_classification_plan(engine, scrambled, storage=recorder)

    assert recorder.locks == [1, 2, 3]


# -- failures roll back -------------------------------------------------------


def test_event_insert_failure_rolls_back(engine):
    seed(engine)
    plan = read_plan(engine)

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan, storage=_InsertFails())

    assert exc.value.result.rows_committed == 0
    assert exc.value.result.rows_rolled_back == 2
    exc.value.result.check_reconciliation(plan)
    assert count_rows(engine, "meeting_events") == 0
    assert extraction_link(engine, 1) is None


def test_missing_event_type_lookup_rolls_back(engine):
    seed(engine)
    plan = read_plan(engine)

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM meeting_event_types"))

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert isinstance(exc.value.__cause__, EventTypeLookupError)
    assert exc.value.result.rows_committed == 0
    exc.value.result.check_reconciliation(plan)
    assert count_rows(engine, "meeting_events") == 0


def test_link_failure_rolls_back_the_event_insert(engine):
    seed(engine)
    plan = read_plan(engine)

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan, storage=_LinkFails())

    assert exc.value.result.rows_committed == 0
    assert exc.value.result.rows_rolled_back == 2
    # The event inserted earlier in the same transaction is gone too.
    assert count_rows(engine, "meeting_events") == 0
    assert extraction_link(engine, 1) is None


# -- accounting across race outcomes -----------------------------------------


def test_accounting_after_concurrent_replay_and_rollback(engine):
    seed(engine)
    plan = read_plan(engine)
    add_event(engine, eid=77)
    set_extraction_link(engine, 1, 77)

    replayed = apply_classification_plan(engine, plan, storage=_StaleLock())
    replayed.check_reconciliation(plan)
    assert replayed.rows_committed == 0
    assert replayed.events_inserted + replayed.event_replay_noops == (
        replayed.events_planned + len(plan.event_replay_noops)
    )
    assert replayed.extraction_links_updated + replayed.extraction_link_replay_noops == (
        replayed.extraction_links_planned + len(plan.link_replay_noops)
    )
    assert replayed.would_mutate == (
        replayed.rows_committed + replayed.rows_reclassified_as_replay
    )

    other = build_engine()
    seed(other)
    conflicted = read_plan(other)
    add_event(other, eid=88, outcome="denied")
    set_extraction_link(other, 1, 88)

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(other, conflicted, storage=_StaleLock())

    failed = exc.value.result
    failed.check_reconciliation(conflicted)
    assert failed.rows_committed == 0
    assert failed.rows_rolled_back == failed.would_mutate
