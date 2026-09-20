#!/usr/bin/env python3
"""Regression fixtures for the reference-propagation contract.

Seven known shapes, all SYNTHETIC and local — no production data access:

  1. Chandler split        — old registered code plus meetings on the missing
                             canonical code
  2. Mesa equivalent       — the same shape in a second jurisdiction
  3. phoenix-gp shape      — a missing parent referenced by many meetings
  4. sentinel shape        — a `__skip__` body value
  5. parent collision      — same code, non-identical identity
  6. clean replay          — already satisfied: a genuine no-op
  7. rollback              — simulated mid-operation failure leaves zero partial state

The four observed body-code values are regression EXAMPLES only; the contract is a
general invariant and is not keyed to any specific code.

No network, no production connection, no non-fixture apply path.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts" / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

from propagation_contract import (  # noqa: E402
    DEPENDENCY_EDGES,
    OPERATION_KIND,
    PUBLIC_BODY_ID_COLUMN,
    SENTINELS,
    apply_transactionally,
    contract_digest,
    deletion_problems,
    evaluate_propagation,
    is_sentinel,
    ordering_problems,
    postcondition_problems,
)


def _parent(code, pid, identity):
    return {"body_code": code, "id": pid, "identity": identity}


def _dependent(key, body=None, fk=None, current=None):
    row = {"key": key, "body": body, "public_body_id": fk}
    if current is not None:
        row["current_body"] = current
    return row


# ── 1. Chandler split ────────────────────────────────────────────────────


def test_chandler_split_missing_canonical_parent_fails_closed():
    """Old code is registered; meetings point at the missing canonical code."""
    result = evaluate_propagation(
        incoming_parents=[],
        existing_parents=[_parent("chandler-planning-zoning", 101, "Chandler P&Z")],
        dependents=[_dependent(1, body="chandler-pz"), _dependent(2, body="chandler-pz")],
    )
    assert not result.ok
    assert result.operations == []
    assert any("missing parent for code 'chandler-pz'" in p for p in result.problems)


def test_chandler_split_canonical_parent_supplied_propagates_parent_first():
    result = evaluate_propagation(
        incoming_parents=[_parent("chandler-pz", 201, "Chandler Planning & Zoning")],
        existing_parents=[],
        dependents=[_dependent(1, body="chandler-pz"), _dependent(2, body="chandler-pz")],
    )
    assert result.ok, result.problems
    ops = result.operations
    assert ops[0]["op"] == "upsert_parent"
    assert ops[0]["body_code"] == "chandler-pz"
    assert all(o["op"] == "upsert_dependent" for o in ops[1:])
    assert len(ops) == 3


# ── 2. Mesa equivalent ───────────────────────────────────────────────────


def test_mesa_equivalent_split_fails_closed_and_then_resolves():
    bad = evaluate_propagation(
        incoming_parents=[],
        existing_parents=[_parent("mesa-planning-zoning", 102, "Mesa P&Z")],
        dependents=[_dependent(10, body="mesa-pz")],
    )
    assert not bad.ok
    assert any("mesa-pz" in p for p in bad.problems)

    good = evaluate_propagation(
        incoming_parents=[_parent("mesa-pz", 202, "Mesa Planning & Zoning")],
        existing_parents=[],
        dependents=[_dependent(10, body="mesa-pz")],
    )
    assert good.ok, good.problems
    assert good.operations[0] == {
        "op": "upsert_parent", "table": "public_bodies",
        "body_code": "mesa-pz", "identity": "Mesa Planning & Zoning",
    }


# ── 3. phoenix-gp shape: missing parent, many meetings ───────────────────


def test_missing_parent_with_many_meetings_is_one_clear_failure():
    dependents = [_dependent(i, body="phoenix-gp") for i in range(1, 41)]
    result = evaluate_propagation(
        incoming_parents=[], existing_parents=[], dependents=dependents,
    )
    assert not result.ok
    assert result.operations == []
    # one problem per dependent, and none of them produce operations
    assert len(result.problems) == 40
    assert all("phoenix-gp" in p for p in result.problems)


def test_missing_parent_is_resolved_by_supplying_it_once():
    dependents = [_dependent(i, body="phoenix-gp") for i in range(1, 41)]
    result = evaluate_propagation(
        incoming_parents=[_parent("phoenix-gp", 300, "Phoenix General Plan")],
        existing_parents=[], dependents=dependents,
    )
    assert result.ok, result.problems
    assert result.operations[0]["op"] == "upsert_parent"
    assert len(result.operations) == 41


# ── 4. sentinel shape ────────────────────────────────────────────────────


def test_sentinel_dependent_without_fk_fails_closed():
    result = evaluate_propagation(
        incoming_parents=[], existing_parents=[],
        dependents=[_dependent(15841, body="__skip__")],
    )
    assert not result.ok
    assert any("sentinel/empty body" in p for p in result.problems)


def test_sentinel_is_never_promoted_to_a_parent():
    result = evaluate_propagation(
        incoming_parents=[_parent("__skip__", 1, "nonsense")],
        existing_parents=[], dependents=[],
    )
    assert not result.ok
    assert any("refusing to promote sentinel" in p for p in result.problems)


def test_sentinel_predicate_covers_all_declared_values():
    for value in SENTINELS:
        assert is_sentinel(value)
    assert is_sentinel(None)
    assert is_sentinel("   ")
    assert not is_sentinel("chandler-pz")


# ── 5. parent collision with non-identical identity ──────────────────────


def test_parent_collision_with_different_identity_is_refused():
    result = evaluate_propagation(
        incoming_parents=[_parent("chandler-pz", 201, "Chandler Planning & Zoning")],
        existing_parents=[_parent("chandler-pz", 201, "Chandler P&Z (legacy)")],
        dependents=[_dependent(1, body="chandler-pz")],
    )
    assert not result.ok
    assert any("conflicting parent identity" in p for p in result.problems)
    assert result.operations == []


def test_duplicate_alias_with_two_ids_is_refused():
    result = evaluate_propagation(
        incoming_parents=[_parent("dup-code", 1, "Same")],
        existing_parents=[_parent("dup-code", 2, "Same")],
        dependents=[],
    )
    assert not result.ok
    assert any("duplicate alias" in p or "multiple ids" in p for p in result.problems)


def test_ambiguous_alias_between_two_registered_codes_is_refused():
    result = evaluate_propagation(
        incoming_parents=[],
        existing_parents=[_parent("a-code", 1, "A"), _parent("b-code", 2, "B")],
        dependents=[_dependent(1, body="a-code")],
        aliases={"a-code": "b-code"},
    )
    assert not result.ok
    assert any("ambiguous mapping" in p for p in result.problems)


# ── 6. clean replay / no-op ──────────────────────────────────────────────


def test_clean_replay_is_a_genuine_no_op():
    result = evaluate_propagation(
        incoming_parents=[],
        existing_parents=[_parent("chandler-pz", 201, "Chandler Planning & Zoning")],
        dependents=[_dependent(1, body="chandler-pz", current="chandler-pz")],
    )
    assert result.ok, result.problems
    assert result.operations == []


def test_replay_with_drift_emits_the_operation():
    result = evaluate_propagation(
        incoming_parents=[],
        existing_parents=[_parent("chandler-pz", 201, "Chandler Planning & Zoning")],
        dependents=[_dependent(1, body="chandler-pz", current="chandler-planning-zoning")],
    )
    assert result.ok, result.problems
    assert len(result.operations) == 1
    assert result.operations[0]["op"] == "upsert_dependent"


def test_idempotent_plan_digest_is_stable():
    payload = {"a": 1, "b": [1, 2, 3]}
    assert contract_digest(payload) == contract_digest({"b": [1, 2, 3], "a": 1})
    assert contract_digest(payload) != contract_digest({"a": 2, "b": [1, 2, 3]})


# ── 7. rollback / zero partial state ─────────────────────────────────────


@pytest.fixture()
def fixture_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE public_bodies (body_code TEXT PRIMARY KEY, name TEXT);"
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT);"
        "INSERT INTO meetings (id, body) VALUES (1, 'chandler-planning-zoning');"
        "INSERT INTO meetings (id, body) VALUES (2, 'chandler-planning-zoning');"
    )
    yield conn
    conn.close()


def _ops():
    result = evaluate_propagation(
        incoming_parents=[_parent("chandler-pz", 201, "Chandler Planning & Zoning")],
        existing_parents=[],
        dependents=[_dependent(1, body="chandler-pz"), _dependent(2, body="chandler-pz")],
    )
    assert result.ok, result.problems
    return result.operations


def test_apply_succeeds_atomically(fixture_db):
    receipt = apply_transactionally(fixture_db, _ops())
    assert receipt["applied"] == 3
    assert receipt["rolled_back"] is False
    codes = [r[0] for r in fixture_db.execute("SELECT body_code FROM public_bodies")]
    assert codes == ["chandler-pz"]
    bodies = sorted(r[0] for r in fixture_db.execute("SELECT body FROM meetings"))
    assert bodies == ["chandler-pz", "chandler-pz"]


def test_mid_operation_failure_leaves_zero_partial_state(fixture_db):
    before_parents = list(fixture_db.execute("SELECT * FROM public_bodies"))
    before_meetings = list(fixture_db.execute("SELECT * FROM meetings"))

    receipt = apply_transactionally(fixture_db, _ops(), fail_after=1)

    assert receipt["rolled_back"] is True
    assert receipt["applied"] == 0
    assert "simulated mid-operation failure" in receipt["error"]
    # nothing partial survived
    assert list(fixture_db.execute("SELECT * FROM public_bodies")) == before_parents
    assert list(fixture_db.execute("SELECT * FROM meetings")) == before_meetings


def test_applier_refuses_anything_that_is_not_a_sqlite_fixture():
    """There is no non-fixture apply path: a non-sqlite3 target is refused."""
    for not_a_fixture in (None, "postgresql://prod/db", Path("/tmp/x.db"), object()):
        with pytest.raises(TypeError, match="fixture-only"):
            apply_transactionally(not_a_fixture, [])


# ── deletion guard, postconditions, ordering ─────────────────────────────


def test_cannot_delete_a_parent_while_dependents_remain():
    problems = deletion_problems(
        parents_to_delete=[_parent("chandler-pz", 201, "Chandler P&Z")],
        remaining_dependents=[_dependent(1, body="chandler-pz")],
    )
    assert problems and "cannot delete" in problems[0]


def test_can_delete_a_parent_once_no_dependents_remain():
    assert deletion_problems(
        parents_to_delete=[_parent("gone", 9, "Gone")],
        remaining_dependents=[_dependent(1, body="chandler-pz")],
    ) == []


def test_newly_introduced_dangling_reference_fails_postcondition():
    problems = postcondition_problems(
        dangling_before={}, dangling_after={"chandler-pz": 1},
    )
    assert problems and "postcondition failed" in problems[0]


def test_preexisting_dangling_reference_is_not_a_new_failure():
    assert postcondition_problems(
        dangling_before={"legacy": 3}, dangling_after={"legacy": 3},
    ) == []


def test_unrelated_state_change_fails_postcondition():
    problems = postcondition_problems(
        dangling_before={}, dangling_after={},
        unrelated_before=10, unrelated_after=9,
    )
    assert problems and "unrelated rows changed" in problems[0]


def test_parents_must_precede_dependents_in_apply_order():
    assert ordering_problems(("jurisdictions", "public_bodies", "meetings")) == []
    assert ordering_problems(("meetings", "public_bodies")) != []
    assert ("public_bodies", "meetings") in DEPENDENCY_EDGES


def test_integer_fk_representation_is_covered():
    """public_body_id with no parent is a failure even when the code resolves."""
    result = evaluate_propagation(
        incoming_parents=[_parent("chandler-pz", 201, "Chandler P&Z")],
        existing_parents=[],
        dependents=[_dependent(1, body="chandler-pz", fk=999)],
    )
    assert not result.ok
    assert any(PUBLIC_BODY_ID_COLUMN in p for p in result.problems)


def test_operation_kind_is_op_repair():
    assert OPERATION_KIND == "OP-REPAIR"


# ── the ordinary sync path cannot silently omit reference tables ─────────


def _sync_declarations():
    """Import the REAL sync declarations (no connection is opened)."""
    import importlib

    db_dir = Path(__file__).resolve().parents[1] / "scripts" / "db"
    if str(db_dir) not in sys.path:
        sys.path.insert(0, str(db_dir))
    return importlib.import_module("sync_declarations")


def test_real_sync_table_order_satisfies_the_contract_edges():
    """Parents precede dependents in the REAL sync table list."""
    decl = _sync_declarations()
    tables = list(decl.ALL_SYNC_TABLES)
    assert ordering_problems(tables) == [], (
        f"sync table order violates the dependency contract: "
        f"{ordering_problems(tables)}"
    )


def test_reference_tables_cannot_be_silently_dropped_from_sync():
    """Every table in the dependency edges must be declared for sync.

    This is the guard against silently omitting required reference-table changes:
    dropping ``public_bodies`` (or ``meetings``) from the sync declarations, or
    excluding them, fails here rather than passing unnoticed.
    """
    decl = _sync_declarations()
    tables = set(decl.ALL_SYNC_TABLES)
    excluded = set(getattr(decl, "EXCLUDED_TABLES", set()))
    local_only = set(getattr(decl, "LOCAL_ONLY_TABLES", set()))

    for parent, dependent in DEPENDENCY_EDGES:
        for table in (parent, dependent):
            assert table in tables, f"{table!r} is missing from ALL_SYNC_TABLES"
            assert table not in excluded, f"{table!r} must not be excluded from sync"
            assert table not in local_only, f"{table!r} must not be local-only"


def test_reconcile_order_is_children_first_for_reference_tables():
    """public_bodies must be reconciled AFTER meetings (children first)."""
    decl = _sync_declarations()
    order = list(getattr(decl, "RECONCILE_ORDER", []))
    assert "public_bodies" in order and "meetings" in order
    assert order.index("meetings") < order.index("public_bodies"), (
        "deleting a parent before its dependents would orphan rows"
    )


def test_contract_and_gate_agree_on_the_dependency_direction():
    """The gate's REQUIRED_ORDER must be a subset of the contract's edges."""
    from reference_integrity_gate import REQUIRED_ORDER

    assert REQUIRED_ORDER == DEPENDENCY_EDGES
