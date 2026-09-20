"""event_normalize_write_contract.py — what may be written, and on what evidence.

This module holds the parts of the write contract that involve no transaction: the
typed failures, the semantic comparison of a stored event against an expected
assertion, and the coherence rules that decide whether a plan may be written at
all.  The result type and its equations live in
:mod:`scripts.entities.event_normalize_write_result`.

Identity rule
-------------
The canonical event identity is scoped to the extraction row that produced the
occurrence (``extraction:{extraction_id}:{content_hash}``), so two distinct
extraction rows are distinct source occurrences and therefore distinct event
assertions.  Nothing here treats a lookalike stored row as the same event.

Pairing rule
------------
A plan carries four normal assertion buckets: ``event_inserts``,
``event_replay_noops``, ``link_updates`` and ``link_replay_noops``.  They are two
sides of one correspondence, not two independent lists:

* every extraction-link assertion must target an event assertion that is present
  in the plan, in either the insert or the replay bucket;
* every event assertion must be targeted by exactly one extraction-link
  assertion, in either the update or the replay bucket;
* the extraction row embedded in the event's occurrence must equal the link's
  ``extraction_id``;
* a replay link may not pair with an event insert -- a plan cannot both assert
  that an event does not exist yet and that an extraction is already linked to it.

Missing, duplicate, orphan and cross-wired pairs are all refused *before* a
transaction is opened.

``stored_event_ids`` is resolution evidence only
------------------------------------------------
A supplied stored event id is a *pointer*, never a payload.  It cannot back a link
on its own: the plan must still carry the corresponding ``event_replay_noop``
assertion, and at execution the resolved row is compared against that assertion's
payload.  A supplied id that disagrees with what an extraction is actually linked
to is a verification failure, not a shortcut.

Known limitation, stated deliberately
-------------------------------------
Current stored rows have no durable event-identity, content-hash, or
model-version columns.  An already-linked row can therefore receive
*current-evidence semantic revalidation* -- the stored event is compared against
the candidate as the evidence stands now -- but it must never be described as
*historically identity-verified*.  No field or message here makes that claim.
"""

from __future__ import annotations

from typing import Any, Mapping

from scripts.kg.registries import OutcomeError, canonicalize_outcome

from scripts.entities.event_normalize_planning import (
    ClassificationPlan,
    EventAssertion,
    ExtractionLinkAssertion,
    InconsistentAssertion,
    PlanInvariantError,
)
from scripts.entities.event_normalize_write_result import (
    ReconciliationError,
    WriteResult,
)
from scripts.entities.event_normalize_write_storage import StoredEventRow

__all__ = [
    "ConcurrentConflictError",
    "MissingExtractionError",
    "PlanNotWritableError",
    "PostconditionError",
    "ReconciliationError",
    "ReplayVerificationError",
    "RowCountMismatchError",
    "UnresolvedEventIdError",
    "WriteError",
    "WriteResult",
    "WriteRolledBackError",
    "plan_coherence_problems",
    "stored_event_matches",
    "validate_plan_for_write",
]


class WriteError(RuntimeError):
    """Base class for every write-contract failure."""


class PlanNotWritableError(WriteError):
    """The plan was refused before any mutation was attempted."""


class ConcurrentConflictError(WriteError):
    """Stored state disagrees with the plan; the transaction must roll back."""


class MissingExtractionError(WriteError):
    """A link targets an extraction row that does not exist."""


class RowCountMismatchError(WriteError):
    """A statement affected a different number of rows than required."""


class UnresolvedEventIdError(WriteError):
    """A link's expected event could not be resolved to a stored row."""


class PostconditionError(WriteError):
    """A postcondition failed before commit."""


class ReplayVerificationError(WriteError):
    """A planned replay assertion failed transaction-time revalidation.

    Raised instead of silently converting a planned replay into a write, and
    reported through ``replay_verification_failures`` so a stale plan produces a
    failed result rather than a successful no-op.
    """


class WriteRolledBackError(WriteError):
    """The transaction was discarded; ``result`` carries the failure evidence.

    The carried result always reports ``rows_committed == 0`` and accounts the
    would-mutate rows as rolled back, so failure evidence can never claim a commit.
    """

    def __init__(self, message: str, result: WriteResult) -> None:
        super().__init__(message)
        self.result = result


def stored_event_matches(stored: StoredEventRow, payload: Any) -> bool:
    """Whether a stored row is semantically exactly the expected assertion.

    This is current-evidence revalidation, not a historical identity check: the
    stored row is compared field by field against the candidate built from the
    evidence as it stands now, and the stored outcome is canonicalized through the
    registry rather than compared textually.
    """
    if str(stored.meeting_id) != str(payload.meeting_source_id):
        return False
    if stored.supporting_document_id != payload.supporting_document_id:
        return False
    if stored.span_start != payload.span_start or stored.span_end != payload.span_end:
        return False
    if stored.case_number != payload.case_number:
        return False
    if stored.action_verb != payload.action_verb:
        return False
    try:
        canonical = canonicalize_outcome(stored.outcome)
    except OutcomeError:
        return False
    return (
        canonical.base == payload.outcome_base
        and canonical.qualifier == payload.outcome_qualifier
    )


