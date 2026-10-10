from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.ops import reuse_daily_sync_evidence as reuse


def test_direct_execution_can_import_repository_packages(tmp_path):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(Path(reuse.__file__).resolve()), "--help"],
        cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "[config]" not in result.stdout


def _receipt(root: Path, name: str, *, run_date: str = "2026-10-09") -> Path:
    preflight = root / f"{name}.preflight.json"
    preflight.write_text("{}")
    receipt = root / f"daily-production-{name}.receipt.json"
    receipt.write_text(json.dumps({
        "run_date": run_date,
        "authorization_id": "standing",
        "preflight_path": str(preflight),
    }))
    return receipt


def test_find_reusable_skips_wrong_day_and_returns_gate_allowed_pair(
        tmp_path, monkeypatch):
    _receipt(tmp_path, "newer", run_date="2026-10-08")
    wanted = _receipt(tmp_path, "wanted")
    monkeypatch.setattr(
        reuse.gate, "validate_pre_sync", lambda **_kwargs: {"status": "ALLOWED"})

    match = reuse.find_reusable(
        run_date="2026-10-09", attempt_id="attempt", authorization_id="standing",
        backup_dir=tmp_path, sync_dir=tmp_path)

    assert match == (
        (tmp_path / "wanted.preflight.json").resolve(), wanted.resolve())


def test_find_reusable_refuses_when_gate_rejects(tmp_path, monkeypatch):
    _receipt(tmp_path, "candidate")
    monkeypatch.setattr(
        reuse.gate, "validate_pre_sync", lambda **_kwargs: {"status": "REFUSED"})

    assert reuse.find_reusable(
        run_date="2026-10-09", attempt_id="attempt", authorization_id="standing",
        backup_dir=tmp_path, sync_dir=tmp_path) is None
