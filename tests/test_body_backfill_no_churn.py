"""Brief 032 regressions — the `_body_backfilled` dropped-column churn.

Incident: `backfill_body_column` ADDed a transient `_body_backfilled` marker and
DROPped it again on every process start.  PostgreSQL never reuses dropped
attribute numbers, so seven tables each accumulated 1,569 dead attribute slots.

These tests are DDL-level — they capture the statements actually issued, because
the defect was invisible to row-level assertions.

SQLite cases run by default.  The PostgreSQL cases spin their own disposable
cluster and prove the behaviours that only exist on PostgreSQL: real concurrent
startup, atomic ledger conflict handling, and attnum invariance.
"""

import os
import socket
import subprocess
import sys
import tempfile
import threading

import pytest
from sqlalchemy import create_engine, event, inspect as sa_inspect, text

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from db import migrations as M  # noqa: E402


LEGACY_DDL = """
CREATE TABLE meetings (
    id INTEGER PRIMARY KEY,
    body VARCHAR(16) NOT NULL DEFAULT '',
    meeting_id VARCHAR(64),
    meeting_type VARCHAR(64)
);
CREATE TABLE agenda_items (
    id INTEGER PRIMARY KEY, body VARCHAR(16) NOT NULL DEFAULT '',
    meeting_id VARCHAR(64));
CREATE TABLE supporting_documents (
    id INTEGER PRIMARY KEY, body VARCHAR(16) NOT NULL DEFAULT '',
    meeting_id VARCHAR(64));
CREATE TABLE case_events (
    id INTEGER PRIMARY KEY, body VARCHAR(16) NOT NULL DEFAULT '',
    meeting_id VARCHAR(64));
CREATE TABLE meeting_members (
    id INTEGER PRIMARY KEY, body VARCHAR(16) NOT NULL DEFAULT '',
    meeting_id VARCHAR(64));
CREATE TABLE agenda_item_votes (
    id INTEGER PRIMARY KEY, body VARCHAR(16) NOT NULL DEFAULT '',
    meeting_id VARCHAR(64));
CREATE TABLE pz_item_details (
    id INTEGER PRIMARY KEY, body VARCHAR(16) NOT NULL DEFAULT '',
    meeting_id VARCHAR(64));
"""

# (id, meeting_id, meeting_type) — meeting 3 has a NULL meeting_type, which the
# original `meeting_type != 'Planning & Zoning'` predicate left permanently empty.
MEETINGS = [
    (1, "m-bos-1", "Regular Meeting"),
    (2, "m-pz-1", "Planning & Zoning"),
    (3, "m-null-1", None),
]

CHILD_ROWS = [(1, "m-bos-1", "bos"), (2, "m-pz-1", "pz")]

CHILD_TABLES = ("agenda_items", "supporting_documents", "case_events",
                "meeting_members", "agenda_item_votes", "pz_item_details")


class _DDLRecorder:
    """Capture every statement so ADD/DROP churn is directly observable."""

    def __init__(self, engine):
        self.statements = []

        def _before(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)

        event.listen(engine, "before_cursor_execute", _before)

    def reset(self):
        self.statements.clear()

    def alters(self):
        return [s for s in self.statements if "ALTER TABLE" in s.upper()]


def _sqlite_engine(*, populated=False, omit=(), null_meeting_type=True):
    """A temp SQLite DB in the pre-backfill legacy shape."""
    tmp = tempfile.mktemp(suffix=".sqlite")
    engine = create_engine(f"sqlite:///{tmp}", future=True)
    with engine.begin() as conn:
        for statement in LEGACY_DDL.strip().split(";"):
            if statement.strip():
                table = statement.strip().split()[2]
                if table in omit:
                    continue
                conn.execute(text(statement))
        if "meetings" not in omit:
            for mid, mkey, mtype in MEETINGS:
                if mtype is None and not null_meeting_type:
                    continue
                body = ""
                if populated:
                    body = "pz" if mtype == "Planning & Zoning" else "bos"
                conn.execute(
                    text("INSERT INTO meetings (id, meeting_id, meeting_type, body)"
                         " VALUES (:i, :m, :t, :b)"),
                    {"i": mid, "m": mkey, "t": mtype, "b": body})
        for table in CHILD_TABLES:
            if table in omit:
                continue
            for row_id, mkey, expected in CHILD_ROWS:
                conn.execute(
                    text(f"INSERT INTO {table} (id, meeting_id, body) "
                         "VALUES (:i, :m, :b)"),
                    {"i": row_id, "m": mkey, "b": expected if populated else ""})
    return engine


