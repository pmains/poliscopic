"""Authoritative producer vocabulary declarations (Brief 018 Step 2).

The compatibility audit must classify what producers *declare*, not a
hand-maintained copy of it.  This module imports the producer modules and reads
their real declaration constants, so new producer vocabulary shows up as an
unmapped value instead of drifting silently.

Producers that keep vocabulary inline in functions, or that require heavy
optional dependencies, are recorded in ``SKIPPED_PRODUCERS`` with the reason.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

#: Producers whose vocabulary cannot be read by importing the module, with the
#: reason.  This is an *introspection* limitation only and is never a coverage
#: claim: coverage is decided by ``scripts.kg.producer_coverage``.
INTROSPECTION_LIMITS: Mapping[str, str] = {
    "role_classifier": "loads ML runtime (torch/sentence-transformers); labels come from a data artifact",
    "event_link": "role labels are inline strings inside functions; no module-level declaration",
    "graph_builder_sources": "predicate strings are inline in source classes; no module-level declaration",
    "graph_builder_materialization": "declares no vocabulary constants",
}


@dataclass(frozen=True)
class Declaration:
    """One vocabulary value declared by a producer."""

    category: str
    value: str
    source: str


def _load(module_name: str) -> Any | None:
    """Import one producer module, returning None when it cannot load."""
    try:
        return importlib.import_module(f"scripts.entities.{module_name}")
    except Exception:  # pragma: no cover - environment dependent
        return None


def _pairs(items: Iterable[Any]) -> Iterable[tuple[Any, Any]]:
    """Yield 2-tuples from a declaration list, ignoring malformed entries."""
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            yield item[0], item[1]


def declared_vocabulary() -> dict[str, list[Declaration]]:
    """Return every producer-declared vocabulary value, keyed by category."""
    found: dict[str, list[Declaration]] = {}

    def add(category: str, value: Any, source: str) -> None:
        if value is None:
            return
        text = str(value)
        found.setdefault(category, []).append(
            Declaration(category=category, value=text, source=source)
        )

    extract = _load("event_extract")
    if extract is not None:
        for _pattern, outcome in _pairs(getattr(extract, "ACTION_PATTERNS", ())):
            add("outcome", outcome, "event_extract.ACTION_PATTERNS")

    normalize = _load("event_normalize")
    if normalize is not None:
        for verb, mapping in (getattr(normalize, "VERB_MAP", {}) or {}).items():
            if isinstance(mapping, (tuple, list)) and len(mapping) >= 2:
                add("event_type", mapping[0], "event_normalize.VERB_MAP")
                add("outcome", mapping[1], "event_normalize.VERB_MAP")
            add("procedure_outcome", verb, "event_normalize.VERB_MAP")
        for outcome in sorted(getattr(normalize, "PROCEDURAL_OUTCOMES", ()) or ()):
            add("outcome", outcome, "event_normalize.PROCEDURAL_OUTCOMES")

    cascade = _load("pattern_cascade")
    if cascade is not None:
        for role, predicate in (getattr(cascade, "ROLE_EDGE_MAP", {}) or {}).items():
            add("role", role, "pattern_cascade.ROLE_EDGE_MAP")
            add("relationship", predicate, "pattern_cascade.ROLE_EDGE_MAP")
        for _body, patterns in (getattr(cascade, "BODY_PATTERNS", {}) or {}).items():
            for pattern in patterns or ():
                if isinstance(pattern, (tuple, list)) and len(pattern) >= 2:
                    add("role", pattern[1], "pattern_cascade.BODY_PATTERNS")

    sweep = _load("sweep_docs")
    if sweep is not None:
        for _name, entity_type in (getattr(sweep, "KNOWN_ORGANIZATIONS", {}) or {}).items():
            add("entity_type", entity_type, "sweep_docs.KNOWN_ORGANIZATIONS")

    return found


def declared_values(category: str) -> dict[str, list[str]]:
    """Return {value: [sources]} for one declared category."""
    values: dict[str, list[str]] = {}
    for declaration in declared_vocabulary().get(category, []):
        values.setdefault(declaration.value, []).append(declaration.source)
    return {value: sorted(set(sources)) for value, sources in sorted(values.items())}


def declaration_drift(
    category: str, classify: Callable[[str, str], str]
) -> dict[str, list[str]]:
    """Return declared values that ``classify`` reports as unmapped."""
    unmapped: dict[str, list[str]] = {}
    for value, sources in declared_values(category).items():
        if classify(category, value) == "unmapped":
            unmapped[value] = sources
    return unmapped
