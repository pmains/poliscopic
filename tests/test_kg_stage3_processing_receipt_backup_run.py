"""Focused regression tests for the canonical Stage 3 backup runner.

Defects pinned here were all observed in real runs of the bootstrap:

* The temporary restore cluster could not start, because the launching environment
  named no resolvable locale and PostgreSQL 18 refuses a postmaster that becomes
  multithreaded during startup (what macOS locale initialization does then).
* The failure path dereferenced an uncaptured ``stderr``, so instead of reporting
  ``pg_ctl: could not start server`` it raised ``AttributeError: 'NoneType' object
  has no attribute 'strip'``, masking the cause of 132 consecutive failures.
* A single unsectioned restore loaded the receipt data before the functions its
  generated columns call, failing the whole verification.

None of these tests needs a live cluster or a database.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest

from scripts.kg import stage3_processing_receipt_backup_run as backup_run


# --------------------------------------------------------------------------- #
# Defect 1 and 2: locale, and a failure path that reports instead of crashing
# --------------------------------------------------------------------------- #


def test_uncaptured_failure_reports_command_and_status_instead_of_masking_it():
    """The reported defect: with capture=False, reporting itself crashed."""
    with pytest.raises(RuntimeError) as refusal:
        backup_run._run(["/bin/sh", "-c", "exit 3"], capture=False)
    assert "sh failed with exit status 3" in str(refusal.value)


def test_failure_detail_never_dereferences_a_missing_stream():
    completed = subprocess.CompletedProcess(["/bin/true"], 9, stdout=None, stderr=None)
    assert backup_run._failure_detail(completed, ["/bin/true"]) == "true failed with exit status 9"


def test_failure_detail_prefers_stderr_then_falls_back_to_stdout():
    assert backup_run._failure_detail(
        subprocess.CompletedProcess(["x"], 1, stdout="out", stderr="err"), ["x"]) == "x failed (1): err"
    assert backup_run._failure_detail(
        subprocess.CompletedProcess(["x"], 1, stdout="out", stderr=None), ["x"]) == "x failed (1): out"
    assert backup_run._failure_detail(
        subprocess.CompletedProcess(["x"], 1, stdout=None, stderr=None),
        ["x"]) == "x failed with exit status 1"


def test_captured_failure_still_reports_the_original_stderr():
    with pytest.raises(RuntimeError) as refusal:
        backup_run._run(["/bin/sh", "-c", "echo boom >&2; exit 2"])
    assert "boom" in str(refusal.value)


def test_cluster_env_names_the_c_locale_and_preserves_the_environment(monkeypatch):
    monkeypatch.setenv("POLISCOPIC_BACKUP_TEST_MARKER", "kept")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    env = backup_run._cluster_env()
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    assert env["POLISCOPIC_BACKUP_TEST_MARKER"] == "kept"


def test_cluster_env_overrides_an_unresolvable_ambient_locale(monkeypatch):
    monkeypatch.setenv("LC_ALL", "definitely-not-a-locale")
    monkeypatch.setenv("LANG", "definitely-not-a-locale")
    env = backup_run._cluster_env()
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"


def test_every_cluster_command_is_issued_under_that_locale(monkeypatch):
    """Cluster lifecycle commands must not inherit an unresolved locale."""
    seen: list[dict] = []

    def recorder(command, *, env=None, capture=True):
        seen.append({"command": list(command), "env": env, "capture": capture})
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(backup_run, "_run", recorder)
    backup_run._cluster_run(["pg_ctl", "-D", "cluster", "-w", "start"], capture=False)
    backup_run._cluster_run(["initdb", "-D", "cluster"])
    assert len(seen) == 2
    for call in seen:
        assert call["env"]["LC_ALL"] == "C"
        assert call["env"]["LANG"] == "C"
    assert seen[0]["capture"] is False
    assert seen[1]["capture"] is True


def test_source_no_longer_dereferences_an_uncaptured_stream():
    source = Path(backup_run.__file__).read_text()
    assert "completed.stderr.strip()" not in source
    assert "_cluster_env" in source


# --------------------------------------------------------------------------- #
# Defect 3: sectional restore with a fail-closed generated-column guard
# --------------------------------------------------------------------------- #

DIGEST_FUNCTION = "processing_receipts_canonical_receipt_digest"
HELPER_FUNCTION = "processing_receipts_canonical_json"
GENERATED_EXPRESSION = "processing_receipts_canonical_receipt_digest(receipt_body)"
DIGEST_BODY = "SELECT processing_receipts_canonical_json(value - 'digest')"
DIGEST_SIGNATURE = f"public.{DIGEST_FUNCTION}(jsonb)"
HELPER_SIGNATURE = f"public.{HELPER_FUNCTION}(jsonb)"


class _FakeResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar(self):
        return self._values[0] if self._values else None


class _FakeConnection:
    """Answers only the catalog queries the guard issues."""

    def __init__(self, *, expressions=(), bodies=None, present=()):
        self.expressions = list(expressions)
        self.bodies = dict(bodies or {})
        self.present = set(present)

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "attgenerated" in sql:
            return _FakeResult(self.expressions)
        if "pg_get_functiondef" in sql:
            return _FakeResult(self.bodies.get(params.get("name"), ()))
        if "to_regprocedure" in sql:
            return _FakeResult([params.get("signature") in self.present])
        raise AssertionError(f"unexpected catalog query: {sql}")


class _FakeEngine:
    """Engine stand-in; the guard needs a connection, the comparisons an engine."""

    def __init__(self, connection=None):
        self.connection = connection or _FakeConnection()
        self.disposed = False

    def connect(self):
        return contextlib.nullcontext(self.connection)

    def dispose(self):
        self.disposed = True


class _FakeUrl:
    drivername = "postgresql+psycopg"
    username = "poliscopic"
    password = "not-a-real-password"
    host = "100.91.173.66"
    port = 5432
    database = "poliscopic_dev"


class _FakeDialect:
    name = "postgresql"


class _FakeSource:
    url = _FakeUrl()
    dialect = _FakeDialect()


def test_requirement_is_anchored_in_the_dump_declaration():
    """A requirement may only come from what the dump declares."""
    connection = _FakeConnection(expressions=[GENERATED_EXPRESSION], present=set())
    required = backup_run._required_generated_functions(
        connection, declared={DIGEST_FUNCTION: {"jsonb"}})
    assert required == {DIGEST_FUNCTION: {"jsonb"}}


def test_helper_reached_only_through_a_body_is_required_before_data():
    """The real failure: the digest function exists, its helper does not."""
    declared = {DIGEST_FUNCTION: {"jsonb"}, HELPER_FUNCTION: {"jsonb"}}
    connection = _FakeConnection(expressions=[GENERATED_EXPRESSION],
                                bodies={DIGEST_FUNCTION: [DIGEST_BODY]},
                                present={DIGEST_SIGNATURE})
    assert backup_run._missing_generated_functions(connection, declared=declared) == [
        f"generated-column function is absent in the scratch catalog: {HELPER_SIGNATURE}"]


def test_wrong_signature_refuses_even_though_a_similar_function_exists():
    declared = {DIGEST_FUNCTION: {"jsonb"}, HELPER_FUNCTION: {"jsonb"}}
    connection = _FakeConnection(expressions=[GENERATED_EXPRESSION],
                                bodies={DIGEST_FUNCTION: [DIGEST_BODY]},
                                present={DIGEST_SIGNATURE, f"public.{HELPER_FUNCTION}(text)"})
    assert backup_run._missing_generated_functions(connection, declared=declared) == [
        f"generated-column function is absent in the scratch catalog: {HELPER_SIGNATURE}"]


def test_undeclared_function_cannot_satisfy_its_own_requirement():
    connection = _FakeConnection(expressions=[GENERATED_EXPRESSION], present={DIGEST_SIGNATURE})
    assert backup_run._missing_generated_functions(connection, declared={}) == [
        f"no generated-column function requirement could be derived for "
        f"{backup_run.RECEIPT_TABLE}"]


def test_fully_present_functions_pass_the_guard():
    declared = {DIGEST_FUNCTION: {"jsonb"}, HELPER_FUNCTION: {"jsonb"}}
    connection = _FakeConnection(expressions=[GENERATED_EXPRESSION],
                                bodies={DIGEST_FUNCTION: [DIGEST_BODY]},
                                present={DIGEST_SIGNATURE, HELPER_SIGNATURE})
    assert backup_run._missing_generated_functions(connection, declared=declared) == []


def _stub_restore(monkeypatch, events, *, fail_on=None):
    """Wire _restore_and_compare's collaborators and record the sequence."""
    def section(port, scratch_name, dump_path, value):
        events.append(value)
        if value == fail_on:
            raise RuntimeError(f"{value} section refused")

    monkeypatch.setattr(backup_run, "_restore_section", section)
    monkeypatch.setattr(backup_run, "create_engine", lambda *a, **k: _FakeEngine())
    monkeypatch.setattr(backup_run, "_dump_function_signatures",
                        lambda dump_path: events.append("declared") or {})
    monkeypatch.setattr(backup_run, "_missing_generated_functions",
                        lambda connection, *, declared: events.append("function-check") or [])
    monkeypatch.setattr(backup_run.verify, "capture_counts", lambda engine: {})
    monkeypatch.setattr(backup_run.verify, "capture_schema_signature", lambda engine: {})
    monkeypatch.setattr(backup_run.verify, "capture_integrity", lambda engine: {})
    monkeypatch.setattr(backup_run.verify, "compare_restore",
                        lambda baseline, snapshot: events.append("compare") or [])


