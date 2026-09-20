"""Step 3 tests: mode-aware, replay-aware event-normalize accounting.

Isolated: every test uses a throwaway in-memory SQLite database or a plain dict.
No development or production database is contacted, and no pipeline is run.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from _kg_event_normalize_sqlite import build_engine, seed
from scripts.entities import event_extractor
from scripts.entities import event_normalize_runtime as runtime
from scripts.entities.event_normalize_accounting import (
    CLASSIFICATION_EQUATION,
    MODE_FORCE,
    MODE_NORMAL,
    child_accounting_error,
    classification_error,
    legacy_classification_error,
    mode_name,
)


def balanced(**overrides):
    """A stats dict that satisfies the mode-aware equation."""
    stats = {
        "normalizable": 0,
        "events_planned": 0,
        "extraction_links_planned": 0,
        "events_replay_noop": 0,
        "extraction_links_replay_noop": 0,
        "assertions_unresolved": 0,
        "assertions_refused": 0,
        "assertions_inconsistent": 0,
        "events_inserted": 0,
        "extraction_links_updated": 0,
    }
    stats.update(overrides)
    return stats


def count_rows(engine, table):
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar())


def envelope_for(stats, **extra):
    envelope = {"step": "normalize", "success": True, "stats": stats}
    envelope.update(extra)
    return event_extractor.json.dumps(envelope)


# -- 1. normal unlinked insert planning ---------------------------------------


def test_normal_unlinked_insert_planning_reconciles():
    engine = build_engine()
    seed(engine)

    stats = runtime.normalize(engine)

    assert stats["accounting_mode"] == MODE_NORMAL
    assert stats["normalizable"] == 1
    assert stats["events_planned"] == 1
    assert stats["extraction_links_planned"] == 1
    assert stats["events_replay_noop"] == 0
    assert stats["assertions_inconsistent"] == 0
    assert stats["classification_reconciles"] is True
    assert classification_error(stats) is None
    # the legacy equation still holds in this mode, so nothing was redefined
    assert stats["events_planned"] == stats["normalizable"]


def test_normal_mode_insert_envelope_passes_the_child_contract():
    engine = build_engine()
    seed(engine)
    stats = runtime.normalize(engine)

    result, error = event_extractor._parse_step_result(
        "normalize", envelope_for(stats)
    )

    assert error is None
    assert result is not None


# -- 2. mixed insert/replay accounting ----------------------------------------


def test_mixed_insert_and_replay_accounting():
    engine = build_engine()
    seed(engine, xid=1, did=1, mid=1)                     # unlinked -> insert
    seed(engine, xid=2, did=2, mid=2, linked=True)        # linked match -> replay

    stats = runtime.normalize(engine, force=True)

    assert stats["normalizable"] == 2
    assert stats["events_planned"] == 1
    assert stats["extraction_links_planned"] == 1
    assert stats["events_replay_noop"] == 1
    assert stats["extraction_links_replay_noop"] == 1
    assert stats["assertions_inconsistent"] == 0
    assert classification_error(stats) is None
    # 2 * 2 == (1 + 1) + (1 + 1) + 0 + 0 -- one unit throughout: assertion slots
    assert 2 * stats["normalizable"] == (
        stats["events_planned"] + stats["extraction_links_planned"]
        + stats["events_replay_noop"] + stats["extraction_links_replay_noop"]
        + stats["assertions_unresolved"] + stats["assertions_refused"]
    )


# -- 3. all-linked force replay -----------------------------------------------


def test_all_linked_force_replay_plans_and_commits_nothing():
    engine = build_engine()
    seed(engine, linked=True)
    events_before = count_rows(engine, "meeting_events")

    stats = runtime.normalize(engine, force=True)

    assert stats["accounting_mode"] == MODE_FORCE
    assert stats["normalizable"] > 0            # the legacy check demanded zero
    assert stats["events_planned"] == 0
    assert stats["extraction_links_planned"] == 0
    assert stats["events_inserted"] == 0
    assert stats["extraction_links_updated"] == 0
    assert stats["rows_committed"] == 0
    assert stats["events_replay_noop"] == stats["normalizable"]
    assert stats["extraction_links_replay_noop"] == stats["normalizable"]
    assert stats["classification_reconciles"] is True
    assert classification_error(stats) is None
    assert count_rows(engine, "meeting_events") == events_before


def test_force_replay_envelope_passes_the_child_contract():
    """The regression this step fixes: the legacy equation rejected this envelope."""
    engine = build_engine()
    seed(engine, linked=True)
    stats = runtime.normalize(engine, force=True)

    assert stats["events_planned"] != stats["normalizable"]  # legacy would fire

    result, error = event_extractor._parse_step_result(
        "normalize", envelope_for(stats)
    )
    assert error is None
    assert result is not None


# -- 4. unresolved / refused assertion ----------------------------------------


def test_refused_page_fails_closed_and_seals_a_receipt():
    engine = build_engine()
    seed(engine, linked=True, outcome="denied")   # stored row disagrees with evidence

    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine, force=True)

    stats = exc.value.stats
    assert stats["assertions_inconsistent"] == 1
    assert stats["failure_reason"] is not None or "refus" in str(exc.value).lower()
    # the refused page's slots are accounted for, so the equation reconciles...
    assert stats["assertions_refused"] == 2
    assert classification_error(stats) is None
    assert stats["classification_reconciles"] is True
    # ...while the run and its receipt remain failed, because the page was refused
    receipt = stats["validation_receipt"]
    assert receipt["state"] == "sealed"
    assert receipt["failure"] is not None


def test_unresolved_assertions_are_permitted_by_the_equation():
    # 2 * 3 == replays (2 + 2) + unresolved 2 + refused 0
    stats = balanced(normalizable=3, assertions_unresolved=2,
                     events_replay_noop=2, extraction_links_replay_noop=2)

    assert classification_error(stats, mode=MODE_FORCE) is None


# -- 5. dry mode --------------------------------------------------------------


def test_dry_mode_classifies_identically_and_commits_nothing():
    engine = build_engine()
    seed(engine, linked=True)
    events_before = count_rows(engine, "meeting_events")

    live_engine = build_engine()
    seed(live_engine, linked=True)
    live = runtime.normalize(live_engine, force=True)
    dry_engine = build_engine()
    seed(dry_engine, linked=True)
    dry = runtime.normalize(dry_engine, force=True, dry_run=True)

    for field in (
        "normalizable", "events_planned", "extraction_links_planned",
        "events_replay_noop", "extraction_links_replay_noop",
        "assertions_unresolved", "assertions_refused", "assertions_inconsistent",
    ):
        assert dry[field] == live[field], field
    assert dry["rows_committed"] == 0
    assert dry["events_inserted"] == 0
    assert dry["extraction_links_updated"] == 0
    assert dry["classification_reconciles"] is True
    assert dry["validation_receipt"]["dry_run"] is True
    assert count_rows(dry_engine, "meeting_events") == events_before


def test_dry_mode_detects_the_same_unresolved_work_as_live():
    engine = build_engine()
    seed(engine, linked=True, outcome="denied")

    with pytest.raises(runtime.NormalizationRunError) as exc:
        runtime.normalize(engine, force=True, dry_run=True)

    assert exc.value.stats["assertions_inconsistent"] == 1
    assert exc.value.stats["rows_committed"] == 0


# -- 6. malformed / under-counted / over-counted accounting -------------------


@pytest.mark.parametrize("missing", ["normalizable", "events_planned",
                                     "extraction_links_replay_noop"])
def test_missing_counter_fails_closed(missing):
    stats = balanced()
    del stats[missing]

    error = classification_error(stats)

    assert error is not None
    assert "missing or invalid" in error


@pytest.mark.parametrize("bad", [-1, 1.5, "1", True, None])
def test_invalid_counter_fails_closed(bad):
    stats = balanced()
    stats["normalizable"] = bad

    assert classification_error(stats) is not None


def test_under_counted_work_is_rejected():
    # two work items but only one assertion classified
    stats = balanced(normalizable=2, events_planned=1, extraction_links_planned=0)

    error = classification_error(stats)

    assert error is not None
    assert "does not reconcile" in error


def test_over_counted_work_is_rejected():
    # one work item but three assertions classified
    stats = balanced(normalizable=1, events_planned=1, extraction_links_planned=1,
                     events_replay_noop=1)

    error = classification_error(stats)

    assert error is not None
    assert "does not reconcile" in error


def test_unclassified_work_is_rejected():
    stats = balanced(normalizable=1)

    error = classification_error(stats)

    assert error is not None
    assert "unclassified" in error
    assert "no insert, update, replay, unresolved or refused assertion" in error


def test_replay_in_normal_mode_is_rejected():
    stats = balanced(normalizable=1, events_replay_noop=1,
                     extraction_links_replay_noop=1)

    error = classification_error(stats, mode=MODE_NORMAL)

    assert error is not None
    assert "normal mode classified replay work" in error
    # the same counters are legitimate in force mode
    assert classification_error(stats, mode=MODE_FORCE) is None


def test_sealed_receipt_reconciles_on_a_successful_run():
    engine = build_engine()
    seed(engine, linked=True)

    stats = runtime.normalize(engine, force=True)

    receipt = stats["validation_receipt"]
    assert receipt["rows"]["reconciles"] is True
    assert receipt["values"]["reconciles"] is True
    assert receipt["dry_run"] is False


# -- 7/8. contract error and expect-no-writes mutation ------------------------


def test_child_contract_rejects_a_non_reconciling_mode_aware_envelope():
    stats = balanced(normalizable=2, events_planned=1)  # under-counted
    stats["accounting_mode"] = MODE_FORCE
    stats["extractions_examined"] = 2
    stats["skipped_unmapped_type"] = 0

    result, error = event_extractor._parse_step_result(
        "normalize", envelope_for(stats)
    )

    assert result is None
    assert "does not reconcile" in error


def test_unexpected_planned_mutation_under_an_expect_no_writes_plan():
    stats = balanced(normalizable=1, events_replay_noop=1,
                     extraction_links_replay_noop=1, events_inserted=1,
                     extraction_links_updated=1, rows_committed=2)

    error = classification_error(stats, mode=MODE_FORCE)

    assert error is not None
    assert "expect-no-writes" in error


def test_unexpected_committed_rows_under_an_expect_no_writes_plan():
    stats = balanced(normalizable=1, events_replay_noop=1,
                     extraction_links_replay_noop=1, rows_committed=1)

    error = classification_error(stats, mode=MODE_FORCE)

    assert error is not None
    assert "expect-no-writes" in error


def test_committed_rows_must_match_written_counters():
    stats = balanced(normalizable=1, events_planned=1, extraction_links_planned=1,
                     events_inserted=1, extraction_links_updated=1, rows_committed=1)

    error = classification_error(stats)

    assert error is not None
    assert "committed rows do not reconcile" in error


def test_inserted_events_cannot_exceed_planned():
    stats = balanced(normalizable=1, events_planned=1, extraction_links_planned=1,
                     events_inserted=2, extraction_links_updated=2)

    error = classification_error(stats)

    assert error is not None
    assert "only planned" in error


# -- 9. legacy envelope compatibility -----------------------------------------


def test_legacy_equation_still_applies_without_an_accounting_mode():
    legacy_ok = {"normalizable": 3, "events_planned": 3}
    assert child_accounting_error(legacy_ok) is None

    legacy_bad = {"normalizable": 3, "events_planned": 2}
    error = child_accounting_error(legacy_bad)
    assert error is not None
    assert "planning does not balance" in error


def test_legacy_envelope_still_passes_the_child_contract():
    """An envelope without accounting_mode keeps its historic meaning."""
    fields = event_extractor.REQUIRED_STEP_FIELDS["normalize"]
    stats = {field: 0 for field in fields}
    stats.update({"extractions_examined": 1, "normalizable": 1, "events_planned": 1,
                  "events_inserted": 1, "extraction_links_updated": 1})

    result, error = event_extractor._parse_step_result(
        "normalize", envelope_for(stats)
    )

    assert error is None
    assert result is not None


def test_required_envelope_fields_are_unchanged():
    """Step 3 adds fields; it must not remove or rename the contract's fields."""
    assert event_extractor.REQUIRED_STEP_FIELDS["normalize"] == {
        "extractions_examined", "normalizable", "events_planned",
        "events_inserted", "extraction_links_updated", "skipped_unmapped_type",
    }


