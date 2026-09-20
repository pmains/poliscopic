"""Compatibility mappings and quarantine reasons (KG-INFORMATION-MODEL.md §7.1, §14).

Historical vocabulary is mapped or explicitly quarantined.  Nothing here
rewrites rows: each mapping records the approved *handling* that a later,
separately approved migration plan must perform.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from scripts.kg.registries.entity_taxonomy import (
    DB_SLUG_ALIASES,
    ENTITY_TYPES,
    LEGACY_PROHIBITED_TYPES,
)
from scripts.kg.registries.events import (
    BASE_OUTCOMES,
    DB_SLUG_ALIASES as EVENT_DB_SLUG_ALIASES,
    EVENT_TYPES,
    OUTCOME_COMPATIBILITY,
)
from scripts.kg.registries.model import (
    CompatibilityMapping,
    QuarantineReason,
    canonical_pairs,
    frozen,
)
from scripts.kg.registries.relationships import HISTORICAL_PREDICATES, PREDICATES
from scripts.kg.registries.roles import ROLES

QUARANTINE_REASONS: Mapping[str, QuarantineReason] = frozen({
    reason.slug: reason for reason in (
        QuarantineReason(
            "extractor_only_label",
            "Extractor-only label that is not a canonical contextual role",
        ),
        QuarantineReason(
            "title_label",
            "Free-text title captured as a role; needs evidence review",
        ),
        QuarantineReason(
            "recommendation_is_event",
            "Recommendation is an event/proposed outcome, not a role",
        ),
        QuarantineReason(
            "legacy_prohibited_type",
            "Legacy entity type retained for readability but prohibited for new emission",
        ),
        QuarantineReason(
            "unregistered_value",
            "Value observed in data but absent from the approved registry",
        ),
        QuarantineReason(
            "scraper_sentinel_non_meeting",
            "Ingestion persisted a scraper sentinel or otherwise non-meeting "
            "record as a meeting; the evidence is preserved for audit but excluded "
            "from ordinary normalization",
        ),
    )
})

#: Canonical values are registered vocabulary; they need no mapping.
CANONICAL_ENTITY_TYPES: tuple[str, ...] = tuple(sorted(ENTITY_TYPES))
CANONICAL_ROLES: tuple[str, ...] = tuple(sorted(ROLES))
CANONICAL_PREDICATES: tuple[str, ...] = tuple(sorted(PREDICATES))
CANONICAL_OUTCOMES: tuple[str, ...] = BASE_OUTCOMES
CANONICAL_EVENT_TYPES: tuple[str, ...] = tuple(sorted(EVENT_TYPES))

#: Structural source-reference types (relationship provenance / mention source).
SOURCE_REFERENCE_TYPES: tuple[str, ...] = (
    "agenda_item", "body_membership", "entity_resolution", "meeting_member",
    "meetings", "public_bodies", "pz_item_detail", "supporting_document",
)

#: Registered mention source/extractor producer pairs.
MENTION_SOURCE_EXTRACTOR_PAIRS: tuple[tuple[str, str], ...] = (
    ("agenda_item", "pattern_cascade"),
    ("agenda_item", "regex"),
    ("agenda_item", "resolver"),
    ("agenda_item", "sweep_meetings"),
    ("body_membership", "graph_builder"),
    ("meeting_member", "graph_builder"),
    ("pz_item_detail", "graph_builder"),
    ("pz_item_detail", "regex"),
    ("supporting_document", "sweep_docs"),
)


def _entity_mappings() -> list[CompatibilityMapping]:
    mappings = [
        CompatibilityMapping(
            category="entity_type",
            historical_value=legacy,
            canonical_value=None,
            handling="Keep readable; block new emission; plan evidence-preserving migration",
            reason="Legacy outcome-shaped type superseded by events/outcomes",
            quarantine=True,
        )
        for legacy in LEGACY_PROHIBITED_TYPES
    ]
    mappings.extend(
        CompatibilityMapping(
            category="entity_type",
            historical_value=alias,
            canonical_value=canonical,
            handling="Treat dotted seed slug as the canonical type",
            reason="Historical entity_types seed used dotted path slugs",
        )
        for alias, canonical in sorted(DB_SLUG_ALIASES.items())
    )
    return mappings


def _relationship_mappings() -> list[CompatibilityMapping]:
    return [
        CompatibilityMapping(
            category="relationship",
            historical_value="HAS_APPLICANT",
            canonical_value="APPLIED_FOR",
            handling="Reverse endpoints where stored case-to-actor; rename where stored actor-to-case",
            reason="Both directions observed in producers; canonical direction is actor-to-case",
            direction="case_to_actor|actor_to_case",
        ),
        CompatibilityMapping(
            category="relationship",
            historical_value="HAS_OWNER",
            canonical_value="OWNS",
            handling="Validate object class, then rename during an approved migration",
            reason="Ownership is actor-to-object in the canonical model",
        ),
        CompatibilityMapping(
            category="relationship",
            historical_value="HAS_ATTORNEY",
            canonical_value="REPRESENTS",
            handling="Create participation; add REPRESENTS only with explicit target",
            reason="Role alone does not identify a represented party",
        ),
        CompatibilityMapping(
            category="relationship",
            historical_value="HAS_STAFF",
            canonical_value="PARTICIPATED_IN",
            handling="Preserve mention; add participation only with supporting evidence",
            reason="Mention or byline is not evidence of participation or employment",
        ),
        CompatibilityMapping(
            category="relationship",
            historical_value="HAS_RECOMMENDATION",
            canonical_value=None,
            handling="Migrate to a recommendation event/proposed outcome after adjudication",
            reason="Recommendation is outcome-shaped, not a timeless edge",
            quarantine=True,
        ),
        CompatibilityMapping(
            category="relationship",
            historical_value="REPRESENTED",
            canonical_value="REPRESENTS",
            handling="Alias only; normalize endpoints to active voice in a migration",
            reason="Approved compatibility alias for the canonical predicate",
        ),
    ]


#: Historical role spellings and labels that must be mapped or quarantined.
ROLE_CASING_VARIANTS: tuple[tuple[str, str], ...] = (
    ("Applicant", "applicant"), ("Attorney", "attorney"),
)
ROLE_RELATIONSHIP_SHAPED: tuple[tuple[str, str], ...] = (
    ("HAS_APPLICANT", "applicant"), ("HAS_STAFF", "staff"),
    ("PRESENT_AT", "participant"), ("MEMBER_OF", "member"),
    ("REPRESENTS", "representative"),
)
ROLE_QUARANTINED_LABELS: tuple[str, ...] = (
    "known_org", "firm", "request", "location", "case_number",
)
ROLE_QUARANTINED_TITLES: tuple[str, ...] = ("Planner",)

#: Every historical role value that must appear in the compatibility layer.
ROLE_HISTORICAL_VALUES: tuple[str, ...] = tuple(
    value for value, _canonical in ROLE_CASING_VARIANTS + ROLE_RELATIONSHIP_SHAPED
) + ROLE_QUARANTINED_LABELS + ROLE_QUARANTINED_TITLES + ("HAS_RECOMMENDATION",)


def _role_mappings() -> list[CompatibilityMapping]:
    mappings = [
        CompatibilityMapping(
            category="role", historical_value=historical, canonical_value=canonical,
            handling="Lower-case to the canonical slug",
            reason="Display casing is presentation metadata, not identity",
        )
        for historical, canonical in ROLE_CASING_VARIANTS
    ]
    mappings.extend(
        CompatibilityMapping(
            category="role", historical_value=historical, canonical_value=canonical,
            handling="Map to the canonical role during producer enforcement",
            reason="Relationship name stored as a role string",
        )
        for historical, canonical in ROLE_RELATIONSHIP_SHAPED
    )
    mappings.append(CompatibilityMapping(
        category="role", historical_value="HAS_RECOMMENDATION", canonical_value=None,
        handling="Quarantine as a role; recommendation is an event/proposed outcome",
        reason="Recommendation is not a contextual role", quarantine=True,
    ))
    mappings.extend(
        CompatibilityMapping(
            category="role", historical_value=value, canonical_value=None,
            handling="Quarantine from role queries pending evidence review",
            reason="Extractor-only label, not a canonical contextual role",
            quarantine=True,
        )
        for value in ROLE_QUARANTINED_LABELS
    )
    mappings.extend(
        CompatibilityMapping(
            category="role", historical_value=value, canonical_value=None,
            handling="Quarantine; treat as staff only with explicit employment evidence",
            reason="Free-text title captured as a role", quarantine=True,
        )
        for value in ROLE_QUARANTINED_TITLES
    )
    return mappings


def _event_mappings() -> list[CompatibilityMapping]:
    """Map dotted seed-table event slugs to canonical leaf event types."""
    return [
        CompatibilityMapping(
            category="event_type",
            historical_value=alias,
            canonical_value=canonical,
            handling="Treat dotted seed slug as the canonical leaf event type",
            reason="Historical meeting_event_types seed keyed children by dotted path",
        )
        for alias, canonical in sorted(EVENT_DB_SLUG_ALIASES.items())
    ]


def _outcome_mappings() -> list[CompatibilityMapping]:
    return [
        CompatibilityMapping(
            category="outcome",
            historical_value=raw,
            canonical_value=base if qualifier is None else f"{base}+{qualifier}",
            handling="Split into base outcome plus controlled qualifier",
            reason="Base outcome plus qualifier representation",
        )
        for raw, (base, qualifier) in sorted(OUTCOME_COMPATIBILITY.items())
    ]


COMPATIBILITY_MAPPINGS: tuple[CompatibilityMapping, ...] = tuple(
    _entity_mappings() + _relationship_mappings() + _role_mappings()
    + _outcome_mappings() + _event_mappings()
)

MAPPINGS_BY_KEY: Mapping[tuple[str, str], CompatibilityMapping] = frozen(
    canonical_pairs(COMPATIBILITY_MAPPINGS)
)

#: Categories the audit must classify, with their canonical value sets.
CATEGORY_CANONICAL_VALUES: Mapping[str, tuple[str, ...]] = frozen({
    "entity_type": CANONICAL_ENTITY_TYPES,
    "event_type": CANONICAL_EVENT_TYPES,
    "role": CANONICAL_ROLES,
    "relationship": CANONICAL_PREDICATES,
    "outcome": CANONICAL_OUTCOMES,
    "source_reference_type": SOURCE_REFERENCE_TYPES,
    "edge_kind": ("relational", "attributional"),
})


def classify_value(category: str, value: str) -> str:
    """Return ``canonical``, ``compatibility_mapped``, or ``quarantined``."""
    if value in CATEGORY_CANONICAL_VALUES.get(category, ()):  # canonical first
        if category == "entity_type" and value in LEGACY_PROHIBITED_TYPES:
            return "quarantined"
        return "canonical"
    mapping = MAPPINGS_BY_KEY.get((category, value))
    if mapping is None:
        return "unmapped"
    return "quarantined" if mapping.quarantine else "compatibility_mapped"


def coverage(category: str, values: Iterable[str]) -> dict[str, list[str]]:
    """Classify every observed value in one category."""
    result: dict[str, list[str]] = {
        "canonical": [], "compatibility_mapped": [], "quarantined": [], "unmapped": [],
    }
    for value in sorted(set(values)):
        result[classify_value(category, value)].append(value)
    return result


def missing_mappings(category: str, values: Iterable[str]) -> list[str]:
    """Return observed values with neither a mapping nor a quarantine entry."""
    return [
        value for value in sorted(set(values))
        if classify_value(category, value) == "unmapped"
    ]


def unmapped_historical_predicates() -> list[str]:
    """Return historical predicates lacking a compatibility mapping."""
    return [
        predicate for predicate in HISTORICAL_PREDICATES
        if (("relationship", predicate) not in MAPPINGS_BY_KEY)
        and predicate not in PREDICATES
    ]
