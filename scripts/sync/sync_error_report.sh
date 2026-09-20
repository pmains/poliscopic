#!/bin/bash
# sync_error_report.sh — Extract errors from today's scrape log and report them.
#
# Reads the gzipped daily scrape log, finds ERROR/Failed lines, and writes
# a structured error report to data/sync/YYYY-MM-DD-errors.txt.
# Also prints the summary for cron job delivery.
#
# Usage:
#   ./scripts/sync_error_report.sh              # today
#   ./scripts/sync_error_report.sh 2026-07-21   # specific date

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$WORKSPACE"

DATE="${1:-$(date +%Y-%m-%d)}"
LOGFILE="data/sync/${DATE}.log.gz"
SUMMARY_FILE="data/sync/${DATE}-summary.txt"
ERROR_FILE="data/sync/${DATE}-errors.txt"

if [ ! -f "$LOGFILE" ]; then
    echo "No scrape log found for ${DATE}."
    exit 0
fi

# Extract error lines (gunzip -c works on macOS and Linux).
# Exclude lines ending " 0 errors" (the per-run INFO "Done: ... 0 errors"
# summaries) so clean runs don't report themselves as errors; lines with a
# non-zero error count are still caught by the case-insensitive "error" match.
ERRORS=$(gunzip -c "$LOGFILE" 2>/dev/null | grep -ni "ERROR\|FAILED\|error.*failed\|Traceback\|Failed.*meeting" | grep -vE " 0 errors$" | head -100 || true)

# Count unique failed meetings
FAILED_MEETINGS=$(echo "$ERRORS" | grep -oiE 'meeting[^ ]* [0-9]+|meeting_id=[0-9]+' | sort -u | head -20 || true)
FAILED_COUNT=$(echo "$FAILED_MEETINGS" | grep -c . || true)

# Get total meetings from summary (current format: post_total_meetings: N;
# keep old 'of N meeting' phrasing as a fallback)
TOTAL_MEETINGS=$(grep -oE '^post_total_meetings: [0-9]+' "$SUMMARY_FILE" 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)
if [ -z "$TOTAL_MEETINGS" ]; then
    TOTAL_MEETINGS=$(grep -oE '^pre_total_meetings: [0-9]+' "$SUMMARY_FILE" 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)
fi
if [ -z "$TOTAL_MEETINGS" ]; then
    TOTAL_MEETINGS=$(grep -oE 'of [0-9]+ meeting' "$SUMMARY_FILE" 2>/dev/null | grep -oE '[0-9]+' | tail -1 || true)
fi
TOTAL_MEETINGS="${TOTAL_MEETINGS:-?}"

# Count error lines without the double-print when count is 0
ERROR_LINES=$(printf '%s\n' "$ERRORS" | grep -cE '.' || true)

cat > "$ERROR_FILE" <<EOR
=== Scrape Error Report — ${DATE} ===

Total meetings: ${TOTAL_MEETINGS}
Failed meetings: ${FAILED_COUNT}
Error lines: ${ERROR_LINES:-0}

EOR

if [ -n "$ERRORS" ]; then
    echo "Failed meetings:" >> "$ERROR_FILE"
    echo "$FAILED_MEETINGS" >> "$ERROR_FILE"
    echo "" >> "$ERROR_FILE"
    echo "Error details (first 50):" >> "$ERROR_FILE"
    echo "$ERRORS" | head -50 >> "$ERROR_FILE" || true
else
    echo "No errors found." >> "$ERROR_FILE"
fi

chmod 644 "$ERROR_FILE"

# Print summary for cron delivery
echo "=== ${DATE} scrape errors ==="
echo "  Meetings: ${TOTAL_MEETINGS}"
echo "  Failed:   ${FAILED_COUNT}"
echo "  See:      ${ERROR_FILE}"
echo ""
if [ -n "$ERRORS" ]; then
    echo "${ERRORS}" | head -10 | sed 's/^/  /'
fi
