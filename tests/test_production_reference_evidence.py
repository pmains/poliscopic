"""Focused offline tests for production reference evidence."""

from __future__ import annotations

import importlib
import json
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
for candidate in (ROOT, ROOT / "scripts", OPS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

E = importlib.import_module("production_reference_evidence")
P = importlib.import_module("production_preflight")
from scripts.body_code_merge_runtime import PRODUCTION_TARGET


def g5_payload(*, registry=None):
    body = {
        "schema": P.SCHEMA, "captured_at": "2026-09-19T12:00:00Z",
        "operation": "OP-PREFLIGHT", "status": "VALID",
        "pinned_target": {"database": PRODUCTION_TARGET["database"],
                          "host": PRODUCTION_TARGET["host"]},
        "target": {
            "database": PRODUCTION_TARGET["database"],
            "configured_host": PRODUCTION_TARGET["host"],
            "configured_port": 25060, "server_address": "10.2.3.4",
            "server_port": 25060, "cluster_system_identifier": "777",
        },
        "transaction": {"connection_count": 1, "transaction_count": 1,
                        "isolation": "repeatable read", "read_only": True},
        "integrity": {"registry_metrics": registry or {}},
    }
    return {**body, "digest": P.digest(body)}


def write_g5(path, payload=None):
    value = payload or g5_payload()
    path.write_bytes(P.canonical_bytes(value))
    return value


def test_g5_digest_target_and_cluster_are_required(tmp_path):
    path = tmp_path / "g5.json"
    original = write_g5(path)
    loaded, digest = E.load_g5(path)
    assert loaded == original
    assert digest == original["digest"]

    for mutation, match in (
        (lambda value: value.update(digest="0" * 64), "digest mismatch"),
        (lambda value: value["target"].update(cluster_system_identifier=""),
         "target or cluster"),
        (lambda value: value["pinned_target"].update(database="dev"),
         "pinned production"),
    ):
        value = g5_payload()
        mutation(value)
        if value.get("digest") != "0" * 64:
            body = {k: v for k, v in value.items() if k != "digest"}
            value["digest"] = P.digest(body)
        write_g5(path, value)
        with pytest.raises(E.Refused, match=match):
            E.load_g5(path)


class Scalar:
    def __init__(self, value): self.value = value
    def scalar_one(self): return self.value


class Rows:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self.rows


class FakeConnection:
    def __init__(self, counts, rows):
        self.counts, self.rows, self.statements = counts, rows, []
    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        marker = sql.split("category:", 1)[1].split(" */", 1)[0]
        name, kind = marker.rsplit(":", 1)
        return Scalar(self.counts[name]) if kind == "count" else Rows(self.rows[name])


def test_collect_has_exact_totals_bounded_rows_and_g5_binding():
    counts = {category.name: 2 for category in E.CATEGORIES}
    rows = {category.name: [{"id": 1, "candidate_parent_count": 0}]
            for category in E.CATEGORIES}
    metrics = {key: 0 for category in E.CATEGORIES for key in category.metric_keys}
    for category in E.CATEGORIES:
        metrics[category.metric_keys[0]] = 2
    connection = FakeConnection(counts, rows)
    result = E.collect(connection, row_limit=1, g5=g5_payload(registry=metrics))
    assert set(result) == {category.name for category in E.CATEGORIES}
    assert all(item["total"] == 2 and item["sample_count"] == 1
               and item["truncated"] is True for item in result.values())
    assert len(connection.statements) == 12
    assert all("UPDATE " not in sql.upper() and "DELETE " not in sql.upper()
               and "INSERT " not in sql.upper() for sql, _ in connection.statements)


def test_collect_refuses_drift_from_bound_g5():
    counts = {category.name: 0 for category in E.CATEGORIES}
    rows = {category.name: [] for category in E.CATEGORIES}
    counts[E.CATEGORIES[0].name] = 1
    with pytest.raises(E.Refused, match="differs from bound G5"):
        E.collect(FakeConnection(counts, rows), row_limit=10, g5=g5_payload())


def test_interlock_precedes_g5_or_environment(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(E.production_interlock, "check",
                        lambda *a, **k: events.append("interlock") or {
                            "status": "REFUSED", "code": "test"})
    with pytest.raises(E.Refused, match="interlock refused"):
        E.run(g5_artifact=tmp_path / "absent.json",
              output=tmp_path / "never.json")
    assert events == ["interlock"]
    assert not (tmp_path / "never.json").exists()


def test_output_writer_is_exclusive_mode_0600_and_digest_bound(tmp_path):
    body = {"schema": E.SCHEMA, "g5_binding": {"digest": "a" * 64}}
    artifact = {**body, "digest": P.digest(body)}
    path = tmp_path / "evidence.json"
    P.write_exclusive(path, artifact)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == artifact
    with pytest.raises(FileExistsError):
        P.write_exclusive(path, artifact)


def test_source_has_one_connection_read_only_and_no_apply_path():
    source = (OPS / "production_reference_evidence.py").read_text()
    assert source.count("engine.connect()") == 1
    assert 'isolation_level="REPEATABLE READ"' in source
    assert "SET TRANSACTION READ ONLY" in source
    for forbidden in (".commit(", "UPDATE meetings", "DELETE FROM", "INSERT INTO",
                      "--apply"):
        assert forbidden not in source