def _occurrence_extraction_id(occurrence: str) -> int | None:
    """The extraction row id embedded in a canonical occurrence string."""
    prefix, _, remainder = str(occurrence).partition(":")
    if prefix != "extraction":
        return None
    head, _, _ = remainder.partition(":")
    try:
        return int(head)
    except ValueError:
        return None


def plan_event_kind(plan: ClassificationPlan) -> dict[str, str]:
    """Map each event identity digest to ``"insert"`` or ``"replay"``."""
    kinds: dict[str, str] = {}
    for assertion in plan.event_inserts:
        kinds[assertion.identity.digest] = "insert"
    for assertion in plan.event_replay_noops:
        kinds.setdefault(assertion.identity.digest, "replay")
    return kinds


def plan_event_assertions(plan: ClassificationPlan) -> dict[str, EventAssertion]:
    """Map each event identity digest to its assertion."""
    assertions: dict[str, EventAssertion] = {}
    for assertion in (*plan.event_inserts, *plan.event_replay_noops):
        assertions.setdefault(assertion.identity.digest, assertion)
    return assertions


def plan_coherence_problems(plan: ClassificationPlan) -> tuple[str, ...]:
    """Every way the plan's event/link correspondence fails to line up."""
    kinds = plan_event_kind(plan)
    assertions = plan_event_assertions(plan)
    problems: list[str] = []

    links: list[tuple[Any, str]] = [
        (link, "update") for link in plan.link_updates
    ]
    links += [(link, "replay") for link in plan.link_replay_noops]

    claimed: dict[str, int] = {}
    for link, link_kind in links:
        digest = link.expected_event.digest
        extraction_id = int(link.extraction_id)

        if digest in claimed:
            problems.append(
                f"event identity {digest} is claimed by extraction rows "
                f"{claimed[digest]} and {extraction_id}; the identity contract "
                "does not permit two extraction links on one event"
            )
        else:
            claimed[digest] = extraction_id

        kind = kinds.get(digest)
        if kind is None:
            problems.append(
                f"link for extraction row {extraction_id} targets event identity "
                f"{digest}, which is backed by no EventAssertion in this plan; a "
                "stored event id is resolution evidence only and cannot substitute "
                "for the assertion payload"
            )
            continue

        if link_kind == "replay" and kind == "insert":
            problems.append(
                f"replay link for extraction row {extraction_id} pairs with the "
                f"event insert {digest}; a plan cannot both insert that event and "
                "assert the extraction is already linked to it"
            )

        embedded = _occurrence_extraction_id(assertions[digest].payload.occurrence)
        if embedded is None:
            problems.append(
                f"event assertion {digest} has an unreadable occurrence "
                f"{assertions[digest].payload.occurrence!r}"
            )
        elif embedded != extraction_id:
            problems.append(
                f"link for extraction row {extraction_id} expects event {digest}, "
                f"which was built from extraction row {embedded}"
            )

    orphans = sorted(set(kinds) - set(claimed))
    if orphans:
        problems.append(
            f"{len(orphans)} EventAssertion(s) are referenced by no "
            "extraction-link assertion"
        )

    return tuple(problems)


def validate_plan_for_write(plan: Any) -> None:
    """Refuse a plan that must not be written.  Runs before any mutation."""
    if not isinstance(plan, ClassificationPlan):
        raise PlanNotWritableError(
            f"expected a ClassificationPlan, got {type(plan).__name__}"
        )

    for label, bucket, expected in (
        ("event_inserts", plan.event_inserts, EventAssertion),
        ("event_replay_noops", plan.event_replay_noops, EventAssertion),
        ("link_updates", plan.link_updates, ExtractionLinkAssertion),
        ("link_replay_noops", plan.link_replay_noops, ExtractionLinkAssertion),
        ("inconsistent", plan.inconsistent, InconsistentAssertion),
    ):
        for assertion in bucket:
            if not isinstance(assertion, expected):
                raise PlanNotWritableError(
                    f"{label} holds {type(assertion).__name__}, expected "
                    f"{expected.__name__}"
                )

    if plan.inconsistent:
        raise PlanNotWritableError(
            f"plan carries {len(plan.inconsistent)} inconsistent assertion(s); "
            "a plan must be consistent before it can be written"
        )

    try:
        plan.check_invariants()
    except PlanInvariantError as exc:
        raise PlanNotWritableError(f"plan invariants are broken: {exc}") from exc

    insert_ids = {a.identity.digest for a in plan.event_inserts}
    replay_ids = {a.identity.digest for a in plan.event_replay_noops}
    if insert_ids & replay_ids:
        raise PlanNotWritableError(
            f"event identities appear as both insert and replay: "
            f"{sorted(insert_ids & replay_ids)!r}"
        )

    update_keys = {a.key for a in plan.link_updates}
    replay_keys = {a.key for a in plan.link_replay_noops}
    if update_keys & replay_keys:
        raise PlanNotWritableError(
            f"link identities appear as both update and replay: "
            f"{sorted(update_keys & replay_keys)!r}"
        )

    problems = plan_coherence_problems(plan)
    if problems:
        raise PlanNotWritableError("plan is not coherent: " + "; ".join(problems))
