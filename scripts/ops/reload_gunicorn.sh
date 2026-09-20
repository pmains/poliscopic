#!/usr/bin/env bash
# Gracefully reload gunicorn — sends SIGHUP, old workers finish current
# requests while new workers start with the deployed code.
#
# No dropped connections.  If the reload fails, old workers keep serving.
#
# Usage:
#   ./scripts/reload_gunicorn.sh

set -euo pipefail

# ── Production interlock (FAIL CLOSED) ───────────────────────────────────────
# Restarting production gunicorn is a production mutation; refuse before SSH.
_pi="${POLISCOPIC_PYTHON:-.venv/bin/python}"
[ -x "$_pi" ] || _pi=python3
"$_pi" scripts/ops/production_interlock.py check \
    --operation OP-RESTORE --entry-point scripts/ops/reload_gunicorn.sh >&2 || {
    echo "REFUSED: production interlock blocked this reload." >&2
    exit 3
}

SSH_ROOT="${POLISCOPIC_DEPLOY_ROOT:?Set POLISCOPIC_DEPLOY_ROOT}"

echo "=== Graceful gunicorn reload ==="
ssh ${SSH_ROOT} "systemctl reload poliscopic" && echo "✅ gunicorn reloaded"
