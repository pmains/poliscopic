#!/bin/bash
# =============================================================================
# sync_checker.sh — Check if the daily sync finished, run analysis, re-run if
#                   issues found.
#
# Called periodically (every 15-30 minutes) by a cron job. This is the
# "brain" of the fire-and-forget pipeline:
#
#   1. Check if today's sync is still running, finished, or never launched.
#   2. If running → exit quietly (not done yet).
#   3. If finished → run sync_monitor.py (analysis + auto-remediation).
#   4. Read the monitor report to determine if re-run is needed.
#   5. If issues remain → re-launch the sync (loop back to launcher).
#   6. If clean → write analysis marker and report.
#
# This script is designed to be run from an OpenClaw isolated cron session.
# It should execute in < 10 seconds unless analysis is running (~5s).
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$PROJECT_ROOT/data/sync"

# ── Python interpreter ─────────────────────────────────────────────────────
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="${PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
else
    PYTHON="${PYTHON:-python3}"
fi
export PYTHONPATH="${PYTHONPATH:-$PROJECT_ROOT/scripts}"
export POLISCOPIC_DB_TIER="${POLISCOPIC_DB_TIER:-development}"

DATE_STAMP=$(date "+%Y-%m-%d")
PID_FILE="$LOG_DIR/$DATE_STAMP.sync.pid"
LAUNCHED_FILE="$LOG_DIR/$DATE_STAMP.launched"
COMPLETE_MARKER="$LOG_DIR/$DATE_STAMP.complete"
ANALYSIS_MARKER="$LOG_DIR/$DATE_STAMP.analyzed"
SUMMARY_FILE="$LOG_DIR/$DATE_STAMP-summary.txt"
MONITOR_FILE="$LOG_DIR/$DATE_STAMP-monitor.txt"
LAUNCHER_LOG="$LOG_DIR/$DATE_STAMP.launcher.log"
MAX_RELAUNCHES_PER_DAY=3
RELAUNCH_COUNT_FILE="$LOG_DIR/$DATE_STAMP.relaunches"

# ══════════════════════════════════════════════════════════════════════════════
# Helper: check if the sync process is still alive
# ══════════════════════════════════════════════════════════════════════════════

is_sync_running() {
    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi
    EXISTING_PID=$(cat "$PID_FILE")
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        return 0
    fi
    # Stale PID — clean up
    rm -f "$PID_FILE"
    return 1
}

# ══════════════════════════════════════════════════════════════════════════════
# Helper: determine if the sync was successful
# ══════════════════════════════════════════════════════════════════════════════

get_sync_result() {
    if [ ! -f "$SUMMARY_FILE" ]; then
        echo "no_summary"
        return
    fi
    STATUS=$(grep '^completion_status:' "$SUMMARY_FILE" | awk '{print $2}')
    EXIT_CODE=$(grep '^exit_code:' "$SUMMARY_FILE" | awk '{print $2}')
    echo "${STATUS:-unknown} (exit $EXIT_CODE)"
}

# ══════════════════════════════════════════════════════════════════════════════
# Helper: determine if the sync found any new data
# ══════════════════════════════════════════════════════════════════════════════

sync_had_problems() {
    if [ ! -f "$SUMMARY_FILE" ]; then
        return 0  # no summary = problem
    fi
    STATUS=$(grep '^completion_status:' "$SUMMARY_FILE" | awk '{print $2}')
    if [ "$STATUS" = "failed" ]; then
        return 0  # problem
    fi
    ERRORS=$(grep '^error_count:' "$SUMMARY_FILE" | awk '{print $2}')
    if [ "${ERRORS:-0}" -gt 5 ]; then
        return 0  # too many errors
    fi
    return 1  # no problems
}

# ══════════════════════════════════════════════════════════════════════════════
# Helper: check if analysis says re-run is needed
# ══════════════════════════════════════════════════════════════════════════════

# ── Re-launch counter (per-day cap, defense against loops) ──────────────

relaunch_count() {
    if [ -f "$RELAUNCH_COUNT_FILE" ]; then
        cat "$RELAUNCH_COUNT_FILE"
    else
        echo 0
    fi
}

bump_relaunch_count() {
    local n
    n=$(relaunch_count)
    echo $((n + 1)) > "$RELAUNCH_COUNT_FILE"
}

# ══════════════════════════════════════════════════════════════════════════════
# Helper: check if analysis says re-run is needed
# ══════════════════════════════════════════════════════════════════════════════

