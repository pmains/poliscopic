"""Focused unit tests for the G5 preflight.  No network or live database."""

from __future__ import annotations

import importlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
for candidate in (ROOT, ROOT / "scripts", OPS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

P = importlib.import_module("production_preflight")
from scripts.body_code_merge_runtime import PRODUCTION_TARGET


class Scalar:
    def __init__(self, value): self.value = value
    def scalar_one(self): return self.value


class FakeConnection:
    def __init__(self, values):
        self.values = iter(values)
        self.statements = []
    def execute(self, statement):
        self.statements.append(str(statement))
        return Scalar(next(self.values))


class FakeInspector:
    def get_table_names(self, schema=None):
        from scripts.ops.propagation_contract import PUBLIC_BODY_DEPENDENTS
        return ["public_bodies", *PUBLIC_BODY_DEPENDENTS]
    def get_columns(self, table, schema=None):
        from scripts.ops.propagation_contract import PUBLIC_BODY_DEPENDENTS
        if table in PUBLIC_BODY_DEPENDENTS:
            return [{"name": name, "type": "TEXT", "nullable": True}
                    for name in PUBLIC_BODY_DEPENDENTS[table]]
        return [{"name": "id", "type": "INTEGER", "nullable": False}]


def _capture(monkeypatch, *, isolation="repeatable read", read_only="on",
             database="poliscopic", host=None, integrity=None):
    import sqlalchemy
    from scripts.entities import detect_entities
    monkeypatch.setattr(sqlalchemy, "inspect", lambda connection: FakeInspector())
    monkeypatch.setattr(detect_entities, "integrity_snapshot",
                        lambda connection: integrity or {"orphans": 0})
    from scripts.ops.propagation_contract import PUBLIC_BODY_DEPENDENTS
    query_count = sum(2 * len(columns) for columns in PUBLIC_BODY_DEPENDENTS.values())
    connection = FakeConnection(
        [isolation, read_only, database, "10.20.30.40", 25060, "777", "16.4"] +
        [0] * query_count)
    result = P.capture_snapshot(
        connection,
        configured_host=host or PRODUCTION_TARGET["host"],
        configured_port=25060,
        dialect="postgresql", driver="psycopg2",
        captured_at="2026-09-19T12:00:00Z")
    return connection, result


def test_snapshot_accepts_and_records_integrity_debt(monkeypatch):
    connection, result = _capture(monkeypatch, integrity={"orphans": 7, "replay": 0})
    assert result["status"] == "VALID"
    assert result["integrity"]["debt"] == {"stage0.orphans": 7}
    assert result["integrity"]["policy"] == "recorded-not-invalidating"
    assert result["transaction"] == {
        "connection_count": 1, "transaction_count": 1,
        "isolation": "repeatable read", "read_only": True}
    from scripts.ops.propagation_contract import PUBLIC_BODY_DEPENDENTS
    assert len(connection.statements) == 7 + sum(
        2 * len(columns) for columns in PUBLIC_BODY_DEPENDENTS.values())
    assert result["target"]["server_address"] == "10.20.30.40"
    assert result["target"]["cluster_system_identifier"] == "777"


@pytest.mark.parametrize("isolation,read_only", [("read committed", "on"),
                                                   ("repeatable read", "off")])
def test_snapshot_refuses_unproven_transaction(monkeypatch, isolation, read_only):
    with pytest.raises(P.Refused, match="REPEATABLE READ, READ ONLY"):
        _capture(monkeypatch, isolation=isolation, read_only=read_only)


@pytest.mark.parametrize("database,host", [
    ("poliscopic_dev", PRODUCTION_TARGET["host"]),
    ("poliscopic", "same-name.example.invalid"),
])
def test_snapshot_refuses_identity_mismatch(monkeypatch, database, host):
    with pytest.raises(P.Refused, match="pinned production target"):
        _capture(monkeypatch, database=database, host=host)


def test_exclusive_canonical_digest_bound_artifact(tmp_path):
    body = {"z": 2, "a": 1}
    artifact = {**body, "digest": P.digest(body)}
    path = tmp_path / "evidence.json"
    P.write_exclusive(path, artifact)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == P.canonical_bytes(artifact)
    assert json.loads(path.read_text())["digest"] == P.digest(body)
    with pytest.raises(FileExistsError):
        P.write_exclusive(path, artifact)


def test_interlock_runs_before_environment_or_engine(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(P.production_interlock, "check",
                        lambda *a, **k: events.append("interlock") or {
                            "status": "REFUSED", "code": "test"})
    monkeypatch.setenv("PROD_DATABASE_URL", "must-not-be-resolved")
    with pytest.raises(P.Refused, match="interlock refused"):
        P.run(output=tmp_path / "never.json")
    assert events == ["interlock"]
    assert not (tmp_path / "never.json").exists()


def test_source_has_one_connection_and_no_commit_call():
    source = (OPS / "production_preflight.py").read_text()
    assert source.count("engine.connect()") == 1
    assert ".commit(" not in source
    assert "SET TRANSACTION READ ONLY" in source
    assert 'isolation_level="REPEATABLE READ"' in source
    assert "redirect_stdout" in source


def test_url_resolution_refuses_duplicate_and_environment_conflicts(tmp_path,
                                                                    monkeypatch):
    secret = "do-not-leak"
    path = tmp_path / ".env"
    path.write_text(
        f"PROD_DATABASE_URL=postgresql://user:{secret}@one.ondigitalocean.com/poliscopic\n"
        "PROD_DATABASE_URL=postgresql://user:other@two.ondigitalocean.com/poliscopic\n")
    with pytest.raises(P.Refused) as caught:
        P.resolve_production_url(path)
    assert secret not in str(caught.value)

    path.write_text(
        f"PROD_DATABASE_URL=postgresql://user:{secret}@one.ondigitalocean.com/poliscopic\n")
    monkeypatch.setenv(
        "PROD_DATABASE_URL",
        "postgresql://user:another-secret@two.ondigitalocean.com/poliscopic")
    with pytest.raises(P.Refused) as caught:
        P.resolve_production_url(path)
    assert secret not in str(caught.value)
    assert "another-secret" not in str(caught.value)
