"""Newsroom models: articles, tags, admin users, FTS search."""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (Column, Integer, String, Text, DateTime, Boolean,
                        ForeignKey, Table, select, func, or_, and_, UniqueConstraint)
from sqlalchemy.orm import relationship

from db.core import get_engine, get_session
from db.models import Base, AgendaItem

log = logging.getLogger(__name__)

# ── Association tables ──

article_tags = Table(
    "article_tags", Base.metadata,
    Column("article_id", Integer, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)

# ── Models ──

class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(128), nullable=False, default="")
    password_hash = Column(String(256), nullable=False)
    role = Column(String(32), nullable=False, default="editor")  # admin, editor
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))

    def get_id(self):
        return str(self.id)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False)
    slug = Column(String(64), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(256), nullable=False)
    slug = Column(String(256), unique=True, nullable=False, index=True)
    summary = Column(Text, nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="draft", index=True)
    # draft, published, archived
    author_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    author = relationship("AdminUser", backref="articles")
    featured_image = Column(String(512), nullable=False, default="")
    image_credit = Column(String(256), nullable=True, default=None)
    is_featured = Column(Boolean, nullable=False, default=False)
    priority = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    published_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)

    tags = relationship("Tag", secondary=article_tags, backref="articles",
                        lazy="selectin")
    sources = relationship("ArticleSource", backref="article",
                           lazy="selectin", cascade="all, delete-orphan")


class ArticleSource(Base):
    __tablename__ = "article_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    body = Column(String(16), nullable=False, default="")
    meeting_id = Column(String(32), nullable=False, default="")
    agenda_item_number = Column(String(32), nullable=False, default="")
    source_url = Column(String(512), nullable=False, default="")
    source_type = Column(String(32), nullable=False, default="agenda")
    item_title = Column(String(512), nullable=False, default="")


# ── Dismissed Suggestions ──

class Notification(Base):
    """In-app admin notification."""
    __tablename__ = "admin_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message = Column(Text, nullable=False)
    url = Column(String(512), nullable=False, default="")
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


class MediaImage(Base):
    """Uploaded media item (image) for use in articles and pages."""
    __tablename__ = "media_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(256), nullable=False)
    original_name = Column(String(256), nullable=False, default="")
    alt_text = Column(String(512), nullable=False, default="")
    tags = Column(String(512), nullable=False, default="")
    file_size = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=False, default=0)
    height = Column(Integer, nullable=False, default=0)
    uploaded_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    uploaded_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))

    @property
    def url(self) -> str:
        return f"/static/uploads/{self.filename}"

    def __repr__(self):
        return f"<MediaImage #{self.id}: {self.filename}>"


class DismissedSuggestion(Base):
    __tablename__ = "dismissed_suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="")
    meeting_id = Column(String(32), nullable=False, default="")
    agenda_item_number = Column(String(32), nullable=False, default="")
    reason = Column(String(32), nullable=False, default="dismissed")
    dismissed_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    dismissed_at = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))


