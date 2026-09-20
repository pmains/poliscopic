#!/usr/bin/env python3
"""Adversarial tests for the development-capable Stage 2 execution path.

Every test here attacks one property: production refusal, plan-derived-only
mutations, deterministic lock order, occupancy and reservation conflicts, schema and
state drift, commit failure, serialization retry, exact no-op replay, and
receipt-owned rollback.  All of it runs on a **SQLite fixture** — nothing in this
file touches ``poliscopic_dev`` or production.
"""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_reservation as reservation  # noqa: E402
from scripts.kg import stage2_s2_admission_tx as tx  # noqa: E402
from scripts.kg import stage2_s2_execute as EX  # noqa: E402
from scripts.kg import stage2_s2_receipt_rollback as RB  # noqa: E402
from scripts.kg import stage2_s2_write_body as WB  # noqa: E402
from tests.kg_stage2_test_fixtures import write_code_current_copy  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"

_AGENDA_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_db_id INTEGER, "
    "agenda_item_number TEXT, agenda_item_title TEXT, agenda_item_text TEXT, "
    "body TEXT, meeting_id TEXT, agenda_item_id TEXT, agenda_item_url TEXT, "
    "vote_or_action TEXT, source_body TEXT, source_url TEXT, c_number TEXT, "
    "c_number_base TEXT, case_number TEXT, agenda_category TEXT, item_type TEXT, "
    "section_level INTEGER, sort_order INTEGER, created_at TIMESTAMP")
AGENDA_DDL = "CREATE TABLE agenda_items (" + _AGENDA_COLUMNS + ", " \
             "UNIQUE (meeting_db_id, agenda_item_number))"
DOCS_DDL = (
    "CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, "
    "agenda_item_db_id INTEGER, agenda_item_id TEXT, agenda_item_number TEXT)")
MEETINGS_DDL = "CREATE TABLE meetings (id INTEGER PRIMARY KEY)"
RESERVATION_DDL = (
    "CREATE TABLE agenda_item_key_reservation (meeting_db_id INTEGER NOT NULL, "
    "agenda_item_number TEXT NOT NULL, plan_digest TEXT NOT NULL, "
    "reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "reserved_by TEXT NOT NULL, "
    "PRIMARY KEY (meeting_db_id, agenda_item_number))")


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


@pytest.fixture(scope="module", autouse=True)
def current_plan_dir(tmp_path_factory):
    """Exercise mechanics with a disposable current-code plan copy.

    The repository artifact remains immutable historical evidence and correctly
    refuses after code drift.
    """
    directory = tmp_path_factory.mktemp("stage2-current-plan")
    source = _live("kg-stage2-s2-label-correction-plan-*.json")
    _, plan = write_code_current_copy(artifacts.load_verified(source), directory,
                                      source.name)
    previous_execute = EX.DEFAULT_PLAN_DIR
    previous_rollback = RB.DEFAULT_PLAN_DIR
    EX.DEFAULT_PLAN_DIR = directory
    RB.DEFAULT_PLAN_DIR = directory
    yield directory, plan
    EX.DEFAULT_PLAN_DIR = previous_execute
    RB.DEFAULT_PLAN_DIR = previous_rollback


@pytest.fixture(scope="module")
def correction_plan(current_plan_dir):
    return current_plan_dir[1]


@pytest.fixture(scope="module")
def correction_path(current_plan_dir):
    return _live("kg-stage2-s2-label-correction-plan-*.json").name


def _fixture(*, seed_documents: bool = True):
    """A SQLite fixture carrying exactly the tables the execution path touches."""
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for ddl in (AGENDA_DDL, DOCS_DDL, MEETINGS_DDL, RESERVATION_DDL):
            c.execute(text(ddl))
    if seed_documents:
        documents = sorted({int(d) for op in
                            WB.operations_for(artifacts.load_verified(
                                _live("kg-stage2-s2-label-correction-plan-*.json")),
                                role="correction")
                            for d in op.document_ids})
        if documents:
            with engine.begin() as c:
                for document_id in documents:
                    c.execute(text("INSERT INTO supporting_documents (id) VALUES (:i)"),
                              {"i": document_id})
    return engine


def _execute(engine, correction_path, correction_plan, tmp_path, **overrides):
    kwargs = dict(engine=engine, plan_path=correction_path, role="correction",
                  plan_digest=artifacts.recorded_digest(correction_plan),
                  approver="Peter Mains", receipt_dir=tmp_path,
                  preimage_dir=tmp_path)
    kwargs.update(overrides)
    return EX.execute_plan(**kwargs)


