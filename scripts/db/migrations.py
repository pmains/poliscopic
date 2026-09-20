"""migrations module."""

import logging
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

from sqlalchemy import func, inspect as sa_inspect, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from db.models import (Base, Meeting, AgendaItem, SupportingDocument,
    CaseEvent, MeetingMember, AgendaItemVote, PZItemDetail,
    BodyMembership, Person, BodySeat, PublicBody, Jurisdiction,
    IngestFailure)
from db.core import get_engine, get_session

def init_db() -> None:
    """Create all tables if they don't exist, or migrate existing ones."""
    Base.metadata.create_all(bind=get_engine())
    init_poliscopic_models()

    _migrate_table("supporting_documents")

    engine = get_engine()
    _migrate_col(engine, "agenda_items", "c_number", "VARCHAR(32) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "c_number_base", "VARCHAR(48) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "c_number_revision", "VARCHAR(16) DEFAULT NULL")

    _migrate_table("cases")
    _migrate_table("case_events")
    _migrate_table("pz_item_details")

    # One-time backfill: normalize legacy non-ISO meeting dates
    _normalize_existing_meeting_dates(engine)

    # Add item_title column to article_sources if missing
    _migrate_col(engine, "article_sources", "item_title", "VARCHAR(512) NOT NULL DEFAULT ''")

    _migrate_col(engine, "agenda_items", "case_number", "VARCHAR(32) NOT NULL DEFAULT ''")

    _migrate_table("persons")
    _migrate_meeting_members_table()
    _migrate_table("meeting_members")
    _migrate_table("agenda_item_votes")
    _migrate_col(engine, "agenda_item_votes", "conditions", "TEXT DEFAULT NULL")
    _migrate_col(engine, "agenda_item_votes", "is_split_vote", "BOOLEAN DEFAULT NULL")
    _migrate_col(engine, "agenda_item_votes", "unanimous", "BOOLEAN DEFAULT NULL")
    _migrate_col(engine, "agenda_item_votes", "majority_position", "VARCHAR(16) DEFAULT NULL")
    _migrate_col(engine, "agenda_items", "lifecycle_status", "VARCHAR(32) DEFAULT NULL")
    # supervisor_votes was migrated to member_votes — table no longer exists
    _migrate_table("public_body_members")
    _migrate_table("meeting_attendance")
    _migrate_table("member_votes")
    _migrate_table("executive_session_participants")
    _migrate_table("_ingest_failures")

    # Resumable sync columns
    _migrate_col(engine, "meetings", "sync_status", "VARCHAR(32) NOT NULL DEFAULT 'pending'")
    _migrate_col(engine, "meetings", "last_synced_at", "DATETIME")
    _migrate_col(engine, "meetings", "last_attempted_at", "DATETIME")
    _migrate_col(engine, "meetings", "last_error", "TEXT")
    _migrate_col(engine, "meetings", "retry_count", "INTEGER NOT NULL DEFAULT 0")
    _migrate_col(engine, "meetings", "item_count_expected", "INTEGER")
    _migrate_col(engine, "meetings", "item_count_actual", "INTEGER")
    _migrate_col(engine, "meetings", "supporting_doc_count", "INTEGER NOT NULL DEFAULT 0")
    _migrate_col(engine, "meetings", "items_extracted", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "meetings", "supporting_docs_extracted", "BOOLEAN NOT NULL DEFAULT 0")

    # Normalization columns
    _migrate_col(engine, "meetings", "meeting_title_raw", "TEXT DEFAULT NULL")
    _migrate_col(engine, "meetings", "meeting_context", "VARCHAR(128) DEFAULT NULL")
    _migrate_col(engine, "meetings", "meeting_body", "VARCHAR(128) DEFAULT NULL")
    _migrate_col(engine, "meetings", "display_name", "VARCHAR(256) DEFAULT NULL")

    # Multi-jurisdiction columns
    _migrate_col(engine, "meetings", "jurisdiction_id", "INTEGER DEFAULT NULL")
    _migrate_col(engine, "meetings", "public_body_id", "INTEGER DEFAULT NULL")
    _migrate_col(engine, "meetings", "source_system", "VARCHAR(64) DEFAULT NULL")
    _migrate_col(engine, "meetings", "source_instance_url", "VARCHAR(512) DEFAULT NULL")
    _migrate_col(engine, "agenda_items", "jurisdiction_id", "INTEGER DEFAULT NULL")
    _migrate_col(engine, "agenda_items", "public_body_id", "INTEGER DEFAULT NULL")
    _migrate_col(engine, "supporting_documents", "jurisdiction_id", "INTEGER DEFAULT NULL")

    # Create indexes for multi-jurisdiction lookups
    _ensure_index(engine, "meetings", "idx_meetings_jurisdiction_id", "jurisdiction_id")
    _ensure_index(engine, "meetings", "idx_meetings_public_body_id", "public_body_id")
    _ensure_index(engine, "meetings", "idx_meetings_source_system", "source_system")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_jurisdiction_id", "jurisdiction_id")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_public_body_id", "public_body_id")
    _ensure_index(engine, "supporting_documents", "idx_supporting_docs_jurisdiction_id", "jurisdiction_id")

    # Backfill multi-jurisdiction columns for existing Maricopa records
    backfill_multi_jurisdiction_columns(engine)

    # Body column migrations (for body-scoped identity)
    #
    # NOTE: `_body_backfilled` is deliberately NOT created here.  Brief 032:
    # creating it on every initialization and dropping it again in
    # backfill_body_column() burned one permanent attribute slot per process
    # start on each of seven tables, because PostgreSQL never reuses dropped
    # attribute numbers.  Completion is now tracked in `_migration_ledger`.
    _migrate_col(engine, "meetings", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "supporting_documents", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "case_events", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "meeting_members", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_item_votes", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "pz_item_details", "body", "VARCHAR(16) NOT NULL DEFAULT ''")

    # Create additional indexes for common query patterns
    _ensure_index(engine, "meetings", "idx_meetings_date_desc", "meeting_date DESC")
    _ensure_index(engine, "meetings", "idx_meetings_meeting_type", "meeting_type")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_meeting_id", "meeting_id")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_c_number", "c_number")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_c_number_base", "c_number_base")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_agenda_item_number", "agenda_item_number")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_body_meeting_id", "body, meeting_id")

    # Create poliscopic tables
    init_poliscopic_models(engine)

    # Composite index for /permits aggregate queries (jurisdiction + category + work_type + date)
    _ensure_index(engine, "permits", "ix_permits_jur_cat_wt_issuedate",
                  "jurisdiction, normalized_category, work_type, permit_issue_date")
    _ensure_index(engine, "permits", "ix_permits_dedup_parts",
                  "permit_number, row_hash, permit_square_feet")

    # Seed default jurisdiction and bodies
    seed_default_jurisdictions()

    # Backfill existing records to body='bos' and determine pz from meeting_type
    backfill_body_column(engine)

    # Drop deprecated Person columns and legacy tables
    _drop_deprecated_person_columns()

    # Migrate to historical membership model (BodySeat + BodyMembership)
    _migrate_membership_model()

