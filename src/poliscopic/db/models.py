"""SQLAlchemy ORM model classes for Poliscopic governance data."""

import logging
from datetime import datetime, timezone

from sqlalchemy import (
    ForeignKeyConstraint,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import deferred, DeclarativeBase

# tsvector column type — PostgreSQL native, Text fallback for SQLite
# Managed by database trigger on supporting_documents
class _TSVector(TypeDecorator):
    """Abstraction for tsvector — PG native type, TEXT for SQLite."""
    impl = Text
    cache_ok = True
    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import TSVECTOR
            return dialect.type_descriptor(TSVECTOR())
        return dialect.type_descriptor(Text())

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Person(Base):
    """Generic person/member registry for any public body.

    Memberships (which body they serve on, term dates, roles, seats)
    are stored in ``body_memberships``.  This table holds only identity.
    """
    __tablename__ = "persons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    normalized_name = Column(String(128), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# Backward-compatible alias — code that imported Supervisor still works
Supervisor = Person


class MeetingMember(Base):
    """Per-meeting attendance for members of any public body."""
    __tablename__ = "meeting_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_db_id = Column(Integer, nullable=False, default=0, index=True)
    member_id = Column(Integer, nullable=False, index=True)
    role = Column(String(64), nullable=True, default=None)
    present = Column(Boolean, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("body", "meeting_id", "member_id", name="uq_meeting_member"),
    )


class AgendaItemVote(Base):
    __tablename__ = "agenda_item_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    agenda_item_id = Column(Integer, nullable=False, index=True, unique=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_db_id = Column(Integer, nullable=False, default=0, index=True)
    agenda_item_number = Column(String(32), nullable=False, index=True)
    c_number = Column(String(32), nullable=True, default=None, index=True)
    c_number_base = Column(String(48), nullable=True, default=None, index=True)
    motion_result = Column(String(64), nullable=True, default=None)
    vote_text = Column(Text, nullable=True, default=None)
    conditions = Column(Text, nullable=True, default=None)
    is_split_vote = Column(Boolean, nullable=True, default=None,
                           comment="True if members voted differently (not unanimous)")
    unanimous = Column(Boolean, nullable=True, default=None,
                       comment="True if all voting members voted the same way, excluding recusals/abstentions")
    majority_position = Column(
        String(16), nullable=True, default=None,
        comment="yes|no|tie|unknown — the position taken by the majority of voting members"
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_date = Column(String(16), nullable=False)
    meeting_type = Column(String(64), nullable=False, default="")
    meeting_title = Column(String(256), nullable=False, default="")
    meeting_title_raw = Column(Text, nullable=True, default=None)
    meeting_context = Column(String(128), nullable=True, default=None)
    meeting_body = Column(String(128), nullable=True, default=None)
    display_name = Column(String(256), nullable=True, default=None)
    source_url = Column(String(512), nullable=False, default="")
    sync_status = Column(String(32), nullable=False, default="pending", index=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True, default=None)
    last_attempted_at = Column(DateTime(timezone=True), nullable=True, default=None)
    last_error = Column(Text, nullable=True, default=None)
    retry_count = Column(Integer, nullable=False, default=0)
    item_count_expected = Column(Integer, nullable=True, default=None)
    item_count_actual = Column(Integer, nullable=True, default=None)
    supporting_doc_count = Column(Integer, nullable=False, default=0)
    items_extracted = Column(Boolean, nullable=False, default=False)
    supporting_docs_extracted = Column(Boolean, nullable=False, default=False)
    next_doc_check_at = Column(DateTime, nullable=True, default=None)
    minutes_url = Column(String(512), nullable=True, default=None)
    votes_extracted = Column(Boolean, nullable=False, default=False)
    jurisdiction_id = Column(Integer, nullable=True, default=None, index=True)
    public_body_id = Column(Integer, nullable=True, default=None, index=True)
    source_system = Column(String(64), nullable=True, default=None)
    source_instance_url = Column(String(512), nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("body", "meeting_id", name="uq_meeting_body_id"),
    )


class Case(Base):
    __tablename__ = "cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_number = Column(String(32), nullable=False, unique=True, index=True)
    case_type = Column(String(16), nullable=False, default="")
    normalized_case_number = Column(String(32), nullable=False, index=True)
    description = Column(Text, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PZItemDetail(Base):
    """Structured fields extracted from P&Z agenda PDF items."""
    __tablename__ = "pz_item_details"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    agenda_item_id = Column(Integer, nullable=True, default=None, index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_db_id = Column(Integer, nullable=False, default=0, index=True)
    agenda_item_number = Column(Integer, nullable=False)
    case_number = Column(String(32), nullable=False, default="", index=True)
    district = Column(String(32), nullable=True, default=None)
    project_name = Column(Text, nullable=True, default=None)
    applicant = Column(Text, nullable=True, default=None)
    request = Column(Text, nullable=True, default=None)
    location = Column(Text, nullable=True, default=None)
    recommendation = Column(Text, nullable=True, default=None)
    presented_by = Column(Text, nullable=True, default=None)
    staff_report_url = Column(String(512), nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CaseEvent(Base):
    __tablename__ = "case_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    case_id = Column(Integer, nullable=False, index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_db_id = Column(Integer, nullable=False, default=0, index=True)
    agenda_item_id = Column(Integer, nullable=True, default=None)
    source = Column(String(16), nullable=False, default="")
    event_type = Column(String(32), nullable=False, default="")
    event_date = Column(String(16), nullable=False, default="")
    notes = Column(Text, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class AgendaItem(Base):
    __tablename__ = "agenda_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_db_id = Column(Integer, nullable=False, default=0, index=True)
    agenda_item_number = Column(String(32), nullable=False, default="", index=True)
    agenda_item_id = Column(String(128), nullable=False, unique=True)
    agenda_item_title = Column(Text, nullable=False, default="")
    agenda_item_text = Column(Text, nullable=False, default="")
    agenda_item_url = Column(String(512), nullable=False, default="")
    vote_or_action = Column(String(64), nullable=False, default="")
    source_body = Column(String(64), nullable=False, default="Board of Supervisors")
    source_url = Column(String(512), nullable=False, default="")
    c_number = Column(String(32), nullable=False, default="", index=True)
    c_number_base = Column(String(48), nullable=False, default="", index=True)
    c_number_revision = Column(String(16), nullable=True, default=None, index=True)
    case_number = Column(String(32), nullable=False, default="", index=True)
    item_type = Column(String(16), nullable=False, default="", index=True)
    section_level = Column(Integer, nullable=True, default=None)
    sort_order = Column(Integer, nullable=True, default=None, index=True)
    agenda_category = Column(String(32), nullable=False, default="", index=True)
    jurisdiction_id = Column(Integer, nullable=True, default=None, index=True)
    public_body_id = Column(Integer, nullable=True, default=None, index=True)
    lifecycle_status = Column(String(32), nullable=True, default=None, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class PublicBodyMember(Base):
    """DEPRECATED — do not use. Use body_memberships + persons instead.

    This table has 0 rows in production and is never written to by active
    code paths.  It is kept only for migration compatibility.  All new
    member data goes through ``body_memberships`` and ``persons``.

    Marked abstract so Base.metadata.create_all() stops recreating it
    after _drop_deprecated_person_columns() drops it.
    """
    __abstract__ = True


class MeetingAttendance(Base):
    """Per-meeting attendance records for members of any public body."""
    __tablename__ = "meeting_attendance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_db_id = Column(Integer, nullable=False, default=0, index=True)
    member_id = Column(Integer, nullable=False, index=True)
    attendance_status = Column(
        String(24), nullable=False, default="unknown",
        comment="present|absent|excused|late|left_early|unknown|inferred_absent"
    )
    source_text = Column(Text, nullable=True, default=None)
    inference_method = Column(String(64), nullable=True, default=None,
                              comment="e.g. 'other_members_voted_but_member_did_not'")
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("body", "meeting_id", "member_id", name="uq_meeting_attendance"),
    )


class MemberVote(Base):
    """Individual member vote per agenda item for any public body."""
    __tablename__ = "member_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    agenda_item_vote_id = Column(Integer, nullable=False, index=True)
    member_id = Column(Integer, nullable=False, index=True)
    vote = Column(String(32), nullable=False, default="unknown", index=True)
    raw_vote_text = Column(String(64), nullable=True, default=None)
    is_dissent = Column(Boolean, nullable=True, default=None,
                        comment="True if member voted against the majority")
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("agenda_item_vote_id", "member_id", name="uq_member_vote"),
    )


class ExecutiveSessionParticipant(Base):
    """BOS executive session advisors and attendees."""
    __tablename__ = "executive_session_participants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_db_id = Column(Integer, nullable=False, default=0, index=True)
    person_name = Column(String(128), nullable=False, index=True)
    normalized_name = Column(String(128), nullable=False, index=True)
    role_or_title = Column(String(128), nullable=True, default=None)
    organization = Column(String(128), nullable=True, default=None)
    participation_type = Column(
        String(32), nullable=False, default="unknown",
        comment="advised|attended|presented|legal_counsel|staff|outside_counsel|unknown"
    )
    agenda_item_number = Column(Integer, nullable=True, default=None)
    source_text = Column(Text, nullable=True, default=None)
    source_url = Column(String(512), nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "body", "meeting_id", "normalized_name",
            "agenda_item_number",
            name="uq_exec_session_participant"
        ),
    )


class SupportingDocument(Base):
    __tablename__ = "supporting_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    # Source-system key, not a database identity: writers store strings such
    # as "0" (no document-level key), "bos-20", or
    # "result-phoenix-cc-publicmeeting-...".  The column is varchar(256);
    # declaring Integer here mis-described every value.
    agenda_item_id = Column(String(256), nullable=False, index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    meeting_db_id = Column(Integer, nullable=False, default=0, index=True)
    agenda_item_number = Column(String(32), nullable=False, default="", index=True)
    c_number = Column(String(32), nullable=True, default=None, index=True)
    c_number_base = Column(String(48), nullable=True, default=None, index=True)
    c_number_revision = Column(String(16), nullable=True, default=None)
    document_title = Column(Text, nullable=False, default="")
    document_url = Column(String(1024), nullable=False, default="")
    document_type = Column(String(64), nullable=True, default=None)
    file_name = Column(String(256), nullable=True, default=None)
    file_extension = Column(String(16), nullable=True, default=None)
    local_path = Column(String(512), nullable=True, default=None)
    content_hash = Column(String(64), nullable=True, default=None)
    scraped_at = Column(DateTime(timezone=True), nullable=True, default=None)
    text_content = Column(Text, nullable=True, default=None)
    text_extracted_at = Column(DateTime(timezone=True), nullable=True, default=None)
    text_extraction_method = Column(String(512), nullable=True, default=None)
    extraction_attempts = Column(Integer, nullable=False, default=0)
    extraction_duration_ms = Column(Integer, nullable=True, default=None)
    search_vector = Column(_TSVector, nullable=True, default=None)  # PG tsvector, SQLite text — auto-populated by trigger
    jurisdiction_id = Column(Integer, nullable=True, default=None, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Additive canonical identity: the FK target of agenda_item_id, which stays
    # a source key.  Nullable by design — only deterministic links are set; the
    # Stage 3 acquisition gaps and meeting-level documents remain NULL.
    #
    # STAGED.  The declarative column exists before the database column does, so
    # it must not appear in the SELECT list of an ordinary query: ``deferred``
    # keeps it out of default loads until a caller asks for it explicitly.  The
    # app, the sync column list (derived from the live catalogue, not the model)
    # and every existing query therefore behave exactly as before the migration.
    # Accessing the attribute before the column exists fails loudly rather than
    # returning a stale value, which is the fail-closed direction.
    agenda_item_db_id = deferred(Column(Integer, nullable=True, default=None, index=True))

    __table_args__ = (
        UniqueConstraint("agenda_item_id", "document_url", name="uq_supporting_doc_item_url"),
        ForeignKeyConstraint(
            ["agenda_item_db_id"], ["agenda_items.id"],
            name="supporting_documents_agenda_item_db_id_fkey",
            ondelete="SET NULL", onupdate="CASCADE",
        ),
    )


class Jurisdiction(Base):
    """A government jurisdiction (county, city, town) whose meetings we track."""
    __tablename__ = "jurisdictions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, unique=True, index=True)
    slug = Column(String(64), nullable=False, unique=True, index=True)
    state = Column(String(2), nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class PublicBody(Base):
    """A public body (board, commission, committee) within a jurisdiction."""
    __tablename__ = "public_bodies"
    id = Column(Integer, primary_key=True, autoincrement=True)
    jurisdiction_id = Column(Integer, nullable=False, index=True)
    name = Column(String(256), nullable=False)
    slug = Column(String(64), nullable=False, index=True)
    body_code = Column(String(16), nullable=True, default=None, index=True)
    body_type = Column(String(64), nullable=True, default=None)
    description = Column(Text, nullable=True, default=None)
    website_url = Column(String(512), nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("jurisdiction_id", "slug", name="uq_public_body_slug"),)

    @property
    def display_name(self) -> str:
        """Short display name with jurisdiction prefix stripped.

        Since jurisdiction is shown in a separate column, this avoids
        redundancy like "Avondale City Council" when "City Council" suffices.
        Computed dynamically — never goes stale if the jurisdiction is renamed.
        """
        if not self.name:
            return self.body_code or ""
        # Strip known jurisdiction name prefixes
        for prefix in ["Maricopa County ", "City of ", "Town of "]:
            if self.name.startswith(prefix):
                # Strip the city name after the prefix (e.g. "City of Avondale ")
                # The full name is like "City of Avondale City Council"
                # Actually most bodies have names like "Avondale City Council"
                # Try: strip everything up to and including the first space after the prefix
                pass
        # Simpler approach: the name is typically "{Jurisdiction} {Body}"
        # Jurisdiction names are single words (except "Maricopa County", "Paradise Valley", "Queen Creek", "El Mirage")
        multi_word_jurs = {"Maricopa County", "Paradise Valley", "Queen Creek", "El Mirage"}
        for jur_name in sorted(multi_word_jurs, key=len, reverse=True):
            prefix = jur_name + " "
            if self.name.startswith(prefix):
                return self.name[len(prefix):]
        # Single-word jurisdictions: strip first word
        parts = self.name.split(" ", 1)
        if len(parts) > 1:
            return parts[1]
        return self.name


class BodySeat(Base):
    """A named seat or district within a public body."""
    __tablename__ = "body_seats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_body_id = Column(Integer, nullable=False, index=True)
    seat_name = Column(String(128), nullable=True, default=None)
    district_number = Column(String(16), nullable=True, default=None)
    seat_type = Column(String(32), nullable=True, default=None, comment="elected|appointed|ex-officio")
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    __table_args__ = (
        UniqueConstraint("public_body_id", "seat_name", name="uq_body_seat_name"),
    )


class BodyMembership(Base):
    """A person's term of service on a public body.
    Membership validity for a given meeting date ``md``::
        term_start <= md AND (term_end IS NULL OR term_end >= md)
    """
    __tablename__ = "body_memberships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    person_id = Column(Integer, nullable=False, index=True)
    public_body_id = Column(Integer, nullable=False, index=True)
    body_seat_id = Column(Integer, nullable=True, default=None, index=True)
    role = Column(String(64), nullable=True, default=None,
                  comment="Supervisor, Councilmember, Mayor, Chair, etc.")
    term_start = Column(Date, nullable=False)
    term_end = Column(Date, nullable=True, default=None)
    selection_method = Column(String(32), nullable=True, default=None,
                              comment="elected|appointed|ex-officio")
    source_url = Column(String(512), nullable=True, default=None)
    notes = Column(Text, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_membership_body_term", "public_body_id", "term_start", "term_end"),
        Index("ix_membership_person_body", "person_id", "public_body_id"),
        Index("ix_membership_body_term_end", "public_body_id", "term_end"),
    )


# ── Entity Layer ──
# These models transform Poliscopic from a meeting archive into a
# relationship graph of the people, organizations, cases, and projects
# that appear in public meetings.


class Entity(Base):
    """A person, organization, case, or project appearing in public meetings."""
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String(32), nullable=False, index=True)
    name = Column(Text, nullable=False)
    normalized_name = Column(Text, nullable=False, unique=True)
    jurisdiction_id = Column(Integer, nullable=True)
    metadata_ = Column("metadata", Text, nullable=True)
    is_government = Column(Boolean, nullable=False, default=False)
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    mention_count = Column(Integer, nullable=False, default=0)
    # Entity resolution fields
    canonical_entity_id = Column(Integer, nullable=True, index=True)
    resolution_block_key = Column(String(128), nullable=True, index=True)
    resolution_status = Column(String(32), nullable=False, default="unresolved")
    resolution_confidence = Column(Float, nullable=True)
    resolution_method = Column(String(64), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                         default=lambda: datetime.now(timezone.utc),
                         onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_entities_type", "entity_type"),
        Index("ix_entities_normalized", "normalized_name"),
    )


class EntityMention(Base):
    """Links an entity to the agenda item, document, or article where it appeared."""
    __tablename__ = "entity_mentions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(Integer, nullable=False)
    source_type = Column(String(32), nullable=False)
    source_id = Column(Integer, nullable=False)
    mention_text = Column(Text, nullable=True)
    context_snippet = Column(Text, nullable=True)
    confidence = Column(Integer, nullable=False, default=0)
    extracted_by = Column(String(16), nullable=False, default="regex")
    role_in_context = Column(String(64), nullable=True)
    is_withdrawn = Column(Boolean, nullable=False, default=False)
    flag_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_mentions_entity", "entity_id"),
        Index("ix_mentions_source", "source_type", "source_id"),
        Index("ix_mentions_entity_source", "entity_id", "source_type"),
    )


class EntityRelationship(Base):
    """A typed link between two entities."""
    __tablename__ = "entity_relationships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_entity_id = Column(Integer, nullable=False)
    to_entity_id = Column(Integer, nullable=False)
    relationship = Column(String(64), nullable=False)
    observed_at = Column(Date, nullable=True)
    valid_from = Column(Date, nullable=True)
    valid_to = Column(Date, nullable=True)
    first_observed_at = Column(Date, nullable=True)
    last_observed_at = Column(Date, nullable=True)
    provenance_type = Column(String(32), nullable=True)
    provenance_id = Column(Integer, nullable=True)
    source_label = Column(Text, nullable=True)
    edge_kind = Column(String(32), nullable=True, default="relational")
    source_type = Column(String(32), nullable=True)
    source_id = Column(Integer, nullable=True)
    confidence = Column(Float, nullable=False, default=0.5)
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_relationships_from", "from_entity_id"),
        Index("ix_relationships_to", "to_entity_id"),
    )


class IngestFailure(Base):
    """Records import-side failures that can't reach the meetings table.

    When a scraper or import process fails before a meeting record exists
    (e.g. CSV persist with unknown body, body resolution failure), the error
    goes here so it's queryable instead of being silently dropped.
    """
    __tablename__ = "_ingest_failures"

    id = Column(Integer, primary_key=True, autoincrement=True)
    error_category = Column(String(32), nullable=False)  # TRANSIENT, CODE, DATA, UNKNOWN
    source = Column(String(64), nullable=False)  # e.g. "csv-persist", "body-resolution"
    body = Column(String(16), nullable=True)  # The attempted body, if known
    # External scraper/source ID only; this table can be written before a
    # meetings row exists and must never be reparented to meetings.id.
    meeting_id = Column(String(32), nullable=True)
    meeting_date = Column(String(16), nullable=True)  # The meeting date, if known
    error = Column(Text, nullable=False)  # Full error message
    context = Column(Text, nullable=True)  # Additional context
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
