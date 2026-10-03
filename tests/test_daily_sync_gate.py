from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.ops import daily_sync_backup as backup
from scripts.ops import daily_sync_gate as gate
from scripts.ops import production_preflight as preflight

DAY = "2026-10-03"
NOW = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)


def test_direct_interlock_execution_exposes_daily_gate_packages(tmp_path):
    root = Path(__file__).resolve().parents[1]
    ops = root / "scripts" / "ops"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(ops)!r}); "
                "import production_interlock; import daily_sync_gate"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload))
    os.chmod(path, 0o600)
    return path


def _completion(sync_dir: Path) -> None:
    metrics = (
        "pre_total_meetings", "pre_completed", "pre_failed", "pre_pending",
        "pre_recent_24h_syncs", "pre_total_items", "post_total_meetings",
        "post_completed", "post_failed", "post_pending",
        "post_recent_24h_syncs", "post_total_items",
    )
    (sync_dir / f"{DAY}-summary.txt").write_text(
        "completion_status: success\nmetrics_status: ok\n" +
        "".join(f"{name}: 0\n" for name in metrics))
    (sync_dir / f"{DAY}.log.gz").write_bytes(b"stub")
    phases = [
        {"name": name, "status": "ok"}
        for name in ("graph_builder", "sweep_docs", "pattern_cascade",
                     "role_classifier", "resolver", "event_pipeline")
    ]
    _write(sync_dir / f"entity-run-{DAY}.json", {
        "state_file": f"entity-run-{DAY}-120000-abcd1234.json",
        "started_at": f"{DAY}T12:00:00-07:00",
        "gate": {"failed": False, "checks": []},
        "receipt_enforcement": {"ok": True, "reasons": []},
        "summary": {"phases_ran": 6, "phases_failed": 0},
        "phases": phases,
    })


def _evidence(tmp_path: Path):
    _completion(tmp_path)
    pre_body = {
        "schema": preflight.SCHEMA, "status": "VALID",
        "captured_at": "2026-10-03T17:30:00Z", "target": {"database": "poliscopic"},
    }
    pre = {**pre_body, "digest": preflight.digest(pre_body)}
    pre_path = _write(tmp_path / "preflight.json", pre)

    stem = backup.PREFIX + "20261003T173500Z"
    baseline_path = _write(tmp_path / f"{stem}.baseline.json", {})
    dump_path = tmp_path / f"{stem}.dump"
    dump_path.write_bytes(b"verified dump")
    os.chmod(dump_path, 0o600)
    receipt_body = {
        "schema": backup.SCHEMA, "status": "VALID", "run_date": DAY,
        "created_at": "2026-10-03T17:35:00Z", "authorization_id": "standing",
        "preflight_digest": pre["digest"], "baseline_path": str(baseline_path.resolve()),
        "dump_path": str(dump_path.resolve()), "dump_sha256": backup.sha256_file(dump_path),
        "comparisons": {"counts": True, "schema": True, "integrity": True},
        "restore": {"server_stopped": True},
    }
    receipt = {**receipt_body, "digest": backup.digest(receipt_body)}
    receipt_path = _write(tmp_path / f"{stem}.receipt.json", receipt)
    return pre_path, receipt_path


def test_gate_accepts_complete_lineage_and_fresh_restore_proof(tmp_path):
    preflight_path, receipt_path = _evidence(tmp_path)

    result = gate.validate_pre_sync(
        run_date=DAY, attempt_id="daily-2026-10-03", authorization_id="standing",
        preflight_path=preflight_path, backup_receipt_path=receipt_path,
        now=NOW, sync_dir=tmp_path)

    assert result["status"] == "ALLOWED"
    assert result["backup_digest"]


def test_gate_refuses_backup_for_another_authorization(tmp_path):
    preflight_path, receipt_path = _evidence(tmp_path)

    result = gate.validate_pre_sync(
        run_date=DAY, attempt_id="daily-2026-10-03", authorization_id="other",
        preflight_path=preflight_path, backup_receipt_path=receipt_path,
        now=NOW, sync_dir=tmp_path)

    assert result["status"] == "REFUSED"
    assert result["code"] == "BACKUP_INVALID"


def test_gate_refuses_stale_preflight(tmp_path):
    preflight_path, receipt_path = _evidence(tmp_path)
    payload = json.loads(preflight_path.read_text())
    payload["captured_at"] = "2026-10-01T00:00:00Z"
    body = {key: value for key, value in payload.items() if key != "digest"}
    payload["digest"] = preflight.digest(body)
    _write(preflight_path, payload)

    result = gate.validate_pre_sync(
        run_date=DAY, attempt_id="daily-2026-10-03", authorization_id="standing",
        preflight_path=preflight_path, backup_receipt_path=receipt_path,
        now=NOW, sync_dir=tmp_path)

    assert result["status"] == "REFUSED"
    assert result["code"] == "PREFLIGHT_INVALID"
