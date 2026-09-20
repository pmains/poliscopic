#!/usr/bin/env bash
#
# Brief 032 — recovery proof on a REPRESENTATIVE schema.
#
# The earlier version used seven toy tables.  This version builds a schema that
# mirrors the real one's structure — primary keys, foreign keys, unique
# constraints, indexes, sequences, a dependent/child table, and an explicit
# integrity fingerprint — then proves that a pg_dump/restore into a fresh
# database:
#
#   * collapses dropped attribute slots (the incident) to zero,
#   * preserves constraints, indexes, sequences (incl. last_value),
#   * preserves ownership and row counts,
#   * preserves dependent rows and integrity fingerprints.
#
# DEVELOPMENT-ONLY. Creates and destroys its own disposable cluster. Reads no
# connection string, never touches poliscopic_dev, never touches production.
#
# Usage: scripts/db/attnum_recovery_proof.sh

set -euo pipefail

PG="${PG_BIN:-/opt/homebrew/opt/postgresql@18/bin}"
CHURN_ROUNDS="${CHURN_ROUNDS:-25}"
BASE_TABLES="meetings agenda_items supporting_documents pz_item_details agenda_item_votes case_events meeting_members"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/attnum-proof-XXXXXX")"
DATA="$TMP/cluster"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
SRC="attnum_proof_src"
DST="attnum_proof_dst"
DUMP="$TMP/proof.dump"
REDUMP="$TMP/proof-redump.dump"

[ -x "$PG/initdb" ] || { echo "FATAL: no initdb at $PG"; exit 1; }