def _add_col_safe(engine: Engine, table: str, col: str, col_def: str) -> None:
    """Add a column if it doesn't exist. Works on both SQLite and PostgreSQL.

    Unlike _migrate_col (which silently swallows errors), this logs failures
    and uses PostgreSQL-native ADD COLUMN IF NOT EXISTS for Postgres.
    """
    inspector = sa_inspect(engine)
    if table not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns(table)}
    if col in existing_cols:
        return
    try:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_def}'))
            else:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {col_def}'))
    except Exception as e:
        log.warning(f"_add_col_safe: could not add {table}.{col}: {e}")


def backfill_multi_jurisdiction_columns(engine: Engine) -> None:
    """Backfill jurisdiction_id, public_body_id for existing Maricopa records.

    Maps meetings.body (body_code) to public_bodies.body_code to set public_body_id.
    All existing data belongs to Maricopa County (jurisdiction_id=1).
    Uses _multi_jurisdiction_backfilled as a migration marker.
    """
    inspector = sa_inspect(engine)
    if "meetings" not in inspector.get_table_names():
        return

    # Add marker column if needed — use safe variant that works on PostgreSQL
    _add_col_safe(engine, "meetings", "_multi_jurisdiction_backfilled", "BOOLEAN NOT NULL DEFAULT false")
    _add_col_safe(engine, "agenda_items", "_multi_jurisdiction_backfilled", "BOOLEAN NOT NULL DEFAULT false")
    _add_col_safe(engine, "supporting_documents", "_multi_jurisdiction_backfilled", "BOOLEAN NOT NULL DEFAULT false")

    with engine.connect() as conn:
        # Check if already backfilled — handle case where column add was skipped
        try:
            existing = conn.execute(
                text("SELECT COUNT(*) FROM meetings WHERE _multi_jurisdiction_backfilled IS false")
            ).scalar()
            if existing == 0:
                return
        except Exception as e:
            log.warning(f"backfill_multi_jurisdiction_columns: column check failed ({e}), skipping")
            return

        # Map body_code to public_body_id
        body_map = {}
        rows = conn.execute(
            text("SELECT id, body_code FROM public_bodies WHERE body_code IS NOT NULL")
        ).fetchall()
        for row in rows:
            body_map[row[1]] = row[0]

        # Backfill meetings — use Python dict to avoid LIMIT-1 ambiguity
        for body_code, pb_id in body_map.items():
            conn.execute(
                text(
                    "UPDATE meetings SET public_body_id = :pb_id, "
                    "jurisdiction_id = (SELECT jurisdiction_id FROM public_bodies WHERE id = :pb_id), "
                    "_multi_jurisdiction_backfilled = true "
                    "WHERE body = :body_code AND _multi_jurisdiction_backfilled IS false"
                ),
                {"pb_id": pb_id, "body_code": body_code}
            )
        # Any remaining unbilled meetings (no matching public body) just get flagged
        conn.execute(
            text("UPDATE meetings SET _multi_jurisdiction_backfilled = true WHERE _multi_jurisdiction_backfilled IS false")
        )

        # Backfill agenda_items from their parent meeting
        conn.execute(
            text("""
                UPDATE agenda_items
                SET jurisdiction_id = (
                        SELECT COALESCE(m.jurisdiction_id, 1) FROM meetings m
                        WHERE m.meeting_id = agenda_items.meeting_id
                          AND m.body = agenda_items.body
                        LIMIT 1
                    ),
                    public_body_id = (
                        SELECT m.public_body_id FROM meetings m
                        WHERE m.meeting_id = agenda_items.meeting_id
                          AND m.body = agenda_items.body
                        LIMIT 1
                    ),
                    _multi_jurisdiction_backfilled = true
                WHERE _multi_jurisdiction_backfilled IS false
            """)
        )

        # Backfill supporting_documents from their parent meeting
        conn.execute(
            text("""
                UPDATE supporting_documents
                SET jurisdiction_id = 1,
                    _multi_jurisdiction_backfilled = true
                WHERE _multi_jurisdiction_backfilled IS false
            """)
        )

        conn.commit()

# ── Body backfill (Brief 032) ────────────────────────────────────────────
#
# This backfill used to MIGRATE by ADDing a transient `_body_backfilled`
# marker, using it, then DROPping it — on every process start.  PostgreSQL
# never reuses dropped attribute numbers, so each start burned one permanent
# attribute slot on each of seven tables (1,569 slots lost per table before
# this was found).
#
# Completion is now recorded once in `_migration_ledger`, and the need for
# work is decided by a read-only DATA predicate.  A routine startup on an
# already-backfilled database performs no ADD/DROP at all.

BODY_BACKFILL_VERSION = "body_backfill_v1"

LEDGER_TABLE = "_migration_ledger"

# Fixed advisory-lock id used to serialise concurrent startup (Brief 032
# review finding 3).  Arbitrary but stable; it must never change.
BODY_BACKFILL_LOCK_ID = 815_032_001

BACKFILL_TABLES = (
    "meetings",
    "agenda_items",
    "supporting_documents",
    "case_events",
    "meeting_members",
    "agenda_item_votes",
    "pz_item_details",
)

SUPPORTED_DIALECTS = ("postgresql", "sqlite")


