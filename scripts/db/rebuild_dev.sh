#!/bin/bash
set -euo pipefail

# rebuild_dev.sh — Rebuild dev database from prod backup with schema migration
# Run from this project directory on the Mac.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

DUMP="data/prod-backup-20260721-2354-full.dump"
PG18_BIN="/opt/homebrew/Cellar/postgresql@18/18.4/bin"
DEV_HOST="${POLISCOPIC_DEV_DB_HOST:?Set POLISCOPIC_DEV_DB_HOST}"
DEV_SSH_HOST="${POLISCOPIC_DEV_SSH_HOST:?Set POLISCOPIC_DEV_SSH_HOST}"
DEV_PORT="5432"
DEV_PASS="${DEV_PASS:?Set DEV_PASS env var}"

echo "═══ Step 1: Create temporary database (via Windows SSH) ═══"
ssh "$DEV_SSH_HOST" "C:\pgsql\pgsql\bin\psql -U postgres -c \"DROP DATABASE IF EXISTS poliscopic_restored;\" -c \"CREATE DATABASE poliscopic_restored;\" -c \"GRANT ALL ON SCHEMA public TO poliscopic;\" -c \"ALTER DATABASE poliscopic_restored OWNER TO poliscopic;\"" 2>&1

echo ""
echo "═══ Step 2: Restore production dump into poliscopic_restored ═══"
echo "  (This will take a few minutes...)"
PGPASSWORD="$DEV_PASS" "$PG18_BIN/pg_restore" \
  --dbname="host=$DEV_HOST port=$DEV_PORT dbname=poliscopic_restored user=poliscopic password=$DEV_PASS" \
  --no-owner --no-acl \
  "$DUMP" 2>&1 | tail -8

echo ""
echo "═══ Step 3: Verify old schema restored ═══"
"$PG18_BIN/psql" -h "$DEV_HOST" -p "$DEV_PORT" -U poliscopic -d poliscopic_restored \
  -c "SELECT '  meeting_supervisors: ' || COUNT(*) || ' rows' FROM meeting_supervisors;" \
  -c "SELECT '  meetings:            ' || COUNT(*) || ' rows' FROM meetings;" \
  2>&1

echo ""
echo "═══ Step 4: Run schema migration (init_db) against poliscopic_restored ═══"
PYTHONPATH="$PROJECT_ROOT/scripts:$PROJECT_ROOT" .venv/bin/python3 << ENDPYTHON
import os, sys

dev_pass = os.environ['DEV_PASS']
dev_host = os.environ.get('DEV_HOST', '127.0.0.1')
restored_url = f"postgresql://poliscopic:{dev_pass}@{dev_host}:5432/poliscopic_restored"
os.environ["DATABASE_URL"] = restored_url

# Clear cached engine state
import db.core as core
if core._engine:
    core._engine.dispose()
core.DATABASE_URL = restored_url
core._engine = None
core._SessionLocal = None

# Force-reload
import importlib
import db.migrations
importlib.reload(db.migrations)

print("Running migrations on poliscopic_restored...")
db.migrations.init_db()
print("Migration complete!")
ENDPYTHON

echo ""
echo "═══ Step 5: Verify new schema ═══"
"$PG18_BIN/psql" -h "$DEV_HOST" -p "$DEV_PORT" -U poliscopic -d poliscopic_restored -c "
  SELECT '  meeting_members:     ' || COUNT(*) || ' rows' FROM meeting_members;
  SELECT '  member_id exists:    ' || column_name FROM information_schema.columns
    WHERE table_name='meeting_members' AND column_name='member_id';
  SELECT '  supervisor_id gone:  ' || column_name FROM information_schema.columns
    WHERE table_name='meeting_members' AND column_name='supervisor_id';
  SELECT '  meetings:            ' || COUNT(*) || ' rows' FROM meetings;
  SELECT '  agenda_items:        ' || COUNT(*) || ' rows' FROM agenda_items;
  SELECT '  member_votes:        ' || COUNT(*) || ' rows' FROM member_votes;
  SELECT '  supporting_docs:     ' || COUNT(*) || ' rows' FROM supporting_documents;
  SELECT '  persons:             ' || COUNT(*) || ' rows' FROM persons;
"

echo ""
echo "═══ Step 6: Confirm old tables gone ═══"
"$PG18_BIN/psql" -h "$DEV_HOST" -p "$DEV_PORT" -U poliscopic -d poliscopic_restored -c "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='public'
    AND table_name IN ('meeting_supervisors','supervisor_votes')
  ORDER BY table_name;
" | head -3
echo "  (Should return zero rows)"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  READY TO SWAP"
echo ""
echo "  If numbers look right, run the swap:"
echo ""
echo "    ssh ${DEV_SSH_HOST} '<run the reviewed database swap command>'"
echo ""
echo "  Then restart your Flask app on the Mac."
echo "═══════════════════════════════════════════════════════════════"
