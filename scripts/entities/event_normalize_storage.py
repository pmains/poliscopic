"""event_normalize_storage.py — typed page assembly for event normalization.

Read-only by construction
-------------------------
This module reads.  It issues no statement of its own beyond the page query it
asks :mod:`scripts.entities.event_normalize_query` to compose, performs no
mutation of any kind, and opens no transaction that could write.

Work-item partition
-------------------
The authoritative partition of a page is:

``work_items``
    One :class:`NormalizationWorkItem` per successfully interpreted extraction
    row.  A work item always carries the candidate reconstructed from *current*
    evidence, and carries the stored snapshot as well when the row is linked.
``failures``
    One :class:`ReadFailure` per unusable row.

A stored snapshot is never returned *instead of* a candidate: a linked row must
present both sides so that ``build_classification_plan`` can compare them.  A
linked row whose current evidence is unusable fails closed rather than letting
stored state bypass current-evidence validation.

Compatibility accessors
-----------------------
``NormalizationPage.candidates`` and ``NormalizationPage.existing_events`` are
derived from ``work_items`` on demand.  They are conveniences for readers and
tests, not separate authoritative collections, so they cannot drift from the
partition that accounting validates.

Planning
--------
Runtime classification goes through
:func:`scripts.entities.event_normalize_work_items.build_plan_from_work_items`,
which consumes these work items directly and cannot lose either side of a linked
row.  ``build_classification_plan`` remains the low-level planner kept for
focused tests and compatibility; it is not the runtime entry point.

Pagination
----------
Ordering is by extraction id ascending and the cursor is exclusive
(``e.id > :after_extraction_id``).  Passing back ``page.last_extraction_id``
therefore always advances, which is what makes a dry run terminate: a dry run
writes nothing, so an inclusive or offset-based cursor would return the same
first page forever.  ``has_more`` means another page *may* exist: it is true
exactly when the page was full, and is never claimed for an unbounded read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import text

from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.entities.event_normalize_query import (
    REASON_INCOMPLETE_CONTEXT,
    REASON_INVALID_ACTION_VERB,
    REASON_INVALID_EVENT_TYPE,
    REASON_INVALID_OUTCOME,
    REASON_MALFORMED_OFFSETS,
    REASON_MISSING_EXTRACTOR,
    REASON_MISSING_JURISDICTION,
    REASON_MISSING_LINKED_EVENT,
    REASON_MISSING_MEETING,
    REASON_MISSING_PUBLIC_BODY,
    REASON_MISSING_SUPPORTING_DOCUMENT,
    REASON_MISSING_TEXT,
    REASON_UNSUPPORTED_METHOD,
    UNBOUNDED_LIMIT,
    ReadFailure,
    analyzed_text_hash,
    build_candidate_from_row,
    build_page_query,
    build_snapshot_from_row,
    row_extraction_id,
    row_is_linked,
)
from scripts.entities.event_normalize_snapshot import ExistingNormalizedEvent
from scripts.entities.event_normalize_work_items import (
    NormalizationWorkItem,
    WorkItemError,
)

__all__ = [
    "REASON_INCOMPLETE_CONTEXT",
    "REASON_INVALID_ACTION_VERB",
    "REASON_INVALID_EVENT_TYPE",
    "REASON_INVALID_OUTCOME",
    "REASON_MALFORMED_OFFSETS",
    "REASON_MISSING_EXTRACTOR",
    "REASON_MISSING_JURISDICTION",
    "REASON_MISSING_LINKED_EVENT",
    "REASON_MISSING_MEETING",
    "REASON_MISSING_PUBLIC_BODY",
    "REASON_MISSING_SUPPORTING_DOCUMENT",
    "REASON_MISSING_TEXT",
    "REASON_UNSUPPORTED_METHOD",
    "NormalizationPage",
    "NormalizationWorkItem",
    "PageAccountingError",
    "ReadFailure",
    "WorkItemError",
    "analyzed_text_hash",
    "fetch_normalization_page",
]


class PageAccountingError(RuntimeError):
    """Raised when a page does not account for every fetched row exactly once."""


@dataclass(frozen=True)
class NormalizationPage:
    """One typed page of normalization work."""

    work_items: tuple[NormalizationWorkItem, ...]
    failures: tuple[ReadFailure, ...]
    examined: int
    last_extraction_id: int | None
    has_more: bool

    @property
    def candidates(self) -> tuple[NormalizationCandidate, ...]:
        """Derived view: the current-evidence candidate of every work item."""
        return tuple(item.candidate for item in self.work_items)

    @property
    def existing_events(self) -> Mapping[int, ExistingNormalizedEvent]:
        """Derived view: stored snapshots keyed by integer extraction row id."""
        return {
            item.extraction_id: item.existing_event
            for item in self.work_items
            if item.existing_event is not None
        }

    def check_accounting(self) -> None:
        """Prove ``work_items`` and ``failures`` partition the fetched rows."""
        accounted = len(self.work_items) + len(self.failures)
        if self.examined != accounted:
            raise PageAccountingError(
                f"examined {self.examined} != work items {len(self.work_items)} "
                f"+ failures {len(self.failures)}"
            )

        work_ids = [item.extraction_id for item in self.work_items]
        failure_ids = [f.extraction_id for f in self.failures]

        if len(work_ids) != len(set(work_ids)):
            raise PageAccountingError("an extraction row produced two work items")
        if len(failure_ids) != len(set(failure_ids)):
            raise PageAccountingError("an extraction row produced two failures")

        overlap = set(work_ids) & set(failure_ids)
        if overlap:
            raise PageAccountingError(
                f"rows accounted as both a work item and a failure: "
                f"{sorted(overlap)!r}"
            )

        every_id = set(work_ids) | set(failure_ids)
        if len(every_id) != accounted:
            raise PageAccountingError(
                "a row was accounted for more than once, or none at all"
            )

        if self.examined == 0:
            if self.last_extraction_id is not None:
                raise PageAccountingError("an empty page must not report a cursor")
        elif self.last_extraction_id != max(every_id):
            raise PageAccountingError(
                f"last_extraction_id {self.last_extraction_id} is not the highest "
                f"id in the page ({max(every_id)})"
            )


def fetch_normalization_page(
    engine: Any,
    *,
    limit: int | None = None,
    after_extraction_id: int | None = None,
    force: bool = False,
) -> NormalizationPage:
    """Read one deterministic page of normalization work.

    ``limit`` bounds the page and is echoed honestly through ``has_more``.
    ``after_extraction_id`` is an exclusive cursor; feed it
    ``page.last_extraction_id`` to advance.  Normal mode selects only unlinked
    rows; ``force`` includes linked rows as well, each of which yields a work item
    carrying both its current candidate and its stored snapshot.
    """
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer or None")
    cursor: int | None = None
    if after_extraction_id is not None:
        cursor = int(after_extraction_id)

    params: dict[str, Any] = {"limit": limit if limit is not None else UNBOUNDED_LIMIT}
    if cursor is not None:
        params["after_extraction_id"] = cursor

    query = build_page_query(force=force, has_cursor=cursor is not None)
    with engine.connect() as connection:
        rows = connection.execute(text(query), params).fetchall()

    work_items: list[NormalizationWorkItem] = []
    failures: list[ReadFailure] = []
    last_id: int | None = None

    for row in rows:
        last_id = row_extraction_id(row)

        # The candidate is reconstructed for every usable row, linked or not,
        # so stored state can never stand in for current-evidence validation.
        candidate = build_candidate_from_row(row)
        if isinstance(candidate, ReadFailure):
            failures.append(candidate)
            continue

        if not row_is_linked(row):
            work_items.append(NormalizationWorkItem(candidate=candidate))
            continue

        snapshot = build_snapshot_from_row(row)
        if isinstance(snapshot, ReadFailure):
            failures.append(snapshot)
            continue

        work_items.append(
            NormalizationWorkItem(candidate=candidate, existing_event=snapshot)
        )

    page = NormalizationPage(
        work_items=tuple(work_items),
        failures=tuple(failures),
        examined=len(rows),
        last_extraction_id=last_id,
        has_more=limit is not None and len(rows) == limit,
    )
    page.check_accounting()
    return page