class SchemaNotReadyError(RuntimeError):
    """Expected schema is not ready for the body backfill.

    Raised rather than marking completion, so an incomplete schema can never
    permanently suppress the work (Brief 032 review finding 2).
    """


def ledger_key(table: str) -> str:
    """Per-table ledger key.

    A single global key would mark completion even when a table was absent or
    incomplete at the time, permanently suppressing its later backfill.  One key
    per table keeps each table's progress independent (finding 2).
    """
    return f"{BODY_BACKFILL_VERSION}:{table}"


def _require_supported_dialect(engine: Engine) -> str:
    """Fail loudly on an unsupported dialect rather than diverge silently."""
    dialect = engine.dialect.name
    if dialect not in SUPPORTED_DIALECTS:
        raise RuntimeError(
            f"backfill_body_column: unsupported dialect {dialect!r}; "
            f"supported: {SUPPORTED_DIALECTS}")
    return dialect


def ledger_ensure(engine: Engine) -> None:
    """Create the migration ledger if absent. Idempotent; no column churn."""
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} ("
            " name VARCHAR(128) PRIMARY KEY,"
            " applied_at TIMESTAMP NOT NULL)"
        ))


def _ledger_done(conn, name: str) -> bool:
    """True if a named migration is recorded, using an existing connection."""
    return conn.execute(
        text(f"SELECT 1 FROM {LEDGER_TABLE} WHERE name = :n"),
        {"n": name}).first() is not None


def ledger_has(engine: Engine, name: str) -> bool:
    """True if a named migration is recorded as applied."""
    ledger_ensure(engine)
    with engine.connect() as conn:
        return _ledger_done(conn, name)


def _ledger_insert(conn, engine: Engine, name: str) -> None:
    """Record a migration, atomically, per dialect (finding 3).

    PostgreSQL races on the primary key, so it needs ON CONFLICT DO NOTHING.
    SQLite serialises writers and uses INSERT OR IGNORE.  Any other dialect is
    refused rather than silently guessed at.
    """
    dialect = engine.dialect.name
    if dialect == "postgresql":
        conn.execute(text(
            f"INSERT INTO {LEDGER_TABLE} (name, applied_at) "
            "VALUES (:n, CURRENT_TIMESTAMP) ON CONFLICT (name) DO NOTHING"),
            {"n": name})
    elif dialect == "sqlite":
        conn.execute(text(
            f"INSERT OR IGNORE INTO {LEDGER_TABLE} (name, applied_at) "
            "VALUES (:n, CURRENT_TIMESTAMP)"), {"n": name})
    else:
        raise RuntimeError(
            f"_ledger_insert: unsupported dialect {dialect!r}")


def ledger_mark(engine: Engine, name: str) -> None:
    """Record a completed migration. Idempotent."""
    ledger_ensure(engine)
    with engine.begin() as conn:
        _ledger_insert(conn, engine, name)


def _acquire_lock(engine: Engine):
    """Serialise concurrent startup. Returns a token for _release_lock.

    PostgreSQL takes a session-level advisory lock so two processes cannot
    backfill and mark simultaneously.  SQLite serialises writers at the database
    level and its `engine.begin()` transaction takes the write lock, so there is
    no separate primitive to acquire; this is stated explicitly rather than
    left implicit.

    If acquisition itself fails the connection is closed before re-raising, so a
    failed lock cannot leak a pooled connection (Brief 032 re-review finding 2).
    """
    if engine.dialect.name == "postgresql":
        conn = engine.connect()
        try:
            conn.execute(text("SELECT pg_advisory_lock(:k)"),
                         {"k": BODY_BACKFILL_LOCK_ID})
        except Exception:
            conn.close()
            raise
        return conn
    return None


def _release_lock(engine: Engine, token) -> None:
    if token is None:
        return
    try:
        token.execute(text("SELECT pg_advisory_unlock(:k)"),
                      {"k": BODY_BACKFILL_LOCK_ID})
    finally:
        token.close()


def schema_readiness(inspector) -> tuple[dict[str, set[str]], list[str]]:
    """Return (columns_by_present_table, present_tables_missing_body).

    Absent tables are not an error — `init_db()` may still be creating them —
    but they are also never marked complete, so their backfill still happens
    when they appear.
    """
    names = set(inspector.get_table_names())
    columns: dict[str, set[str]] = {}
    incomplete: list[str] = []
    for table in BACKFILL_TABLES:
        if table not in names:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        columns[table] = cols
        if "body" not in cols:
            incomplete.append(table)
    return columns, incomplete


def pending_backfill_tables(engine: Engine) -> list[str]:
    """Tables that exist, carry `body`, and are not yet marked complete."""
    _require_supported_dialect(engine)
    ledger_ensure(engine)
    inspector = sa_inspect(engine)
    columns, incomplete = schema_readiness(inspector)
    if incomplete:
        raise SchemaNotReadyError(
            f"expected table(s) missing 'body': {incomplete}")
    with engine.connect() as conn:
        return [t for t in BACKFILL_TABLES
                if t in columns and not _ledger_done(conn, ledger_key(t))]


