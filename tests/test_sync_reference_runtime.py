#!/usr/bin/env python3
"""Real-runtime regressions for reference propagation enforcement.

These exercise the ENFORCEMENT layer the sync path actually calls
(`db.sync_reference`) and the orchestration in `db.sync_runtime.run_sync`, not a
parallel declaration. Where the sync's own SQL is Postgres-specific
(``public."table"`` qualification, advisory locks) the runtime is driven with
stubbed upserts so the ORDERING and ABORT behaviour is still proven.

Required scenarios covered:
  * dev parent exists but predates the checkpoint; target parent missing; meeting
    pending -> the parent is transferred before the meeting
  * parent upsert failure/skip -> no dependent write and no checkpoint advancement
  * integrity-query exception -> validation fails
  * code-string AND integer-FK representations
  * sentinel / collision / ambiguous identity / clean replay
  * dependency + reconcile parity

No production, network, SSH, or real database access. SQLite only, plus stubs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "ops"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import create_engine, text  # noqa: E402

from db import sync_reference as ref  # noqa: E402
from db import sync_runtime  # noqa: E402
from db.sync_declarations import FULL_SYNC_TABLES, propagation_mode  # noqa: E402
from propagation_contract import SENTINELS  # noqa: E402
from propagation_contract import PUBLIC_BODY_DEPENDENTS  # noqa: E402


def _engine(*statements: str):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    return engine


# ── transfer strategy: parents are re-sent in full ───────────────────────


def test_public_bodies_is_full_reference_for_transfer():
    """Gap 1: an incremental filter would never re-select a parent older than the
    checkpoint. Full-reference transfer removes that failure mode entirely."""
    assert "public_bodies" in FULL_SYNC_TABLES
    assert propagation_mode("public_bodies") == "full_reference"
    assert "jurisdictions" in FULL_SYNC_TABLES


def test_audit_stamping_is_separate_from_transfer_strategy():
    """Owner decision 2: full-reference transfer AND mandatory audit stamping."""
    from db.sync_declarations import stamp_required

    assert propagation_mode("public_bodies") == "full_reference"
    assert stamp_required("public_bodies") is True, (
        "defense in depth: an in-place body_code rename must still stamp updated_at"
    )


def test_force_include_returns_the_parent_key_even_when_it_predates_checkpoint():
    """The historical failure: the dev parent exists, so it MUST be selected."""
    dev = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT, name TEXT,"
        " updated_at TEXT)",
        "INSERT INTO public_bodies (id, body_code, name, updated_at)"
        " VALUES (201, 'chandler-pz', 'Chandler P&Z', '2020-01-01 00:00:00')",
    )
    rows = [{"key": 1, "body": "chandler-pz"}]
    assert ref.force_include_parent_keys(dev, rows) == [201]


def test_force_include_fails_closed_when_the_parent_is_absent_from_the_source():
    dev = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT)")
    with pytest.raises(ref.ReferenceGuardError, match="absent from the SOURCE"):
        ref.force_include_parent_keys(dev, [{"key": 1, "body": "gone-code"}])


def test_missing_target_parent_is_detected():
    prod = _engine("CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT)")
    missing = ref.missing_target_parents(prod, codes={"chandler-pz"}, ids={201})
    assert missing == {"codes": ["chandler-pz"], "ids": [201]}


def test_reviewed_dependency_map_matches_every_synced_model_reference():
    from db.models import Base
    from db.sync_declarations import ALL_SYNC_TABLES

    reference_columns = {"body", "body_code", "public_body_id"}
    discovered = {}
    for table_name in ALL_SYNC_TABLES:
        if table_name == "public_bodies":
            continue
        table = Base.metadata.tables.get(table_name)
        if table is None:
            continue
        columns = tuple(c for c in ("body", "body_code", "public_body_id")
                        if c in table.c and c in reference_columns)
        if columns:
            discovered[table_name] = columns
    assert discovered == PUBLIC_BODY_DEPENDENTS


def test_every_public_body_dependent_is_ordered_after_its_parent():
    from db.sync_declarations import ALL_SYNC_TABLES

    parent_index = ALL_SYNC_TABLES.index("public_bodies")
    assert ALL_SYNC_TABLES.index("jurisdictions") < parent_index
    for table in PUBLIC_BODY_DEPENDENTS:
        assert parent_index < ALL_SYNC_TABLES.index(table), table


@pytest.mark.parametrize(
    ("table", "ddl", "insert_sql"),
    [
        ("agenda_items", "body TEXT, public_body_id INTEGER",
         "INSERT INTO agenda_items VALUES (1, 'chandler-pz', 201)"),
        ("body_seats", "public_body_id INTEGER",
         "INSERT INTO body_seats VALUES (1, 201)"),
        ("body_memberships", "public_body_id INTEGER",
         "INSERT INTO body_memberships VALUES (1, 201)"),
        ("meetings", "body TEXT, public_body_id INTEGER",
         "INSERT INTO meetings VALUES (1, 'chandler-pz', 201)"),
    ],
)
def test_exact_target_coverage_for_each_reference_shape(table, ddl, insert_sql):
    dev = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT, name TEXT)",
        "INSERT INTO public_bodies VALUES (201, 'chandler-pz', 'Chandler P&Z')",
        f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {ddl})",
        insert_sql,
    )
    prod = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT, name TEXT)",
        "INSERT INTO public_bodies VALUES (201, 'chandler-pz', 'Chandler P&Z')",
    )
    ref.assert_target_parent_coverage(dev, prod, table)

    missing = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT, name TEXT)")
    with pytest.raises(ref.ReferenceGuardError, match="target parent"):
        ref.assert_target_parent_coverage(dev, missing, table)


def test_target_parent_identity_conflict_refuses():
    dev = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT, name TEXT)",
        "INSERT INTO public_bodies VALUES (201, 'chandler-pz', 'Chandler P&Z')",
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT, public_body_id INTEGER)",
        "INSERT INTO meetings VALUES (1, 'chandler-pz', 201)",
    )
    prod = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT, name TEXT)",
        "INSERT INTO public_bodies VALUES (201, 'chandler-pz', 'Wrong body')",
    )
    with pytest.raises(ref.ReferenceGuardError, match="conflicting target parent"):
        ref.assert_target_parent_coverage(dev, prod, "meetings")


# ── ordering and abort at the runtime ────────────────────────────────────


class _Lock:
    def __init__(self):
        self.closed = False

    def execute(self, _statement):
        class _S:
            def scalar(self):
                return True
        return _S()

    def close(self):
        self.closed = True


class _Prod:
    def __init__(self, lock):
        self._lock = lock

    def connect(self):
        return self._lock


def _drive(monkeypatch, tables, upsert):
    monkeypatch.setattr(sync_runtime, "ALL_SYNC_TABLES", tuple(tables))
    monkeypatch.setattr(sync_runtime, "assert_parity", lambda: None)
    monkeypatch.setattr(sync_runtime, "assert_target_parent_coverage", lambda *_a: None)
    monkeypatch.setattr(sync_runtime, "_column_intersection", lambda *_a: ["id"])
    monkeypatch.setattr(sync_runtime, "_upsert_table", upsert)
    monkeypatch.setattr(sync_runtime, "_validate", lambda *_a: True)
    monkeypatch.setattr(sync_runtime, "_reference_postconditions", lambda *_a: [])
    monkeypatch.setattr(sync_runtime, "_set_last_sync", lambda *_a: None)
    return _Prod(_Lock())


def test_parent_is_transferred_before_the_dependent(monkeypatch):
    """Parent-first apply order, proven at the runtime."""
    order: list[str] = []

    def upsert(_dev, _prod, table, _cols, *, is_full_sync=False,
               advance_checkpoint=True):
        order.append(table)
        return 0

    prod = _drive(monkeypatch, ["jurisdictions", "public_bodies", "meetings"], upsert)
    assert sync_runtime.run_sync(object(), prod) == 0
    assert order.index("public_bodies") < order.index("meetings")
    assert order.index("jurisdictions") < order.index("public_bodies")


def test_parent_skip_aborts_before_the_dependent_write(monkeypatch):
    """Gap 2: a parent skip must abort BEFORE meetings, and the dependent must not
    be written at all."""
    calls: list[str] = []

    def upsert(_dev, _prod, table, _cols, *, is_full_sync=False,
               advance_checkpoint=True):
        calls.append(table)
        return 1 if table == "public_bodies" else 0  # parent skips one row

    prod = _drive(monkeypatch, ["public_bodies", "meetings"], upsert)
    with pytest.raises(ref.ReferenceGuardError, match="skipped"):
        sync_runtime.run_sync(object(), prod)
    assert "public_bodies" in calls
    assert "meetings" not in calls, "no dependent write across an unsatisfied parent"


def test_parent_failure_also_prevents_the_dependent(monkeypatch):
    def upsert(_dev, _prod, table, _cols, *, is_full_sync=False,
               advance_checkpoint=True):
        if table == "public_bodies":
            raise RuntimeError("parent upsert exploded")
        return 0

    prod = _drive(monkeypatch, ["public_bodies", "meetings"], upsert)
    with pytest.raises(RuntimeError, match="parent upsert exploded"):
        sync_runtime.run_sync(object(), prod)


def test_assert_parents_synced_is_a_noop_for_clean_parents():
    ref.assert_parents_synced("meetings", {"public_bodies": 0, "jurisdictions": 0})


def test_assert_parents_synced_refuses_on_any_parent_skip():
    for parent in ref.PARENT_TABLES:
        with pytest.raises(ref.ReferenceGuardError):
            ref.assert_parents_synced("meetings", {parent: 3})


def test_checkpoint_is_not_advanced_on_skip():
    """The upsert layer only advances the checkpoint when nothing was skipped, and
    the runtime aborts before dependents — so the skip is retried, not bypassed."""
    import inspect

    source = inspect.getsource(sync_runtime)
    assert "assert_parents_synced" in source
    upsert_source = Path(REPO_ROOT / "scripts" / "db" / "sync_upsert.py").read_text()
    assert "if skipped_total == 0:" in upsert_source
    assert "Checkpoint NOT advanced" in upsert_source


# ── postconditions: both representations, query failure = failure ────────


def test_scoped_postcondition_detects_code_string_dangling():
    prod = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT)",
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT)",
        "INSERT INTO meetings (id, body) VALUES (1, 'chandler-pz')",
    )
    problems = ref.scoped_dangling_problems(prod, ["chandler-pz"])
    assert any("scoped dangling reference" in p for p in problems)


def test_scoped_postcondition_detects_integer_fk_dangling():
    """Representation 2 — no FK constraint exists, so this must be caught here."""
    prod = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT)",
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT, public_body_id INTEGER)",
        "INSERT INTO meetings (id, body, public_body_id) VALUES (1, NULL, 999)",
    )
    problems = ref.scoped_dangling_problems(prod, ["anything"])
    assert any("scoped dangling FK" in p and "999" in p for p in problems)


def test_scoped_postcondition_clean_when_parents_present():
    prod = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT)",
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT, public_body_id INTEGER)",
        "INSERT INTO public_bodies (id, body_code) VALUES (201, 'chandler-pz')",
        "INSERT INTO meetings (id, body, public_body_id) VALUES (1, 'chandler-pz', 201)",
    )
    assert ref.scoped_dangling_problems(prod, ["chandler-pz"]) == []


def test_scoped_postcondition_reports_unresolved_sentinels():
    prod = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT)",
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT)",
        "INSERT INTO meetings (id, body) VALUES (1, '__skip__')",
    )
    problems = ref.scoped_dangling_problems(prod, [])
    assert any("invalid/unresolved reference" in p for p in problems)


def test_integrity_query_failure_is_a_failure_not_a_pass():
    prod = _engine("CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT)")
    problems = ref.scoped_dangling_problems(prod, ["chandler-pz"])
    assert problems, "a query failure must produce a problem, never a silent pass"


def test_runtime_validation_fails_when_the_integrity_query_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("integrity query failed")

    def upsert(_dev, _prod, _table, _cols, *, is_full_sync=False):
        return 0

    monkeypatch.setattr(sync_runtime, "ALL_SYNC_TABLES", ())
    monkeypatch.setattr(sync_runtime, "_column_intersection", lambda *_a: ["id"])
    monkeypatch.setattr(sync_runtime, "_upsert_table", upsert)
    monkeypatch.setattr(sync_runtime, "_validate", lambda *_a: True)  # counts say OK
    monkeypatch.setattr(sync_runtime, "_reference_postconditions", boom)
    prod = _Prod(_Lock())
    assert sync_runtime.run_sync(object(), prod) == 1, (
        "an integrity-query exception must fail validation overall"
    )


def test_parity_is_invoked_before_the_runtime_takes_a_lock(monkeypatch):
    def refuse():
        raise ref.ReferenceGuardError("parity canary")

    monkeypatch.setattr(sync_runtime, "assert_parity", refuse)
    class NeverConnect:
        def connect(self):
            pytest.fail("parity must run before the lock")
    with pytest.raises(ref.ReferenceGuardError, match="parity canary"):
        sync_runtime.run_sync(object(), NeverConnect())


def test_parent_checkpoint_is_deferred_until_all_postconditions_pass(monkeypatch):
    checkpoints = []
    advance_flags = []

    def upsert(_dev, _prod, _table, _cols, *, is_full_sync=False,
               advance_checkpoint=True):
        advance_flags.append(advance_checkpoint)
        return 0

    prod = _drive(monkeypatch, ["public_bodies"], upsert)
    monkeypatch.setattr(sync_runtime, "_set_last_sync",
                        lambda _engine, table: checkpoints.append(table))
    assert sync_runtime.run_sync(object(), prod) == 0
    assert advance_flags == [False]
    assert checkpoints == ["public_bodies"]


def test_safety_dangling_counter_does_not_swallow_introspection_failure(monkeypatch):
    monkeypatch.setattr(ref, "inspect", lambda _engine: (_ for _ in ()).throw(
        RuntimeError("introspection failed")))
    with pytest.raises(RuntimeError, match="introspection failed"):
        ref.dangling_counts(object())


def test_count_validation_query_exception_is_failure(monkeypatch):
    from db import sync_validate

    class BrokenEngine:
        def connect(self):
            raise RuntimeError("count query failed")

    monkeypatch.setattr(sync_validate, "ALL_SYNC_TABLES", ("meetings",))
    assert sync_validate._validate(BrokenEngine(), BrokenEngine()) is False


def test_newly_dangling_detects_new_rows_and_tolerates_preexisting():
    prod = _engine(
        "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT)",
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT)",
        "INSERT INTO meetings (id, body) VALUES (1, 'legacy-pre-existing')",
    )
    baseline = ref.dangling_counts(prod)
    assert "meetings.body=legacy-pre-existing" in baseline
    assert ref.newly_dangling_problems(prod, baseline) == []
    with prod.begin() as conn:
        conn.execute(text("INSERT INTO meetings (id, body) VALUES (2, 'brand-new')"))
    assert ref.newly_dangling_problems(prod, baseline)


# ── sentinels, parity ────────────────────────────────────────────────────


@pytest.mark.parametrize("value", sorted(SENTINELS))
def test_sentinels_are_never_valid_references(value):
    assert ref.is_sentinel(value)
    assert value not in ref.required_parent_codes([{"key": 1, "body": value}])


def test_sentinel_never_becomes_a_required_parent():
    assert ref.required_parent_codes([{"key": 1, "body": "__skip__"},
                                      {"key": 2, "body": "chandler-pz"}]) == {"chandler-pz"}


def test_dependency_and_reconcile_parity_holds():
    assert ref.parity_problems() == [], "declarations have drifted from the contract"
    ref.assert_parity()


def test_reconcile_order_remains_a_consistent_projection():
    from db.sync_declarations import RECONCILE_ORDER as declared
    from propagation_contract import RECONCILE_ORDER as contract

    shared = [t for t in contract if t in declared]
    projection = [t for t in declared if t in set(contract)]
    assert shared == projection


def test_parity_detects_a_deliberately_broken_reconcile_order(monkeypatch):
    """The parity check must be able to fail, or it proves nothing."""
    import db.sync_reference as module
    import db.sync_declarations as decl
    import propagation_contract as contract

    monkeypatch.setattr(decl, "RECONCILE_ORDER",
                        ["public_bodies", "meetings"], raising=False)
    monkeypatch.setattr(contract, "RECONCILE_ORDER",
                        ["public_bodies", "meetings"], raising=False)
    problems = module.parity_problems()
    assert problems, "parity must detect a parent-before-dependent reconcile order"
    assert any("reconcile must delete" in p for p in problems)
