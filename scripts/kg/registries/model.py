"""Shared primitives for the versioned knowledge-graph registries.

These types are deliberately small and pure: the registries are code-owned
immutable data plus lookups, not a database.  Every registry module imports
its primitives from here so validation can treat the bundle uniformly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence, TypeVar

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PREDICATE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

T = TypeVar("T")


class RegistryError(ValueError):
    """Raised when a registry or the assembled bundle violates its contract."""


def require_slug(value: str, *, what: str) -> str:
    """Return ``value`` unchanged when it is a canonical lowercase slug."""
    if not isinstance(value, str) or not SLUG_RE.match(value):
        raise RegistryError(f"{what} must be a lowercase slug, got {value!r}")
    return value


def require_predicate(value: str, *, what: str) -> str:
    """Return ``value`` unchanged when it is an upper-case predicate token."""
    if not isinstance(value, str) or not PREDICATE_RE.match(value):
        raise RegistryError(f"{what} must be an upper-case predicate, got {value!r}")
    return value


def ensure_unique(values: Sequence[str], *, what: str) -> tuple[str, ...]:
    """Return ``values`` as a tuple, rejecting duplicates."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    if duplicates:
        raise RegistryError(f"duplicate {what}: {sorted(duplicates)}")
    return tuple(values)


def frozen(mapping: Mapping[str, T]) -> Mapping[str, T]:
    """Return an immutable view of ``mapping``."""
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class CompatibilityMapping:
    """One explicit historical-to-canonical value transition.

    A mapping never rewrites history by itself: ``handling`` records what a
    future approved migration must do, and ``reason`` is retained for review.
    """

    category: str
    historical_value: str
    canonical_value: str | None
    handling: str
    reason: str
    direction: str | None = None
    quarantine: bool = False


@dataclass(frozen=True)
class QuarantineReason:
    """A registered reason a historical or produced value stays unmapped."""

    slug: str
    meaning: str


def canonical_pairs(mappings: Iterable[CompatibilityMapping]) -> dict[tuple[str, str], CompatibilityMapping]:
    """Index compatibility mappings by (category, historical value)."""
    indexed: dict[tuple[str, str], CompatibilityMapping] = {}
    for mapping in mappings:
        key = (mapping.category, mapping.historical_value)
        if key in indexed:
            raise RegistryError(
                f"duplicate compatibility mapping for {mapping.category}:{mapping.historical_value}"
            )
        indexed[key] = mapping
    return indexed
