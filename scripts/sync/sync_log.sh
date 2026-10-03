#!/bin/bash
# =============================================================================
# sync_log.sh — Run run_pipeline.py with structured logging and downstream checks
#
# Usage:
#   ./scripts/sync/sync_log.sh                           # default 3/14 day windows
#   ./scripts/sync/sync_log.sh --days-back 30            # wide scan
#   PYTHON=/path/to/python ./scripts/sync/sync_log.sh   # Override python binary
#
# What it does:
#   1. Runs a lightweight DB pre-check (counts meetings, recent syncs)
#   2. Runs run_pipeline.py, capturing stdout+stderr
#   3. Runs a DB post-check to show what changed
#   4. Saves full gzipped log → data/sync/YYYY-MM-DD.log.gz
#   5. Saves plain-text summary  → data/sync/YYYY-MM-DD-summary.txt
#   6. Cleans up logs older than 90 days
#   7. Exits with run_pipeline.py's exit code
#
# Environment:
#   POLISCOPIC_DB_TIER  — set to "development" (default if unset)
#   PYTHON              — python interpreter (default: from .venv or system)
#   PYTHONPATH          — defaults to <project>/scripts
# =============================================================================

set -euo pipefail

# ── Project root (where scripts/, data/, .venv/ live) ──────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

# launchd supplies a minimal PATH that omits Homebrew on Apple Silicon.  The
# document extraction cascade invokes Poppler (`pdftotext`) and Tesseract by
# name, so make their canonical install location explicit for unattended runs.
# Keep the system paths as fallbacks for Intel Macs and system-provided tools.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# ── Settings ────────────────────────────────────────────────────────────────
LOG_DIR="data/sync"
LOG_RETENTION_DAYS=90

# Ensure the log directory exists
mkdir -p "$LOG_DIR"

# Date stamp for filenames
DATE_STAMP=$(date "+%Y-%m-%d")
LOG_FILE="$LOG_DIR/$DATE_STAMP.log.gz"
SUMMARY_FILE="$LOG_DIR/$DATE_STAMP-summary.txt"

# ── Python interpreter and path ─────────────────────────────────────────────
# Prefer project .venv, fall back to PYTHON env var, then system python
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="${PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
else
    PYTHON="${PYTHON:-python3}"
fi

# PYTHONPATH must include scripts/ so 'from db import ...' works
export PYTHONPATH="${PYTHONPATH:-$PROJECT_ROOT/scripts}"

# Database — db/config.py loads .env which supplies DATABASE_URL.
# For backwards compat, set tier too (harmless when DATABASE_URL is set).
export POLISCOPIC_DB_TIER="${POLISCOPIC_DB_TIER:-development}"

# ── Timestamp helpers ──────────────────────────────────────────────────────
START_EPOCH=$(date +%s)
START_ISO=$(date -Iseconds)

log_info() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ── Non-executable metric parsing ──────────────────────────────────────────
# NEVER eval the DB pre/post-check output: it mixes a human config banner in with
# the assignments, and the banner's "(tier=development)" parentheses made `eval` a
# syntax error that blanked EVERY metric to "?" (pre-existing, not just 09-23).
# See scripts/sync/metric_parse.sh and tests/test_sync_summary_metrics.py.
if [ ! -f "$SCRIPT_DIR/metric_parse.sh" ]; then
    echo "FATAL: metric_parse.sh missing next to sync_log.sh" >&2
    exit 5
fi
# shellcheck source=/dev/null
. "$SCRIPT_DIR/metric_parse.sh"
METRICS_STATUS="ok"
PRE_METRICS="$(mktemp -t sync_pre_metrics.XXXXXX)"
POST_METRICS="$(mktemp -t sync_post_metrics.XXXXXX)"

# ── Step 1: DB pre-check ───────────────────────────────────────────────────
log_info "Running database pre-check..."

PRE_CHECK=$(
    "$PYTHON" -c "
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname('$SCRIPT_DIR'), 'scripts'))
sys.path.insert(0, '$PROJECT_ROOT/scripts')

from db import get_engine, get_session
from db.models import Meeting, AgendaItem
from sqlalchemy import func, text

engine = get_engine()
session = get_session()

# Total meetings in DB
total_meetings = session.query(func.count(Meeting.id)).scalar()

# Meetings with sync_status = 'complete'
completed = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'complete'
).scalar()

# Meetings that failed
failed = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'error'
).scalar()

