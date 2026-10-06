"""Event taxonomy and outcome registries (KG-INFORMATION-MODEL.md §8).

Events carry a controlled type; results are a controlled *base outcome* plus
optional controlled qualifiers.  Outcomes are values, never entity nodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from scripts.kg.registries.model import frozen


@dataclass(frozen=True)
class EventTypeEntry:
    """One event-taxonomy value."""

    slug: str
    parent: str | None
    canonical_use: str


_RAW_EVENT_TYPES: tuple[EventTypeEntry, ...] = (
    EventTypeEntry("decision", None, "A decision-taking action"),
    EventTypeEntry("approval", "decision", "Motion or item approved"),
    EventTypeEntry("denial", "decision", "Motion or item denied"),
    EventTypeEntry("continuation", "decision", "Item continued, tabled, or deferred"),
    EventTypeEntry("legislation", None, "An action on legislation or an ordinance"),
    EventTypeEntry("adoption", "legislation", "Ordinance, code, or policy adopted"),
    EventTypeEntry("introduction", "legislation", "Legislation introduced"),
    EventTypeEntry("amendment", "legislation", "Legislation or motion amended"),
    EventTypeEntry("administration", None, "An administrative personnel action"),
    EventTypeEntry("appointment", "administration", "Appointment to a body or role"),
    EventTypeEntry("removal", "administration", "Removal from a body or role"),
    EventTypeEntry("resignation", "administration", "Resignation or vacancy"),
    EventTypeEntry("procedure", None, "A procedural or agenda action"),
    EventTypeEntry("discussion", "procedure", "Discussion or review item"),
    EventTypeEntry("public_hearing", "procedure", "Public hearing"),
    EventTypeEntry("executive_session", "procedure", "Executive session"),
    EventTypeEntry("receipt", "procedure", "Formal receipt or filing"),
)

EVENT_TYPES: Mapping[str, EventTypeEntry] = frozen(
    {entry.slug: entry for entry in _RAW_EVENT_TYPES}
)

#: Slug spellings used by the historical ``meeting_event_types`` seed table.
#: The table keys children by dotted path while ``event_type`` holds the leaf.
DB_SLUG_ALIASES: Mapping[str, str] = frozen({
    "decision.approval": "approval",
    "decision.denial": "denial",
    "decision.continuation": "continuation",
    "legislation.adoption": "adoption",
    "legislation.introduction": "introduction",
    "legislation.amendment": "amendment",
    "administration.appointment": "appointment",
    "administration.removal": "removal",
    "administration.resignation": "resignation",
    "procedure.discussion": "discussion",
    "procedure.public_hearing": "public_hearing",
    "procedure.executive_session": "executive_session",
    "procedure.receipt": "receipt",
})


def normalize_event_slug(slug: str) -> str:
    """Map a dotted seed-table slug to its canonical event-type slug."""
    return DB_SLUG_ALIASES.get(slug, slug)

#: Roots classify events; they are not emitted when a supported leaf is known.
EVENT_ROOTS: tuple[str, ...] = ("decision", "legislation", "administration", "procedure")

#: Fallback event type used when no supported leaf applies.
EVENT_FALLBACK_SLUG = "discussion"

BASE_OUTCOMES: tuple[str, ...] = (
    "adopted", "amended", "approved", "called_to_order", "continued", "deferred",
    "denied", "discussed", "extended", "introduced", "no_action", "no_response",
    "received", "reviewed", "sustained", "tabled", "vacated", "withdrawn",
)

QUALIFIERS: tuple[str, ...] = (
    "with_conditions", "with_stipulations", "without_prejudice", "as_amended",
    "subject_to",
)

#: Base outcome -> event types the outcome may describe.
OUTCOME_EVENT_TYPES: Mapping[str, tuple[str, ...]] = frozen({
    "adopted": ("adoption", "approval"),
    "amended": ("amendment", "approval"),
    "approved": ("approval", "adoption"),
    "called_to_order": ("discussion",),
    "continued": ("continuation",),
    "deferred": ("continuation",),
    "denied": ("denial",),
    "discussed": ("discussion", "public_hearing"),
    "extended": ("continuation",),
    "introduced": ("introduction",),
    "no_action": ("discussion",),
    "no_response": ("discussion",),
    "received": ("receipt",),
    "reviewed": ("discussion",),
    "sustained": ("approval", "denial"),
    "tabled": ("continuation",),
    "vacated": ("approval", "denial"),
    "withdrawn": ("continuation",),
})

#: Base outcome -> qualifiers the outcome may carry.
OUTCOME_QUALIFIERS: Mapping[str, tuple[str, ...]] = frozen({
    "approved": ("with_conditions", "with_stipulations", "as_amended", "subject_to"),
    "denied": ("without_prejudice",),
})

#: Historical raw outcome -> (base outcome, qualifier or None).
OUTCOME_COMPATIBILITY: Mapping[str, tuple[str, str | None]] = frozen({
    "approved_with_conditions": ("approved", "with_conditions"),
    "approved_with_stipulations": ("approved", "with_stipulations"),
    "approved_subject_to": ("approved", "subject_to"),
    "approved_as_amended": ("approved", "as_amended"),
    "denied_without_prejudice": ("denied", "without_prejudice"),
    "received_and_filed": ("received", None),
    "discussion_only": ("discussed", None),
    "preliminary_review": ("reviewed", None),
    "for_discussion": ("discussed", None),
    "discussion": ("discussed", None),
})

#: Qualifier -> historical raw forms that must resolve to it.
QUALIFIER_RAW_FORMS: Mapping[str, tuple[str, ...]] = frozen({
    "with_conditions": ("approved_with_conditions",),
    "with_stipulations": ("approved_with_stipulations",),
    "subject_to": ("approved_subject_to",),
    "as_amended": ("approved_as_amended",),
    "without_prejudice": ("denied_without_prejudice",),
})


def children_of(slug: str) -> tuple[str, ...]:
    """Return event-type slugs whose parent is ``slug``."""
    return tuple(sorted(
        entry.slug for entry in EVENT_TYPES.values() if entry.parent == slug
    ))


def is_leaf_event_type(slug: str) -> bool:
    """Return True when the event type is a leaf (emittable) value."""
    return slug in EVENT_TYPES and not children_of(slug)


def accepted_outcome_forms(base: str) -> tuple[str, ...]:
    """Return every raw outcome form that satisfies a base-outcome query.

    Searching ``approved`` therefore matches qualified approvals without the
    caller enumerating ``approved_with_conditions``.
    """
    forms = [base]
    for raw, (mapped_base, _qualifier) in OUTCOME_COMPATIBILITY.items():
        if mapped_base == base:
            forms.append(raw)
    return tuple(sorted(set(forms)))


def split_outcome(raw: str) -> tuple[str, str | None]:
    """Return (base, qualifier) for a raw outcome string.

    Lenient: an unknown form is returned unchanged rather than rejected.  Use
    :func:`canonicalize_outcome` where an unrecognised form must fail closed.
    """
    if raw in OUTCOME_COMPATIBILITY:
        return OUTCOME_COMPATIBILITY[raw]
    if raw in BASE_OUTCOMES:
        return raw, None
    return raw, None


class OutcomeError(ValueError):
    """Raised when a raw outcome cannot be canonicalised."""


@dataclass(frozen=True)
class CanonicalOutcome:
    """A canonical outcome: a base outcome plus an optional controlled qualifier.

    This is the representation producers and bundles carry.  A qualified outcome
    is never encoded as ``approved_with_conditions``, nor as a combined
    ``approved+with_conditions`` string: base and qualifier stay separate fields.

    Constructing an invalid combination raises, so an unpaired qualifier cannot
    exist as a ``CanonicalOutcome`` at all.
    """

    base: str
    qualifier: str | None = None

    def __post_init__(self) -> None:
        if self.base not in BASE_OUTCOMES:
            raise OutcomeError(f"unknown base outcome {self.base!r}")
        if self.qualifier is None:
            return
        if self.qualifier not in QUALIFIERS:
            raise OutcomeError(f"unknown outcome qualifier {self.qualifier!r}")
        allowed = OUTCOME_QUALIFIERS.get(self.base, ())
        if self.qualifier not in allowed:
            permitted = ", ".join(allowed) or "none"
            raise OutcomeError(
                f"base outcome {self.base} does not permit qualifier "
                f"{self.qualifier} (allowed: {permitted})"
            )

    @property
    def is_qualified(self) -> bool:
        return self.qualifier is not None

    def serialize(self) -> dict[str, str | None]:
        """Receipt-shaped pair: base and qualifier are recorded separately."""
        return {"outcome": self.base, "outcome_qualifier": self.qualifier}


def canonicalize_outcome(raw: str) -> CanonicalOutcome:
    """Return the typed canonical outcome for a raw outcome string.

    Accepts the canonical base outcomes and the historical qualified storage
    forms, and **fails closed** on an unknown form or on a base/qualifier
    combination the registry does not permit.  Producers consume this rather
    than reproducing compatibility logic.
    """
    text = "_".join(str(raw or "").split()).lower()
    if not text:
        raise OutcomeError("outcome value is empty")
    if text in OUTCOME_COMPATIBILITY:
        base, qualifier = OUTCOME_COMPATIBILITY[text]
        return CanonicalOutcome(base, qualifier)
    if text in BASE_OUTCOMES:
        return CanonicalOutcome(text, None)
    # A recognisable ``<base>_<qualifier>`` shape that has no historical raw form
    # still derives its pair here, so the pairing rule (not form recognition)
    # decides: constructing raises when the base does not permit the qualifier.
    for qualifier in QUALIFIERS:
        suffix = f"_{qualifier}"
        if not text.endswith(suffix):
            continue
        candidate_base = text[: -len(suffix)]
        if candidate_base in BASE_OUTCOMES:
            return CanonicalOutcome(candidate_base, qualifier)
    raise OutcomeError(f"unknown outcome form {raw!r}")