def test_equation_is_named_in_the_envelope():
    engine = build_engine()
    seed(engine)

    stats = runtime.normalize(engine)

    assert stats["classification_equation"] == CLASSIFICATION_EQUATION
    assert "normalizable" in stats["classification_equation"]


def test_mode_name_maps_the_read_mode():
    assert mode_name(True) == MODE_FORCE
    assert mode_name(False) == MODE_NORMAL


def test_legacy_helper_is_directly_exercised():
    assert legacy_classification_error({"normalizable": 1, "events_planned": 1}) is None
    assert legacy_classification_error({"normalizable": 1, "events_planned": 0}) is not None


# -- refusal accounting: one unit, no double counting -------------------------


def test_refused_slots_balance_the_equation():
    """A refused page's every slot is refused: the six well-formed assertions too."""
    # the known failing page: 256 items, 253 inconsistent assertions, 3 well-formed
    stats = balanced(normalizable=256, assertions_refused=512,
                     assertions_inconsistent=253)
    assert classification_error(stats, mode=MODE_FORCE) is None


def test_a_refused_page_of_well_formed_items_alone_is_not_a_replay():
    """Refused slots must not be smuggled in as processed replays."""
    refused_only = balanced(normalizable=256, assertions_refused=512)
    as_replays = balanced(normalizable=256, events_replay_noop=256,
                          extraction_links_replay_noop=256)
    assert classification_error(refused_only, mode=MODE_FORCE) is None
    assert classification_error(as_replays, mode=MODE_FORCE) is None
    assert refused_only["assertions_refused"] > 0
    assert refused_only["events_replay_noop"] == 0


