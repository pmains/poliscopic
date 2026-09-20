#!/usr/bin/env bash
# TEST-ONLY ADAPTER for the release mechanism. NOT an operational command.
#
# Binds scripts/ops/deploy_release_lib.sh to a throwaway LOCAL sandbox so the
# staging/activation/health/rollback mechanism can still be exercised now that the
# production wrapper refuses unconditionally.
#
# Guarantees enforced here (and asserted by tests):
#   * every writable path (app dir, staging, releases, rollback dir/artifact,
#     release source, systemctl stub, health probe) stays BENEATH the sandbox;
#   * the "remote" side is a local exec, never ssh;
#   * rsync targets are local absolute paths, never a host:path route;
#   * no external URL, no production path, no real systemctl;
#   * the sandbox must be explicitly provided, so there is no default target.
#
# This file is intentionally NOT named deploy_release*.sh in scripts/ops and is
# excluded from the release allowlist policy (tests/ is excluded from releases).
set -uo pipefail

SANDBOX="${POLISCOPIC_SANDBOX:-}"
[ -n "$SANDBOX" ] || { echo "ADAPTER: POLISCOPIC_SANDBOX is required" >&2; exit 2; }

_canon() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }

SANDBOX_C="$(_canon "$SANDBOX")"
[ -d "$SANDBOX_C" ] || { echo "ADAPTER: sandbox does not exist: $SANDBOX_C" >&2; exit 2; }

# ── context supplied by the caller (tests) ─────────────────────────────────
REPO_ROOT="${ADAPTER_REPO_ROOT:-}"
APP_DIR="${ADAPTER_APP_DIR:-}"
[ -n "$REPO_ROOT" ] || { echo "ADAPTER: ADAPTER_REPO_ROOT is required" >&2; exit 2; }
[ -n "$APP_DIR" ] || { echo "ADAPTER: ADAPTER_APP_DIR is required" >&2; exit 2; }

# ── sandbox containment: every writable path must be beneath SANDBOX ───────
_assert_under() {
    local label="$1" path="$2" resolved
    case "$path" in
        *:*) echo "ADAPTER: refusing host:path route for $label ($path)" >&2; exit 2 ;;
    esac
    resolved="$(_canon "$path")"
    case "$resolved" in
        "$SANDBOX_C"|"$SANDBOX_C"/*) : ;;
        *) echo "ADAPTER: $label escapes the sandbox: $resolved" >&2; exit 2 ;;
    esac
}

APP_DIR="$(_canon "$APP_DIR")"
REPO_ROOT="$(_canon "$REPO_ROOT")"
_assert_under "app dir" "$APP_DIR"
_assert_under "release source" "$REPO_ROOT"

SYSTEMCTL="${ADAPTER_SYSTEMCTL:-}"
HEALTH_PROBE="${ADAPTER_HEALTH_PROBE:-}"
[ -n "$SYSTEMCTL" ] || { echo "ADAPTER: ADAPTER_SYSTEMCTL (stub) is required" >&2; exit 2; }
[ -n "$HEALTH_PROBE" ] || { echo "ADAPTER: ADAPTER_HEALTH_PROBE (stub) is required" >&2; exit 2; }
_assert_under "systemctl stub" "$SYSTEMCTL"
_assert_under "health probe" "$HEALTH_PROBE"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE_DIR="$APP_DIR/.staging/$STAMP"
REL_DIR="$APP_DIR/.releases"
ROLLBACK_TGZ="$REL_DIR/pre-$STAMP.tgz"
ROLLBACK_DIR="$APP_DIR/.staging/rollback-$STAMP"
_assert_under "stage dir" "$STAGE_DIR"
_assert_under "release dir" "$REL_DIR"
_assert_under "rollback artifact" "$ROLLBACK_TGZ"
_assert_under "rollback dir" "$ROLLBACK_DIR"

# ── mechanism library ──────────────────────────────────────────────────────
LIB="$REPO_ROOT/scripts/ops/deploy_release_lib.sh"
[ -r "$LIB" ] || LIB="$(_canon "$(dirname "${BASH_SOURCE[0]}")/../scripts/ops/deploy_release_lib.sh")"
[ -r "$LIB" ] || { echo "ADAPTER: cannot locate deploy_release_lib.sh" >&2; exit 2; }
# shellcheck source=../scripts/ops/deploy_release_lib.sh
. "$LIB"

# ── local-only bindings ────────────────────────────────────────────────────
# HOST is a local sentinel, never a routable target; `remote` runs locally.
HOST="$SANDBOX_C"
DEST_APP="$APP_DIR"
DEST_STAGE="$STAGE_DIR"
remote() { bash -c "$1"; }

UNIT="${ADAPTER_UNIT:-fixture.service}"
SERVICE_USER="${ADAPTER_SERVICE_USER:-$(id -un 2>/dev/null || echo nobody)}"
HEALTH_URLS=("https://example.invalid/")
if [ -n "${ADAPTER_HEALTH_URLS:-}" ]; then
    read -r -a HEALTH_URLS <<< "$ADAPTER_HEALTH_URLS"
fi
HEALTH_ATTEMPTS="${ADAPTER_HEALTH_ATTEMPTS:-2}"
HEALTH_SLEEP="${ADAPTER_HEALTH_SLEEP:-0}"
DEPLOY_PATHS="${ADAPTER_DEPLOY_PATHS:-}"
PREFLIGHT_CMD="${ADAPTER_PREFLIGHT_CMD:-true}"
STAGE_ONLY=0
[ "${1:-}" = "--stage-only" ] && STAGE_ONLY=1

deploy_release_run
