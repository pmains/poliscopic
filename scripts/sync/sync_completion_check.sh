#!/bin/bash
# =============================================================================
# sync_completion_check.sh — is a given day's scrape ACTUALLY complete?
#
# TWO LEVELS, DELIBERATELY DISTINGUISHED
#   scrape   : the dated summary + full log exist, say success/partial, and no
#              pipeline process survives
#   pipeline : the post-scrape entity run for the SAME DATE passed its gate
#
#   COMPLETE (exit 0) requires BOTH. Anything else is INCOMPLETE (exit 1) whose
#   message names which level failed, so "the scrape worked" is never reported as
#   "the day is done".
#
# WHY THIS EXISTS
#   sync_launcher.sh is fire-and-forget BY DESIGN: it nohups the pipeline, writes
#   a PID file, and exits 0 immediately. A cron job that runs the launcher
#   therefore records success even when the detached child dies seconds later.
#
#   On 2026-09-23 the pipeline died at the database pre-check (Tailscale stopped)
#   and the job still recorded "ok" — no summary, no log, no marker, no failure
#   signal. That day's entity run then failed its gate too (gate.failed = true on
#   unresolved_relationship_provenance plus a refused event_pipeline receipt;
#   receipt_enforcement.ok = false). An earlier version of THIS checker returned
#   COMPLETE for that day because it only looked at the scrape level — a contract
#   defect. See tests/test_sync_completion_contract.py for the pinned cases.
#
# NOT WIRED INTO THE SCHEDULER
#   Deliberately. Changing what the scheduler treats as success is a scheduler
#   change and needs separate authorization. This script only answers the
#   question; it touches no job, marker, or production state. Read-only.
#
# Usage:
#   ./scripts/sync/sync_completion_check.sh              # today
#   ./scripts/sync/sync_completion_check.sh 2026-09-23   # a specific day
#   SYNC_DIR=/tmp/x ./scripts/sync/sync_completion_check.sh 2026-09-23
#
# Exit codes: 0 complete · 1 incomplete · 2 usage
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/../.." && pwd)"
SYNC_DIR="${SYNC_DIR:-$WORKSPACE/data/sync}"
GATE_VERDICT="$SCRIPT_DIR/entity_gate_verdict.py"

DATE="${1:-$(date +%Y-%m-%d)}"
case "$DATE" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) echo "usage: $0 [YYYY-MM-DD]" >&2; exit 2 ;;
esac

SUMMARY="$SYNC_DIR/${DATE}-summary.txt"
LOG_GZ="$SYNC_DIR/${DATE}.log.gz"
PID_FILE="$SYNC_DIR/${DATE}.sync.pid"
ENTITY_STATE="$SYNC_DIR/entity-run-${DATE}.json"

PY="$WORKSPACE/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

fail() { echo "INCOMPLETE: $*"; exit 1; }

# ── Level 1: scrape ──────────────────────────────────────────────────────
[ -f "$SUMMARY" ] || fail "scrape: no summary for ${DATE} (${SUMMARY})"
[ -f "$LOG_GZ" ]  || fail "scrape: no full log for ${DATE} (${LOG_GZ})"

if [ -f "$PID_FILE" ]; then
    PID="$(tr -d ' \n' < "$PID_FILE")"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        fail "scrape: pipeline still running (pid ${PID})"
    fi
fi

SCRAPE_STATUS="$(grep -oE '^completion_status: .*' "$SUMMARY" 2>/dev/null | head -1 | sed 's/^completion_status: //')"
[ -n "$SCRAPE_STATUS" ] || fail "scrape: summary has no completion_status line (malformed)"

case "$SCRAPE_STATUS" in
    success|partial) ;;
    *) fail "scrape: summary completion_status=${SCRAPE_STATUS}" ;;
esac

# ── Level 2: post-scrape entity gate (same date / run lineage) ───────────
[ -f "$ENTITY_STATE" ] || \
    fail "full pipeline: no entity run state for ${DATE} (scrape ok; ${ENTITY_STATE})"

[ -f "$GATE_VERDICT" ] || fail "full pipeline: gate verdict helper missing (${GATE_VERDICT})"

ENTITY_VERDICT="$("$PY" "$GATE_VERDICT" "$ENTITY_STATE" "$DATE" 2>&1)"
ENTITY_RC=$?

if [ "$ENTITY_RC" -ne 0 ]; then
    fail "full pipeline: ${ENTITY_VERDICT} (scrape ok)"
fi
[ "$ENTITY_VERDICT" = "ok" ] || fail "full pipeline: unexpected verdict ${ENTITY_VERDICT} (scrape ok)"

echo "COMPLETE: ${DATE} (scrape=${SCRAPE_STATUS}, entity_gate=ok)"
exit 0
