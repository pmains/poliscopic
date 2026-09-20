#!/usr/bin/env python3
"""
Poliscopic.com uptime health check with email alerts.

Usage:
    python3 scripts/health_check.py

Exits 0 when the site returns 200.  Exits 1 on any failure (HTTP error,
timeout, DNS failure, SSL error).  Writes a log line to data/health.log
on every run so we can chart uptime later.

After 3 consecutive failures (ALERT_THRESHOLD) it sends an email alert
to the addresses configured in config/alerts.json.

Intended to be run as a cron job every 5 minutes.
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z",
    force=True,
)
log = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────

HEALTH_LOG = _PROJECT_ROOT / "data" / "health.log"
ALERT_CONFIG = _PROJECT_ROOT / "config" / "alerts.json"
FAILURE_COUNTER = Path("/tmp/poliscopic_health_failures")

# ── Defaults (overridden by config/alerts.json) ─────────────────────────────

TARGET_URL = "https://poliscopic.com"
TIMEOUT_SECONDS = 10
ALERT_THRESHOLD = 3
ALERT_EMAILS: list[str] = []


def _load_config() -> dict[str, Any]:
    """Load alert configuration from config/alerts.json."""
    if ALERT_CONFIG.exists():
        try:
            cfg = json.loads(ALERT_CONFIG.read_text())
            return cfg.get("uptime", {})
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Cannot load %s: %s", ALERT_CONFIG, exc)
    return {}


def _configure() -> None:
    """Apply config-file values over the defaults."""
    global TARGET_URL, TIMEOUT_SECONDS, ALERT_THRESHOLD, ALERT_EMAILS
    cfg = _load_config()
    TARGET_URL = cfg.get("target_url", TARGET_URL)
    TIMEOUT_SECONDS = cfg.get("check_interval_minutes", 5)  # not a great name but kept for compat
    ALERT_THRESHOLD = cfg.get("alert_after_failures", ALERT_THRESHOLD)
    ALERT_EMAILS = cfg.get("email_to", ALERT_EMAILS)
    log.debug("Alerts will go to: %s", ALERT_EMAILS or "(none)")


def _read_failure_count() -> int:
    try:
        return int(FAILURE_COUNTER.read_text().strip())
    except (ValueError, OSError):
        return 0


def _write_failure_count(count: int) -> None:
    FAILURE_COUNTER.write_text(str(count))


def _clear_failure_count() -> None:
    _write_failure_count(0)


def _append_log(entry: dict) -> None:
    HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _send_email(subject: str, body: str, recipients: list[str]) -> None:
    """Send an email via the system ``mail`` command."""
    if not recipients:
        return
    for addr in recipients:
        try:
            proc = subprocess.run(
                ["mail", "-s", subject, addr],
                input=body,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                log.info("Alert emailed to %s", addr)
            else:
                log.warning("mail to %s returned %d: %s", addr, proc.returncode, proc.stderr.strip())
        except FileNotFoundError:
            log.error("mail command not found — cannot send alert")
        except subprocess.TimeoutExpired:
            log.error("mail to %s timed out", addr)


def main() -> int:
    _configure()
    timestamp = datetime.now(timezone.utc).isoformat()
    start = time.time()
    healthy = False
    status_code: Any = "error"
    error_msg: str | None = None

    try:
        req = urllib.request.Request(
            TARGET_URL,
            headers={"User-Agent": "Poliscopic-HealthCheck/1.0"},
            method="HEAD",
        )
        resp = urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS)
        status_code = resp.status

        # Follow one redirect level
        if 300 <= status_code < 400:
            redirect_url = resp.headers.get("Location", "")
            log.info("Redirected to %s", redirect_url)
            if redirect_url:
                req2 = urllib.request.Request(
                    redirect_url,
                    headers={"User-Agent": "Poliscopic-HealthCheck/1.0"},
                )
                resp2 = urllib.request.urlopen(req2, timeout=TIMEOUT_SECONDS)
                status_code = resp2.status

        healthy = status_code == 200

    except urllib.error.HTTPError as e:
        status_code = e.code
        error_msg = str(e)
    except urllib.error.URLError as e:
        error_msg = str(e.reason)
    except Exception as e:
        error_msg = str(e)

    elapsed_ms = int((time.time() - start) * 1000)
    entry = {
        "ts": timestamp,
        "url": TARGET_URL,
        "status": status_code,
        "ok": healthy,
        "elapsed_ms": elapsed_ms,
    }
    if error_msg:
        entry["error"] = error_msg
    _append_log(entry)

    if healthy:
        _clear_failure_count()
        log.info("OK %s (%dms)", status_code, elapsed_ms)
        return 0

    # ── Unhealthy path ──
    failures = _read_failure_count() + 1
    _write_failure_count(failures)
    log.error("UNHEALTHY status=%s (%dms) consecutive_failures=%d", status_code, elapsed_ms, failures)

    if failures >= ALERT_THRESHOLD:
        downtime_min = failures * 5
        subject = f"[ALERT] poliscopic.com DOWN ({downtime_min} min)"
        body = (
            f"poliscopic.com has been unreachable for {downtime_min} minutes.\n\n"
            f"  Last status: {status_code}\n"
            f"  Error:       {error_msg or 'n/a'}\n"
            f"  Response:    {elapsed_ms}ms\n"
            f"  Checked at:  {timestamp}\n"
            f"  Failures:    {failures}\n\n"
            "Investigate the production service using the configured deployment host.\n"
        )
        _send_email(subject, body, ALERT_EMAILS)

    return 1


if __name__ == "__main__":
    sys.exit(main())