def _counts(engine):
    with engine.connect() as c:
        return {
            "agenda_items": c.execute(text("SELECT COUNT(*) FROM agenda_items")).scalar(),
            "reservations": c.execute(text(
                "SELECT COUNT(*) FROM agenda_item_key_reservation")).scalar(),
        }


# ══ the public surface carries no arbitrary authority ══════════════════

def test_the_executor_takes_no_callback_sql_table_or_mapping():
    import inspect
    banned = ("callback", "hook", "sql", "table", "mapping", "operation")
    for name, parameter in inspect.signature(EX.execute_plan).parameters.items():
        assert "Callable" not in str(parameter.annotation), name
        assert not any(b in name.lower() for b in banned), name


def test_the_executor_does_not_accept_a_connection():
    """The transaction owner acquires its own connection from an Engine."""
    import inspect
    parameters = inspect.signature(EX.execute_plan).parameters
    assert "engine" in parameters
    assert "connection" not in parameters
    with pytest.raises(EX.ExecuteRefused):
        EX.execute_plan(object(), plan_path="x.json", plan_digest="d", role="correction",
                        approver="P", write_artifacts=False)


def test_an_apply_must_name_an_approver(correction_path, correction_plan, tmp_path):
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(_fixture(), correction_path, correction_plan, tmp_path, approver="  ")
    assert "authorized" in str(exc.value)


def test_a_missing_plan_digest_is_refused(correction_path, correction_plan, tmp_path):
    with pytest.raises(EX.ExecuteRefused):
        _execute(_fixture(), correction_path, correction_plan, tmp_path,
                 plan_digest="")


# ══ production is refused structurally ═════════════════════════════════

def test_a_production_host_engine_is_refused_before_any_connection(
        correction_path, correction_plan, tmp_path):
    class _Engine:
        class url:
            host = "db.ondigitalocean.com"
            database = "poliscopic_dev"

        class dialect:
            name = "postgresql"

        connect_calls = 0

        def connect(self):  # pragma: no cover - only on a regression
            type(self).connect_calls += 1
            raise AssertionError("no connection may be opened")

    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(_Engine(), correction_path, correction_plan, tmp_path)
    assert "production host" in str(exc.value)
    assert _Engine.connect_calls == 0


def test_a_production_database_engine_is_refused(correction_path, correction_plan,
                                                tmp_path):
    class _Engine:
        class url:
            host = "127.0.0.1"
            database = "poliscopic"

        class dialect:
            name = "postgresql"

    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(_Engine(), correction_path, correction_plan, tmp_path)
    assert "production database" in str(exc.value)


# ══ the happy path, and the exact no-op replay ═════════════════════════

def test_a_correction_apply_commits_rows_and_reservations(
        correction_path, correction_plan, tmp_path):
    engine = _fixture()
    result = _execute(engine, correction_path, correction_plan, tmp_path)
    expected = correction_plan["counts"]["new_item_row"]
    assert result["status"] == "committed"
    assert result["commit_status"] == "committed"
    counts = _counts(engine)
    assert counts["agenda_items"] == expected
    assert counts["reservations"] == expected
    assert result["reservations"] == expected
    assert len(result["inserted"]) == expected
    for entry in result["inserted"]:
        assert len(entry["row_fingerprint"]) == 64


def test_a_replay_is_an_exact_no_op(correction_path, correction_plan, tmp_path):
    engine = _fixture()
    first = _execute(engine, correction_path, correction_plan, tmp_path)
    before = _counts(engine)
    replay = EX.replay(engine, plan_path=correction_path,
                       plan_digest=artifacts.recorded_digest(correction_plan),
                       role="correction", approver="Peter Mains")
    assert replay["status"] == "replayed"
    assert replay["commit_status"] == "no-op-already-applied"
    assert replay["writes"] == 0
    assert _counts(engine) == before == {
        "agenda_items": first["rows_after"],
        "reservations": correction_plan["counts"]["new_item_row"]}


def test_the_receipt_and_preimage_are_written_immutably(
        correction_path, correction_plan, tmp_path):
    result = _execute(_fixture(), correction_path, correction_plan, tmp_path)
    receipt = tmp_path / result["artifacts"]["receipt"]["path"]
    preimage = tmp_path / result["artifacts"]["preimage"]["path"]
    assert receipt.exists() and preimage.exists()
    assert oct(receipt.stat().st_mode)[-3:] == "600"
    assert oct(preimage.stat().st_mode)[-3:] == "600"
    loaded = artifacts.load_verified(receipt)
    assert loaded["plan_digest"] == artifacts.recorded_digest(correction_plan)
    assert loaded["commit_status"] == "committed"
    assert loaded["authorized_by"] == "Peter Mains"
    # Immutability: a second write to the same path is a collision, not an overwrite.
    with pytest.raises(artifacts.ArtifactCollision):
        artifacts.write_immutable(receipt, {"kind": "tampered"})


