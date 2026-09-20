"""Pure per-category registry checks (Brief 018 Step 4).

Each check raises :class:`EmissionError` on refusal and records nothing.  They
are deliberately side-effect free so bundle validation can stage every component
before committing any of them.
"""

from __future__ import annotations

from typing import Any, Callable

from scripts.kg import registries as r
from scripts.kg.emission_models import (
    PROHIBITED_ENTITY_TYPES,
    PROHIBITED_PREDICATES,
    EmissionError,
)
from scripts.kg.registries.evidence import ASSERTION_CLASSES, NON_SOURCE_CLASSES


def _classify(category: str, value: Any) -> str:
    if value is None:
        raise EmissionError(f"{category} value is required")
    return r.classify_value(category, str(value))


def check_entity_type(value: Any, **_criteria: Any) -> str:
    text = str(value) if value is not None else ""
    if text in PROHIBITED_ENTITY_TYPES:
        raise EmissionError(
            f"entity type {text} is prohibited for new emission "
            "(historical rows remain readable)"
        )
    status = _classify("entity_type", value)
    if status in ("quarantined", "unmapped"):
        raise EmissionError(f"entity type {value} is {status}")
    return text


def check_role(
    value: Any,
    *,
    context_class: str | None = None,
    assertion_kind: str | None = None,
    **_criteria: Any,
) -> str:
    """Check a role, in an assertion-kind-aware way.

    A mention records that a source used a contextual label; it asserts no
    participation, so an explicitly extracted label may be retained in evidence
    context.  Any other assertion kind uses the stricter participation rule, so
    the mention permission never widens participation.
    """
    status = _classify("role", value)
    if status in ("quarantined", "unmapped"):
        raise EmissionError(f"role {value} is {status}")
    text = str(value)
    if context_class is None:
        return text
    if assertion_kind == "mention":
        allowed = r.role_allows_mention_context(text, context_class)
    else:
        allowed = r.role_allows_context(text, context_class)
    if not allowed:
        suffix = " for a mention" if assertion_kind == "mention" else ""
        raise EmissionError(
            f"role {text} is not permitted in context {context_class}{suffix}"
        )
    return text


def check_participation_basis(value: Any, **_criteria: Any) -> str:
    text = str(value) if value is not None else ""
    if text not in r.PARTICIPATION_BASES:
        raise EmissionError(f"participation basis {value} is not registered")
    return text


def check_relationship(
    value: Any,
    *,
    from_class: str | None = None,
    to_class: str | None = None,
    **_criteria: Any,
) -> str:
    text = str(value) if value is not None else ""
    if text in PROHIBITED_PREDICATES:
        raise EmissionError(
            f"predicate {text} is prohibited for new emission "
            "(historical edges remain readable)"
        )
    if text not in r.PREDICATES:
        raise EmissionError(
            f"predicate {text} is not canonical "
            f"({_classify('relationship', value)}); producers must emit canonical "
            "predicates"
        )
    if (from_class is None) != (to_class is None):
        raise EmissionError("relationship check needs both from_class and to_class")
    if from_class is not None and to_class is not None:
        if not r.direction_allows(text, from_class, to_class):
            raise EmissionError(
                f"predicate {text} does not allow {from_class} -> {to_class}"
            )
    return text


def check_event_type(value: Any, **_criteria: Any) -> str:
    text = r.normalize_event_slug(str(value)) if value is not None else ""
    if text not in r.EVENT_TYPES:
        raise EmissionError(f"event type {value} is not registered")
    return text


def check_outcome(value: Any, **_criteria: Any) -> str:
    """Return the canonical BASE outcome for a raw outcome value.

    Historical qualified storage forms normalise to their base here; the
    qualifier is validated and recorded separately, so a qualified outcome is
    never certified as ``approved_with_conditions``.
    """
    text = str(value) if value is not None else ""
    if not text:
        raise EmissionError("outcome value is empty")
    # Canonicalise first: a recognisable ``<base>_<qualifier>`` shape that the
    # registry does not permit must report the pairing fault, not a bare
    # "unmapped".  Classification then runs on the canonical base.
    try:
        canonical = r.canonicalize_outcome(text)
    except r.OutcomeError as error:
        raise EmissionError(str(error)) from error
    status = _classify("outcome", canonical.base)
    if status in ("quarantined", "unmapped"):
        raise EmissionError(f"outcome {canonical.base} is {status}")
    return canonical.base


def check_outcome_qualifier(value: Any, **_criteria: Any) -> str:
    """Validate one controlled qualifier independently of any base outcome.

    Pairing against the base is enforced by the bundle, which validates the two
    fields together; this check establishes that the qualifier itself is
    registered.
    """
    text = str(value) if value is not None else ""
    if not text:
        raise EmissionError("outcome qualifier value is empty")
    if text not in r.QUALIFIERS:
        raise EmissionError(f"outcome qualifier {value} is not registered")
    return text


def check_evidence_class(value: Any, **_criteria: Any) -> str:
    text = str(value) if value is not None else ""
    if text not in r.EVIDENCE_CLASSES:
        raise EmissionError(f"evidence class {value} is not registered")
    return text


def check_assertion_class(
    value: Any, *, source_supported: bool = False, **_criteria: Any,
) -> str:
    text = str(value) if value is not None else ""
    if text not in ASSERTION_CLASSES:
        raise EmissionError(f"assertion class {value} is not registered")
    if source_supported and text in NON_SOURCE_CLASSES:
        raise EmissionError(
            f"assertion class {text} must not be emitted as source-supported"
        )
    return text


def check_model_version(value: Any, **_criteria: Any) -> str:
    text = str(value) if value is not None else ""
    if text != r.MODEL_VERSION:
        raise EmissionError(
            f"model version {text or '<missing>'} does not match {r.MODEL_VERSION}"
        )
    return text


CHECKS: dict[str, Callable[..., str]] = {
    "entity_type": check_entity_type,
    "role": check_role,
    "participation_basis": check_participation_basis,
    "relationship": check_relationship,
    "event_type": check_event_type,
    "outcome": check_outcome,
    "outcome_qualifier": check_outcome_qualifier,
    "evidence_class": check_evidence_class,
    "assertion_class": check_assertion_class,
    "model_version": check_model_version,
}


def check(category: str, value: Any, **criteria: Any) -> str:
    """Run one pure category check, raising on failure and recording nothing."""
    handler = CHECKS.get(category)
    if handler is None:
        raise EmissionError(f"unknown validation category {category}")
    return handler(value, **criteria)
