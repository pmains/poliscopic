#!/bin/bash
# =============================================================================
# metric_parse.sh — non-executable parsing of the daily sync's DB metrics
#
# WHY THIS EXISTS
#   sync_log.sh captured the database pre/post-check output and ran
#   `eval "$PRE_CHECK"`. That output is NOT pure shell assignment: importing
#   db.config prints a human banner first —
#
#     [config] Using PostgreSQL: 100.91.173.66:5432/poliscopic_dev (tier=development)
#
#   The parentheses in "(tier=development)" make that line a bash SYNTAX ERROR,
#   so `eval` aborted before assigning anything and EVERY metric silently fell
#   back to "?" — making pre/post values and all computed deltas meaningless.
#   Verified pre-existing: 2026-09-21-summary.txt shows the same "?" values.
#
#   Parsing is now structural. Only "KEY=<digits>" is accepted; human/config
#   banners are routed to stderr; anything else is rejected loudly. Missing
#   metrics are reported by the caller and FAIL the run — never become "?".
#
# Usage from a caller:
#   source scripts/sync/metric_parse.sh
#   parse_metrics "$RAW_OUTPUT" "$OUT_FILE"
#   value="$(metric "$OUT_FILE" TOTAL_MEETINGS)"
#   value="$(metric_or "$OUT_FILE" TOTAL_MEETINGS "$FALLBACK")"
#
# Bash 3.2 compatible (macOS): no associative arrays, no mapfile.
# =============================================================================

# parse_metrics <raw-text> <out-file>
#   Writes only well-formed KEY=<digits> lines to <out-file>.
#   Banners/diagnostics go to stderr. Never executes anything.
parse_metrics() {
    _raw="$1"
    _out="$2"
    : > "$_out"
    printf '%s\n' "$_raw" | while IFS= read -r _line; do
        case "$_line" in
            '')
                ;;
            [A-Z]*=*)
                case "${_line#*=}" in
                    '' | *[!0-9]*)
                        printf '[metrics] rejected (non-numeric value): %s\n' "$_line" >&2
                        ;;
                    *)
                        printf '%s\n' "$_line" >> "$_out"
                        ;;
                esac
                ;;
            *)
                printf '[metrics] banner/diagnostic -> stderr: %s\n' "$_line" >&2
                ;;
        esac
    done
}

# metric <out-file> <KEY>   -> prints the value, or nothing if absent/malformed
metric() {
    _v="$(grep -E "^$2=[0-9]+$" "$1" 2>/dev/null | head -1 | cut -d= -f2)"
    printf '%s' "$_v"
}

# metric_or <out-file> <KEY> <fallback>  -> value, else the documented fallback
metric_or() {
    _v="$(metric "$1" "$2")"
    if [ -n "$_v" ]; then
        printf '%s' "$_v"
    else
        printf '%s' "$3"
    fi
}

# missing_metrics <out-file> <KEY...>  -> prints the missing keys, or nothing
missing_metrics() {
    _file="$1"
    shift
    _missing=""
    for _key in "$@"; do
        if [ -z "$(metric "$_file" "$_key")" ]; then
            _missing="$_missing $_key"
        fi
    done
    printf '%s' "$_missing"
}

# metric_or_missing <out-file> <KEY>  -> the value, else the literal "unavailable"
# Never substitutes a fabricated number: a missing metric must be visibly absent.
metric_or_missing() {
    _v="$(metric "$1" "$2")"
    if [ -n "$_v" ]; then
        printf '%s' "$_v"
    else
        printf 'unavailable'
    fi
}

# delta <a> <b>  -> a-b, or "unavailable" when either side is not a number
delta() {
    case "$1$2" in
        '' | *[!0-9]*)
            printf 'unavailable'
            ;;
        *)
            printf '%s' "$(( $1 - $2 ))"
            ;;
    esac
}
