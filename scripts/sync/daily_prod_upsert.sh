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
mkdir -p "$SYNC_DIR"

if [ -f "$TERMINAL" ] && grep -q '"status":"success"' "$TERMINAL"; then
  echo "daily production upsert already complete for $RUN_DATE"
  exit 0
fi

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "daily production upsert already running for $RUN_DATE"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

if ! COMPLETION="$(bash scripts/sync/sync_completion_check.sh "$RUN_DATE" 2>&1)"; then
  echo "$COMPLETION"
  exit 75
fi
echo "$COMPLETION"

# Upsert only. Never add --reconcile here: deletion propagation is a separate,
# explicitly reviewed operation. The standing authorization and production
# interlock are enforced inside sync_prod.py before its production connection.
BATCH_SIZE="${BATCH_SIZE:-5000}" BATCH_SLEEP_MS="${BATCH_SLEEP_MS:-100}" \
  .venv/bin/python -u scripts/db/sync_prod.py \
    --authorization-id OP-RECON-standing-daily-sync

# Public smoke checks are part of success, not best-effort diagnostics.
curl -fsS --max-time 30 -o /dev/null https://poliscopic.com/
curl -fsS --max-time 30 -o /dev/null \
  https://poliscopic.com/meetings/tempe-cc/1964

TMP="${TERMINAL}.tmp.$$"
printf '{"schema":"daily-prod-upsert-terminal/1","date":"%s","status":"success","mode":"upsert-only","completed_at":"%s"}\n' \
  "$RUN_DATE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$TERMINAL"
echo "daily production upsert complete for $RUN_DATE: $TERMINAL"
