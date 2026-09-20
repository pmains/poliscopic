"""Runtime boundary semantics: atomic page validation, and honest accounting.

Every ordinary failure must arrive as a ``NormalizationRunError`` carrying stats
and a sealed receipt.  No dev database, no production database, no pipeline.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
from sqlalchemy import text

from scripts.entities import event_normalize_runtime as runtime
from scripts.entities.event_normalize_receipts import PRODUCER
from scripts.entities.event_normalize_write_storage import SqlAlchemyWriteStorage

from _kg_event_normalize_sqlite import add_extraction, build_engine, seed

ENTITIES_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "entities"

COMPATIBILITY_COUNTERS = (
    "skipped_unmapped_type", "events_replay_noop", "extraction_links_planned",
    "extraction_links_replay_noop", "assertions_inconsistent", "read_failures",
    "rows_rolled_back", "failure_reason", "replay_verification_failures",
)


@pytest.fixture()
def engine():
    return build_engine()


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


class _FailsOnSecondInsert(SqlAlchemyWriteStorage):
    """Lets page one commit, then fails, to expose partial commits."""

    def __init__(self):
        self.inserts = 0

    def insert_event(self, conn, values):
        self.inserts += 1
        if self.inserts > 1:
            raise RuntimeError("insert rejected")
        return super().insert_event(conn, values)


def patch_bundles(monkeypatch, *, fail_for=()):
    """Make the named extraction rows produce an incomplete bundle."""
    attempts: list[int] = []
    real = runtime.build_event_bundle

    def fake(candidate):
        attempts.append(int(candidate.extraction_id))
        bundle = real(candidate)
        if int(candidate.extraction_id) in fail_for:
            return dataclasses.replace(bundle, event_type=None)
        return bundle

    monkeypatch.setattr(runtime, "build_event_bundle", fake)
    return attempts


# -- atomic page validation ---------------------------------------------------


def test_a_late_bundle_refusal_refuses_the_whole_page(engine, monkeypatch):
    seed(engine, xid=1)
    add_extraction(engine, xid=2)
    add_extraction(engine, xid=3)
    attempts = patch_bundles(monkeypatch, fail_for={3})

    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine)

    # Every candidate was attempted, so the third could not hide behind the first.
    assert attempts == [1, 2, 3]

    receipt = exc.value.receipt
    # The complete plan was classified unresolved: 3 candidates = 3 event
    # assertions + 3 link assertions.
    assert receipt["rows"]["proposed"] == 6
    assert receipt["rows"]["unresolved"] == 6
    assert receipt["rows"]["would_insert"] == 0
    assert receipt["rows"]["would_update"] == 0
    assert receipt["rows"]["replay_noop"] == 0
    assert receipt["values"]["rejected"] >= 1
    assert receipt["state"] == "sealed"


def test_a_bundle_refusal_opens_no_transaction(engine, monkeypatch):
    seed(engine, xid=1)
    add_extraction(engine, xid=2)
    patch_bundles(monkeypatch, fail_for={1, 2})

    with pytest.raises(runtime.NormalizationRunError):
        runtime.normalize(engine)

    assert count_rows(engine, "meeting_events") == 0
    assert extraction_link(engine, 1) is None
    assert extraction_link(engine, 2) is None


def test_early_and_late_refusals_report_the_same_accounting(engine, monkeypatch):
    seed(engine, xid=1)
    add_extraction(engine, xid=2)

    patch_bundles(monkeypatch, fail_for={1})
    with pytest.raises(runtime.NormalizationRunError) as early:
        runtime.normalize(engine)
    assert early.value.receipt["rows"]["unresolved"] == 4

    patch_bundles(monkeypatch, fail_for={2})
    with pytest.raises(runtime.NormalizationRunError) as late:
        runtime.normalize(engine)
    assert late.value.receipt["rows"]["unresolved"] == 4


# -- read failures are input failures ----------------------------------------


def test_read_failure_seals_a_receipt_outside_row_accounting(engine):
    seed(engine, method="bogus_method")

    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine)

    error = exc.value
    assert error.stats["read_failures"] == 1
    assert error.stats["extractions_examined"] == 1
    # A row that could not be interpreted is not an assertion row.
    assert error.receipt["rows"]["proposed"] == 0
    assert error.receipt["rows"]["unresolved"] == 0
    assert error.receipt["state"] == "sealed"
    assert error.cause is not None
    assert count_rows(engine, "meeting_events") == 0


# -- later-page failure preserves prior commits -------------------------------


def test_later_page_write_failure_preserves_prior_commits(engine):
    seed(engine, xid=1)
    add_extraction(engine, xid=2)

    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine, page_size=1, storage=_FailsOnSecondInsert())

    error = exc.value
    assert error.earlier_pages_committed is True
    assert error.receipt["rows"]["committed"] == 2       # page one
    assert error.receipt["rows"]["rolled_back"] == 2     # page two
    assert error.receipt["state"] == "sealed"
    assert error.stats["events_inserted"] == 1           # page one only
    assert error.stats["rows_rolled_back"] == 2
    assert error.stats["failure_reason"]

    assert count_rows(engine, "meeting_events") == 1
    assert extraction_link(engine, 1) is not None
    assert extraction_link(engine, 2) is None


def test_no_failure_escapes_raw(engine, monkeypatch):
    """Every ordinary failure arrives typed, with stats and a sealed receipt."""
    seed(engine, method="bogus_method")
    with pytest.raises(runtime.NormalizationRunError) as read_exc:
        runtime.normalize(engine)
    assert read_exc.value.receipt["state"] == "sealed"
    assert read_exc.value.stats

    other = build_engine()
    seed(other, xid=1)
    patch_bundles(monkeypatch, fail_for={1})
    with pytest.raises(runtime.NormalizationRunError) as bundle_exc:
        runtime.normalize(other)
    assert bundle_exc.value.receipt["state"] == "sealed"

    third = build_engine()
    seed(third, xid=1)
    add_extraction(third, xid=2)
    with pytest.raises(runtime.NormalizationRunError) as write_exc:
        runtime.normalize(third, page_size=1, storage=_FailsOnSecondInsert())
    assert write_exc.value.receipt["state"] == "sealed"


# -- counters -----------------------------------------------------------------


def test_compatibility_counters_on_an_empty_run(engine):
    stats = runtime.normalize(engine)

    assert stats["extractions_examined"] == 0
    for counter in COMPATIBILITY_COUNTERS:
        assert stats[counter] in (0, None), counter
    assert stats["failure_reason"] is None
    assert stats["validation_receipt"]["rows"]["proposed"] == 0


def test_compatibility_counters_on_a_successful_run(engine):
    seed(engine)

    stats = runtime.normalize(engine)

    assert stats["extractions_examined"] == 1
    assert stats["normalizable"] == 1
    assert stats["events_planned"] == 1
    assert stats["events_inserted"] == 1
    assert stats["extraction_links_planned"] == 1
    assert stats["extraction_links_updated"] == 1
    assert stats["events_replay_noop"] == 0
    assert stats["extraction_links_replay_noop"] == 0
    assert stats["assertions_inconsistent"] == 0
    assert stats["skipped_unmapped_type"] == 0
    assert stats["read_failures"] == 0
    assert stats["rows_rolled_back"] == 0
    assert stats["failure_reason"] is None


def test_verified_replay_counters(engine):
    seed(engine, linked=True)

    stats = runtime.normalize(engine, force=True)

    assert stats["events_inserted"] == 0
    assert stats["extraction_links_updated"] == 0
    assert stats["events_replay_noop"] == 1
    assert stats["extraction_links_replay_noop"] == 1
    assert count_rows(engine, "meeting_events") == 1


def test_compatibility_counters_on_a_failure(engine):
    seed(engine, method="bogus_method")

    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine)

    stats = exc.value.stats
    assert stats["read_failures"] == 1
    assert stats["errors"] == 1
    assert stats["events_inserted"] == 0
    for counter in COMPATIBILITY_COUNTERS:
        assert counter in stats, counter


# -- unchanged behavior -------------------------------------------------------


def test_dry_run_writes_nothing(engine):
    seed(engine)

    stats = runtime.normalize(engine, dry_run=True)

    assert stats["events_inserted"] == 0
    assert stats["validation_receipt"]["dry_run"] is True
    assert count_rows(engine, "meeting_events") == 0


def test_limit_bounds_the_run(engine):
    seed(engine, xid=1)
    for xid in (2, 3, 4):
        add_extraction(engine, xid=xid)

    stats = runtime.normalize(engine, limit=2, page_size=1)

    assert stats["extractions_examined"] == 2
    assert count_rows(engine, "meeting_events") == 2


def test_no_page_is_read_twice(engine):
    seed(engine, xid=1)
    for xid in (2, 3, 4, 5):
        add_extraction(engine, xid=xid)

    stats = runtime.normalize(engine, page_size=2)

    assert stats["extractions_examined"] == 5
    assert count_rows(engine, "meeting_events") == 5


def test_receipts_seal_under_the_pipeline_producer_name(engine):
    seed(engine)

    stats = runtime.normalize(engine, dry_run=True)

    assert stats["validation_receipt"]["producer"] == PRODUCER


# -- source contract ----------------------------------------------------------


def test_runtime_uses_the_atomic_gate_and_the_bridge():
    source = (ENTITIES_DIR / "event_normalize_runtime.py").read_text(encoding="utf-8")

    assert "validate_bundle(" in source
    assert "build_event_bundle(" in source
    assert "build_plan_from_work_items(" in source

    # The name may appear in prose explaining that it is avoided; it must not
    # appear in code.  Drop the module docstring before checking.
    body = source.split('"""', 2)[2]
    assert "build_classification_plan" not in body
