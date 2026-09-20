"""Compatibility facade for the emission boundary (Brief 018 Step 4).

The boundary is implemented across focused modules:

- :mod:`scripts.kg.emission_models` — states, errors, categories, rejections
- :mod:`scripts.kg.emission_bundles` — typed bundles and completeness rules
- :mod:`scripts.kg.emission_receipts` — receipts and reconciliation
- :mod:`scripts.kg.emission_validation` — the validator and lifecycle

This facade re-exports the public surface so existing imports keep working.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from scripts.kg.emission_bundles import (
    BUNDLE_REQUIRED_IDENTITIES,
    BUNDLE_VALUE_CATEGORIES,
    CONTEXT_CLASS_IDENTITY_KINDS,
    EVIDENCED_ASSERTION_CLASSES,
    IDENTITY_KIND_REQUIREMENTS,
    EmissionBundle,
    bundle_problems,
    bundle_source_reference,
    required_bundle_categories,
)
from scripts.kg.emission_models import (
    CATEGORIES,
    PROHIBITED_ENTITY_TYPES,
    PROHIBITED_PREDICATES,
    STATE_COLLECTING,
    STATE_FAILED,
    STATE_SEALED,
    STATE_VALIDATION_COMPLETE,
    STATE_WRITING,
    STATES,
    BoundaryStateError,
    EmissionError,
    Rejection,
)
from scripts.kg.emission_receipts import ValidationReceipt, reconcile_receipts
from scripts.kg.emission_validation import EmissionValidator, emit_validated

__all__ = [
    "BUNDLE_REQUIRED_IDENTITIES",
    "BUNDLE_VALUE_CATEGORIES",
    "BoundaryStateError",
    "CATEGORIES",
    "CONTEXT_CLASS_IDENTITY_KINDS",
    "EVIDENCED_ASSERTION_CLASSES",
    "EmissionBundle",
    "EmissionError",
    "EmissionValidator",
    "IDENTITY_KIND_REQUIREMENTS",
    "PROHIBITED_ENTITY_TYPES",
    "PROHIBITED_PREDICATES",
    "Rejection",
    "STATES",
    "STATE_COLLECTING",
    "STATE_FAILED",
    "STATE_SEALED",
    "STATE_VALIDATION_COMPLETE",
    "STATE_WRITING",
    "ValidationReceipt",
    "bundle_problems",
    "bundle_source_reference",
    "emit_validated",
    "observe_emissions",
    "reconcile_receipts",
    "required_bundle_categories",
]


def observe_emissions(
    producer: str,
    producer_version: str,
    emissions: Mapping[str, Iterable[Any]],
    *,
    dry_run: bool = False,
    source: str = "unknown",
) -> ValidationReceipt:
    """Validate a batch of proposals and return its receipt (no writes)."""
    validator = EmissionValidator(producer, producer_version, dry_run=dry_run)
    for category, values in emissions.items():
        for value in values:
            validator.validate(category, value, source=source)
    validator.complete_validation()
    return validator.seal()