def test_inconsistent_assertions_without_refused_slots_is_rejected():
    """Guards the double-counting defect: a diagnostic count needs refused slots."""
    stats = balanced(normalizable=1, events_replay_noop=1,
                     extraction_links_replay_noop=1, assertions_inconsistent=1)
    error = classification_error(stats, mode=MODE_FORCE)
    assert error is not None and "without any refused assertion slots" in error


def test_inconsistent_assertions_exceeding_refused_slots_is_rejected():
    stats = balanced(normalizable=1, assertions_refused=2, assertions_inconsistent=3)
    error = classification_error(stats, mode=MODE_FORCE)
    assert error is not None and "double counting" in error


def test_the_legacy_formula_is_gone_from_the_enforced_equation():
    assert "2 * assertions_inconsistent" not in CLASSIFICATION_EQUATION
    assert "assertions_refused" in CLASSIFICATION_EQUATION
    assert "assertions_unresolved" in CLASSIFICATION_EQUATION


def test_all_inconsistent_page_reconciles():
    # 4 items, every one mismatched -> 4 inconsistent assertions, 8 refused slots
    stats = balanced(normalizable=4, assertions_refused=8, assertions_inconsistent=4)
    assert classification_error(stats, mode=MODE_FORCE) is None


def test_mixed_page_reconciles_and_is_flagged_failed_by_the_run():
    # 256 items: 253 mismatched assertions + 3 well-formed items (6 assertions)
    stats = balanced(normalizable=256, assertions_refused=2 * 256,
                     assertions_inconsistent=253)
    assert classification_error(stats, mode=MODE_FORCE) is None


def test_healthy_remainder_with_one_inconsistent_item_reconciles():
    # 10 items: 5 replays (10 slots) + one refused page of 5 items (10 refused slots)
    stats = balanced(normalizable=10, events_replay_noop=5,
                     extraction_links_replay_noop=5, assertions_refused=10,
                     assertions_inconsistent=1)
    assert classification_error(stats, mode=MODE_FORCE) is None


def test_empty_run_reconciles():
    assert classification_error(balanced(), mode=MODE_FORCE) is None


def test_per_kind_totals_are_preserved_once_each():
    """Planned and replay stays split by kind; refused is a single slot total."""
    stats = balanced(normalizable=2, events_planned=1, extraction_links_planned=0,
                     events_replay_noop=1, extraction_links_replay_noop=1,
                     assertions_refused=1)
    assert classification_error(stats, mode=MODE_FORCE) is None
    assert stats["assertions_refused"] == 1
