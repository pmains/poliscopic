"""event_normalize_write_units.py — per-unit verification and postconditions.

One *unit* is a single extraction-link assertion plus the event assertion it
pairs with.  This module holds the checks a unit must pass inside the
transaction: revalidating a planned replay, resolving a lost compare-and-set
race, resolving an already-stored event through supplied id evidence, and the
postconditions that run before commit.

Everything here raises a typed write error rather than returning a verdict, so a
caller cannot accidentally treat a failed check as a pass.  A planned replay that
no longer holds raises :class:`ReplayVerificationError`; it is never silently
converted into a write.
"""

from __future__ import annotations

from typing import Any, Mapping

from scripts.entities.event_normalize_planning import EventAssertion
from scripts.entities.event_normalize_write_contract import (
    ConcurrentConflictError,
    MissingExtractionError,
    PostconditionError,
    ReplayVerificationError,
    RowCountMismatchError,
    UnresolvedEventIdError,
    stored_event_matches,
)

__all__ = [
    "claim_lost_race",
    "require_event_matches",
    "require_link_points_at",
    "resolve_stored_event",
    "verify_replay",
]


def verify_replay(
    writer: Any,
    conn: Any,
    extraction_id: int,
    state: Any,
    digest: str,
    payload: Any,
    type_id: int | None,
    known: Mapping[str, int],
) -> int:
    """Re-establish one planned replay inside the transaction, or fail.

    Every check is performed here rather than assumed from planning: the row must
    still exist and still be linked, any supplied stored id must agree with what
    it is actually linked to, and the linked event must still exist with the same
    event type and the same persisted semantics.
    """
    if payload is None:
        raise ReplayVerificationError(
            f"replay link for extraction row {extraction_id} has no corresponding "
            "event replay assertion"
        )
    if state.event_id is None:
        raise ReplayVerificationError(
            f"extraction row {extraction_id} was planned as a replay but is now "
            "unlinked; a planned replay is never converted into a write"
        )

    linked_id = int(state.event_id)
    supplied = known.get(digest)
    if supplied is not None and int(supplied) != linked_id:
        raise ReplayVerificationError(
            f"supplied stored event id {int(supplied)} for {digest} disagrees with "
            f"the event actually linked to extraction row {extraction_id} "
            f"({linked_id})"
        )

    stored = writer.load_event(conn, linked_id)
    if stored is None:
        raise ReplayVerificationError(
            f"event {linked_id} linked from extraction row {extraction_id} no "
            "longer exists"
        )
    if type_id is not None and stored.event_type_id != type_id:
        raise ReplayVerificationError(
            f"event {linked_id} has event type id {stored.event_type_id}, expected "
            f"{type_id}"
        )
    if not stored_event_matches(stored, payload):
        raise ReplayVerificationError(
            f"event {linked_id} no longer matches the replay assertion {digest}"
        )
    return linked_id


def claim_lost_race(
    writer: Any,
    conn: Any,
    extraction_id: int,
    digest: str,
    payload: Any,
    type_id: int | None,
    known: Mapping[str, int],
) -> int:
    """Resolve a lost compare-and-set race, returning the winning event id.

    The competing link is never overwritten.  It is accepted only when it points
    at an event whose stored semantics match the expected assertion.
    """
    later = writer.load_extraction_link(conn, extraction_id)
    if not later.exists:
        raise MissingExtractionError(
            f"extraction row {extraction_id} vanished mid-transaction"
        )
    if later.event_id is None:
        raise RowCountMismatchError(
            f"the guarded link for extraction row {extraction_id} affected no "
            "rows, yet the row is unlinked"
        )

    linked_id = int(later.event_id)
    supplied = known.get(digest)
    if supplied is not None and int(supplied) != linked_id:
        raise ReplayVerificationError(
            f"supplied stored event id {int(supplied)} for {digest} disagrees with "
            f"the event actually linked to extraction row {extraction_id} "
            f"({linked_id})"
        )

    stored = writer.load_event(conn, linked_id)
    if stored is None:
        raise ReplayVerificationError(
            f"event {linked_id} linked from extraction row {extraction_id} no "
            "longer exists"
        )
    if type_id is not None and stored.event_type_id != type_id:
        raise ConcurrentConflictError(
            f"event {linked_id} linked from extraction row {extraction_id} has "
            f"event type id {stored.event_type_id}, expected {type_id}"
        )
    if payload is not None and not stored_event_matches(stored, payload):
        raise ConcurrentConflictError(
            f"extraction row {extraction_id} was concurrently linked to event "
            f"{linked_id}, which does not match the expected semantics"
        )
    return linked_id


def resolve_stored_event(
    writer: Any,
    conn: Any,
    extraction_id: int,
    digest: str,
    known: Mapping[str, int],
    payload: Any,
    type_id: int | None,
) -> Any:
    """Resolve an already-stored event through supplied id evidence, then verify.

    A supplied id is a pointer, never a payload: the resolved row is still
    compared against the plan's own assertion before it is trusted.
    """
    supplied = known.get(digest)
    if supplied is None:
        raise UnresolvedEventIdError(
            f"link for extraction row {extraction_id} targets the already-stored "
            f"event {digest}; a stored event id is required to resolve it, and it "
            "is resolution evidence only"
        )

    stored = writer.load_event(conn, int(supplied))
    if stored is None:
        raise UnresolvedEventIdError(
            f"supplied stored event id {int(supplied)} for {digest} does not exist"
        )
    if type_id is not None and stored.event_type_id != type_id:
        raise ReplayVerificationError(
            f"supplied stored event {stored.event_id} has event type id "
            f"{stored.event_type_id}, expected {type_id}"
        )
    if payload is not None and not stored_event_matches(stored, payload):
        raise ReplayVerificationError(
            f"supplied stored event {stored.event_id} does not match the assertion "
            f"{digest}"
        )
    return stored


def require_event_matches(
    writer: Any,
    conn: Any,
    digest: str,
    event_id: int,
    assertion: EventAssertion,
    type_id: int | None,
    stage: str,
) -> None:
    """One postcondition over a resolved event."""
    stored = writer.load_event(conn, event_id)
    if stored is None:
        raise PostconditionError(f"event {digest} is missing {stage}")
    if type_id is not None and stored.event_type_id != type_id:
        raise PostconditionError(
            f"event {digest} persisted the wrong event type id {stage}"
        )
    if not stored_event_matches(stored, assertion.payload):
        raise PostconditionError(f"event {digest} does not match the plan {stage}")


def require_link_points_at(
    writer: Any,
    conn: Any,
    extraction_id: int,
    expected_id: int | None,
    stage: str,
) -> None:
    """One postcondition over a resolved extraction link."""
    state = writer.load_extraction_link(conn, extraction_id)
    if not state.exists:
        raise PostconditionError(f"extraction row {extraction_id} is missing {stage}")
    if state.event_id != expected_id:
        raise PostconditionError(
            f"extraction row {extraction_id} is not linked to the expected event "
            f"{stage}"
        )
