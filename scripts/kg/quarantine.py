#!/usr/bin/env python3
"""``quarantine.py`` — the bounded Stage 1 quarantine contract (pure).

Quarantine is the modelled way to exclude evidence from ordinary normalization
without deleting or reassigning it.  This module owns the *semantics*; the DDL
lives in :mod:`scripts.db.quarantine_schema`.

What the Stage 1 contract actually requires
-------------------------------------------
The authoritative registries already define both halves of this:

* ``registries.evidence.ASSERTION_CLASSES['quarantined']`` — "Unmappable,
  conflicting, or insufficiently supported", whose public behaviour is
  "Excluded from ordinary knowledge queries".
* ``registries.compatibility.QUARANTINE_REASONS`` — the closed set of reason
  slugs a quarantine may cite.

So a quarantine must carry (a) a reason drawn from that registry and (b) the
exclusion behaviour.  Field-set evaluation against the contract:

* ``quarantine_reason`` — **required**, validated against the registry.
* ``quarantined_at`` — **required**, so the exclusion is auditable in time.
* ``quarantined_by`` / ``decision_id`` — **required when a human decides**.
  ``ASSERTION_CLASSES['human_validated']`` is "Human accepted a candidate against
  cited evidence", presented "with review provenance"; a human quarantine is the
  same review act, so provenance is mandatory for it.  Both stay nullable for
  purely mechanical quarantines, which have no human reviewer to name.
* ``model_version`` — **required**, and derived rather than assumed: the
  system-wide assertion vocabulary carries it (``emission_models.CATEGORIES``
  includes ``model_version``; ``registries.MODEL_VERSION`` is the current value),
  so a quarantine must record which model/registry produced the decision.

The human-decision fields mirror the contract's typed adjudication identity
(``identity_keys.adjudication_identity(adjudicator, decision_id, decided_at)``):
``quarantined_by`` is the adjudicator, ``decision_id`` the decision, and
``quarantined_at`` the decided-at timestamp.

Nothing in this module writes to a database.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg.registries.compatibility import QUARANTINE_REASONS  # noqa: E402

__all__ = [
    "QUARANTINED_ASSERTION_CLASS",
    "QUARANTINE_COLUMNS",
    "QUARANTINE_REASONS",
    "QUARANTINE_SELECTION_CLAUSE",
    "QuarantineError",
    "QuarantineState",
    "is_quarantined_row",
    "quarantine_reason_slugs",
    "quarantine_selection_clause",
    "validate_quarantine_reason",
]

#: The assertion class a quarantined row carries.
QUARANTINED_ASSERTION_CLASS = "quarantined"

#: Columns the quarantine state occupies.  ``reason``/``at`` are mandatory;
#: ``by``/``decision_id`` are mandatory only for human decisions.
QUARANTINE_COLUMNS = (
    "quarantine_reason",
    "quarantined_at",
    "quarantined_by",
    "decision_id",
    "model_version",
)

#: The single exclusion predicate.  Both read modes use it, so force mode cannot
#: resurrect evidence that was explicitly quarantined.
QUARANTINE_SELECTION_CLAUSE = "e.quarantined_at IS NULL"


class QuarantineError(ValueError):
    """The quarantine state is invalid, incomplete, or cites an unknown reason."""


def quarantine_reason_slugs() -> tuple[str, ...]:
    """The closed set of registry-approved quarantine reason slugs."""
    return tuple(sorted(QUARANTINE_REASONS))


def validate_quarantine_reason(reason: str | None) -> str:
    """Return a registry-approved reason slug, or raise.

    Reuses the authoritative registry: an arbitrary or copied string is refused
    rather than accepted as a free-text reason.
    """
    if reason is None or not str(reason).strip():
        raise QuarantineError("a quarantine reason is required")
    slug = str(reason).strip()
    if slug not in QUARANTINE_REASONS:
        raise QuarantineError(
            f"unknown quarantine reason {slug!r}; approved reasons are "
            f"{list(quarantine_reason_slugs())}"
        )
    return slug


@dataclass(frozen=True)
class QuarantineState:
    """The recorded reason a row is excluded from ordinary normalization."""

    reason: str
    at: datetime | None = None
    by: str | None = None
    decision_id: str | None = None
    model_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", validate_quarantine_reason(self.reason))
        if self.model_version is None or not str(self.model_version).strip():
            raise QuarantineError(
                "model_version is required on a quarantine record "
                "(the assertion vocabulary carries it)"
            )
        if self.by is not None and not str(self.by).strip():
            raise QuarantineError("quarantined_by is blank")
        if self.decision_id is not None and not str(self.decision_id).strip():
            raise QuarantineError("decision_id is blank")
        # Provenance belongs together: a human decision names both who and which.
        if (self.by is None) != (self.decision_id is None):
            raise QuarantineError(
                "a human quarantine must record both quarantined_by and decision_id"
            )

    @property
    def is_human_decision(self) -> bool:
        return self.by is not None and self.decision_id is not None

    @property
    def assertion_class(self) -> str:
        return QUARANTINED_ASSERTION_CLASS

    def values(self, *, now: datetime | None = None) -> dict[str, Any]:
        """The column values to persist for this quarantine."""
        return {
            "quarantine_reason": self.reason,
            "quarantined_at": self.at or now or datetime.now(timezone.utc),
            "quarantined_by": self.by,
            "decision_id": self.decision_id,
            "model_version": self.model_version,
        }


def quarantine_selection_clause(alias: str = "e") -> str:
    """The exclusion predicate, optionally for a specific table alias."""
    if alias == "e":
        return QUARANTINE_SELECTION_CLAUSE
    return f"{alias}.quarantined_at IS NULL"


def is_quarantined_row(row: Mapping[str, Any]) -> bool:
    """Whether a stored row is quarantined (reason or timestamp present)."""
    if "quarantined_at" not in row and "quarantine_reason" not in row:
        return False
    return row.get("quarantined_at") is not None or bool(row.get("quarantine_reason"))
