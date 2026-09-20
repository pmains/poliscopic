"""Role-proposal classification for the ML role classifier.

The classifier scores mention text and predicts a role.  A *proposal* is one
selected mention reaching the decision point — never an aggregate scan count.
Each proposal is classified **exactly once**, identically in dry and live runs:

``would_update``
    The predicted role differs from the stored role and clears the confidence
    threshold.  This is a real change, so it counts as a would-update **in dry
    mode too**; dry mode simply does not write it.

``replay_noop``
    The predicted role equals the role already stored.  There is nothing to
    write, so the proposal is a replay no-op.

``unresolved``
    The record cannot safely identify or update its target: no usable predicted
    role, a prediction below the confidence threshold, a missing mention id, or
    a predicted role that is not canonical emission vocabulary.  Nothing is
    written for these, and they are counted rather than silently dropped.

Canonical validation uses the authoritative registry checker from
:mod:`scripts.kg.emission_checks` — there is deliberately no second validator
here.  A non-canonical prediction is a *refused emission*: the proposal becomes
``unresolved`` and the reason is counted separately so it stays visible.
"""

from __future__ import annotations

from scripts.kg.emission_checks import check

__all__ = [
    "PROPOSAL_REPLAY_NOOP",
    "PROPOSAL_UNRESOLVED",
    "PROPOSAL_WOULD_UPDATE",
    "classify_role_proposal",
    "flush_role_updates",
    "is_canonical_role_emission",
    "role_classifier_accounting",
]

PROPOSAL_WOULD_UPDATE = "would_update"
PROPOSAL_REPLAY_NOOP = "replay_noop"
PROPOSAL_UNRESOLVED = "unresolved"

#: Context criteria a mention role must satisfy to be emittable.
_MENTION_ROLE_CRITERIA = {"assertion_kind": "mention", "context_class": "evidence"}


def is_canonical_role_emission(role: str) -> bool:
    """Whether ``role`` may be emitted as a mention role right now.

    Delegates to the canonical registry checker; a quarantined, prohibited, or
    context-inappropriate role is not emittable.
    """
    try:
        check("role", role, **_MENTION_ROLE_CRITERIA)
    except Exception:
        return False
    return True


def classify_role_proposal(
    *,
    mention_id: object,
    predicted_role: str,
    current_role: str,
    confidence: float,
    threshold: float,
) -> str:
    """Classify exactly one role proposal.

    Order matters: an unidentifiable target or an unemittable role is
    ``unresolved`` before any comparison with the stored role, so a bad
    prediction is never mistaken for a replay.
    """
    if mention_id is None or not str(mention_id).strip():
        return PROPOSAL_UNRESOLVED
    role = str(predicted_role or "").strip()
    if not role:
        return PROPOSAL_UNRESOLVED
    if not is_canonical_role_emission(role):
        return PROPOSAL_UNRESOLVED
    if role == str(current_role or "").strip():
        return PROPOSAL_REPLAY_NOOP
    if float(confidence) < float(threshold):
        return PROPOSAL_UNRESOLVED
    return PROPOSAL_WOULD_UPDATE


def flush_role_updates(engine, updates: list[tuple[str, int]]) -> None:
    """Bulk-update role classifications for ``(role, mention_id)`` pairs.

    One statement per call, so the caller owns chunking and a failure rolls back
    exactly the chunk it was applying.
    """
    if not updates:
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        case_parts = []
        for role, mention_id in updates:
            escaped = str(role).replace("'", "''")
            case_parts.append(f"WHEN {mention_id} THEN '{escaped}'")
        ids = ",".join(str(mid) for _, mid in updates)
        conn.execute(text(f"""
            UPDATE entity_mentions
            SET role_in_context = CASE id
                {' '.join(case_parts)}
                ELSE role_in_context
            END
            WHERE id IN ({ids})
        """))


def role_classifier_accounting(counts: dict, *, committed: int,
                               dry_run: bool):
    """Build the receipt accounting from per-proposal counts.

    ``proposed`` is the number of proposals that reached the decision point, so
    every one of them is accounted for exactly once.  ``committed`` reflects the
    rows the transaction actually wrote; anything proposed but not written is
    ``rolled_back``, which is what keeps the live mutation equation honest.
    """
    from scripts.entities.phase_receipt import RowAccounting

    would_update = int(counts.get("would_update", 0))
    replay = int(counts.get("replay_noop", 0))
    unresolved = int(counts.get("unresolved", 0))
    if dry_run:
        return RowAccounting(
            proposed=would_update + replay + unresolved,
            would_update=would_update,
            replay_noop=replay,
            unresolved=unresolved,
        )
    lost = max(0, would_update - int(committed))
    return RowAccounting(
        proposed=would_update + replay + unresolved,
        would_update=would_update,
        replay_noop=replay,
        unresolved=unresolved,
        committed=int(committed),
        rolled_back=lost,
    )
