"""event_normalize_write_storage.py — narrow write storage for normalization.

A narrow seam
-------------
:class:`SqlAlchemyWriteStorage` exposes exactly the operations the write adapter
needs -- resolve a leaf event type, claim one extraction row, read its link state,
insert one event, apply one guarded link -- and nothing else.  It holds no plan
state and makes no decisions, so a fault can be injected by overriding a single
method rather than mocking the adapter or the transaction.

There is deliberately **no coarse event lookup**.  A tuple of
``(meeting, document, event type, span)`` is not an event identity: the canonical
identity is scoped to the extraction row that produced the occurrence, so a
lookup on that tuple could collapse two distinct event identities into one stored
event.  Claims about "the same event" are therefore made only about an event an
extraction row is actually linked to, never about a lookalike row.

Link updates are compare-and-set
--------------------------------
``link_extraction`` only ever fills a NULL link.  A competing writer can therefore
never have its link overwritten by this seam; a lost race is reported as an
affected-row count of zero and the caller decides what that means.

SQLite-compatible, PostgreSQL-preserving
----------------------------------------
Statements use only portable SQL: named parameters, a ``RETURNING`` clause
(PostgreSQL and SQLite >= 3.35), and explicit null-safe comparison rather than
``IS NOT DISTINCT FROM``.  ``created_at`` is omitted from the insert so the
database keeps supplying it from its own default, exactly as the live schema
declares.

Row locking is dialect-aware: PostgreSQL takes ``SELECT ... FOR UPDATE``, which
serializes competing normalizations of the same extraction row.  SQLite has no row
locks, so its correctness rests on the guarded compare-and-set plus SQLite's
single-writer transaction; the lock call still reads the row's link state so the
adapter's logic is identical on both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import text

from scripts.kg.registries import EVENT_TYPES, normalize_event_slug

__all__ = [
    "EventTypeLookupError",
    "ExtractionLinkState",
    "SqlAlchemyWriteStorage",
    "StoredEventRow",
    "stored_outcome_text",
]


class EventTypeLookupError(LookupError):
    """Raised when a canonical leaf event type has no matching database row."""


@dataclass(frozen=True)
class StoredEventRow:
    """A stored ``meeting_events`` row, as this seam reads it."""

    event_id: int
    meeting_id: str
    supporting_document_id: int | None
    event_type_id: int
    outcome: str
    action_verb: str
    span_start: int | None
    span_end: int | None
    case_number: str | None


@dataclass(frozen=True)
class ExtractionLinkState:
    """The link state of one extraction row.

    ``exists`` distinguishes a missing row from an unlinked one, which the
    guarded update alone cannot: an affected-row count of zero means either.
    """

    exists: bool
    event_id: int | None


def stored_outcome_text(base: str, qualifier: str | None) -> str:
    """The single-column storage form of a canonical outcome.

    ``CanonicalOutcome.serialize()`` is the receipt-shaped *pair* and is not this:
    ``meeting_events.outcome`` is one column, so a qualified outcome is stored as
    ``<base>_<qualifier>`` -- precisely the form ``canonicalize_outcome`` reads
    back into the same pair.  No compatibility logic is restated here.
    """
    if qualifier is None:
        return base
    return f"{base}_{qualifier}"


_SELECT_EVENT_COLUMNS = """
    id, meeting_id, supporting_doc_id, event_type_id, outcome, action_verb,
    text_offset_start, text_offset_end, case_number
"""

_LOAD_EVENT = f"""
SELECT {_SELECT_EVENT_COLUMNS}
FROM meeting_events
WHERE id = :event_id
"""

_INSERT_EVENT = """
INSERT INTO meeting_events
    (meeting_id, supporting_doc_id, event_type_id, outcome, action_verb,
     text_offset_start, text_offset_end, case_number)
VALUES
    (:meeting_id, :supporting_document_id, :event_type_id, :outcome,
     :action_verb, :span_start, :span_end, :case_number)