class SkeetDraft(Base):
    """Curated Bluesky post draft linked to an article.

    The editor reviews the auto-generated draft, tweaks the text,
    optionally replaces the link-card image, and approves or skips.
    A separate cron posts approved drafts.
    """
    __tablename__ = "skeet_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    article = relationship("Article", backref="skeet_drafts")
    draft_text = Column(String(300), nullable=False, default="")
    status = Column(String(16), nullable=False, default="draft", index=True)
    # draft, approved, posted, skipped
    image_path = Column(String(512), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    posted_at = Column(DateTime(timezone=True), nullable=True)
    bluesky_post_uri = Column(String(256), nullable=False, default="")


# ── Topic Intelligence Models ──────────────────────────────────────────


class Topic(Base):
    """A curated topic for regional intelligence reports.

    Examples: Housing, Water, Energy, Transportation.
    Each topic has its own weekly report generation cycle.
    """
    __tablename__ = "topics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(64), unique=True, nullable=False, index=True)
    title = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    keywords = Column(Text, nullable=False, default="",
                      comment="Comma-separated agenda item keywords for filtering")
    tags = Column(Text, nullable=False, default="",
                  comment="Comma-separated article tag slugs for this topic")
    metric_defs = Column(Text, nullable=False, default="",
                         comment="JSON defining metric names and extraction rules")
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"<Topic {self.slug}>"


class NewsletterSubscriber(Base):
    """A newsletter subscriber (one row per email address).

    status: pending → active → unsubscribed
    pending rows hold an unconfirmed signup; the confirmation link flips
    the row to active and activates all pending subscription rows.
    """
    __tablename__ = "newsletter_subscribers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(320), unique=True, nullable=False, index=True)
    status = Column(String(16), nullable=False, default="pending", index=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    confirmed_at = Column(DateTime(timezone=True), nullable=True, default=None)
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True, default=None)

    subscriptions = relationship("NewsletterSubscription",
                                 back_populates="subscriber",
                                 cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<NewsletterSubscriber {self.email} {self.status}>"


class NewsletterSubscription(Base):
    """A subscriber's membership in one newsletter topic.

    status: pending (awaiting email confirmation) → active → unsubscribed.
    Uniqueness per (subscriber, topic) so re-subscribing updates in place.
    """
    __tablename__ = "newsletter_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(Integer,
                           ForeignKey("newsletter_subscribers.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    topic = Column(String(64), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="pending", index=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    subscriber = relationship("NewsletterSubscriber", back_populates="subscriptions")

    __table_args__ = (UniqueConstraint("subscriber_id", "topic", name="uq_subscriber_topic"),)

    def __repr__(self) -> str:
        return f"<NewsletterSubscription {self.topic} {self.status}>"


class NewsletterSubmitLog(Base):
    """Rate-limit / abuse audit log for newsletter form submissions.

    Rows are short-lived (pruned after ~48h). Records email + IP + kind so
    per-email and per-IP windows can be enforced without blocking real users.
    """
    __tablename__ = "newsletter_submit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(320), nullable=False, index=True)
    ip = Column(String(64), nullable=True, default=None, index=True)
    kind = Column(String(24), nullable=False, default="subscribe")
    note = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc), index=True)


class TopicWeeklyReport(Base):
    """A single weekly report for a topic.

    Stores the AI-generated executive summary, computed metrics,
    and the rendered HTML body.  Once generated, reports are immutable.
    """
    __tablename__ = "topic_weekly_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic_id = Column(Integer, ForeignKey("topics.id", ondelete="CASCADE"), nullable=False, index=True)
    topic = relationship("Topic", backref="weekly_reports")
    report_date = Column(String(16), nullable=False, index=True)  # "YYYY-MM-DD" (Monday of week)
    summary = Column(Text, nullable=False, default="")
    body_html = Column(Text, nullable=False, default="")
    featured_article_id = Column(Integer, ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)
    activity_by_jurisdiction = Column(Text, nullable=False, default="{}",
                                      comment="JSON: {jurisdiction: {count, approvals, pending}}")
    metric_values = Column(Text, nullable=False, default="{}",
                           comment="JSON: {metric_name: value}")
    article_ids = Column(Text, nullable=False, default="[]",
                         comment="JSON list of article IDs included in this report")
    is_archived = Column(Boolean, nullable=False, default=False)
    generated_at = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"<TopicReport {self.report_date} {self.topic_id}>"


# ── FTS Setup ──

FTS_TABLES = {}


# Module-level flag: only the first gunicorn worker runs the FTS migration.
# Without this guard, all 3 workers attempt ALTER TABLE simultaneously,
# causing ACCESS EXCLUSIVE lock contention that blocks reads and writes.
_init_pg_fts_done = False


def _init_pg_fts(engine) -> None:
    """Set up PostgreSQL full-text search (tsvector columns + GIN indexes)."""
    global _init_pg_fts_done
    if _init_pg_fts_done:
        return
    _init_pg_fts_done = True
    from sqlalchemy import text as _text
    with engine.begin() as c:
        # Agenda items — search across title + text
        c.execute(_text("""
            ALTER TABLE agenda_items ADD COLUMN IF NOT EXISTS search_vector tsvector
        """))
        c.execute(_text("""
            UPDATE agenda_items SET search_vector =
                setweight(to_tsvector('english', COALESCE(agenda_item_title, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(agenda_item_text, '')), 'B')
            WHERE search_vector IS NULL
        """))
        c.execute(_text("""
            CREATE INDEX IF NOT EXISTS ix_agenda_items_search_vector
            ON agenda_items USING GIN(search_vector)
        """))
        # Trigger to keep search_vector current
        c.execute(_text("""
            CREATE OR REPLACE FUNCTION agenda_items_search_update()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector :=
                    setweight(to_tsvector('english', COALESCE(NEW.agenda_item_title, '')), 'A') ||
                    setweight(to_tsvector('english', COALESCE(NEW.agenda_item_text, '')), 'B');
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        c.execute(_text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_agenda_items_search'
                ) THEN
                    CREATE TRIGGER trg_agenda_items_search
                        BEFORE INSERT OR UPDATE OF agenda_item_title, agenda_item_text
                        ON agenda_items
                        FOR EACH ROW EXECUTE FUNCTION agenda_items_search_update();
                END IF;
            END;
            $$
        """))
        # Articles — search across title + summary + body
        c.execute(_text("""
            ALTER TABLE articles ADD COLUMN IF NOT EXISTS search_vector tsvector
        """))
        c.execute(_text("""
            UPDATE articles SET search_vector =
                setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(summary, '')), 'B') ||
                setweight(to_tsvector('english', COALESCE(body, '')), 'C')
            WHERE search_vector IS NULL
        """))
        c.execute(_text("""
            CREATE INDEX IF NOT EXISTS ix_articles_search_vector
            ON articles USING GIN(search_vector)
        """))
        c.execute(_text("""
            CREATE OR REPLACE FUNCTION articles_search_update()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector :=
                    setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
                    setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B') ||
                    setweight(to_tsvector('english', COALESCE(NEW.body, '')), 'C');
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        c.execute(_text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_articles_search'
                ) THEN
                    CREATE TRIGGER trg_articles_search
                        BEFORE INSERT OR UPDATE OF title, summary, body
                        ON articles
                        FOR EACH ROW EXECUTE FUNCTION articles_search_update();
                END IF;
            END;
            $$
        """))
        # Supporting documents — search across title + text_content
        c.execute(_text("""
            ALTER TABLE supporting_documents ADD COLUMN IF NOT EXISTS search_vector tsvector
        """))
        c.execute(_text("""
            UPDATE supporting_documents SET search_vector =
                setweight(to_tsvector('english', COALESCE(document_title, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(text_content, '')), 'B')
            WHERE search_vector IS NULL
            AND text_content IS NOT NULL AND text_content != ''
        """))
        c.execute(_text("""
            CREATE INDEX IF NOT EXISTS ix_supporting_documents_search_vector
            ON supporting_documents USING GIN(search_vector)
        """))
        c.execute(_text("""
            CREATE OR REPLACE FUNCTION supporting_documents_search_update()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector :=
                    setweight(to_tsvector('english', COALESCE(NEW.document_title, '')), 'A') ||
                    setweight(to_tsvector('english', COALESCE(NEW.text_content, '')), 'B');
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        c.execute(_text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_supporting_documents_search'
                ) THEN
                    CREATE TRIGGER trg_supporting_documents_search
                        BEFORE INSERT OR UPDATE OF document_title, text_content
                        ON supporting_documents
                        FOR EACH ROW EXECUTE FUNCTION supporting_documents_search_update();
                END IF;
            END;
            $$
        """))


def init_fts() -> None:
    """Create full-text search indexes.

    SQLite:      FTS5 virtual tables (agenda_items_fts, articles_fts).
    PostgreSQL:  tsvector columns + GIN indexes.
    """
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        _init_pg_fts(engine)
        return
    if engine.dialect.name != "sqlite":
        return
    if engine.dialect.name == "postgresql":
        _init_pg_fts(engine)
        return
    if engine.dialect.name != "sqlite":
        return
    conn = engine.raw_connection()

    # Articles FTS (standalone — no content-sync, tags are in association table)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
            title, summary, body, tags,
            tokenize='porter unicode61'
        )
    """)

    # Agenda items FTS (standalone)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS agenda_items_fts USING fts5(
            agenda_item_title, agenda_item_text,
            tokenize='porter unicode61'
        )
    """)

    conn.commit()
    conn.close()
    log.info("FTS tables initialized")