def backfill_body_column(engine: Engine) -> None:
    """Populate `body` for legacy rows. Idempotent and DDL-free once complete.

    Semantics (Brief 032 review finding 1):
      - a meeting with a missing/empty body becomes 'pz' when
        meeting_type = 'Planning & Zoning', otherwise 'bos' — including when
        meeting_type IS NULL;
      - a child inherits its meeting's body via NULLIF(parent.body, '') so an
        empty parent value becomes NULL and then falls back to 'bos';
      - a non-empty body is never overwritten.

    Completion is recorded PER TABLE, and only after a postcondition asserted
    **inside the same transaction** proves no expected table still has a
    NULL/empty body.  A failure therefore rolls back both data and ledger and
    stays retryable.
    """
    _require_supported_dialect(engine)

    # Acquire the lock BEFORE ledger_ensure().  Its CREATE TABLE IF NOT EXISTS
    # must not race between two first-startup processes (Brief 032 re-review
    # finding 1).  The same lock is held through schema readiness, the backfill
    # DML, the postcondition, and the ledger writes; it is released in finally.
    lock = _acquire_lock(engine)
    try:
        ledger_ensure(engine)

        inspector = sa_inspect(engine)
        columns, incomplete = schema_readiness(inspector)
        if incomplete:
            raise SchemaNotReadyError(
                "refusing: expected table(s) present but missing 'body': "
                f"{incomplete}; schema is not ready for the body backfill")

        with engine.connect() as conn:
            pending = [t for t in BACKFILL_TABLES
                       if t in columns and not _ledger_done(conn, ledger_key(t))]
        if not pending:
            return  # nothing outstanding: no DDL, no DML

        has_meetings = "meetings" in columns

        with engine.begin() as conn:
            if "meetings" in pending:
                conn.execute(text(
                    "UPDATE meetings SET body = "
                    "CASE WHEN meeting_type = 'Planning & Zoning' "
                    "     THEN 'pz' ELSE 'bos' END "
                    "WHERE body IS NULL OR body = ''"))

            for table in pending:
                if table == "meetings":
                    continue
                if has_meetings and "meeting_id" in columns[table]:
                    conn.execute(text(
                        f"UPDATE {table} SET body = COALESCE(NULLIF(("
                        f"  SELECT m.body FROM meetings m"
                        f"  WHERE m.meeting_id = {table}.meeting_id LIMIT 1"
                        f"), ''), 'bos') WHERE body IS NULL OR body = ''"))
                else:
                    conn.execute(text(
                        f"UPDATE {table} SET body = 'bos' "
                        "WHERE body IS NULL OR body = ''"))

            # POSTCONDITION, inside the transaction: nothing may be left
            # unresolved before any completion is recorded (finding 1).
            unresolved = {}
            for table in pending:
                n = conn.execute(text(
                    f"SELECT COUNT(*) FROM {table} "
                    "WHERE body IS NULL OR body = ''")).scalar() or 0
                if n:
                    unresolved[table] = int(n)
            if unresolved:
                raise RuntimeError(
                    "refusing: body backfill left unresolved rows: "
                    f"{unresolved}")

            for table in pending:
                _ledger_insert(conn, engine, ledger_key(table))
    finally:
        _release_lock(engine, lock)


def _migrate_meeting_members_table() -> None:
    """Migrate meeting_supervisors → meeting_members if the old table exists.

    Renames the table and the supervisor_id column.  Drops and recreates
    the unique constraint so the constraint name matches the new table.
    """
    from sqlalchemy import inspect as sa_inspect

    engine = get_engine()
    inspector = sa_inspect(engine)
    tables = inspector.get_table_names()

    if "meeting_supervisors" not in tables:
        return  # Already migrated or never existed

    if "meeting_members" in tables:
        # Both exist — migrate data from the old table, then drop it.
        # This happens when Base.metadata.create_all() created an empty
        # meeting_members BEFORE this migration ran.
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO meeting_members "
                "(body, meeting_id, meeting_db_id, member_id, role, present, created_at, updated_at) "
                "SELECT body, meeting_id, meeting_db_id, supervisor_id, role, present, created_at, updated_at "
                "FROM meeting_supervisors "
                "ON CONFLICT (body, meeting_id, member_id) DO NOTHING"
            ))
            conn.execute(text("DROP TABLE IF EXISTS meeting_supervisors CASCADE"))
            conn.commit()
        return

    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE meeting_supervisors RENAME TO meeting_members"))
        conn.execute(text("ALTER TABLE meeting_members RENAME COLUMN supervisor_id TO member_id"))
        # Drop and recreate the unique constraint with the new name
        conn.execute(text("ALTER TABLE meeting_members DROP CONSTRAINT IF EXISTS uq_meeting_supervisor"))
        try:
            conn.execute(text(
                "ALTER TABLE meeting_members "
                "ADD CONSTRAINT uq_meeting_member UNIQUE (body, meeting_id, member_id)"
            ))
        except Exception:
            pass  # May fail if SQLite doesn't support constraint renaming mid-flight
        conn.commit()


def _migrate_table(table_name: str) -> None:
    """Create a table via raw SQL if the model doesn't already exist."""
    from sqlalchemy import inspect as sa_inspect

    engine = get_engine()
    inspector = sa_inspect(engine)
    if table_name not in inspector.get_table_names():
        Base.metadata.create_all(bind=get_engine())

def _migrate_col(engine: Engine, table: str, col: str, col_def: str) -> None:
    """Add a column to an existing table if it doesn't exist yet."""
    inspector = sa_inspect(engine)
    if table not in inspector.get_table_names():
        return  # Table doesn't exist yet, will be created when needed
    existing_cols = {c["name"] for c in inspector.get_columns(table)}
    if col not in existing_cols:
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(f'ALTER TABLE {table} ADD COLUMN {col} {col_def}')
                )
                conn.commit()
        except Exception:
            pass  # Race: parallel worker may have added it first

def _ensure_index(engine: Engine, table: str, index_name: str, column_expr: str) -> None:
    """Create an index if it doesn't already exist."""
    inspector = sa_inspect(engine)
    if table not in inspector.get_table_names():
        return  # Table doesn't exist yet
    existing = {ix["name"] for ix in inspector.get_indexes(table)}
    if index_name not in existing:
        with engine.connect() as conn:
            conn.execute(
                text(f'CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column_expr})')
            )
            conn.commit()

def _create_agenda_items_fk_trigger(engine: Engine) -> None:
    """Create a trigger to prevent agenda_items inserts without a matching meeting.

    This ensures (body, meeting_id) references an existing meeting row at write time,
    preventing the cross-contamination bug where items were stored with body/meeting_id
    combos that didn't match any meeting.
    """
    with engine.connect() as conn:
        # Drop existing trigger if any (idempotent)
        conn.execute(text("DROP TRIGGER IF EXISTS trg_agenda_items_check_meeting"))
        conn.execute(text("""
            CREATE TRIGGER trg_agenda_items_check_meeting
            BEFORE INSERT ON agenda_items
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'agenda_items (body, meeting_id) must reference an existing meeting')
                WHERE NOT EXISTS (
                    SELECT 1 FROM meetings
                    WHERE meetings.body = NEW.body
                      AND meetings.meeting_id = NEW.meeting_id
                );
            END
        """))
        conn.commit()


