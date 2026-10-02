#!/usr/bin/env python3
"""The production sync must DECLARE exactly the write set it will touch.

`scripts/db/sync_prod.py` used to consult the interlock with no scope at all.
The interlock's validator requires a declared scope, so every sync was refused
SCOPE_MISSING even when a plan existed — and, more importantly, a caller could
otherwise have its declared scope understate what the run actually writes.

These tests pin BOTH halves of the fix:
  * the write set is derived once (`select_sync_tables`) and shared by the sync
    loop and the interlock declaration, so they cannot drift (parity);
  * missing, altered, and broader scopes are refused, and the refusal happens
    BEFORE any database engine is created.

No production access: refusals occur at the interlock, and `create_engine` is
stubbed to explode if anything tries to open a connection.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

sync_runtime = importlib.import_module("db.sync_runtime")
sync_prod = importlib.import_module("db.sync_prod")
from db.sync_declarations import ALL_SYNC_TABLES  # noqa: E402

SIX = ["agenda_items", "entities", "entity_mentions",
       "entity_relationships", "meetings", "supporting_documents"]


# ── the write-set selector ───────────────────────────────────────────────


def test_unrestricted_selects_every_declared_table():
    assert sync_runtime.select_sync_tables(None) == list(ALL_SYNC_TABLES)


def test_restriction_selects_exactly_those_tables():
    selected = sync_runtime.select_sync_tables(SIX)
    assert sorted(selected) == sorted(SIX)
    assert len(selected) == len(SIX)


def test_restriction_preserves_declared_order():
    selected = sync_runtime.select_sync_tables(SIX)
    assert selected == [t for t in ALL_SYNC_TABLES if t in set(SIX)]


def test_unknown_table_is_refused_not_ignored():
    """Silently dropping an unknown name would make the declaration a lie."""
    with pytest.raises(ValueError) as excinfo:
        sync_runtime.select_sync_tables(["agenda_items", "not_a_table"])
    assert "not_a_table" in str(excinfo.value)


# ── parity: declaration == write set ─────────────────────────────────────


def test_declared_scope_equals_the_write_set(monkeypatch):
    """The interlock scope must be exactly what the sync loop will iterate."""
    captured: dict[str, object] = {}

    fake = types.ModuleType("production_interlock")

    def fake_check(op, entry_point="", scope=None, target="production", now=None,
                   mode=None):
        captured["op"] = op
        captured["entry_point"] = entry_point
        captured["scope"] = list(scope) if scope is not None else None
        captured["target"] = target
        captured["mode"] = mode
        return {"status": "REFUSED", "code": "TEST_STUB", "reason": "stub"}

    fake.check = fake_check
    monkeypatch.setitem(sys.modules, "production_interlock", fake)

    sync_prod._interlock_verdict(False, SIX)

    assert captured["op"] == "OP-RECON"
    assert captured["entry_point"] == "scripts/db/sync_prod.py"
    assert captured["scope"] == sync_runtime.select_sync_tables(SIX)


def test_unrestricted_declaration_covers_every_declared_table(monkeypatch):
    captured: dict[str, object] = {}
    fake = types.ModuleType("production_interlock")
    fake.check = lambda op, entry_point="", scope=None, target="production", \
        now=None, mode=None: (
        captured.update(scope=list(scope) if scope is not None else None)
        or {"status": "REFUSED", "code": "TEST_STUB", "reason": "stub"})
    monkeypatch.setitem(sys.modules, "production_interlock", fake)

    sync_prod._interlock_verdict(False, None)
    assert captured["scope"] == list(ALL_SYNC_TABLES)


def test_explicit_authorization_id_is_passed_to_interlock(monkeypatch):
    captured = {}
    fake = types.ModuleType("production_interlock")

    def fake_check(op, entry_point="", scope=None, target="production", now=None,
                   mode=None, authorization_id=None):
        captured["authorization_id"] = authorization_id
        return {"status": "REFUSED", "code": "TEST_STUB", "reason": "stub"}

    fake.check = fake_check
    monkeypatch.setitem(sys.modules, "production_interlock", fake)
    sync_prod._interlock_verdict(
        False, SIX, "upsert", "maintenance-20260924")
    assert captured["authorization_id"] == "maintenance-20260924"


# ── refusals ─────────────────────────────────────────────────────────────


def test_missing_authorization_refuses(monkeypatch):
    """The validated six-table scope matches the plan, but no authorization
    exists — so it must refuse (this is 'missing scope' at the authority)."""
    allowed, verdict = sync_prod._interlock_verdict(False, SIX)
    assert allowed is False
    assert verdict["status"] == "REFUSED"


def test_altered_scope_is_refused(monkeypatch):
    allowed, verdict = sync_prod._interlock_verdict(False, ["agenda_items"])
    assert allowed is False
    assert verdict["status"] == "REFUSED"


def test_broader_scope_is_refused():
    """Adding a table to the reviewed six must not match the authorization."""
    allowed, verdict = sync_prod._interlock_verdict(False, SIX + ["jurisdictions"])
    assert allowed is False
    assert verdict["status"] == "REFUSED"


def test_unknown_table_in_declaration_refuses():
    allowed, verdict = sync_prod._interlock_verdict(False, ["agenda_items", "nope"])
    assert allowed is False
    assert verdict["code"] == "SCOPE_INVALID"


@pytest.mark.parametrize("kwargs", [
    {"reconcile": True},
    {"reconcile_only": True},
    {"schema_only": True},
    {"bootstrap_schema": True},
])
def test_tables_refused_with_modes_that_touch_other_tables(kwargs):
    """reconcile walks its own fixed order; schema modes touch DDL beyond the set."""
    rc = sync_prod.main(tables=SIX, **kwargs)
    assert rc == 3


# ── refusal precedes any database activity ───────────────────────────────


def test_refusal_happens_before_any_engine_is_created(monkeypatch):
    """No production connection may be opened for a refused sync."""
    opened: list[tuple] = []

    def explode(*args, **kwargs):
        opened.append(args)
        raise AssertionError("create_engine must not be reached before the interlock allows")

    monkeypatch.setattr(sync_prod, "create_engine", explode)

    assert sync_prod.main(tables=SIX) == 3
    assert sync_prod.main(tables=["agenda_items", "bogus"]) == 3
    assert sync_prod.main(tables=None) == 3
    assert opened == []


def test_url_resolution_also_not_reached(monkeypatch):
    """Scope/authorization failures must precede URL resolution too."""
    called: list[str] = []

    monkeypatch.setattr(sync_prod, "_resolve_dev_url",
                        lambda: called.append("dev"))
    monkeypatch.setattr(sync_prod, "_resolve_prod_url",
                        lambda: called.append("prod"))

    assert sync_prod.main(tables=SIX) == 3
    assert called == []