def _bodies(engine):
    with engine.connect() as conn:
        return {t: conn.execute(
            text(f"SELECT id, body FROM {t} ORDER BY id")).fetchall()
            for t in M.BACKFILL_TABLES}


def _columns(engine, table):
    return {c["name"] for c in sa_inspect(engine).get_columns(table)}


def _unresolved(engine, tables=None):
    with engine.connect() as conn:
        return {t: conn.execute(text(
            f"SELECT COUNT(*) FROM {t} WHERE body IS NULL OR body = ''")).scalar()
            for t in (tables or M.BACKFILL_TABLES)}


def _ledger_names(engine):
    with engine.connect() as conn:
        return sorted(r[0] for r in conn.execute(
            text(f"SELECT name FROM {M.LEDGER_TABLE}")).fetchall())


# ── finding 1: no ADD/DROP churn after completion ───────────────────────

def test_two_consecutive_runs_issue_no_ddl_after_completion():
    engine = _sqlite_engine()
    recorder = _DDLRecorder(engine)

    M.backfill_body_column(engine)
    recorder.reset()
    M.backfill_body_column(engine)
    M.backfill_body_column(engine)

    assert recorder.alters() == [], f"churn: {recorder.alters()}"


def test_repeated_runs_never_create_the_transient_marker():
    engine = _sqlite_engine()
    for _ in range(3):
        M.backfill_body_column(engine)
    for table in M.BACKFILL_TABLES:
        assert "_body_backfilled" not in _columns(engine, table), table


# ── finding 2: per-table ledger, no global suppression ──────────────────

def test_completion_is_recorded_per_table_not_globally():
    engine = _sqlite_engine()
    M.backfill_body_column(engine)
    names = _ledger_names(engine)
    assert names == sorted(M.ledger_key(t) for t in M.BACKFILL_TABLES), names
    assert M.BODY_BACKFILL_VERSION not in names, "global key must not be used"


def test_absent_table_is_not_marked_and_is_backfilled_when_it_appears():
    """An absent table must not be suppressed by another table's completion."""
    engine = _sqlite_engine(omit=("pz_item_details",))
    M.backfill_body_column(engine)

    assert M.ledger_key("pz_item_details") not in _ledger_names(engine), (
        "an absent table must not be marked complete")

    # the table appears later (later init phase) -> it must still be backfilled
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE pz_item_details (id INTEGER PRIMARY KEY,"
            " body VARCHAR(16) NOT NULL DEFAULT '', meeting_id VARCHAR(64))"))
        conn.execute(text(
            "INSERT INTO pz_item_details (id, meeting_id, body)"
            " VALUES (1, 'm-pz-1', '')"))

    M.backfill_body_column(engine)
    assert M.ledger_key("pz_item_details") in _ledger_names(engine)
    assert _unresolved(engine, ["pz_item_details"])["pz_item_details"] == 0


def test_present_table_missing_body_fails_closed():
    """A present table without `body` is a real schema error, not a skip."""
    engine = _sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE agenda_items"))
        conn.execute(text(
            "CREATE TABLE agenda_items (id INTEGER PRIMARY KEY,"
            " meeting_id VARCHAR(64))"))
    with pytest.raises(M.SchemaNotReadyError, match="missing 'body'"):
        M.backfill_body_column(engine)
    assert _ledger_names(engine) == [], "nothing may be marked on refusal"


def test_missing_expected_table_is_reported_but_not_marked():
    engine = _sqlite_engine(omit=("case_events",))
    M.backfill_body_column(engine)
    assert M.ledger_key("case_events") not in _ledger_names(engine)


# ── finding 1 / 4: fallback semantics ───────────────────────────────────

def test_null_meeting_type_defaults_to_bos():
    """`meeting_type != 'P&Z'` is NULL-false; NULL must still get a body."""
    engine = _sqlite_engine()
    M.backfill_body_column(engine)
    with engine.connect() as conn:
        got = conn.execute(text(
            "SELECT body FROM meetings WHERE id = 3")).scalar()
    assert got == "bos", f"NULL meeting_type left body as {got!r}"


def test_child_with_no_matching_meeting_falls_back_to_bos():
    engine = _sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO agenda_items (id, meeting_id, body)"
            " VALUES (99, 'no-such-meeting', '')"))
    M.backfill_body_column(engine)
    with engine.connect() as conn:
        got = conn.execute(text(
            "SELECT body FROM agenda_items WHERE id = 99")).scalar()
    assert got == "bos"


