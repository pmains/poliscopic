"""Assertion layer over typed identity keys (Brief 018 Step 3).

Split from :mod:`scripts.kg.identity` so the key builders and the assertion
layer stay under the module size limit.  ``scripts.kg.identity`` re-exports both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from scripts.kg import registries as r
from scripts.kg.identity_keys import IdentityError, IdentityKey
from scripts.kg.registries.evidence import ASSERTION_CLASSES, is_source_supported
from scripts.kg.registries.roles import ROLES, basis_forbids, role_allows_context

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

_SOURCE_CLASSES: tuple[str, ...] = tuple(
    slug for slug in ASSERTION_CLASSES if is_source_supported(slug)
)


@dataclass(frozen=True)
class Assertion:
    """A typed assertion linking keys to evidence under an assertion class."""

    subject: IdentityKey
    predicate: str
    object: IdentityKey
    assertion_class: str
    evidence: tuple[IdentityKey, ...] = ()
    context: IdentityKey | None = None
    basis: str | None = None
    role: str | None = None
    promotion: str | None = None
    valid_time: str | None = None
    observed_at: str | None = None
    inputs: tuple[IdentityKey, ...] = ()

    @property
    def cites_source(self) -> bool:
        """Return True when the class may be presented as a source fact."""
        return self.assertion_class in _SOURCE_CLASSES


def assertion_problems(assertion: Assertion) -> list[str]:
    """Return every reason this assertion must not be published as-is."""
    problems: list[str] = []
    if assertion.assertion_class not in ASSERTION_CLASSES:
        problems.append(
            f"assertion class {assertion.assertion_class} is not registered"
        )
    if assertion.predicate not in r.PREDICATES:
        problems.append(f"predicate {assertion.predicate} is not canonical")

    if assertion.assertion_class in _SOURCE_CLASSES:
        if not assertion.evidence:
            problems.append(
                f"{assertion.assertion_class} assertion must cite at least one "
                "evidence key"
            )
        for key in assertion.evidence:
            if key.kind != "evidence":
                problems.append(
                    f"evidence entry {key.kind} is not an evidence key"
                )
    else:
        if assertion.evidence:
            problems.append(
                f"{assertion.assertion_class} assertion must not cite evidence "
                "as source support"
            )
        if not assertion.inputs:
            problems.append(
                f"{assertion.assertion_class} assertion must declare its inputs"
            )

    if not assertion.observed_at:
        # The observation clock is never implicit.  Without it an unknown
        # valid_time would read as a timeless fact.
        problems.append(
            "assertion requires observed_at so an unknown valid_time is scoped "
            "to an observation time rather than stated as timeless"
        )

    if assertion.basis is not None:
        if assertion.basis not in r.PARTICIPATION_BASES:
            problems.append(f"participation basis {assertion.basis} is not registered")
        elif assertion.promotion is not None and basis_forbids(
            assertion.basis, assertion.promotion
        ):
            problems.append(
                f"basis {assertion.basis} cannot support promotion "
                f"{assertion.promotion} without corroborating evidence"
            )

    if assertion.role is not None and assertion.role not in ROLES:
        problems.append(f"role {assertion.role} is not registered")

    return problems


def build_assertion(**kwargs: Any) -> Assertion:
    """Build and validate an assertion, raising on any violation."""
    assertion = Assertion(**kwargs)
    problems = assertion_problems(assertion)
    if problems:
        raise IdentityError("invalid assertion: " + "; ".join(problems))
    return assertion


def co_occurrence_assertion(
    subject: IdentityKey,
    predicate: str,
    object_: IdentityKey,
    *,
    context: IdentityKey,
    inputs: Iterable[IdentityKey],
    observed_at: str,
) -> Assertion:
    """Build a derived assertion for meeting-wide co-occurrence.

    Meeting-level co-occurrence is computed, never observed, so the result is
    tagged ``derived`` and can never be serialized as source-supported.
    """
    return build_assertion(
        subject=subject,
        predicate=predicate,
        object=object_,
        assertion_class="derived",
        context=context,
        inputs=tuple(inputs),
        observed_at=observed_at,
    )


def serialize_assertion(
    assertion: Assertion,
    *,
    as_source_supported: bool = False,
) -> dict[str, Any]:
    """Serialize an assertion, refusing to overstate its class.

    Passing ``as_source_supported=True`` for a derived or quarantined
    assertion raises rather than emitting a record that reads as a directly
    evidenced fact.
    """
    if as_source_supported and not assertion.cites_source:
        raise IdentityError(
            f"refusing to serialize a {assertion.assertion_class} assertion as "
            "source-supported"
        )
    problems = assertion_problems(assertion)
    if problems:
        raise IdentityError("invalid assertion: " + "; ".join(problems))
    return {
        "subject": assertion.subject.serialize(),
        "predicate": assertion.predicate,
        "object": assertion.object.serialize(),
        "assertion_class": assertion.assertion_class,
        "source_supported": assertion.cites_source,
        "label": (
            "inferred/derived" if assertion.assertion_class == "derived"
            else assertion.assertion_class
        ),
        "evidence": [key.serialize() for key in assertion.evidence],
        "inputs": [key.serialize() for key in assertion.inputs],
        "context": None if assertion.context is None else assertion.context.serialize(),
        "basis": assertion.basis,
        "role": assertion.role,
        "valid_time": assertion.valid_time,
        "observed_at": assertion.observed_at,
    }
