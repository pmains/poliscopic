"""Transaction-time replay verification tests against isolated SQLite.

A plan carries replay assertions that were true when it was built.  These tests
prove they are re-established against the database before the plan is accepted,
and that a stale plan fails rather than returning a successful no-op.

No dev database, no production database, no pipeline.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import text

from scripts.entities.event_normalize_storage import fetch_normalization_page
from scripts.entities.event_normalize_write_contract import (
    ReplayVerificationError, WriteRolledBackError,
)
from scripts.entities.event_normalize_write_storage import SqlAlchemyWriteStorage
from scripts.entities.event_normalize_writes import apply_classification_plan
from scripts.entities.event_normalize_work_items import build_plan_from_work_items

from _kg_event_normalize_sqlite import (
    add_event, add_event_type, add_extraction, build_engine, seed,
)


@pytest.fixture()
def engine():
    return build_engine()


def read_plan(engine, *, force=False):
    page = fetch_normalization_page(engine, force=force)
    return build_plan_from_work_items(page.work_items)


def replay_plan(engine):
    """A plan consisting of one verified replay pair."""
    seed(engine, linked=True)
    plan = read_plan(engine, force=True)
    assert len(plan.event_replay_noops) == 1 and len(plan.link_replay_noops) == 1
    return plan


def run_sql(engine, statement, params=None):
    with engine.begin() as conn:
        conn.execute(text(statement), params or {})


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


class _RecordingLocks(SqlAlchemyWriteStorage):
    """Records the order in which extraction rows are claimed."""

    def __init__(self):
        self.locks = []

    def lock_extraction(self, conn, extraction_id):
        self.locks.append(int(extraction_id))
        return super().lock_extraction(conn, extraction_id)


def assert_replay_failed(exc, plan, engine, *, still_linked=1):
    """Every stale-plan failure reports the same honest evidence."""
    result = exc.value.result
    assert isinstance(exc.value.__cause__, ReplayVerificationError)
    assert result.rows_committed == 0
    assert result.event_replay_noops == 0
    assert result.extraction_link_replay_noops == 0
    assert result.replay_verification_failures == 1
    assert result.failure_reason
    result.check_reconciliation(plan)
    assert extraction_link(engine, 1) == still_linked


# -- the happy path -----------------------------------------------------------


def test_unchanged_replay_is_transactionally_reverified(engine):
    plan = replay_plan(engine)

    result = apply_classification_plan(engine, plan)

    assert result.rows_committed == 0
    assert result.events_inserted == 0
    assert result.extraction_links_updated == 0
    assert result.event_replay_noops == 1
    assert result.extraction_link_replay_noops == 1
    assert result.replay_verification_failures == 0
    assert result.failure_reason is None
    result.check_reconciliation(plan)
    assert extraction_link(engine, 1) == 1


def test_replay_with_an_agreeing_supplied_id_is_accepted(engine):
    plan = replay_plan(engine)
    digest = plan.event_replay_noops[0].identity.digest

    result = apply_classification_plan(
        engine, plan, stored_event_ids={digest: 1}
    )

    assert result.rows_committed == 0
    assert result.event_replay_noops == 1
    result.check_reconciliation(plan)


# -- the extraction side of a replay goes stale -------------------------------


def test_replay_extraction_disappeared_after_planning(engine):
    plan = replay_plan(engine)
    run_sql(engine, "DELETE FROM meeting_event_extractions WHERE id = 1")

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    result = exc.value.result
    assert result.rows_committed == 0
    assert result.replay_verification_failures == 1
    result.check_reconciliation(plan)
    assert count_rows(engine, "meeting_event_extractions") == 0


def test_replay_extraction_became_unlinked(engine):
    plan = replay_plan(engine)
    run_sql(
        engine,
        "UPDATE meeting_event_extractions SET meeting_event_id = NULL WHERE id = 1",
    )

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert_replay_failed(exc, plan, engine, still_linked=None)
    # A planned replay is never silently converted into a write.
    assert count_rows(engine, "meeting_events") == 1


def test_replay_extraction_points_to_a_different_event(engine):
    plan = replay_plan(engine)
    add_event(engine, eid=50, outcome="denied")
    run_sql(
        engine,
        "UPDATE meeting_event_extractions SET meeting_event_id = 50 WHERE id = 1",
    )

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert_replay_failed(exc, plan, engine, still_linked=50)


# -- the event side of a replay goes stale ------------------------------------


def test_replay_event_disappeared(engine):
    plan = replay_plan(engine)
    run_sql(engine, "DELETE FROM meeting_events WHERE id = 1")

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert_replay_failed(exc, plan, engine)
    assert count_rows(engine, "meeting_events") == 0


def test_replay_event_outcome_changed(engine):
    plan = replay_plan(engine)
    run_sql(engine, "UPDATE meeting_events SET outcome = 'denied' WHERE id = 1")

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert_replay_failed(exc, plan, engine)


def test_replay_event_type_changed(engine):
    plan = replay_plan(engine)
    add_event_type(engine, tid=2, slug="decision.denial")
    run_sql(engine, "UPDATE meeting_events SET event_type_id = 2 WHERE id = 1")

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert_replay_failed(exc, plan, engine)


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("case_number", "'CV-99'"),
        ("action_verb", "'denied'"),
        ("supporting_doc_id", "2"),
        ("text_offset_start", "5"),
        ("meeting_id", "'OTHER-MEETING'"),
    ),
)
def test_replay_event_other_persisted_semantics_changed(engine, column, value):
    plan = replay_plan(engine)
    run_sql(engine, f"UPDATE meeting_events SET {column} = {value} WHERE id = 1")

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    assert_replay_failed(exc, plan, engine)


# -- supplied event ids are evidence, not payloads ----------------------------


def test_supplied_id_disagreeing_with_the_actual_link_fails(engine):
    plan = replay_plan(engine)
    digest = plan.event_replay_noops[0].identity.digest
    add_event(engine, eid=99)

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan, stored_event_ids={digest: 99})

    assert_replay_failed(exc, plan, engine)


def test_supplied_stored_event_matching_is_linked(engine):
    """An unlinked extraction resolved to an already-stored event."""
    seed(engine)
    plan = read_plan(engine)
    digest = plan.event_inserts[0].identity.digest
    stored_plan = dataclasses.replace(
        plan, event_inserts=(), event_replay_noops=plan.event_inserts
    )

    add_event(engine, eid=7)
    result = apply_classification_plan(
        engine, stored_plan, stored_event_ids={digest: 7}
    )

    assert result.events_inserted == 0
    assert result.extraction_links_updated == 1
    assert result.rows_committed == 1
    assert count_rows(engine, "meeting_events") == 1
    assert extraction_link(engine, 1) == 7
    result.check_reconciliation(stored_plan)


def test_supplied_stored_event_with_different_semantics_fails(engine):
    seed(engine)
    plan = read_plan(engine)
    digest = plan.event_inserts[0].identity.digest
    stored_plan = dataclasses.replace(
        plan, event_inserts=(), event_replay_noops=plan.event_inserts
    )

    add_event(engine, eid=7, outcome="denied")

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(
            engine, stored_plan, stored_event_ids={digest: 7}
        )

    result = exc.value.result
    assert isinstance(exc.value.__cause__, ReplayVerificationError)
    assert result.rows_committed == 0
    assert result.replay_verification_failures == 1
    assert extraction_link(engine, 1) is None
    assert count_rows(engine, "meeting_events") == 1


# -- ordering and rollback ----------------------------------------------------


def test_mixed_update_and_replay_rows_lock_in_one_sorted_order(engine):
    seed(engine, xid=1, linked=True)      # extraction 1: replay
    add_extraction(engine, xid=3)         # extraction 3: update
    plan = read_plan(engine, force=True)
    assert [l.extraction_id for l in plan.link_replay_noops] == [1]
    assert [l.extraction_id for l in plan.link_updates] == [3]

    recorder = _RecordingLocks()
    apply_classification_plan(engine, plan, storage=recorder)

    # Not updates-then-replays: one ascending sequence over both kinds.
    assert recorder.locks == [1, 3]


def test_late_replay_failure_rolls_back_earlier_inserts_and_links(engine):
    seed(engine, xid=1)                                   # extraction 1: insert
    seed(engine, xid=2, linked=True, did=2, mid=2, bid=2, jid=2)
    plan = read_plan(engine, force=True)
    assert [u.extraction_id for u in plan.link_updates] == [1]
    assert [r.extraction_id for r in plan.link_replay_noops] == [2]

    # Extraction 1 writes first; extraction 2's replay is then found stale.
    run_sql(engine, "UPDATE meeting_events SET outcome = 'denied' WHERE id = 1")

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(engine, plan)

    result = exc.value.result
    assert isinstance(exc.value.__cause__, ReplayVerificationError)
    assert result.rows_committed == 0
    assert result.events_inserted == 0
    assert result.extraction_links_updated == 0
    assert result.replay_verification_failures == 1
    result.check_reconciliation(plan)

    # The earlier insert and link are gone with the transaction.
    assert count_rows(engine, "meeting_events") == 1
    assert extraction_link(engine, 1) is None
    assert extraction_link(engine, 2) == 1


# -- accounting truthfulness --------------------------------------------------


def test_replay_accounting_counts_only_verified_rows(engine):
    verified_engine = build_engine()
    plan = replay_plan(verified_engine)
    verified = apply_classification_plan(verified_engine, plan)

    assert verified.event_replay_noops == len(plan.event_replay_noops)
    assert verified.extraction_link_replay_noops == len(plan.link_replay_noops)
    assert verified.rows_committed == 0
    assert verified.replay_verification_failures == 0
    verified.check_reconciliation(plan)

    stale_engine = build_engine()
    stale_plan = replay_plan(stale_engine)
    run_sql(
        stale_engine,
        "UPDATE meeting_event_extractions SET meeting_event_id = NULL WHERE id = 1",
    )

    with pytest.raises(WriteRolledBackError) as exc:
        apply_classification_plan(stale_engine, stale_plan)

    stale = exc.value.result
    # A stale replay plan produces a failure, never a successful no-op.
    assert stale.event_replay_noops == 0
    assert stale.extraction_link_replay_noops == 0
    assert stale.replay_verification_failures == 1
    assert stale.succeeded is False
    stale.check_reconciliation(stale_plan)