def test_empty_parent_body_becomes_bos_not_empty_string():
    """COALESCE alone would pass '' through; NULLIF must convert it."""
    engine = _sqlite_engine()
    # mark meetings complete while one meeting's body is deliberately blank
    with engine.begin() as conn:
        conn.execute(text("UPDATE meetings SET body = '' WHERE id = 1"))
    M.ledger_mark(engine, M.ledger_key("meetings"))
    M.backfill_body_column(engine)   # only the children are pending now

    with engine.connect() as conn:
        got = conn.execute(text(
            "SELECT body FROM agenda_items WHERE meeting_id = 'm-bos-1'")).scalar()
    assert got == "bos", f"empty parent body propagated as {got!r}"


# ── finding 1: postcondition asserted inside the transaction ────────────

def test_ledger_is_not_written_when_the_postcondition_is_unresolved(monkeypatch):
    """Completion must be impossible while any row is still unresolved."""
    engine = _sqlite_engine()
    real_text = M.text

    def neutering_text(statement):
        if "UPDATE supporting_documents" in str(statement):
            return real_text("SELECT 1 WHERE 0")   # valid, but a no-op
        return real_text(statement)

    monkeypatch.setattr(M, "text", neutering_text)
    with pytest.raises(RuntimeError, match="unresolved rows"):
        M.backfill_body_column(engine)
    monkeypatch.setattr(M, "text", real_text)

    assert _ledger_names(engine) == [], (
        "no table may be marked complete when the postcondition fails")


def test_zero_unresolved_rows_when_the_ledger_is_written():
    engine = _sqlite_engine()
    M.backfill_body_column(engine)
    assert set(_unresolved(engine).values()) == {0}
    assert len(_ledger_names(engine)) == len(M.BACKFILL_TABLES)


# ── finding 3 / 4: rollback, retry ──────────────────────────────────────

def test_partial_failure_rolls_back_and_stays_retryable(monkeypatch):
    engine = _sqlite_engine()
    before = _bodies(engine)
    real_text = M.text

    def exploding_text(statement):
        if "UPDATE supporting_documents" in str(statement):
            raise RuntimeError("injected failure")
        return real_text(statement)

    monkeypatch.setattr(M, "text", exploding_text)
    with pytest.raises(RuntimeError, match="injected failure"):
        M.backfill_body_column(engine)
    monkeypatch.setattr(M, "text", real_text)

    assert _ledger_names(engine) == []
    assert _bodies(engine) == before, "partial data must be rolled back"

    M.backfill_body_column(engine)
    assert len(_ledger_names(engine)) == len(M.BACKFILL_TABLES)
    assert set(_unresolved(engine).values()) == {0}


# ── finding 3: concurrent initialization ────────────────────────────────

def test_concurrent_initialization_sqlite_marks_each_table_once():
    engine = _sqlite_engine()
    errors = []

    def worker():
        try:
            M.backfill_body_column(engine)
        except Exception as exc:  # SQLite may report a busy database
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # either it completed cleanly or it failed closed; never a torn result
    names = _ledger_names(engine)
    assert len(names) == len(set(names)), "duplicate ledger rows"
    if not errors:
        assert len(names) == len(M.BACKFILL_TABLES)
        assert set(_unresolved(engine).values()) == {0}


# ── finding 4: dialect behaviour is explicit ────────────────────────────

def test_unsupported_dialect_is_refused_loudly():
    class _Dialect:
        name = "mysql"

    class _Engine:
        dialect = _Dialect()

    with pytest.raises(RuntimeError, match="unsupported dialect"):
        M.backfill_body_column(_Engine())


def test_supported_dialects_are_declared_explicitly():
    assert set(M.SUPPORTED_DIALECTS) == {"postgresql", "sqlite"}


# ── finding 5: no data or body values lost ──────────────────────────────

def test_no_rows_or_existing_body_values_are_lost():
    engine = _sqlite_engine()
    before = _bodies(engine)
    M.backfill_body_column(engine)
    after = _bodies(engine)
    assert {t: len(v) for t, v in after.items()} == \
           {t: len(v) for t, v in before.items()}


