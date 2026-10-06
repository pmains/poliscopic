from datetime import datetime, timezone
from pathlib import Path

from scripts.sync import prod_sync_alert as alert


SETTINGS = {
    "deadline_local": "05:00",
    "repeat_minutes": 60,
    "email_to": ["ops@example.com"],
    "email_enabled": False,
    "local_notification": True,
}


def test_pending_is_quiet_before_deadline(tmp_path: Path):
    sent = []
    action, _ = alert.process(
        status="pending", run_date="2026-10-06", reason="scrape still running",
        now=datetime(2026, 10, 6, 11, 59, tzinfo=timezone.utc),
        settings=SETTINGS, state_path=tmp_path / "state.json",
        log_path=tmp_path / "alerts.jsonl", sender=lambda *args: sent.append(args))
    assert action == "skip"
    assert sent == []


def test_pending_alerts_at_deadline_and_repeats_hourly(tmp_path: Path):
    sent = []
    state, log = tmp_path / "state.json", tmp_path / "alerts.jsonl"
    def sender(*args):
        sent.append(args)
    first = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert alert.process(status="pending", run_date="2026-10-06", reason="no terminal",
                         now=first, settings=SETTINGS, state_path=state,
                         log_path=log, sender=sender)[0] == "alert"
    assert alert.process(status="pending", run_date="2026-10-06", reason="still no terminal",
                         now=first.replace(minute=30), settings=SETTINGS,
                         state_path=state, log_path=log, sender=sender)[0] == "skip"
    assert alert.process(status="pending", run_date="2026-10-06", reason="still no terminal",
                         now=first.replace(hour=13), settings=SETTINGS,
                         state_path=state, log_path=log, sender=sender)[0] == "alert"
    assert len(sent) == 2


def test_hard_failure_is_immediate_and_recovery_is_announced(tmp_path: Path):
    sent = []
    state, log = tmp_path / "state.json", tmp_path / "alerts.jsonl"
    def sender(*args):
        sent.append(args)
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    assert alert.process(status="failure", run_date="2026-10-06", reason="parity failed",
                         now=now, settings=SETTINGS, state_path=state,
                         log_path=log, sender=sender)[0] == "alert"
    assert alert.process(status="healthy", run_date="2026-10-06", reason="parity passed",
                         now=now.replace(hour=10), settings=SETTINGS, state_path=state,
                         log_path=log, sender=sender)[0] == "recovery"
    assert sent[0][0].startswith("[ALERT]")
    assert sent[1][0].startswith("[RESOLVED]")


def test_failed_delivery_does_not_suppress_retry(tmp_path: Path):
    state = tmp_path / "state.json"

    def fail(*_args):
        raise RuntimeError("smtp unavailable")

    try:
        alert.process(status="failure", run_date="2026-10-06", reason="sync failed",
                      now=datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc),
                      settings=SETTINGS, state_path=state,
                      log_path=tmp_path / "alerts.jsonl", sender=fail)
    except RuntimeError:
        pass
    assert not state.exists()
