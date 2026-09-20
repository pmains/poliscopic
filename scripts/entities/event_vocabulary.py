"""event_vocabulary.py — the event producer's verb vocabulary, one authority.

This module owns the producer-side verb knowledge: how a raw ``action_verb`` is
normalized to a lookup key, which canonical dotted event type and outcome that
key maps to, and which outcomes are procedural boilerplate.

It is deliberately separate from the runtime so the vocabulary can be inspected
without importing a database engine, and so the facade can re-export it without
duplicating it.  :mod:`scripts.entities.event_normalize` re-exports all three
names, and ``producer_vocabulary`` introspects them from there, so this remains
the single source of truth.

The registries remain the authority for *whether* a mapped event type or outcome
is legitimate; this map only says what the extractor's verbs mean.
"""

from __future__ import annotations

import re

__all__ = ["PROCEDURAL_OUTCOMES", "VERB_MAP", "normalize_verb"]


def normalize_verb(raw: str) -> str:
    """Normalize action_verb to a lookup key.

    Handles newlines, multiple spaces, weird whitespace from pdftotext -layout.
    """
    collapsed = re.sub(r"\s+", " ", raw).strip().lower()
    return collapsed.replace(" ", "_")


# Action verb → (canonical dotted event type, raw outcome).
VERB_MAP = {
    # Decision — approval
    "approved":                       ("decision.approval",       "approved"),
    "approved_with_conditions":       ("decision.approval",       "approved_with_conditions"),
    "approved_with_stipulations":     ("decision.approval",       "approved_with_stipulations"),
    "approved_subject_to":            ("decision.approval",       "approved_subject_to"),
    "approved_subject_to_conditions": ("decision.approval",       "approved_subject_to"),
    "approved_subject_to_stipulations": ("decision.approval",     "approved_subject_to"),
    "approved_as_amended":            ("decision.approval",       "approved_as_amended"),

    # Decision — denial
    "denied":                         ("decision.denial",         "denied"),
    "denied_without_prejudice":       ("decision.denial",         "denied_without_prejudice"),

    # Decision — continuation
    "continued":                      ("decision.continuation",   "continued"),
    "tabled":                         ("decision.continuation",   "tabled"),
    "deferred":                       ("decision.continuation",   "deferred"),
    "extended":                       ("decision.continuation",   "extended"),

    # Legislation — adoption / introduction / amendment
    "adopted":                        ("legislation.adoption",    "adopted"),
    "introduced":                     ("legislation.introduction","introduced"),
    "amended":                        ("legislation.amendment",   "amended"),

    # Procedure — receipt / discussion
    "received":                       ("procedure.receipt",       "received"),
    "received_and_filed":             ("procedure.receipt",       "received"),
    "discussed":                      ("procedure.discussion",    "discussed"),
    "discussion_only":                ("procedure.discussion",    "discussed"),
    "for_discussion":                 ("procedure.discussion",    "discussed"),
    "preliminary_review":             ("procedure.discussion",    "reviewed"),

    # Other decisions
    "withdrawn":                      ("decision.continuation",   "withdrawn"),
    "sustained":                      ("decision.approval",       "sustained"),
    "vacated":                        ("decision.approval",       "vacated"),

    # Procedural — low value, but still events
    "called_to_order":                ("procedure.discussion",    "called_to_order"),
    "no_action":                      ("procedure.discussion",    "no_action"),
    "no_response":                    ("procedure.discussion",    "no_response"),
    "discussion":                     ("procedure.discussion",    "discussed"),
    "for_discussion":                 ("procedure.discussion",    "discussed"),
}

# Outcomes that are procedural boilerplate — still written to events but
# can be filtered downstream.
PROCEDURAL_OUTCOMES = {"called_to_order", "no_action", "no_response"}
