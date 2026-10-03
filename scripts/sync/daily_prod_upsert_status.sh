#!/bin/bash
# Read-only human status for the daily production upsert.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"
DATE="${1:-$(date +%Y-%m-%d)}"
SYNC_DIR="$ROOT/data/sync"
TERMINAL="$SYNC_DIR/prod-upsert-${DATE}.terminal.json"
DAILY_LOG="$SYNC_DIR/daily-prod-upsert-launchd.log"
CORE_LOG="$ROOT/data/core-civic-sync-launchd-20260924.log"
PY="$ROOT/.venv/bin/python"

case "$DATE" in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
  *) echo "usage: $0 [YYYY-MM-DD]" >&2; exit 2 ;;
esac

if [ -f "$TERMINAL" ] && "$PY" scripts/ops/daily_sync_terminal.py \
    --output "$TERMINAL" --check-existing >/dev/null 2>&1; then
  HOME_CODE="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' https://poliscopic.com/ || true)"
  TEMPE_CODE="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' https://poliscopic.com/meetings/tempe-cc/1964 || true)"
  echo "COMPLETE: production upsert for $DATE"
  echo "receipt: $TERMINAL"
  cat "$TERMINAL"
  echo "homepage_http: $HOME_CODE"
  echo "tempe_1964_http: $TEMPE_CODE"
  [ "$HOME_CODE" = "200" ] && [ "$TEMPE_CODE" = "200" ]
  exit $?
fi

echo "PENDING: no successful terminal receipt for $DATE"
echo "expected: $TERMINAL"
if launchctl print "gui/$(id -u)/com.poliscopic.core-civic-sync" >/dev/null 2>&1; then
  launchctl print "gui/$(id -u)/com.poliscopic.core-civic-sync" 2>/dev/null |
    grep -E 'state =|pid =|last exit code|runs =' | head -8
fi
for LOG in "$CORE_LOG" "$DAILY_LOG"; do
  if [ -f "$LOG" ]; then
    echo "--- $(basename "$LOG") ---"
    tail -12 "$LOG"
  fi
done
exit 1