analysis_needs_rerun() {
    if [ ! -f "$MONITOR_FILE" ]; then
        return 0  # no analysis = run it
    fi

    # Signal 1: the sync itself failed or had too many errors.
    if sync_had_problems; then
        return 0
    fi

    # Signal 2: fresh failures in the last 24h — a re-run may clear transient errors.
    if grep -q "Failed (last 24h): [1-9]" "$MONITOR_FILE"; then
        return 0
    fi

    # Signal 3: "Remaining Issues" bullets that are GENUINE failures. Items
    # like "unknown → left for human review" or "db_corrupt → needs VACUUM"
    # are permanent flags that no re-run can fix — they must NOT trigger a
    # re-launch loop.
    if grep -q "Remaining Issues.*human attention" "$MONITOR_FILE"; then
        local in_section=false
        while IFS= read -r line; do
            if [[ "$line" == *"Remaining Issues (require human attention):"* ]]; then
                in_section=true
                continue
            fi
            if ! $in_section; then
                continue
            fi
            # Section end — blank line or next section header
            if [[ "$line" == "" ]] || [[ "$line" =~ ^[A-Za-z] ]]; then
                break
            fi
            if [[ "$line" =~ ^[[:space:]]*\(none ]]; then
                break
            fi
            if [[ "$line" =~ ^[[:space:]]*• ]]; then
                # Skip permanent human-review flags; only real failures count.
                if [[ "$line" != *"left for human review"* \
                   && "$line" != *"left for review"* \
                   && "$line" != *"needs VACUUM"* \
                   && "$line" != *"manual recovery"* ]]; then
                    return 0  # actionable failure remains
                fi
            fi
        done < "$MONITOR_FILE"
    fi

    return 1  # no actionable issues — no re-run needed
}

# ══════════════════════════════════════════════════════════════════════════════
# Main logic
# ══════════════════════════════════════════════════════════════════════════════

echo "=== Sync Checker — $DATE_STAMP ==="
echo ""

# ── Case 1: Sync is still running ─────────────────────────────────────────
if is_sync_running; then
    SYNC_PID=$(cat "$PID_FILE")
    echo "Sync is still running (PID $SYNC_PID). Nothing to do yet."
    echo "Will check again on next cycle."
    exit 0
fi

# ── Case 2: Never launched today (no launcher file, no summary) ───────────
if [ ! -f "$LAUNCHED_FILE" ] && [ ! -f "$SUMMARY_FILE" ]; then
    echo "No sync launched today. Checking time..."
    HOUR=$(date +%H)
    if [ "$HOUR" -ge 5 ] && [ "$HOUR" -lt 12 ]; then
        echo "It's between 5 AM and noon — launching sync now."
        bash "$PROJECT_ROOT/scripts/sync/sync_launcher.sh"
        exit 0
    else
        echo "Outside sync window (5 AM - noon). Skipping."
        exit 0
    fi
fi

# ── Case 3: Launched but not yet complete (no summary) ────────────────────
if [ -f "$LAUNCHED_FILE" ] && [ ! -f "$SUMMARY_FILE" ]; then
    echo "Launched but sync hasn't written summary yet."
    if is_sync_running; then
        echo "Process still running — waiting for completion."
    else
        echo "Process ended but no summary found — sync may have crashed."
        echo "Checking launcher log for clues..."
        if [ -f "$LAUNCHER_LOG" ]; then
            tail -5 "$LAUNCHER_LOG"
        fi
        # Re-launch
        echo "Re-launching sync due to apparent crash."
        rm -f "$LAUNCHED_FILE" "$PID_FILE"
        bash "$PROJECT_ROOT/scripts/sync/sync_launcher.sh"
    fi
    exit 0
fi

# ── Case 4: Sync complete, run analysis ───────────────────────────────────
if [ -f "$SUMMARY_FILE" ] && [ ! -f "$ANALYSIS_MARKER" ]; then
    RESULT=$(get_sync_result)
    echo "Sync complete! Result: $RESULT"
    echo ""
    echo "Running analysis (sync_monitor.py)..."
    $PYTHON "$PROJECT_ROOT/scripts/sync/sync_monitor.py" 2>&1
    MONITOR_EXIT=$?
    echo ""
    if [ $MONITOR_EXIT -eq 0 ] && [ -f "$MONITOR_FILE" ]; then
        echo "Analysis complete → $MONITOR_FILE"
    else
        echo "Analysis had issues (exit $MONITOR_EXIT)"
    fi
    echo ""

    # Check if analysis says re-run (capped per day to prevent loops)
    if analysis_needs_rerun; then
        if [ "$(relaunch_count)" -lt "$MAX_RELAUNCHES_PER_DAY" ]; then
            echo "Analysis indicates remaining issues — re-launching sync ($(( $(relaunch_count) + 1 ))/$MAX_RELAUNCHES_PER_DAY today)."
            bump_relaunch_count
            rm -f "$LAUNCHED_FILE" "$PID_FILE" "$COMPLETE_MARKER"
            bash "$PROJECT_ROOT/scripts/sync/sync_launcher.sh"
        else
            echo "Re-launch limit ($MAX_RELAUNCHES_PER_DAY/day) reached — marking analyzed with issues remaining."
            date -Iseconds > "$ANALYSIS_MARKER"
            echo "  Remaining issues are NOT auto-fixable; see $MONITOR_FILE"
        fi
    else
        echo "Analysis clean — marking as analyzed."
        date -Iseconds > "$ANALYSIS_MARKER"
        echo ""
        echo "=== Syncing to poliscopic.com ==="
        bash "$PROJECT_ROOT/sync.sh" 2>&1
        SYNC_EXIT=$?
        if [ $SYNC_EXIT -eq 0 ]; then
            echo "Production sync complete."
        else
            echo "WARNING: Production sync exited with code $SYNC_EXIT"
        fi
        echo ""
        echo "Done for today. Final report:"
        echo ""
        cat "$SUMMARY_FILE"
        echo ""
        echo "=== Summary ==="
        echo "  Meetings: $(grep '^post_total_meetings:' "$SUMMARY_FILE" | awk '{print $2}')"
        echo "  Completed: $(grep '^post_completed:' "$SUMMARY_FILE" | awk '{print $2}')"
        echo "  New items: $(grep '^new_agenda_items:' "$SUMMARY_FILE" | awk '{print $2}')"
        echo "  New meets: $(grep '^new_meetings_discovered:' "$SUMMARY_FILE" | awk '{print $2}')"
    fi
    exit 0
fi

# ── Case 5: Already analyzed — nothing more to do ─────────────────────────
if [ -f "$ANALYSIS_MARKER" ]; then
    echo "Sync already analyzed today. All done."
    exit 0
fi

# ── Fallback ──────────────────────────────────────────────────────────────
echo "Unknown state. Files present:"
ls -la "$PID_FILE" "$LAUNCHED_FILE" "$SUMMARY_FILE" "$MONITOR_FILE" "$ANALYSIS_MARKER" 2>&1 || true
exit 0
