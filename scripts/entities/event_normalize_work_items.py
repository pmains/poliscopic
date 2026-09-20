"""event_normalize_work_items.py — work items and the planning bridge (pure).

One row, both sides
-------------------
A :class:`NormalizationWorkItem` is the typed pairing the read contract produces:
the candidate reconstructed from *current* evidence, plus the stored snapshot
when the row is already linked.  The two halves describe the same extraction row
and can never straddle two.

Two planning entry points, deliberately distinct
------------------------------------------------
``build_plan_from_work_items`` is the **runtime** entry point.  It consumes work
items directly, so a caller cannot separate the halves by hand or drop one: every
candidate and every non-null snapshot is carried into the planner.

``build_classification_plan`` remains the **low-level planner**, kept for focused
tests and compatibility.  Runtime integration must go through the bridge instead.
The bridge calls that planner rather than restating any of its logic.

This module is deliberately **pure**: no SQL, no database driver, no validator or
receipt mutation, no subprocess, and no writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.entities.event_normalize_planning import (
    ClassificationPlan,
    build_classification_plan,
)
from scripts.entities.event_normalize_snapshot import ExistingNormalizedEvent

__all__ = [
    "NormalizationWorkItem",
    "WorkItemError",
    "build_plan_from_work_items",
]


class WorkItemError(ValueError):
    """Raised when work items are malformed, repeated, or lose a side."""


@dataclass(frozen=True)
class NormalizationWorkItem:
    """One usable extraction row: its claim plus what is already stored.

    ``candidate`` is always the assertion reconstructed from current evidence.
    ``existing_event`` is the stored snapshot when the row is already linked, and
    ``None`` when it is not.  When a snapshot is present it must describe the same
    extraction row, so a pair can never straddle two rows.
    """

    candidate: NormalizationCandidate
    existing_event: ExistingNormalizedEvent | None = None

    def __post_init__(self) -> None:
        if self.existing_event is None:
            return
        if int(self.existing_event.extraction_id) != int(self.candidate.extraction_id):
            raise WorkItemError(
                "a work item must pair a candidate with the stored snapshot of "
                f"the same extraction row (candidate {self.candidate.extraction_id} "
                f"vs snapshot {self.existing_event.extraction_id})"
            )

    @property
    def extraction_id(self) -> int:
        return int(self.candidate.extraction_id)

    @property
    def is_linked(self) -> bool:
        return self.existing_event is not None


def build_plan_from_work_items(
    work_items: Iterable[NormalizationWorkItem],
) -> ClassificationPlan:
    """Classify work items, carrying both sides of every linked row.

    Every candidate is passed to the planner, and so is every non-null stored
    snapshot, so neither half can be lost between the read contract and the
    classification.  Duplicate extraction rows fail closed rather than being
    silently merged.  Ordering is deterministic regardless of input order,
    because the planner orders its buckets itself.
    """
    items = tuple(work_items)
    candidates: list[NormalizationCandidate] = []
    snapshots: list[ExistingNormalizedEvent] = []
    seen: set[int] = set()

    for item in items:
        if not isinstance(item, NormalizationWorkItem):
            raise WorkItemError(
                f"expected NormalizationWorkItem, got {type(item).__name__}"
            )
        if item.extraction_id in seen:
            raise WorkItemError(
                f"duplicate work item for extraction row {item.extraction_id}"
            )
        seen.add(item.extraction_id)
        candidates.append(item.candidate)
        if item.existing_event is not None:
            snapshots.append(item.existing_event)

    plan = build_classification_plan(candidates, existing_events=snapshots)

    # Conservation: each work item contributes two assertions -- one event and one
    # extraction link -- except an inconsistent one, which contributes only its
    # failed link.  A mismatch here means the bridge dropped a side.
    expected = 2 * len(items) - plan.total_inconsistent
    if plan.total_proposed != expected:
        raise WorkItemError(
            f"bridge dropped work: planned {plan.total_proposed} assertions for "
            f"{len(items)} work items (expected {expected})"
        )
    return plan
