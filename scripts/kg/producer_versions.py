"""Authoritative producer-version declarations (Brief 018 Step 4).

The orchestrator must be able to check a receipt's ``producer_version`` against a
*declared* value.  Requiring only that the string be non-empty proves nothing: a
producer could report any version and still pass.

This module is the single place that declares those versions.  The producer set
is derived from :mod:`scripts.kg.producer_coverage`, so there is no second
producer list to drift out of sync — adding a producer to coverage without
declaring its version is reported as a problem, and declaring a version for a
non-producer is reported too.

Versions are bumped deliberately when a producer's *emission behaviour* changes
(which values it emits, or how it classifies rows), because that is what the
receipt binds.  They are not build identifiers and must not be derived from file
hashes: two deploys of identical behaviour must agree.
"""

from __future__ import annotations

from typing import Mapping

from scripts.kg.producer_coverage import producers_requiring_receipts

__all__ = [
    "COMPONENT_VERSIONS",
    "PRODUCER_VERSIONS",
    "declared_component_versions",
    "declared_producer_version",
    "version_declaration_problems",
]

#: producer -> declared emission-behaviour version.
PRODUCER_VERSIONS: Mapping[str, str] = {
    "graph_builder": "graph_builder/1.0",
    "sweep_docs": "sweep_docs/1.0",
    "pattern_cascade": "pattern_cascade/1.0",
    "role_classifier": "role_classifier/1.0",
    "resolver": "resolver/1.0",
    "event_pipeline": "event_pipeline/1.0",
}

#: producer -> {component step: declared version}.
#:
#: A multi-domain phase seals one receipt per domain, each carrying the version
#: of the module that sealed it.  Those versions are declared here so an envelope
#: is checked against a known expectation instead of trusting whatever a
#: component reports.
#:
#: They are literals on purpose.  Importing the producer modules here would make
#: this shared registry reachable from every phase that imports it, which would
#: silently widen several producers' code-evidence manifests.  A drift test
#: asserts each literal equals the constant its own module seals with, so the
#: declaration cannot diverge unnoticed.
COMPONENT_VERSIONS: Mapping[str, Mapping[str, str]] = {
    "event_pipeline": {
        "normalize": "event-normalize/1.0",
        "link": "2026-07-27.1",
    },
}


def declared_producer_version(producer: str) -> str | None:
    """Return the declared version for ``producer``, or ``None`` if undeclared."""
    return PRODUCER_VERSIONS.get(producer)


def declared_component_versions(producer: str) -> dict[str, str]:
    """Return the declared version of each component step of ``producer``.

    A multi-domain phase keeps one canonical receipt per domain, each sealed by
    its own module with its own version.  An envelope can therefore be checked
    against a declared expectation rather than merely trusting what a component
    reports.  See :data:`COMPONENT_VERSIONS` for why these are literals.
    """
    return dict(COMPONENT_VERSIONS.get(producer, {}))


def version_declaration_problems() -> list[str]:
    """Reconcile the version table against the receipt-bearing producer set.

    Fail-closed coverage: every producer that must emit a receipt needs exactly
    one declaration, and no declaration may name a non-producer.
    """
    required = set(producers_requiring_receipts())
    declared = set(PRODUCER_VERSIONS)
    problems: list[str] = []
    for name in sorted(required - declared):
        problems.append(f"producer {name} requires a receipt but declares no version")
    for name in sorted(declared - required):
        problems.append(
            f"version declared for {name}, which is not a receipt-bearing producer"
        )
    for name in sorted(required & declared):
        if not PRODUCER_VERSIONS[name].strip():
            problems.append(f"producer {name} declares a blank version")
    return problems
