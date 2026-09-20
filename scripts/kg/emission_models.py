"""Shared vocabulary and errors for the emission boundary (Brief 018 Step 4)."""

from __future__ import annotations

from dataclasses import dataclass


class EmissionError(ValueError):
    """Raised when a proposed emission is not registrable."""


class BoundaryStateError(EmissionError):
    """Raised when the lifecycle is violated (for example, writing early)."""


#: Validator lifecycle states.
STATE_COLLECTING = "collecting"
STATE_VALIDATION_COMPLETE = "validation_complete"
STATE_WRITING = "writing"
STATE_SEALED = "sealed"
STATE_FAILED = "failed"

STATES: tuple[str, ...] = (
    STATE_COLLECTING,
    STATE_VALIDATION_COMPLETE,
    STATE_WRITING,
    STATE_SEALED,
    STATE_FAILED,
)

#: Registry categories the boundary can classify individually.
CATEGORIES: tuple[str, ...] = (
    "entity_type", "role", "participation_basis", "relationship", "event_type",
    "outcome", "outcome_qualifier", "evidence_class", "assertion_class",
    "model_version",
)

#: Entity types that must not be emitted again.  Existing rows are untouched.
PROHIBITED_ENTITY_TYPES: tuple[str, ...] = ("recommendation",)

#: Predicates that must not be emitted again.  Existing edges are untouched.
PROHIBITED_PREDICATES: tuple[str, ...] = ("HAS_RECOMMENDATION",)


@dataclass(frozen=True)
class Rejection:
    """One refused emission, with enough identity to find the source row."""

    category: str
    value: str
    reason: str
    source: str

    def serialize(self) -> dict[str, str]:
        return {
            "category": self.category,
            "value": self.value,
            "reason": self.reason,
            "source": self.source,
        }