def rebuild_fts():
    """Rebuild FTS indexes from current data.

    SQLite (FTS5):    INSERT special 'rebuild' command.
    PostgreSQL:       No-op — FTS is handled by tsvector triggers.
    """
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
    conn = engine.raw_connection()
    conn.execute("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO agenda_items_fts(agenda_items_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()


def sync_article_fts(article_id: int):
    """Update FTS index for a single article.

    SQLite (FTS5):    delete + insert.
    PostgreSQL:       No-op — FTS is handled by tsvector triggers.
    """
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
    session = get_session()
    article = session.get(Article, article_id)
    if not article:
        session.close()
        return
    tags_str = " ".join(t.name for t in article.tags)
    conn = engine.raw_connection()
    conn.execute("DELETE FROM articles_fts WHERE rowid = ?", (article_id,))
    conn.execute(
        "INSERT INTO articles_fts(rowid, title, summary, body, tags) VALUES (?, ?, ?, ?, ?)",
        (article_id, article.title, article.summary, article.body, tags_str),
    )
    conn.commit()
    conn.close()
    session.close()


def _build_tsquery(raw_query: str) -> str:
    """Build a tsquery string from a raw user query with advanced syntax.

    Supports:
      Quoted phrases:  "greater phoenix"  →  greater <-> phoenix
      Exclusion:        -tucson            →  !tucson
      Normal terms:     zoning             →  zoning:*
      Mixed:            "greater phoenix" -tucson zoning
                        →  (greater <-> phoenix) & !tucson & zoning:*
    """
    import re
    terms = []
    pos = 0
    raw = raw_query.strip()

    while pos < len(raw):
        # Skip whitespace
        if raw[pos] in (' ', '\t'):
            pos += 1
            continue

        # Quoted phrase
        if raw[pos] == '"':
            end = raw.find('"', pos + 1)
            if end == -1:
                end = len(raw)
            phrase = raw[pos + 1:end].strip()
            if phrase:
                words = phrase.split()
                if len(words) == 1:
                    terms.append(f"{words[0]}:*")
                else:
                    phrase_terms = " <-> ".join(f"{w}:*" for w in words)
                    terms.append(f"({phrase_terms})")
            pos = end + 1
            continue

        # Exclusion (leading -) or normal term
        is_exclude = raw[pos] == '-'
        word_start = pos + 1 if is_exclude else pos

        # Find end of word
        word_end = word_start
        while word_end < len(raw) and raw[word_end] not in (' ', '\t', '"'):
            word_end += 1

        word = raw[word_start:word_end]
        if word:
            if is_exclude:
                terms.append(f"!{word}:*")
            else:
                terms.append(f"{word}:*")
        pos = word_end

    if not terms:
        return ""
    return " & ".join(terms)


def _resolve_body_code(engine, body_slug: str) -> str:
    """Resolve a public_body slug to a body_code for filtering.

    The body filter dropdown sends public_bodies.slug, but meetings store
    body codes like 'bos', 'pz', 'mag-itsc'.  This maps slug → body_code.
    Falls back to the raw slug if no match.
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as c:
            row = c.execute(
                _text("SELECT body_code FROM public_bodies WHERE slug = :slug LIMIT 1"),
                {"slug": body_slug},
            ).fetchone()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    return body_slug


def search_agenda_items(query: str, limit: int = 50, _fetch_extra: bool = True,
                         sort: str = "date", from_date: str = None,
                         to_date: str = None, jurisdiction: str = None,
                         body: str = None) -> tuple[list[dict], bool]:
    """Full-text search across agenda items.

    Accepts optional sort direction ('date' or 'relevance')
    and optional from_date/to_date (YYYY-MM-DD strings) for time-frame filtering,
    plus jurisdiction slug and/or body slug for jurisdictional filtering.

    SQLite:      FTS5 via agenda_items_fts.
    PostgreSQL:  tsquery via search_vector column + ILIKE fallback.

    Returns (results, truncated) where truncated is True when more
    results exist beyond the limit.
    """
    engine = get_engine()
    dialect = engine.dialect.name
    fetch_limit = limit + 1 if _fetch_extra else limit

    # Resolve body slug to body_code so the filter matches meetings.body
    if body:
        body = _resolve_body_code(engine, body)

    if dialect == "postgresql":
        rows = _search_pg_agenda_items(engine, query, fetch_limit, sort,
                                        from_date, to_date, jurisdiction, body)
    else:
        rows = _search_sqlite_agenda_items(engine, query, fetch_limit, sort,
                                            from_date, to_date)

    truncated = len(rows) > limit
    rows = rows[:limit]

    results = []
    for r in rows:
        results.append({
            "id": r[0], "body": r[1], "meeting_id": r[2],
            "agenda_item_number": r[3], "title": r[4], "text": r[5][:300],
            "source_url": r[6], "rank": r[7],
            "highlight": r[8], "meeting_date": r[9],
        })
    return results, truncated


def _search_pg_agenda_items(engine, query: str, fetch_limit: int, sort: str,
                             from_date: str = None, to_date: str = None,
                             jurisdiction: str = None, body: str = None) -> list:
    """PostgreSQL full-text search for agenda items."""
    from sqlalchemy import text as _text
    try:
        ts_query = _build_tsquery(query)
        sql = """
            SELECT a.id, a.body, a.meeting_id, a.agenda_item_number,
                   a.agenda_item_title, a.agenda_item_text,
                   a.source_url,
                   ts_rank(a.search_vector, to_tsquery('english', :q), 32) AS rank,
                   ts_headline(a.agenda_item_text, to_tsquery('english', :q),
                       'StartSel=<mark>, StopSel=</mark>, MaxWords=40, MinWords=15') AS highlight,
                   m.meeting_date
            FROM agenda_items a
            JOIN meetings m ON m.id = a.meeting_db_id
            JOIN jurisdictions j ON j.id = m.jurisdiction_id
            WHERE a.search_vector @@ to_tsquery('english', :q)
        """
        params = {"q": ts_query}
        if from_date:
            sql += " AND m.meeting_date >= :from_date"
            params["from_date"] = from_date
        if to_date:
            sql += " AND m.meeting_date <= :to_date"
            params["to_date"] = to_date
        if jurisdiction:
            sql += " AND j.slug = :jurisdiction"
            params["jurisdiction"] = jurisdiction
        if body:
            sql += " AND m.body = :body"
            params["body"] = body
        if sort == "date":
            sql += " ORDER BY m.meeting_date DESC, rank DESC"
        else:
            sql += " ORDER BY rank DESC"
        sql += " LIMIT :lim"
        params["lim"] = fetch_limit

        with engine.connect() as c:
            rows = c.execute(_text(sql), params).fetchall()
        return rows
    except Exception:
        return _search_pg_agenda_items_ilike(engine, query, fetch_limit, sort,
                                              from_date, to_date, jurisdiction, body)


def _search_pg_agenda_items_ilike(engine, query: str, fetch_limit: int, sort: str,
                                    from_date: str = None, to_date: str = None,
                                    jurisdiction: str = None, body: str = None) -> list:
    """ILIKE fallback for PostgreSQL agenda item search."""
    from sqlalchemy import text as _text
    terms = [w.strip() for w in query.split() if w.strip()]
    if not terms:
        return []

    like_clauses = " OR ".join(f"(a.agenda_item_title ILIKE :t{i} OR a.agenda_item_text ILIKE :t{i})" for i in range(len(terms)))
    sql = f"""
        SELECT a.id, a.body, a.meeting_id, a.agenda_item_number,
               a.agenda_item_title, a.agenda_item_text,
               a.source_url,
               0.0 AS rank,
               m.meeting_date
        FROM agenda_items a
        JOIN meetings m ON m.id = a.meeting_db_id
        JOIN jurisdictions j ON j.id = m.jurisdiction_id
        WHERE ({like_clauses})
    """
    params = {}
    for i, t in enumerate(terms):
        params[f"t{i}"] = f"%{t}%"
    if from_date:
        sql += " AND m.meeting_date >= :from_date"
        params["from_date"] = from_date
    if to_date:
        sql += " AND m.meeting_date <= :to_date"
        params["to_date"] = to_date
    if jurisdiction:
        sql += " AND j.slug = :jurisdiction"
        params["jurisdiction"] = jurisdiction
    if body:
        sql += " AND m.body = :body"
        params["body"] = body
    sql += " ORDER BY m.meeting_date DESC"
    sql += " LIMIT :lim"
    params["lim"] = fetch_limit

    try:
        with engine.connect() as c:
            rows = c.execute(_text(sql), params).fetchall()
        return rows
    except Exception:
        return []


def _search_sqlite_agenda_items(engine, query: str, fetch_limit: int, sort: str,
                                  from_date: str = None, to_date: str = None) -> list:
    """SQLite FTS5 search for agenda items."""
    conn = engine.raw_connection()
    params = []
    where_clauses = ["agenda_items_fts MATCH ?"]
    params.append(query)

    if from_date:
        where_clauses.append("m.meeting_date >= ?")
        params.append(from_date)
    if to_date:
        where_clauses.append("m.meeting_date <= ?")
        params.append(to_date)

    where_sql = " AND ".join(where_clauses)

    if sort == "date":
        order_sql = "ORDER BY m.meeting_date DESC, rank"
    else:
        order_sql = "ORDER BY rank"

    try:
        rows = conn.execute(
            f"""SELECT f.rowid, a.body, a.meeting_id, a.agenda_item_number,
                      a.agenda_item_title, a.agenda_item_text,
                      a.source_url,
                      rank,
                      m.meeting_date
               FROM agenda_items_fts f
               JOIN agenda_items a ON a.id = f.rowid
               JOIN meetings m ON m.id = a.meeting_db_id
               WHERE {where_sql}
               {order_sql}
               LIMIT ?""",
            (*params, fetch_limit),
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return rows


def search_articles(query: str, limit: int = 50, _fetch_extra: bool = True) -> tuple[list[dict], bool]:
    """Full-text search across published articles.

    SQLite:      FTS5 via articles_fts.
    PostgreSQL:  tsquery via search_vector column + ILIKE fallback.

    Returns (results, truncated) where truncated is True when more
    results exist beyond the limit.
    """
    engine = get_engine()
    dialect = engine.dialect.name
    fetch_limit = limit + 1 if _fetch_extra else limit

    if dialect == "postgresql":
        rows = _search_pg_articles(engine, query, fetch_limit)
    else:
        rows = _search_sqlite_articles(engine, query, fetch_limit)

    truncated = len(rows) > limit
    rows = rows[:limit]

    results = []
    for r in rows:
        results.append({
            "id": r[0], "title": r[1], "summary": r[2],
            "status": r[3], "published_at": r[4], "slug": r[5], "rank": r[6],
            "highlight": r[7],
        })
    return results, truncated


def _search_pg_articles(engine, query: str, fetch_limit: int) -> list:
    """PostgreSQL full-text search for articles."""
    from sqlalchemy import text as _text
    try:
        ts_query = _build_tsquery(query)
        with engine.connect() as c:
            rows = c.execute(
                _text("""
                    SELECT a.id, a.title, a.summary, a.status, a.published_at,
                           a.slug,
                           ts_rank(a.search_vector, to_tsquery('english', :q), 32) AS rank,
                           ts_headline(a.body, to_tsquery('english', :q),
                               'StartSel=<mark>, StopSel=</mark>, MaxWords=40, MinWords=15') AS highlight
                    FROM articles a
                    WHERE a.search_vector @@ to_tsquery('english', :q)
                    ORDER BY rank DESC
                    LIMIT :lim
                """),
                {"q": ts_query, "lim": fetch_limit},
            ).fetchall()
        return rows
    except Exception:
        return _search_pg_articles_ilike(engine, query, fetch_limit)


def _search_pg_articles_ilike(engine, query: str, fetch_limit: int) -> list:
    """ILIKE fallback for PostgreSQL article search."""
    from sqlalchemy import text as _text
    terms = [w.strip() for w in query.split() if w.strip()]
    if not terms:
        return []

    like_clauses = " OR ".join(
        f"(a.title ILIKE :t{i} OR a.summary ILIKE :t{i} OR a.body ILIKE :t{i})"
        for i in range(len(terms))
    )
    params = {f"t{i}": f"%{t}%" for i, t in enumerate(terms)}
    params["lim"] = fetch_limit

    try:
        with engine.connect() as c:
            rows = c.execute(
                _text(f"""
                    SELECT a.id, a.title, a.summary, a.status, a.published_at,
                           a.slug, 0.0 AS rank
                    FROM articles a
                    WHERE ({like_clauses})
                    ORDER BY a.published_at DESC NULLS LAST
                    LIMIT :lim
                """),
                params,
            ).fetchall()
        return rows
    except Exception:
        return []


def _search_sqlite_articles(engine, query: str, fetch_limit: int) -> list:
    """SQLite FTS5 search for articles."""
    conn = engine.raw_connection()
    try:
        rows = conn.execute(
            """SELECT a.id, a.title, a.summary, a.status, a.published_at,
                      a.slug, rank
               FROM articles_fts f
               JOIN articles a ON a.id = f.rowid
               WHERE articles_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, fetch_limit),
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return rows


