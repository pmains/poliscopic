"""Producer emission coverage (Brief 018 Step 4).

Coverage is a statement about *emission*, not about how easily a module can be
introspected.  Every executed producer must be one of two things:

- **requires a receipt** — it emits ontology-bearing values and must route them
  through :mod:`scripts.kg.emission` before any database write; or
- **exempt** — it is proven to emit no ontology-bearing values, with the proof
  recorded here and asserted by tests.

A producer that merely cannot be introspected is *not* exempt.  That is why the
former ``producer_vocabulary.SKIPPED_PRODUCERS`` list no longer decides
coverage: it described an introspection limitation, which is an implementation
detail, not evidence about emissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ProducerCoverage:
    """How one producer is covered by the emission boundary."""

    producer: str
    module: str
    requires_receipt: bool
    reason: str


#: Producer name -> coverage.  Mirror this with ``detect_entities.PHASES``.
PRODUCER_COVERAGE: Mapping[str, ProducerCoverage] = {
    entry.producer: entry for entry in (
        ProducerCoverage(
            "graph_builder", "scripts.entities.graph_builder", True,
            "Emits entity types, predicates, and mention roles from structured tables",
        ),
        ProducerCoverage(
            "sweep_docs", "scripts.entities.sweep_docs", True,
            "Emits entity types and mention roles from document text",
        ),
        ProducerCoverage(
            "pattern_cascade", "scripts.entities.pattern_cascade", True,
            "Emits entity types, predicates, and contextual roles",
        ),
        ProducerCoverage(
            "role_classifier", "scripts.entities.role_classifier", True,
            "Emits contextual roles from ML inference",
        ),
        ProducerCoverage(
            "resolver", "scripts.entities.resolver", True,
            "Emits entity types, predicates, and mention roles while resolving",
        ),
        ProducerCoverage(
            "event_pipeline", "scripts.entities.event_extractor", True,
            "Emits event types, outcomes, and participation roles",
        ),
    )
}


def producers_requiring_receipts() -> tuple[str, ...]:
    """Return producers that must return a validation receipt."""
    return tuple(sorted(
        name for name, item in PRODUCER_COVERAGE.items() if item.requires_receipt
    ))


def exempt_producers() -> tuple[str, ...]:
    """Return producers proven to emit no ontology-bearing values."""
    return tuple(sorted(
        name for name, item in PRODUCER_COVERAGE.items() if not item.requires_receipt
    ))


def preflight_declarations(declared: Mapping[str, bool]) -> list[str]:
    """Check the imported producer modules against declared coverage.

    Each producer module reports whether it emits ontology-bearing values; a
    disagreement means the coverage table is stale and must be corrected rather
    than silently trusted.
    """
    problems: list[str] = []
    for name, item in sorted(PRODUCER_COVERAGE.items()):
        if name not in declared:
            problems.append(
                f"producer {name} declares no emission capability; coverage "
                "cannot be verified"
            )
        elif bool(declared[name]) != item.requires_receipt:
            problems.append(
                f"producer {name} declares emits_ontology="
                f"{bool(declared[name])} but coverage says "
                f"requires_receipt={item.requires_receipt}"
            )
    for name in sorted(set(declared) - set(PRODUCER_COVERAGE)):
        problems.append(f"producer {name} is not covered by PRODUCER_COVERAGE")
    return problems


def coverage_problems(phase_names) -> list[str]:
    """Reconcile the coverage table against the orchestrator's phase list."""
    problems: list[str] = []
    covered = set(PRODUCER_COVERAGE)
    for name in sorted(set(phase_names) - covered):
        problems.append(f"phase {name} has no producer coverage entry")
    for name in sorted(covered - set(phase_names)):
        problems.append(f"coverage lists producer {name} which is not a phase")
    return problems
