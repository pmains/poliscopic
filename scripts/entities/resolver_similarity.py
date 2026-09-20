"""Pure name-similarity helpers for the entity resolver.

Extracted from :mod:`scripts.entities.resolver` so the orchestration module stays
focused on proposals, classification, and persistence.  Nothing here touches a
database: these are deterministic string scorers, which is also what makes them
cheap to unit-test on their own.
"""

from __future__ import annotations

import re

__all__ = [
    "acronym_match",
    "make_block_key",
    "substring_match",
    "token_normalize",
    "token_set_similarity",
    "token_sort_similarity",
]


def token_normalize(s: str) -> str:
    """Remove punctuation, normalize whitespace, lowercase."""
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def token_set_similarity(a: str, b: str) -> float:
    """Jaccard similarity on token sets."""
    ta = set(token_normalize(a).split())
    tb = set(token_normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def token_sort_similarity(a: str, b: str) -> float:
    """Check if sorted-token strings match (same words, different order)."""
    ta = sorted(token_normalize(a).split())
    tb = sorted(token_normalize(b).split())
    return 1.0 if ta == tb and ta else 0.0


def substring_match(a: str, b: str) -> float:
    """Score 0.9 if one name is a clear substring of the other."""
    na = token_normalize(a)
    nb = token_normalize(b)
    if len(na) < 3 or len(nb) < 3:
        return 0.0
    # One is fully contained in the other (e.g., "Vertical Bridge" in
    # "Annmarie Beckett, Vertical Bridge/Clear Blue Services")
    if na in nb or nb in na:
        # But only if the longer name isn't drastically longer
        ratio = min(len(na), len(nb)) / max(len(na), len(nb))
        if ratio > 0.25:
            return 0.85
    return 0.0


def _acronym(s: str) -> str:
    return "".join(w[0] for w in s.split() if w)


def acronym_match(a: str, b: str) -> float:
    """Score if acronym of one matches the other (e.g., 'JCJ' == 'JCJ Services')."""
    na = token_normalize(a)
    nb = token_normalize(b)
    acr_a = _acronym(na)
    acr_b = _acronym(nb)
    if acr_a and acr_b and (acr_a == acr_b or acr_a in nb or acr_b in na):
        # Check it's not just a single-letter match
        if len(acr_a) >= 2:
            return 0.75
    return 0.0


def make_block_key(a: str, b: str) -> str | None:
    """Determine if two names should be compared. Returns block key or None.

    ``None`` means the pair is not a candidate at all, so it is a comparison that
    never becomes a proposal.
    """
    na = token_normalize(a)
    nb = token_normalize(b)
    if na == nb:
        return f"exact:{na}"

    # Same first token (usually the most distinctive: "Hitt" ≈ "Huitt")
    a_tokens = na.split()
    b_tokens = nb.split()
    if a_tokens and b_tokens and a_tokens[0] == b_tokens[0]:
        return f"first:{a_tokens[0]}"

    # Same last token
    if len(a_tokens) > 0 and len(b_tokens) > 0 and a_tokens[-1] == b_tokens[-1]:
        return f"last:{a_tokens[-1]}"

    # Token subset — one is wholly contained in the other
    set_a, set_b = set(a_tokens), set(b_tokens)
    if set_a and set_b and (set_a <= set_b or set_b <= set_a):
        return f"subset:{min(len(set_a), len(set_b))}"

    # Acronym match — first letters of each token
    acr_a, acr_b = _acronym(na), _acronym(nb)
    if acr_a and acr_b and (acr_a == acr_b or acr_a in nb or acr_b in na):
        return f"acr:{acr_a}"

    return None