def init_poliscopic_models(engine: Optional[Engine] = None) -> None:
    """Create all poliscopic tables that may not yet exist (jurisdictions, public_bodies, etc.)."""
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine, checkfirst=True)

    # Create FTS5 full-text search index on supporting document text
    _init_docs_fts(engine)

def _init_docs_fts(engine: Engine) -> None:
    """Create the FTS5 full-text search index for supporting_document text if missing."""
    if engine.dialect.name != "sqlite":
        return

    with engine.connect() as conn:
        # Check if FTS table already exists
        result = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='docs_fts'")
        )
        if result.fetchone():
            return  # Already exists

        log.info("Creating docs_fts FTS5 full-text search index...")
        conn.execute(text("""
            CREATE VIRTUAL TABLE docs_fts USING fts5(
                title,
                body
            )
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS docs_fts_after_update AFTER UPDATE OF text_content ON supporting_documents
            WHEN new.text_content IS NOT NULL AND new.text_content != ''
            BEGIN
                DELETE FROM docs_fts WHERE rowid = new.id;
                INSERT INTO docs_fts(rowid, title, body)
                VALUES (new.id, new.document_title, new.text_content);
            END
        """))

        # Populate from existing docs that have text
        conn.execute(text("""
            INSERT INTO docs_fts(rowid, title, body)
            SELECT id, document_title, text_content
            FROM supporting_documents
            WHERE text_content IS NOT NULL AND text_content != ''
        """))
        conn.commit()
        count = conn.execute(text("SELECT COUNT(*) FROM docs_fts")).scalar()
        log.info(f"docs_fts indexed: {count} documents")


def _migrate_existing_tables(engine: Optional[Engine] = None) -> None:
    """Add columns to existing tables that were introduced after initial creation.

    SQLite's CREATE TABLE IF NOT EXISTS won't ALTER existing tables, so
    newly-added columns on tables that already exist need explicit ALTER TABLE.
    This function uses PRAGMA table_info to check before adding.
    """
    if engine is None:
        engine = get_engine()

    # Skip migration for non-SQLite databases (PostgreSQL handles schema via ORM)
    if engine.dialect.name != "sqlite":
        return

    migrations = [
        ("public_body_members", "jurisdiction_id", "INTEGER DEFAULT NULL"),
        ("public_body_members", "public_body_id", "INTEGER DEFAULT NULL"),
        ("permits", "jurisdiction", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "application_date", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "height_stories", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "native_type", "TEXT DEFAULT NULL"),
        ("permits", "native_category", "TEXT DEFAULT NULL"),
        ("permits", "normalized_category", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "work_type", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "applied_date", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "completed_date", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "certificate_of_occupancy_date", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "units", "VARCHAR(16) DEFAULT NULL"),
        ("permits", "project_name", "TEXT DEFAULT NULL"),
        ("permits", "fee", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "latitude", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "longitude", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "raw_permit_type", "TEXT DEFAULT NULL"),
        ("permits", "raw_permit_type_description", "TEXT DEFAULT NULL"),
        ("permits", "raw_permit_class", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "zone", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "source_system", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "source_record_id", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "contractor_license", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "struct_class", "VARCHAR(8) DEFAULT NULL"),
    ]

    with engine.connect() as conn:
        for table, column, col_type in migrations:
            # Check if table exists
            result = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
                {"t": table},
            )
            if not result.fetchone():
                continue  # table doesn't exist yet — create_all will handle it

            # Check if column exists
            result = conn.execute(
                text(f"PRAGMA table_info('{table}')"),
            )
            existing_cols = {row[1] for row in result.fetchall()}
            if column not in existing_cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
        conn.commit()

def _migrate_supervisors_to_public_body_members() -> None:
    """DEPRECATED — membership data now lives in BodyMembership.
    Kept as a true no-op."""
    pass

