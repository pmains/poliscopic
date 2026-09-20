#!/usr/bin/env bash
# Deploy code to production (rsync only — no DB sync, no restart).
#
# Usage:
#   ./scripts/deploy_code.sh              # dry-run first
#   ./scripts/deploy_code.sh --execute    # actually deploy
#
# This is extracted from sync.sh's code-deploy steps for use in the
# staged migration playbook.  Run BEFORE reloading gunicorn.

set -euo pipefail

cd "$(dirname "$0")/.."

# ── Production interlock (FAIL CLOSED) ───────────────────────────────────────
# Refuses before .env is sourced and before any rsync or restart.
_pi="${POLISCOPIC_PYTHON:-.venv/bin/python}"
[ -x "$_pi" ] || _pi=python3
"$_pi" scripts/ops/production_interlock.py check \
    --operation OP-CODE --entry-point scripts/ops/deploy_code.sh >&2 || {
    echo "REFUSED: production interlock blocked this deployment." >&2
    exit 3
}

set -a; source .env 2>/dev/null || true; set +a

rsync_safe() {
  rsync "$@" || { rc=$?; [ $rc -le 24 ] && return 0 || exit $rc; }
}

SSH_TARGET="${POLISCOPIC_DEPLOY_HOST:?Set POLISCOPIC_DEPLOY_HOST}"
APP_DIR="/opt/poliscopic"
MODE="${1:-}"  # --execute or empty (dry-run)

DRYRUN=""
if [ "$MODE" != "--execute" ]; then
  DRYRUN="--dry-run"
  echo "=== DRY RUN (add --execute to actually deploy) ==="
fi

# ── Verify production DB is reachable ──
echo "=== Verify production DB ==="
.venv/bin/python -c "
import psycopg2, os
pg = psycopg2.connect(os.environ.get('PROD_DATABASE_URL', ''), options='-c client_encoding=UTF8')
cur = pg.cursor()
cur.execute('SELECT COUNT(*) FROM meetings')
cnt = cur.fetchone()[0]
cur.close()
pg.close()
print(f'Production DB OK: {cnt} meetings')
" && echo "✅"

echo "=== Deploy code ${DRYRUN:+(dry-run)} ==="

# Sync root-level files (but NEVER --delete)
rsync_safe -avz --checksum $DRYRUN \
  app.py requirements.txt ${SSH_TARGET}:${APP_DIR}/

# Sync scripts/ — keep directory structure
rsync_safe -avz --checksum $DRYRUN \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='.env' \
  scripts/ ${SSH_TARGET}:${APP_DIR}/scripts/

# Sync static assets and templates
rsync_safe -avz --checksum $DRYRUN \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='podcast/' \
  static/ ${SSH_TARGET}:${APP_DIR}/static/

rsync_safe -avz --checksum $DRYRUN \
  templates/ ${SSH_TARGET}:${APP_DIR}/templates/

# Sync routes/
rsync_safe -avz --checksum $DRYRUN \
  routes/ ${SSH_TARGET}:${APP_DIR}/routes/

echo "=== Deploy code complete ${DRYRUN:+(dry-run)} ==="