def test_existing_body_values_are_never_overwritten():
    engine = _sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE meetings SET body = 'keepme' WHERE id = 3"))
        conn.execute(text(
            "UPDATE agenda_items SET body = 'existing' WHERE id = 1"))

    M.backfill_body_column(engine)

    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT body FROM meetings WHERE id = 3")).scalar() == "keepme"
        assert conn.execute(text(
            "SELECT body FROM agenda_items WHERE id = 1")).scalar() == "existing"


# ════════════════════════════════════════════════════════════════════════
# PostgreSQL: real concurrency and real attribute numbers.
# ════════════════════════════════════════════════════════════════════════

PG_BIN = "/opt/homebrew/opt/postgresql@18/bin"

PG_DDL = LEGACY_DDL.replace("INTEGER PRIMARY KEY",
                            "serial PRIMARY KEY").replace("VARCHAR(16)", "text")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(args, **kw):
    env = dict(os.environ, LC_ALL="C", LANG="C")
    r = subprocess.run([str(a) for a in args], text=True, env=env,
                       capture_output=True, **kw)
    if r.returncode:
        raise RuntimeError(f"{args[0]} failed: {r.stderr.strip()}")
    return r


@pytest.fixture(scope="module")
def pg_cluster():
    if not (os.path.exists(f"{PG_BIN}/initdb")):
        pytest.fail(f"postgresql binaries not found at {PG_BIN}")
    with tempfile.TemporaryDirectory(prefix="poliscopic-032-pg-") as temp:
        data = os.path.join(temp, "cluster")
        port = _free_port()
        _run([f"{PG_BIN}/initdb", "-A", "trust", "-U", "poliscopic", "-D", data,
              "--encoding=UTF8", "--locale=C"])
        _run([f"{PG_BIN}/pg_ctl", "-D", data,
              "-l", os.path.join(temp, "pg.log"),
              "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"])
        try:
            yield {"port": port}
        finally:
            _run([f"{PG_BIN}/pg_ctl", "-D", data, "-m", "fast", "-w", "stop"],
                 check=False)


def _pg_engine(pg_cluster, name):
    port = pg_cluster["port"]
    _run([f"{PG_BIN}/createdb", "-h", "127.0.0.1", "-p", port,
          "-U", "poliscopic", name])
    url = f"postgresql://poliscopic@127.0.0.1:{port}/{name}"
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        for statement in PG_DDL.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
        for mid, mkey, mtype in MEETINGS:
            conn.execute(
                text("INSERT INTO meetings (id, meeting_id, meeting_type, body)"
                     " VALUES (:i, :m, :t, '')"),
                {"i": mid, "m": mkey, "t": mtype})
        for table in CHILD_TABLES:
            for row_id, mkey, _ in CHILD_ROWS:
                conn.execute(
                    text(f"INSERT INTO {table} (id, meeting_id, body)"
                         " VALUES (:i, :m, '')"), {"i": row_id, "m": mkey})
    return engine