# ══ occupancy and reservation conflicts ════════════════════════════════

def test_an_occupied_live_key_is_refused(correction_path, correction_plan, tmp_path):
    engine = _fixture()
    operation = WB.operations_for(correction_plan, role="correction")[0]
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_items (meeting_db_id, agenda_item_number, "
                       "agenda_item_title) VALUES (:m, :n, 'already here')"),
                  {"m": operation.meeting_db_id, "n": operation.agenda_item_number})
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(engine, correction_path, correction_plan, tmp_path)
    assert "already holds" in str(exc.value)
    assert _counts(engine)["reservations"] == 0
    assert _counts(engine)["agenda_items"] == 1


def test_a_key_reserved_by_another_plan_is_refused(correction_path, correction_plan,
                                                   tmp_path):
    """The concurrent-writer case: another plan got there first."""
    engine = _fixture()
    operation = WB.operations_for(correction_plan, role="correction")[0]
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_item_key_reservation (meeting_db_id, "
                       "agenda_item_number, plan_digest, reserved_by) "
                       "VALUES (:m, :n, :d, 'other')"),
                  {"m": operation.meeting_db_id, "n": operation.agenda_item_number,
                   "d": "f" * 64})
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(engine, correction_path, correction_plan, tmp_path)
    assert "already reserved" in str(exc.value)
    assert _counts(engine)["agenda_items"] == 0
    assert _counts(engine)["reservations"] == 1


def test_a_partially_reserved_plan_is_refused_whole(correction_path, correction_plan,
                                                    tmp_path):
    engine = _fixture()
    operations = WB.operations_for(correction_plan, role="correction")
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_item_key_reservation (meeting_db_id, "
                       "agenda_item_number, plan_digest, reserved_by) "
                       "VALUES (:m, :n, :d, 'other')"),
                  {"m": operations[0].meeting_db_id,
                   "n": operations[0].agenda_item_number, "d": "f" * 64})
    with pytest.raises(EX.ExecuteRefused):
        _execute(engine, correction_path, correction_plan, tmp_path)
    assert _counts(engine) == {"agenda_items": 0, "reservations": 1}


def test_locks_are_taken_in_deterministic_key_order(correction_path, correction_plan,
                                                    tmp_path, monkeypatch):
    """Locks are acquired in ascending key order, in both passes.

    There are two passes by design: the executor locks before it reads occupancy (so
    the read and the later insert cannot interleave), and ``reserve_keys`` locks again
    before it inserts.  Re-acquiring a lock this transaction already holds is a no-op,
    so each pass must be ascending and the two must agree.
    """
    taken: list[str] = []
    real = reservation.lock_key
    monkeypatch.setattr(reservation, "lock_key",
                        lambda c, m, n: (taken.append(reservation.reservation_key(m, n)),
                                         real(c, m, n))[1])
    _execute(_fixture(), correction_path, correction_plan, tmp_path)
    planned = correction_plan["counts"]["new_item_row"]
    assert len(taken) == 2 * planned, len(taken)
    first, second = taken[:planned], taken[planned:]
    assert first == sorted(first), "the occupancy-read pass is not in key order"
    assert second == sorted(second), "the reservation pass is not in key order"
    assert first == second, "the two passes disagree on order"
    assert set(first) == {o["key"] for o in
                          correction_plan["reservation_operations"]["operations"]
                          if o["outcome"] == "reserve"}


# ══ drift: schema, state, code, digest ═════════════════════════════════

def test_a_missing_reservation_table_is_refused(correction_path, correction_plan,
                                                tmp_path):
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for ddl in (AGENDA_DDL, DOCS_DDL, MEETINGS_DDL):
            c.execute(text(ddl))
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(engine, correction_path, correction_plan, tmp_path)
    assert "reservation contract" in str(exc.value)


def test_affected_scope_drift_is_refused(correction_path, correction_plan, tmp_path):
    engine = _fixture()
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(engine, correction_path, correction_plan, tmp_path,
                 expected_scope_sha256="0" * 64)
    assert "drifted" in str(exc.value) or "not the one" in str(exc.value)
    assert _counts(engine)["agenda_items"] == 0


