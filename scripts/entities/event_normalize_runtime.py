"""event_normalize_runtime.py — orchestration for the event normalizer.

Call graph
----------
::

    normalize(engine, limit, dry_run, force)
      └─ validator = new_validator(NORMALIZER_VERSION, dry_run=…)
         loop over pages (keyset cursor, never a page twice):
           fetch_normalization_page(engine, limit=page_cap,
                                    after_extraction_id=cursor, force=force)
             ├─ read failures        → input failure, fail closed (see below)
             ├─ build_plan_from_work_items(page.work_items)
             ├─ validator.start_batch()                [collecting]
             ├─ validate_bundle(build_event_bundle(c)) for EVERY work item
             │      └─ any refusal → whole page refused, no writes
             ├─ validator.complete_validation()
             ├─ apply_classification_plan(engine, plan, …)
             ├─ classify_page(validator, plan)  → commit | rollback
             └─ cursor = page.last_extraction_id
         _finalize(...) → reconcile, seal exactly once, return or raise

One finalization helper
-----------------------
``_finalize`` is the only place that seals.  It reconciles the **live** receipt
before sealing, records any failure on the receipt *before* the seal, then seals
exactly once and attaches the same serialized receipt to both ``stats`` and any
:class:`NormalizationRunError`.  Consequences:

* a sealed receipt can never claim ``failure is None`` for a run that failed;
* a reconciliation failure is itself recorded, so a receipt that will not
  reconcile is reported rather than returned as success;
* the caller always gets the same receipt data whether the run succeeded or not.

Only ``Exception`` is caught.  ``KeyboardInterrupt`` and ``SystemExit`` are
control-flow signals for the process, not run failures, and propagate untouched —
which also means they deliberately do **not** seal a receipt.

Atomic page validation
----------------------
Bundles are validated through the emission boundary's atomic
``validate_bundle``: every component is staged, and a bundle either validates
wholly or records exactly one rejection and commits nothing.  Every candidate
bundle on a page is **attempted before any decision**, so one bad candidate
cannot hide another.  If any bundle is refused, the page is refused as a whole:
all ``plan.total_proposed`` assertions are classified unresolved, nothing is
classified as insert or replay, and no transaction is opened.

Read failures are input failures
--------------------------------
A ``ReadFailure`` means a row could not be interpreted at all, so it never enters
assertion-row accounting: ``rows_proposed`` is untouched and the refusal is
reported through ``read_failures``.  The run fails closed and seals.

Force mode is an operator override, never a convenience
-------------------------------------------------------
``force=True`` widens the read to already-linked extractions and relaxes nothing:
each linked row is revalidated against current evidence and a stale row fails
closed exactly as in normal mode.

This module always builds its plan with ``build_plan_from_work_items``.  It never
calls the low-level planner directly, so a candidate cannot reach the writer
without its stored-state counterpart.

Counters
--------
Legacy: ``extractions`` (rows accepted for normalization), ``events`` (planned in
dry, inserted live), ``skipped`` (rows refused on a page), ``errors`` (runs that
failed or would not reconcile).

Stage 0: ``extractions_examined`` (rows read), ``normalizable`` (rows that became
work items), ``events_planned`` / ``events_inserted``, ``extraction_links_updated``.

Compatibility: ``skipped_unmapped_type`` (retained for the orchestrator's envelope
shape; always 0, because a verb with no registered type is now a read failure
rather than a silent skip), ``events_replay_noop`` (event replay no-ops the write
adapter verified), ``extraction_links_planned`` (links the plan expected to
apply), ``extraction_links_replay_noop`` (links already correct), and
``assertions_inconsistent`` (planner conflicts seen; these fail the run).

Input and outcome: ``read_failures`` (rows the read contract could not
interpret), ``rows_rolled_back`` (mutations discarded with a rolled-back page),
``failure_reason`` (the write adapter's reason, or ``None``), and
``replay_verification_failures`` (replays that failed verification).  A rolled-back
page contributes its *planned* and *rolled-back* counts and contributes no
committed insert or update; earlier committed pages are never reduced.
"""

