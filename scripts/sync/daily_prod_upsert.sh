#!/bin/bash
# Deterministic daily production upsert. Intended for launchd, not an LLM turn.
# Exit 0: already complete or completed now. Exit 75: scrape/entity pipeline is
# not ready yet (the scheduler should retry). Any other nonzero exit is a real
# failure and no success marker is written.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"

RUN_DATE="${1:-$(date +%Y-%m-%d)}"
case "$RUN_DATE" in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
  *) echo "usage: $0 [YYYY-MM-DD]" >&2; exit 2 ;;
esac

SYNC_DIR="$ROOT/data/sync"
TERMINAL="$SYNC_DIR/prod-upsert-${RUN_DATE}.terminal.json"
LOCK="$SYNC_DIR/.prod-upsert-${RUN_DATE}.lock"
AUTHORIZATION_ID="${POLISCOPIC_DAILY_SYNC_AUTHORIZATION_ID:-OP-RECON-standing-daily-sync-v4}"
PY="$ROOT/.venv/bin/python"
mkdir -p "$SYNC_DIR"

ALERT_STATUS="failure"
ALERT_REASON_FILE="$SYNC_DIR/.prod-upsert-${RUN_DATE}.alert-reason"
LOCK_ACQUIRED=0
alert_on_exit() {
  RC=$?
  if [ "$RC" -ne 0 ]; then
    REASON="daily_prod_upsert.sh exited ${RC}"
    [ -f "$ALERT_REASON_FILE" ] && REASON="$(cat "$ALERT_REASON_FILE")"
    "$PY" scripts/sync/prod_sync_alert.py \
      --status "$ALERT_STATUS" --run-date "$RUN_DATE" --reason "$REASON" || true
  fi
  rm -f "$ALERT_REASON_FILE"
  [ "$LOCK_ACQUIRED" -eq 1 ] && rmdir "$LOCK" 2>/dev/null || true
  exit "$RC"
}
trap alert_on_exit EXIT

if [ -f "$TERMINAL" ]; then
  if "$PY" scripts/ops/daily_sync_terminal.py \
      --output "$TERMINAL" --reconcile-existing; then
    "$PY" scripts/sync/prod_sync_alert.py --status healthy \
      --run-date "$RUN_DATE" --reason "existing production terminal is valid" || true
    echo "daily production upsert already complete and accounted for $RUN_DATE"
    exit 0
  fi
  echo "existing daily terminal is invalid or cannot be reconciled: $TERMINAL" >&2
  exit 1
fi

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "daily production upsert already running for $RUN_DATE"
  exit 0
fi
LOCK_ACQUIRED=1

if ! COMPLETION="$(bash scripts/sync/sync_completion_check.sh "$RUN_DATE" 2>&1)"; then
  echo "$COMPLETION"
  ALERT_STATUS="pending"
  printf '%s\n' "$COMPLETION" > "$ALERT_REASON_FILE"
  exit 75
fi
echo "$COMPLETION"

# Every production write is preceded by a fresh identity/integrity preflight and
# a snapshot-consistent, restore-verified backup.  Failed backup attempts never
# prune a known-good generation; successful attempts retain the newest five.
ATTEMPT_ID="daily-${RUN_DATE}-$(date -u +%Y%m%dT%H%M%SZ)"
PREFLIGHT="$SYNC_DIR/${ATTEMPT_ID}.preflight.json"
"$PY" scripts/ops/production_preflight.py --output "$PREFLIGHT"
"$PY" scripts/ops/daily_sync_backup.py \
  --run-date "$RUN_DATE" \
  --preflight "$PREFLIGHT" \
  --authorization-id "$AUTHORIZATION_ID"
BACKUP_RECEIPT="$(ls -t "$ROOT"/data/backups/daily-production/daily-production-*.receipt.json 2>/dev/null | head -1)"
[ -n "$BACKUP_RECEIPT" ] || { echo "no verified daily backup receipt" >&2; exit 1; }

# Upsert only. Never add --reconcile here: deletion propagation is a separate,
# explicitly reviewed operation. The standing authorization and production
# interlock are enforced inside sync_prod.py before its production connection.
BATCH_SIZE="${BATCH_SIZE:-5000}" BATCH_SLEEP_MS="${BATCH_SLEEP_MS:-100}" \
  "$PY" -u scripts/db/sync_prod.py \
    --authorization-id "$AUTHORIZATION_ID" \
    --attempt-id "$ATTEMPT_ID" \
    --run-date "$RUN_DATE" \
    --preflight "$PREFLIGHT" \
    --backup-receipt "$BACKUP_RECEIPT"

# A zero exit from the upsert is not enough: require every development meeting
# and synchronized field to be present in production before recording success.
PARITY="$SYNC_DIR/${ATTEMPT_ID}.meeting-parity.json"
"$PY" scripts/ops/verify_meeting_prod_parity.py \
  --run-date "$RUN_DATE" --lookback-days 7 --output "$PARITY"

# Public smoke checks are part of success, not best-effort diagnostics.
curl -fsS --max-time 30 -o /dev/null https://poliscopic.com/
curl -fsS --max-time 30 -o /dev/null \
  https://poliscopic.com/meetings/tempe-cc/1964

"$PY" scripts/ops/daily_sync_terminal.py \
  --run-date "$RUN_DATE" \
  --attempt-id "$ATTEMPT_ID" \
  --authorization-id "$AUTHORIZATION_ID" \
  --preflight "$PREFLIGHT" \
  --backup-receipt "$BACKUP_RECEIPT" \
  --output "$TERMINAL" \
  --public-url https://poliscopic.com/ \
  --public-url https://poliscopic.com/meetings/tempe-cc/1964
"$PY" scripts/sync/prod_sync_alert.py --status healthy \
  --run-date "$RUN_DATE" --reason "production terminal and meeting parity check succeeded" || true
echo "daily production upsert complete for $RUN_DATE: $TERMINAL"
