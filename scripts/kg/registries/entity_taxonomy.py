"""Entity taxonomy registry (KG-INFORMATION-MODEL.md §5).

The taxonomy is an is-a tree of canonical entity types.  ``organization`` is a
permitted fallback when no supported subtype applies; every other value is
leaf-compliant only when it has no children.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from scripts.kg.registries.model import (
    RegistryError,
    frozen,
    require_slug,
)

FALLBACK_SLUG = "organization"

#: Legacy types that remain readable but must not be emitted for new rows.
LEGACY_PROHIBITED_TYPES: tuple[str, ...] = ("recommendation",)


@dataclass(frozen=True)
class EntityTypeEntry:
    """One canonical entity type."""

    slug: str
    parent: str | None
    canonical_use: str
    fallback: bool = False


_RAW_ENTITY_TYPES: tuple[EntityTypeEntry, ...] = (
    EntityTypeEntry("person", None, "Human being"),
    EntityTypeEntry(
        "organization", None, "Known organization, subtype unknown", fallback=True
    ),
    EntityTypeEntry("firm", "organization", "Generic professional firm"),
    EntityTypeEntry("law_firm", "firm", "Legal-services firm"),
    EntityTypeEntry("planning_firm", "firm", "Planning/engineering/land-use firm"),
    EntityTypeEntry("developer", "organization", "Development organization"),
    EntityTypeEntry("agency", "organization", "Government or quasi-government agency"),
    EntityTypeEntry("utility", "organization", "Utility organization"),
    EntityTypeEntry("vendor", "organization", "Supplier or contractor organization"),
    EntityTypeEntry("department", "organization", "Government department or staff"),
    EntityTypeEntry("advocacy_group", "organization", "Advocacy or community interest"),
    EntityTypeEntry("case", None, "Continuing civic case or project"),
    EntityTypeEntry("parcel", None, "Validated jurisdiction-scoped parcel"),
    EntityTypeEntry("address", None, "Canonical address"),
    EntityTypeEntry("meeting", None, "Materialized meeting container"),
    EntityTypeEntry("body", None, "Materialized public body"),
    EntityTypeEntry("jurisdiction", None, "Materialized jurisdiction"),
)

ENTITY_TYPES: Mapping[str, EntityTypeEntry] = frozen(
    {entry.slug: entry for entry in _RAW_ENTITY_TYPES}
)

#: Slug spellings used by the historical ``entity_types`` seed table.
DB_SLUG_ALIASES: Mapping[str, str] = frozen({
    "organization.firm": "firm",
    "organization.firm.law_firm": "law_firm",
    "organization.firm.planning_firm": "planning_firm",
    "organization.developer": "developer",
    "organization.agency": "agency",
    "organization.utility": "utility",
    "organization.vendor": "vendor",
    "organization.department": "department",
    "organization.advocacy_group": "advocacy_group",
})


def children_of(slug: str) -> tuple[str, ...]:
    """Return the canonical slugs whose parent is ``slug``."""
    return tuple(sorted(
        entry.slug for entry in ENTITY_TYPES.values() if entry.parent == slug
    ))


def descendants(slug: str) -> tuple[str, ...]:
    """Return every transitive child of ``slug`` (excluding ``slug``)."""
    canonical = DB_SLUG_ALIASES.get(slug, slug)
    found: list[str] = []
    pending = list(children_of(canonical))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.append(current)
        pending.extend(children_of(current))
    return tuple(sorted(found))


def ancestors(slug: str) -> tuple[str, ...]:
    """Return every ancestor of ``slug`` from its parent upward."""
    canonical = DB_SLUG_ALIASES.get(slug, slug)
    found: list[str] = []
    entry = ENTITY_TYPES.get(canonical)
    while entry is not None and entry.parent is not None:
        parent = DB_SLUG_ALIASES.get(entry.parent, entry.parent)
        if parent in found:
            break
        found.append(parent)
        entry = ENTITY_TYPES.get(parent)
    return tuple(found)


def is_a(slug: str, ancestor: str) -> bool:
    """Return True when ``slug`` is or descends from ``ancestor``.

    Producers use this instead of restating type unions by hand, so adding a
    new organization subtype needs no producer change.
    """
    canonical = DB_SLUG_ALIASES.get(slug, slug)
    target = DB_SLUG_ALIASES.get(ancestor, ancestor)
    return canonical == target or target in ancestors(canonical)


#: Every organizational subtype, derived by traversal (never hardcoded).
ORGANIZATION_TYPES: tuple[str, ...] = (
    FALLBACK_SLUG,
) + descendants(FALLBACK_SLUG)


def get_entity_type(slug: str) -> EntityTypeEntry:
    """Return one entity type entry, accepting historical dotted aliases."""
    canonical = DB_SLUG_ALIASES.get(slug, slug)
    try:
        return ENTITY_TYPES[canonical]
    except KeyError as error:  # pragma: no cover - message only
        raise RegistryError(f"unregistered entity type: {slug}") from error


def is_leaf_compliant(slug: str) -> bool:
    """Return True when the value may be emitted as an entity type.

    A type with children may only be emitted when it is the explicit fallback;
    every other parent type must be narrowed to a supported leaf.
    """
    entry = get_entity_type(slug)
    if entry.fallback:
        return True
    return not children_of(entry.slug)


def normalize_db_slug(slug: str) -> str:
    """Map a historical seed slug to its canonical registry slug."""
    require_slug(slug.split(".")[-1], what="entity type")
    return DB_SLUG_ALIASES.get(slug, slug)
