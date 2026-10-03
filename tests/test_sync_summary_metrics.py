#!/usr/bin/env python3
"""The daily sync summary must never publish unusable DB metrics as "?".

Regression for the `eval` protocol in `scripts/sync/sync_log.sh`. The script used
to capture the DB pre/post-check output and run `eval "$PRE_CHECK"`. That output
is not pure assignment — importing db.config prints a human banner first:

    [config] Using PostgreSQL: 100.91.173.66:5432/poliscopic_dev (tier=development)

The parentheses in "(tier=development)" make that line a bash SYNTAX ERROR, so
`eval` aborted before assigning anything and every metric silently became "?".
Pre-existing: 2026-09-21-summary.txt shows the same "?" values.

Parsing is now structural (`scripts/sync/metric_parse.sh`): only KEY=<digits> is
accepted, banners are routed to stderr, and anything missing fails the run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "sync" / "metric_parse.sh"
SYNC_LOG = ROOT / "scripts" / "sync" / "sync_log.sh"

#: The exact banner that broke `eval`, verbatim.
BANNER = ("[config] Using PostgreSQL: 100.91.173.66:5432/poliscopic_dev "
          "(tier=development)")


def _bash(script: str):
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, cwd=str(ROOT), timeout=120)


def _parse(raw: str, out: Path):
    """Run parse_metrics on `raw` via the real helper; return the CompletedProcess."""
    return _bash(
        'set -uo pipefail\n'
        f'. "{HELPER}"\n'
        f"raw=$(cat <<'RAWEOF'\n{raw}\nRAWEOF\n)\n"
        f'parse_metrics "$raw" "{out}"\n'
    )


# ── the exact banner that caused the defect ──────────────────────────────


def test_exact_tier_banner_is_excluded_structurally(tmp_path):
    out = tmp_path / "m.txt"
    result = _parse(f"{BANNER}\nTOTAL_MEETINGS=16013\nCOMPLETED=13567", out)
    written = out.read_text()

    # the real metrics survive
    assert "TOTAL_MEETINGS=16013" in written
    assert "COMPLETED=13567" in written
    # the banner is NOT in the metrics file ...
    assert "tier=development" not in written
    assert BANNER not in written
    # ... and it is surfaced to stderr rather than silently dropped
    assert "tier=development" in result.stderr


def test_banner_parentheses_do_not_break_parsing(tmp_path):
    """The precise defect: the banner's parens made eval abort entirely."""
    out = tmp_path / "m.txt"
    _parse(f"{BANNER}\nTOTAL_MEETINGS=42", out)
    assert out.read_text().strip() == "TOTAL_MEETINGS=42"
    assert "?" not in out.read_text()


def test_banner_alone_produces_no_metrics(tmp_path):
    out = tmp_path / "m.txt"
    _parse(BANNER, out)
    assert out.read_text().strip() == ""


# ── structural parse rules ───────────────────────────────────────────────


def test_non_numeric_value_is_rejected_not_truncated(tmp_path):
    out = tmp_path / "m.txt"
    result = _parse("TOTAL_MEETINGS=abc\nCOMPLETED=7", out)
    written = out.read_text()
    assert "TOTAL_MEETINGS" not in written
    assert "COMPLETED=7" in written
    assert "rejected" in result.stderr


def test_lowercase_and_prose_lines_are_rejected(tmp_path):
    out = tmp_path / "m.txt"
    _parse("total=5\nSome diagnostic line\nCOMPLETED=3", out)
    written = out.read_text()
    assert "total=5" not in written
    assert "diagnostic" not in written
    assert "COMPLETED=3" in written


def test_blank_lines_are_ignored_quietly(tmp_path):
    out = tmp_path / "m.txt"
    result = _parse("\n\nCOMPLETED=1\n", out)
    assert out.read_text().strip() == "COMPLETED=1"
    assert result.stderr.strip() == ""


# ── value accessors ──────────────────────────────────────────────────────


def test_missing_metric_is_unavailable_never_question_mark(tmp_path):
    out = tmp_path / "m.txt"
    _parse("COMPLETED=9", out)
    result = _bash(
        'set -uo pipefail\n'
        f'. "{HELPER}"\n'
        f'echo "got=$(metric_or_missing "{out}" TOTAL_MEETINGS)"\n'
    )
    assert "got=unavailable" in result.stdout
    assert "?" not in result.stdout


def test_missing_metrics_helper_reports_gaps(tmp_path):
    out = tmp_path / "m.txt"
    _parse("COMPLETED=9\nFAILED=0", out)
    result = _bash(
        'set -uo pipefail\n'
        f'. "{HELPER}"\n'
        f'missing_metrics "{out}" TOTAL_MEETINGS COMPLETED FAILED\n'
    )
    assert "TOTAL_MEETINGS" in result.stdout
    assert "COMPLETED" not in result.stdout
    assert "FAILED" not in result.stdout


def test_delta_is_numeric_or_unavailable(tmp_path):
    result = _bash(
        'set -uo pipefail\n'
        f'. "{HELPER}"\n'
        'echo "a=$(delta 10 4)"\n'
        'echo "b=$(delta unavailable 4)"\n'
        'echo "c=$(delta 4 unavailable)"\n'
    )
    assert "a=6" in result.stdout
    assert "b=unavailable" in result.stdout
    assert "c=unavailable" in result.stdout


# ── contract pins on sync_log.sh itself ──────────────────────────────────


def test_sync_log_never_evals_captured_check_output():
    """The defect must not return: no eval of pre/post-check output."""
    src = SYNC_LOG.read_text()
    assert 'eval "$PRE_CHECK"' not in src
    assert 'eval "$POST_CHECK"' not in src
    assert "parse_metrics" in src
    assert "metric_parse.sh" in src


def test_sync_log_fails_closed_on_unusable_metrics():
    src = SYNC_LOG.read_text()
    assert 'echo "metrics_status: $METRICS_STATUS"' in src
    assert 'METRICS_STATUS="failed"' in src
    assert 'if [ "$METRICS_STATUS" != "ok" ]; then' in src
    # a defective summary must not exit 0
    assert "exit 5" in src


def test_sync_log_is_bash_syntax_valid():
    result = subprocess.run(["bash", "-n", str(SYNC_LOG)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
