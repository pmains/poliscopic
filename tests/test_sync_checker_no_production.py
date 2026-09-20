#!/usr/bin/env python3
"""Batch 1 regression: the sync checker must never mutate production.

`sync_checker.sh` used to invoke the root `sync.sh` in its steady state — the
"analysis clean" branch — which combined a full-tree code deployment with
production reconciliation. That call is removed.

This test proves the removal two independent ways:

  A. STATIC  — no production entry point is referenced anywhere in the checker.
  B. DYNAMIC — every checker state branch is actually EXECUTED in a sandbox, with
     canary executables planted at every production entry-point path (and on the
     PATH, for ssh/scp/rsync/curl/wget). If any branch reached production, the
     canary would fire.

The dynamic half is only meaningful if it can fail, so a NEGATIVE CONTROL runs the
same harness against the immediate pre-fix file (fixture, byte-identical to
git HEAD~ at the time of the fix) and asserts the canary DOES fire.

No production access, no database access, no network: the sandbox is a temp
directory and every "external" command is a local marker-writing stub.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
CHECKER = _ROOT / "scripts" / "sync" / "sync_checker.sh"
PRE_FIX_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sync_checker_prefix_batch1.sh"

# Production entry points and transport/credential surfaces the checker must not touch.
PRODUCTION_TOKENS = (
    r"\bsync\.sh\b",
    r"sync_prod\.sh",
    r"sync_prod\.py",
    r"deploy_release",
    r"deploy_code",
    r"\bssh\b",
    r"\bscp\b",
    r"\brsync\b",
    r"poliscopic\.com",
    r"PROD_DATABASE_URL",
)
# Planted both as files in the sandbox tree and as PATH shims.
CANARY_TREE_PATHS = (
    "sync.sh",
    "scripts/sync/sync_prod.sh",
    "scripts/db/sync_prod.py",
    "scripts/ops/deploy_release.sh",
    "scripts/ops/deploy_code.sh",
)
CANARY_PATH_SHIMS = ("ssh", "scp", "rsync", "curl", "wget")


# ── sandbox ──────────────────────────────────────────────────────────────


def _canary_script(log: Path, label: str) -> str:
    return (
        "#!/bin/bash\n"
        f'echo "{label} $*" >> "{log}"\n'
        "exit 0\n"
    )


def _build_sandbox(tmp: Path, checker_src: Path) -> Path:
    """Lay out a self-contained PROJECT_ROOT containing `checker_src`."""
    (tmp / "scripts" / "sync").mkdir(parents=True, exist_ok=True)
    (tmp / "scripts" / "db").mkdir(parents=True, exist_ok=True)
    (tmp / "scripts" / "ops").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "sync").mkdir(parents=True, exist_ok=True)
    (tmp / ".venv" / "bin").mkdir(parents=True, exist_ok=True)

    sandbox_checker = tmp / "scripts" / "sync" / "sync_checker.sh"
    shutil.copy2(checker_src, sandbox_checker)

    canary_log = tmp / "canary-fired.txt"

    # Canaries at every production entry-point path.
    for rel in CANARY_TREE_PATHS:
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_canary_script(canary_log, f"TREE:{rel}"))
        p.chmod(0o755)

    # PATH shims for transport/network tools.
    shim = tmp / "shim"
    shim.mkdir()
    for tool in CANARY_PATH_SHIMS:
        p = shim / tool
        p.write_text(_canary_script(canary_log, f"PATH:{tool}"))
        p.chmod(0o755)

    # Benign development launcher (legitimate checker behaviour, NOT a canary).
    launcher = tmp / "scripts" / "sync" / "sync_launcher.sh"
    launcher.write_text(
        "#!/bin/bash\n"
        f'echo "launcher-invoked" >> "{tmp / "launcher-invoked.txt"}"\n'
        "exit 0\n"
    )
    launcher.chmod(0o755)

    # Stand-in for .venv/bin/python: writes the analysis report the checker reads.
    py_stub = tmp / ".venv" / "bin" / "python"
    py_stub.write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    *sync_monitor.py)\n'
        f'      printf "%s\\n" "Failed (last 24h): 0" "Analysis OK" > "{tmp}/data/sync/$(date \'+%Y-%m-%d\')-monitor.txt"\n'
        "      exit 0 ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    py_stub.chmod(0o755)
    return sandbox_checker


def _run_checker(sandbox: Path, tmp: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{tmp / 'shim'}:{env['PATH']}"
    env["POLISCOPIC_DB_TIER"] = "test"
    env.pop("POLISCOPIC_PROD_DATABASE_URL", None)
    return subprocess.run(
        ["bash", str(sandbox)],
        capture_output=True, text=True, env=env, cwd=str(tmp), timeout=120,
    )


def _canary_fired(tmp: Path) -> str:
    log = tmp / "canary-fired.txt"
    return log.read_text() if log.exists() else ""


def _today(tmp: Path) -> str:
    logdir = tmp / "data" / "sync"
    logdir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["date", "+%Y-%m-%d"], capture_output=True,
                          text=True).stdout.strip()


def _summary(tmp: Path) -> None:
    """A clean development summary: no problems, no failures."""
    d = _today(tmp)
    (tmp / "data" / "sync" / f"{d}-summary.txt").write_text(
        "completion_status: ok\n"
        "exit_code: 0\n"
        "error_count: 0\n"
        "post_total_meetings: 10\n"
        "post_completed: 5\n"
        "new_agenda_items: 3\n"
        "new_meetings_discovered: 1\n"
    )


# ── branch states ────────────────────────────────────────────────────────


def _state_running(tmp: Path) -> None:
    _today(tmp)
    (tmp / "data" / "sync" / f"{_today(tmp)}.sync.pid").write_text(str(os.getpid()))


def _state_never_launched(tmp: Path) -> None:
    _today(tmp)


def _state_launched_no_summary(tmp: Path) -> None:
    _today(tmp)
    (tmp / "data" / "sync" / f"{_today(tmp)}.launched").write_text("")


def _state_analysis_clean(tmp: Path) -> None:
    """The steady state where the production call used to live."""
    _today(tmp)
    _summary(tmp)


def _state_already_analyzed(tmp: Path) -> None:
    _today(tmp)
    _summary(tmp)
    (tmp / "data" / "sync" / f"{_today(tmp)}.analyzed").write_text("")


def _state_monitor_only(tmp: Path) -> None:
    """A stray monitor file with none of the expected markers.

    Note the expectation below: Case 2 (`no LAUNCHED and no SUMMARY`) catches this
    BEFORE the tail fallback is reached. The fallback is defensive dead code — see
    `test_tail_fallback_is_unreachable_by_case_ordering`.
    """
    d = _today(tmp)
    (tmp / "data" / "sync" / f"{d}-monitor.txt").write_text("orphan\n")


BRANCHES = [
    ("running", _state_running, "Sync is still running"),
    ("never-launched", _state_never_launched, "No sync launched today"),
    ("launched-no-summary", _state_launched_no_summary,
     "Launched but sync hasn't written summary yet"),
    ("analysis-clean", _state_analysis_clean, "Sync complete! Result:"),
    ("already-analyzed", _state_already_analyzed, "Sync already analyzed today"),
    ("monitor-only", _state_monitor_only, "No sync launched today"),
]


# ── A. static ────────────────────────────────────────────────────────────


def _production_tokens_in(path: Path) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        for token in PRODUCTION_TOKENS:
            if re.search(token, line):
                hits.append((token, i))
    return hits


def test_checker_references_no_production_entry_point():
    hits = _production_tokens_in(CHECKER)
    assert hits == [], f"production entry point referenced in {CHECKER}: {hits}"


def test_checker_does_not_call_root_sync(monkeypatch):
    source = CHECKER.read_text()
    assert 'bash "$PROJECT_ROOT/sync.sh"' not in source
    assert "Syncing to poliscopic.com" not in source


# ── B. dynamic: every branch executes, no canary fires ───────────────────


@pytest.mark.parametrize("name,setup,marker", BRANCHES, ids=[b[0] for b in BRANCHES])
def test_branch_executes_without_touching_production(tmp_path, name, setup, marker):
    sandbox = _build_sandbox(tmp_path, CHECKER)
    setup(tmp_path)

    result = _run_checker(sandbox, tmp_path)

    assert marker in result.stdout, (
        f"branch {name!r} did not execute as expected.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    fired = _canary_fired(tmp_path)
    assert fired == "", (
        f"branch {name!r} invoked a production entry point:\n{fired}"
    )


def test_analysis_clean_branch_reports_production_paused(tmp_path):
    """The fixed steady state must say production is separately gated."""
    sandbox = _build_sandbox(tmp_path, CHECKER)
    _state_analysis_clean(tmp_path)
    result = _run_checker(sandbox, tmp_path)

    assert "Production synchronization: PAUSED" in result.stdout
    assert _canary_fired(tmp_path) == ""
    assert result.returncode == 0


# ── negative control: the harness must catch the removed call ────────────


def test_negative_control_prefix_file_fires_canary(tmp_path):
    """The pre-fix checker DID invoke sync.sh; the harness must detect that.

    Without this, the dynamic test above could pass vacuously.
    """
    assert PRE_FIX_FIXTURE.exists(), f"missing pre-fix fixture: {PRE_FIX_FIXTURE}"
    sandbox = _build_sandbox(tmp_path, PRE_FIX_FIXTURE)
    _state_analysis_clean(tmp_path)

    result = _run_checker(sandbox, tmp_path)

    fired = _canary_fired(tmp_path)
    assert fired != "", (
        "the pre-fix checker did not fire the canary — the dynamic harness is "
        f"vacuous.\nstdout:\n{result.stdout}"
    )
    assert "sync.sh" in fired, f"unexpected canary payload: {fired}"


def test_negative_control_static_check_flags_prefix_file():
    """The static scanner must flag the pre-fix file it is meant to catch."""
    assert PRE_FIX_FIXTURE.exists(), f"missing pre-fix fixture: {PRE_FIX_FIXTURE}"
    hits = _production_tokens_in(PRE_FIX_FIXTURE)
    assert hits, "static scanner failed to flag the pre-fix file"
    assert any(tok == r"\bsync\.sh\b" for tok, _ in hits), (
        f"scanner did not report the root sync.sh call: {hits}"
    )


def test_fixture_matches_recorded_prefix_provenance():
    """The fixture must be a real pre-fix artifact, not a hand-written stub."""
    text = PRE_FIX_FIXTURE.read_text()
    assert 'bash "$PROJECT_ROOT/sync.sh" 2>&1' in text
    assert 'echo "=== Syncing to poliscopic.com ==="' in text
    # and it must NOT contain the fix
    assert "Production synchronization: PAUSED" not in text


def test_tail_fallback_is_unreachable_by_case_ordering():
    """Exhaustively prove the tail fallback cannot be selected by any file state.

    Case order (after the independent `is_sync_running` short-circuit):
      2) not LAUNCHED and not SUMMARY
      3) LAUNCHED and not SUMMARY
      4) SUMMARY and not ANALYSIS
      5) ANALYSIS

    Enumerating all 8 (LAUNCHED, SUMMARY, ANALYSIS) combinations shows every one is
    claimed by a case, so the fallback tail is defensive dead code. This is asserted
    as a model rather than observed, because no input can reach it.
    """

    def reached(launched: bool, summary: bool, analyzed: bool) -> str:
        if not launched and not summary:
            return "case2"
        if launched and not summary:
            return "case3"
        if summary and not analyzed:
            return "case4"
        if analyzed:
            return "case5"
        return "fallback"

    outcomes = {
        (L, S, A): reached(L, S, A)
        for L in (False, True) for S in (False, True) for A in (False, True)
    }
    assert "fallback" not in outcomes.values(), (
        f"the tail fallback IS reachable, so it must be exercised: {outcomes}"
    )
    assert set(outcomes.values()) == {"case2", "case3", "case4", "case5"}


def test_fallback_branch_still_contains_no_production_call():
    """Even the unreachable tail must be clean — it is never a place to hide a call."""
    text = CHECKER.read_text()
    tail = text.split("# ── Fallback", 1)
    assert len(tail) == 2, "fallback marker not found"
    for token in PRODUCTION_TOKENS:
        assert not re.search(token, tail[1]), (
            f"production token {token!r} present in the fallback tail"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
