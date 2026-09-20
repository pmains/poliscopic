# deploy_release_lib.sh — SOURCED LIBRARY, NOT AN OPERATIONAL COMMAND.
#
# This file holds the release MECHANISM: staging, preflight, snapshot,
# activation, restart + health check, and rollback.
#
# It is deliberately:
#   * not independently executable (it refuses if run as a script),
#   * not an advertised operational command,
#   * free of ANY production host, path, unit, or service-user default.
#
# Every piece of context must be supplied by the caller. The only caller in the
# repository is the interlock-gated wrapper scripts/ops/deploy_release.sh. Tests
# drive it through tests/deploy_release_fixture_adapter.sh, which binds it to a
# throwaway local sandbox.
#
# There is no shebang on purpose: this is sourced, never executed.

# Refuse to act as a command if someone runs it directly.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    printf 'ERROR: %s is a sourced library, not a command.\n' "${BASH_SOURCE[0]}" >&2
    printf 'Use scripts/ops/deploy_release.sh (interlock-gated) instead.\n' >&2
    exit 1
fi

# Generic repo-shape constants only — nothing production-specific lives here.
DR_EXCLUDES=(
    --exclude '.git' --exclude '.venv' --exclude 'venv' --exclude '__pycache__'
    --exclude '*.pyc' --exclude 'data' --exclude '*.sqlite' --exclude '*.sqlite3'
    --exclude '.staging' --exclude '.releases' --exclude 'node_modules'
    --exclude '.env' --exclude '.env.*'
    --exclude '*.log' --exclude '.ipynb_checkpoints' --exclude '.server.pid'
    --exclude 'tmp.py' --exclude 'tests'
)

# Never copy ownership/group or workstation directory modes to the target; keep
# dirs traversable (755) and files readable (644).
DR_RSYNC_SAFE=(-rlptz --checksum --no-owner --no-group --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r)

dr_say() { printf '%s\n' "$*"; }
dr_die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Require that each named variable is SET (declared), even if empty. The library
# refuses to invent a value: no production defaults, ever.
dr_require_context() {
    local var missing=()
    for var in "$@"; do
        if [ -z "${!var+x}" ]; then missing+=("$var"); fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        dr_die "deploy_release_lib: missing required context: ${missing[*]} (this library has no defaults)"
    fi
}

dr_require_function() {
    local name
    for name in "$@"; do
        if [ "$(type -t "$name")" != "function" ]; then
            dr_die "deploy_release_lib: required function '$name' was not supplied by the caller"
        fi
    done
}

dr_health_ok() {
    local url code
    for url in "${HEALTH_URLS[@]}"; do
        if [ -n "${HEALTH_PROBE:-}" ]; then
            code="$("$HEALTH_PROBE" "$url" || true)"
        else
            code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$url" || true)"
        fi
        if [ "$code" != "200" ]; then
            dr_say "health: $url -> ${code:-no-response}"
            return 1
        fi
        dr_say "health: $url -> 200"
    done
    return 0
}

dr_wait_for_health() {
    local attempt
    for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
        if dr_health_ok; then return 0; fi
        sleep "$HEALTH_SLEEP"
    done
    return 1
}

