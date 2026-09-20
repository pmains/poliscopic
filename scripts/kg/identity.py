"""Typed identity keys for knowledge-graph assertions (Brief 018 Step 3).

The identity layer is split into focused modules:

- :mod:`scripts.kg.identity_keys` — keys and their builders
- :mod:`scripts.kg.identity_assertions` — assertions over those keys

This facade re-exports the public surface so existing imports keep working.
"""

from __future__ import annotations

from scripts.kg.identity_assertions import (
    Assertion,
    assertion_problems,
    build_assertion,
    co_occurrence_assertion,
    serialize_assertion,
)
from scripts.kg.identity_keys import (
    IdentityError,
    IdentityKey,
    adjudication_identity,
    assert_distinct,
    canonical_entity_identity,
    civic_context_identity,
    claim_identity,
    entity_candidate_identity,
    event_identity,
    evidence_identity,
    extraction_identity,
    mention_identity,
    participation_identity,
    vote_identity,
)

__all__ = [
    "Assertion", "IdentityError", "IdentityKey", "adjudication_identity",
    "assert_distinct", "assertion_problems", "build_assertion",
    "canonical_entity_identity", "civic_context_identity", "claim_identity",
    "co_occurrence_assertion", "entity_candidate_identity", "event_identity",
    "evidence_identity", "extraction_identity", "mention_identity",
    "participation_identity", "serialize_assertion", "vote_identity",
]
