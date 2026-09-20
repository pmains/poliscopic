"""Pure typed identity keys for knowledge-graph assertions (Brief 018 Step 3).

Every builder here is a pure function: no database, no filesystem, no clock.
Callers pass the observed time explicitly, so replaying identical inputs always
yields identical keys.

Identity is separated by *what makes two things the same*:

- an **evidence** key identifies a source occurrence, so two extractors finding
  the same occurrence agree on it;
- an **extraction** key adds the extractor, so those two findings stay distinct;
- **context** is part of every claim, event, participation, and vote key, so
  similar facts at different agenda items or meetings never collapse.

Assertion class is not decoration.  A derived or quarantined assertion cannot
be serialized as a source-supported fact, and a source-supported assertion must
cite at least one evidence key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from scripts.kg import registries as r
from scripts.kg.registries.evidence import (
    ASSERTION_CLASSES,
    NON_SOURCE_CLASSES,
    is_source_supported,
)
from scripts.kg.registries.roles import ROLES, basis_forbids, role_allows_context

#: Rendered for absent optional parts, so "unknown" is never an empty string.
_ABSENT = "<none>"


class IdentityError(ValueError):
    """Raised when a key or assertion would be ambiguous or unverifiable."""


def _scalar(value: Any) -> str:
    """Render one key part deterministically."""
    if value is None:
        return _ABSENT
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True)
class IdentityKey:
    """An immutable, deterministically serializable identity."""

    kind: str
    parts: tuple[tuple[str, str], ...]

    @property
    def canonical(self) -> str:
        """Return the canonical string form (stable key ordering)."""
        ordered = sorted(self.parts)
        joined = "|".join(f"{name}={value}" for name, value in ordered)
        return f"{self.kind}:{joined}"

    @property
    def digest(self) -> str:
        """Return the SHA-256 of :attr:`canonical`."""
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()

    def serialize(self) -> dict[str, Any]:
        """Return a plain, JSON-safe representation."""
        return {
            "kind": self.kind,
            "canonical": self.canonical,
            "digest": self.digest,
            "parts": {name: value for name, value in sorted(self.parts)},
        }

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.canonical


def _key(kind: str, **parts: Any) -> IdentityKey:
    """Build a key, rejecting unnamed or empty parts."""
    if not parts:
        raise IdentityError(f"{kind} key requires at least one part")
    return IdentityKey(
        kind=kind,
        parts=tuple((name, _scalar(value)) for name, value in parts.items()),
    )


def _require(value: Any, what: str) -> Any:
    """Require a present, non-empty value."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise IdentityError(f"{what} is required")
    return value


# ---------------------------------------------------------------------------
# Evidence and extraction
# ---------------------------------------------------------------------------


def evidence_identity(
    *,
    source_type: str,
    source_id: Any,
    span_start: int | None = None,
    span_end: int | None = None,
    content_hash: str | None = None,
    extraction_method: str | None = None,
    url: str | None = None,
) -> IdentityKey:
    """Identify a source occurrence — deliberately without the extractor.

    Two extractors reading the same occurrence of the same source version must
    produce the same evidence identity, which is why ``extractor`` is absent
    here and present in :func:`extraction_identity`.
    """
    _require(source_type, "evidence source_type")
    _require(source_id, "evidence source_id")
    # A URL is a locator and an extraction method is processing metadata; neither
    # is a content version.  Only a content hash identifies *what* was read, so
    # it is required.  The other two stay available as optional metadata.
    _require(content_hash, "evidence content_hash")
    if (span_start is None) != (span_end is None):
        raise IdentityError("evidence span requires both span_start and span_end")
    if span_start is not None and span_end < span_start:
        raise IdentityError("evidence span_end precedes span_start")
    return _key(
        "evidence",
        source_type=source_type,
        source_id=source_id,
        span_start=span_start,
        span_end=span_end,
        content_hash=content_hash,
        extraction_method=extraction_method,
        url=url,
    )


def extraction_identity(
    evidence: IdentityKey,
    *,
    extractor: str,
    extractor_version: str | None = None,
) -> IdentityKey:
    """Identify one extraction *act* over an evidence occurrence."""
    _require(extractor, "extractor")
    if evidence.kind != "evidence":
        raise IdentityError("extraction identity requires an evidence key")
    return _key(
        "extraction",
        evidence=evidence.digest,
        extractor=extractor,
        extractor_version=extractor_version,
    )


# ---------------------------------------------------------------------------
# Mentions, entities, claims
# ---------------------------------------------------------------------------


def mention_identity(
    evidence: IdentityKey,
    *,
    extractor: str,
    mention_text: str,
    span_start: int,
    span_end: int,
) -> IdentityKey:
    """Identify one mention of a surface form within an evidence occurrence."""
    _require(mention_text, "mention_text")
    if evidence.kind != "evidence":
        raise IdentityError("mention identity requires an evidence key")
    if span_end < span_start:
        raise IdentityError("mention span_end precedes span_start")
    return _key(
        "mention",
        evidence=evidence.digest,
        extractor=extractor,
        mention_text=mention_text,
        span_start=span_start,
        span_end=span_end,
    )