def test_pre_data_completes_before_data_and_functions_are_checked_first(monkeypatch):
    events: list[str] = []
    _stub_restore(monkeypatch, events)
    backup_run._restore_and_compare(_FakeSource(), port=1, scratch_name="scratch",
                                    dump_path=Path("dump"), baseline={})
    assert events == ["pre-data", "declared", "function-check", "data", "post-data", "compare"]


def test_data_failure_stops_before_post_data(monkeypatch):
    events: list[str] = []
    _stub_restore(monkeypatch, events, fail_on="data")
    with pytest.raises(RuntimeError, match="data section refused"):
        backup_run._restore_and_compare(_FakeSource(), port=1, scratch_name="scratch",
                                        dump_path=Path("dump"), baseline={})
    assert events == ["pre-data", "declared", "function-check", "data"]


def test_post_data_failure_prevents_the_comparison(monkeypatch):
    events: list[str] = []
    _stub_restore(monkeypatch, events, fail_on="post-data")
    with pytest.raises(RuntimeError, match="post-data section refused"):
        backup_run._restore_and_compare(_FakeSource(), port=1, scratch_name="scratch",
                                        dump_path=Path("dump"), baseline={})
    assert events == ["pre-data", "declared", "function-check", "data", "post-data"]


def _drive_main(monkeypatch, tmp_path, *, compare):
    """Run main() with every external removed, returning what it recorded."""
    record = {"cluster": [], "written": []}
    monkeypatch.setattr(backup_run, "_source_or_refuse", lambda: _FakeSource())
    monkeypatch.setattr(backup_run.verify, "build_baseline",
                        lambda source, created_at=None: {"digest": "b" * 64})

    def write_immutable(path, body):
        record["written"].append(Path(path).name)
        return "d" * 64

    monkeypatch.setattr(backup_run.artifacts, "write_immutable", write_immutable)
    monkeypatch.setattr(backup_run.artifacts, "load_verified",
                        lambda path: {"digest": "b" * 64})
    monkeypatch.setattr(backup_run, "_run", lambda command, env=None, capture=True:
                        subprocess.CompletedProcess(command, 0, stdout="", stderr=""))
    monkeypatch.setattr(backup_run, "file_sha256", lambda path, chunk_size=None: "c" * 64)
    monkeypatch.setattr(backup_run.os, "chmod", lambda path, mode: None)
    monkeypatch.setattr(backup_run, "_free_port", lambda: 65000)
    monkeypatch.setattr(backup_run, "_cluster_run", lambda command, capture=True:
                        record["cluster"].append(list(command))
                        or subprocess.CompletedProcess(command, 0))
    monkeypatch.setattr(backup_run, "_restore_and_compare", compare)
    monkeypatch.setattr(backup_run.verify, "build_receipt", lambda **kwargs: {})
    return record


