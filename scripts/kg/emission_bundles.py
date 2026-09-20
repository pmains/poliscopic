"""Typed emission bundles and their completeness rules (Brief 018 Step 4).

Bundles reuse the Stage 3 identity system (:mod:`scripts.kg.identity`) rather
than defining a second evidence-identity model.  Identity fields carry typed
:class:`IdentityKey` values whose *kind* is validated, so an entity candidate
cannot stand in for an evidence occurrence.

Assertion classes make different demands:

===============  ==========================================================
source_supported  evidence identity
human_validated   evidence identity + adjudicator, decision id, decided_at
derived           typed, nonempty derived input identities; no direct evidence
quarantined       evidence identity retained + a *registered* quarantine reason
===============  ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from scripts.kg import registries as r
from scripts.kg.emission_models import EmissionError
from scripts.kg.identity import IdentityKey
from scripts.kg.registries.evidence import ASSERTION_CLASSES

#: Per-kind value categories a bundle must carry.
BUNDLE_VALUE_CATEGORIES: Mapping[str, tuple[str, ...]] = {
    "mention": ("entity_type", "role"),
    "relationship": ("relationship",),
    "event": ("event_type",),
    "participation": ("role", "participation_basis"),
}

#: Kind -> identities (and classes) that must be present for a complete assertion.
BUNDLE_REQUIRED_IDENTITIES: Mapping[str, tuple[str, ...]] = {
    "mention": ("context_identity",),
    "relationship": ("from_identity", "to_identity", "from_class", "to_class"),
    "event": ("context_identity",),
    "participation": ("actor_identity", "context_identity"),
}

#: Identity field -> the identity kinds that field may legitimately hold.
IDENTITY_KIND_REQUIREMENTS: Mapping[str, frozenset[str]] = {
    "evidence_identity": frozenset({"evidence"}),
    "context_identity": frozenset(
        {"context", "event", "evidence", "canonical_entity", "entity_candidate"}
    ),
    "adjudication_identity": frozenset({"adjudication"}),
    "actor_identity": frozenset({"canonical_entity", "entity_candidate"}),
    "from_identity": frozenset({"canonical_entity", "entity_candidate"}),
    "to_identity": frozenset({"canonical_entity", "entity_candidate"}),
}

#: Registered context class -> the identity kinds that class requires.
#: An evidence key with context_class="agenda_item" must fail: an agenda item is
#: a civic context, not an evidence occurrence.
CONTEXT_CLASS_IDENTITY_KINDS: Mapping[str, frozenset[str]] = {
    "evidence": frozenset({"evidence"}),
    "event": frozenset({"event"}),
    "agreement_event": frozenset({"event", "context"}),
    "meeting": frozenset({"context"}),
    "agenda_item": frozenset({"context"}),
    "agenda_subitem": frozenset({"context"}),
    "body": frozenset({"context"}),
    "jurisdiction": frozenset({"context"}),
    "case": frozenset({"canonical_entity", "entity_candidate"}),
    "parcel": frozenset({"canonical_entity", "entity_candidate"}),
}

#: Rendered by the identity layer when an optional part is absent.
_ABSENT = "<none>"


def _identity_part(key: IdentityKey, name: str) -> str | None:
    """Return one rendered part of an identity key, or None when absent."""
    for part_name, value in key.parts:
        if part_name == name:
            return None if value == _ABSENT else value
    return None

#: Assertion classes that must cite evidence rather than derived inputs.
EVIDENCED_ASSERTION_CLASSES: tuple[str, ...] = (
    "source_supported", "human_validated", "quarantined",
)


@dataclass(frozen=True)
class EmissionBundle:
    """A complete assertion's worth of registry-bearing values.

    Identity fields hold typed :class:`IdentityKey` values from the Stage 3
    identity system; their kinds are validated.  ``source_reference`` is gone:
    a bundle's source presentation is derived from its evidence key.
    """

    kind: str
    assertion_class: str = "source_supported"
    model_version: str = r.MODEL_VERSION
    evidence_class: str | None = None

    evidence_identity: IdentityKey | None = None
    adjudication_identity: IdentityKey | None = None
    quarantine_reason: str | None = None

    entity_type: str | None = None
    role: str | None = None
    relationship: str | None = None
    event_type: str | None = None
    outcome: str | None = None
    outcome_qualifier: str | None = None
    participation_basis: str | None = None

    context_class: str | None = None
    context_identity: IdentityKey | None = None
    actor_identity: IdentityKey | None = None
    from_class: str | None = None
    to_class: str | None = None
    from_identity: IdentityKey | None = None
    to_identity: IdentityKey | None = None

    promotion: str | None = None
    derived_inputs: tuple[IdentityKey, ...] = ()
    meta: Mapping[str, str] = field(default_factory=dict)


def required_bundle_categories(kind: str, assertion_class: str) -> tuple[str, ...]:
    """Registry categories a complete bundle of ``kind`` must validate."""
    if kind not in BUNDLE_VALUE_CATEGORIES:
        raise EmissionError(f"unknown emission bundle kind {kind}")
    categories = list(BUNDLE_VALUE_CATEGORIES[kind])
    categories += ["assertion_class", "model_version"]
    if assertion_class == "derived":
        # Only genuinely computed assertions declare inputs and cite no evidence.
        # A quarantined assertion is *evidenced*: it is set aside, not derived.
        categories.append("derived_inputs")
    else:
        categories.append("evidence_class")
    return tuple(categories)


def bundle_source_reference(bundle: EmissionBundle) -> str | None:
    """Presentation-only source reference derived from the evidence key."""
    if bundle.evidence_identity is None:
        if bundle.derived_inputs:
            return "derived(" + ",".join(k.digest[:12] for k in bundle.derived_inputs) + ")"
        return None
    return bundle.evidence_identity.canonical


def _identity_problems(bundle: EmissionBundle) -> list[str]:
    """Assertion-class provenance and typed identity-kind requirements."""
    problems: list[str] = []
    assertion_class = bundle.assertion_class

    if assertion_class in EVIDENCED_ASSERTION_CLASSES:
        if bundle.evidence_identity is None:
            problems.append(
                f"{assertion_class} bundle requires an evidence identity"
            )
    if assertion_class == "human_validated":
        if bundle.adjudication_identity is None:
            problems.append(
                "human_validated bundle requires an adjudication identity"
            )
    if assertion_class == "derived":
        if bundle.evidence_identity is not None or bundle.evidence_class:
            problems.append("derived bundle must not claim direct evidence support")
        if not bundle.derived_inputs:
            problems.append("derived bundle must declare derived_inputs")
    if assertion_class == "quarantined":
        if not bundle.quarantine_reason:
            problems.append("quarantined bundle requires a quarantine_reason")
        elif bundle.quarantine_reason not in r.QUARANTINE_REASONS:
            problems.append(
                f"quarantine reason {bundle.quarantine_reason} is not registered "
                f"(known: {', '.join(sorted(r.QUARANTINE_REASONS))})"
            )

    for field_name, allowed in IDENTITY_KIND_REQUIREMENTS.items():
        key = getattr(bundle, field_name, None)
        if key is None:
            continue
        if not isinstance(key, IdentityKey):
            problems.append(
                f"{field_name} must be a typed identity key, "
                f"not {type(key).__name__}"
            )
        elif key.kind not in allowed:
            problems.append(
                f"{field_name} must be a {' or '.join(sorted(allowed))} identity, "
                f"got {key.kind}"
            )

    for index, key in enumerate(bundle.derived_inputs):
        if not isinstance(key, IdentityKey):
            problems.append(
                f"derived_inputs[{index}] must be a typed identity key, "
                f"not {type(key).__name__}"
            )

    # An evidence identity must pin a content version: an extraction method
    # alone cannot distinguish a changed document at the same source.
    if bundle.evidence_identity is not None and isinstance(
        bundle.evidence_identity, IdentityKey
    ):
        if _identity_part(bundle.evidence_identity, "content_hash") is None:
            problems.append(
                "evidence identity must pin a content version; an extraction "
                "method alone cannot distinguish changed content at one source"
            )
    return problems


def _outcome_problems(bundle: EmissionBundle) -> list[str]:
    """Canonical outcome contract: base and qualifier validated separately.

    The canonical representation is a base outcome plus an optional controlled
    qualifier.  A historical raw form is normalised here, then the base, the
    qualifier, their exact pairing, and outcome/event-type compatibility are all
    checked against the registries.
    """
    problems: list[str] = []
    declared = bundle.outcome_qualifier

    if not bundle.outcome:
        if declared:
            problems.append(
                f"outcome qualifier {declared!r} was supplied without a base "
                "outcome"
            )
        return problems

    try:
        canonical = r.canonicalize_outcome(bundle.outcome)
    except r.OutcomeError as error:
        problems.append(str(error))
        return problems

    if (
        canonical.qualifier is not None
        and declared is not None
        and declared != canonical.qualifier
    ):
        problems.append(
            f"outcome {bundle.outcome!r} canonicalises to base {canonical.base} "
            f"with qualifier {canonical.qualifier!r}, which contradicts the "
            f"declared qualifier {declared!r}"
        )
        return problems

    qualifier = declared if declared is not None else canonical.qualifier
    if qualifier is not None:
        if qualifier not in r.QUALIFIERS:
            problems.append(f"outcome qualifier {qualifier} is not registered")
            return problems
        allowed = tuple(r.OUTCOME_QUALIFIERS.get(canonical.base, ()))
        if qualifier not in allowed:
            problems.append(
                f"base outcome {canonical.base} does not permit qualifier "
                f"{qualifier} (allowed: {', '.join(allowed) or 'none'})"
            )
            return problems

    if bundle.event_type:
        allowed_types = tuple(r.OUTCOME_EVENT_TYPES.get(canonical.base, ()))
        event_type = r.normalize_event_slug(bundle.event_type)
        if allowed_types and event_type not in allowed_types:
            problems.append(
                f"outcome {canonical.base} is not compatible with event type "
                f"{event_type} (allowed: {', '.join(allowed_types)})"
            )
    return problems


def _cross_field_problems(bundle: EmissionBundle) -> list[str]:
    """Relationships between fields that must agree with the registries."""
    problems: list[str] = []

    if bundle.kind == "event":
        problems.extend(_outcome_problems(bundle))

    if bundle.kind == "relationship" and bundle.relationship and bundle.evidence_class:
        entry = r.PREDICATES.get(bundle.relationship)
        if entry is not None and bundle.evidence_class not in entry.allowed_evidence_classes:
            problems.append(
                f"predicate {bundle.relationship} does not allow evidence class "
                f"{bundle.evidence_class} "
                f"(allowed: {', '.join(entry.allowed_evidence_classes)})"
            )

    if bundle.context_class and bundle.context_class not in r.CONTEXT_CLASSES:
        problems.append(
            f"context class {bundle.context_class} is not registered"
        )

    # A context class and its identity kind must agree.
    if bundle.context_class and isinstance(bundle.context_identity, IdentityKey):
        required = CONTEXT_CLASS_IDENTITY_KINDS.get(bundle.context_class)
        if required is not None and bundle.context_identity.kind not in required:
            problems.append(
                f"context class {bundle.context_class} requires a "
                f"{' or '.join(sorted(required))} identity, got "
                f"{bundle.context_identity.kind}"
            )
    return problems


def _completeness_problems(bundle: EmissionBundle) -> list[str]:
    """Kind-specific completeness, including the anti-bypass rules."""
    problems: list[str] = []
    for category in required_bundle_categories(bundle.kind, bundle.assertion_class):
        if category == "derived_inputs":
            if not bundle.derived_inputs:
                problems.append("derived bundle is missing derived_inputs")
            continue
        value = getattr(bundle, category, None)
        if value in (None, "", ()):
            problems.append(f"{bundle.kind} bundle is missing {category}")

    derived = bundle.assertion_class == "derived"
    for identity in BUNDLE_REQUIRED_IDENTITIES.get(bundle.kind, ()):
        if getattr(bundle, identity, None) in (None, ""):
            problems.append(f"{bundle.kind} bundle is missing {identity}")

    # Endpoint classes and context may not be omitted to dodge their checks.
    if bundle.kind == "relationship" and not derived:
        if not bundle.from_class or not bundle.to_class:
            problems.append(
                "relationship bundle must declare from_class and to_class so "
                "domain/range checks cannot be bypassed"
            )
    if bundle.kind in ("mention", "participation"):
        if bundle.context_identity is None:
            problems.append(
                f"{bundle.kind} bundle requires a typed context identity"
            )
        if not bundle.context_class:
            problems.append(
                f"{bundle.kind} bundle requires a registered context class so "
                "role/context checks cannot be bypassed"
            )

    if bundle.participation_basis and bundle.promotion:
        from scripts.kg.registries.roles import basis_forbids

        if basis_forbids(bundle.participation_basis, bundle.promotion):
            problems.append(
                f"basis {bundle.participation_basis} cannot support promotion "
                f"{bundle.promotion} without corroborating evidence"
            )
    return problems


def bundle_problems(bundle: EmissionBundle) -> list[str]:
    """Every structural, identity-kind, or cross-field reason a bundle fails."""
    problems: list[str] = []
    if bundle.assertion_class not in ASSERTION_CLASSES:
        problems.append(
            f"assertion class {bundle.assertion_class} is not registered"
        )
        return problems
    if bundle.kind not in BUNDLE_VALUE_CATEGORIES:
        return problems + [f"unknown emission bundle kind {bundle.kind}"]
    problems += _identity_problems(bundle)
    problems += _cross_field_problems(bundle)
    problems += _completeness_problems(bundle)
    return problems
