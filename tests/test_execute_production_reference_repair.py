"""Focused, mocked proofs for the closed OP-REPAIR executor."""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
for candidate in (ROOT, ROOT / "scripts", OPS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

E = importlib.import_module("execute_production_reference_repair")
TARGET = {"database": "poliscopic", "configured_host": "prod.example",
          "configured_port": 25060, "server_address": "192.0.2.8",
          "server_port": 25060, "cluster_system_identifier": "cluster-1"}


class AuthorizedRepairContext:
    def __init__(self, operations, terminal):
        self.operation = "OP-REPAIR"
        self.target = MappingProxyType(TARGET)
        self.schema_sha256 = "s" * 64
        self.operations = tuple(operations)
        ordered = [{"table": operation["table"],
                    "id": operation["primary_key"]["id"],
                    "before": dict(operation["before"])}
                   for operation in self.operations]
        ordered.sort(key=lambda item: (item["table"], item["id"]))
        self.preimage_digest = E._digest(ordered)
        self.quarantine_digest = "d" * 64
        self.integrity_before = MappingProxyType({"orphans": 7973})
        self.integrity_after = MappingProxyType({"orphans": 0})
        self.bindings = MappingProxyType({"g7_digest": "g" * 64})
        self.nonce = "n" * 64
        self.attempt_id = "attempt-1"
        self._test_terminal = terminal
        self._claim = "CLAIMED"


@pytest.fixture(autouse=True)
def exact_g8_type(monkeypatch):
    module = types.ModuleType("production_reference_g8")
    module.AuthorizedRepairContext = AuthorizedRepairContext
    module.assert_claimed_context = lambda context: (
        None if context._claim == "CLAIMED" else (_ for _ in ()).throw(ValueError()))
    module.write_context_terminal = lambda context, receipt: context._test_terminal(receipt)
    monkeypatch.setitem(sys.modules, "production_reference_g8", module)
    preflight = types.ModuleType("production_preflight")
    preflight.resolve_production_url = lambda path: (
        "postgresql://u:p@prod.example:25060/poliscopic")
    monkeypatch.setitem(sys.modules, "production_preflight", preflight)


def operations():
    result = []
    for table, count, offset in (("meetings", 1124, 0),
                                 ("agenda_items", 6849, 10000)):
        for number in range(1, count + 1):
            row_id = offset + number
            before = MappingProxyType({"id": row_id, "public_body_id": None,
                                       "body": "mesa-pz", "title": f"r{row_id}"})
            result.append(MappingProxyType({"kind": "UPDATE", "table": table,
                           "primary_key": MappingProxyType({"id": row_id}),
                           "before": before,
                           "set": MappingProxyType({"public_body_id": 37})}))
    return result


class Adapter:
    def __init__(self):
        self.events = []
        self.rows = {table: {} for table in E.EXPECTED}
        for operation in operations():
            row = dict(operation["before"])
            row["physical_only"] = "must-stay-unchanged"
            self.rows[operation["table"]][operation["primary_key"]["id"]] = row
        self.transaction_snapshot = None
        self.fail_commit = False
        self.bad_rowcount = False
        self.drift = False
        self.missing_expected_key = False
        self.fail_second_update = False

    def connect(self, url): self.events.append(("connect", url)); return self
    def target_identity(self, connection): self.events.append("target"); return TARGET
    def schema_sha256(self, connection): self.events.append("schema"); return "s" * 64
    def execute(self, connection, sql, params=()):
        self.events.append(("sql", sql, params))
        if sql.startswith("BEGIN"):
            self.transaction_snapshot = {
                table: {row_id: dict(row) for row_id, row in rows.items()}
                for table, rows in self.rows.items()}
        if sql.startswith("UPDATE"):
            table = "agenda_items" if "agenda_items" in sql else "meetings"
            if self.fail_second_update and table == "agenda_items":
                raise RuntimeError("injected midpoint failure")
            for row_id, value in zip(*params):
                self.rows[table][row_id]["public_body_id"] = value
            return E.EXPECTED[table] - (1 if self.bad_rowcount and table == "agenda_items" else 0)
        return 0
    def fetch_rows(self, connection, table, row_ids, *, for_update):
        assert for_update is True
        self.events.append(("read", table, tuple(row_ids), "FOR UPDATE"))
        values = [dict(self.rows[table][row_id]) for row_id in row_ids]
        if table == "meetings" and row_ids[0] == 1:
            if self.missing_expected_key:
                values[0].pop("title")
            if self.drift and values[0]["public_body_id"] is not None:
                values[0]["physical_only"] = "post-update-drift"
        return values
    def integrity_vector(self, connection):
        updated = sum(row["public_body_id"] is not None
                      for rows in self.rows.values() for row in rows.values())
        return {"orphans": 0 if updated else 7973}
    def quarantine_digest(self, connection): return "d" * 64
    def commit(self, connection):
        self.events.append("commit")
        if self.fail_commit: raise OSError("lost acknowledgement")
    def rollback(self, connection):
        self.events.append("rollback")
        if self.transaction_snapshot is not None:
            self.rows = self.transaction_snapshot
    def close(self, connection): self.events.append("close")


def context(terminals):
    return AuthorizedRepairContext(operations(), terminals.append)


def _real_artifact(g8, path, body):
    value = {**body, "digest": g8.digest(body)}
    path.write_bytes(g8.canonical_bytes(value))
    os.chmod(path, 0o600)
    return value


def _real_authorized_context(tmp_path, monkeypatch):
    """Exercise the actual G8 artifact validation and durable claim path."""
    monkeypatch.delitem(sys.modules, "production_reference_g8")
    g8 = importlib.import_module("production_reference_g8")
    planned = []
    canonical = []
    for operation in operations():
        before = dict(operation["before"])
        table = operation["table"]
        row_id = operation["primary_key"]["id"]
        planned.append({"kind": "UPDATE", "table": table,
                        "primary_key": {"id": row_id}, "before": before,
                        "set": {"public_body_id": 37},
                        "preimage_digest": E._digest(before)})
        canonical.append({"table": table, "id": row_id, "before": before})
    canonical.sort(key=lambda item: (item["table"], item["id"]))
    preimage_digest = E._digest(canonical)
    now = datetime(2026, 9, 19, 17, 0, tzinfo=timezone.utc)
    g7_path = tmp_path / "real-g7.json"
    g7 = _real_artifact(g8, g7_path, {
        "schema": g8.G7_SCHEMA, "operation": "OP-REPAIR",
        "status": g8.READY_STATUS,
        "authorization": "none - this plan authorizes nothing",
        "applies_anything": False, "created_at": "2026-09-19T16:00:00Z",
        "expires_at": "2026-09-19T19:00:00Z", "nonce": "1" * 64,
        "bindings": {"exact_commit": "a" * 40,
                     "g6_proposal_preimages_digest": preimage_digest},
        "target": TARGET, "schema_sha256": "s" * 64,
        "preimage_digest": preimage_digest, "quarantine_digest": "d" * 64,
        "integrity_before": {"orphans": 7973},
        "integrity_after": {"orphans": 0}, "operations": planned,
        "operation_counts": g8.EXPECTED_COUNTS, "scope": g8.EXPECTED_SCOPE})
    approval = f"I approve OP-REPAIR for the exact G7 digest {g7['digest']}"
    g8_path = tmp_path / "real-g8.json"
    _real_artifact(g8, g8_path, {
        "schema": g8.G8_SCHEMA, "operation": "OP-REPAIR",
        "g7_digest": g7["digest"], "nonce": g7["nonce"],
        "exact_commit": g7["bindings"]["exact_commit"],
        "scope": g8.EXPECTED_SCOPE, "operation_counts": g8.EXPECTED_COUNTS,
        "author": {"kind": "human", "name": "Peter Mains"},
        "approved_at": "2026-09-19T16:30:00Z",
        "expires_at": "2026-09-19T18:00:00Z",
        "approval": {"verbatim": approval,
                     "sha256": hashlib.sha256(approval.encode()).hexdigest()}})
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return g8, g8.authorize(g7_path, g8_path, state, now), state


def test_missing_real_g8_type_refuses_before_url_or_network(monkeypatch):
    monkeypatch.delitem(sys.modules, "production_reference_g8")
    monkeypatch.setattr(E.importlib, "import_module", lambda name: (_ for _ in ()).throw(ImportError()))
    adapter = Adapter()
    with pytest.raises(E.Refused, match="G8"):
        E.execute_authorized(object(), adapter)
    assert adapter.events == []


def test_actual_g8_authorize_context_runs_mocked_executor_and_writes_terminal(
        tmp_path, monkeypatch):
    g8, authorized, state = _real_authorized_context(tmp_path, monkeypatch)
    adapter = Adapter()
    receipt = E.execute_authorized(authorized, adapter)
    assert type(authorized) is g8.AuthorizedRepairContext
    assert receipt.values["terminal"] == "SUCCESS"
    assert g8.inspect_state(state, authorized.g7) == "SUCCESS"


def test_no_boolean_or_subclass_authority_and_no_url_override():
    adapter = Adapter()
    with pytest.raises(E.Refused, match="exact G8"):
        E.execute_authorized(True, adapter)
    class Broadened(AuthorizedRepairContext): pass
    with pytest.raises(E.Refused, match="exact G8"):
        E.execute_authorized(Broadened(operations(), lambda receipt: None), adapter)
    assert adapter.events == []


def test_exact_transaction_lock_read_update_postcondition_and_receipt():
    adapter, terminals = Adapter(), []
    receipt = E.execute_authorized(context(terminals), adapter)
    sqls = [event[1] for event in adapter.events if isinstance(event, tuple) and event[0] == "sql"]
    advisory = next(i for i, sql in enumerate(sqls) if "pg_advisory_xact_lock" in sql)
    table_lock = next(i for i, sql in enumerate(sqls) if sql.startswith("LOCK TABLE"))
    first_read = next(i for i, event in enumerate(adapter.events) if isinstance(event, tuple) and event[0] == "read")
    last_lock_event = max(i for i, event in enumerate(adapter.events)
                          if isinstance(event, tuple) and event[0] == "sql" and
                          ("pg_advisory" in event[1] or event[1].startswith("LOCK TABLE")))
    assert advisory < table_lock
    assert last_lock_event < first_read
    updates = [sql for sql in sqls if sql.startswith("UPDATE")]
    assert len(updates) == 2
    assert all(" SET public_body_id = " in sql for sql in updates)
    assert all("public.meetings" in sql or "public.agenda_items" in sql for sql in updates)
    assert receipt.values["terminal"] == "SUCCESS"
    assert receipt.values["update_rowcounts"] == {"meetings": 1124, "agenda_items": 6849}
    assert terminals == [receipt]
    assert adapter.events[-2:] == ["commit", "close"]
    with pytest.raises(TypeError):
        receipt.values["terminal"] = "FAILED"


def test_g6_preimage_digest_is_table_then_id_canonical_and_ignores_extra_physical_columns():
    adapter, terminals = Adapter(), []
    authorized = context(terminals)
    canonical = [{"table": operation["table"],
                  "id": operation["primary_key"]["id"],
                  "before": dict(operation["before"])}
                 for operation in authorized.operations]
    canonical.sort(key=lambda item: (item["table"], item["id"]))
    assert authorized.preimage_digest == E._digest(canonical)
    receipt = E.execute_authorized(authorized, adapter)
    assert receipt.values["preimage_digest"] == authorized.preimage_digest


def test_missing_authorized_before_key_refuses_before_update():
    adapter, terminals = Adapter(), []
    adapter.missing_expected_key = True
    with pytest.raises(E.Refused, match="missing an authorized preimage key"):
        E.execute_authorized(context(terminals), adapter)
    assert not any(isinstance(event, tuple) and event[0] == "sql" and
                   event[1].startswith("UPDATE") for event in adapter.events)


def test_midpoint_failure_rolls_back_first_table_physical_changes():
    adapter, terminals = Adapter(), []
    baseline = {table: {row_id: dict(row) for row_id, row in rows.items()}
                for table, rows in adapter.rows.items()}
    adapter.fail_second_update = True
    with pytest.raises(E.Refused):
        E.execute_authorized(context(terminals), adapter)
    assert adapter.rows == baseline
    assert "rollback" in adapter.events and "commit" not in adapter.events
    assert terminals[0].values["terminal"] == "FAILED"


@pytest.mark.parametrize("defect", ["preimage", "rowcount", "postcondition"])
def test_every_precommit_failure_rolls_back_both_updates(defect):
    adapter, terminals = Adapter(), []
    if defect == "preimage":
        adapter.rows["meetings"][1]["title"] = "changed"
    elif defect == "rowcount":
        adapter.bad_rowcount = True
    else:
        adapter.drift = True
    with pytest.raises(E.Refused):
        E.execute_authorized(context(terminals), adapter)
    assert "rollback" in adapter.events and "commit" not in adapter.events
    assert terminals[0].values["terminal"] == "FAILED"


def test_missing_extra_and_duplicate_preimages_refuse_before_update(monkeypatch):
    for mutation in ("missing", "extra", "duplicate"):
        adapter, terminals = Adapter(), []
        original = adapter.fetch_rows
        def broken(connection, table, ids, *, for_update, kind=mutation):
            rows = list(original(connection, table, ids, for_update=for_update))
            if table == "meetings" and ids[0] == 1:
                if kind == "missing": rows.pop()
                elif kind == "extra": rows.append({"id": 999999})
                else: rows[-1] = dict(rows[0])
            return rows
        adapter.fetch_rows = broken
        with pytest.raises(E.Refused):
            E.execute_authorized(context(terminals), adapter)
        assert not any(isinstance(event, tuple) and event[0] == "sql" and
                       event[1].startswith("UPDATE") for event in adapter.events)


def test_commit_ambiguity_is_terminal_and_never_rolls_back():
    adapter, terminals = Adapter(), []
    adapter.fail_commit = True
    with pytest.raises(E.CommitUncertain):
        E.execute_authorized(context(terminals), adapter)
    assert terminals[0].values["terminal"] == "COMMIT_UNCERTAIN"
    assert "rollback" not in adapter.events


def test_scope_population_and_canonical_target_refuse_before_connect(monkeypatch):
    adapter, terminals = Adapter(), []
    narrowed = operations()[:-1]
    with pytest.raises(E.Refused, match="population"):
        E.execute_authorized(AuthorizedRepairContext(narrowed, terminals.append), adapter)
    sys.modules["production_preflight"].resolve_production_url = lambda path: (
        "postgresql://u:p@dev.example:25060/poliscopic_dev")
    with pytest.raises(E.Refused, match="resolved production URL"):
        E.execute_authorized(context(terminals), adapter)
    assert adapter.events == []