def _pg_attnums(engine, tables) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            select c.relname,
                   count(*) filter (where a.attnum > 0 and not a.attisdropped),
                   count(*) filter (where a.attisdropped),
                   max(a.attnum)
            from pg_attribute a join pg_class c on c.oid = a.attrelid
            where c.relname = any(:t) and c.relnamespace = current_schema()::regnamespace
            group by c.relname order by c.relname"""),
            {"t": list(tables)}).fetchall()
    return {r[0]: {"visible": r[1], "dropped": r[2], "max_attnum": r[3]}
            for r in rows}


def test_pg_repeated_calls_do_not_increase_attnum(pg_cluster):
    """The heart of the incident: on PostgreSQL, attnum must stop growing."""
    engine = _pg_engine(pg_cluster, "churn_attnum")
    tables = [t for t in M.BACKFILL_TABLES
              if t in sa_inspect(engine).get_table_names()]

    M.backfill_body_column(engine)
    first = _pg_attnums(engine, tables)
    for _ in range(5):
        M.backfill_body_column(engine)
    later = _pg_attnums(engine, tables)

    for table in tables:
        assert later[table]["dropped"] == 0, (
            f"{table} accumulated {later[table]['dropped']} dropped slots")
        assert later[table]["max_attnum"] == first[table]["max_attnum"], (
            f"{table} attnum grew across repeated calls")
        assert later[table]["visible"] == first[table]["visible"]


def test_pg_concurrent_initialization_marks_each_table_once(pg_cluster):
    """Real concurrent startup against PostgreSQL (finding 3)."""
    engine = _pg_engine(pg_cluster, "churn_concurrent")
    tables = [t for t in M.BACKFILL_TABLES
              if t in sa_inspect(engine).get_table_names()]
    failures = []

    def worker():
        try:
            M.backfill_body_column(engine)
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures, f"concurrent startup raised: {failures}"
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(
            text(f"SELECT name FROM {M.LEDGER_TABLE}")).fetchall()]
    assert len(names) == len(set(names)), "duplicate ledger rows under concurrency"
    assert sorted(names) == sorted(M.ledger_key(t) for t in tables)
    assert len(names) == len(tables)
    assert set(_unresolved(engine).values()) == {0}


def test_pg_postconditions_and_attnums_hold(pg_cluster):
    engine = _pg_engine(pg_cluster, "churn_post")
    M.backfill_body_column(engine)
    assert set(_unresolved(engine).values()) == {0}
    with engine.connect() as conn:
        nulls = conn.execute(text(
            "SELECT COUNT(*) FROM meetings WHERE body IS NULL OR body = ''")
        ).scalar()
        bos = conn.execute(text(
            "SELECT body FROM meetings WHERE meeting_type IS NULL")).scalar()
    assert nulls == 0
    assert bos == "bos"
    stats = _pg_attnums(engine, [t for t in M.BACKFILL_TABLES
                                 if t in sa_inspect(engine).get_table_names()])
    assert all(v["dropped"] == 0 for v in stats.values())


# ── finding 3 (re-review): concurrent FIRST startup, no ledger yet ──────

def test_pg_concurrent_first_startup_without_existing_ledger(pg_cluster):
    """Two processes racing to create the ledger must both finish safely.

    This is the case the ordering fix exists for: the advisory lock must be held
    *before* `CREATE TABLE IF NOT EXISTS _migration_ledger` can run.
    """
    engine = _pg_engine(pg_cluster, "churn_firststart")

    with engine.connect() as conn:
        assert conn.execute(text(
            "select to_regclass('_migration_ledger')")).scalar() is None, \
            "precondition: the ledger must not exist yet"

    tables = [t for t in M.BACKFILL_TABLES
              if t in sa_inspect(engine).get_table_names()]
    before = _pg_attnums(engine, tables)
    failures = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait(timeout=30)   # maximise the overlap
            M.backfill_body_column(engine)
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # both callers finish safely
    assert not failures, f"concurrent first startup raised: {failures}"

    # the ledger is correct: one row per table, no duplicates
    names = _ledger_names(engine)
    assert sorted(names) == sorted(M.ledger_key(t) for t in tables), names
    assert len(names) == len(set(names)), "duplicate ledger rows"

    # the backfill postcondition holds
    assert set(_unresolved(engine).values()) == {0}

    # repeated startup does not advance attnums
    M.backfill_body_column(engine)
    M.backfill_body_column(engine)
    after = _pg_attnums(engine, tables)
    for table in tables:
        assert after[table]["max_attnum"] == before[table]["max_attnum"], (
            f"{table} attnum advanced across repeated startup")
        assert after[table]["dropped"] == 0, (
            f"{table} accumulated {after[table]['dropped']} dropped slots")


# ── finding 2 (re-review): lock-acquisition cleanup ─────────────────────

class _RecordingConn:
    """Minimal connection stub: records execute() and close() only."""

    def __init__(self, boom: bool):
        self.closed = False
        self._boom = boom

    def execute(self, statement, params=None):
        if self._boom:
            raise RuntimeError("advisory lock acquisition failed")

    def close(self):
        self.closed = True


class _PgShapedEngine:
    """Just enough shape for _acquire_lock: a dialect name and connect()."""

    class dialect:
        name = "postgresql"

    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def test_lock_acquisition_failure_closes_the_connection():
    """A failed advisory lock must not leak the acquired connection."""
    conn = _RecordingConn(boom=True)
    with pytest.raises(RuntimeError, match="acquisition failed"):
        M._acquire_lock(_PgShapedEngine(conn))
    assert conn.closed is True


def test_lock_acquisition_success_keeps_the_connection_open():
    conn = _RecordingConn(boom=False)
    token = M._acquire_lock(_PgShapedEngine(conn))
    assert token is conn
    assert conn.closed is False, "a held lock must stay open until release"


def test_sqlite_acquisition_returns_no_token_and_needs_no_cleanup():
    """SQLite has no separate primitive; that must be explicit, not incidental."""
    engine = _sqlite_engine()
    assert M._acquire_lock(engine) is None
    M._release_lock(engine, None)   # must be a safe no-op
