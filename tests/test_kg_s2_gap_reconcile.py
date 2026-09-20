#!/usr/bin/env python3
"""Reconciliation invariant, plus the Tempe 9775 and Phoenix 107 regressions.

The invariant is narrow and absolute: every supporting document that records a
nonempty item reference resolves to a parsed canonical item or is surfaced and
held.  Nothing is dropped and nothing is invented.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO, _REPO / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import stage2_s2_gap_reconcile as G  # noqa: E402

_FX = _REPO / "tests" / "fixtures" / "buckeye"


def _fixture(name: str) -> dict:
    return json.loads((_FX / name).read_text())


# ── the invariant itself ───────────────────────────────────────────────


def test_a_resolved_reference_is_linked():
    result = G.reconcile_references(
        [{"document_id": 1, "meeting_db_id": 10, "body": "b", "item_number": "2.A"}],
        canonical_numbers={"2.A", "2.B"})
    G.assert_population_closed(result)
    assert result["counts"] == {"references": 1, "resolved": 1, "held": 0, "non_references": 0}


def test_an_absent_reference_is_held_not_invented():
    result = G.reconcile_references(
        [{"document_id": 2, "meeting_db_id": 10, "body": "b", "item_number": "2.C"}],
        canonical_numbers={"2.A", "2.B"})
    G.assert_population_closed(result)
    assert result["counts"]["held"] == 1
    assert result["held"][0]["item_number"] == "2.C"
    assert result["held"][0]["reason"] == "gap_missing_target"
    assert result["resolved"] == []


def test_a_held_reference_never_creates_a_canonical_item():
    """Nothing in the result carries a materialized item id."""
    result = G.reconcile_references(
        [{"document_id": 3, "meeting_db_id": 10, "body": "b", "item_number": "4.AA"}],
        canonical_numbers={"4.A"})
    for record in result["held"]:
        assert "agenda_item_db_id" not in record
        assert "title" not in record or record.get("title") is None


def test_an_empty_reference_is_not_a_reference():
    result = G.reconcile_references(
        [{"document_id": 4, "meeting_db_id": 10, "body": "b", "item_number": ""},
         {"document_id": 5, "meeting_db_id": 10, "body": "b", "item_number": "0"}],
        canonical_numbers={"2.A"})
    G.assert_population_closed(result)
    # "0" is nonempty text, so it engages the invariant and is held, not dropped.
    assert result["counts"]["non_references"] == 1
    assert result["counts"]["references"] == 1
    assert result["held"][0]["item_number"] == "0"


def test_the_population_must_close():
    with pytest.raises(G.ReconcileRefused):
        G.assert_population_closed(
            {"counts": {"references": 5, "resolved": 2, "held": 1}, "held": []})


def test_a_held_reference_without_a_reason_is_refused():
    with pytest.raises(G.ReconcileRefused):
        G.assert_population_closed(
            {"counts": {"references": 1, "resolved": 0, "held": 1},
             "held": [{"document_id": 9, "item_number": "2.C"}]})


def test_a_held_reference_with_an_empty_number_is_refused():
    with pytest.raises(G.ReconcileRefused):
        G.assert_population_closed(
            {"counts": {"references": 1, "resolved": 0, "held": 1},
             "held": [{"document_id": 9, "item_number": "", "reason": "gap_missing_target"}]})


def test_summary_groups_held_by_body_meeting_and_reference():
    references = [
        {"document_id": 1, "meeting_db_id": 11, "body": "buckeye-cfd", "item_number": "2.C"},
        {"document_id": 2, "meeting_db_id": 11, "body": "buckeye-cfd", "item_number": "2.C"},
        {"document_id": 3, "meeting_db_id": 12, "body": "buckeye-pz", "item_number": "3.D"},
    ]
    summary = G.summarize_held(G.reconcile_references(references, canonical_numbers={"2.A"}))
    assert summary["total"] == 3
    assert summary["by_body"] == {"buckeye-cfd": 2, "buckeye-pz": 1}
    assert summary["by_reference"] == {"2.C": 2, "3.D": 1}
    assert summary["by_meeting"] == {"11": 2, "12": 1}


def test_dotted_selects_only_the_split_label_population():
    references = [{"item_number": "2.C"}, {"item_number": "3.D"}, {"item_number": "107"},
                  {"item_number": "4.AA"}]
    assert [r["item_number"] for r in G.dotted(references)] == ["2.C", "3.D", "4.AA"]


# ── Tempe 9775: Agenda/Minutes attachments are meeting-level ───────────


def test_an_agenda_or_minutes_attachment_is_meeting_level():
    assert G.is_meeting_level_attachment("Agenda: 6.2.26 Agenda")
    assert G.is_meeting_level_attachment("Minutes: 6.2.26 Agenda")
    assert G.is_meeting_level_attachment("  MINUTES: something")
    assert not G.is_meeting_level_attachment("4.A Council to take action")
    assert not G.is_meeting_level_attachment("Agenda items were discussed")


def test_tempe_meeting_9775_rows_classify_as_meeting_level():
    """Regression: the two rows are attachments, not references to item 1."""
    fixture = _fixture("tempe_9775_meeting_level.json")
    result = G.reconcile_references(fixture["references"], fixture["canonical_numbers"])
    G.assert_population_closed(result)

    assert result["counts"]["held"] == fixture["expected_held"] == 0
    assert result["counts"]["non_references"] == 2
    classes = {record["class"] for record in result["non_references"]}
    assert classes == {fixture["expected_class"]}
    assert {r["document_id"] for r in result["non_references"]} == {46970, 46971}
    # A stray number '1' must not be reported as a missing item.
    assert result["held"] == []


def test_tempe_9775_would_have_been_a_false_gap_otherwise():
    """The old reading: a nonempty '1' with no canonical item 1."""
    fixture = _fixture("tempe_9775_meeting_level.json")
    raw = [{k: v for k, v in r.items() if k != "title"} for r in fixture["references"]]
    result = G.reconcile_references(raw, fixture["canonical_numbers"])
    assert result["counts"]["held"] == 2
    # ...which is exactly why the title rule matters.
    titled = G.reconcile_references(fixture["references"], fixture["canonical_numbers"])
    assert titled["counts"]["held"] == 0


# ── Phoenix 107: the add-on is held until materialized ─────────────────


def test_phoenix_add_on_item_107_is_held_when_no_canonical_item_exists():
    fixture = _fixture("phoenix_107_addon.json")
    result = G.reconcile_references(fixture["references"], fixture["canonical_numbers"])
    G.assert_population_closed(result)

    assert result["counts"]["held"] == fixture["expected_held"] == 1
    held = result["held"][0]
    assert held["item_number"] == "107"
    assert held["class"] == fixture["expected_class"]
    assert held["document_id"] == 45499
    assert result["resolved"] == []


def test_phoenix_item_107_resolves_once_a_canonical_item_is_materialized():
    fixture = _fixture("phoenix_107_addon.json")
    result = G.reconcile_references(fixture["references"], canonical_numbers={"107"})
    G.assert_population_closed(result)
    assert result["counts"]["held"] == 0
    assert result["counts"]["resolved"] == 1


def test_phoenix_item_107_is_not_confused_with_another_number():
    """A neighbouring item must not satisfy the 107 reference."""
    fixture = _fixture("phoenix_107_addon.json")
    result = G.reconcile_references(fixture["references"],
                                    canonical_numbers={"106", "108", "10"})
    assert result["counts"]["held"] == 1
    assert result["held"][0]["item_number"] == "107"


def test_phoenix_add_on_reference_is_never_inferred_from_the_source_key():
    """The source key ends in '_107'; that is not a licence to invent item 107."""
    fixture = _fixture("phoenix_107_addon.json")
    reference = fixture["references"][0]
    assert reference["source_key"].endswith("_107")
    result = G.reconcile_references(fixture["references"], canonical_numbers=[])
    assert result["resolved"] == []
    assert result["held"][0]["reason"] == "gap_missing_target"