def search_supporting_documents(query: str, limit: int = 50, _fetch_extra: bool = True,
                                  sort: str = "date", from_date: str = None,
                                  to_date: str = None, jurisdiction: str = None,
                                  body: str = None) -> tuple[list[dict], bool]:
    """Full-text search across supporting document title + text_content.

    Accepts optional sort direction ('date' or 'relevance')
    plus jurisdiction slug and/or body slug for filtering.

    SQLite:      FTS5 via docs_fts.
    PostgreSQL:  tsquery via search_vector column + ILIKE fallback.

    Returns (results, truncated).
    """
    engine = get_engine()
    dialect = engine.dialect.name
    fetch_limit = limit + 1 if _fetch_extra else limit

    # Resolve body slug to body_code so the filter matches meetings.body
    if body:
        body = _resolve_body_code(engine, body)

    if dialect == "postgresql":
        rows = _search_pg_supporting_docs(engine, query, fetch_limit, sort,
                                          from_date, to_date, jurisdiction, body)
    else:
        rows = _search_sqlite_supporting_docs(engine, query, fetch_limit, sort)

    truncated = len(rows) > limit
    rows = rows[:limit]

    results = []
    for r in rows:
        results.append({
            "id": r[0], "body": r[1], "meeting_id": r[2],
            "document_title": r[3], "document_type": r[4],
            "document_url": r[5], "text_snippet": (r[6] or "")[:300],
            "agenda_item_id": r[7], "agenda_item_number": r[8],
            "rank": r[9], "highlight": r[10], "meeting_date": r[11],
            "jurisdiction_name": r[12], "jurisdiction_slug": r[13],
            "body_name": r[14],
        })
    return results, truncated