def test_a_pin_that_is_not_the_live_scope_is_refused(correction_path,
                                                   correction_plan, tmp_path):
    """The pin is THIS executor's affected-scope digest, never the plan's.

    ``current_state_sha256`` comes from the S2 current-state machinery over a
    different scope and a different algorithm, so comparing the two could only ever
    refuse a correct apply.  What the pin must do is prove the exact rows and
    documents the caller read are the ones the transaction sees.
    """
    engine = _fixture()
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(engine, correction_path, correction_plan, tmp_path,
                 expected_scope_sha256="0" * 64)
    assert "drifted" in str(exc.value)
    assert _counts(engine) == {"agenda_items": 0, "reservations": 0}


def test_a_correct_pin_is_accepted(correction_path, correction_plan, tmp_path):
    """The pin is usable: the digest the caller reads is the one the apply accepts."""
    engine = _fixture()
    operations = WB.operations_for(correction_plan, role="correction")
    with engine.connect() as connection:
        pinned = EX.affected_scope_digest(connection, operations)
    result = _execute(engine, correction_path, correction_plan, tmp_path,
                      expected_scope_sha256=pinned)
    assert result["status"] == "committed"


def test_a_wrong_plan_digest_is_refused(correction_path, correction_plan, tmp_path):
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(_fixture(), correction_path, correction_plan, tmp_path,
                 plan_digest="0" * 64)
    assert "is not the" in str(exc.value)


def test_bound_code_drift_is_refused(correction_path, correction_plan, tmp_path,
                                     monkeypatch):
    """If a bound module changes, the plan no longer describes the code on disk."""
    from scripts.kg import stage2_s2_admission_binding as AB

    def drift(plan, *a, **k):
        raise AB.ApplyRefused("code has drifted for ['scripts/kg/x.py']")

    monkeypatch.setattr(AB, "_verify_code_hashes", drift)
    with pytest.raises(Exception) as exc:
        _execute(_fixture(), correction_path, correction_plan, tmp_path)
    assert "drifted" in str(exc.value)


# ══ commit failure and serialization retry ═════════════════════════════

def test_a_commit_failure_rolls_the_whole_unit_back(correction_path, correction_plan,
                                                    tmp_path, monkeypatch):
    """A failure at COMMIT must leave neither rows nor reservations behind."""
    engine = _fixture()
    real_begin = tx._begin
    attempts = {"n": 0}

    class _FailingCommit:
        def __init__(self, inner):
            self._inner = inner

        def commit(self):
            attempts["n"] += 1
            raise OperationalError("commit failed", {}, Exception("commit failed"))

        def rollback(self):
            self._inner.rollback()

    monkeypatch.setattr(tx, "_begin", lambda c: _FailingCommit(real_begin(c)))
    with pytest.raises(EX.ExecuteRefused):
        _execute(engine, correction_path, correction_plan, tmp_path)
    assert attempts["n"] >= 1
    assert _counts(engine) == {"agenda_items": 0, "reservations": 0}


def test_a_serialization_failure_reruns_the_whole_unit(correction_path,
                                                       correction_plan, tmp_path,
                                                       monkeypatch):
    """The retry path redoes checks and write together, then commits."""
    engine = _fixture()
    real_begin = tx._begin
    seen = {"n": 0}

    def flaky(connection):
        seen["n"] += 1
        if seen["n"] == 1:
            raise OperationalError("could not serialize access due to concurrent update",
                                   {}, Exception("40001"))
        return real_begin(connection)

    monkeypatch.setattr(tx, "_begin", flaky)
    result = _execute(engine, correction_path, correction_plan, tmp_path)
    assert seen["n"] == 2
    assert result["status"] == "committed"
    assert _counts(engine)["agenda_items"] == correction_plan["counts"]["new_item_row"]


def test_serialization_exhaustion_commits_nothing(correction_path, correction_plan,
                                                  tmp_path, monkeypatch):
    engine = _fixture()

    def always(connection):
        raise OperationalError("could not serialize access", {}, Exception("40001"))

    monkeypatch.setattr(tx, "_begin", always)
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(engine, correction_path, correction_plan, tmp_path)
    assert "serialization" in str(exc.value)
    assert _counts(engine) == {"agenda_items": 0, "reservations": 0}


def test_a_postcondition_failure_rolls_everything_back(correction_path,
                                                       correction_plan, tmp_path,
                                                       monkeypatch):
    engine = _fixture()
    real = WB.postcondition_expectation
    monkeypatch.setattr(WB, "postcondition_expectation",
                        lambda *a, **k: {**real(*a, **k), "row_count": 999_999})
    with pytest.raises(EX.ExecuteRefused) as exc:
        _execute(engine, correction_path, correction_plan, tmp_path)
    assert "postconditions failed" in str(exc.value) or "row count" in str(exc.value)
    assert _counts(engine) == {"agenda_items": 0, "reservations": 0}


