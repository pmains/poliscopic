"""event_normalize_query.py — read query and row construction (read-only).

Read-only by construction
-------------------------
Every statement this module issues is a projection over the civic chain; it
issues no mutation statement of any kind and no DDL, and it never opens a
transaction that could write.  A source-scan test proves the only statement verb
present is ``SELECT``.

Query contract
--------------
One page query walks the authoritative chain with left joins, so a broken link
surfaces as a typed failure rather than a silently vanished row::

    meeting_event_extractions  e
      -> supporting_documents  sd   ON sd.id = e.supporting_doc_id
      -> meetings              m    ON m.id  = sd.meeting_db_id
      -> public_bodies         pb   ON pb.id = m.public_body_id
      -> jurisdictions         j    ON j.id  = pb.jurisdiction_id
      -> meeting_events        me   ON me.id = e.meeting_event_id
      -> meeting_event_types   met  ON met.id = me.event_type_id

The meeting join is keyed on ``supporting_documents.meeting_db_id``, which is
itself the coherence check for that link: an unset or wrong value fails to join
and is reported as a missing meeting.

Distinguished meeting references
--------------------------------
``meetings.id``                        -> ``meeting_db_id`` (canonical)
``meetings.meeting_id``                -> ``canonical_meeting_source_id``
``supporting_documents.meeting_db_id`` -> ``supporting_document_meeting_db_id``
``supporting_documents.meeting_id``    -> ``supporting_document_meeting_source_id``
``meeting_events.meeting_id``          -> ``stored_meeting_source_id``

Row helpers
-----------
A row is interpreted here and only here.  Each builder returns either a typed
artefact or a :class:`ReadFailure`; neither raises for bad data.  Callers decide
what to do with a failure.

Evidence limitation
-------------------
The event tables do not persist the evidence content hash or the model version.
``analyzed_text_hash`` therefore reflects *current* analysed text, which is what
makes an exact semantic match a revalidation rather than a historical claim.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from scripts.kg.registries import (
    EVENT_TYPES,
    OutcomeError,
    canonicalize_outcome,
    evidence_class_for_extraction_method,
    normalize_event_slug,
)

from scripts.entities.event_normalize_models import (
    CandidateError,
    NormalizationCandidate,
    normalize_action_verb,
    normalize_offsets,
)
from scripts.entities.event_normalize_snapshot import ExistingNormalizedEvent

__all__ = [
    "REASON_INCOMPLETE_CONTEXT",
    "REASON_INVALID_ACTION_VERB",
    "REASON_INVALID_EVENT_TYPE",
    "REASON_INVALID_OUTCOME",
    "REASON_MALFORMED_OFFSETS",
    "REASON_MISSING_EXTRACTOR",
    "REASON_MISSING_JURISDICTION",
    "REASON_MISSING_LINKED_EVENT",
    "REASON_MISSING_MEETING",
    "REASON_MISSING_PUBLIC_BODY",
    "REASON_MISSING_SUPPORTING_DOCUMENT",
    "REASON_MISSING_TEXT",
    "REASON_UNSUPPORTED_METHOD",
    "UNBOUNDED_LIMIT",
    "ReadFailure",
    "analyzed_text_hash",
    "build_candidate_from_row",
    "build_page_query",
    "build_snapshot_from_row",
    "row_extraction_id",
    "row_is_linked",
]

# --- typed read-failure reasons ---------------------------------------------

REASON_MISSING_SUPPORTING_DOCUMENT = "missing_supporting_document"
REASON_MISSING_MEETING = "missing_meeting"
REASON_MISSING_PUBLIC_BODY = "missing_public_body"
REASON_MISSING_JURISDICTION = "missing_jurisdiction"
REASON_MISSING_TEXT = "missing_text_content"
REASON_UNSUPPORTED_METHOD = "unsupported_extraction_method"
REASON_MALFORMED_OFFSETS = "malformed_offsets"
REASON_INVALID_ACTION_VERB = "invalid_action_verb"
REASON_INVALID_EVENT_TYPE = "invalid_event_type"
REASON_INVALID_OUTCOME = "invalid_outcome"
REASON_MISSING_LINKED_EVENT = "missing_linked_event"
REASON_INCOMPLETE_CONTEXT = "incomplete_civic_context"
REASON_MISSING_EXTRACTOR = "missing_extractor"

#: Sent to the database in place of ``LIMIT`` for an unbounded read.
UNBOUNDED_LIMIT = 2**31 - 1

_PAGE_QUERY = """
SELECT
    e.id                        AS extraction_id,
    e.supporting_doc_id         AS supporting_doc_id,
    e.meeting_event_id          AS linked_event_id,
    e.extractor                 AS extractor,
    e.extractor_version         AS extractor_version,
    e.action_verb               AS action_verb,
    e.confidence                AS confidence,
    e.text_offset_start         AS span_start,
    e.text_offset_end           AS span_end,
    e.case_number               AS case_number,
    sd.id                       AS sd_id,
    sd.meeting_id               AS sd_meeting_source_id,
    sd.meeting_db_id            AS sd_meeting_db_id,
    sd.text_content             AS sd_text_content,
    sd.text_extraction_method   AS sd_extraction_method,
    m.id                        AS meeting_db_id,
    m.meeting_id                AS canonical_meeting_source_id,
    m.public_body_id            AS public_body_id,
    pb.id                       AS pb_id,
    pb.jurisdiction_id          AS jurisdiction_id,
    j.id                        AS jurisdiction_row_id,
    me.id                       AS event_id,
    me.meeting_id               AS stored_meeting_source_id,
    me.outcome                  AS stored_outcome,
    me.action_verb              AS stored_action_verb,
    me.text_offset_start        AS stored_span_start,
    me.text_offset_end          AS stored_span_end,
    me.case_number              AS stored_case_number,
    met.slug                    AS stored_event_type_slug