def _search_pg_supporting_docs(engine, query: str, fetch_limit: int, sort: str,
                                from_date: str = None, to_date: str = None,
                                jurisdiction: str = None, body: str = None) -> list:
    """PostgreSQL full-text search for supporting documents."""
    from sqlalchemy import text as _text
    try:
        ts_query = _build_tsquery(query)
        sql = """
            SELECT sd.id, sd.body, sd.meeting_id,
                   sd.document_title, sd.document_type, sd.document_url,
                   sd.text_content, sd.agenda_item_id, sd.agenda_item_number,
                   ts_rank(sd.search_vector, to_tsquery('english', :q), 32) AS rank,
                   ts_headline(sd.text_content, to_tsquery('english', :q),
                       'StartSel=<mark>, StopSel=</mark>, MaxWords=40, MinWords=15') AS highlight,
                   m.meeting_date,
                   j.name, j.slug,
                   pb.name AS body_name
            FROM supporting_documents sd
            JOIN meetings m ON m.id = sd.meeting_db_id
            JOIN jurisdictions j ON j.id = m.jurisdiction_id
            LEFT JOIN public_bodies pb ON pb.body_code = m.body
            WHERE sd.search_vector @@ to_tsquery('english', :q)
        """
        params = {"q": ts_query}
        if from_date:
            sql += " AND m.meeting_date >= :from_date"
            params["from_date"] = from_date
        if to_date:
            sql += " AND m.meeting_date <= :to_date"
            params["to_date"] = to_date
        if jurisdiction:
            sql += " AND j.slug = :jurisdiction"
            params["jurisdiction"] = jurisdiction
        if body:
            sql += " AND m.body = :body"
            params["body"] = body
        if sort == "date":
            sql += " ORDER BY m.meeting_date DESC, rank DESC"
        else:
            sql += " ORDER BY rank DESC"
        sql += " LIMIT :lim"
        params["lim"] = fetch_limit

        with engine.connect() as c:
            rows = c.execute(_text(sql), params).fetchall()
        return rows
    except Exception:
        return _search_pg_supporting_docs_ilike(engine, query, fetch_limit, sort,
                                                from_date, to_date, jurisdiction, body)


