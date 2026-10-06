#!/usr/bin/env python3
"""Loud, deduplicated alerts for the daily development-to-production sync."""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / "config" / "alerts.json"
DEFAULT_STATE = REPO / "data" / "sync" / "prod-sync-alert-state.json"
DEFAULT_LOG = REPO / "data" / "sync" / "prod-sync-alerts.jsonl"
PHOENIX = ZoneInfo("America/Phoenix")


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    config = _json(path)
    section = config.get("production_sync", {})
    fallback = config.get("uptime", {})
    return {
        "deadline_local": str(section.get("deadline_local", "05:00")),
        "repeat_minutes": int(section.get("repeat_minutes", 60)),
        "email_to": list(section.get("email_to") or fallback.get("email_to") or []),
        "email_enabled": bool(section.get("email_enabled", False)),
        "local_notification": bool(section.get("local_notification", True)),
    }


def _deadline(run_date: str, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":", 1))
    return datetime.combine(date.fromisoformat(run_date), time(hour, minute), PHOENIX)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def decide(*, status: str, run_date: str, now: datetime, state: dict[str, Any],
           settings: dict[str, Any]) -> tuple[str, str]:
    """Return ``(action, reason)`` where action is alert/recovery/skip."""
    open_incident = bool(state.get("incident_open"))
    state_date = state.get("run_date")
    if status == "healthy":
        if open_incident:
            return "recovery", "a valid production terminal and parity check now exist"
        return "skip", "production is healthy and no incident is open"
    if status == "pending" and now.astimezone(PHOENIX) < _deadline(
            run_date, settings["deadline_local"]):
        return "skip", "pending run is still inside the morning grace period"
    if not open_incident or state_date != run_date:
        return "alert", "new production-sync incident"
    last_alert = _parse_timestamp(state.get("last_alert_at"))
    repeat = timedelta(minutes=max(1, int(settings["repeat_minutes"])))
    if last_alert is None or now.astimezone(timezone.utc) - last_alert.astimezone(timezone.utc) >= repeat:
        return "alert", "production-sync incident reminder is due"
    return "skip", "incident already alerted within the reminder interval"


def smtp_sender(subject: str, body: str, recipients: list[str]) -> None:
    env = dotenv_values(REPO / ".env")
    password = env.get("EMAIL_APP_PASSWORD") or env.get("EMAIL_PASSWORD")
    if not password:
        raise RuntimeError("EMAIL_APP_PASSWORD is not configured")
    if not recipients:
        raise RuntimeError("no production-sync alert recipients are configured")
    message = MIMEText(body, "plain")
    message["Subject"] = subject
    message["From"] = formataddr(("Poliscopic Operations", "contact@poliscopic.com"))
    message["To"] = ", ".join(recipients)
    context = ssl.create_default_context()
    with smtplib.SMTP("mail.privateemail.com", 587, timeout=30) as server:
        server.starttls(context=context)
        server.login("contact@poliscopic.com", str(password))
        server.send_message(message)


def local_sender(subject: str, body: str) -> None:
    summary = " ".join(body.split())[:240]
    script = (
        "on run argv\n"
        "display notification (item 1 of argv) with title (item 2 of argv) "
        "sound name \"Basso\"\n"
        "end run"
    )
    completed = subprocess.run(
        ["osascript", "-e", script, summary, subject],
        capture_output=True, text=True, timeout=15)
    if completed.returncode:
        raise RuntimeError(
            f"macOS notification failed ({completed.returncode}): {completed.stderr.strip()}")


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _append_log(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def process(*, status: str, run_date: str, reason: str, now: datetime,
            settings: dict[str, Any], state_path: Path = DEFAULT_STATE,
            log_path: Path = DEFAULT_LOG,
            sender: Callable[[str, str, list[str]], None] | None = None,
            dry_run: bool = False) -> tuple[str, str]:
    state = _json(state_path)
    action, decision_reason = decide(
        status=status, run_date=run_date, now=now, state=state, settings=settings)
    if action == "skip":
        return action, decision_reason

    local_now = now.astimezone(PHOENIX)
    if action == "recovery":
        subject = f"[RESOLVED] Poliscopic production sync recovered — {run_date}"
        body = (
            f"Production synchronization for {run_date} has recovered.\n\n"
            f"Verified at: {local_now.isoformat()}\nResult: {reason}\n")
    else:
        subject = f"[ALERT] Meetings are not reaching Poliscopic production — {run_date}"
        body = (
            f"The {run_date} development-to-production civic-data sync has not completed.\n\n"
            f"Status: {status}\nDetected at: {local_now.isoformat()}\n"
            f"Expected deadline: {settings['deadline_local']} America/Phoenix\n"
            f"Reason: {reason}\n\n"
            "There is no valid successful production terminal receipt. Treat this as a "
            "production data incident until a parity-verified sync completes.\n")

    if not dry_run:
        if sender is not None:
            sender(subject, body, settings["email_to"])
        else:
            delivered = False
            if settings.get("local_notification", True):
                local_sender(subject, body)
                delivered = True
            if settings.get("email_enabled", False):
                smtp_sender(subject, body, settings["email_to"])
                delivered = True
            if not delivered:
                raise RuntimeError("no production-sync alert delivery channel is enabled")
    timestamp = now.astimezone(timezone.utc).isoformat()
    next_state = dict(state)
    if action == "recovery":
        next_state.update({"incident_open": False, "run_date": run_date,
                           "recovered_at": timestamp, "last_reason": reason})
    else:
        next_state.update({
            "incident_open": True, "run_date": run_date,
            "first_alert_at": state.get("first_alert_at")
            if state.get("incident_open") and state.get("run_date") == run_date
            else timestamp,
            "last_alert_at": timestamp, "last_reason": reason, "status": status,
        })
    if not dry_run:
        _write_state(state_path, next_state)
        _append_log(log_path, {"action": action, "at": timestamp,
                               "run_date": run_date, "status": status,
                               "reason": reason})
    return action, decision_reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", required=True, choices=("pending", "failure", "healthy"))
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--reason-file", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        date.fromisoformat(arguments.run_date)
        reason = arguments.reason_file.read_text().strip() if arguments.reason_file else arguments.reason
        action, why = process(
            status=arguments.status, run_date=arguments.run_date,
            reason=reason or "no diagnostic detail was recorded",
            now=datetime.now(timezone.utc), settings=load_settings(arguments.config),
            state_path=arguments.state, log_path=arguments.log,
            dry_run=arguments.dry_run)
    except Exception as exc:
        print(f"prod_sync_alert: delivery/check failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"action": action, "reason": why, "run_date": arguments.run_date}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
