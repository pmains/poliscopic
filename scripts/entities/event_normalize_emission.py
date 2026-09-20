"""event_normalize_emission.py — complete event EmissionBundle per candidate.

Builds the one complete ``kind="event"`` bundle that requirement 4 demands for
every candidate before any row classification or write, and reports exactly why
a bundle is incomplete.

No mapping is reproduced here
-----------------------------
Every ontology-bearing value is a projection of an already-canonicalized
:class:`~scripts.entities.event_normalize_models.NormalizationCandidate`:

* the event type is the canonical **leaf** the candidate resolved through the
  registry's dotted-slug compatibility contract -- a dotted database slug is never
  emitted as an event type, and nothing here maps a slug to a type;
* the outcome is the candidate's ``CanonicalOutcome``, carried as a base plus an
  optional qualifier, never recombined into a raw historical string;
* the evidence class came from the registry's extraction-method inventory;
* the evidence, extraction and meeting-context identities are the candidate's own
  typed keys, so an assertion cannot drift from the evidence it cites;
* the model version is the registry's, not a literal.

This module performs no I/O and holds no vocabulary table of its own; a source
scan in its tests enforces that.
"""

from __future__ import annotations

from scripts.kg.emission import EmissionBundle, bundle_problems
from scripts.kg.registries import MODEL_VERSION

from scripts.entities.event_normalize_models import NormalizationCandidate

__all__ = [
    "ASSERTION_CLASS",
    "BUNDLE_KIND",
    "CONTEXT_CLASS",
    "build_event_bundle",
    "bundle_value_pairs",
    "event_bundle_problems",
    "is_complete_event_bundle",
]

#: The bundle kind and assertion class for an extracted event.
BUNDLE_KIND = "event"
ASSERTION_CLASS = "source_supported"

#: A meeting event is scoped to the meeting it happened in.
CONTEXT_CLASS = "meeting"


def build_event_bundle(candidate: NormalizationCandidate) -> EmissionBundle:
    """Project one candidate into a complete event bundle."""
    return EmissionBundle(
        kind=BUNDLE_KIND,
        assertion_class=ASSERTION_CLASS,
        model_version=MODEL_VERSION,
        evidence_class=candidate.evidence_class,
        evidence_identity=candidate.evidence_identity,
        context_class=CONTEXT_CLASS,
        context_identity=candidate.meeting_identity,
        event_type=candidate.event_type,
        outcome=candidate.outcome.base,
        outcome_qualifier=candidate.outcome.qualifier,
    )


def event_bundle_problems(candidate: NormalizationCandidate) -> tuple[str, ...]:
    """Every reason this candidate's bundle is not a complete assertion."""
    return tuple(bundle_problems(build_event_bundle(candidate)))


def bundle_value_pairs(
    candidate: NormalizationCandidate,
) -> tuple[tuple[str, str], ...]:
    """The ``(category, value)`` pairs this bundle proposes to the registries.

    These are exactly the ontology-bearing values a receipt must account for, so
    the validator can accept or refuse each one by name.  A qualifier is proposed
    only when one exists, and the values are the bundle's own fields -- nothing is
    recomputed here.
    """
    bundle = build_event_bundle(candidate)
    pairs = [
        ("event_type", bundle.event_type),
        ("outcome", bundle.outcome),
        ("assertion_class", bundle.assertion_class),
        ("model_version", bundle.model_version),
    ]
    if bundle.outcome_qualifier:
        pairs.append(("outcome_qualifier", bundle.outcome_qualifier))
    if bundle.evidence_class:
        pairs.append(("evidence_class", bundle.evidence_class))
    return tuple(pairs)


def is_complete_event_bundle(candidate: NormalizationCandidate) -> bool:
    """Whether the candidate's bundle would validate."""
    return not event_bundle_problems(candidate)
