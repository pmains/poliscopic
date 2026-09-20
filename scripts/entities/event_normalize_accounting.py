#!/usr/bin/env python3
"""``event_normalize_accounting.py`` — mode-aware, replay-aware accounting (pure).

Replaces the legacy assumption ``events_planned == normalizable``.

Why the legacy equation was wrong
---------------------------------
It held only while every examined row was a planned insert.  Force mode
deliberately re-reads already-linked rows, so a clean replay legitimately has
``normalizable > 0`` with ``events_planned == 0``.  The legacy check therefore
failed on a perfectly healthy force replay.

The exact contract
------------------
The planner emits two assertions per work item — one event, one extraction link —
except a mismatched item, which emits only its failed link.  Classifying every
assertion into mutually exclusive buckets gives, per page and cumulatively:

    total_assertions == inserts + link_writes + event_replays + link_replays
                        + unresolved

and the planning bridge guarantees conservation at the work-item level:

    total_assertions == 2 * normalizable - unresolved

Collecting terms yields the single exact, mode-independent equation enforced
here:

    2 * normalizable == inserts + link_writes + event_replays + link_replays
                        + 2 * unresolved

It reduces exactly to the legacy equation when ``replays`` and ``unresolved`` are
zero, so it *generalises* the old rule rather than redefining any existing
counter.

Mode gates
----------
``normal`` mode reads only unlinked rows, so it may not classify replay work.
``force`` mode may.  Both modes must satisfy the equation above.

This module is pure: no SQL, no engine, no receipt or validator mutation, no
writes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

__all__ = [
    "CLASSIFICATION_EQUATION",
    "MODE_FORCE",
    "MODE_NORMAL",
    "WORK_FIELDS",
    "child_accounting_error",
    "classification_error",
    "legacy_classification_error",
    "mode_name",
]

MODE_NORMAL = "normal"
MODE_FORCE = "force"

#: The enforced work-classification equation, named for the envelope.
#:
#: One unit throughout: **assertion slots**.  Every work item owes two (one event,
#: one extraction link), so the left side is ``2 * normalizable``.  The right side
#: accounts for every slot exactly once:
#:
#: * ``planned``   — classified as an insert/update,
#: * ``replay``    — classified as an idempotent replay,
#: * ``unresolved``— slots whose item could not be classified on its own terms,
#: * ``refused``   — every slot on a page refused as a whole.
#:
#: ``assertions_inconsistent`` is deliberately **not** a term here.  It counts
#: *inconsistent assertions* for diagnosis, which is a different unit; multiplying
#: it into the equation double-counted mismatched items and produced a residual
#: equal to twice the well-formed items on a refused page.
CLASSIFICATION_EQUATION = (
    "2 * normalizable == (events_planned + extraction_links_planned) "
    "+ (events_replay_noop + extraction_links_replay_noop) "
    "+ assertions_unresolved + assertions_refused"
)

#: Counters the equation consumes, all in assertion slots.
WORK_FIELDS = (
    "normalizable",
    "events_planned",
    "extraction_links_planned",
    "events_replay_noop",
    "extraction_links_replay_noop",
    "assertions_unresolved",
    "assertions_refused",
)

#: Diagnostic counters: validated for shape, never summed into the equation.
DIAGNOSTIC_FIELDS = ("assertions_inconsistent",)

#: Counters describing what was actually written.
WRITE_FIELDS = ("events_inserted", "extraction_links_updated")


def mode_name(force: bool) -> str:
    """The accounting mode implied by the read mode."""
    return MODE_FORCE if force else MODE_NORMAL


def _counter(stats: Mapping[str, Any], field: str) -> int | None:
    """Read a non-negative integer counter, or ``None`` when unusable."""
    value = stats.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def classification_error(stats: Mapping[str, Any], *, mode: str | None = None) -> str | None:
    """Return a reason when work classification does not reconcile, else ``None``.

    Fails closed on: a missing or invalid counter, an unbalanced equation, replay
    work classified in normal mode, or a planned/committed mutation mismatch.
    """
    resolved_mode = mode or stats.get("accounting_mode") or MODE_NORMAL
    values: dict[str, int] = {}
    for field in WORK_FIELDS:
        counter = _counter(stats, field)
        if counter is None:
            return f"normalize accounting counter {field!r} is missing or invalid"
        values[field] = counter

    normalizable = values["normalizable"]
    inserts = values["events_planned"]
    link_writes = values["extraction_links_planned"]
    event_replays = values["events_replay_noop"]
    link_replays = values["extraction_links_replay_noop"]
    unresolved = values["assertions_unresolved"]
    refused = values["assertions_refused"]

    planned = inserts + link_writes
    replay = event_replays + link_replays
    work_side = 2 * normalizable
    planned_side = planned + replay + unresolved + refused
    if work_side != planned_side:
        if normalizable > 0 and planned_side == 0:
            return (
                f"normalize left {normalizable} work item(s) unclassified: no insert, "
                "update, replay, unresolved or refused assertion was recorded"
            )
        return (
            "normalize work classification does not reconcile: "
            f"2 * normalizable = {work_side}, but planned {planned} + replays {replay} "
            f"+ unresolved {unresolved} + refused {refused} = {planned_side}"
        )

    # Diagnostic counters are checked for shape and plausibility only, never summed.
    for field in DIAGNOSTIC_FIELDS:
        if field in stats and _counter(stats, field) is None:
            return f"normalize diagnostic counter {field!r} is invalid"
    inconsistent = _counter(stats, "assertions_inconsistent")
    if inconsistent:
        if refused == 0:
            return (
                f"normalize reported {inconsistent} inconsistent assertion(s) without "
                "any refused assertion slots: a refused page must account for them"
            )
        if inconsistent > refused:
            return (
                f"normalize reported {inconsistent} inconsistent assertion(s) exceeding "
                f"the {refused} refused assertion slot(s): double counting"
            )

    if resolved_mode == MODE_NORMAL and (event_replays or link_replays):
        return (
            "normal mode classified replay work, but it reads only unlinked rows: "
            f"event replays {event_replays}, link replays {link_replays}"
        )

    inserted = _counter(stats, "events_inserted")
    links_updated = _counter(stats, "extraction_links_updated")
    if inserted is None or links_updated is None:
        return "normalize write counters are missing or invalid"

    committed: int | None = None
    if "rows_committed" in stats:
        committed = _counter(stats, "rows_committed")
        if committed is None:
            return "normalize rows_committed is missing or invalid"

    # Checked before the generic bounds so the diagnostic names the real problem:
    # a plan that writes nothing must commit nothing.
    if inserts == 0 and link_writes == 0 and (inserted or links_updated or committed):
        return (
            "normalize committed mutations under an expect-no-writes plan: "
            f"inserted {inserted}, links updated {links_updated}, committed {committed}"
        )

    if inserted > inserts:
        return f"normalize inserted {inserted} event(s) but only planned {inserts}"
    if links_updated > link_writes:
        return f"normalize updated {links_updated} link(s) but only planned {link_writes}"

    if committed is not None and committed != inserted + links_updated:
        return (
            "normalize committed rows do not reconcile: "
            f"committed {committed} != inserted {inserted} + links updated {links_updated}"
        )
    return None


def legacy_classification_error(stats: Mapping[str, Any]) -> str | None:
    """The pre-Step-3 equation, retained only for envelopes that predate it."""
    normalizable = _counter(stats, "normalizable")
    planned = _counter(stats, "events_planned")
    if normalizable is None or planned is None:
        return "normalize accounting counters are missing or invalid"
    if planned != normalizable:
        return (
            "normalize event planning does not balance (legacy): "
            f"planned = {planned}, normalizable = {normalizable}"
        )
    return None


def child_accounting_error(stats: Mapping[str, Any]) -> str | None:
    """The child-contract entry point.

    Uses the mode-aware equation whenever the envelope declares its accounting
    mode, and falls back to the legacy equation for older envelopes so previously
    consumed results keep their meaning.
    """
    if "accounting_mode" in stats:
        return classification_error(stats)
    return legacy_classification_error(stats)
