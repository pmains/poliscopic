"""Versioned knowledge-graph registries (Brief 018 Step 1).

The bundle is code-owned, immutable, and deterministically serializable.  A
single ``MODEL_VERSION`` identifies every registry at once, so persisted
assertions can declare the model they were validated against.

Importing this package validates the bundle; an invalid registry raises
``RegistryError`` rather than loading silently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from scripts.kg.registries.compatibility import (
    CANONICAL_ENTITY_TYPES,
    CANONICAL_EVENT_TYPES,
    CANONICAL_OUTCOMES,
    CANONICAL_PREDICATES,
    CANONICAL_ROLES,
    CATEGORY_CANONICAL_VALUES,
    COMPATIBILITY_MAPPINGS,
    MAPPINGS_BY_KEY,
    MENTION_SOURCE_EXTRACTOR_PAIRS,
    QUARANTINE_REASONS,
    ROLE_HISTORICAL_VALUES,
    SOURCE_REFERENCE_TYPES,
    classify_value,
    coverage,
    missing_mappings,
)
from scripts.kg.registries.entity_taxonomy import (
    DB_SLUG_ALIASES,
    ENTITY_TYPES,
    FALLBACK_SLUG,
    LEGACY_PROHIBITED_TYPES,
    get_entity_type,
    is_leaf_compliant,
)
from scripts.kg.registries.evidence import (
    AGENDA_LINEAGE_STEPS,
    ASSERTION_CLASSES,
    DISTINCT_AGENDA_OBSERVATIONS,
    EVIDENCE_CLASSES,
    EXTRACTION_METHOD_EVIDENCE_CLASSES,
    NON_SOURCE_CLASSES,
    evidence_class_for_extraction_method,
)
from scripts.kg.registries.events import (
    BASE_OUTCOMES,
    CanonicalOutcome,
    DB_SLUG_ALIASES as EVENT_DB_SLUG_ALIASES,
    EVENT_ROOTS,
    EVENT_TYPES,
    OUTCOME_COMPATIBILITY,
    OUTCOME_EVENT_TYPES,
    OUTCOME_QUALIFIERS,
    QUALIFIERS,
    OutcomeError,
    accepted_outcome_forms,
    canonicalize_outcome,
    normalize_event_slug,
    split_outcome,
)
from scripts.kg.registries.model import CompatibilityMapping, RegistryError
from scripts.kg.registries.relationships import (
    EDGE_KINDS,
    HISTORICAL_PREDICATES,
    NODE_CLASSES,
    PREDICATES,
    direction_allows,
    evidence_allows,
    get_predicate,
)
from scripts.kg.registries.roles import (
    ACTOR_CLASSES,
    CONTEXT_CLASSES,
    FORBIDDEN_PROMOTIONS,
    PARTICIPATION_BASES,
    ROLES,
    basis_forbids,
    role_allows_context,
    role_allows_mention_context,
)
from scripts.kg.registries.temporal import (
    CLOCKS,
    CLOCK_BY_SLUG,
    Clock,
    ClockField,
    assert_temporal_contract,
    schema_mapping,
    temporal_contract,
    validate_temporal_contract,
)
from scripts.kg.registries.validation import (
    duplicate_inverse_labels,
    validate_registry_data,
)

MODEL_VERSION = "kg-model/1.0"

#: Historical/producer values that must each be mapped or quarantined.
REQUIRED_COMPATIBILITY_VALUES: Mapping[str, tuple[str, ...]] = {
    "entity_type": tuple(LEGACY_PROHIBITED_TYPES) + tuple(sorted(DB_SLUG_ALIASES)),
    "event_type": tuple(sorted(EVENT_DB_SLUG_ALIASES)),
    "relationship": HISTORICAL_PREDICATES,
    "role": ROLE_HISTORICAL_VALUES,
    "outcome": tuple(sorted(OUTCOME_COMPATIBILITY)),
}

__all__ = [
    "MODEL_VERSION",
    "CLOCKS",
    "CLOCK_BY_SLUG",
    "Clock",
    "ClockField",
    "assert_temporal_contract",
    "schema_mapping",
    "temporal_contract",
    "validate_temporal_contract",
    "RegistryError",
    "ACTOR_CLASSES",
    "AGENDA_LINEAGE_STEPS",
    "ASSERTION_CLASSES",
    "BASE_OUTCOMES",
    "CANONICAL_ENTITY_TYPES",
    "CANONICAL_EVENT_TYPES",
    "CANONICAL_OUTCOMES",
    "CANONICAL_PREDICATES",
    "CANONICAL_ROLES",
    "CATEGORY_CANONICAL_VALUES",
    "COMPATIBILITY_MAPPINGS",
    "CONTEXT_CLASSES",
    "DISTINCT_AGENDA_OBSERVATIONS",
    "EDGE_KINDS",
    "ENTITY_TYPES",
    "EVIDENCE_CLASSES",
    "EXTRACTION_METHOD_EVIDENCE_CLASSES",
    "EXTRACTION_METHOD_WRITERS",
    "FAILED_EXTRACTION_METHODS",
    "QUARANTINE_METHOD_PREFIX",
    "REJECT_METHOD_PREFIX",
    "evidence_class_for_extraction_method",
    "EVENT_ROOTS",
    "EVENT_TYPES",
    "FALLBACK_SLUG",
    "FORBIDDEN_PROMOTIONS",
    "CanonicalOutcome",
    "MAPPINGS_BY_KEY",
    "MENTION_SOURCE_EXTRACTOR_PAIRS",
    "NODE_CLASSES",
    "NON_SOURCE_CLASSES",
    "OUTCOME_COMPATIBILITY",
    "OUTCOME_EVENT_TYPES",
    "OUTCOME_QUALIFIERS",
    "OutcomeError",
    "PARTICIPATION_BASES",
    "PREDICATES",
    "QUALIFIERS",
    "QUARANTINE_REASONS",
    "ROLES",
    "SOURCE_REFERENCE_TYPES",
    "accepted_outcome_forms",
    "basis_forbids",
    "classify_value",
    "canonicalize_outcome",
    "coverage",
    "direction_allows",
    "duplicate_inverse_labels",
    "evidence_allows",
    "get_entity_type",
    "get_predicate",
    "is_leaf_compliant",
    "missing_mappings",
    "normalize_event_slug",
    "registry_bundle",
    "role_allows_context",
    "role_allows_mention_context",
    "snapshot_json",
    "snapshot_sha256",
    "split_outcome",
    "validate_registries",
]


def _serialize(value: Any) -> Any:
    """Return a JSON-ready canonical form for registry data."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def registry_bundle() -> dict[str, Any]:
    """Return the complete registry bundle as plain, deterministic data."""
    return {
        "model_version": MODEL_VERSION,
        "entity_taxonomy": {
            "types": {slug: _serialize(entry) for slug, entry in sorted(ENTITY_TYPES.items())},
            "fallback_slug": FALLBACK_SLUG,
            "legacy_prohibited": list(LEGACY_PROHIBITED_TYPES),
            "db_slug_aliases": dict(sorted(DB_SLUG_ALIASES.items())),
        },
        "event_taxonomy": {
            "types": {slug: _serialize(entry) for slug, entry in sorted(EVENT_TYPES.items())},
            "roots": list(EVENT_ROOTS),
        },
        "outcomes": {
            "base": list(BASE_OUTCOMES),
            "qualifiers": list(QUALIFIERS),
            "event_types": {k: list(v) for k, v in sorted(OUTCOME_EVENT_TYPES.items())},
            "qualifiers_by_outcome": {
                k: list(v) for k, v in sorted(OUTCOME_QUALIFIERS.items())
            },
            "historical": {
                raw: list(mapped) for raw, mapped in sorted(OUTCOME_COMPATIBILITY.items())
            },
        },
        "roles": {
            "actor_classes": list(ACTOR_CLASSES),
            "context_classes": list(CONTEXT_CLASSES),
            "entries": {slug: _serialize(entry) for slug, entry in sorted(ROLES.items())},
        },
        "participation": {
            "bases": {slug: _serialize(entry) for slug, entry in sorted(PARTICIPATION_BASES.items())},
            "forbidden_promotions": {
                k: list(v) for k, v in sorted(FORBIDDEN_PROMOTIONS.items())
            },
        },
        "relationships": {
            "node_classes": list(NODE_CLASSES),
            "edge_kinds": list(EDGE_KINDS),
            "predicates": {slug: _serialize(entry) for slug, entry in sorted(PREDICATES.items())},
            "historical_predicates": list(HISTORICAL_PREDICATES),
        },
        "evidence": {
            "evidence_classes": {
                slug: _serialize(entry) for slug, entry in sorted(EVIDENCE_CLASSES.items())
            },
            "assertion_classes": {
                slug: _serialize(entry) for slug, entry in sorted(ASSERTION_CLASSES.items())
            },
            "non_source_classes": list(NON_SOURCE_CLASSES),
            "agenda_lineage_steps": list(AGENDA_LINEAGE_STEPS),
            "distinct_agenda_observations": list(DISTINCT_AGENDA_OBSERVATIONS),
        },
        "temporal_contract": temporal_contract(),
        "compatibility": {
            "mappings": [
                _serialize(mapping)
                for mapping in sorted(
                    COMPATIBILITY_MAPPINGS,
                    key=lambda item: (item.category, item.historical_value),
                )
            ],
            "quarantine_reasons": {
                slug: _serialize(reason) for slug, reason in sorted(QUARANTINE_REASONS.items())
            },
            "source_reference_types": list(SOURCE_REFERENCE_TYPES),
            "mention_source_extractor_pairs": [list(pair) for pair in MENTION_SOURCE_EXTRACTOR_PAIRS],
        },
    }


