"""Structural validation for the registry bundle.

Every check is a pure function over registry *data* so tests can feed broken
structures and prove the failure modes required by Brief 018 Step 1: duplicate
slugs, invalid parents, cycles, missing inverses, invalid domain/range
references, and incomplete compatibility mappings.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from scripts.kg.registries.model import CompatibilityMapping, RegistryError

MAX_REPORTED = 12


def _summarize(problems: Sequence[str]) -> str:
    head = "; ".join(problems[:MAX_REPORTED])
    if len(problems) > MAX_REPORTED:
        head += f"; (+{len(problems) - MAX_REPORTED} more)"
    return head


def check_unique_slugs(slugs: Iterable[str], *, what: str) -> list[str]:
    """Return problems for duplicated slugs."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for slug in slugs:
        if slug in seen and slug not in duplicates:
            duplicates.append(slug)
        seen.add(slug)
    return [f"duplicate {what}: {slug}" for slug in duplicates]


def check_parents(parents: Mapping[str, str | None], *, what: str) -> list[str]:
    """Return problems for missing parents and parent cycles."""
    problems: list[str] = []
    for slug, parent in parents.items():
        if parent is not None and parent not in parents:
            problems.append(f"{what} {slug} has unknown parent {parent}")
    for slug in parents:
        seen: list[str] = []
        cursor: str | None = slug
        while cursor is not None:
            if cursor in seen:
                problems.append(f"{what} cycle detected at {cursor} (from {slug})")
                break
            seen.append(cursor)
            cursor = parents.get(cursor)
    return sorted(set(problems))


def check_inverses(
    inverse_labels: Mapping[str, str], *, what: str
) -> list[str]:
    """Return problems for missing inverse labels.

    The approved model intentionally reuses one presentation label across two
    structurally different predicates (``OCCURRED_IN`` and ``ABOUT`` are both
    "had event"), so duplicates are reported by :func:`duplicate_inverse_labels`
    rather than failing validation.
    """
    return [
        f"{what} {predicate} is missing an inverse label"
        for predicate, label in inverse_labels.items()
        if not label or not str(label).strip()
    ]


def duplicate_inverse_labels(inverse_labels: Mapping[str, str]) -> list[str]:
    """Return inverse labels shared by more than one predicate (diagnostic)."""
    owners: dict[str, list[str]] = {}
    for predicate, label in inverse_labels.items():
        owners.setdefault(str(label), []).append(predicate)
    return sorted(
        f"{label}: {', '.join(sorted(predicates))}"
        for label, predicates in owners.items()
        if len(predicates) > 1
    )


def check_domain_range(
    predicates: Mapping[str, tuple[Sequence[str], Sequence[str]]],
    node_classes: Sequence[str],
    *,
    what: str,
) -> list[str]:
    """Return problems for domain/range values outside the node classes."""
    known = set(node_classes)
    problems: list[str] = []
    for predicate, (domain, range_) in predicates.items():
        for value in domain:
            if value not in known:
                problems.append(f"{what} {predicate} domain has unknown class {value}")
        for value in range_:
            if value not in known:
                problems.append(f"{what} {predicate} range has unknown class {value}")
        if not domain or not range_:
            problems.append(f"{what} {predicate} must declare domain and range")
    return problems


def check_references(
    values: Iterable[str], known: Iterable[str], *, what: str
) -> list[str]:
    """Return problems for references outside a known value set."""
    known_set = set(known)
    return [f"{what} references unknown value {value}" for value in sorted(set(values))
            if value not in known_set]


def check_compatibility_completeness(
    mappings: Sequence[CompatibilityMapping],
    *,
    required_values: Mapping[str, Iterable[str]],
    categories: Mapping[str, Sequence[str]],
) -> list[str]:
    """Return problems for incomplete or dangling compatibility mappings.

    ``required_values`` maps each category to every historical/producer value
    that must be canonical, mapped, or quarantined.
    """
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    canonical_by_category = {
        category: set(values) for category, values in categories.items()
    }
    for mapping in mappings:
        key = (mapping.category, mapping.historical_value)
        if key in seen:
            problems.append(
                f"duplicate compatibility mapping {mapping.category}:{mapping.historical_value}"
            )
        seen.add(key)
        if mapping.category not in canonical_by_category:
            problems.append(f"mapping category {mapping.category} is not registered")
            continue
        if mapping.canonical_value is None:
            if not mapping.quarantine:
                problems.append(
                    f"mapping {mapping.category}:{mapping.historical_value} has no "
                    "canonical value and is not quarantined"
                )
            continue
        target = mapping.canonical_value.split("+")[0]
        if target not in canonical_by_category[mapping.category]:
            problems.append(
                f"mapping {mapping.category}:{mapping.historical_value} targets "
                f"unregistered value {target}"
            )
    for category, values in required_values.items():
        for value in values:
            if (category, value) not in seen:
                problems.append(f"{category} value {value} is neither mapped nor quarantined")
    return problems