# Pending meetings (never synced)
pending = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'pending'
).scalar()

# Meetings synced in the last 24 hours
from datetime import datetime, timezone, timedelta
since = datetime.now(timezone.utc) - timedelta(hours=24)
recent_syncs = session.query(func.count(Meeting.id)).filter(
    Meeting.last_synced_at >= since
).scalar()

# Total agenda items
total_items = session.query(func.count(AgendaItem.id)).scalar()

session.close()
engine.dispose()

print(f'TOTAL_MEETINGS={total_meetings}')
print(f'COMPLETED={completed}')
print(f'FAILED={failed}')
print(f'PENDING={pending}')
print(f'RECENT_SYNCS={recent_syncs}')
print(f'TOTAL_ITEMS={total_items}')
" 2>&1
)

echo "$PRE_CHECK"
parse_metrics "$PRE_CHECK" "$PRE_METRICS"
PREMISSING="$(missing_metrics "$PRE_METRICS" TOTAL_MEETINGS COMPLETED FAILED PENDING RECENT_SYNCS TOTAL_ITEMS)"
if [ -n "$PREMISSING" ]; then
    log_info "ERROR: pre-check metrics missing/malformed:$PREMISSING"
    METRICS_STATUS="failed"
fi

PRE_TOTAL="$(metric_or_missing "$PRE_METRICS" TOTAL_MEETINGS)"
PRE_COMPLETED="$(metric_or_missing "$PRE_METRICS" COMPLETED)"
PRE_FAILED="$(metric_or_missing "$PRE_METRICS" FAILED)"
PRE_PENDING="$(metric_or_missing "$PRE_METRICS" PENDING)"
PRE_RECENT="$(metric_or_missing "$PRE_METRICS" RECENT_SYNCS)"
PRE_ITEMS="$(metric_or_missing "$PRE_METRICS" TOTAL_ITEMS)"

# ── Step 2: Run run_pipeline.py ─────────────────────────────────────────────
log_info "Starting run_pipeline.py..."

# We run run_pipeline.py and capture everything to a temp file,
# then compress + save it after.  We also tee to stdout so the
# operator can see progress.
TEMP_LOG=$(mktemp -t sync_log.XXXXXX)
trap 'rm -f "$TEMP_LOG" "$PRE_METRICS" "$POST_METRICS"' EXIT

# Disable set -e for the sync run so we capture the exit code
set +e
"$PYTHON" -u "$PROJECT_ROOT/scripts/run_pipeline.py" "$@" 2>&1 | tee "$TEMP_LOG"
SYNC_EXIT=${PIPESTATUS[0]}
set -e

SYNC_END_EPOCH=$(date +%s)
SYNC_DURATION=$((SYNC_END_EPOCH - START_EPOCH))

# ── Step 3: DB post-check ──────────────────────────────────────────────────
log_info "Running database post-check..."

POST_CHECK=$(
    "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT/scripts')

from db import get_engine, get_session
from db.models import Meeting, AgendaItem
from sqlalchemy import func
from datetime import datetime, timezone, timedelta

engine = get_engine()
session = get_session()

total_meetings = session.query(func.count(Meeting.id)).scalar()
completed = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'complete'
).scalar()
failed = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'error'
).scalar()
pending = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'pending'
).scalar()

since = datetime.now(timezone.utc) - timedelta(hours=24)
recent_syncs = session.query(func.count(Meeting.id)).filter(
    Meeting.last_synced_at >= since
).scalar()

# Meetings synced in the last 24 hours that were NOT synced before
# (i.e., first-time syncs — meetings that got their first last_synced_at)
# We approximate by counting all that were synced within this run
run_start = datetime.fromtimestamp($START_EPOCH, tz=timezone.utc)
synced_in_run = session.query(func.count(Meeting.id)).filter(
    Meeting.last_synced_at >= run_start
).scalar()

# New meetings discovered (created in this run)
# We approximate by counting meetings created_at >= run_start
new_meetings = session.query(func.count(Meeting.id)).filter(
    Meeting.created_at >= run_start
).scalar()

# New agenda items
total_items = session.query(func.count(AgendaItem.id)).scalar()
new_items = session.query(func.count(AgendaItem.id)).filter(
    AgendaItem.created_at >= run_start
).scalar() if hasattr(AgendaItem, 'created_at') else 0

session.close()
engine.dispose()

