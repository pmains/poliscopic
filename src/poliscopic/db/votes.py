"""Vote normalization and tally helpers."""

from typing import Optional

from sqlalchemy import select

from poliscopic.db.models import MemberVote, Supervisor


def _normalize_vote_value(vote: str) -> str:
    """Normalize a vote value to its canonical representation."""
    value = (vote or "").lower().strip()
    if value in ("yes", "aye"):
        return "yes"
    if value in ("no", "nay"):
        return "no"
    if value in ("abstain", "abstained"):
        return "abstain"
    if value == "absent":
        return "absent"
    if value == "recused":
        return "recused"
    return value


def _make_supervisor_slug(supervisor: Supervisor) -> str:
    """Derive a URL-safe slug from a supervisor record."""
    return supervisor.normalized_name.replace(" ", "-")


def infer_majority_position(session, aiv_id: int) -> Optional[str]:
    """Return the substantive majority position for one agenda-item vote."""
    votes = session.execute(
        select(MemberVote.vote).where(MemberVote.agenda_item_vote_id == aiv_id)
    ).scalars().all()
    normalized = [_normalize_vote_value(vote) for vote in votes]
    yes_count = sum(1 for vote in normalized if vote == "yes")
    no_count = sum(1 for vote in normalized if vote == "no")
    if yes_count == 0 and no_count == 0:
        return None
    if yes_count > no_count:
        return "yes"
    if no_count > yes_count:
        return "no"
    return "tie"


def compute_vote_tally(session, aiv_id: int) -> dict[str, int]:
    """Return yes, no, abstain, and total counts for one agenda-item vote."""
    votes = session.execute(
        select(MemberVote.vote).where(MemberVote.agenda_item_vote_id == aiv_id)
    ).scalars().all()
    normalized = [_normalize_vote_value(vote) for vote in votes]
    return {
        "yes": sum(1 for vote in normalized if vote == "yes"),
        "no": sum(1 for vote in normalized if vote == "no"),
        "abstain": sum(1 for vote in normalized if vote == "abstain"),
        "total": len(votes),
    }