def _search_pg_supporting_docs_ilike(engine, query: str, fetch_limit: int, sort: str,
                                      from_date: str = None, to_date: str = None,
                                      jurisdiction: str = None, body: str = None) -> list:
    """ILIKE fallback for PostgreSQL supporting document search."""
    from sqlalchemy import text as _text
    terms = [w.strip() for w in query.split() if w.strip()]
    if not terms:
        return []

    like_clauses = " OR ".join(
        f"(sd.document_title ILIKE :t{i} OR sd.text_content ILIKE :t{i})"
        for i in range(len(terms))
    )
    params = {f"t{i}": f"%{t}%" for i, t in enumerate(terms)}
    params["lim"] = fetch_limit

    where_extra = ""
    if from_date:
        where_extra += " AND m.meeting_date >= :from_date"
        params["from_date"] = from_date
    if to_date:
        where_extra += " AND m.meeting_date <= :to_date"
        params["to_date"] = to_date
    if jurisdiction:
        where_extra += " AND j.slug = :jurisdiction"
        params["jurisdiction"] = jurisdiction
    if body:
        where_extra += " AND m.body = :body"
        params["body"] = body

    order = "m.meeting_date DESC" if sort == "date" else "rank DESC"

    try:
        with engine.connect() as c:
            rows = c.execute(
                _text(f"""
                    SELECT sd.id, sd.body, sd.meeting_id,
                           sd.document_title, sd.document_type, sd.document_url,
                           sd.text_content, sd.agenda_item_id, sd.agenda_item_number,
                           0.0 AS rank,
                           m.meeting_date,
                           j.name, j.slug,
                           pb.name AS body_name
                    FROM supporting_documents sd
                    JOIN meetings m ON m.id = sd.meeting_db_id
                    JOIN jurisdictions j ON j.id = m.jurisdiction_id
                    LEFT JOIN public_bodies pb ON pb.body_code = m.body
                    WHERE ({like_clauses})
                    {where_extra}
                    ORDER BY {order}
                    LIMIT :lim
                """),
                params,
            ).fetchall()
        return rows
    except Exception:
        return []


