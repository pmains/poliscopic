"""Focused regression tests for the canonical Stage 3 backup runner.

Both defects pinned here were observed in real runs of the bootstrap:

* The temporary restore cluster could not start at all, because the launching
  environment named no resolvable locale and PostgreSQL 18 refuses a postmaster
  that becomes multithreaded during startup (which is what macOS locale
  initialization does in that case).
* The failure path dereferenced an uncaptured ``stderr``, so instead of
  reporting ``pg_ctl: could not start server`` it raised ``AttributeError:
  'NoneType' object has no attribute 'strip'``.  That masked the real cause of
  132 consecutive failed attempts and delayed diagnosis.

Neither test needs a live cluster or a database.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.kg import stage3_processing_receipt_backup_run as backup_run


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