cleanup() {
  "$PG/pg_ctl" -D "$DATA" -m fast -w stop >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

export LC_ALL=C LANG=C
umask 077

echo "== disposable cluster =="
"$PG/initdb" -A trust -U poliscopic -D "$DATA" --encoding=UTF8 --locale=C >/dev/null
"$PG/pg_ctl" -D "$DATA" -l "$TMP/pg.log" -o "-h 127.0.0.1 -p $PORT" -w start >/dev/null
echo "   port=$PORT (destroyed on exit)"

q() { "$PG/psql" -h 127.0.0.1 -p "$PORT" -U poliscopic -v ON_ERROR_STOP=1 -qAt "$@"; }

schema_facts() {  # constraints + indexes + owners, normalised
  q -d "$1" -c "
    select 'CONSTRAINT ' || conrelid::regclass::text || ' ' || conname || ' ' ||
           pg_get_constraintdef(oid)
    from pg_constraint where connamespace = 'public'::regnamespace
    union all
    select 'INDEX ' || tablename || ' ' || indexname || ' ' || indexdef
    from pg_indexes where schemaname = 'public' and tablename <> '_migration_ledger'
    union all
    select 'OWNER ' || tablename || ' ' || tableowner
    from pg_tables where schemaname = 'public' and tablename <> '_migration_ledger'
    order by 1"
}

sequences() {  # name + last_value, so a restore cannot silently reset them
  q -d "$1" -c "
    select c.relname || '=' || s.last_value
    from pg_sequences s join pg_class c on c.relname = s.sequencename
    where s.schemaname = 'public' order by 1"
}

attnums() {
  q -d "$1" -c "
    select c.relname || ' visible=' ||
           count(*) filter (where a.attnum > 0 and not a.attisdropped) ||
           ' dropped=' || count(*) filter (where a.attisdropped) ||
           ' max_attnum=' || max(a.attnum)
    from pg_attribute a join pg_class c on c.oid = a.attrelid
    where c.relname = any(string_to_array('$BASE_TABLES',' '))
      and c.relnamespace = 'public'::regnamespace
    group by c.relname order by c.relname"
}

fingerprint() {  # deterministic per-table digest of ordered rows
  local out=""
  for t in $BASE_TABLES; do
    out="$out $(q -d "$1" -c "select '$t=' || coalesce(md5(string_agg(t::text, '|' order by id)), 'empty') from $t t")"
  done
  echo "$out" | tr ' ' '\n' | sed '/^$/d'
}

echo
echo "== build representative source schema =="
"$PG/createdb" -h 127.0.0.1 -p "$PORT" -U poliscopic "$SRC"
q -d "$SRC" -c "create table meetings (id serial primary key, body text not null default '', meeting_id text not null, meeting_type text, constraint uq_m unique (body, meeting_id))" >/dev/null
q -d "$SRC" -c "create table agenda_items (id serial primary key, meeting_id text, body text not null default '', num text)" >/dev/null
q -d "$SRC" -c "create table supporting_documents (id serial primary key, meeting_id text, body text not null default '')" >/dev/null
q -d "$SRC" -c "create table case_events (id serial primary key, meeting_id text, body text not null default '')" >/dev/null
q -d "$SRC" -c "create table meeting_members (id serial primary key, meeting_id text, body text not null default '')" >/dev/null
q -d "$SRC" -c "create table agenda_item_votes (id serial primary key, meeting_id text, body text not null default '')" >/dev/null
q -d "$SRC" -c "create table pz_item_details (id serial primary key, meeting_id text, body text not null default '')" >/dev/null
# dependent (child) table with a foreign key into a base table
q -d "$SRC" -c "create table item_documents (id serial primary key, agenda_item_id integer references agenda_items(id), body text default '')" >/dev/null
# secondary indexes
q -d "$SRC" -c "create index idx_meetings_meeting_id on meetings(meeting_id)" >/dev/null
q -d "$SRC" -c "create index idx_items_meeting_id on agenda_items(meeting_id)" >/dev/null

q -d "$SRC" -c "insert into meetings (body, meeting_id, meeting_type) select '', 'm'||g, case when g % 3 = 0 then 'Planning & Zoning' when g % 3 = 1 then 'Regular' else null end from generate_series(1,200) g" >/dev/null
for t in agenda_items supporting_documents case_events meeting_members agenda_item_votes pz_item_details; do
  q -d "$SRC" -c "insert into $t (meeting_id, body) select 'm'||g, '' from generate_series(1,200) g" >/dev/null
done
q -d "$SRC" -c "insert into item_documents (agenda_item_id, body) select id, '' from agenda_items" >/dev/null

for t in $BASE_TABLES; do
  for _ in $(seq 1 "$CHURN_ROUNDS"); do
    q -d "$SRC" -c "alter table $t add column _body_backfilled boolean not null default false" >/dev/null
    q -d "$SRC" -c "alter table $t drop column _body_backfilled" >/dev/null
  done
done
echo "   churn rounds per base table: $CHURN_ROUNDS"
echo "   [BEFORE]"; attnums "$SRC" | sed 's/^/     /'

BEFORE_SCHEMA="$(schema_facts "$SRC")"
BEFORE_SEQS="$(sequences "$SRC")"
BEFORE_FP="$(fingerprint "$SRC")"

echo
echo "== backup =="
"$PG/pg_dump" -h 127.0.0.1 -p "$PORT" -U poliscopic -Fc --no-owner --no-privileges -d "$SRC" -f "$DUMP"
echo "   mode=$(stat -f '%Lp' "$DUMP" 2>/dev/null || stat -c '%a' "$DUMP")"
echo "   sha256=$(shasum -a 256 "$DUMP" | cut -d' ' -f1)"

echo
echo "== restore into a fresh database (destination of a different name) =="
"$PG/createdb" -h 127.0.0.1 -p "$PORT" -U poliscopic "$DST"
q -d "$DST" -c "drop schema if exists public cascade; create schema public" >/dev/null
"$PG/pg_restore" -h 127.0.0.1 -p "$PORT" -U poliscopic --no-owner --no-privileges -d "$DST" "$DUMP" >/dev/null
echo "   [AFTER]"; attnums "$DST" | sed 's/^/     /'

AFTER_SCHEMA="$(schema_facts "$DST")"
AFTER_SEQS="$(sequences "$DST")"
AFTER_FP="$(fingerprint "$DST")"

echo
echo "== assertions =="
fail=0
ok()   { echo "   PASS  $1"; }
bad()  { echo "   FAIL  $1"; fail=1; }

before_dropped="$(attnums "$SRC" | sed -n 's/.*dropped=\([0-9]*\).*/\1/p' | sort -u | tr -d ' ')"
[ "$before_dropped" = "$CHURN_ROUNDS" ] && ok "source accumulated $before_dropped dropped slots per table" \
  || bad "expected $CHURN_ROUNDS dropped slots, got '$before_dropped'"

after_dropped="$(attnums "$DST" | sed -n 's/.*dropped=\([0-9]*\).*/\1/p' | paste -sd+ - | bc)"
[ "${after_dropped:-1}" = "0" ] && ok "restored base tables have ZERO dropped attribute slots" \
  || bad "restored tables still carry $after_dropped dropped slots"

if attnums "$DST" | awk '{gsub(/visible=|dropped=|max_attnum=/,""); if ($2 != $4) bad=1} END{exit bad}'; then
  ok "max_attnum == visible (contiguous) for every restored table"
else
  bad "restored tables are not contiguous"
fi

# constraints + indexes + ownership must survive the round trip
if [ "$BEFORE_SCHEMA" = "$AFTER_SCHEMA" ]; then
  ok "constraints, indexes and ownership identical after restore"
else
  bad "schema objects differ after restore"; diff <(echo "$BEFORE_SCHEMA") <(echo "$AFTER_SCHEMA") | head -8
fi

# sequences must not silently reset
if [ "$BEFORE_SEQS" = "$AFTER_SEQS" ]; then
  ok "sequence names and last_value preserved"
else
  bad "sequences differ"; diff <(echo "$BEFORE_SEQS") <(echo "$AFTER_SEQS") | head -8
fi

# dependents must be intact, not orphaned
orphans="$(q -d "$DST" -c "select count(*) from item_documents d left join agenda_items a on a.id = d.agenda_item_id where a.id is null")"
[ "$orphans" = "0" ] && ok "dependent table has no orphaned rows after restore" \
  || bad "$orphans dependent rows were orphaned"

# integrity fingerprints must match exactly
if [ "$BEFORE_FP" = "$AFTER_FP" ]; then
  ok "per-table integrity fingerprints identical before/after"
else
  bad "integrity fingerprints differ"; diff <(echo "$BEFORE_FP") <(echo "$AFTER_FP") | head -8
fi

# row counts
for t in $BASE_TABLES; do
  n="$(q -d "$DST" -c "select count(*) from $t")"
  [ "$n" = "200" ] || bad "$t row count $n != 200"
done
ok "all base tables preserved 200 rows each"

# a second dump must not reintroduce churn
"$PG/pg_dump" -h 127.0.0.1 -p "$PORT" -U poliscopic -Fc --no-owner --no-privileges -d "$DST" -f "$REDUMP"
[ "$(attnums "$DST" | sed -n 's/.*dropped=\([0-9]*\).*/\1/p' | sort -u | tr -d ' ')" = "0" ] \
  && ok "re-dump of the restored database contains no dropped slots" \
  || bad "churn reappeared after re-dump"

echo
if [ "$fail" = "0" ]; then
  echo "RESULT: RECOVERY MECHANISM VERIFIED on a representative schema"
else
  echo "RESULT: RECOVERY MECHANISM FAILED"
fi
exit "$fail"