# ══ receipt-owned rollback, on the same fixture ════════════════════════

def test_a_receipt_owned_rollback_undoes_an_apply(correction_path, correction_plan,
                                                    tmp_path):
    engine = _fixture()
    result = _execute(engine, correction_path, correction_plan, tmp_path)
    receipt_path = tmp_path / result["artifacts"]["receipt"]["path"]
    digest = artifacts.recorded_digest(correction_plan)
    restored = RB.rollback(engine, receipt_path, plan_path=correction_path,
                           plan_digest=digest,
                           target=result["target"])
    assert restored["status"] == "rolled-back"
    assert restored["deleted_by_natural_key"] is False
    assert len(restored["deleted"]) == correction_plan["counts"]["new_item_row"]
    assert _counts(engine)["agenda_items"] == 0


def test_a_rollback_preflight_writes_nothing(correction_path, correction_plan,
                                             tmp_path):
    engine = _fixture()
    result = _execute(engine, correction_path, correction_plan, tmp_path)
    before = _counts(engine)
    preview = RB.preflight(engine, tmp_path / result["artifacts"]["receipt"]["path"],
                           plan_path=correction_path,
                           plan_digest=artifacts.recorded_digest(correction_plan),
                           target=result["target"])
    assert preview["status"] == "preflight-only"
    assert _counts(engine) == before


def test_a_rollback_refuses_a_production_target(correction_path, correction_plan,
                                                tmp_path):
    class _Engine:
        class url:
            host = "db.ondigitalocean.com"
            database = "poliscopic_dev"

        class dialect:
            name = "postgresql"

    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(_Engine(), "receipt.json", plan_path=correction_path,
                    plan_digest="d", target={})
    assert "production host" in str(exc.value)


def test_a_rollback_refuses_a_receipt_bound_to_another_plan(correction_path,
                                                            correction_plan, tmp_path):
    engine = _fixture()
    result = _execute(engine, correction_path, correction_plan, tmp_path)
    with pytest.raises(RB.RollbackRefused):
        RB.rollback(engine, tmp_path / result["artifacts"]["receipt"]["path"],
                    plan_path=correction_path, plan_digest="0" * 64,
                    target=result["target"])


def test_a_rollback_refuses_when_an_owned_row_has_drifted(correction_path,
                                                          correction_plan, tmp_path):
    engine = _fixture()
    result = _execute(engine, correction_path, correction_plan, tmp_path)
    with engine.begin() as c:
        c.execute(text("UPDATE agenda_items SET agenda_item_title = 'drifted' "
                       "WHERE id = (SELECT MIN(id) FROM agenda_items)"))
    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(engine, tmp_path / result["artifacts"]["receipt"]["path"],
                    plan_path=correction_path,
                    plan_digest=artifacts.recorded_digest(correction_plan),
                    target=result["target"])
    assert "drifted" in str(exc.value)


# ══ mutations stay closed and plan-derived ═════════════════════════════

def test_executed_rows_are_exactly_the_planned_keys(correction_path, correction_plan,
                                                    tmp_path):
    engine = _fixture()
    _execute(engine, correction_path, correction_plan, tmp_path)
    planned = {(op.meeting_db_id, op.agenda_item_number)
               for op in WB.operations_for(correction_plan, role="correction")}
    with engine.connect() as c:
        actual = {(int(r[0]), str(r[1])) for r in c.execute(text(
            "SELECT meeting_db_id, agenda_item_number FROM agenda_items"))}
    assert actual == planned


def test_a_tampered_plan_file_fails_to_load(correction_path, correction_plan,
                                            tmp_path):
    """A hand-edited plan no longer verifies, so it can never be executed."""
    tampered = copy.deepcopy(correction_plan)
    tampered["operations"] = list(tampered["operations"][:-1])
    path = tmp_path / correction_path
    path.write_text(artifacts.canonical_json(tampered))
    with pytest.raises(artifacts.ArtifactDigestMismatch):
        artifacts.load_verified(path)
    with pytest.raises(EX.ExecuteRefused):
        EX.execute_plan(_fixture(), plan_path=correction_path,
                        plan_digest=artifacts.recorded_digest(correction_plan),
                        role="correction", approver="Peter Mains",
                        plan_dir=tmp_path, write_artifacts=False)
