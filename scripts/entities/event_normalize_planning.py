"""event_normalize_planning.py — pure assertion classification (pure).

Classifies typed normalization candidates into exact event-row and
extraction-link assertions, without touching a database.

The layer is deliberately **pure**: no SQL, no database driver, no validator or
receipt mutation, no subprocess logic, and no writes.  Callers feed it a typed
snapshot of stored state and act on the plan themselves.

Replay is revalidation, not recall
----------------------------------
An already-linked extraction is a replay only when a typed
:class:`~scripts.entities.event_normalize_snapshot.ExistingNormalizedEvent`
snapshot of the stored row matches the freshly built candidate on every semantic
field.  A caller-supplied identity digest is never accepted as proof: the expected
event identity is reconstructed from the candidate, and the field comparison
decides.  That comparison lives in
:mod:`scripts.entities.event_normalize_snapshot`.

Meeting references are distinct and compared separately: the canonical
``meeting_db_id`` builds the civic identity, while the external
``meeting_source_id``, the stored event reference, and the supporting-document
linkage are each checked under their own field name.

`existing_events` is an iterable of stored snapshots.  There is intentionally no
API that accepts a bare ``{extraction_id: digest}`` mapping.

This is the low-level planner, kept for focused tests and compatibility.
Runtime integration must call
:func:`scripts.entities.event_normalize_work_items.build_plan_from_work_items`
instead, which carries both sides of a linked row by construction and delegates
here rather than restating any of this logic.

Accounting units
----------------
``proposed`` counts *assertions considered*: event assertions plus
extraction-link assertions.  ``would_insert`` counts event rows to insert,
``would_update`` counts extraction rows to link, ``replay_noop`` counts assertions
already satisfied, and ``inconsistent`` counts assertions that failed closed.

Extraction links are keyed by the **integer extraction row ID**.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from scripts.kg.identity import IdentityKey, event_identity

from scripts.entities.event_normalize_models import (
    NormalizationCandidate,
    normalize_nullable_string,
)
from scripts.entities.event_normalize_snapshot import (
    ExistingNormalizedEvent,
    FieldMismatch,
)

__all__ = [
    "ClassificationPlan",
    "EventAssertion",
    "EventSemanticPayload",
    "ExistingNormalizedEvent",
    "ExtractionLinkAssertion",
    "InconsistentAssertion",
    "PlanInvariantError",
    "build_classification_plan",
]


class PlanInvariantError(RuntimeError):
    """Raised when a plan, or the input to one, violates a stated invariant."""


@dataclass(frozen=True)
class EventSemanticPayload:
    """The canonical semantic payload an event assertion actually asserts.

    Two assertions may share an identity digest while asserting different
    payloads; carrying the payload is what stops such a pair from silently
    deduplicating.  The signature is a stable, order-fixed rendering used for
    deduplication, conflict detection, and deterministic ordering.
    """

    event_type: str
    outcome_base: str
    outcome_qualifier: str | None
    meeting_db_id: int
    meeting_source_id: str
    supporting_document_id: int
    action_verb: str
    span_start: int | None
    span_end: int | None
    case_number: str | None
    occurrence: str

    @property
    def signature(self) -> str:
        """Deterministic rendering of every payload field."""
        return "|".join(
            (
                self.event_type,
                self.outcome_base,
                repr(self.outcome_qualifier),
                str(self.meeting_db_id),
                self.meeting_source_id,
                str(self.supporting_document_id),
                self.action_verb,
                repr(self.span_start),
                repr(self.span_end),
                repr(self.case_number),
                self.occurrence,
            )
        )

    @classmethod
    def from_candidate(
        cls, candidate: NormalizationCandidate
    ) -> "EventSemanticPayload":
        return cls(
            event_type=candidate.event_type,
            outcome_base=candidate.outcome.base,
            outcome_qualifier=candidate.outcome.qualifier,
            meeting_db_id=int(candidate.meeting_db_id),
            meeting_source_id=candidate.meeting_source_id,
            supporting_document_id=int(candidate.supporting_document_id),
            action_verb=candidate.action_verb,
            span_start=candidate.span_start,
            span_end=candidate.span_end,
            case_number=normalize_nullable_string(candidate.case_number),
            occurrence=candidate.occurrence,
        )


@dataclass(frozen=True)
class EventAssertion:
    """One unique ``meeting_events`` row assertion.

    Identity is a typed event identity built from the canonical leaf event type,
    the meeting context, and the exact source occurrence.  Two distinct source
    extractions therefore yield distinct events, while an unchanged replay of the
    same extraction reproduces exactly the same identity.

    The assertion also carries its :class:`EventSemanticPayload`.  Deduplication
    and ordering key on *both* the identity digest and the payload signature, so
    two assertions that share an identity but disagree semantically are never
    collapsed into one.
    """

    identity: IdentityKey
    payload: EventSemanticPayload

    @property
    def key(self) -> tuple[str, str]:
        """Deduplication key: identity digest plus payload signature."""
        return (self.identity.digest, self.payload.signature)

    @property
    def event_type(self) -> str:
        return self.payload.event_type

    @property
    def occurrence(self) -> str:
        return self.payload.occurrence

    @classmethod
    def from_candidate(cls, candidate: NormalizationCandidate) -> "EventAssertion":
        return cls(
            identity=event_identity(
                event_type=candidate.event_type,
                context=candidate.meeting_identity,
                occurrence=candidate.occurrence,
            ),
            payload=EventSemanticPayload.from_candidate(candidate),
        )


@dataclass(frozen=True)
class ExtractionLinkAssertion:
    """One unique link assertion: an extraction row plus its expected event.

    The extraction **row** is the unit that gets linked, so the integer
    extraction row ID scopes the assertion key.  The typed extraction occurrence
    is carried alongside for provenance.  Two distinct extraction rows therefore
    never collide, even when they read the same evidence occurrence.
    """

    extraction_id: int
    extraction_identity: IdentityKey
    expected_event: IdentityKey

    @property
    def key(self) -> str:
        """Row-scoped link identity, keyed by integer extraction row ID."""
        return f"extraction-row:{self.extraction_id}"

    @classmethod
    def from_candidate(
        cls, candidate: NormalizationCandidate
    ) -> "ExtractionLinkAssertion":
        return cls(
            extraction_id=candidate.extraction_id,
            extraction_identity=candidate.extraction_identity,
            expected_event=EventAssertion.from_candidate(candidate).identity,
        )


@dataclass(frozen=True)
class InconsistentAssertion:
    """A stored link that does not match the candidate.

    Carries the linked event row ID, the expected and observed values, the exact
    differing field names, and a reason, so a failure can be explained without
    re-deriving anything.
    """

    extraction_id: int
    extraction_identity: IdentityKey
    linked_event_id: object
    expected_event: IdentityKey
    differing_fields: tuple[str, ...]
    expected_values: tuple[tuple[str, object], ...]
    observed_values: tuple[tuple[str, object], ...]
    reason: str

    @property
    def key(self) -> str:
        return f"extraction-row:{self.extraction_id}"

    @classmethod
    def from_mismatches(
        cls,
        candidate: NormalizationCandidate,
        link: ExtractionLinkAssertion,
        snapshot: ExistingNormalizedEvent,
        mismatches: tuple[FieldMismatch, ...],
    ) -> "InconsistentAssertion":
        fields = tuple(m.field for m in mismatches)
        reason = (
            f"stored event {snapshot.event_id!r} linked from extraction row "
            f"{candidate.extraction_id} does not match the candidate on: "
            f"{', '.join(fields)}"
        )
        return cls(
            extraction_id=candidate.extraction_id,
            extraction_identity=link.extraction_identity,
            linked_event_id=snapshot.event_id,
            expected_event=link.expected_event,
            differing_fields=fields,
            expected_values=tuple((m.field, m.expected) for m in mismatches),
            observed_values=tuple((m.field, m.observed) for m in mismatches),
            reason=reason,
        )


@dataclass(frozen=True)
class ClassificationPlan:
    """The classified result for one set of candidates."""

    event_inserts: tuple[EventAssertion, ...] = ()
    event_replay_noops: tuple[EventAssertion, ...] = ()
    link_updates: tuple[ExtractionLinkAssertion, ...] = ()
    link_replay_noops: tuple[ExtractionLinkAssertion, ...] = ()
    inconsistent: tuple[InconsistentAssertion, ...] = ()

    @property
    def total_proposed(self) -> int:
        """Every assertion considered, including inconsistent ones."""
        return (
            len(self.event_inserts)
            + len(self.event_replay_noops)
            + len(self.link_updates)
            + len(self.link_replay_noops)
            + len(self.inconsistent)
        )

    @property
    def total_would_insert(self) -> int:
        return len(self.event_inserts)

    @property
    def total_would_update(self) -> int:
        return len(self.link_updates)

    @property
    def total_replay_noop(self) -> int:
        return len(self.event_replay_noops) + len(self.link_replay_noops)

    @property
    def total_inconsistent(self) -> int:
        return len(self.inconsistent)

    @property
    def is_consistent(self) -> bool:
        """False when any assertion failed closed as inconsistent."""
        return not self.inconsistent

    def check_invariants(self) -> None:
        """Raise :class:`PlanInvariantError` if any invariant is broken."""
        for label, keys in (
            ("event_inserts", [a.key for a in self.event_inserts]),
            ("event_replay_noops", [a.key for a in self.event_replay_noops]),
            ("link_updates", [a.key for a in self.link_updates]),
            ("link_replay_noops", [a.key for a in self.link_replay_noops]),
            ("inconsistent", [a.key for a in self.inconsistent]),
        ):
            if len(keys) != len(set(keys)):
                raise PlanInvariantError(f"duplicate assertion in {label}")

        # One identity may not carry two different semantic payloads.
        payloads: dict[str, set[str]] = {}
        for assertion in (*self.event_inserts, *self.event_replay_noops):
            payloads.setdefault(assertion.identity.digest, set()).add(
                assertion.payload.signature
            )
        for digest, signatures in payloads.items():
            if len(signatures) > 1:
                raise PlanInvariantError(
                    f"event identity {digest} carries {len(signatures)} "
                    "conflicting payloads"
                )

        event_overlap = (
            {a.identity.digest for a in self.event_inserts}
            & {a.identity.digest for a in self.event_replay_noops}
        )
        if event_overlap:
            raise PlanInvariantError(
                f"event assertions classified twice: {sorted(event_overlap)!r}"
            )

        link_overlap = (
            {a.key for a in self.link_updates}
            & {a.key for a in self.link_replay_noops}
        )
        if link_overlap:
            raise PlanInvariantError(
                f"link assertions classified twice: {sorted(link_overlap)!r}"
            )

        # An inconsistent link may not also be planned as an update or replay.
        inconsistent = {a.key for a in self.inconsistent}
        for label, items in (
            ("link_updates", self.link_updates),
            ("link_replay_noops", self.link_replay_noops),
        ):
            clash = inconsistent & {a.key for a in items}
            if clash:
                raise PlanInvariantError(
                    f"inconsistent assertions also present in {label}: "
                    f"{sorted(clash)!r}"
                )

        if self.total_proposed != (
            self.total_would_insert
            + self.total_would_update
            + self.total_replay_noop
            + self.total_inconsistent
        ):
            raise PlanInvariantError(
                f"proposed {self.total_proposed} != would_insert "
                f"{self.total_would_insert} + would_update "
                f"{self.total_would_update} + replay_noop "
                f"{self.total_replay_noop} + inconsistent {self.total_inconsistent}"
            )


def _index_snapshots(
    existing_events: Iterable[ExistingNormalizedEvent] | None,
) -> dict[int, ExistingNormalizedEvent]:
    """Index stored snapshots by integer extraction row ID, rejecting duplicates."""
    snapshots: dict[int, ExistingNormalizedEvent] = {}
    for snapshot in existing_events or ():
        if not isinstance(snapshot, ExistingNormalizedEvent):
            raise PlanInvariantError(
                "existing_events must contain ExistingNormalizedEvent snapshots, "
                f"got {type(snapshot).__name__}"
            )
        if snapshot.extraction_id in snapshots:
            raise PlanInvariantError(
                f"duplicate stored snapshot for extraction row "
                f"{snapshot.extraction_id}"
            )
        snapshots[snapshot.extraction_id] = snapshot
    return snapshots


def _resolve_candidates(
    candidates: Iterable[NormalizationCandidate],
) -> dict[int, tuple[NormalizationCandidate, EventAssertion, ExtractionLinkAssertion]]:
    """Collapse candidates by extraction row ID, rejecting conflicting payloads."""
    resolved: dict[
        int, tuple[NormalizationCandidate, EventAssertion, ExtractionLinkAssertion]
    ] = {}
    for candidate in candidates:
        event = EventAssertion.from_candidate(candidate)
        link = ExtractionLinkAssertion.from_candidate(candidate)
        prior = resolved.get(candidate.extraction_id)
        if prior is None:
            resolved[candidate.extraction_id] = (candidate, event, link)
            continue
        if prior[1].key != event.key:
            raise PlanInvariantError(
                f"extraction row {candidate.extraction_id} has conflicting "
                "candidate payloads; refusing to collapse them"
            )
    return resolved


def build_classification_plan(
    candidates: Iterable[NormalizationCandidate],
    existing_events: Iterable[ExistingNormalizedEvent] | None = None,
) -> ClassificationPlan:
    """Classify candidates into event and extraction-link assertions.

    ``existing_events`` is a typed snapshot of what is currently stored for
    already-linked extractions.  An assertion whose stored row mismatches the
    candidate on any semantic field fails closed as inconsistent; only an exact
    field match is classified as a replay.  A bare digest mapping is not
    accepted, because a digest alone is not proof that the stored row agrees.
    """
    snapshots = _index_snapshots(existing_events)
    resolved = _resolve_candidates(candidates)

    event_inserts: dict[tuple[str, str], EventAssertion] = {}
    event_replays: dict[tuple[str, str], EventAssertion] = {}
    link_updates: dict[str, ExtractionLinkAssertion] = {}
    link_replays: dict[str, ExtractionLinkAssertion] = {}
    inconsistent: dict[str, InconsistentAssertion] = {}

    for extraction_id, (candidate, event, link) in resolved.items():
        snapshot = snapshots.get(extraction_id)

        if snapshot is None:
            # Unlinked: the event is created together with its extraction link.
            event_inserts.setdefault(event.key, event)
            link_updates.setdefault(link.key, link)
            continue

        mismatches = snapshot.compare_to(candidate)
        if not mismatches:
            # Exact stored match: revalidate the identity from current evidence.
            event_replays.setdefault(event.key, event)
            link_replays.setdefault(link.key, link)
            continue

        inconsistent.setdefault(
            link.key,
            InconsistentAssertion.from_mismatches(
                candidate, link, snapshot, mismatches
            ),
        )

    plan = ClassificationPlan(
        event_inserts=tuple(sorted(event_inserts.values(), key=_event_key)),
        event_replay_noops=tuple(sorted(event_replays.values(), key=_event_key)),
        link_updates=tuple(sorted(link_updates.values(), key=_link_key)),
        link_replay_noops=tuple(sorted(link_replays.values(), key=_link_key)),
        inconsistent=tuple(sorted(inconsistent.values(), key=_link_key)),
    )
    plan.check_invariants()
    return plan


def _event_key(assertion: EventAssertion) -> str:
    digest, signature = assertion.key
    return f"{digest}\x00{signature}"


def _link_key(assertion) -> str:
    return assertion.key
