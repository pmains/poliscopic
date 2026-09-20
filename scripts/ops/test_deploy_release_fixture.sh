#!/usr/bin/env bash
# Brief 032 release prep, task C — exercise deploy_release.sh in its fixture seam.
#
# Proves, entirely inside a local sandbox and without touching production:
#   1. successful activation + health gate                      (exit 0)
#   2. forced health failure triggers rollback + re-check       (exit 1)
#   3. rollback restores the previous tree contents
#
# The sandbox is created and destroyed by this script.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ALLOWLIST_FILE="${POLISCOPIC_ALLOWLIST_FILE:-$REPO_ROOT/data/bridge/release-manifest.paths}"
[ -f "$ALLOWLIST_FILE" ] || { echo "FATAL: allowlist not found: $ALLOWLIST_FILE"; exit 1; }
ALLOWLIST="$(cat "$ALLOWLIST_FILE")"

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/deploy-fixture-XXXXXX")"
APP="$SANDBOX/opt/poliscopic"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

pass=0; fail=0
ok()  { echo "   PASS  $1"; pass=$((pass+1)); }
bad() { echo "   FAIL  $1"; fail=$((fail+1)); }

# ── sandbox: a plausible live tree the release will be staged against ────
mkdir -p "$APP"/{scripts/db,routes,templates,static,data}
printf 'LIVE app.py v1\n'                   > "$APP/app.py"
printf 'LIVE migrations v1\n'               > "$APP/scripts/db/migrations.py"
printf 'LIVE sync_prod v1\n'                > "$APP/scripts/db/sync_prod.py"
printf 'LIVE routes v1\n'                   > "$APP/routes/__init__.py"
printf 'LIVE template v1\n'                 > "$APP/templates/base.html"
printf 'LIVE static v1\n'                   > "$APP/static/app.css"
printf 'PRECIOUS DATA\n'                    > "$APP/data/keepme.txt"

# stub systemctl: records restarts
cat > "$SANDBOX/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "[stub systemctl] $*" >> "${STUB_LOG:-/dev/null}"
exit 0
EOF
chmod +x "$SANDBOX/systemctl"

# stub chown: deploy_release.sh does `chown -R user:user`, and the sandbox user
# has no same-named group.  Ownership is a server-side concern, so it is a no-op
# in the fixture; everything else (rsync, tar, find, chmod) stays real.
mkdir -p "$SANDBOX/bin"
cat > "$SANDBOX/bin/chown" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$SANDBOX/bin/chown"

# health probe: prints the code named by the file $HEALTH_CODE_FILE
cat > "$SANDBOX/health_probe" <<'EOF'
#!/usr/bin/env bash
cat "${HEALTH_CODE_FILE:-/dev/null}" 2>/dev/null || echo 000
EOF
chmod +x "$SANDBOX/health_probe"

run_release() {   # $1 = health code to report
  local code="$1" stage_only="${2:-}"
  printf '%s' "$code" > "$SANDBOX/health_code"
  STUB_LOG="$SANDBOX/restarts.log" \
  PATH="$SANDBOX/bin:$PATH" \
  POLISCOPIC_FIXTURE_ROOT="$SANDBOX" \
  POLISCOPIC_APP_DIR="$APP" \
  POLISCOPIC_UNIT="poliscopic-test" \
  POLISCOPIC_SERVICE_USER="$(id -un)" \
  POLISCOPIC_SYSTEMCTL="$SANDBOX/systemctl" \
  POLISCOPIC_PREFLIGHT_CMD="true" \
  POLISCOPIC_HEALTH_PROBE="$SANDBOX/health_probe" \
  POLISCOPIC_HEALTH_URLS="http://fixture.invalid/" \
  POLISCOPIC_HEALTH_ATTEMPTS=2 \
  POLISCOPIC_HEALTH_SLEEP=0 \
  HEALTH_CODE_FILE="$SANDBOX/health_code" \
  POLISCOPIC_DEPLOY_PATHS="$ALLOWLIST" \
    bash "$REPO_ROOT/scripts/ops/deploy_release.sh" ${stage_only:+--stage-only}
}

