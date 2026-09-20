"""event_normalize_models.py — typed normalization candidate (pure).

Turns one raw ``meeting_event_extractions`` row plus its resolved civic context
into a validated :class:`NormalizationCandidate`.

This module is deliberately **pure**: no SQL, no database driver, no validator,
no receipt mutation, no subprocess, and no writes.  It performs no I/O at all,
so it is importable and testable without a database.

Meeting references
------------------
Two distinct meeting references are carried and never conflated:

``meeting_db_id``
    The canonical ``meetings.id`` integer.  This -- and only this -- builds the
    civic meeting identity, so that identity stays stable when the external
    reference string changes.

``meeting_source_id``
    The external ``meetings.meeting_id`` string.  It is an attribute of the
    meeting, not its identity, and never participates in the context chain.

Evidence limitation
-------------------
The event tables do not persist the evidence content hash or the model version.
A candidate's ``content_hash`` is computed from *current* analysed text, so an
exact semantic match against stored state is **current-evidence revalidation**:
it is not proof of the content version used by the historical write.  Durable
evidence and model identity is deferred to the Stage 1 migration plan.  There is
deliberately no ``stored_content_hash`` field, because populating one from
current source data would fabricate a provenance claim the database cannot
support.

Vocabulary is never duplicated here.  The producer's own ``VERB_MAP`` is the
single authority for verb -> (dotted slug, raw outcome); the registries are the
single authority for leaf event types and outcome compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.kg.identity import (
    IdentityKey,
    civic_context_identity,
    evidence_identity,
    extraction_identity,
)
from scripts.kg.registries import (
    CanonicalOutcome,
    canonicalize_outcome,
    evidence_class_for_extraction_method,
    normalize_event_slug,
)
from scripts.kg.registries import EVENT_TYPES

__all__ = [
    "SOURCE_SYSTEM",
    "CandidateError",
    "NormalizationCandidate",
    "normalize_action_verb",
    "normalize_nullable_string",
    "normalize_offsets",
    "require_integer",
    "require_nonempty",
]

#: The civic source system every context identity in this producer is scoped to.
SOURCE_SYSTEM = "poliscopic"

#: Extractor name recorded on the extraction identity for this phase.
EXTRACTOR_NAME = "event_extract"


class CandidateError(ValueError):
    """Raised when a candidate cannot be built from incomplete or invalid input."""


def require_nonempty(value: object, name: str) -> None:
    """Raise unless ``value`` is a non-empty, non-whitespace value."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise CandidateError(f"{name} is required to construct a candidate")


def require_integer(value: object, name: str) -> int:
    """Return ``value`` as an ``int``, or raise :class:`CandidateError`."""
    require_nonempty(value, name)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise CandidateError(f"{name} must be an integer, got {value!r}") from None


