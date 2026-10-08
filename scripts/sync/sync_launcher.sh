#!/bin/bash
# =============================================================================
# sync_launcher.sh — Fire-and-forget launcher for the daily sync pipeline
#
# Called by cron at 5 AM. Does NOT wait for the sync to finish — it launches
# the sync script in the background and exits immediately, avoiding cron
# timeout issues with long-running isolated agent sessions.
#
# Design:
#   1. Check if a sync is already running (PID file)
#   2. If not, launch sync_log.sh with nohup in the background
#   3. Write a PID + launcher state file for the checker to find
#   4. Exit immediately
#
# The checker (sync_checker.sh) polls for completion and runs analysis.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$PROJECT_ROOT/data/sync"

mkdir -p "$LOG_DIR"

DATE_STAMP=$(date "+%Y-%m-%d")
PID_FILE="$LOG_DIR/$DATE_STAMP.sync.pid"
LAUNCHED_FILE="$LOG_DIR/$DATE_STAMP.launched"
COMPLETE_MARKER="$LOG_DIR/$DATE_STAMP.complete"
ANALYSIS_MARKER="$LOG_DIR/$DATE_STAMP.analyzed"

# ── Step 1: Guard — is a sync already running or done? ─────────────────────
if [ -f "$COMPLETE_MARKER" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync for $DATE_STAMP already completed. Exiting."
    exit 0
fi

# The historical launcher expected a separate checker to create .complete, but
# that marker is not written by sync_log.sh.  Use the authoritative artifact
# checker as the primary idempotency gate so a scheduler retry cannot launch a
# second scrape after today's real summary/log already prove completion.
if [ -x "$SCRIPT_DIR/sync_completion_check.sh" ] && \
   "$SCRIPT_DIR/sync_completion_check.sh" "$DATE_STAMP" >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync for $DATE_STAMP already verified complete. Exiting."
    exit 0
fi

if [ -f "$PID_FILE" ]; then
    EXISTING_PID=$(cat "$PID_FILE")
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync already running (PID $EXISTING_PID). Exiting."
        exit 0
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stale PID file found (PID $EXISTING_PID not running). Cleaning up."
        rm -f "$PID_FILE"
    fi
fi

# ── Step 2: Clean up any stale state files ─────────────────────────────────
rm -f "$LAUNCHED_FILE" "$COMPLETE_MARKER" "$ANALYSIS_MARKER"

# ── Step 3: Launch sync in background ──────────────────────────────────────
# We run sync_log.sh which is itself a wrapper around run_pipeline.py + doc_check
LOG_OUTPUT="$LOG_DIR/$DATE_STAMP.launcher.log"

nohup bash "$PROJECT_ROOT/scripts/sync/sync_log.sh" > "$LOG_OUTPUT" 2>&1 &
SYNC_PID=$!

echo "$SYNC_PID" > "$PID_FILE"
echo "launched_at=$(date -Iseconds)" > "$LAUNCHED_FILE"
echo "pid=$SYNC_PID" >> "$LAUNCHED_FILE"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync launched (PID $SYNC_PID). "
echo "  PID file: $PID_FILE"
echo "  Launched: $LAUNCHED_FILE"
echo "  Log:      $LOG_OUTPUT"
echo ""
echo "The checker cron will pick up results when the sync completes."
exit 0