def seed_default_jurisdictions() -> None:
    """Populate the Maricopa County jurisdiction and its public bodies if empty."""
    # Ensure tables exist (both new ones and columns added to existing ones)
    _migrate_existing_tables()
    init_poliscopic_models()

    # Migrate legacy Supervisor data into public_body_members (idempotent)
    _migrate_supervisors_to_public_body_members()



    session = get_session()
    try:
        # ── Maricopa County (jurisdiction_id=1) ──
        mc = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "maricopa-county")
        ).scalar_one_or_none()
        if mc is None:
            mc = Jurisdiction(name="Maricopa County", slug="maricopa-county", state="AZ")
            session.add(mc)
            session.flush()

        maricopa_bodies = [
            PublicBody(jurisdiction_id=mc.id, name="Board of Supervisors", slug="board-of-supervisors", body_code="bos", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Planning & Zoning Commission", slug="planning-zoning-commission", body_code="pz", body_type="Commission"),
            PublicBody(jurisdiction_id=mc.id, name="Board of Adjustment", slug="board-of-adjustment", body_code="adj", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Board of Health", slug="board-of-health", body_code="health", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Drainage Review Board", slug="drainage-review-board", body_code="drain", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Transportation Advisory Board", slug="transportation-advisory-board", body_code="tab", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Industrial Development Authority", slug="industrial-development-authority", body_code="ida", body_type="Authority"),
        ]
        for pb in maricopa_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == mc.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)
        session.flush()

        # ── City of Tempe (jurisdiction_id=2) ──
        tempe = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "tempe")
        ).scalar_one_or_none()
        if tempe is None:
            tempe = Jurisdiction(name="City of Tempe", slug="tempe", state="AZ")
            session.add(tempe)
            session.flush()

        # OnBase Agenda Online meeting type IDs are noted in comments
        # Actual body names, not session types (e.g. "Executive Session" is a type, not a body)
        tempe_bodies = [
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe City Council",
                slug="tempe-city-council",
                body_code="tempe-cc",
                body_type="Council",
                website_url="https://www.tempe.gov/government/mayor-and-city-council",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Development Review Commission",
                slug="tempe-development-review-commission",
                body_code="tempe-drc",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Board of Adjustment",
                slug="tempe-board-of-adjustment",
                body_code="tempe-boa",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Historic Preservation Commission",
                slug="tempe-historic-preservation-commission",
                body_code="tempe-hpc",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Housing Authority",
                slug="tempe-housing-authority",
                body_code="tempe-ha",
                body_type="Authority",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Rio Salado Community Facilities District Board",
                slug="tempe-rio-salado-cfd",
                body_code="tempe-rio",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Risk Management Trust Board",
                slug="tempe-risk-management-trust",
                body_code="tempe-rmt",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Joint Review Committee",
                slug="tempe-joint-review-committee",
                body_code="tempe-jrc",
                body_type="Committee",
            ),
        ]
        for pb in tempe_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == tempe.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)
        session.flush()

        # ── City of Chandler (jurisdiction_id=3) ──
        chandler = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "chandler")
        ).scalar_one_or_none()
        if chandler is None:
            chandler = Jurisdiction(name="City of Chandler", slug="chandler", state="AZ")
            session.add(chandler)
            session.flush()

        chandler_bodies = [
            PublicBody(
                jurisdiction_id=chandler.id,
                name="Chandler City Council",
                slug="chandler-city-council",
                body_code="chandler-cc",
                body_type="Council",
            ),
            PublicBody(
                jurisdiction_id=chandler.id,
                name="Chandler Planning & Zoning Commission",
                slug="chandler-planning-zoning-commission",
                body_code="chandler-pz",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=chandler.id,
                name="Chandler Board of Adjustment",
                slug="chandler-board-of-adjustment",
                body_code="chandler-boa",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=chandler.id,
                name="Chandler Development Review Commission",
                slug="chandler-development-review-commission",
                body_code="chandler-drc",
                body_type="Commission",
            ),
        ]
        for pb in chandler_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == chandler.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)
        session.flush()

        # ── City of Phoenix (jurisdiction_id=4) ──
        phoenix = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "phoenix")
        ).scalar_one_or_none()
        if phoenix is None:
            phoenix = Jurisdiction(name="City of Phoenix", slug="phoenix", state="AZ")
            session.add(phoenix)
            session.flush()

        phoenix_bodies = [
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix City Council",
                slug="phoenix-city-council",
                body_code="phoenix-cc",
                body_type="Council",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Planning Commission",
                slug="phoenix-planning-commission",
                body_code="phoenix-pc",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Board of Adjustment",
                slug="phoenix-board-of-adjustment",
                body_code="phoenix-boa",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Village Planning Committees",
                slug="phoenix-village-planning",
                body_code="phoenix-village-planning",
                body_type="Committee",
            ),
        ]
        for pb in phoenix_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == phoenix.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # New Phoenix AEM-discovered bodies
        phoenix_aem_bodies = [
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Historic Preservation Commission",
                slug="phoenix-historic-preservation",
                body_code="phoenix-historic-preservation",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Zoning Adjustment",
                slug="phoenix-zoning-adjustment",
                body_code="phoenix-zoning-adjustment",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Human Services Commission",
                slug="phoenix-human-services",
                body_code="phoenix-human-services",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Human Relations Commission",
                slug="phoenix-human-relations",
                body_code="phoenix-human-relations",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Environmental Quality & Sustainability Commission",
                slug="phoenix-environmental-quality",
                body_code="phoenix-environmental-quality",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Mayor's Commission on Disability Issues",
                slug="phoenix-disability-issues",
                body_code="phoenix-disability-issues",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Women's Commission",
                slug="phoenix-womens-commission",
                body_code="phoenix-womens-commission",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Heritage Commission",
                slug="phoenix-heritage-commission",
                body_code="phoenix-heritage-commission",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix License Appeal Board",
                slug="phoenix-license-appeal",
                body_code="phoenix-license-appeal",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Fire Pension Board",
                slug="phoenix-fire-pension",
                body_code="phoenix-fire-pension",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Police Pension Board",
                slug="phoenix-police-pension",
                body_code="phoenix-police-pension",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix City of Phoenix Employees' Retirement System Board",
                slug="phoenix-copers-board",
                body_code="phoenix-copers-board",
                body_type="Board",
            ),
        ]
        for pb in phoenix_aem_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == phoenix.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── City of Mesa (jurisdiction_id=5) ──
        mesa = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "mesa")
        ).scalar_one_or_none()
        if mesa is None:
            mesa = Jurisdiction(name="City of Mesa", slug="mesa", state="AZ")
            session.add(mesa)
            session.flush()

        mesa_bodies = [
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa City Council",
                slug="mesa-city-council",
                body_code="mesa-city-council",
                body_type="Council",
                website_url="https://www.mesaaz.gov/Government/City-Council-Meetings",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Planning & Zoning Board",
                slug="mesa-planning-zoning",
                body_code="mesa-pz",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Design Review Board",
                slug="mesa-design-review-board",
                body_code="mesa-design-review-board",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Board of Adjustment",
                slug="mesa-board-of-adjustment",
                body_code="mesa-board-of-adjustment",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Historic Preservation Board",
                slug="mesa-historic-preservation-board",
                body_code="mesa-historic-preservation-board",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Cadence Community Facilities District Board",
                slug="mesa-cadence-cfd",
                body_code="mesa-cadence",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Eastmark Community Facilities District No. 1 Board",
                slug="mesa-eastmark-cfd-1",
                body_code="mesa-eastmark1",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Eastmark Community Facilities District No. 2 Board",
                slug="mesa-eastmark-cfd-2",
                body_code="mesa-eastmark2",
                body_type="Board",
            ),
        ]
        for pb in mesa_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == mesa.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── Town of Gilbert (jurisdiction_id=6) ──
        gilbert = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "gilbert")
        ).scalar_one_or_none()
        if gilbert is None:
            gilbert = Jurisdiction(name="Town of Gilbert", slug="gilbert", state="AZ")
            session.add(gilbert)
            session.flush()

        gilbert_bodies = [
            PublicBody(
                jurisdiction_id=gilbert.id,
                name="Gilbert Town Council",
                slug="gilbert-town-council",
                body_code="gilbert-tc",
                body_type="Council",
                website_url="https://www.gilbertaz.gov/departments/town-hall/mayor-town-council",
            ),
        ]
        for pb in gilbert_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == gilbert.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── City of Scottsdale (jurisdiction_id=7) ──
        scottsdale = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "scottsdale")
        ).scalar_one_or_none()
        if scottsdale is None:
            scottsdale = Jurisdiction(name="City of Scottsdale", slug="scottsdale", state="AZ")
            session.add(scottsdale)
            session.flush()

        scottsdale_bodies = [
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale City Council",
                slug="scottsdale-city-council",
                body_code="scottsdale-cc",
                body_type="Council",
                website_url="https://www.scottsdaleaz.gov/council/meeting-information",
            ),
        ]
        for pb in scottsdale_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == scottsdale.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── Scottsdale boards (housing/construction related) ──
        scottsdale_boards = [
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Planning Commission",
                slug="scottsdale-planning-commission",
                body_code="scottsdale-pc",
                body_type="Commission",
                website_url="https://www.scottsdaleaz.gov/boards/planning-commission",
            ),
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Board of Adjustment",
                slug="scottsdale-board-of-adjustment",
                body_code="scottsdale-boa",
                body_type="Board",
                website_url="https://www.scottsdaleaz.gov/boards/board-of-adjustment",
            ),
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Development Review Board",
                slug="scottsdale-development-review-board",
                body_code="scottsdale-drb",
                body_type="Board",
                website_url="https://www.scottsdaleaz.gov/boards/development-review-board",
            ),
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Historic Preservation Commission",
                slug="scottsdale-historic-preservation-commission",
                body_code="scottsdale-hpc",
                body_type="Commission",
                website_url="https://www.scottsdaleaz.gov/boards/historic-preservation-commission",
            ),
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Building Advisory Board of Appeals",
                slug="scottsdale-building-advisory-board-of-appeals",
                body_code="scottsdale-baba",
                body_type="Board",
                website_url="https://www.scottsdaleaz.gov/boards/building-advisory-board-of-appeals",
            ),
        ]
        for pb in scottsdale_boards:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == scottsdale.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── City of Tucson (jurisdiction_id=8) ──
        tucson = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "tucson")
        ).scalar_one_or_none()
        if tucson is None:
            tucson = Jurisdiction(name="City of Tucson", slug="tucson", state="AZ")
            session.add(tucson)
            session.flush()

        tucson_bodies = [
            PublicBody(
                jurisdiction_id=tucson.id,
                name="Tucson City Council",
                slug="tucson-city-council",
                body_code="tucson-cc",
                body_type="Council",
                website_url="https://www.tucsonaz.gov/Government/Mayor-Council-and-City-Manager",
            ),
            PublicBody(
                jurisdiction_id=tucson.id,
                name="Tucson Planning Commission",
                slug="tucson-planning-commission",
                body_code="tucson-pc",
                body_type="Commission",
                website_url="https://www.tucsonaz.gov/Departments/Planning-Development-Services/Public-Meetings-Boards-Committees-Commissions/Planning-Commission",
            ),
            PublicBody(
                jurisdiction_id=tucson.id,
                name="Tucson Public Housing Authority Board of Commissioners",
                slug="tucson-public-housing-authority",
                body_code="tucson-pha",
                body_type="Board",
            ),
        ]
        for pb in tucson_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == tucson.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── Valley Metro (jurisdiction_id=25) ──
        valley = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "valley-metro")
        ).scalar_one_or_none()
        if valley is None:
            valley = Jurisdiction(id=25, name="Valley Metro", slug="valley-metro", state="AZ")
            session.add(valley)
            session.flush()

        valley_bodies = [
            PublicBody(
                jurisdiction_id=valley.id,
                name="Valley Metro Board of Directors",
                slug="valley-metro-board-of-directors",
                body_code="valley-metro-bod",
                body_type="Board",
                website_url="https://www.valleymetro.org/board-directors",
            ),
            PublicBody(
                jurisdiction_id=valley.id,
                name="Valley Metro Procurement & Business Practices Joint Ad Hoc Committee",
                slug="valley-metro-procurement",
                body_code="valley-metro-procurement",
                body_type="Committee",
            ),
            PublicBody(
                jurisdiction_id=valley.id,
                name="Valley Metro Joint Boards Subcommittee",
                slug="valley-metro-joint-boards",
                body_code="valley-metro-joint-boards",
                body_type="Subcommittee",
            ),
            PublicBody(
                jurisdiction_id=valley.id,
                name="Valley Metro Management Committee",
                slug="valley-metro-management-committee",
                body_code="valley-metro-management",
                body_type="Committee",
            ),
            PublicBody(
                jurisdiction_id=valley.id,
                name="Valley Metro Operations Committee",
                slug="valley-metro-operations-committee",
                body_code="valley-metro-operations",
                body_type="Committee",
            ),
            PublicBody(
                jurisdiction_id=valley.id,
                name="Valley Metro Planning Committee",
                slug="valley-metro-planning-committee",
                body_code="valley-metro-planning",
                body_type="Committee",
            ),
            PublicBody(
                jurisdiction_id=valley.id,
                name="Valley Metro Finance Committee",
                slug="valley-metro-finance-committee",
                body_code="valley-metro-finance",
                body_type="Committee",
            ),
        ]
        for pb in valley_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == valley.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        session.commit()
    finally:
        session.close()