def _search_sqlite_supporting_docs(engine, query: str, fetch_limit: int, sort: str) -> list:
    """SQLite FTS5 search for supporting documents."""
    conn = engine.raw_connection()
    try:
        rows = conn.execute(
            """SELECT sd.id, sd.body, sd.meeting_id,
                      sd.document_title, sd.document_type, sd.document_url,
                      sd.text_content, sd.agenda_item_id, sd.agenda_item_number,
                      rank,
                      m.meeting_date,
                      j.name, j.slug,
                      pb.name AS body_name
               FROM docs_fts f
               JOIN supporting_documents sd ON sd.id = f.rowid
               JOIN meetings m ON m.id = sd.meeting_db_id
               JOIN jurisdictions j ON j.id = m.jurisdiction_id
               LEFT JOIN public_bodies pb ON pb.body_code = m.body
               WHERE docs_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, fetch_limit),
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return rows


def init_newsroom_db():
    """Create all newsroom tables including topic infrastructure."""
    Base.metadata.create_all(get_engine(), tables=[
        AdminUser.__table__, Tag.__table__, Article.__table__,
        ArticleSource.__table__, article_tags, DismissedSuggestion.__table__,
        Topic.__table__, TopicWeeklyReport.__table__,
        NewsletterSubscriber.__table__, NewsletterSubscription.__table__,
        NewsletterSubmitLog.__table__,
    ])
    init_fts()


def seed_default_tags():
    """Seed the default tag vocabulary."""
    session = get_session()
    defaults = [
        ("housing", "Housing", "Housing developments, affordable housing, residential projects"),
        ("zoning", "Zoning", "Zoning changes, rezonings, land use amendments"),
        ("data-centers", "Data Centers", "Data center developments and related infrastructure"),
        ("enforcement", "Enforcement", "Code enforcement, citations, penalties"),
        ("health", "Health", "Public health issues, health board actions"),
        ("environment", "Environment", "Environmental impact, sustainability, conservation"),
        ("transportation", "Transportation", "Roads, transit, bike/pedestrian infrastructure"),
        ("budget", "Budget", "Budgets, taxes, fees, financial decisions"),
        ("development", "Development", "General development proposals and projects"),
        ("economy", "Economy", "Economic development, incentives, job creation"),
        ("public-safety", "Public Safety", "Police, fire, emergency services"),
        ("education", "Education", "Schools, libraries, educational programs"),
        ("parks", "Parks", "Parks, recreation, open space"),
        ("water", "Water", "Water resources, utilities, infrastructure"),
        ("government", "Government", "Council operations, policy, governance"),
    ]
    existing = {t.name for t in session.execute(select(Tag)).scalars().all()}
    for slug, name, desc in defaults:
        if name not in existing:
            session.add(Tag(name=name, slug=slug, description=desc))
    session.commit()
    session.close()


def seed_default_topics():
    """Seed default topic definitions."""
    session = get_session()
    existing = {t.slug for t in session.execute(select(Topic)).scalars().all()}
    defaults = [
        {
            "slug": "housing",
            "title": "Housing",
            "description": "Housing developments, affordability, ADUs, multifamily projects, and zoning.",
            "keywords": "housing,rezoning,multifamily,adu,affordable,apartment,density,subdivision",
            "tags": "housing,development,zoning",
            "metric_defs": '{"units_proposed": "Units proposed", "units_approved": "Units approved", "adu_proposals": "ADU proposals", "multifamily_projects": "Multifamily projects"}',
            "sort_order": 1,
        },
        {
            "slug": "water",
            "title": "Water",
            "description": "Water supply, conservation, wastewater, well projects, and flood control.",
            "keywords": "water,well,wastewater,conservation,flood,reclaimed,irrigation",
            "tags": "water,environment",
            "metric_defs": '{"well_projects": "Well projects", "wastewater_improvements": "Wastewater improvements", "conservation_programs": "Conservation programs"}',
            "sort_order": 3,
        },
        {
            "slug": "energy",
            "title": "Energy",
            "description": "Solar, battery storage, data centers, transmission, and utility-scale projects.",
            "keywords": "solar,battery,energy,data center,transmission,utility,renewable",
            "tags": "development,environment",
            "metric_defs": '{"solar_projects": "Solar projects", "battery_storage": "Battery storage projects", "data_centers": "Data centers"}',
            "sort_order": 4,
        },
        {
            "slug": "transportation",
            "title": "Transportation",
            "description": "Roads, transit, bicycle infrastructure, sidewalks, and transportation planning.",

            "tags": "transportation",
            "keywords": "roadway,transit,bicycle,bike,sidewalk,transportation,traffic,highway,roundabout,overpass,underpass,pedestrian",
            "metric_defs": '{"road_projects": "Road projects", "transit_projects": "Transit projects", "bike_infrastructure": "Bicycle infrastructure projects"}',
            "sort_order": 2,
        },
    ]
    for d in defaults:
        if d["slug"] not in existing:
            session.add(Topic(**d))
    session.commit()


def search_entities(query: str, limit: int = 50) -> tuple[list[dict], bool]:
    """Search entities by name using ILIKE.

    Returns (results, truncated).
    """
    from sqlalchemy import text as _text
    from db import get_session

    session = get_session()
    try:
        rows = session.execute(
            _text("""
                SELECT e.id, e.name, e.entity_type, e.mention_count,
                       e.last_seen_at
                FROM entities e
                WHERE e.name ILIKE :q
                ORDER BY e.mention_count DESC, e.name
                LIMIT :limit
            """),
            {"q": f"%{query}%", "limit": limit + 1},
        ).fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]
        results = []
        for r in rows:
            results.append({
                "id": r[0],
                "name": r[1],
                "entity_type": r[2],
                "mention_count": r[3] or 0,
                "last_seen_at": str(r[4]) if r[4] else None,
            })
        return results, truncated
    finally:
        session.close()

    session.close()


def seed_default_users():
    """Seed default admin users."""
    from flask_bcrypt import generate_password_hash
    session = get_session()
    existing = {u.username for u in session.execute(select(AdminUser)).scalars().all()}
    users = [
        ("poston", "Poston", "admin", "changeme"),  # placeholder hash, set on first login
        ("editor", "Editor", "editor", "changeme"),
    ]
    for username, display, role, pw in users:
        if username not in existing:
            ph = generate_password_hash(pw).decode("utf-8")
            session.add(AdminUser(
                username=username, display_name=display,
                password_hash=ph, role=role,
            ))
    session.commit()
    session.close()
