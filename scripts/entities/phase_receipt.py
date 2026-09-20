"""Shared builder for one sealed canonical phase receipt.

Every receipt-bearing phase must hand the orchestrator exactly one sealed, fully
reconciled :class:`~scripts.kg.emission_receipts.ValidationReceipt`.  The
emission *lifecycle* is identical for all of them, so it is written once here
rather than five times — but the *classification* is not: each producer knows
something different about its own proposals, so it supplies its own accounting
and this module refuses to seal a receipt that does not add up.

Producers supply only what is theirs to know:

* the ontology-bearing values they actually emitted, as ``(category, value)``
  pairs — validated through the canonical registry, so an invalid value is
  recorded as a rejection and fails closed rather than being silently accepted;
* their row accounting, with every proposal already classified exactly once.

``proposed`` is supplied *independently* of the classification, so the
classification equation is a genuine check rather than a tautology.  A receipt
that does not reconcile is refused here rather than sealed and discovered later.

This module is deliberately not a validator: registry checks, receipt equations
and reconciliation all remain owned by :mod:`scripts.kg.emission_checks` and
:mod:`scripts.kg.emission_receipts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from scripts.kg.emission import EmissionValidator
from scripts.kg.producer_versions import declared_producer_version

__all__ = [
    "RowAccounting",
    "ReceiptAccountingError",
    "build_phase_receipt",
]


class ReceiptAccountingError(RuntimeError):
    """Raised when a producer's accounting cannot form a truthful receipt."""


@dataclass(frozen=True)
class RowAccounting:
    """One phase's row accounting, every proposal classified exactly once."""

    proposed: int = 0
    would_insert: int = 0
    would_update: int = 0
    replay_noop: int = 0
    unresolved: int = 0
    committed: int = 0
    rolled_back: int = 0

    @property
    def classified(self) -> int:
        """Proposals that received a classification."""
        return (self.would_insert + self.would_update
                + self.replay_noop + self.unresolved)

    @property
    def mutations(self) -> int:
        """Proposals that would (or did) change stored rows."""
        return self.would_insert + self.would_update

    def problems(self, *, dry_run: bool) -> list[str]:
        """Reasons this accounting cannot describe an honest run."""
        found: list[str] = []
        if self.proposed != self.classified:
            found.append(
                f"proposed {self.proposed} != classified {self.classified} "
                f"(insert {self.would_insert} + update {self.would_update} "
                f"+ replay {self.replay_noop} + unresolved {self.unresolved})"
            )
        for name in ("proposed", "would_insert", "would_update", "replay_noop",
                     "unresolved", "committed", "rolled_back"):
            if getattr(self, name) < 0:
                found.append(f"{name} is negative ({getattr(self, name)})")
        if dry_run and (self.committed or self.rolled_back):
            found.append(
                f"dry run reported mutations (committed {self.committed}, "
                f"rolled_back {self.rolled_back})"
            )
        if not dry_run and self.mutations != self.committed + self.rolled_back:
            found.append(
                f"mutations {self.mutations} != committed {self.committed} "
                f"+ rolled_back {self.rolled_back}"
            )
        return found


def build_phase_receipt(
    producer: str,
    *,
    dry_run: bool,
    values: Iterable[tuple[str, str]] = (),
    rows: RowAccounting | None = None,
    failure: str | None = None,
) -> dict:
    """Validate ``values``, classify ``rows``, and return one sealed receipt.

    ``values`` are canonical ontology values the producer actually emitted, as
    ``(category, value)`` pairs.  ``failure`` marks a phase that ran but did not
    complete; the receipt still seals so prior accounting stays visible.
    """
    rows = rows or RowAccounting()
    version = declared_producer_version(producer)
    if version is None:
        raise ReceiptAccountingError(
            f"producer {producer!r} has no declared version; declare it in "
            "scripts.kg.producer_versions.PRODUCER_VERSIONS"
        )

    problems = rows.problems(dry_run=dry_run)
    if problems and failure is None:
        raise ReceiptAccountingError(
            f"{producer}: refusing to seal an unreconciled receipt: "
            + "; ".join(problems)
        )

    validator = EmissionValidator(producer, version, dry_run=dry_run)
    validator.start_batch()
    for category, value in values:
        validator.validate(category, value, source=producer)
    validator.complete_validation()
    if not dry_run:
        validator.begin_writes()
    validator.classify_rows(
        would_insert=rows.would_insert,
        would_update=rows.would_update,
        replay_noop=rows.replay_noop,
        unresolved=rows.unresolved,
    )
    if not dry_run and rows.committed:
        validator.commit(rows.committed)
    if failure is not None:
        if rows.rolled_back:
            validator.rollback(rows.rolled_back, failure)
        else:
            validator.fail(failure)
    return validator.seal().serialize()


def collect_values(
    *categories: tuple[str, Sequence[str]],
) -> list[tuple[str, str]]:
    """Expand ``(category, values)`` pairs into per-value validation entries.

    Repeats are preserved deliberately: a value emitted for each of N proposals
    is N ontology-value attempts, which is what the receipt's value equation
    counts.
    """
    entries: list[tuple[str, str]] = []
    for category, emitted in categories:
        for value in emitted:
            entries.append((category, str(value)))
    return entries
