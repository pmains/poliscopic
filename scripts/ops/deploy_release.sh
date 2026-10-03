#!/usr/bin/env bash
# Code-only production release. INTERLOCK-GATED, and refused while production
# authorization validation is disabled.
#
#   ./scripts/ops/deploy_release.sh               # refused today (interlock)
#   ./scripts/ops/deploy_release.sh --stage-only  # refused today (interlock)
#
# This wrapper does exactly two things:
#   1. run the fail-closed production interlock BEFORE the release mechanism is
#      invoked, and
#   2. supply the production context the mechanism requires.
#
# The mechanism itself lives in scripts/ops/deploy_release_lib.sh, a sourced
# library that holds NO production host/path/unit defaults and is not
# independently executable. There is deliberately no fixture, test, or
# caller-selected target mode in this file: a caller cannot make the production
# wrapper behave like a test. Tests drive the mechanism through
# tests/deploy_release_fixture_adapter.sh, which binds it to a local sandbox.
#
# Flow: stage -> preflight -> snapshot -> activate -> restart + health -> rollback.
# It never syncs or mutates database data, and never prints secrets.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ── Production interlock (FAIL CLOSED) — before the mechanism is sourced ─────
# Refuses before staging, rsync, or any restart. --stage-only still writes to the
# production filesystem, so it is gated too.
_pi="${POLISCOPIC_PYTHON:-.venv/bin/python}"
[ -x "$_pi" ] || _pi=python3
DEPLOY_PATHS="${POLISCOPIC_DEPLOY_PATHS:-}"
AUTHORIZATION_ID="${POLISCOPIC_CODE_AUTHORIZATION_ID:-}"
[ -n "$DEPLOY_PATHS" ] || {
    echo "REFUSED: production interlock requires POLISCOPIC_DEPLOY_PATHS." >&2
    exit 3
}
[ -n "$AUTHORIZATION_ID" ] || {
    echo "REFUSED: production interlock requires POLISCOPIC_CODE_AUTHORIZATION_ID." >&2
    exit 3
}
AUTHORIZATION_SCOPE="${DEPLOY_PATHS// /,}"
"$_pi" "$REPO_ROOT/scripts/ops/production_interlock.py" check \
    --operation OP-CODE --entry-point scripts/ops/deploy_release.sh \
    --mode code --scope "$AUTHORIZATION_SCOPE" \
    --authorization-id "$AUTHORIZATION_ID" >&2 || {
    echo "REFUSED: production interlock blocked this release." >&2
    exit 3
}

LIB="$REPO_ROOT/scripts/ops/deploy_release_lib.sh"
[ -r "$LIB" ] || { echo "ERROR: missing release library: $LIB" >&2; exit 1; }
# shellcheck source=deploy_release_lib.sh
. "$LIB"

# ── Production context (the ONLY place production defaults live) ─────────────
HOST="${POLISCOPIC_DEPLOY_HOST:?Set POLISCOPIC_DEPLOY_HOST}"
APP_DIR="${POLISCOPIC_APP_DIR:-/opt/poliscopic}"
UNIT="${POLISCOPIC_UNIT:-poliscopic}"
SERVICE_USER="${POLISCOPIC_SERVICE_USER:-poliscopic}"
HEALTH_URLS=("https://poliscopic.com/" "https://poliscopic.com/meetings" "https://poliscopic.com/entities")
if [ -n "${POLISCOPIC_HEALTH_URLS:-}" ]; then
    read -r -a HEALTH_URLS <<< "$POLISCOPIC_HEALTH_URLS"
fi
HEALTH_ATTEMPTS="${POLISCOPIC_HEALTH_ATTEMPTS:-12}"
HEALTH_SLEEP="${POLISCOPIC_HEALTH_SLEEP:-5}"
# Optional allowlist: stage the whole tree (faithful preflight) but activate only
# these repo-relative paths, so unrelated work-in-progress cannot ride along.
DEPLOY_PATHS="${POLISCOPIC_DEPLOY_PATHS:-}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE_DIR="$APP_DIR/.staging/$STAMP"
REL_DIR="$APP_DIR/.releases"
ROLLBACK_TGZ="$REL_DIR/pre-$STAMP.tgz"
ROLLBACK_DIR="$APP_DIR/.staging/rollback-$STAMP"
STAGE_ONLY=0
[ "${1:-}" = "--stage-only" ] && STAGE_ONLY=1

SYSTEMCTL="${POLISCOPIC_SYSTEMCTL:-systemctl}"
HEALTH_PROBE="${POLISCOPIC_HEALTH_PROBE:-}"
PREFLIGHT_CMD="${POLISCOPIC_PREFLIGHT_CMD:-python3 '$STAGE_DIR/scripts/ops/preflight_service.py' --release-dir '$STAGE_DIR' --unit '$UNIT' --user '$SERVICE_USER'}"

# The remote side is ALWAYS the production host over ssh. There is no local or
# fixture alternative in this file.
DEST_APP="$HOST:$APP_DIR"
DEST_STAGE="$HOST:$STAGE_DIR"
remote() { ssh "$HOST" "$1"; }

# A real release is always narrow. There is no exemption, because there is no
# test mode here to exempt.
if [ -z "$DEPLOY_PATHS" ]; then
    echo "ERROR: POLISCOPIC_DEPLOY_PATHS is required for a production release" >&2
    exit 1
fi

deploy_release_run
