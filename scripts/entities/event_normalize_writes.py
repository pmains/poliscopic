"""event_normalize_writes.py — transaction-aware execution of a validated plan.

Scope
-----
This module *executes* an already classified, already validated
:class:`~scripts.entities.event_normalize_planning.ClassificationPlan`.  It holds
no CLI, no producer iteration, and no classification logic.  Everything that
decides *what may be written* lives in
:mod:`scripts.entities.event_normalize_write_contract`, and the per-unit checks it
applies live in :mod:`scripts.entities.event_normalize_write_units`.

Execution model
---------------
*Validation precedes mutation.*  ``validate_plan_for_write`` runs to completion
before a transaction is opened, so a refused plan cannot leave a partial write.

*One transaction.*  Inserts, links and replay revalidation share a single
transaction.  Any failure discards all of it.

*Replay is revalidated, never assumed.*  A plan carries replay assertions that
were true when it was built; this module re-establishes them against the database
before accepting them.  Every planned replay is re-locked, re-read and re-compared
inside the transaction, and a plan that has gone stale produces a **failed**
result rather than a successful no-op.  A planned replay is never silently
converted into a write.

*Serialized per extraction row.*  Every unit -- mutation or replay -- locks its
extraction row before reading anything, and all of them are processed in one
ascending extraction-id sequence so a replay check can never invert lock order
with a write.

*Links are compare-and-set.*  The attach step only ever fills a NULL link.  A
competing link is never overwritten, and a race that resolves to an exactly
equivalent event is accepted as a verified replay only after that event is
revalidated; the redundant insert is discarded through a savepoint.

*Nothing historical is overwritten.*  Stored events are never updated or removed,
and an existing link is never replaced.

Current-evidence revalidation
-----------------------------
Stored rows carry no durable identity, content-hash, or model-version columns, so
every comparison here is against the evidence as it stands now.  This is
explicitly **not** a historically identity-verified replay, and nothing here
claims otherwise.
"""

from __future__ import annotations

from typing import Any, Mapping

from scripts.entities.event_normalize_planning import (
    ClassificationPlan,
    EventAssertion,
)
from scripts.entities.event_normalize_write_contract import (
    MissingExtractionError,
    PostconditionError,
    ReplayVerificationError,
    RowCountMismatchError,
    WriteResult,
    WriteRolledBackError,
    plan_event_kind,
    validate_plan_for_write,
)
from scripts.entities.event_normalize_write_storage import (
    SqlAlchemyWriteStorage,
    stored_outcome_text,
)
from scripts.entities.event_normalize_write_units import (
    claim_lost_race,
    require_event_matches,
    require_link_points_at,
    resolve_stored_event,
    verify_replay,
)

__all__ = ["apply_classification_plan"]


def _event_values(assertion: EventAssertion, type_ids: Mapping[str, int]) -> dict:
    """The ``meeting_events`` column values for one event assertion."""
    payload = assertion.payload
    return {
        "meeting_id": str(payload.meeting_source_id),
        "supporting_document_id": payload.supporting_document_id,
        "event_type_id": type_ids[assertion.identity.digest],
        "outcome": stored_outcome_text(
            payload.outcome_base, payload.outcome_qualifier
        ),
        "action_verb": payload.action_verb,
        "span_start": payload.span_start,
        "span_end": payload.span_end,
        "case_number": payload.case_number,
    }


def _failure_result(
    plan: ClassificationPlan, reason: str, replay_failures: int = 0
) -> WriteResult:
    """Failure evidence: nothing committed, every planned mutation rolled back.

    Replay counts are deliberately zero: a rolled-back run completed no replay
    assertions, and any replay that failed verification is reported through
    ``replay_verification_failures`` instead of being counted as a no-op.
    """
    planned_events = len(plan.event_inserts)
    planned_links = len(plan.link_updates)
    return WriteResult(
        events_planned=planned_events,
        events_inserted=0,
        event_replay_noops=0,
        extraction_links_planned=planned_links,
        extraction_links_updated=0,
        extraction_link_replay_noops=0,
        rows_committed=0,
        rows_rolled_back=planned_events + planned_links,
        dry_run=False,
        failure_reason=reason,
        replay_verification_failures=replay_failures,
    )


def apply_classification_plan(
    engine: Any,
    plan: ClassificationPlan,
    *,
    dry_run: bool = False,
    storage: Any | None = None,
    stored_event_ids: Mapping[str, int] | None = None,
) -> WriteResult:
    """Apply a validated, coherent plan in one transaction.

    ``dry_run`` validates and accounts for the whole plan without issuing any
    write.  ``stored_event_ids`` maps an event identity digest to a stored event
    row id; it is resolution evidence only, and every event it resolves to is
    still compared against the plan's own assertion payload.
    """
    validate_plan_for_write(plan)

    if dry_run:
        result = WriteResult(
            events_planned=len(plan.event_inserts),
            events_inserted=0,
            event_replay_noops=len(plan.event_replay_noops),
            extraction_links_planned=len(plan.link_updates),
            extraction_links_updated=0,
            extraction_link_replay_noops=len(plan.link_replay_noops),
            rows_committed=0,
            rows_rolled_back=0,
            dry_run=True,
        )
        result.check_reconciliation(plan)
        return result

    writer = storage if storage is not None else SqlAlchemyWriteStorage()
    known = dict(stored_event_ids or {})

    try:
        with engine.begin() as conn:
            counters = _apply_live(conn, plan, writer, known)
    except ReplayVerificationError as exc:
        failed = _failure_result(plan, f"replay verification failed: {exc}", 1)
        failed.check_reconciliation(plan)
        raise WriteRolledBackError(str(exc), failed) from exc
    except Exception as exc:
        failed = _failure_result(plan, str(exc))
        failed.check_reconciliation(plan)
        raise WriteRolledBackError(
            f"normalization write rolled back: {exc}", failed
        ) from exc

    result = WriteResult(dry_run=False, **counters)
    result.check_reconciliation(plan)
    return result