def _reporting():
    return {"n": 0}


def test_data_failure_issues_no_receipt_and_still_tears_the_server_down(monkeypatch, tmp_path, capsys):
    def compare(source, *, port, scratch_name, dump_path, baseline):
        raise RuntimeError("data copy refused")

    record = _drive_main(monkeypatch, tmp_path, compare=compare)
    with pytest.raises(RuntimeError, match="data copy refused"):
        backup_run.main(["--out-dir", str(tmp_path)])
    assert any("stop" in command for command in record["cluster"]), "temporary server not stopped"
    assert not [name for name in record["written"] if name.startswith("kg-stage2-backup-receipt")]


def test_post_data_failure_issues_no_receipt_and_still_tears_the_server_down(monkeypatch, tmp_path, capsys):
    def compare(source, *, port, scratch_name, dump_path, baseline):
        raise RuntimeError("post-data refused")

    record = _drive_main(monkeypatch, tmp_path, compare=compare)
    with pytest.raises(RuntimeError, match="post-data refused"):
        backup_run.main(["--out-dir", str(tmp_path)])
    assert any("stop" in command for command in record["cluster"]), "temporary server not stopped"
    assert not [name for name in record["written"] if name.startswith("kg-stage2-backup-receipt")]


def test_successful_verification_issues_the_receipt_after_teardown(monkeypatch, tmp_path, capsys):
    record = _drive_main(monkeypatch, tmp_path,
                         compare=lambda *a, **k: None)
    assert backup_run.main(["--out-dir", str(tmp_path)]) == 0
    assert any(name.startswith("kg-stage2-backup-receipt") for name in record["written"])
    assert any("stop" in command for command in record["cluster"])
