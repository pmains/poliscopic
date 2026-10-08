#!/usr/bin/env python3
"""Silent-success regression tests for the newsletter publish step.

The defect: `_handle_publish_step` wrote `{"status": "failed"}` to
publish-result.json and then RETURNED, so the step wrapper recorded `succeeded`.
Result — no retry, no failure email, and `workflow.state` stuck on `running`,
while the article never reached production.

These tests pin the corrected contract so a refused production push can never be
reported as success again.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "workflows" / "workflow-runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("workflow_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


REFUSAL = json.dumps({"status": "REFUSED", "code": "CODE_CHANGED",
                      "reason": "bound code changed since authorization"})


def _workspace(tmp_path, *, approved=True, items=3):
    run_dir = tmp_path / "run"
    verified = run_dir / "verified"
    output = run_dir / "publish-output"
    verified.mkdir(parents=True)
    output.mkdir(parents=True)
    (verified / "verify-result.json").write_text(json.dumps(
        {"approved": approved, "items": [{"i": n} for n in range(items)]}))
    return run_dir, verified, output


def _patch(monkeypatch, calls, *, dev_rc=0, push_rc=0,
           dev_out="published 2026-09-24-water-environment-watch"):
    """Record every subprocess invocation and script its outcome."""
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if any("editorial_sync.py" in str(part) for part in cmd):
            return _Result(push_rc, stdout="" if push_rc else "synced", stderr="" if not push_rc else REFUSAL)
        if any("publish_newsletter_article.py" in str(part) for part in cmd):
            return _Result(dev_rc, stdout=dev_out, stderr="" if not dev_rc else "boom")
        return _Result(0, stdout="ok")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)


# ── (a) successful dev publish + REFUSED production sync ─────────────────

def test_refused_production_sync_raises_and_preserves_dev_evidence(tmp_path, monkeypatch):
    run_dir, verified, output = _workspace(tmp_path)
    calls: list = []
    _patch(monkeypatch, calls, dev_rc=0, push_rc=3)

    with pytest.raises(runner.StepFailure) as excinfo:
        runner._handle_publish_step("water-environment", run_dir, verified, output,
                                    _logger())

    # it raised, so the step can no longer be recorded as succeeded
    assert "production editorial sync" in str(excinfo.value)

    saved = json.loads((output / "publish-result.json").read_text())
    assert saved["status"] == "failed"
    # the SUCCESSFUL development publication survives as separate evidence
    assert saved["dev_publication"]["status"] == "succeeded"
    assert saved["dev_publication"]["returncode"] == 0
    assert "water-environment-watch" in saved["dev_publication"]["stdout_tail"]
    # and the failed production push is recorded as its own phase
    assert saved["production_push"]["status"] == "refused_or_failed"
    assert saved["production_push"]["returncode"] == 3
    assert "CODE_CHANGED" in saved["production_push"]["reason"]
    assert excinfo.value.result == saved


# ── (b) editorial subprocess non-zero ───────────────────────────────────

def test_editorial_nonzero_returncode_is_a_failure(tmp_path, monkeypatch):
    run_dir, verified, output = _workspace(tmp_path)
    calls: list = []
    _patch(monkeypatch, calls, push_rc=1)
    with pytest.raises(runner.StepFailure):
        runner._handle_publish_step("water-environment", run_dir, verified, output,
                                    _logger())
    saved = json.loads((output / "publish-result.json").read_text())
    assert saved["production_push"]["returncode"] == 1
    assert saved["status"] == "failed"
    assert saved["reason"], "a failed result must carry a reason"


# ── (c) malformed / missing publish result ──────────────────────────────

def test_missing_publish_result_is_refused(tmp_path):
    with pytest.raises(runner.PublishResultError):
        runner.read_publish_result(tmp_path)


@pytest.mark.parametrize("payload", [
    "not json at all",
    '["a", "list"]',
    '{"no": "status"}',
    '{"status": "weird"}',
    json.dumps({"status": "succeeded"}),                       # no production_push
    json.dumps({"status": "succeeded", "production_push": {"status": "refused_or_failed"}}),
    json.dumps({"status": "failed"}),                          # no reason
])
def test_malformed_or_inconsistent_publish_result_is_refused(tmp_path, payload):
    out = tmp_path / "publish-output"
    out.mkdir(parents=True)
    (out / "publish-result.json").write_text(payload)
    with pytest.raises(runner.PublishResultError):
        runner.read_publish_result(tmp_path)


# ── (d) true success ────────────────────────────────────────────────────

def test_true_success_does_not_raise_and_reads_back(tmp_path, monkeypatch):
    run_dir, verified, output = _workspace(tmp_path)
    calls: list = []
    _patch(monkeypatch, calls, dev_rc=0, push_rc=0)
    runner._handle_publish_step("water-environment", run_dir, verified, output,
                                _logger())          # must NOT raise
    saved = runner.read_publish_result(run_dir)
    assert saved["status"] == "succeeded"
    assert saved["production_push"]["status"] == "succeeded"
    assert saved["dev_publication"]["status"] == "succeeded"


def test_skipped_run_is_not_a_failure(tmp_path, monkeypatch):
    run_dir, verified, output = _workspace(tmp_path, approved=False)
    calls: list = []
    _patch(monkeypatch, calls)
    runner._handle_publish_step("water-environment", run_dir, verified, output,
                                _logger())          # must NOT raise
    saved = json.loads((output / "publish-result.json").read_text())
    assert saved["status"] == "skipped"
    assert calls == [], "a skipped run must not touch dev or production"


# ── (e) retry / alert decision and terminal workflow state ──────────────

def test_failed_step_is_retry_eligible_then_terminal(tmp_path):
    fresh = {"status": "failed", "retries": 0}
    assert runner.should_retry(fresh, 2) is True
    assert runner.should_retry({"status": "failed", "retries": 2}, 2) is False
    assert runner.should_retry({"status": "succeeded"}, 2) is False


def test_mis_recorded_success_is_never_re_run(tmp_path):
    """Why the old bug was permanent: `succeeded` is terminal for a step."""
    assert runner.step_should_run({"status": "succeeded"}) is False
    assert runner.step_should_run({"status": "failed"}) is False
    assert runner.step_should_run({"status": "running"}) is True


def test_a_failed_publish_step_fails_the_whole_workflow(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "steps").mkdir(parents=True)
    (run_dir / "steps" / "publish.state").write_text(json.dumps(
        {"status": "failed", "error": "production editorial sync refused",
         "retries": 2}))
    workflow = {"steps": [{"name": "publish", "max_retries": 2}]}
    failed, step = runner.any_step_failed(workflow, run_dir)
    assert failed is True
    assert step == "publish"


def test_a_succeeded_publish_step_does_not_fail_the_workflow(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "steps").mkdir(parents=True)
    (run_dir / "steps" / "publish.state").write_text(json.dumps(
        {"status": "succeeded"}))
    workflow = {"steps": [{"name": "publish", "max_retries": 2}]}
    assert runner.any_step_failed(workflow, run_dir) == (False, None)


def test_workflow_budget_survives_set_and_forget_process_handoffs(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    started = datetime.now(timezone.utc) - timedelta(minutes=181)
    (run_dir / "workflow.state").write_text(json.dumps({
        "status": "running",
        "started_at": started.strftime(runner.DATE_FORMAT),
    }))

    assert runner.workflow_is_over_budget(run_dir, 180) is True
    assert runner.workflow_is_over_budget(run_dir, 240) is False


def test_workflow_owner_addresses_come_only_from_environment(monkeypatch):
    monkeypatch.setattr(runner, "_load_env", lambda: None)
    monkeypatch.setenv(
        "NEWSLETTER_OWNER_EMAILS",
        " Editor@Example.com, alerts@example.com, ",
    )

    assert runner._owner_emails_from_env() == [
        "editor@example.com",
        "alerts@example.com",
    ]


# ── (f) no production call after an interlock refusal ───────────────────

def test_production_is_called_once_and_never_retried_after_refusal(tmp_path, monkeypatch):
    run_dir, verified, output = _workspace(tmp_path)
    calls: list = []
    _patch(monkeypatch, calls, push_rc=3)
    with pytest.raises(runner.StepFailure):
        runner._handle_publish_step("water-environment", run_dir, verified, output,
                                    _logger())
    sync_calls = [c for c in calls if any("editorial_sync.py" in p for p in c)]
    assert len(sync_calls) == 1, f"production must be attempted exactly once: {calls}"


def test_no_production_attempt_when_the_dev_publish_fails(tmp_path, monkeypatch):
    run_dir, verified, output = _workspace(tmp_path)
    calls: list = []
    _patch(monkeypatch, calls, dev_rc=1)
    with pytest.raises(runner.StepFailure):
        runner._handle_publish_step("water-environment", run_dir, verified, output,
                                    _logger())
    assert not any("editorial_sync.py" in p for c in calls for p in c), (
        "production must not be touched when the development article failed")
    saved = json.loads((output / "publish-result.json").read_text())
    assert saved["production_push"]["status"] == "not_attempted"
    assert saved["dev_publication"]["status"] == "failed"


# ── the handler must raise, never merely return ─────────────────────────

def test_handler_source_raises_on_failure():
    source = RUNNER_PATH.read_text()
    assert "raise StepFailure(" in source
    assert "def write_publish_result(" in source
    # the failure path must persist evidence before raising
    assert source.index("write_publish_result(output_dir, result)") < \
        source.index("raise StepFailure(")


def _logger():
    import logging
    log = logging.getLogger("publish-test")
    log.addHandler(logging.NullHandler())
    return log