def _drop_deprecated_person_columns() -> None:
    """Drop legacy Person columns migrated to BodyMembership, and the
    legacy public_body_members table.

    Deprecated columns dropped:
      active_from, active_to, body, title, district, district_or_seat,
      jurisdiction_id, public_body_id, _person_dates_backfilled,
      _membership_migrated
    """
    engine = get_engine()
    inspector = sa_inspect(engine)
    if "persons" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("persons")}
    to_drop = [
        "active_from", "active_to", "body", "title", "district",
        "district_or_seat", "jurisdiction_id", "public_body_id",
        "_person_dates_backfilled", "_membership_migrated",
    ]
    existing_to_drop = [c for c in to_drop if c in existing_cols]

    if not existing_to_drop:
        pass  # Already clean
    else:
        try:
            with engine.connect() as conn:
                for col in existing_to_drop:
                    conn.execute(text(f"ALTER TABLE persons DROP COLUMN {col}"))
                conn.commit()
            log.info(f"_drop_deprecated_person_columns: dropped {len(existing_to_drop)} column(s)")
        except Exception as e:
            log.warning(f"_drop_deprecated_person_columns: partial failure: {e}")

    # Drop legacy table
    if "public_body_members" in inspector.get_table_names():
        try:
            with engine.connect() as conn:
                conn.execute(text("DROP TABLE IF EXISTS public_body_members"))
                conn.commit()
            log.info("_drop_deprecated_person_columns: dropped public_body_members table")
        except Exception as e:
            log.warning(f"_drop_deprecated_person_columns: drop public_body_members failed: {e}")