def normalize_nullable_string(value: object) -> str | None:
    """Canonicalize an optional free-text field.

    ``None``, the empty string, and a whitespace-only string all normalize to
    ``None`` (absent).  Any other value becomes ``str(value).strip()``.  Without
    this rule, ``None`` and ``""`` would compare as different values even though
    the database cannot meaningfully distinguish them.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_offsets(span_start: object, span_end: object) -> tuple[int, int] | None:
    """Canonicalize a text span.

    Both offsets absent -> ``None`` (the row asserts no span).  Both present ->
    an ``(int, int)`` pair.  A half-populated or inverted span raises
    :class:`CandidateError` rather than being coerced into something comparable.
    """
    if span_start is None and span_end is None:
        return None
    if (span_start is None) != (span_end is None):
        raise CandidateError(
            "text offsets must be both present or both absent "
            f"(span_start={span_start!r}, span_end={span_end!r})"
        )
    start, end = int(span_start), int(span_end)  # type: ignore[arg-type]
    if start > end:
        raise CandidateError(
            f"text offsets are inverted (span_start {start} > span_end {end})"
        )
    return start, end


def normalize_action_verb(raw_verb: str) -> tuple[str, CanonicalOutcome]:
    """Return ``(canonical leaf event type, CanonicalOutcome)`` for a raw verb.

    The producer's ``VERB_MAP`` supplies the dotted seed slug and the raw
    outcome.  The dotted slug is mapped to its canonical leaf through
    ``normalize_event_slug``, and the raw outcome through ``canonicalize_outcome``.
    No outcome compatibility logic is reproduced locally.

    The producer module is imported lazily so this pure module does not pull the
    producer's database stack in at import time.
    """
    from scripts.entities.event_normalize import VERB_MAP, normalize_verb

    key = normalize_verb(str(raw_verb or ""))
    if not key:
        raise CandidateError("action verb is required")
    mapping = VERB_MAP.get(key)
    if mapping is None:
        raise CandidateError(f"action verb {raw_verb!r} has no canonical mapping")
    dotted_slug, raw_outcome = mapping
    leaf = normalize_event_slug(dotted_slug)
    if leaf not in EVENT_TYPES:
        raise CandidateError(
            f"action verb {raw_verb!r} maps to unregistered event type {leaf!r}"
        )
    return leaf, canonicalize_outcome(raw_outcome)


@dataclass(frozen=True)
class NormalizationCandidate:
    """A fully resolved, typed normalization candidate.

    ``meeting_db_id`` is the canonical ``meetings.id`` integer and is the only
    meeting value that participates in the civic context identity.
    ``meeting_source_id`` is the external ``meetings.meeting_id`` string and is
    carried purely as a comparable attribute.

    Construction fails closed: a missing member of the civic context chain, a
    missing content version, an unusable extraction method, or an invalid span all
    raise rather than producing a partially identified candidate.
    """

    extraction_id: int
    supporting_document_id: int
    meeting_db_id: int
    meeting_source_id: str
    public_body_id: object
    jurisdiction_id: object
    action_verb: str
    event_type: str
    outcome: CanonicalOutcome
    confidence: float
    content_hash: str
    evidence_class: str
    extraction_method: str | None
    case_number: str | None
    span_start: int | None
    span_end: int | None

    jurisdiction_identity: IdentityKey
    body_identity: IdentityKey
    meeting_identity: IdentityKey
    evidence_identity: IdentityKey
    extraction_identity: IdentityKey

    @classmethod
    def create(
        cls,
        *,
        extraction_id: object,
        supporting_document_id: object,
        meeting_db_id: object,
        meeting_source_id: object,
        public_body_id: object,
        jurisdiction_id: object,
        action_verb: str,
        content_hash: str | None,
        extraction_method: str | None,
        confidence: float = 0.0,
        case_number: str | None = None,
        span_start: int | None = None,
        span_end: int | None = None,
        extractor: str = EXTRACTOR_NAME,
        extractor_version: str | None = None,
    ) -> "NormalizationCandidate":
        """Validate the inputs and build the candidate, or raise."""
        extraction_id = require_integer(extraction_id, "extraction_id")
        supporting_document_id = require_integer(
            supporting_document_id, "supporting_document_id"
        )
        meeting_db_id = require_integer(meeting_db_id, "meeting_db_id")
        require_nonempty(meeting_source_id, "meeting_source_id")
        require_nonempty(public_body_id, "public body id")
        require_nonempty(jurisdiction_id, "jurisdiction id")
        require_nonempty(content_hash, "content hash")

        # Both offsets or neither; a half-populated or inverted span is invalid.
        span = normalize_offsets(span_start, span_end)
        span_start, span_end = span if span is not None else (None, None)

        evidence_class = evidence_class_for_extraction_method(extraction_method)
        if evidence_class is None:
            raise CandidateError(
                f"extraction method {extraction_method!r} has no honest evidence "
                "class"
            )

        event_type, outcome = normalize_action_verb(action_verb)

        jurisdiction_key = civic_context_identity(
            context_class="jurisdiction",
            source_system=SOURCE_SYSTEM,
            context_id=jurisdiction_id,
        )
        body_key = civic_context_identity(
            context_class="body",
            source_system=SOURCE_SYSTEM,
            context_id=public_body_id,
            parent=jurisdiction_key,
        )
        # Only the canonical database id builds the meeting identity.
        meeting_key = civic_context_identity(
            context_class="meeting",
            source_system=SOURCE_SYSTEM,
            context_id=meeting_db_id,
            parent=body_key,
        )
        evidence_key = evidence_identity(
            source_type="supporting_document",
            source_id=supporting_document_id,
            span_start=span_start,
            span_end=span_end,
            content_hash=str(content_hash),
            extraction_method=extraction_method,
        )
        extraction_key = extraction_identity(
            evidence_key, extractor=extractor, extractor_version=extractor_version,
        )

        return cls(
            extraction_id=extraction_id,
            supporting_document_id=supporting_document_id,
            meeting_db_id=meeting_db_id,
            meeting_source_id=str(meeting_source_id).strip(),
            public_body_id=public_body_id,
            jurisdiction_id=jurisdiction_id,
            action_verb=str(action_verb),
            event_type=event_type,
            outcome=outcome,
            confidence=float(confidence),
            content_hash=str(content_hash),
            evidence_class=evidence_class,
            extraction_method=extraction_method,
            case_number=case_number,
            span_start=span_start,
            span_end=span_end,
            jurisdiction_identity=jurisdiction_key,
            body_identity=body_key,
            meeting_identity=meeting_key,
            evidence_identity=evidence_key,
            extraction_identity=extraction_key,
        )

    @property
    def occurrence(self) -> str:
        """The exact source occurrence this event normalizes.

        The extraction row *is* the occurrence: two distinct source extractions
        produce distinct occurrences, while an unchanged replay of the same
        extraction reproduces the same one.  The analysed-text content version is
        included, so a re-extraction of changed text is not the same occurrence.
        """
        return f"extraction:{self.extraction_id}:{self.content_hash}"

    @property
    def is_qualified_outcome(self) -> bool:
        return self.outcome.is_qualified
