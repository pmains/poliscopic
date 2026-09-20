"""Offline atomicity tests for production stale-row reconciliation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from db import sync_reconcile  # noqa: E402


def _engine_with_public_schema():
    """SQLite fixture with a PostgreSQL-like ``public`` schema name."""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS public"))
        for table in ("child_rows", "parent_rows"):
            connection.execute(text(f"CREATE TABLE public.{table} (id INTEGER PRIMARY KEY)"))
    return engine


def _ids(engine, table):
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(
            text(f"SELECT id FROM public.{table} ORDER BY id"))]


def _fixtures():
    dev, prod = _engine_with_public_schema(), _engine_with_public_schema()
    for engine, ids in ((dev, (1,)), (prod, (1, 2))):
        with engine.begin() as connection:
            for table in ("child_rows", "parent_rows"):
                for ident in ids:
                    connection.execute(text(f"INSERT INTO public.{table} (id) VALUES (:id)"), {"id": ident})
    return dev, prod


def test_mid_reconcile_failure_rolls_back_every_table_and_propagates(monkeypatch):
    dev, prod = _fixtures()
    monkeypatch.setattr(sync_reconcile, "RECONCILE_ORDER", ("child_rows", "parent_rows"))
    monkeypatch.setattr(sync_reconcile, "_pk_cols", lambda _engine, _table: ["id"])
    with prod.begin() as connection:
        connection.execute(text("""
            CREATE TRIGGER public.fail_parent_delete BEFORE DELETE ON parent_rows
            BEGIN SELECT RAISE(FAIL, 'forced reconcile failure'); END
        """))

    with pytest.raises(Exception, match="forced reconcile failure"):
        sync_reconcile._reconcile(dev, prod)

    # The child deletion happened first but was part of the same transaction.
    assert _ids(prod, "child_rows") == [1, 2]
    assert _ids(prod, "parent_rows") == [1, 2]


def test_dry_run_remains_read_only_and_reports_the_full_plan(monkeypatch):
    dev, prod = _fixtures()
    monkeypatch.setattr(sync_reconcile, "RECONCILE_ORDER", ("child_rows", "parent_rows"))
    monkeypatch.setattr(sync_reconcile, "_pk_cols", lambda _engine, _table: ["id"])

    assert sync_reconcile._reconcile(dev, prod, dry_run=True) == 2
    assert _ids(prod, "child_rows") == [1, 2]
    assert _ids(prod, "parent_rows") == [1, 2]