print(f'TOTAL_MEETINGS={total_meetings}')
print(f'COMPLETED={completed}')
print(f'FAILED={failed}')
print(f'PENDING={pending}')
print(f'RECENT_SYNCS={recent_syncs}')
print(f'SYNCED_IN_RUN={synced_in_run}')
print(f'NEW_MEETINGS={new_meetings}')
print(f'TOTAL_ITEMS={total_items}')
print(f'NEW_ITEMS={new_items}')
" 2>&1
)

echo "$POST_CHECK"
parse_metrics "$POST_CHECK" "$POST_METRICS"
POSTMISSING="$(missing_metrics "$POST_METRICS" TOTAL_MEETINGS COMPLETED FAILED PENDING RECENT_SYNCS TOTAL_ITEMS SYNCED_IN_RUN NEW_MEETINGS NEW_ITEMS)"
if [ -n "$POSTMISSING" ]; then
    log_info "ERROR: post-check metrics missing/malformed:$POSTMISSING"
    METRICS_STATUS="failed"
fi

# Totals may legitimately be unchanged; fall back to the pre-check value rather
# than fabricating one. Everything else reads its own value or "unavailable", and
# any gap has already set METRICS_STATUS=failed (the run then fails closed).
POST_TOTAL="$(metric_or "$POST_METRICS" TOTAL_MEETINGS "$PRE_TOTAL")"
POST_COMPLETED="$(metric_or "$POST_METRICS" COMPLETED "$PRE_COMPLETED")"
POST_FAILED="$(metric_or "$POST_METRICS" FAILED "$PRE_FAILED")"
POST_PENDING="$(metric_or "$POST_METRICS" PENDING "$PRE_PENDING")"
POST_RECENT="$(metric_or "$POST_METRICS" RECENT_SYNCS "$PRE_RECENT")"
POST_SYNCED="$(metric_or_missing "$POST_METRICS" SYNCED_IN_RUN)"
POST_NEW_MEETINGS="$(metric_or_missing "$POST_METRICS" NEW_MEETINGS)"
POST_ITEMS="$(metric_or "$POST_METRICS" TOTAL_ITEMS "$PRE_ITEMS")"
POST_NEW_ITEMS="$(metric_or_missing "$POST_METRICS" NEW_ITEMS)"

# ── Determine completion status ────────────────────────────────────────────
# We consider the sync successful if we got at least some new syncs and
# no catastrophic errors from run_pipeline.py
if [ $SYNC_EXIT -eq 0 ]; then
    COMPLETION_STATUS="success"
elif [ $SYNC_EXIT -gt 0 ] && [ $POST_SYNCED -gt 0 ]; then
    COMPLETION_STATUS="partial"   # Some syncs ran but some failed
else
    COMPLETION_STATUS="failed"
fi

# Count errors from the log
ERROR_COUNT=$(grep -ci '\bERROR\b' "$TEMP_LOG" 2>/dev/null || echo 0)

# ── Step 4: Write gzipped full log ─────────────────────────────────────────
log_info "Saving full log → $LOG_FILE"
gzip -c "$TEMP_LOG" > "$LOG_FILE"

# ── Step 5: Write summary file ─────────────────────────────────────────────
log_info "Writing summary → $SUMMARY_FILE"

{
    echo "# Sync Summary — $DATE_STAMP"
    echo "# Generated: $START_ISO"
    echo ""
    echo "start_time: $START_ISO"
    echo "duration_seconds: $SYNC_DURATION"
    echo "exit_code: $SYNC_EXIT"
    echo "completion_status: $COMPLETION_STATUS"
    echo "error_count: $ERROR_COUNT"
    echo "metrics_status: $METRICS_STATUS"
    echo ""
    echo "# ── DB pre-check ──"
    echo "pre_total_meetings: $PRE_TOTAL"
    echo "pre_completed: $PRE_COMPLETED"
    echo "pre_failed: $PRE_FAILED"
    echo "pre_pending: $PRE_PENDING"
    echo "pre_recent_24h_syncs: $PRE_RECENT"
    echo "pre_total_items: $PRE_ITEMS"
    echo ""
    echo "# ── DB post-check ──"
    echo "post_total_meetings: $POST_TOTAL"
    echo "post_completed: $POST_COMPLETED"
    echo "post_failed: $POST_FAILED"
    echo "post_pending: $POST_PENDING"
    echo "post_recent_24h_syncs: $POST_RECENT"
    echo "post_total_items: $POST_ITEMS"
    echo ""
    echo "# ── Changes during this run ──"
    echo "meetings_synced_this_run: $POST_SYNCED"
    echo "new_meetings_discovered: $POST_NEW_MEETINGS"
    echo "new_agenda_items: $POST_NEW_ITEMS"
    echo ""
    echo "# ── Computed deltas ──"
    echo "delta_total_meetings: $(delta "$POST_TOTAL" "$PRE_TOTAL")"
    echo "delta_completed: $(delta "$POST_COMPLETED" "$PRE_COMPLETED")"
    echo "delta_items: $(delta "$POST_ITEMS" "$PRE_ITEMS")"

} > "$SUMMARY_FILE"