def snapshot_json() -> str:
    """Return the bundle as canonical sorted JSON (stable across runs)."""
    return json.dumps(registry_bundle(), sort_keys=True, separators=(",", ":"))


def snapshot_sha256() -> str:
    """Return the SHA-256 of :func:`snapshot_json`."""
    return hashlib.sha256(snapshot_json().encode("utf-8")).hexdigest()


def validate_registries() -> None:
    """Validate the shipped bundle; raise ``RegistryError`` on any violation."""
    validate_registry_data(
        entity_types=ENTITY_TYPES,
        event_types=EVENT_TYPES,
        roles=ROLES,
        predicates=PREDICATES,
        mappings=COMPATIBILITY_MAPPINGS,
        required_values=REQUIRED_COMPATIBILITY_VALUES,
        categories=CATEGORY_CANONICAL_VALUES,
        node_classes=NODE_CLASSES,
        outcome_compatibility=OUTCOME_COMPATIBILITY,
        registered_qualifiers=QUALIFIERS,
        outcome_qualifier_map=OUTCOME_QUALIFIERS,
        participation_bases=tuple(sorted(PARTICIPATION_BASES)),
        assertion_classes=tuple(sorted(ASSERTION_CLASSES)),
        non_source_classes=NON_SOURCE_CLASSES,
        actor_classes=ACTOR_CLASSES,
        context_classes=CONTEXT_CLASSES,
    )
    assert_temporal_contract()


validate_registries()