def entity_candidate_identity(
    *,
    entity_type: str,
    surface_form: str,
    jurisdiction: str | None = None,
    discriminator: str | None = None,
) -> IdentityKey:
    """Identify an unresolved entity candidate.

    Context (meeting, agenda item) is intentionally excluded: one case
    appearing at several meetings is one candidate, not several.
    """
    _require(entity_type, "entity_type")
    _require(surface_form, "surface_form")
    if entity_type not in r.ENTITY_TYPES:
        raise IdentityError(f"entity_type {entity_type} is not registered")
    return _key(
        "entity_candidate",
        entity_type=entity_type,
        surface_form=" ".join(surface_form.split()).casefold(),
        jurisdiction=jurisdiction,
        discriminator=discriminator,
    )


def claim_identity(
    subject: IdentityKey,
    predicate: str,
    object_: IdentityKey,
    *,
    context: IdentityKey | None = None,
) -> IdentityKey:
    """Identify a canonical claim, including the context it holds in."""
    if predicate not in r.PREDICATES:
        raise IdentityError(f"predicate {predicate} is not canonical")
    return _key(
        "claim",
        subject=subject.digest,
        predicate=predicate,
        object=object_.digest,
        context=None if context is None else context.digest,
    )


# ---------------------------------------------------------------------------
# Events, participation, votes
# ---------------------------------------------------------------------------


def event_identity(
    *,
    event_type: str,
    context: IdentityKey,
    occurrence: str,
    valid_time: str | None = None,
) -> IdentityKey:
    """Identify a meeting event within a bounded context."""
    if r.normalize_event_slug(event_type) not in r.EVENT_TYPES:
        raise IdentityError(f"event_type {event_type} is not registered")
    return _key(
        "event",
        event_type=r.normalize_event_slug(event_type),
        context=context.digest,
        occurrence=occurrence,
        valid_time=valid_time,
    )


def participation_identity(
    actor: IdentityKey,
    context: IdentityKey,
    *,
    role: str,
    basis: str,
) -> IdentityKey:
    """Identify one participation assertion.

    Basis is part of identity on purpose: agenda listing, scheduled role,
    observed attendance, and observed action are four different assertions
    about the same actor and context, and must not collapse into one.
    """
    if role not in ROLES:
        raise IdentityError(f"role {role} is not registered")
    if basis not in r.PARTICIPATION_BASES:
        raise IdentityError(f"participation basis {basis} is not registered")
    return _key(
        "participation",
        actor=actor.digest,
        context=context.digest,
        role=role,
        basis=basis,
    )


def vote_identity(
    *,
    context: IdentityKey,
    actor: IdentityKey | None,
    motion: str,
    occurrence: str,
    valid_time: str | None = None,
) -> IdentityKey:
    """Identify one vote.

    ``occurrence`` distinguishes a reconsidered or repeated vote on the same
    motion, so a later decision never overwrites an earlier one.
    """
    _require(motion, "motion")
    _require(occurrence, "vote occurrence")
    return _key(
        "vote",
        context=context.digest,
        actor=None if actor is None else actor.digest,
        motion=motion,
        occurrence=occurrence,
        valid_time=valid_time,
    )


def civic_context_identity(
    *,
    context_class: str,
    source_system: str,
    context_id: Any,
    parent: IdentityKey | None = None,
) -> IdentityKey:
    """Identify a bounded civic context (meeting, agenda item, body, case...).

    Context identity is its own kind so an agenda item cannot be confused with
    an evidence occurrence.  ``parent`` disambiguates item/subitem numbering,
    which is only meaningful relative to its container.
    """
    _require(context_class, "context_class")
    if context_class not in r.CONTEXT_CLASSES:
        raise IdentityError(f"context class {context_class} is not registered")
    _require(source_system, "context source_system")
    _require(context_id, "context_id")
    if parent is not None and parent.kind != "context":
        raise IdentityError(
            f"parent context identity must be a context key, got {parent.kind}"
        )
    return _key(
        "context",
        context_class=context_class,
        source_system=source_system,
        context_id=context_id,
        parent=None if parent is None else parent.digest,
    )


def canonical_entity_identity(*, entity_id: Any, entity_type: str) -> IdentityKey:
    """Identify an already-resolved entity by its canonical id.

    Graph producers holding canonical entity ids must not have to masquerade as
    unresolved entity candidates.
    """
    _require(entity_id, "entity_id")
    _require(entity_type, "entity_type")
    if entity_type not in r.ENTITY_TYPES:
        raise IdentityError(f"entity_type {entity_type} is not registered")
    return _key("canonical_entity", entity_id=entity_id, entity_type=entity_type)


def adjudication_identity(
    *, adjudicator: str, decision_id: str, decided_at: str,
) -> IdentityKey:
    """Identify a human adjudication decision.

    All three parts are required: a validation decision without who, which, or
    when is indistinguishable from an unreviewed candidate.
    """
    _require(adjudicator, "adjudicator")
    _require(decision_id, "decision_id")
    _require(decided_at, "decided_at")
    return _key(
        "adjudication",
        adjudicator=adjudicator,
        decision_id=decision_id,
        decided_at=decided_at,
    )


def assert_distinct(keys: Sequence[IdentityKey], what: str) -> None:
    """Raise when any two keys in ``keys`` are equal."""
    digests = [key.digest for key in keys]
    if len(set(digests)) != len(digests):
        raise IdentityError(f"{what} collapsed to fewer identities than inputs")