RETURNING id
"""

_SELECT_LINK = """
SELECT id, meeting_event_id
FROM meeting_event_extractions
WHERE id = :extraction_id
"""

# Compare-and-set: only ever fills a NULL link, so a competing link survives.
_LINK_EXTRACTION = """
UPDATE meeting_event_extractions
SET meeting_event_id = :event_id
WHERE id = :extraction_id
  AND meeting_event_id IS NULL
"""

_SELECT_EVENT_TYPES = "SELECT id, slug FROM meeting_event_types"


def _row_to_stored(row: Any) -> StoredEventRow:
    return StoredEventRow(
        event_id=int(row[0]),
        meeting_id=str(row[1]),
        supporting_document_id=None if row[2] is None else int(row[2]),
        event_type_id=int(row[3]),
        outcome=str(row[4]),
        action_verb=str(row[5]),
        span_start=None if row[6] is None else int(row[6]),
        span_end=None if row[7] is None else int(row[7]),
        case_number=None if row[8] is None else str(row[8]),
    )


class SqlAlchemyWriteStorage:
    """The narrow storage seam the write adapter drives."""

    def resolve_event_type_id(self, conn: Any, leaf: str) -> int:
        """Resolve a canonical leaf event type to ``meeting_event_types.id``.

        Resolution goes through the registry's ``normalize_event_slug``, so the
        dotted-seed-slug compatibility contract stays the single authority.
        """
        if leaf not in EVENT_TYPES:
            raise EventTypeLookupError(f"{leaf!r} is not a registered event type")

        rows = conn.execute(text(_SELECT_EVENT_TYPES)).fetchall()

        exact = sorted(int(r[0]) for r in rows if str(r[1] or "") == leaf)
        if exact:
            return exact[0]

        derived = sorted(
            (str(r[1] or ""), int(r[0]))
            for r in rows
            if normalize_event_slug(str(r[1] or "")) == leaf
        )
        if derived:
            return derived[0][1]

        raise EventTypeLookupError(
            f"no meeting_event_types row maps to leaf event type {leaf!r}"
        )

    def lock_extraction(self, conn: Any, extraction_id: int) -> ExtractionLinkState:
        """Claim one extraction row for this transaction and read its link state.

        PostgreSQL appends ``FOR UPDATE``, so a competing normalization of the
        same extraction row blocks rather than racing.  Callers must acquire these
        in ascending extraction-id order.
        """
        statement = _SELECT_LINK
        if conn.dialect.name == "postgresql":
            statement = statement + " FOR UPDATE"
        row = conn.execute(
            text(statement), {"extraction_id": int(extraction_id)}
        ).fetchone()
        if row is None:
            return ExtractionLinkState(exists=False, event_id=None)
        return ExtractionLinkState(
            exists=True, event_id=None if row[1] is None else int(row[1])
        )

    def load_extraction_link(self, conn: Any, extraction_id: int) -> ExtractionLinkState:
        """Read one extraction row's link state without claiming it."""
        row = conn.execute(
            text(_SELECT_LINK), {"extraction_id": int(extraction_id)}
        ).fetchone()
        if row is None:
            return ExtractionLinkState(exists=False, event_id=None)
        return ExtractionLinkState(
            exists=True, event_id=None if row[1] is None else int(row[1])
        )

    def load_event(self, conn: Any, event_id: int) -> StoredEventRow | None:
        """One stored event by id, or ``None`` when it is absent."""
        row = conn.execute(text(_LOAD_EVENT), {"event_id": int(event_id)}).fetchone()
        return None if row is None else _row_to_stored(row)

    def insert_event(self, conn: Any, values: Mapping[str, Any]) -> int:
        """Insert one event and return its generated id."""
        result = conn.execute(text(_INSERT_EVENT), dict(values))
        return int(result.scalar_one())

    def link_extraction(self, conn: Any, extraction_id: int, event_id: int) -> int:
        """Compare-and-set one extraction link; returns the affected row count.

        Returns ``1`` when this call filled a NULL link, and ``0`` when the row is
        missing or already linked.  A competing link is never overwritten.
        """
        result = conn.execute(text(_LINK_EXTRACTION), {
            "extraction_id": int(extraction_id),
            "event_id": int(event_id),
        })
        return int(result.rowcount or 0)
