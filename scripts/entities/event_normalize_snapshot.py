"""event_normalize_snapshot.py — stored-state snapshot and comparison (pure).

Holds :class:`ExistingNormalizedEvent`, the typed snapshot of what is *actually
stored* for an already-linked extraction, and the field-by-field comparison that
decides whether it may be treated as a replay.

This module is deliberately **pure**: no SQL, no database driver, no validator or
receipt mutation, no subprocess, and no writes.

A snapshot is not proof of identity
-----------------------------------
A caller-supplied identity digest is never accepted as evidence.  The expected
event identity is always reconstructed from the candidate, and the comparison in
:meth:`ExistingNormalizedEvent.compare_to` is what decides replay.  Nothing here
claims the database stored an identity digest historically.

Meeting references are compared independently
---------------------------------------------
Three distinct stored meeting references are checked against the candidate, each
under its own field name, so a disagreement can never hide behind a generic
"meeting" verdict:

``meeting_db_id``
    canonical ``meetings.id``, reached through the supporting-document/meeting
    join.  Checked against ``candidate.meeting_db_id``.
``stored_meeting_source_id``
    ``meeting_events.meeting_id`` exactly as written on the event row.  Checked
    against ``candidate.meeting_source_id``.
``canonical_meeting_source_id``
    ``meetings.meeting_id`` from the joined canonical meeting.  Checked against
    ``candidate.meeting_source_id``.

The supporting-document linkage is checked for internal coherence as well:
``supporting_documents.meeting_db_id`` must agree with the canonical meeting, and
``supporting_documents.meeting_id`` must agree with the canonical external
reference.  Carrying these expected values here means storage cannot silently
omit the checks later.

Evidence limitation
-------------------
The event tables do not persist the evidence content hash or the model version.
An exact semantic match is therefore **current-evidence revalidation**: it is not
proof of the content version used by the historical write.  Durable evidence and
model identity is deferred to the Stage 1 migration plan.  No
``stored_content_hash`` field exists, because filling one from current source
data would fabricate provenance the database cannot support.

Comparison rules (all deterministic)
------------------------------------
*nullable strings* -- ``None``, ``""``, and whitespace-only all normalize to
``None``; otherwise the stripped string.  Applied to ``case_number`` and
``outcome_qualifier``.

*offsets* -- absent/absent normalizes to ``None``; present/present to an
``(int, int)`` pair; a half-populated or inverted span is rejected at
construction, so a snapshot can never carry an ambiguous span.

*event type* -- the stored value is mapped through ``normalize_event_slug``, so a
dotted seed slug and its canonical leaf compare equal.

*outcome* -- the stored base and qualifier are re-canonicalized through
``CanonicalOutcome``; an invalid or contradictory stored pair is reported on both
outcome fields rather than silently ignored.

*action verb* -- both sides are normalized through the producer's own
``normalize_verb`` rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.kg.identity import IdentityKey
from scripts.kg.registries import (
    CanonicalOutcome,
    OutcomeError,
    normalize_event_slug,
)

from scripts.entities.event_normalize_models import (
    NormalizationCandidate,
    normalize_nullable_string,
    normalize_offsets,
    require_integer,
    require_nonempty,
)

__all__ = [
    "ExistingNormalizedEvent",
    "FieldMismatch",
    "stored_verb_key",
]


@dataclass(frozen=True)
class FieldMismatch:
    """One stored field that disagreed with the expected candidate value."""

    field: str
    expected: object
    observed: object


def stored_verb_key(raw: object) -> str:
    """Normalize a stored action verb through the producer's own rule."""
    from scripts.entities.event_normalize import normalize_verb

    return normalize_verb(str(raw or ""))