def _migrate_membership_model() -> None:
    """One-time migration from flat Person fields to BodyMembership rows.

    Creates BodyMembership rows for every person who has attended meetings
    (recorded in meeting_members) or who has explicit term data on their
    Person record (active_from/active_to).

    Uses `_membership_migrated` marker column on persons to run once.
    """
    engine = get_engine()
    inspector = sa_inspect(engine)
    needed = {"persons", "public_bodies", "meeting_members", "meetings"}
    existing_tables = set(inspector.get_table_names())
    if not needed.issubset(existing_tables):
        return

    # Ensure the new tables exist
    init_poliscopic_models(engine)

    # Check if migration is still relevant — the deprecated columns
    # (active_from, active_to, public_body_id, title, district, etc.)
    # may have already been dropped by _drop_deprecated_person_columns().
    existing_cols = {c["name"] for c in inspector.get_columns("persons")}
    if "active_from" not in existing_cols:
        # Columns already gone; nothing to migrate.
        return

    # Add marker column
    _migrate_col(engine, "persons", "_membership_migrated", "BOOLEAN NOT NULL DEFAULT 0")

    session = get_session()
    try:
        with engine.connect() as conn:
            already = conn.execute(
                text("SELECT COUNT(*) FROM persons WHERE _membership_migrated = 1")
            ).scalar() or 0
            if already > 0:
                return

        # Map body code → public_body_id
        body_map = {}
        for pb in session.execute(select(PublicBody)).scalars().all():
            if pb.body_code:
                body_map[pb.body_code] = pb.id

        created = 0
        already_covered_ids = set()

        # 1) Create memberships from persons with explicit public_body_id + active_from
        rows = session.execute(text("""
            SELECT id, public_body_id, title, district_or_seat, district,
                   active_from, active_to
            FROM persons
            WHERE _membership_migrated = 0
              AND public_body_id IS NOT NULL
              AND active_from IS NOT NULL
        """)).fetchall()
        for row in rows:
            already_covered_ids.add(row.id)
            membership = BodyMembership(
                person_id=row.id,
                public_body_id=row.public_body_id,
                role=row.title or None,
                term_start=_parse_date(row.active_from) or date(2000, 1, 1),
                term_end=_parse_date(row.active_to) if row.active_to else None,
                selection_method="elected",
            )
            session.add(membership)
            created += 1

        # 2) Create memberships from persons who have attended meetings
        meeting_rows = session.execute(text("""
            SELECT p.id, ms.body, p.name,
                   COALESCE(p.active_from, MIN(m.meeting_date)) AS term_start,
                   p.active_to,
                   COUNT(ms.id) AS attendance
            FROM persons p
            JOIN meeting_members ms ON ms.member_id = p.id
            JOIN meetings m ON m.meeting_id = ms.meeting_id AND m.body = ms.body
            WHERE p._membership_migrated = 0
              AND LENGTH(p.name) < 40
              AND p.name NOT GLOB '*[0-9]*'
            GROUP BY p.id, ms.body
            HAVING COUNT(ms.id) >= 3
        """)).fetchall()
        for mr in meeting_rows:
            if mr.id in already_covered_ids:
                continue
            pb_id = body_map.get(mr.body)
            if pb_id is None:
                continue
            if mr.name.lower().startswith("also present") or mr.name.lower().startswith("also "):
                continue
            membership = BodyMembership(
                person_id=mr.id,
                public_body_id=pb_id,
                role=session.execute(
                    text("SELECT title FROM persons WHERE id = :pid"),
                    {"pid": mr.id}
                ).scalar() or None,
                term_start=_parse_date(mr.term_start) or date(2000, 1, 1),
                term_end=_parse_date(mr.active_to) if mr.active_to else None,
                selection_method="elected" if mr.body == "bos" else "appointed",
            )
            session.add(membership)
            created += 1

        # Mark all persons as migrated
        session.execute(
            text("UPDATE persons SET _membership_migrated = 1")
        )
        session.commit()
        if created:
            log.info(f"_migrate_membership_model: created {created} membership(s)")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _normalize_existing_meeting_dates(engine: Optional[Engine] = None) -> None:
    """Backfill: normalize Chandler and Mesa legacy dates to YYYY-MM-DD.

    Chandler stores "September 9, 2025" (month-name format).
    Mesa stores "9/9/2025" (M/D/YYYY format).
    Fix both to ISO 8601.
    """
    from scraper.common.io_utils import _normalize_text_date, normalize_meeting_date
    if engine is None:
        engine = get_engine()
    session = Session(engine)
    try:
        # Fix Chandler: month-name dates
        rows = session.execute(
            text("SELECT id, meeting_date FROM meetings WHERE meeting_date LIKE '% %'")
        ).fetchall()
        fixed = 0
        for row_id, raw in rows:
            normalized = _normalize_text_date(raw)
            if normalized:
                session.execute(
                    text("UPDATE meetings SET meeting_date = :norm WHERE id = :rid"),
                    {"norm": normalized, "rid": row_id}
                )
                fixed += 1
        if fixed:
            session.commit()
            log.info("Normalized %d Chandler-style meeting dates", fixed)

        # Fix Mesa: M/D/YYYY dates
        rows2 = session.execute(
            text("SELECT id, meeting_date FROM meetings WHERE meeting_date LIKE '%/%'")
        ).fetchall()
        fixed2 = 0
        for row_id, raw in rows2:
            normalized = normalize_meeting_date(raw)
            if normalized:
                session.execute(
                    text("UPDATE meetings SET meeting_date = :norm WHERE id = :rid"),
                    {"norm": normalized, "rid": row_id}
                )
                fixed2 += 1
        if fixed2:
            session.commit()
            log.info("Normalized %d Mesa-style meeting dates", fixed2)

        return fixed + fixed2
    finally:
        session.close()


