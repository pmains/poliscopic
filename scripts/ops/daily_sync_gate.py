#!/usr/bin/env python3
"""Validate the immutable evidence required before a daily production upsert."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.ops import daily_sync_backup as backup
from scripts.ops import production_preflight as preflight

REPO = Path(__file__).resolve().parents[2]
COMPLETION_CHECKER = REPO / "scripts" / "sync" / "sync_completion_check.sh"
MAX_EVIDENCE_AGE_SECONDS = 6 * 3600


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fresh(value: object, *, now: datetime) -> bool:
    created = _parse_time(value)
    if created is None:
        return False
    age = (now - created).total_seconds()
    return -300 <= age <= MAX_EVIDENCE_AGE_SECONDS


def _load(path: Path, *, label: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            return None, f"{label} mode is not 0600"
        value = json.loads(path.read_text())
    except Exception as exc:
        return None, f"{label} is unreadable: {type(exc).__name__}"
    if not isinstance(value, dict):
        return None, f"{label} is not a JSON object"
    return value, None


def validate_pre_sync(*, run_date: str, attempt_id: str, authorization_id: str,
                      preflight_path: Path, backup_receipt_path: Path,
                      now: datetime | None = None,
                      sync_dir: Path | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    environment = dict(os.environ)
    if sync_dir is not None:
        environment["SYNC_DIR"] = str(sync_dir)
    completion = subprocess.run(
        ["bash", str(COMPLETION_CHECKER), run_date], capture_output=True,
        text=True, env=environment, timeout=120)
    if completion.returncode:
        return {"status": "REFUSED", "code": "COMPLETION_INCOMPLETE",
                "reason": (completion.stdout or completion.stderr).strip()}

    production, problem = _load(preflight_path, label="preflight")
    if problem:
        return {"status": "REFUSED", "code": "PREFLIGHT_INVALID", "reason": problem}
    production_body = {key: value for key, value in production.items()
                       if key != "digest"}
    if (production.get("schema") != preflight.SCHEMA or
            production.get("status") != "VALID" or
            production.get("digest") != preflight.digest(production_body) or
            not _fresh(production.get("captured_at"), now=now)):
        return {"status": "REFUSED", "code": "PREFLIGHT_INVALID",
                "reason": "preflight is stale, malformed, or digest-invalid"}

    receipt, problem = _load(backup_receipt_path, label="backup receipt")
    if problem:
        return {"status": "REFUSED", "code": "BACKUP_INVALID", "reason": problem}
    receipt_body = {key: value for key, value in receipt.items() if key != "digest"}
    if (receipt.get("schema") != backup.SCHEMA or
            receipt.get("status") != "VALID" or
            receipt.get("digest") != backup.digest(receipt_body) or
            receipt.get("run_date") != run_date or
            receipt.get("authorization_id") != authorization_id or
            receipt.get("preflight_digest") != production.get("digest") or
            not _fresh(receipt.get("created_at"), now=now)):
        return {"status": "REFUSED", "code": "BACKUP_INVALID",
                "reason": "backup receipt is stale, malformed, or not bound to this run"}
    paths = backup.verified_generation(backup_receipt_path,
                                       backup_dir=backup_receipt_path.parent)
    if paths is None:
        return {"status": "REFUSED", "code": "BACKUP_INVALID",
                "reason": "backup generation or dump digest cannot be verified"}
    comparisons = receipt.get("comparisons")
    if (not isinstance(comparisons, Mapping) or not comparisons or
            any(value is not True for value in comparisons.values()) or
            (receipt.get("restore") or {}).get("server_stopped") is not True):
        return {"status": "REFUSED", "code": "BACKUP_INVALID",
                "reason": "backup restore comparisons or teardown did not pass"}

    return {
        "status": "ALLOWED", "code": None, "run_date": run_date,
        "attempt_id": attempt_id, "authorization_id": authorization_id,
        "completion": completion.stdout.strip(),
        "preflight_digest": production["digest"],
        "backup_digest": receipt["digest"],
        "backup_dump_sha256": receipt["dump_sha256"],
    }
