"""event_normalize_receipts.py — receipt accounting for the normalizer.

The receipt is the normalizer's contract with the orchestrator: one receipt per
producer execution, values and rows accounted in separate units, both reconciling
exactly.

What this module owns
---------------------
* the producer name every receipt is sealed under;
* the mapping from one classification plan to the four row classes, classified
  exactly once per page;
* proposing each bundle's values, so refusals are recorded by name rather than
  discovered later;
* success and failure recording, which are the only two ways a batch ends;
* the reconciliation check that must pass before a receipt is trusted.

Two units, never conflated
--------------------------
*values* count ontology-bearing proposals: every proposed value is accepted or
rejected.  *rows* count database rows: every proposed row is classified, and every
planned mutation is either committed or rolled back.  The row equation
``proposed == would_insert + would_update + replay_noop + unresolved`` is enforced
by the validator's own ``classify_rows``; this module supplies the arguments and
verifies the result.

One page is one batch.  A page's classification is reported before its writes are
accounted, so a later page's failure leaves earlier committed batches committed
and fully reported.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable

from scripts.kg.emission import (
    STATE_SEALED,
    EmissionValidator,
    ValidationReceipt,
    reconcile_receipts,
)

from scripts.entities.event_normalize_emission import bundle_value_pairs

__all__ = [
    "PRODUCER",
    "ReceiptError",
    "check_pre_seal_receipt",
    "check_receipt",
    "classify_page",
    "new_validator",
    "page_counts",
    "propose_bundle_values",
    "receipt_problems",
    "record_failure",
    "record_success",
    "rejected_values",
]

#: The producer name this phase seals its receipts under.
PRODUCER = "event_pipeline"


class ReceiptError(RuntimeError):
    """Raised when a receipt cannot be trusted."""


def new_validator(version: str, *, dry_run: bool) -> EmissionValidator:
    """Open one validator, which seals exactly one receipt."""
    return EmissionValidator(PRODUCER, version, dry_run=dry_run)


def page_counts(plan: Any) -> dict[str, int]:
    """The four row classes one page proposes, each counted once.

    The four together equal the plan's ``total_proposed``, so the receipt's
    ``rows_proposed`` and the planner's own arithmetic cannot disagree.
    """
    counts = {
        "would_insert": len(plan.event_inserts),
        "would_update": len(plan.link_updates),
        "replay_noop": len(plan.event_replay_noops) + len(plan.link_replay_noops),
        "unresolved": len(plan.inconsistent),
    }
    if sum(counts.values()) != plan.total_proposed:
        raise ReceiptError(
            f"page classification {sum(counts.values())} does not equal the "
            f"plan's proposed total {plan.total_proposed}"
        )
    return counts


def classify_page(validator: EmissionValidator, plan: Any) -> dict[str, int]:
    """Classify one page's rows exactly once, and return the counts."""
    counts = page_counts(plan)
    validator.classify_rows(**counts)
    return counts


def propose_bundle_values(
    validator: EmissionValidator, work_items: Iterable[Any]
) -> None:
    """Propose every bundle value of every work item.

    A refusal is recorded against its category and value, so a fail-closed
    decision can name exactly which value was refused.
    """
    for item in work_items:
        for category, value in bundle_value_pairs(item.candidate):
            validator.validate(
                category, value, source=f"extraction:{item.extraction_id}"
            )


def rejected_values(receipt: ValidationReceipt) -> tuple[Any, ...]:
    """Every value the registries refused, in order."""
    return tuple(receipt.rejections)


def record_success(validator: EmissionValidator, result: Any) -> None:
    """Account a committed write result."""
    validator.commit(result.rows_committed)


def record_failure(
    validator: EmissionValidator, result: Any, reason: str
) -> None:
    """Account a discarded write result, explaining what was rolled back."""
    validator.rollback(result.rows_rolled_back, reason)


def receipt_problems(receipt: ValidationReceipt) -> tuple[str, ...]:
    """Every reason this receipt cannot be trusted."""
    problems = list(
        reconcile_receipts([receipt], expected_producers=[PRODUCER])
    )
    if not receipt.values_reconcile:
        problems.append(
            f"values do not reconcile: attempted {receipt.values_attempted} != "
            f"accepted {receipt.values_accepted} + rejected "
            f"{receipt.values_rejected}"
        )
    if not receipt.classification_reconciles:
        problems.append(
            f"rows do not reconcile: proposed {receipt.rows_proposed} != "
            f"classified {receipt.rows_would_insert + receipt.rows_would_update + receipt.rows_replay_noop + receipt.rows_unresolved}"
        )
    return tuple(problems)


def check_receipt(receipt: ValidationReceipt) -> None:
    """Raise unless the receipt reconciles in both units."""
    problems = receipt_problems(receipt)
    if problems:
        raise ReceiptError("; ".join(problems))


def check_pre_seal_receipt(receipt: ValidationReceipt) -> None:
    """Reconcile a receipt that has not been sealed yet.

    ``reconcile_receipts`` only accepts a receipt that is already sealed, so this
    reconciles a **copy** projected to the sealed state.  The live receipt is
    never mutated: it stays under the emission validator's lifecycle control, and
    ``validator.seal()`` remains the single operation that seals it.
    """
    check_receipt(dataclasses.replace(receipt, state=STATE_SEALED))