@dataclass(frozen=True)
class ExistingNormalizedEvent:
    """What is actually stored for one already-linked extraction."""

    extraction_id: int
    event_id: object
    supporting_document_id: int
    event_type: str
    outcome_base: str
    meeting_db_id: int
    stored_meeting_source_id: str
    canonical_meeting_source_id: str
    supporting_document_meeting_db_id: int
    supporting_document_meeting_source_id: str
    action_verb: str
    outcome_qualifier: str | None = None
    meeting_context: IdentityKey | None = None
    span_start: int | None = None
    span_end: int | None = None
    case_number: str | None = None

    def __post_init__(self) -> None:
        require_integer(self.extraction_id, "extraction_id")
        require_nonempty(self.event_id, "event_id")
        require_integer(self.supporting_document_id, "supporting_document_id")
        require_nonempty(self.event_type, "event_type")
        require_nonempty(self.outcome_base, "outcome_base")
        require_integer(self.meeting_db_id, "meeting_db_id")
        require_nonempty(
            self.stored_meeting_source_id, "stored_meeting_source_id"
        )
        require_nonempty(
            self.canonical_meeting_source_id, "canonical_meeting_source_id"
        )
        require_integer(
            self.supporting_document_meeting_db_id,
            "supporting_document_meeting_db_id",
        )
        require_nonempty(
            self.supporting_document_meeting_source_id,
            "supporting_document_meeting_source_id",
        )
        require_nonempty(self.action_verb, "action_verb")
        # Rejects an ambiguous stored span at construction time.
        normalize_offsets(self.span_start, self.span_end)
        object.__setattr__(
            self, "case_number", normalize_nullable_string(self.case_number)
        )
        object.__setattr__(
            self,
            "outcome_qualifier",
            normalize_nullable_string(self.outcome_qualifier),
        )

    def canonical_stored_outcome(self) -> CanonicalOutcome | None:
        """Re-canonicalize the stored outcome, or ``None`` if it is invalid."""
        try:
            return CanonicalOutcome(self.outcome_base, self.outcome_qualifier)
        except OutcomeError:
            return None

    def compare_to(
        self, candidate: NormalizationCandidate
    ) -> tuple[FieldMismatch, ...]:
        """Return every field that disagrees with ``candidate``.

        An empty result means the stored row matches the candidate on every
        semantic field, which is the only basis on which a replay is claimed.
        Each meeting reference is compared separately and named separately.
        The candidate's current evidence span is validated first; an invalid span
        fails closed rather than comparing.
        """
        # Condition 5: the candidate's current evidence span must be valid.
        candidate_span = normalize_offsets(candidate.span_start, candidate.span_end)

        mismatches: list[FieldMismatch] = []

        def note(field: str, expected: object, observed: object) -> None:
            mismatches.append(FieldMismatch(field, expected, observed))

        if int(self.supporting_document_id) != int(candidate.supporting_document_id):
            note(
                "supporting_document_id",
                int(candidate.supporting_document_id),
                int(self.supporting_document_id),
            )

        stored_type = normalize_event_slug(str(self.event_type))
        if stored_type != candidate.event_type:
            note("event_type", candidate.event_type, stored_type)

        stored_outcome = self.canonical_stored_outcome()
        if stored_outcome is None:
            note("outcome_base", candidate.outcome.base, self.outcome_base)
            note(
                "outcome_qualifier",
                candidate.outcome.qualifier,
                self.outcome_qualifier,
            )
        else:
            if stored_outcome.base != candidate.outcome.base:
                note("outcome_base", candidate.outcome.base, stored_outcome.base)
            if stored_outcome.qualifier != candidate.outcome.qualifier:
                note(
                    "outcome_qualifier",
                    candidate.outcome.qualifier,
                    stored_outcome.qualifier,
                )

        # --- meeting references, each checked and named independently ---------
        if int(self.meeting_db_id) != int(candidate.meeting_db_id):
            note(
                "meeting_db_id",
                int(candidate.meeting_db_id),
                int(self.meeting_db_id),
            )

        if str(self.canonical_meeting_source_id) != str(candidate.meeting_source_id):
            note(
                "canonical_meeting_source_id",
                candidate.meeting_source_id,
                self.canonical_meeting_source_id,
            )

        if str(self.stored_meeting_source_id) != str(candidate.meeting_source_id):
            note(
                "stored_meeting_source_id",
                candidate.meeting_source_id,
                self.stored_meeting_source_id,
            )

        # --- supporting-document linkage coherence ---------------------------
        if int(self.supporting_document_meeting_db_id) != int(candidate.meeting_db_id):
            note(
                "supporting_document_meeting_db_id",
                int(candidate.meeting_db_id),
                int(self.supporting_document_meeting_db_id),
            )

        expected_sd_source = str(candidate.meeting_source_id)
        if str(self.supporting_document_meeting_source_id) != expected_sd_source:
            note(
                "supporting_document_meeting_source_id",
                expected_sd_source,
                self.supporting_document_meeting_source_id,
            )

        if (
            self.meeting_context is not None
            and self.meeting_context.digest != candidate.meeting_identity.digest
        ):
            note(
                "meeting_context",
                candidate.meeting_identity.digest,
                self.meeting_context.digest,
            )

        stored_verb = stored_verb_key(self.action_verb)
        candidate_verb = stored_verb_key(candidate.action_verb)
        if stored_verb != candidate_verb:
            note("action_verb", candidate_verb, stored_verb)

        if normalize_offsets(self.span_start, self.span_end) != candidate_span:
            note("span", candidate_span, (self.span_start, self.span_end))

        stored_case = normalize_nullable_string(self.case_number)
        candidate_case = normalize_nullable_string(candidate.case_number)
        if stored_case != candidate_case:
            note("case_number", candidate_case, stored_case)

        return tuple(mismatches)
