#!/usr/bin/env python3
"""Completion contract for the daily scrape — TWO levels, both required.

Regression for 2026-09-23. That day's scrape died at the database pre-check
(Tailscale stopped) while the cron job still recorded "ok", and its post-scrape
entity gate failed too. An earlier checker returned COMPLETE for a day whose
entity gate had failed, because it only inspected the scrape level. That was a
contract defect.

  scrape   : dated summary + full log exist, say success/partial, no live pipeline
  pipeline : the post-scrape entity run for the SAME DATE passed its gate

COMPLETE requires BOTH; anything else refuses and names the level that failed.

The launcher is never executed here (running it would start a real scrape).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "sync" / "sync_completion_check.sh"
VERDICT = ROOT / "scripts" / "sync" / "entity_gate_verdict.py"
LAUNCHER = ROOT / "scripts" / "sync" / "sync_launcher.sh"
ERROR_REPORT = ROOT / "scripts" / "sync" / "sync_error_report.sh"

DAY = "2026-09-23"


def _run_checker(sync_dir: Path, day: str = DAY):
    env = dict(os.environ)
    env["SYNC_DIR"] = str(sync_dir)
    return subprocess.run(["bash", str(CHECKER), day],
                          capture_output=True, text=True, env=env, timeout=60)


def _write_scrape(sync_dir: Path, day: str = DAY, *, summary=True, log_gz=True,
                  status="success"):
    sync_dir.mkdir(parents=True, exist_ok=True)
    if summary:
        body = "" if status is None else f"completion_status: {status}\nexit_code: 0\n"
        (sync_dir / f"{day}-summary.txt").write_text(body)
    if log_gz:
        (sync_dir / f"{day}.log.gz").write_bytes(b"\x1f\x8b\x08\x00stub")


def _entity_state(day: str = DAY, **over):
    state = {
        "state_file": f"entity-run-{day}-062341-0c2adaf2.json",
        "started_at": f"{day}T06:23:41.925140-07:00",
        "finished_at": f"{day}T06:36:32.384709-07:00",
        "gate": {"failed": False, "checks": [{"check": "entities", "ok": True,
                                              "detail": "delta +4"}]},
        "receipt_enforcement": {"ok": True, "reasons": []},
        "summary": {"phases_ran": 6, "phases_failed": 0},
        "phases": [
            {"name": name, "status": "ok"}
            for name in (
                "graph_builder", "sweep_docs", "pattern_cascade",
                "role_classifier", "resolver", "event_pipeline",
            )
        ],
    }
    state.update(over)
    return state


def _write_entity(sync_dir: Path, day: str = DAY, payload=None, raw=None):
    path = sync_dir / f"entity-run-{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw)
    else:
        path.write_text(json.dumps(payload if payload is not None else _entity_state(day)))


def _complete_day(sync_dir: Path, day: str = DAY):
    _write_scrape(sync_dir, day)
    _write_entity(sync_dir, day)


# ── both levels present ──────────────────────────────────────────────────


def test_complete_when_scrape_and_entity_gate_both_ok(tmp_path):
    _complete_day(tmp_path)
    result = _run_checker(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COMPLETE" in result.stdout
    assert "entity_gate=ok" in result.stdout


# ── level 1: scrape ──────────────────────────────────────────────────────


def test_incomplete_without_summary(tmp_path):
    """Exactly the 2026-09-23 case: the run died before writing a summary."""
    _write_scrape(tmp_path, summary=False)
    _write_entity(tmp_path)
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "no summary" in result.stdout


def test_incomplete_without_full_log(tmp_path):
    _write_scrape(tmp_path, log_gz=False)
    _write_entity(tmp_path)
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "no full log" in result.stdout


def test_incomplete_while_pipeline_still_running(tmp_path):
    _complete_day(tmp_path)
    (tmp_path / f"{DAY}.sync.pid").write_text(str(os.getpid()))
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "still running" in result.stdout


def test_stale_pid_does_not_block_completion(tmp_path):
    _complete_day(tmp_path)
    (tmp_path / f"{DAY}.sync.pid").write_text("999999")
    result = _run_checker(tmp_path)
    assert result.returncode == 0, result.stdout


def test_malformed_summary_status_refuses(tmp_path):
    _write_scrape(tmp_path, status=None)   # no completion_status line
    _write_entity(tmp_path)
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "completion_status" in result.stdout


def test_failed_scrape_status_refuses(tmp_path):
    _write_scrape(tmp_path, status="failed")
    _write_entity(tmp_path)
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "completion_status=failed" in result.stdout


def test_usage_error_on_bad_date(tmp_path):
    result = _run_checker(tmp_path, day="not-a-date")
    assert result.returncode == 2


def test_absent_day_is_incomplete_not_an_error(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    result = _run_checker(tmp_path)
    assert result.returncode == 1


# ── level 2: post-scrape entity gate ─────────────────────────────────────


def test_missing_entity_state_refuses(tmp_path):
    """A good scrape with no entity evidence is NOT a complete day."""
    _write_scrape(tmp_path)
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "no entity run state" in result.stdout
    assert "scrape ok" in result.stdout          # distinction preserved


def test_failed_entity_gate_refuses(tmp_path):
    _write_scrape(tmp_path)
    state = _entity_state()
    state["gate"] = {"failed": True, "checks": [
        {"check": "unresolved_relationship_provenance", "ok": False,
         "detail": "1 (requires zero; before 1, delta +0)"}]}
    _write_entity(tmp_path, payload=state)
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "entity gate failed" in result.stdout
    assert "unresolved_relationship_provenance" in result.stdout
    assert "scrape ok" in result.stdout


def test_entity_receipt_enforcement_failure_refuses(tmp_path):
    _write_scrape(tmp_path)
    _write_entity(tmp_path, payload=_entity_state(
        receipt_enforcement={"ok": False, "reasons": ["phase receipts refused: ['event_pipeline']"]}))
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "receipt enforcement" in result.stdout


def test_entity_phase_failure_refuses(tmp_path):
    _write_scrape(tmp_path)
    _write_entity(tmp_path, payload=_entity_state(
        summary={"phases_ran": 6, "phases_failed": 1},
        phases=[{"name": "event_pipeline", "status": "failed"}]))
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "failed phase" in result.stdout
    assert "scrape ok" in result.stdout


def test_partial_diagnostic_run_cannot_certify_full_pipeline(tmp_path):
    """A newer --phase receipt must not overwrite six-phase completion proof."""
    _write_scrape(tmp_path)
    _write_entity(tmp_path, payload=_entity_state(
        summary={"phases_ran": 1, "phases_failed": 0},
        phases=[{"name": "pattern_cascade", "status": "ok"}],
    ))
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "not the full six-phase pipeline" in result.stdout


def test_malformed_entity_state_refuses(tmp_path):
    _write_scrape(tmp_path)
    _write_entity(tmp_path, raw="{not json")
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "not valid JSON" in result.stdout


def test_empty_entity_state_refuses(tmp_path):
    _write_scrape(tmp_path)
    _write_entity(tmp_path, raw="")
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "empty" in result.stdout


def test_entity_state_from_a_different_date_refuses(tmp_path):
    """Lineage: a stale state file from another day must not certify today."""
    _write_scrape(tmp_path)
    _write_entity(tmp_path, payload=_entity_state(day="2026-09-21"))
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "not this date's run" in result.stdout


def test_entity_state_without_gate_block_refuses(tmp_path):
    _write_scrape(tmp_path)
    _write_entity(tmp_path, payload=_entity_state(gate=None))
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "no gate block" in result.stdout


def test_gate_not_explicitly_false_refuses(tmp_path):
    """`gate.failed` absent is NOT success — it must be explicitly False."""
    _write_scrape(tmp_path)
    _write_entity(tmp_path, payload=_entity_state(gate={"checks": []}))
    result = _run_checker(tmp_path)
    assert result.returncode == 1


# ── the verdict helper in isolation ──────────────────────────────────────


def test_verdict_helper_usage_error(tmp_path):
    result = subprocess.run([os.sys.executable, str(VERDICT)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2


def test_verdict_helper_missing_file(tmp_path):
    result = subprocess.run([os.sys.executable, str(VERDICT),
                             str(tmp_path / "nope.json"), DAY],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "unreadable" in result.stdout


# ── contract pins: why the success signal was misleading ─────────────────


def test_launcher_exits_immediately_without_waiting(tmp_path):
    """CONTRACT PIN — sync_launcher.sh is fire-and-forget, so its exit 0 only
    confirms a LAUNCH, never a completion.

    Asserted from source rather than by running it: executing the launcher would
    start a real scrape. If this ever becomes a waiting launcher, the cron wiring
    must be revisited at the same time.
    """
    src = LAUNCHER.read_text()
    assert "nohup bash" in src
    assert "exit 0" in src
    assert "COMPLETE_MARKER" in src
    assert 'rm -f "$LAUNCHED_FILE" "$COMPLETE_MARKER" "$ANALYSIS_MARKER"' in src
    assert '> "$COMPLETE_MARKER"' not in src
    assert 'touch "$COMPLETE_MARKER"' not in src


def test_missing_log_detection_exists_but_does_not_fail():
    """CONTRACT PIN — the 03:15 backstop DETECTS a missing log yet exits 0.

    That is why 2026-09-23 produced a readable "No scrape log found" report with
    no failure signal attached.
    """
    src = ERROR_REPORT.read_text()
    assert "No scrape log found" in src
    assert "exit 0" in src
