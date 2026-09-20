"""sweep_docs_payloads.py — deterministic write-payload selection.

Chooses exactly one write payload per assertion identity from the raw extractor
hits.  This is the only place duplicate candidates are resolved, and it keys on
the same ``entity_assertion()`` / ``mention_assertion()`` identities the planner
uses, so there is no second identity or deduplication rule to drift.

Pure module: no SQL, no database, no validator, no receipt accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .sweep_docs_planning import (
    EntityAssertion,
    ExtractedCandidate,
    MentionAssertion,
)

__all__ = [
    "EntityPayload",
    "MentionPayload",
    "select_write_payloads",
]


# ── Write payloads ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityPayload:
    """The single chosen write payload for one entity assertion identity."""

    assertion: EntityAssertion
    name: str
    confidence: int


@dataclass(frozen=True)
class MentionPayload:
    """The single chosen write payload for one mention assertion identity."""

    assertion: MentionAssertion
    name: str
    confidence: int


def _selection_key(candidate: ExtractedCandidate) -> tuple:
    """Total ordering that makes payload selection input-order independent."""
    return (
        -int(candidate.confidence),
        str(candidate.name),
        str(candidate.normalized_name),
        str(candidate.entity_type),
        str(candidate.role),
        int(candidate.source_id),
        str(candidate.source_type),
    )


def _keep_best(store: dict, key: tuple, candidate: ExtractedCandidate) -> None:
    current = store.get(key)
    if current is None or _selection_key(candidate) < _selection_key(current):
        store[key] = candidate


def select_write_payloads(
    candidates: Iterable[ExtractedCandidate],
) -> tuple[dict[tuple, EntityPayload], dict[tuple, MentionPayload]]:
    """Choose exactly one write payload per assertion identity.

    Duplicate raw candidates are resolved **here and only here**.  Both this
    bridge and :func:`build_classification_plan` derive their keys from the same
    ``entity_assertion()`` / ``mention_assertion()`` identities, so there is no
    second identity or deduplication rule to drift.

    The selection rule is deterministic and independent of input order:

    1. highest ``confidence`` wins;
    2. ties are broken by the lexicographically smallest display ``name``;
    3. any remaining tie is broken by the full assertion identity
       (normalized name, entity type, role, source id, source type).

    Returns ``(entity_payloads, mention_payloads)``, each keyed by the exact
    assertion identity used by the plan.
    """
    entity_winners: dict[tuple, ExtractedCandidate] = {}
    mention_winners: dict[tuple, ExtractedCandidate] = {}

    for candidate in candidates:
        _keep_best(entity_winners, candidate.entity_assertion().identity, candidate)
        _keep_best(mention_winners, candidate.mention_assertion().identity, candidate)

    entity_payloads = {
        key: EntityPayload(
            assertion=EntityAssertion(winner.normalized_name, winner.entity_type),
            name=winner.name,
            confidence=winner.confidence,
        )
        for key, winner in entity_winners.items()
    }
    mention_payloads = {
        key: MentionPayload(
            assertion=winner.mention_assertion(),
            name=winner.name,
            confidence=winner.confidence,
        )
        for key, winner in mention_winners.items()
    }
    return entity_payloads, mention_payloads