def _apply_live(
    conn: Any,
    plan: ClassificationPlan,
    writer: Any,
    known: Mapping[str, int],
) -> dict[str, int]:
    """Perform the writes and verify postconditions inside the caller's txn."""
    inserts = {a.identity.digest: a for a in plan.event_inserts}
    replays = {a.identity.digest: a for a in plan.event_replay_noops}
    kinds = plan_event_kind(plan)
    planned_events = len(plan.event_inserts)
    planned_links = len(plan.link_updates)

    # Resolve an event type for everything that will be inserted *or* verified,
    # before any mutation, so a missing lookup aborts with nothing written.
    type_ids: dict[str, int] = {}
    for assertion in (*plan.event_inserts, *plan.event_replay_noops):
        type_ids[assertion.identity.digest] = writer.resolve_event_type_id(
            conn, assertion.event_type
        )

    # One ascending extraction-id sequence over every link row, so replay checks
    # and writes cannot invert lock order.
    units = sorted(
        [(int(link.extraction_id), False, link) for link in plan.link_updates]
        + [(int(link.extraction_id), True, link) for link in plan.link_replay_noops],
        key=lambda unit: unit[0],
    )

    verified: dict[str, int] = {}
    events_inserted = 0
    links_updated = 0
    reclassified_events = 0
    reclassified_links = 0

    for extraction_id, is_replay_link, link in units:
        digest = link.expected_event.digest
        assertion = inserts.get(digest) or replays.get(digest)
        payload = assertion.payload if assertion is not None else None
        type_id = type_ids.get(digest)

        state = writer.lock_extraction(conn, extraction_id)
        if not state.exists:
            # A replay whose row vanished is failed verification evidence, not a
            # generic structural error: the plan asserted that row was linked.
            if is_replay_link:
                raise ReplayVerificationError(
                    f"extraction row {extraction_id} was planned as a replay but "
                    "no longer exists"
                )
            raise MissingExtractionError(
                f"extraction row {extraction_id} does not exist"
            )

        if is_replay_link:
            verified[digest] = verify_replay(
                writer, conn, extraction_id, state, digest, payload, type_id, known
            )
            continue

        if kinds.get(digest) == "replay":
            stored = resolve_stored_event(
                writer, conn, extraction_id, digest, known, payload, type_id
            )
            verified[digest] = int(stored.event_id)

            if state.event_id is not None:
                if int(state.event_id) != int(stored.event_id):
                    raise ReplayVerificationError(
                        f"extraction row {extraction_id} is linked to event "
                        f"{state.event_id}, not the resolved stored event "
                        f"{stored.event_id}"
                    )
                reclassified_links += 1
                continue

            if writer.link_extraction(conn, extraction_id, stored.event_id) == 1:
                links_updated += 1
                continue

            later = writer.load_extraction_link(conn, extraction_id)
            if not later.exists:
                raise MissingExtractionError(
                    f"extraction row {extraction_id} vanished mid-transaction"
                )
            if later.event_id is None:
                raise RowCountMismatchError(
                    f"the guarded link for extraction row {extraction_id} affected "
                    "no rows, yet the row is unlinked"
                )
            if int(later.event_id) != int(stored.event_id):
                raise ReplayVerificationError(
                    f"extraction row {extraction_id} was concurrently linked to "
                    f"event {later.event_id}, not the resolved stored event "
                    f"{stored.event_id}"
                )
            reclassified_links += 1
            continue

        # An insert of our own, guarded so a lost race cannot orphan it.
        savepoint = conn.begin_nested()
        try:
            new_id = writer.insert_event(conn, _event_values(assertion, type_ids))
        except Exception:
            savepoint.rollback()
            raise

        if writer.link_extraction(conn, extraction_id, new_id) == 1:
            savepoint.commit()
            verified[digest] = new_id
            events_inserted += 1
            links_updated += 1
            continue

        savepoint.rollback()
        verified[digest] = claim_lost_race(
            writer, conn, extraction_id, digest, payload, type_id, known
        )
        reclassified_events += 1
        reclassified_links += 1

    # Postconditions, still inside the transaction and before commit: they cover
    # the mutations and the replays this run claimed to have verified.
    for assertion in plan.event_inserts:
        digest = assertion.identity.digest
        resolved = verified.get(digest)
        if resolved is None:
            raise PostconditionError(f"event {digest} was never resolved")
        require_event_matches(
            writer, conn, digest, resolved, assertion, type_ids[digest],
            "after the write",
        )

    for assertion in plan.event_replay_noops:
        digest = assertion.identity.digest
        resolved = verified.get(digest)
        if resolved is None:
            raise PostconditionError(
                f"event replay {digest} was never transactionally verified"
            )
        require_event_matches(
            writer, conn, digest, resolved, assertion, type_ids[digest],
            "after verification",
        )

    for link in (*plan.link_updates, *plan.link_replay_noops):
        digest = link.expected_event.digest
        require_link_points_at(
            writer, conn, int(link.extraction_id), verified.get(digest),
            "after the write",
        )

    return {
        "events_planned": planned_events,
        "events_inserted": events_inserted,
        "event_replay_noops": len(plan.event_replay_noops) + reclassified_events,
        "extraction_links_planned": planned_links,
        "extraction_links_updated": links_updated,
        "extraction_link_replay_noops": len(plan.link_replay_noops)
        + reclassified_links,
        "rows_committed": events_inserted + links_updated,
        "rows_rolled_back": 0,
    }