# ── Step 6: Extract text from newly scraped documents ────────────────────
# Only runs if the main sync succeeded.
if [ "$COMPLETION_STATUS" != "failed" ]; then
    log_info "Running text extraction for newly scraped documents..."
    EXTRACT_LOG="$LOG_DIR/$DATE_STAMP-extract.log"
    # Pass 1: untouched docs (never attempted).
    $PYTHON -u "$PROJECT_ROOT/scripts/ingest_docs.py" \
        --workers 5 --limit 500 2>&1 | tee "$EXTRACT_LOG"
    # Pass 2: bounded auto-retry of high-probability transient failures
    # (Pete directive 2026-09-04): recent download_failed → process_error →
    # older download_failed. Attempt-capped (--max-attempts) with a 24h
    # backoff so no single doc can loop forever. extraction_failed is
    # excluded (extraction stack unchanged → corrupt/scanned PDFs fail
    # identically).
    log_info "Running bounded retry of transient failures..."
    $PYTHON -u "$PROJECT_ROOT/scripts/ingest_docs.py" \
        --retry-priority --workers 5 --limit 200 \
        --max-attempts 3 --backoff-hours 24 2>&1 | tee -a "$EXTRACT_LOG"
    log_info "Text extraction complete. Log → $EXTRACT_LOG"
fi

# ── Step 7: Detect entities ────────────────────────────────────────────────
# Only runs if the main sync succeeded.
if [ "$COMPLETION_STATUS" != "failed" ]; then
    log_info "Running entity detection..."
    ENTITY_LOG="$LOG_DIR/$DATE_STAMP-entities.log"
    $PYTHON -u "$PROJECT_ROOT/scripts/entities/detect_entities.py" 2>&1 | tee "$ENTITY_LOG"
    ENTITY_EXIT=${PIPESTATUS[0]}
    if [ $ENTITY_EXIT -eq 0 ]; then
        log_info "Entity detection complete. Log → $ENTITY_LOG"
    else
        log_info "Entity detection had errors (exit $ENTITY_EXIT). Log → $ENTITY_LOG"
    fi
fi

# ── Step 8: Clean up old logs ──────────────────────────────────────────────
log_info "Cleaning up logs older than $LOG_RETENTION_DAYS days..."
find "$LOG_DIR" -name '*.log.gz' -type f -mtime +$LOG_RETENTION_DAYS -exec rm -v {} \;
find "$LOG_DIR" -name '*-summary.txt' -type f -mtime +$LOG_RETENTION_DAYS -exec rm -v {} \;

# ── Final output ───────────────────────────────────────────────────────────
log_info "=== Sync complete ==="
echo "  Status:     $COMPLETION_STATUS (exit code $SYNC_EXIT)"
echo "  Duration:   ${SYNC_DURATION}s"
echo "  Errors:     $ERROR_COUNT"
echo "  Meetings:   $POST_TOTAL total (Δ $((POST_TOTAL - PRE_TOTAL)))"
echo "  Completed:  $POST_COMPLETED (Δ $((POST_COMPLETED - PRE_COMPLETED)))"
echo "  Synced now: $POST_SYNCED"
echo "  New meets:  $POST_NEW_MEETINGS"
echo "  New items:  $POST_NEW_ITEMS"
echo "  Full log:   $LOG_FILE"
echo "  Summary:    $SUMMARY_FILE"

# Fail closed: a summary whose DB metrics could not be parsed is a defective
# artifact. Never exit 0 with unusable numbers, and never emit "?".
if [ "$METRICS_STATUS" != "ok" ]; then
    log_info "ERROR: summary metrics unavailable (METRICS_STATUS=$METRICS_STATUS) — failing closed"
    exit 5
fi

exit $SYNC_EXIT
