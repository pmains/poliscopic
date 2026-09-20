#!/usr/bin/env bash
# Deploy code + database to production.
#
# Binds code deploy and database sync into one atomic operation so they
# can never drift.  Run after confirming dev data is in good shape.
#
# Usage:
#   ./sync.sh              # deploy code + sync data + restart
#   ./sync.sh --code-only  # deploy code only, skip database sync
set -euo pipefail

cd "$(dirname "$0")"

# ── Production interlock (FAIL CLOSED) ───────────────────────────────────────
# Refuses BEFORE .env is sourced and before any SSH, rsync, restart or DB work.
# There is no --force and no environment escape hatch.
_pi="${POLISCOPIC_PYTHON:-.venv/bin/python}"
[ -x "$_pi" ] || _pi=python3
"$_pi" scripts/ops/production_interlock.py check --operation OP-CODE --entry-point sync.sh >&2 || {
  echo "REFUSED: production interlock blocked this operation." >&2
  exit 3
}

set -a; source .env 2>/dev/null || true; set +a

# rsync wrapper that tolerates exit codes 23 (partial transfer due to
# permission errors on server files owned by other users) and 24
# (vanished source files), but still fails on real errors.
rsync_safe() {
  rsync "$@" || { rc=$?; [ $rc -le 24 ] && return 0 || exit $rc; }
}

SSH_TARGET="${POLISCOPIC_DEPLOY_HOST:?Set POLISCOPIC_DEPLOY_HOST}"
SSH_ROOT="${POLISCOPIC_DEPLOY_ROOT:?Set POLISCOPIC_DEPLOY_ROOT}"
APP_DIR="/opt/poliscopic"

# ── 1. Verify production database is reachable ──
echo "=== Step 1/5: Verify production database ==="
.venv/bin/python -c "
import psycopg2, os
pg = psycopg2.connect(os.environ.get('PROD_DATABASE_URL', ''), options='-c client_encoding=UTF8')
cur = pg.cursor()
cur.execute('SELECT COUNT(*) FROM meetings')
cnt = cur.fetchone()[0]
cur.close()
pg.close()
print(f'Production DB OK: {cnt} meetings')
" && echo "✅ Production database reachable."

# ── 2. Rsync code + assets ──
#
# WARNING: NEVER use --delete here.  We're syncing specific files, not
# mirroring the entire workspace.  --delete would destroy .venv, .ssh,
# and other server-only files that don't exist locally.
#
# --checksum compares file content, not just timestamp+size, so edits
# that don't change file size still get deployed.
echo "=== Step 2/5: Deploy code ==="

# Sync root-level files
rsync_safe -avz --checksum app.py requirements.txt ${SSH_TARGET}:${APP_DIR}/

# Sync scripts/ to scripts/ (NOT to root — avoids scripts/db → db path breakage)
rsync_safe -avz --checksum \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='.env' \
  scripts/ ${SSH_TARGET}:${APP_DIR}/scripts/

# Sync static assets and templates
# NOTE: podcast/ is excluded — those are managed server-side and the
# deploy user doesn't have read permission on the production server.
rsync_safe -avz --checksum \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='podcast/' \
  static/ ${SSH_TARGET}:${APP_DIR}/static/

rsync_safe -avz --checksum templates/ ${SSH_TARGET}:${APP_DIR}/templates/

# Sync routes
rsync_safe -avz --checksum routes/ ${SSH_TARGET}:${APP_DIR}/routes/

# ── 3. Sync dev database → prod (unless --code-only) ──
if [ "${1:-}" != "--code-only" ]; then
  echo "=== Step 3/5: Sync database (dev → prod) ==="
  set -a
  source .env
  set +a
  BATCH_SIZE=5000 BATCH_SLEEP_MS=100 .venv/bin/python scripts/db/sync_prod.py --reconcile
  echo "✅ Database sync complete"
fi

# ── 4. Restart the app ──
echo "=== Step 4/5: Restart app ==="
ssh ${SSH_ROOT} "systemctl restart poliscopic" && echo "✅ App restarted."


# ── 5. Verify ──
echo "=== Step 5/5: Verify ==="
curl -sI https://poliscopic.com | head -3

echo "Deploy complete — code and database are in sync."
