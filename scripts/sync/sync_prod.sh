#!/bin/bash
# sync_prod.sh — Sync development data to production as a background job
# Usage: ./scripts/sync_prod.sh [--with-code]
#   1. Pre-check: verify today's daily scrape completed successfully
#   2. By default, run data sync only. Code deployment is a separate reviewed
#      release and requires the explicit --with-code switch.
#   3. Exit immediately (caller gets back in <1 second)
# Logs: data/sync/prod-sync-YYYY-MM-DD.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# ── Production interlock (FAIL CLOSED) ───────────────────────────────────────
# Runs before the readiness check, before nohup, and before any prod connection.
.venv/bin/python scripts/ops/production_interlock.py check \
    --operation OP-RECON --entry-point scripts/sync/sync_prod.sh >&2 || {
    echo "REFUSED: production interlock blocked this sync." >&2
    exit 3
}

LOG_DIR="data/sync"
DATE_STAMP=$(date "+%Y-%m-%d")
SUMMARY_FILE="$LOG_DIR/$DATE_STAMP-summary.txt"
LOG_FILE="$LOG_DIR/prod-sync-$DATE_STAMP.log"
mkdir -p "$LOG_DIR"

# Code deployment has exactly one valid opt-in.  Reject extra arguments before
# the scrape check so a typo can never fall through to sync.sh.
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--with-code" ]; }; then
    echo "ERROR: unsupported option(s) (use --with-code for an explicit full release)"
    exit 2
fi
if [ "${1:-}" = "--with-code" ] && [ -z "${POLISCOPIC_DEPLOY_PATHS:-}" ]; then
    echo "ERROR: --with-code requires a nonempty POLISCOPIC_DEPLOY_PATHS allowlist"
    exit 2
fi

# Pre-check: daily scrape must have completed successfully
if [ ! -f "$SUMMARY_FILE" ]; then
    echo "ERROR: No scrape summary for $DATE_STAMP (expected: $SUMMARY_FILE)"
    echo "  Run sync_log.sh (daily scrape) first."
    exit 2
fi
if ! grep -q '^completion_status: success$' "$SUMMARY_FILE"; then
    echo "ERROR: Today's scrape did not complete successfully."
    echo "  See: $SUMMARY_FILE"
    exit 2
fi

# The morning readiness receipt is DIAGNOSTIC ONLY and no longer authorizes
# anything. It validates local historical artifacts (a prior merge receipt plus
# its plan and restore-runbook proofs); it does not establish current production
# identity or integrity, and an old receipt must never permit a new operation.
# The production interlock above is the gate.

# Launch the bounded sync in background and exit immediately
echo "Pre-check passed. Launching production sync in background..."
echo "  Log: $LOG_FILE"

set +euo pipefail
if [ "${1:-}" = "--with-code" ]; then
    echo "  Mode: reviewed allowlisted code release + data sync"
    nohup env POLISCOPIC_DEPLOY_PATHS="$POLISCOPIC_DEPLOY_PATHS" bash -lc "\
      cd '$PROJECT_ROOT' && \
      bash scripts/ops/deploy_release.sh && \
      set -a && source .env && set +a && \
      BATCH_SIZE=5000 BATCH_SLEEP_MS=100 .venv/bin/python scripts/db/sync_prod.py --reconcile" \
      > "$LOG_FILE" 2>&1 &
else
    echo "  Mode: data only (no rsync, no service restart)"
    nohup bash -lc "cd '$PROJECT_ROOT'; set -a; source .env; set +a; \
      BATCH_SIZE=5000 BATCH_SLEEP_MS=100 .venv/bin/python scripts/db/sync_prod.py --reconcile" \
      > "$LOG_FILE" 2>&1 &
fi
PID=$!
disown
set -euo pipefail

echo "  PID: $PID"
echo "sync_prod.sh launched (pid $PID). Check $LOG_FILE for progress."
exit 0
