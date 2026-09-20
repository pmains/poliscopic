"""Offline tests for the chained immutable-receipt morning-sync gate."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kg import stage2_artifacts


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_morning_sync_readiness",
    ROOT / "scripts" / "ops" / "verify_morning_sync_readiness.py",
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _identity():
    return {"database": gate.PRODUCTION_TARGET["database"],
            "host": gate.PRODUCTION_TARGET["host"], "port": 25060,
            "server_version": "16.3", "dialect": "postgresql",
            "driver": "psycopg", "cluster_identity": "cluster-1"}


def _write(path, payload):
    stage2_artifacts.write_immutable(path, payload)
    os.chmod(path, 0o600)
    return stage2_artifacts.load_verified(path)


def _receipt(tmp_path, *, backup_over=None, **over):
    """Build the exact plan -> backup -> runbook -> terminal proof chain."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    identity = _identity()
    plan_path = tmp_path / "reviewed-plan.json"
    plan_artifact = _write(plan_path, {
        "kind": "body-code-merge", "version": 6, "tier": "production",
        "target": identity["database"], "target_identity": identity,
        "artifact_created_at": datetime.now(timezone.utc).isoformat(),
        "code_hashes": {"runtime.py": "1" * 64}, "baseline": {"counts": {"meetings": 1}},
        "merges": [], "plan_digest": "a" * 64,
    })
    plan = gate._execution_plan(plan_artifact)
    binding = gate._plan_binding(plan_path, plan_artifact, plan, identity)
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"offline backup")
    os.chmod(dump, 0o600)
    dump_sha = hashlib.sha256(dump.read_bytes()).hexdigest()
    runbook = tmp_path / "restore.md"
    runbook.write_text(f"dump {dump}\nsha {dump_sha}\nplan {plan['digest']}\n")
    os.chmod(runbook, 0o600)
    runbook_proof = {"path": str(runbook.resolve()),
                     "sha256": hashlib.sha256(runbook.read_bytes()).hexdigest(),
                     "dump_sha256": dump_sha, "plan_digest": plan["digest"],
                     "present": True}
    backup_path = tmp_path / "backup-receipt.json"
    backup_payload = {
        "kind": "body-code-merge-prod-backup-receipt", "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(), "tier": "production",
        "target": identity, "comparisons": {"dump_restored": True}, "problems": [],
        "dump_path": str(dump), "dump_sha256": dump_sha,
        "plan_artifact": binding, "plan_content_digest": gate.content_digest(plan),
        "restore_runbook": runbook_proof,
    }
    backup_payload.update(backup_over or {})
    backup = _write(backup_path, backup_payload)
    payload = {
        "kind": gate.RECEIPT_KIND, "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "success",
        "tier": "production", "target": gate.PRODUCTION_TARGET,
        "target_identity": identity, "plan_digest": plan["digest"],
        "content_digest": gate.content_digest(plan), "capabilities": {"optional_tables": {}},
        "plan_artifact": binding,
        "backup": {"receipt": str(backup_path.resolve()), "receipt_digest": backup["digest"],
                   "dump_path": str(dump), "dump_sha256": dump_sha,
                   "plan_artifact": binding, "restore_runbook": runbook_proof},
        "restore_runbook": runbook_proof, "post_counts": {"meetings": 1},
        "merges": [
            {"old": "chandler-planning-zoning-commission", "new": "chandler-pz"},
            {"old": "mesa-planning-zoning", "new": "mesa-pz"},
        ],
    }
    payload.update(over)
    path = tmp_path / "body-code-merge-prod-receipt-aaaaaaaaaaaaaaaa.json"
    _write(path, payload)
    return path, plan_path, runbook


def test_gate_blocks_when_no_terminal_receipt_exists(tmp_path):
    assert gate.main(["--receipt-dir", str(tmp_path)]) == 2


def test_gate_accepts_a_fresh_fully_bound_terminal_receipt(tmp_path):
    _receipt(tmp_path)
    assert gate.main(["--receipt-dir", str(tmp_path)]) == 0


def test_gate_rejects_a_stale_or_malformed_terminal_receipt(tmp_path):
    _receipt(tmp_path, created_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    assert gate.main(["--receipt-dir", str(tmp_path)]) == 2


def test_gate_rejects_wrong_canonical_reference_or_file_mode(tmp_path):
    path, _, _ = _receipt(tmp_path, merges=[{"old": "wrong", "new": "chandler-pz"}])
    assert gate.main(["--receipt-dir", str(tmp_path)]) == 2
    os.chmod(path, 0o644)
    assert gate.main(["--receipt-dir", str(tmp_path)]) == 2


def test_gate_rejects_tampered_plan_or_runbook(tmp_path):
    _, plan, _ = _receipt(tmp_path)
    plan.write_text("{}")
    os.chmod(plan, 0o600)
    assert gate.main(["--receipt-dir", str(tmp_path)]) == 2
    _, _, runbook = _receipt(tmp_path / "runbook")
    runbook.write_text("tampered")
    os.chmod(runbook, 0o600)
    assert gate.main(["--receipt-dir", str(tmp_path / "runbook")]) == 2


def test_gate_rejects_a_signed_port_only_identity_change(tmp_path):
    _receipt(tmp_path, target_identity={**_identity(), "port": 9999})
    assert gate.main(["--receipt-dir", str(tmp_path)]) == 2


def test_gate_rejects_a_non_0600_backup_dump(tmp_path):
    path, _, _ = _receipt(tmp_path)
    terminal = stage2_artifacts.load_verified(path)
    os.chmod(terminal["backup"]["dump_path"], 0o644)
    assert gate.main(["--receipt-dir", str(tmp_path)]) == 2


def test_gate_rejects_backup_problems_and_false_or_empty_comparisons(tmp_path):
    _receipt(tmp_path / "problems", backup_over={"problems": ["scratch mismatch"]})
    assert gate.main(["--receipt-dir", str(tmp_path / "problems")]) == 2
    _receipt(tmp_path / "false", backup_over={"comparisons": {"dump_restored": False}})
    assert gate.main(["--receipt-dir", str(tmp_path / "false")]) == 2
    _receipt(tmp_path / "empty", backup_over={"comparisons": {}})
    assert gate.main(["--receipt-dir", str(tmp_path / "empty")]) == 2