from __future__ import annotations

from typing import Any

from scripts.entities.event_normalize_accounting import (
    CLASSIFICATION_EQUATION,
    MODE_NORMAL,
    classification_error,
    mode_name,
)
from scripts.entities.event_normalize_emission import build_event_bundle
from scripts.entities.event_normalize_planning import EventAssertion
from scripts.entities.event_normalize_receipts import (
    ReceiptError,
    check_pre_seal_receipt,
    classify_page,
    new_validator,
    record_failure,
    record_success,
)
from scripts.entities.event_normalize_storage import fetch_normalization_page
from scripts.entities.event_normalize_work_items import build_plan_from_work_items
from scripts.entities.event_normalize_write_contract import WriteRolledBackError
from scripts.entities.event_normalize_writes import apply_classification_plan
from scripts.kg.emission import EmissionError

__all__ = ["DEFAULT_PAGE_SIZE", "NORMALIZER_VERSION", "NormalizationRunError", "normalize"]

#: Version recorded on every receipt this runtime seals.
NORMALIZER_VERSION = "event-normalize/1.0"

#: Rows read per page when the caller sets no run-wide limit.
DEFAULT_PAGE_SIZE = 256


class NormalizationRunError(RuntimeError):
    """A failed normalization run, carrying its own evidence.

    Raised for every ordinary failure, so the caller always has the cumulative
    stats, the sealed receipt, the original cause, and whether earlier pages had
    already committed.  ``KeyboardInterrupt`` and ``SystemExit`` are not wrapped.
    """

    def __init__(
        self,
        reason: str,
        *,
        stats: dict[str, Any],
        receipt: dict[str, Any],
        earlier_pages_committed: bool,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stats = stats
        self.receipt = receipt
        self.earlier_pages_committed = bool(earlier_pages_committed)
        self.cause = cause


def _stats() -> dict[str, Any]:
    """Every counter the runtime reports, including the compatibility set."""
    return {
        "extractions": 0,
        "events": 0,
        "skipped": 0,
        "errors": 0,
        "extractions_examined": 0,
        "normalizable": 0,
        "events_planned": 0,
        "events_inserted": 0,
        "extraction_links_updated": 0,
        "skipped_unmapped_type": 0,
        "events_replay_noop": 0,
        "extraction_links_planned": 0,
        "extraction_links_replay_noop": 0,
        "assertions_inconsistent": 0,
        "assertions_unresolved": 0,
        "assertions_refused": 0,
        "read_failures": 0,
        "rows_rolled_back": 0,
        "failure_reason": None,
        "replay_verification_failures": 0,
        # Step 3, additive.  ``events_planned`` and ``normalizable`` keep their
        # existing meanings; these fields make the mode-aware classification
        # explicit so a caller can verify it without re-deriving the equation.
        "rows_committed": 0,
        "accounting_mode": MODE_NORMAL,
        "classification_equation": CLASSIFICATION_EQUATION,
        "classification_reconciles": False,
    }


def _stored_event_ids(work_items: Any) -> dict[str, int]:
    """Map each linked row's event identity to its stored event row id."""
    stored: dict[str, int] = {}
    for item in work_items:
        snapshot = item.existing_event
        if snapshot is None:
            continue
        digest = EventAssertion.from_candidate(item.candidate).identity.digest
        stored[digest] = int(snapshot.event_id)
    return stored


def _validate_bundles(validator: Any, work_items: Any) -> tuple[str, ...]:
    """Attempt every candidate bundle, returning the refusals."""
    refusals: list[str] = []
    for item in work_items:
        bundle = build_event_bundle(item.candidate)
        try:
            validator.validate_bundle(
                bundle, source=f"extraction:{item.extraction_id}"
            )
        except EmissionError as exc:
            refusals.append(f"extraction {item.extraction_id}: {exc}")
    return tuple(refusals)


def _accumulate_discarded_page(stats: dict[str, Any], result: Any) -> None:
    """Align cumulative stats with the receipt for a rolled-back page.

    The page's planned work is counted because the receipt classified it, its
    rollback is counted, and no committed insert or update is added.  Counts from
    earlier committed pages are left untouched.
    """
    stats["events_planned"] += result.events_planned
    stats["extraction_links_planned"] += result.extraction_links_planned
    stats["events_replay_noop"] += result.event_replay_noops
    stats["extraction_links_replay_noop"] += result.extraction_link_replay_noops
    stats["rows_rolled_back"] += result.rows_rolled_back
    stats["failure_reason"] = result.failure_reason
    stats["replay_verification_failures"] = result.replay_verification_failures


def _run(
    engine: Any,
    validator: Any,
    stats: dict[str, Any],
    committed: dict[str, bool],
    *,
    limit: int | None,
    dry_run: bool,
    force: bool,
    page_size: int,
    storage: Any | None,
) -> None:
    """Walk pages until the limit, the end, or a failure."""
    cursor: int | None = None
    examined = 0
    mode = mode_name(force)

    while True:
        remaining = None if limit is None else limit - examined
        if remaining is not None and remaining <= 0:
            break
        page_cap = page_size if remaining is None else min(page_size, remaining)

        page = fetch_normalization_page(
            engine, limit=page_cap, after_extraction_id=cursor, force=force
        )
        if page.examined == 0:
            break
        examined += page.examined
        stats["extractions_examined"] += page.examined

        if page.failures:
            # An input failure, not an assertion row: nothing is classified.
            stats["read_failures"] += len(page.failures)
            reasons = "; ".join(
                f"extraction {failure.extraction_id}: {failure.reason}"
                for failure in page.failures
            )
            validator.fail(f"read failures: {reasons}")
            raise RuntimeError(f"event normalizer read failures: {reasons}")

        plan = build_plan_from_work_items(page.work_items)
        stats["extractions"] += len(page.work_items)
        stats["normalizable"] += len(page.work_items)
        stats["assertions_inconsistent"] += len(plan.inconsistent)

        validator.start_batch()
        refusals = _validate_bundles(validator, page.work_items)

        if refusals or plan.inconsistent:
            # Refuse the page as a whole: every assertion is unresolved, nothing
            # is classified as an insert or a replay, and no transaction opens.
            validator.complete_validation()
            validator.classify_rows(unresolved=plan.total_proposed)
            stats["skipped"] += len(page.work_items)
            # Every assertion slot on a refused page is refused, including the
            # well-formed items: nothing on this page was classified.
            stats["assertions_refused"] += 2 * len(page.work_items)
            reason = "; ".join(refusals) or (
                f"{len(plan.inconsistent)} inconsistent assertion(s)"
            )
            validator.fail(f"page refused: {reason}")
            raise RuntimeError(f"event normalizer refused a page: {reason}")

        validator.complete_validation()

        if dry_run:
            result = apply_classification_plan(engine, plan, dry_run=True)
        else:
            validator.begin_writes()
            try:
                result = apply_classification_plan(
                    engine,
                    plan,
                    storage=storage,
                    stored_event_ids=_stored_event_ids(page.work_items),
                )
            except WriteRolledBackError as exc:
                # Classify once and record the rollback once.  ``rollback`` fails
                # the run itself, so no second failure is recorded here.
                classify_page(validator, plan)
                record_failure(validator, exc.result, str(exc))
                _accumulate_discarded_page(stats, exc.result)
                raise

        classify_page(validator, plan)
        if not dry_run:
            record_success(validator, result)
            committed["page"] = True

        stats["events_planned"] += result.events_planned
        stats["events_inserted"] += result.events_inserted
        stats["events_replay_noop"] += result.event_replay_noops
        stats["extraction_links_planned"] += result.extraction_links_planned
        stats["extraction_links_updated"] += result.extraction_links_updated
        stats["extraction_links_replay_noop"] += result.extraction_link_replay_noops
        stats["rows_committed"] += result.rows_committed
        stats["events"] = (
            stats["events_planned"] if dry_run else stats["events_inserted"]
        )

        imbalance = classification_error(stats, mode=mode)
        if imbalance is not None:
            # Fail closed: never continue past work this run cannot account for.
            raise RuntimeError(imbalance)
        stats["classification_reconciles"] = True

        cursor = page.last_extraction_id
        if not page.has_more:
            break


def _reconcile_before_seal(live: Any) -> tuple[str, ...]:
    """Reconcile the receipt as it *will* be sealed, before sealing it.

    ``reconcile_receipts`` refuses any receipt that is not already sealed, so the
    pre-seal check runs against a *copy* projected to the sealed state, provided
    by the receipt module.  The live receipt is never touched: it stays under the
    validator's lifecycle control, and ``validator.seal()`` remains the single
    operation that seals it.
    """
    try:
        check_pre_seal_receipt(live)
        return ()
    except ReceiptError as exc:
        return (f"receipt does not reconcile: {exc}",)


def _finalize(
    validator: Any,
    stats: dict[str, Any],
    committed: dict[str, bool],
    *,
    reason: str | None = None,
    cause: BaseException | None = None,
) -> dict[str, Any]:
    """The one finalization path: reconcile, seal once, return or raise.

    The live receipt is reconciled *before* sealing.  Any failure — the run's own
    reason, a failure already recorded by a rollback, or a reconciliation failure
    — is written onto the receipt first, so a sealed receipt never claims
    ``failure is None`` for a failed run.  The same serialized receipt is attached
    to ``stats`` and to the error.
    """
    live = validator.receipt

    reasons: list[str] = []
    if live.failure:
        reasons.append(str(live.failure))
    if reason and reason not in reasons:
        reasons.append(reason)
    reasons.extend(_reconcile_before_seal(live))

    # Whether the *classification equation* balances.  Deliberately independent of
    # whether the run failed: a page can be refused (a failure) while every one of
    # its assertion slots is accounted for.  The failure itself is carried by
    # `failure` and the receipt state, not by this flag.
    stats["classification_reconciles"] = (
        classification_error(stats, mode=stats.get("accounting_mode")) is None
    )

    failure = "; ".join(reasons) or None
    if failure is not None:
        validator.fail(failure)
        stats["errors"] += 1

    receipt = validator.seal().serialize()
    stats["validation_receipt"] = receipt

    if failure is not None:
        raise NormalizationRunError(
            f"event normalizer failed: {failure}",
            stats=stats,
            receipt=receipt,
            earlier_pages_committed=bool(committed["page"]),
            cause=cause,
        )
    return stats


def normalize(
    engine: Any,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    storage: Any | None = None,
    version: str = NORMALIZER_VERSION,
) -> dict[str, Any]:
    """Normalize unlinked (or, with ``force``, all) extractions into events.

    Returns the cumulative stats including ``validation_receipt``.  On any
    ordinary failure raises :class:`NormalizationRunError`, which carries the same
    stats, the sealed receipt, the original cause, and whether earlier pages had
    committed.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative or None")
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    validator = new_validator(version, dry_run=dry_run)
    stats = _stats()
    stats["accounting_mode"] = mode_name(force)
    committed = {"page": False}

    try:
        _run(
            engine, validator, stats, committed,
            limit=limit, dry_run=dry_run, force=force,
            page_size=page_size, storage=storage,
        )
    except Exception as exc:  # not BaseException: signals keep their behavior
        return _finalize(validator, stats, committed, reason=str(exc), cause=exc)
    return _finalize(validator, stats, committed)