# The mechanism. Order: stage -> preflight -> snapshot -> activate -> restart +
# health -> rollback on failure. Never mutates database data; never prints secrets.
deploy_release_run() {
    dr_require_context HOST APP_DIR UNIT SERVICE_USER REPO_ROOT STAMP \
        HEALTH_ATTEMPTS HEALTH_SLEEP SYSTEMCTL PREFLIGHT_CMD \
        DEST_APP DEST_STAGE STAGE_DIR REL_DIR ROLLBACK_TGZ ROLLBACK_DIR \
        STAGE_ONLY DEPLOY_PATHS
    dr_require_function remote
    if [ "${#HEALTH_URLS[@]}" -eq 0 ]; then
        dr_die "deploy_release_lib: HEALTH_URLS must contain at least one URL"
    fi

    local EXCLUDES=("${DR_EXCLUDES[@]}")
    local RSYNC_SAFE=("${DR_RSYNC_SAFE[@]}")

    dr_say "== release $STAMP -> $HOST"

    # ── 1. stage ────────────────────────────────────────────────────────────
    remote "mkdir -p '$STAGE_DIR' '$REL_DIR' '$STAGE_DIR/data'" || dr_die "cannot create staging dirs"
    if [ -n "$DEPLOY_PATHS" ]; then
        # The preflight must exercise the exact eventual release: start from the
        # current live tree and overlay only the allowlisted local files.
        remote "tar -C '$APP_DIR' -cf - \
    --exclude=./data --exclude=./.venv --exclude=./.staging --exclude=./.releases \
    --exclude='./*.log' --exclude=./.git --exclude='./__pycache__' . | \
    tar -C '$STAGE_DIR' -xf -" || dr_die "cannot seed allowlisted staging tree"
        read -r -a STAGE_PATHS <<< "$DEPLOY_PATHS"
        (cd "$REPO_ROOT" && rsync -R "${RSYNC_SAFE[@]}" "${EXCLUDES[@]}" \
            "${STAGE_PATHS[@]}" "$DEST_STAGE/") || dr_die "allowlist staging overlay failed"
    else
        rsync "${RSYNC_SAFE[@]}" "${EXCLUDES[@]}" "$REPO_ROOT/" "$DEST_STAGE/" \
            || dr_die "rsync to staging failed"
    fi
    remote "chown -R $SERVICE_USER:$SERVICE_USER '$STAGE_DIR'" || dr_die "chown staging failed"
    dr_say "-- staged"

    # ── 2. preflight (before anything live changes) ─────────────────────────
    if ! remote "$PREFLIGHT_CMD"; then
        dr_die "preflight failed; live release untouched (staging kept at $STAGE_DIR)"
    fi
    if [ "$STAGE_ONLY" = "1" ]; then
        dr_say "-- stage-only requested; live release untouched"
        exit 0
    fi

    # ── 3. snapshot the current release (rollback artifact) ─────────────────
    # tar exits 1 for benign conditions and 2 for real errors; tolerate 1.
    local snapshot_rc
    remote "tar -C '$APP_DIR' -czf '$ROLLBACK_TGZ' \
  --exclude=./data --exclude=./.venv --exclude=./.staging --exclude=./.releases \
  --exclude=./.cache --exclude=./.gunicorn --exclude=./.pytest_cache \
  --exclude='./*.log' --exclude=./.git --exclude='./__pycache__' ."
    snapshot_rc=$?
    if [ "$snapshot_rc" -gt 1 ]; then
        dr_die "could not snapshot the current release (tar exit $snapshot_rc)"
    fi
    if [ "$snapshot_rc" -eq 1 ]; then
        dr_say "-- WARN: tar reported benign warnings while snapshotting (exit 1); archive written"
    fi
    dr_say "-- rollback artifact: $ROLLBACK_TGZ"

    # ── 4. activate ─────────────────────────────────────────────────────────
    if [ -n "$DEPLOY_PATHS" ]; then
        read -r -a ACTIVATE_PATHS <<< "$DEPLOY_PATHS"
        (cd "$REPO_ROOT" && rsync -R "${RSYNC_SAFE[@]}" "${EXCLUDES[@]}" "${ACTIVATE_PATHS[@]}" "$DEST_APP/") \
            || dr_die "activation rsync failed"
        dr_say "-- activated allowlist: ${DEPLOY_PATHS}"
    else
        rsync "${RSYNC_SAFE[@]}" "${EXCLUDES[@]}" "$REPO_ROOT/" "$DEST_APP/" \
            || dr_die "activation rsync failed"
        dr_say "-- activated full tree"
    fi
    remote "chown -R $SERVICE_USER:$SERVICE_USER '$APP_DIR/scripts' '$APP_DIR/routes' '$APP_DIR/templates' '$APP_DIR/static' '$APP_DIR/app.py' 2>/dev/null; \
  find '$APP_DIR/scripts' '$APP_DIR/routes' '$APP_DIR/templates' '$APP_DIR/static' -type d -exec chmod 755 {} + 2>/dev/null; \
  find '$APP_DIR/scripts' '$APP_DIR/routes' '$APP_DIR/templates' '$APP_DIR/static' -type f -exec chmod 644 {} + 2>/dev/null; \
  find '$APP_DIR/scripts' -name '*.sh' -exec chmod 755 {} + 2>/dev/null; \
  chmod 644 '$APP_DIR/app.py' 2>/dev/null; \
  find '$APP_DIR' -maxdepth 1 -name '*.py' -exec chmod 644 {} + 2>/dev/null; true"

    # ── 5. restart once + health check ──────────────────────────────────────
    remote "$SYSTEMCTL restart '$UNIT'" || dr_say "WARN: restart command returned non-zero"

    if dr_wait_for_health; then
        dr_say "== release $STAMP healthy"
        exit 0
    fi

    # ── 6. rollback ─────────────────────────────────────────────────────────
    dr_say "!! health checks failed after activation - rolling back to $ROLLBACK_TGZ"
    remote "mkdir -p '$ROLLBACK_DIR' && \
  tar -C '$ROLLBACK_DIR' -xzf '$ROLLBACK_TGZ' && \
  rsync -rlpt --checksum --no-owner --no-group --delete \
    --exclude='.env' --exclude='.env.*' --exclude='data' \
    --exclude='.venv' --exclude='venv' --exclude='.staging' --exclude='.releases' \
    --exclude='*.log' '$ROLLBACK_DIR/' '$APP_DIR/' && \
  $SYSTEMCTL restart '$UNIT'" \
        || dr_die "rollback failed; manual intervention required (artifact: $ROLLBACK_TGZ)"
    if dr_wait_for_health; then
        dr_say "== rolled back to the previous release and healthy again"
    else
        dr_say "!! rollback did not restore health; manual intervention required"
    fi
    exit 1
}