echo "== fixture sandbox: $SANDBOX =="
echo "   allowlist entries: $(wc -w <<<"$ALLOWLIST" | tr -d ' ')"
echo

# ── 1. successful activation + health ───────────────────────────────────
echo "== case 1: healthy activation =="
out="$(run_release 200 2>&1)"; rc=$?
echo "$out" | sed 's/^/     /'
[ "$rc" = "0" ] && ok "exit 0 on healthy activation" || bad "expected exit 0, got $rc"
grep -q "staged" <<<"$out"     && ok "staged"     || bad "did not stage"
grep -q "activated allowlist" <<<"$out" && ok "activated the allowlist" || bad "did not activate"
grep -q "healthy" <<<"$out"    && ok "health gate passed" || bad "health gate not reported"

# allowlisted content actually reached the live tree
if [ "$(cat "$APP/scripts/db/migrations.py")" = "$(cat "$REPO_ROOT/scripts/db/migrations.py")" ]; then
  ok "allowlisted file activated (migrations.py matches repo)"
else
  bad "allowlisted file did not activate"
fi
# non-allowlisted live file must be untouched.
#
# The sentinel must be a path the manifest never contains.  It used to be
# routes/__init__.py on the assumption that routes/ is never allowlisted — but
# the 2026-09-18 body-registry release added app.py as an entry point (so that
# route changes are actually shipped), which legitimately brought routes/ into
# the allowlist and activated the old sentinel.  Templates are safe: the release
# is Python-only and carries no .html at all.
[ "$(cat "$APP/templates/base.html")" = "LIVE template v1" ] \
  && ok "non-allowlisted file left untouched (templates/base.html)" \
  || bad "non-allowlisted file was modified"
# data must be untouched
[ "$(cat "$APP/data/keepme.txt")" = "PRECIOUS DATA" ] \
  && ok "data/ preserved" || bad "data/ was modified"
grep -q "restart" "$SANDBOX/restarts.log" 2>/dev/null \
  && ok "unit restarted once" || bad "unit was not restarted"

# ── 2. forced health failure -> rollback ────────────────────────────────
echo
echo "== case 2: forced health failure =="
# poison a live file so we can prove the rollback restored it
printf 'LIVE migrations v2\n' > "$APP/scripts/db/migrations.py"
out="$(run_release 503 2>&1)"; rc=$?
echo "$out" | sed 's/^/     /'
[ "$rc" = "1" ] && ok "exit 1 on unhealthy activation" || bad "expected exit 1, got $rc"
grep -qE "rolling back|rolled back" <<<"$out" && ok "rollback invoked" || bad "rollback not invoked"
n_tgz="$(ls -1 "$APP/.releases/"*.tgz 2>/dev/null | wc -l | tr -d ' ')"
[ "${n_tgz:-0}" -ge 1 ] && ok "rollback artifact written ($n_tgz)" || bad "no rollback artifact"
[ "$(cat "$APP/data/keepme.txt")" = "PRECIOUS DATA" ] \
  && ok "data/ survived rollback" || bad "data/ lost during rollback"
# the rollback must have undone the allowlist activation
if [ "$(cat "$APP/scripts/db/migrations.py")" = "LIVE migrations v2" ]; then
  ok "rollback restored the pre-activation tree"
else
  bad "rollback did not restore the pre-activation tree"
fi

# ── 3. stage-only must not change the live tree ─────────────────────────
echo
echo "== case 3: --stage-only =="
before="$(cat "$APP/routes/__init__.py")"
out="$(run_release 200 --stage-only 2>&1)"; rc=$?
echo "$out" | sed 's/^/     /'
[ "$rc" = "0" ] && ok "stage-only exit 0" || bad "stage-only exit $rc"
grep -q "stage-only requested" <<<"$out" && ok "reported stage-only" || bad "stage-only not reported"
[ "$(cat "$APP/routes/__init__.py")" = "$before" ] \
  && ok "live tree unchanged by stage-only" || bad "stage-only modified the live tree"

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" = "0" ] || exit 1
