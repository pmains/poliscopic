"""Runtime finalization: signal passthrough, and the sealing contract.

``KeyboardInterrupt`` and ``SystemExit`` are process-control signals, not run
failures, and must propagate untouched.  Every ordinary failure must seal exactly
one receipt that never claims ``failure is None``.

No dev database, no production database, no pipeline.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
from sqlalchemy import text

from scripts.entities import event_normalize_runtime as runtime
from scripts.entities.event_normalize_receipts import ReceiptError
from scripts.entities.event_normalize_write_storage import SqlAlchemyWriteStorage
from scripts.kg.emission import EmissionValidator

from _kg_event_normalize_sqlite import add_extraction, build_engine, seed

ENTITIES_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "entities"


@pytest.fixture()
def engine():
    return build_engine()


def count_rows(engine, table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


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
    real = runtime.build_event_bundle

    def fake(candidate):
        bundle = real(candidate)
        if int(candidate.extraction_id) in fail_for:
            return dataclasses.replace(bundle, event_type=None)
        return bundle

    monkeypatch.setattr(runtime, "build_event_bundle", fake)


def count_seals(monkeypatch):
    calls: list[int] = []
    real = EmissionValidator.seal

    def counting(self):
        calls.append(1)
        return real(self)

    monkeypatch.setattr(EmissionValidator, "seal", counting)
    return calls


def read_failure(engine, monkeypatch):
    seed(engine, method="bogus_method")
    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine)
    return exc.value


def bundle_failure(engine, monkeypatch):
    seed(engine, xid=1)
    patch_bundles(monkeypatch, fail_for={1})
    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine)
    return exc.value


def write_failure(engine):
    seed(engine, xid=1)
    add_extraction(engine, xid=2)
    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine, page_size=1, storage=_FailsOnSecondInsert())
    return exc.value


# -- process-control signals are not wrapped ----------------------------------


def test_keyboard_interrupt_is_not_wrapped(engine, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "_run", interrupt)

    with pytest.raises(KeyboardInterrupt):
        runtime.normalize(engine)


def test_system_exit_is_not_wrapped(engine, monkeypatch):
    def exit_now(*args, **kwargs):
        raise SystemExit(3)

    monkeypatch.setattr(runtime, "_run", exit_now)

    with pytest.raises(SystemExit) as exc:
        runtime.normalize(engine)

    assert exc.value.code == 3


# -- reconciliation is part of finalization -----------------------------------


def test_a_healthy_run_seals_without_a_failure(engine):
    seed(engine)

    stats = runtime.normalize(engine)

    assert stats["validation_receipt"]["failure"] is None
    assert stats["validation_receipt"]["state"] == "sealed"


def test_reconciliation_failure_seals_with_a_nonempty_failure(engine, monkeypatch):
    seed(engine)

    def broken(receipt):
        raise ReceiptError("synthetic reconciliation failure")

    monkeypatch.setattr(runtime, "check_pre_seal_receipt", broken)

    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine)

    receipt = exc.value.receipt
    assert receipt["failure"]
    assert "does not reconcile" in receipt["failure"]
    assert receipt["state"] == "sealed"
    assert exc.value.stats["errors"] == 1


def test_no_failed_receipt_claims_no_failure(engine, monkeypatch):
    errors = [
        read_failure(build_engine(), monkeypatch),
        bundle_failure(build_engine(), monkeypatch),
        write_failure(build_engine()),
    ]

    for error in errors:
        assert error.receipt["failure"], error.receipt
        assert error.receipt["state"] == "sealed"
        # The same receipt data reaches stats and the error.
        assert error.stats["validation_receipt"] == error.receipt


def test_ordinary_exceptions_carry_sealed_evidence(engine, monkeypatch):
    error = write_failure(build_engine())

    assert isinstance(error, runtime.NormalizationRunError)
    assert error.cause is not None
    assert error.stats is not None
    assert error.receipt["state"] == "sealed"


# -- stats and receipt stay aligned -------------------------------------------


def test_failed_page_counters_align_between_stats_and_receipt(engine):
    error = write_failure(build_engine())

    stats = error.stats
    rows = error.receipt["rows"]

    # The discarded page's planned work is counted in both places.
    assert stats["events_planned"] == rows["would_insert"] == 2
    assert stats["extraction_links_planned"] == rows["would_update"] == 2
    assert stats["rows_rolled_back"] == rows["rolled_back"] == 2
    assert stats["failure_reason"] == "insert rejected"
    assert stats["replay_verification_failures"] == 0


def test_earlier_committed_counters_survive_a_later_rollback(engine):
    error = write_failure(build_engine())

    assert error.earlier_pages_committed is True
    # Page one's committed rows are preserved, not reduced by the rollback.
    assert error.receipt["rows"]["committed"] == 2
    assert error.stats["events_inserted"] == 1
    assert error.stats["extraction_links_updated"] == 1
    assert error.stats["rows_rolled_back"] == 2


def test_stats_keys_are_identical_across_outcomes(engine, monkeypatch):
    empty = runtime.normalize(build_engine())

    success_engine = build_engine()
    seed(success_engine)
    success = runtime.normalize(success_engine)

    replay_engine = build_engine()
    seed(replay_engine, linked=True)
    replay = runtime.normalize(replay_engine, force=True)

    read = read_failure(build_engine(), monkeypatch).stats
    write = write_failure(build_engine()).stats
    # Patches ``build_event_bundle``, so it runs last.
    bundle = bundle_failure(build_engine(), monkeypatch).stats

    def keys(stats):
        return {name for name in stats if name != "validation_receipt"}

    expected = keys(empty)
    for label, stats in (
        ("success", success), ("replay", replay), ("read", read),
        ("write", write), ("bundle", bundle),
    ):
        assert keys(stats) == expected, label
        assert "validation_receipt" in stats, label


def test_receipt_state_is_never_assigned_by_the_runtime():
    """The live receipt stays under the emission lifecycle's control."""
    source = (ENTITIES_DIR / "event_normalize_runtime.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]

    # No impersonating a sealed receipt, and no direct lifecycle mutation.
    assert ".state =" not in body
    assert "STATE_SEALED" not in body


# -- exactly one seal per run -------------------------------------------------


def test_success_seals_exactly_once(engine, monkeypatch):
    seed(engine)
    calls = count_seals(monkeypatch)

    runtime.normalize(engine)

    assert len(calls) == 1


def test_read_failure_seals_exactly_once(engine, monkeypatch):
    seed(engine, method="bogus_method")
    calls = count_seals(monkeypatch)

    with pytest.raises(runtime.NormalizationRunError):
        runtime.normalize(engine)

    assert len(calls) == 1
    assert count_rows(engine, "meeting_events") == 0


def test_bundle_failure_seals_exactly_once(engine, monkeypatch):
    patch_bundles(monkeypatch, fail_for={1})
    seed(engine, xid=1)
    calls = count_seals(monkeypatch)

    with pytest.raises(runtime.NormalizationRunError):
        runtime.normalize(engine)

    assert len(calls) == 1


def test_write_failure_seals_exactly_once(engine, monkeypatch):
    seed(engine, xid=1)
    add_extraction(engine, xid=2)
    calls = count_seals(monkeypatch)

    with pytest.raises(runtime.NormalizationRunError):
        runtime.normalize(engine, page_size=1, storage=_FailsOnSecondInsert())

    assert len(calls) == 1


def test_reconciliation_failure_seals_exactly_once(engine, monkeypatch):
    seed(engine)

    def broken(receipt):
        raise ReceiptError("synthetic")

    monkeypatch.setattr(runtime, "check_pre_seal_receipt", broken)
    calls = count_seals(monkeypatch)

    with pytest.raises(runtime.NormalizationRunError):
        runtime.normalize(engine)

    assert len(calls) == 1
