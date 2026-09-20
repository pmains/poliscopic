"""Audit-timestamp contract regressions (the public_bodies NOT NULL defect).

The defect these prevent: the canonical `public_bodies` INSERT omitted `created_at`
and `updated_at`, which are `NOT NULL` with no server default, so the apply aborted
and rolled back.

The tests assert the fix, not just the symptom: the three inserts succeed inside one
transaction against a schema that faithfully mirrors the PostgreSQL NOT NULL
contract, the audit timestamps are non-null and transaction-consistent, the SQL stays
parameterized apart from the single reviewed expression, and rollback stays atomic.

Isolated: no PostgreSQL, no network, no real apply.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from scripts.kg import stage1_apply_runner as runner
from scripts.kg import stage1_packet_components as components
from scripts.kg import stage1_runner_checks as checks

from test_kg_stage1_packet_v2 import (
    FIXED_NOW,
    _counts,
    _packet,
    _receipt,
    stage1_test_engine,
)


@pytest.fixture
def engine():
    return stage1_test_engine()


# -- the reviewed expression ---------------------------------------------------

def test_generated_sql_is_parameterized_except_the_reviewed_expression():
    sql = runner.body_insert_sql()
    assert sql.count("now()") == len(components.AUDIT_TIMESTAMP_COLUMNS)
    # every value column is a bind parameter; no literals are interpolated
    for column in runner._BODY_INSERT_VALUE_COLUMNS:
        assert f":{column}" in sql
    assert "2026-" not in sql and "'" not in sql


def test_the_audit_expression_is_transaction_start_not_clock_timestamp():
    """`now()` is constant within a transaction; `clock_timestamp()` is not."""
    assert components.AUDIT_TIMESTAMP_EXPRESSION == "now()"
    assert "clock_timestamp" not in runner.body_insert_sql()


def test_audit_columns_are_exactly_the_required_ones():
    assert components.AUDIT_TIMESTAMP_COLUMNS == ("created_at", "updated_at")
    contract = components.audit_timestamp()
    assert contract["columns"] == ["created_at", "updated_at"]
    assert contract["expression"] == "now()"
    semantics = contract["semantics"].lower()
    assert "transaction-start" in semantics
    assert "invented" in semantics  # states plainly that no historical time is fabricated
    assert "consistent" in semantics


def test_contract_is_bound_into_the_canonical_expectation():
    expected = components.canonical_expectation()
    assert expected["audit_timestamp"] == components.audit_timestamp()


# -- the schema is faithful, so the defect would reproduce ---------------------

def test_the_test_schema_reproduces_the_original_failure(engine):
    """An insert omitting the audit columns must fail, as it did on development."""
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO public_bodies (body_code, name, slug, body_type, "
                "jurisdiction_id, description) "
                "VALUES ('x', 'n', 's', 'Committee', 4, NULL)"))


# -- the three inserts succeed, with consistent audit timestamps ---------------

def test_all_three_inserts_succeed_inside_the_transaction(engine):
    terminal = runner.apply_packet(engine, _packet(), receipt=_receipt(),
                                   require_transactional_ddl=False,
                                   integrity_provider=lambda connection: {})
    assert terminal["status"] == "applied", terminal["problems"]
    assert terminal["rowcounts"]["public_bodies_inserts"] == 3
    assert _counts(engine)["public_bodies"] == 3


def test_audit_timestamps_are_non_null_and_consistent(engine):
    runner.apply_packet(engine, _packet(), receipt=_receipt(),
                        require_transactional_ddl=False,
                        integrity_provider=lambda connection: {})
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT body_code, created_at, updated_at FROM public_bodies ORDER BY id"
        )).all()
    assert len(rows) == 3
    stamps = {(row[1], row[2]) for row in rows}
    assert len(stamps) == 1, "all three inserts must share one transaction timestamp"
    created, updated = stamps.pop()
    assert created is not None and updated is not None
    assert created == updated == FIXED_NOW


def test_rollback_remains_atomic(engine):
    before = _counts(engine)
    packet = _packet()
    packet["expected_after_state"]["public_bodies_total"] = 99  # force a failure
    terminal = runner.apply_packet(engine, packet, receipt=_receipt(),
                                   require_transactional_ddl=False,
                                   integrity_provider=lambda connection: {})
    assert terminal["status"] == "rolled_back"
    assert terminal["mutations_performed"] == 0
    assert _counts(engine) == before
    with engine.connect() as connection:
        assert int(connection.execute(
            text("SELECT COUNT(*) FROM public_bodies")).scalar()) == 0


# -- a forged audit contract is refused ---------------------------------------

def test_forged_audit_timestamp_is_refused(engine):
    packet = _packet()
    for operation in packet["operations"]:
        if operation["op"] == "INSERT":
            operation["audit_timestamp"] = {
                "expression": "clock_timestamp()",
                "columns": ["created_at"],
                "semantics": "forged",
            }
    problems = checks.canonical_problems(packet)
    assert any("audit timestamp" in problem for problem in problems)


def test_missing_audit_timestamp_is_refused():
    packet = _packet()
    for operation in packet["operations"]:
        if operation["op"] == "INSERT":
            operation.pop("audit_timestamp")
    problems = checks.canonical_problems(packet)
    assert any("audit timestamp" in problem for problem in problems)


def test_forged_audit_timestamp_mutates_nothing(engine):
    packet = _packet()
    for operation in packet["operations"]:
        if operation["op"] == "INSERT":
            operation["audit_timestamp"] = {"expression": "clock_timestamp()",
                                            "columns": ["created_at"], "semantics": "x"}
    before = _counts(engine)
    terminal = runner.apply_packet(engine, packet, receipt=_receipt(),
                                   require_transactional_ddl=False)
    assert terminal["status"] == "refused"
    assert terminal["mutations_performed"] == 0
    assert _counts(engine) == before


# -- scope is unchanged --------------------------------------------------------

def test_scope_is_still_exactly_3_55_18_and_374_18():
    packet = _packet()
    assert packet["counts"]["public_body_inserts"] == 3
    assert packet["counts"]["meeting_updates"] == 55
    assert packet["counts"]["quarantine_updates"] == 18
    expectation = components.canonical_expectation()
    assert len(expectation["repair_ids"]) == 374
    assert len(expectation["quarantine_ids"]) == 18
    assert len(expectation["repair_ids"]) + len(expectation["quarantine_ids"]) == 392


def test_the_only_insert_change_is_the_audit_columns():
    """Values remain the six semantic columns; audit fields come from the expression."""
    sql = runner.body_insert_sql()
    assert list(runner._BODY_INSERT_VALUE_COLUMNS) == [
        "body_code", "name", "slug", "body_type", "jurisdiction_id", "description"]
    payload = json.dumps(_packet())
    assert "created_at" not in payload.split('"values"')[1].split("}")[0]