def check_outcome_compatibility(
    outcome_compatibility: Mapping[str, object],
    *,
    categories: Mapping[str, Sequence[str]],
    registered_qualifiers: Sequence[str] = (),
    outcome_qualifier_map: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Check base-outcome registration and base/qualifier pairing.

    A qualifier must be registered *and* permitted for the base outcome it is
    attached to, so a qualifier such as ``without_prejudice`` cannot drift onto
    ``approved``.
    """
    problems: list[str] = []
    bases = categories.get("outcome", ())
    for raw, (base, qualifier) in outcome_compatibility.items():
        if base not in bases:
            problems.append(
                f"outcome compatibility {raw} maps to unregistered base outcome {base}"
            )
        if qualifier is None:
            continue
        if registered_qualifiers and qualifier not in registered_qualifiers:
            problems.append(
                f"outcome compatibility {raw} carries unregistered qualifier {qualifier}"
            )
        if outcome_qualifier_map is not None:
            allowed = tuple(outcome_qualifier_map.get(base, ()))
            if qualifier not in allowed:
                problems.append(
                    f"outcome compatibility {raw} attaches qualifier {qualifier} "
                    f"to {base}, which does not permit it (allowed: {allowed})"
                )
    if outcome_qualifier_map is not None and registered_qualifiers:
        for base, qualifiers in outcome_qualifier_map.items():
            if base not in bases:
                problems.append(
                    f"outcome qualifier map declares unregistered base outcome {base}"
                )
            for qualifier in qualifiers:
                if qualifier not in registered_qualifiers:
                    problems.append(
                        f"outcome qualifier map declares unregistered qualifier {qualifier}"
                    )
    return problems


def validate_registry_data(
    *,
    entity_types: Mapping[str, object],
    event_types: Mapping[str, object],
    roles: Mapping[str, object],
    predicates: Mapping[str, object],
    mappings: Sequence[CompatibilityMapping],
    required_values: Mapping[str, Iterable[str]],
    categories: Mapping[str, Sequence[str]],
    node_classes: Sequence[str],
    outcome_compatibility: Mapping[str, object],
    registered_qualifiers: Sequence[str] = (),
    outcome_qualifier_map: Mapping[str, Sequence[str]] | None = None,
    participation_bases: Sequence[str],
    assertion_classes: Sequence[str],
    non_source_classes: Sequence[str],
    actor_classes: Sequence[str],
    context_classes: Sequence[str],
) -> None:
    """Validate one registry bundle, raising on the first aggregated failure."""
    problems: list[str] = []

    entity_parents = {
        slug: getattr(entry, "parent", None) for slug, entry in entity_types.items()
    }
    event_parents = {
        slug: getattr(entry, "parent", None) for slug, entry in event_types.items()
    }
    problems += check_unique_slugs(entity_types, what="entity type")
    problems += check_unique_slugs(event_types, what="event type")
    problems += check_unique_slugs(roles, what="role")
    problems += check_unique_slugs(predicates, what="predicate")
    problems += check_parents(entity_parents, what="entity type")
    problems += check_parents(event_parents, what="event type")
    problems += check_inverses(
        {slug: getattr(entry, "inverse_label", "") for slug, entry in predicates.items()},
        what="relationship predicate",
    )
    problems += check_domain_range(
        {
            slug: (getattr(entry, "domain", ()), getattr(entry, "range", ()))
            for slug, entry in predicates.items()
        },
        node_classes,
        what="relationship predicate",
    )
    for slug, entry in roles.items():
        problems += check_references(
            getattr(entry, "allowed_contexts", ()), context_classes,
            what=f"role {slug} context",
        )
        problems += check_references(
            getattr(entry, "allowed_actors", ()), actor_classes,
            what=f"role {slug} actor",
        )
    problems += check_outcome_compatibility(
        outcome_compatibility,
        categories=categories,
        registered_qualifiers=registered_qualifiers,
        outcome_qualifier_map=outcome_qualifier_map,
    )
    problems += [
        f"participation basis {slug} is not a lowercase slug"
        for slug in participation_bases
        if not isinstance(slug, str) or not slug.islower() or " " in slug
    ]
    problems += [
        f"assertion class {slug} is not a lowercase slug"
        for slug in assertion_classes
        if not isinstance(slug, str) or not slug.islower() or " " in slug
    ]
    problems += check_references(
        non_source_classes, tuple(assertion_classes),
        what="non-source assertion class",
    )
    problems += check_compatibility_completeness(
        mappings, required_values=required_values, categories=categories
    )

    if problems:
        raise RegistryError(
            f"registry bundle invalid: {_summarize(sorted(set(problems)))}"
        )