FROM meeting_event_extractions e
LEFT JOIN supporting_documents sd  ON sd.id  = e.supporting_doc_id
LEFT JOIN meetings m               ON m.id   = sd.meeting_db_id
LEFT JOIN public_bodies pb         ON pb.id  = m.public_body_id
LEFT JOIN jurisdictions j          ON j.id   = pb.jurisdiction_id
LEFT JOIN meeting_events me        ON me.id  = e.meeting_event_id
LEFT JOIN meeting_event_types met  ON met.id = me.event_type_id
{where}
ORDER BY e.id
LIMIT :limit
"""


@dataclass(frozen=True)
class ReadFailure:
    """One row that could not be turned into a usable work item."""

    extraction_id: int
    reason: str
    detail: str = ""


def analyzed_text_hash(text_content: object) -> str:
    """SHA-256 over the exact analysed text being normalized.

    This is deliberately *not* ``supporting_documents.content_hash``, which may
    describe the source bytes rather than the analysed text produced from them.
    """
    return hashlib.sha256(str(text_content).encode("utf-8")).hexdigest()


def _get(row: Any, name: str) -> Any:
    return row._mapping[name]


def row_extraction_id(row: Any) -> int:
    """The extraction row id a result row belongs to."""
    return int(_get(row, "extraction_id"))


def row_is_linked(row: Any) -> bool:
    """Whether the extraction row already points at a stored event."""
    return _get(row, "linked_event_id") is not None


def build_page_query(*, force: bool, has_cursor: bool) -> str:
    """Compose the page query for the requested mode and cursor state.

    Quarantined rows are excluded in *both* modes.  ``force`` re-reads rows that
    are already linked, but it must never resurrect evidence that was explicitly
    quarantined: quarantine is a knowledge decision, not a linkage state.
    """
    from scripts.kg.quarantine import QUARANTINE_SELECTION_CLAUSE

    clauses = [QUARANTINE_SELECTION_CLAUSE]
    if not force:
        clauses.append("e.meeting_event_id IS NULL")
    if has_cursor:
        clauses.append("e.id > :after_extraction_id")
    where = ""
    if clauses:
        where = "WHERE " + "\n  AND ".join(clauses)
    return _PAGE_QUERY.format(where=where)


def _chain_failure(row: Any, extraction_id: int) -> ReadFailure | None:
    """Return the failure for a broken civic link, or ``None`` when intact."""
    if _get(row, "sd_id") is None:
        return ReadFailure(
            extraction_id,
            REASON_MISSING_SUPPORTING_DOCUMENT,
            "supporting_doc_id did not join supporting_documents",
        )
    if _get(row, "meeting_db_id") is None:
        return ReadFailure(
            extraction_id,
            REASON_MISSING_MEETING,
            "supporting_documents.meeting_db_id did not join meetings.id",
        )
    if _get(row, "public_body_id") is None or _get(row, "pb_id") is None:
        return ReadFailure(
            extraction_id,
            REASON_MISSING_PUBLIC_BODY,
            "meetings.public_body_id did not join public_bodies.id",
        )
    if _get(row, "jurisdiction_row_id") is None:
        return ReadFailure(
            extraction_id,
            REASON_MISSING_JURISDICTION,
            "public_bodies.jurisdiction_id did not join jurisdictions.id",
        )
    return None


def build_candidate_from_row(row: Any) -> NormalizationCandidate | ReadFailure:
    """Reconstruct the current-evidence candidate for a row, or fail closed.

    This runs for every usable row, linked or not: the candidate is the assertion
    derived from the extraction and the source document as they stand now, and it
    must never be replaced by stored state.
    """
    extraction_id = row_extraction_id(row)

    broken = _chain_failure(row, extraction_id)
    if broken is not None:
        return broken

    text_content = _get(row, "sd_text_content")
    if text_content is None or not str(text_content).strip():
        return ReadFailure(
            extraction_id,
            REASON_MISSING_TEXT,
            "supporting_documents.text_content is empty",
        )

    extraction_method = _get(row, "sd_extraction_method")
    if evidence_class_for_extraction_method(extraction_method) is None:
        return ReadFailure(
            extraction_id,
            REASON_UNSUPPORTED_METHOD,
            f"extraction method {extraction_method!r} has no honest evidence class",
        )

    span_start = _get(row, "span_start")
    span_end = _get(row, "span_end")
    try:
        normalize_offsets(span_start, span_end)
    except CandidateError as exc:
        return ReadFailure(extraction_id, REASON_MALFORMED_OFFSETS, str(exc))

    action_verb = str(_get(row, "action_verb") or "")
    try:
        normalize_action_verb(action_verb)
    except CandidateError as exc:
        return ReadFailure(extraction_id, REASON_INVALID_ACTION_VERB, str(exc))

    extractor = str(_get(row, "extractor") or "").strip()
    if not extractor:
        return ReadFailure(
            extraction_id,
            REASON_MISSING_EXTRACTOR,
            "meeting_event_extractions.extractor is empty, so the extraction "
            "identity cannot be formed",
        )

    try:
        return NormalizationCandidate.create(
            extraction_id=extraction_id,
            supporting_document_id=_get(row, "sd_id"),
            meeting_db_id=_get(row, "meeting_db_id"),
            meeting_source_id=_get(row, "canonical_meeting_source_id"),
            public_body_id=_get(row, "public_body_id"),
            jurisdiction_id=_get(row, "jurisdiction_row_id"),
            action_verb=action_verb,
            content_hash=analyzed_text_hash(text_content),
            extraction_method=extraction_method,
            confidence=float(_get(row, "confidence") or 0.0),
            case_number=_get(row, "case_number"),
            span_start=span_start,
            span_end=span_end,
            extractor=extractor,
            extractor_version=_get(row, "extractor_version"),
        )
    except CandidateError as exc:
        return ReadFailure(extraction_id, REASON_INCOMPLETE_CONTEXT, str(exc))


def build_snapshot_from_row(row: Any) -> ExistingNormalizedEvent | ReadFailure:
    """Build the stored-state snapshot for a linked row, or fail closed.

    ``meeting_context`` is left ``None`` because the database does not persist a
    context identity, and deriving one from the same joins that produced the
    candidate would be a manufactured digest.  Every other meeting-coherence
    field is populated from its own real column.
    """
    extraction_id = row_extraction_id(row)

    broken = _chain_failure(row, extraction_id)
    if broken is not None:
        return broken

    event_id = _get(row, "event_id")
    if event_id is None:
        return ReadFailure(
            extraction_id,
            REASON_MISSING_LINKED_EVENT,
            "meeting_event_id is set but no matching meeting_events row exists",
        )

    slug = _get(row, "stored_event_type_slug")
    if slug is None or normalize_event_slug(str(slug)) not in EVENT_TYPES:
        return ReadFailure(
            extraction_id,
            REASON_INVALID_EVENT_TYPE,
            f"stored event type slug {slug!r} is not a registered event type",
        )

    try:
        outcome = canonicalize_outcome(str(_get(row, "stored_outcome") or ""))
    except OutcomeError as exc:
        return ReadFailure(extraction_id, REASON_INVALID_OUTCOME, str(exc))

    stored_span_start = _get(row, "stored_span_start")
    stored_span_end = _get(row, "stored_span_end")
    try:
        normalize_offsets(stored_span_start, stored_span_end)
    except CandidateError as exc:
        return ReadFailure(extraction_id, REASON_MALFORMED_OFFSETS, str(exc))

    try:
        return ExistingNormalizedEvent(
            extraction_id=extraction_id,
            event_id=event_id,
            supporting_document_id=_get(row, "sd_id"),
            event_type=str(slug),
            outcome_base=outcome.base,
            outcome_qualifier=outcome.qualifier,
            meeting_db_id=_get(row, "meeting_db_id"),
            stored_meeting_source_id=_get(row, "stored_meeting_source_id"),
            canonical_meeting_source_id=_get(row, "canonical_meeting_source_id"),
            supporting_document_meeting_db_id=_get(row, "sd_meeting_db_id"),
            supporting_document_meeting_source_id=_get(row, "sd_meeting_source_id"),
            meeting_context=None,
            action_verb=str(_get(row, "stored_action_verb") or ""),
            span_start=stored_span_start,
            span_end=stored_span_end,
            case_number=_get(row, "stored_case_number"),
        )
    except CandidateError as exc:
        return ReadFailure(extraction_id, REASON_INCOMPLETE_CONTEXT, str(exc))
