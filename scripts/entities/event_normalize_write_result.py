"""event_normalize_write_result.py — the write result and its equations.

``WriteResult`` measures the two assertion classes separately and states, in one
place, exactly what each mode may claim.

Semantics of the replay counters
--------------------------------
``event_replay_noops`` and ``extraction_link_replay_noops`` count replay
assertions **accounted for in this result**:

* live success -- only replays that passed transaction-time verification, plus
  planned inserts that were satisfied by an already-stored equivalent event;
* dry run -- the replays the plan expects, since nothing is executed or verified
  and ``dry_run`` says so explicitly;
* failure -- **zero**.  A stale or unverifiable replay is never reported as a
  completed no-op; it is reported through ``replay_verification_failures`` and
  ``failure_reason`` instead.

That is why the failure branch forbids nonzero replay counts: a run that rolled
back completed no replay assertions, and saying otherwise would be the exact
dishonesty this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.entities.event_normalize_planning import ClassificationPlan

__all__ = ["ReconciliationError", "WriteResult"]


class ReconciliationError(RuntimeError):
    """A write result failed one of its own reconciliation equations."""


@dataclass(frozen=True)
class WriteResult:
    """The two assertion classes, measured separately."""

    events_planned: int
    events_inserted: int
    event_replay_noops: int
    extraction_links_planned: int
    extraction_links_updated: int
    extraction_link_replay_noops: int
    rows_committed: int
    rows_rolled_back: int
    dry_run: bool
    failure_reason: str | None = None
    replay_verification_failures: int = 0

    @property
    def would_mutate(self) -> int:
        """Planned event inserts plus planned extraction-link updates."""
        return self.events_planned + self.extraction_links_planned

    @property
    def rows_reclassified_as_replay(self) -> int:
        """Planned mutations that turned out to need no write."""
        return (
            self.events_planned
            - self.events_inserted
            + self.extraction_links_planned
            - self.extraction_links_updated
        )

    @property
    def succeeded(self) -> bool:
        """Whether this result describes a completed, non-dry run."""
        return not self.dry_run and self.failure_reason is None

    def check_reconciliation(self, plan: ClassificationPlan) -> None:
        """Raise unless every equation for the mode that ran holds."""
        if self.events_planned != len(plan.event_inserts):
            raise ReconciliationError("events_planned does not match the plan")
        if self.extraction_links_planned != len(plan.link_updates):
            raise ReconciliationError(
                "extraction_links_planned does not match the plan"
            )
        if self.rows_committed != self.events_inserted + self.extraction_links_updated:
            raise ReconciliationError(
                "rows_committed must equal inserted events plus updated links"
            )
        if self.events_inserted > self.events_planned:
            raise ReconciliationError("inserted more events than were planned")
        if self.extraction_links_updated > self.extraction_links_planned:
            raise ReconciliationError("updated more links than were planned")
        if self.rows_rolled_back < 0 or self.rows_committed < 0:
            raise ReconciliationError("row counters cannot be negative")
        if self.replay_verification_failures < 0:
            raise ReconciliationError("verification failures cannot be negative")

        if self.dry_run:
            if self.failure_reason is not None:
                raise ReconciliationError("a dry run cannot report a failure reason")
            if self.replay_verification_failures:
                raise ReconciliationError("a dry run verifies no replays")
            if self.events_inserted or self.extraction_links_updated:
                raise ReconciliationError("a dry run must not write")
            if self.rows_committed or self.rows_rolled_back:
                raise ReconciliationError("a dry run commits and rolls back nothing")
            if self.event_replay_noops != len(plan.event_replay_noops):
                raise ReconciliationError("dry event replay count must be the plan's")
            if self.extraction_link_replay_noops != len(plan.link_replay_noops):
                raise ReconciliationError("dry link replay count must be the plan's")
            return

        if self.failure_reason is not None:
            if (
                self.rows_committed != 0
                or self.events_inserted
                or self.extraction_links_updated
            ):
                raise ReconciliationError(
                    "a rolled-back run must not claim a committed row"
                )
            if self.rows_rolled_back != self.would_mutate:
                raise ReconciliationError(
                    "a rolled-back run must account every planned mutation as "
                    "rolled back"
                )
            if self.event_replay_noops or self.extraction_link_replay_noops:
                raise ReconciliationError(
                    "a rolled-back run must not report replay no-ops as completed"
                )
            return

        if self.rows_rolled_back != 0:
            raise ReconciliationError("a successful run rolls nothing back")
        if self.replay_verification_failures:
            raise ReconciliationError("a successful run has no failed verifications")
        if self.events_inserted + self.event_replay_noops != (
            self.events_planned + len(plan.event_replay_noops)
        ):
            raise ReconciliationError(
                "every planned event assertion must resolve to an insert or a "
                "verified replay"
            )
        if self.extraction_links_updated + self.extraction_link_replay_noops != (
            self.extraction_links_planned + len(plan.link_replay_noops)
        ):
            raise ReconciliationError(
                "every planned link assertion must resolve to an update or a "
                "verified replay"
            )
        if self.event_replay_noops < len(plan.event_replay_noops):
            raise ReconciliationError(
                "a successful run must have verified every planned event replay"
            )
        if self.extraction_link_replay_noops < len(plan.link_replay_noops):
            raise ReconciliationError(
                "a successful run must have verified every planned link replay"
            )
        if self.would_mutate != self.rows_committed + self.rows_reclassified_as_replay:
            raise ReconciliationError(
                "planned mutations must be exactly committed plus reclassified"
            )
