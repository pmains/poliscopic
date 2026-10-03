from __future__ import annotations

import json

import pytest

from scripts.ops import daily_sync_terminal as terminal
oa = terminal.authorization


def _authorized(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_RELEASE_DIR", str(tmp_path / "release"))
    monkeypatch.setenv("POLISCOPIC_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(oa, "PROJECT_ROOT", tmp_path)
    code = tmp_path / "sync.py"
    code.write_text("sync\n")
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    plan = oa.build_plan(
        "OP-RECON", "standing", "sync.py", ["meetings"], ["sync.py"],
        "Pete", now - timedelta(minutes=1), now + timedelta(days=1))
    oa.write_plan(plan)
    auth_path = oa.record_authorization(
        plan, verbatim_approval="approved", author="Pete", mode="standing",
        max_uses=5, use_accounting="successful-terminal")
    return plan, json.loads(auth_path.read_text())


def _terminal(plan, auth):
    body = {
        "schema": terminal.SCHEMA, "terminal": True, "status": "succeeded",
        "reconciled": True, "operation": "OP-RECON", "operation_id": "standing",
        "attempt_id": "daily-2026-10-03", "run_date": "2026-10-03",
        "mode": "upsert-only", "plan_digest": plan["digest"],
        "authorization_digest": auth["auth_digest"], "completed_at": "now",
        "gates": {}, "public_smoke_urls": [],
        "integrity_postconditions": "sync_prod exit 0",
    }
    return {**body, "digest": oa.hashlib.sha256(
        oa.canonical_json(body).encode()).hexdigest()}


def test_terminal_write_and_accounting_are_reconciled(tmp_path, monkeypatch):
    plan, auth = _authorized(tmp_path, monkeypatch)
    output = tmp_path / "terminal.json"

    result = terminal.record_terminal(output=output, terminal=_terminal(plan, auth))

    assert output.is_file()
    assert oa.count_uses("standing") == 1
    assert terminal.check_terminal_accounted(output) == result


def test_existing_terminal_can_repair_missing_use_once(tmp_path, monkeypatch):
    plan, auth = _authorized(tmp_path, monkeypatch)
    output = tmp_path / "terminal.json"
    oa._write_exclusive(output, _terminal(plan, auth))

    assert oa.count_uses("standing") == 0
    terminal.ensure_terminal_consumed(output)
    assert oa.count_uses("standing") == 1
    terminal.ensure_terminal_consumed(output)
    assert oa.count_uses("standing") == 1


def test_tampered_terminal_is_never_accounted(tmp_path, monkeypatch):
    plan, auth = _authorized(tmp_path, monkeypatch)
    output = tmp_path / "terminal.json"
    payload = _terminal(plan, auth)
    payload["status"] = "failed"
    output.write_text(json.dumps(payload))

    with pytest.raises(terminal.Refused):
        terminal.ensure_terminal_consumed(output)
    assert oa.count_uses("standing") == 0
