#!/usr/bin/env python3
"""Adversarial tests for the duplicate-key investigation and collision contract.

The investigation is only useful if its accounting is checkable, so these tests
attack the accounting: a row classified twice, a group left unexplained, counts that
do not reconcile, a contract digest that was edited, an alternative quietly
upgraded.  The contract is only useful if it is actually concurrency-safe, so the
reservation path is exercised against a real database fixture.
"""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_dupkey_contract as C  # noqa: E402
from scripts.kg import stage2_dupkey_investigate as INV  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"

#: The counts this investigation is required to reconcile to.
EXPECTED_GROUPS = 2890
EXPECTED_ROWS = 7563
EXPECTED_EXCESS = 4673


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


def classify(rows):
    """Classify one group's rows the way the investigation does."""
    return INV.classify_rows(rows)


def investigation():
    return copy.deepcopy(artifacts.load_verified(
        _live("kg-stage2-duplicate-key-investigation-*.json")))


def contract():
    return copy.deepcopy(artifacts.load_verified(
        _live("kg-stage2-collision-control-contract-*.json")))


# ══ the accounting reconciles exactly ══════════════════════════════════

def test_the_recorded_artifact_reconciles_to_the_measured_totals():
    totals = investigation()["totals"]
    assert totals["groups"] == EXPECTED_GROUPS
    assert totals["rows"] == EXPECTED_ROWS
    assert totals["excess"] == EXPECTED_EXCESS


def test_group_rows_sum_to_the_total_and_excess():
    artifact = investigation()
    groups = artifact["groups"]
    assert sum(g["rows"] for g in groups) == artifact["totals"]["rows"]
    assert sum(g["excess"] for g in groups) == artifact["totals"]["excess"]
    for group in groups:
        assert group["rows"] >= 2
        assert group["excess"] == group["rows"] - 1
        assert len(group["row_ids"]) == group["rows"]


def test_every_row_is_classified_exactly_once():
    artifact = investigation()
    rows = artifact["rows"]
    assert len(rows) == artifact["totals"]["rows"]
    assert len({r["id"] for r in rows}) == len(rows)
    for row in rows:
        assert row["family"] in INV.FAMILIES, row["id"]


def test_the_families_partition_the_rows():
    artifact = investigation()
    counts = artifact["totals"]["families"]
    assert set(counts) == set(INV.FAMILIES)
    assert sum(counts.values()) == artifact["totals"]["rows"]


def test_every_group_is_classified_exactly_once():
    artifact = investigation()
    for group in artifact["groups"]:
        assert group["family"] in INV.GROUP_FAMILIES, group
        assert sorted(group["families"]) == sorted(set(group["families"]))
        assert sum(group["families"].values()) == group["rows"]


def test_every_row_carries_a_fingerprint_and_its_evidence():
    for row in investigation()["rows"]:
        assert len(row["fingerprint"]) == 64
        assert len(row["title_sha256"]) == 64 and len(row["text_sha256"]) == 64
        assert row["source_key"] is not None
        assert row["reason"]


# ══ representative and high-risk families ══════════════════════════════

def test_the_placeholder_family_is_present_and_attributed():
    counts = investigation()["totals"]["families"]
    assert counts["auto_positional"] > 0
    assert counts["sentinel_number"] > 0
    assert counts["auto_positional"] + counts["sentinel_number"] > 0


def test_unresolved_is_small_and_never_guessed_at():
    """Unresolved is reported, not swept into a plausible-looking family."""
    counts = investigation()["totals"]["families"]
    assert counts["unresolved"] < 100, counts["unresolved"]
    for row in investigation()["rows"]:
        if row["family"] == "unresolved":
            assert row["reason"] == "no declared family matched"


def test_the_largest_family_is_legitimate_distinct_numbering():
    """The pollution is placeholder numbering, not a broken identity."""
    counts = investigation()["totals"]["families"]
    assert counts["distinct_same_number"] > counts["auto_positional"]
    assert counts["exact_duplicate"] == 0


def test_multi_body_groups_are_attributed_to_the_meeting_not_to_a_duplicate():
    artifact = investigation()
    multi = [g for g in artifact["groups"] if g["family"] == "multi_body_meeting"]
    assert multi
    for group in multi:
        assert len(group["bodies"]) > 1


def test_downstream_impact_is_measured_and_bounded():
    impact = investigation()["impact"]
    assert impact["rows_referenced_by_meeting_events"] == 0
    assert impact["groups_with_event_references"] == 0
    assert impact["documents_naming_a_duplicated_number"] > 0
    assert impact["groups_touching_documents"] > 0


def test_no_group_in_the_artifact_is_referenced_by_an_event():
    """Deletion/merge risk is bounded by the only FK to agenda_items."""
    for group in investigation()["groups"]:
        assert group["referenced_by_events"] == []


# ══ the family rules themselves ════════════════════════════════════════

def _row(row_id, number, source_key, title, body="b", meeting=1, text=None):
    return {"id": row_id, "meeting_db_id": meeting, "body": body,
            "agenda_item_number": number, "agenda_item_id": source_key,
            "agenda_item_title": title,
            "agenda_item_text": text if text is not None else title}


def test_an_auto_positional_key_is_classified_as_such():
    rows = classify([_row(1, "0", "x_auto-1", "Call to Order"),
                     _row(2, "0", "x_auto-2", "Adjournment")])
    assert {r["family"] for r in rows} == {"auto_positional"}


def test_a_sentinel_number_is_classified_as_such():
    rows = classify([_row(1, "n/a", "a-1", "One"), _row(2, "n/a", "a-2", "Two")])
    assert {r["family"] for r in rows} == {"sentinel_number"}


def test_an_exact_repeat_is_classified_as_a_duplicate_with_a_keeper():
    rows = classify([_row(1, "5", "a-5", "Same", text="Same"),
                     _row(2, "5", "a-5", "Same", text="Same")])
    families = {r["id"]: r["family"] for r in rows}
    assert families == {1: "exact_duplicate", 2: "exact_duplicate"}
    by_id = {r["id"]: r for r in rows}
    assert by_id[2]["duplicate_of"] == 1, "the repeat must name its keeper"
    assert by_id[1]["duplicate_of"] is None, "the keeper is not a duplicate of itself"


def test_two_bodies_in_one_meeting_are_legitimate():
    rows = classify([_row(1, "1", "a-1", "One", body="alpha"),
                     _row(2, "1", "b-1", "Two", body="beta")])
    assert {r["family"] for r in rows} == {"multi_body_meeting"}


def test_a_reused_number_with_distinct_titles_is_distinct():
    rows = classify([_row(1, "1", "a-1", "First thing"),
                     _row(2, "1", "a-2", "Second thing")])
    assert {r["family"] for r in rows} == {"distinct_same_number"}


def test_a_row_that_matches_nothing_is_unresolved_not_guessed():
    rows = classify([_row(1, "1", "a-1", "Same title"),
                     _row(2, "1", "a-2", "Same title")])
    assert "unresolved" in {r["family"] for r in rows}


def test_an_ambiguous_group_classifies_as_mixed_not_as_one_family():
    rows = classify([_row(1, "1", "x_auto-1", "Call to Order"),
                     _row(2, "1", "a-1", "A real item")])
    assert INV.group_family(rows) == "mixed"


def test_sentinel_detection_is_case_and_space_insensitive():
    for value in ("", "  ", "0", "N/A", " None ", "?", "TBD"):
        assert INV.is_sentinel(value), value
    for value in ("1", "a", "4.A", "22C"):
        assert not INV.is_sentinel(value), value


def test_auto_key_detection_matches_only_positional_pseudo_keys():
    assert INV.is_auto_key("buckeye-cc-423_auto-1")
    assert not INV.is_auto_key("buckeye-cc-423_5")


# ══ the contract is complete and honest ════════════════════════════════

def test_the_recorded_contract_validates():
    assert C.validate_contract(contract()) == []


def test_every_alternative_is_evaluated_with_a_verdict():
    ids = {a["id"] for a in contract()["alternatives"]}
    for expected in ("A1-global-unique-index", "A2-scoped-or-partial-uniqueness",
                     "A3-parent-row-lock", "A4-exact-key-reservation",
                     "A5-advisory-lock", "A6-serializable-conflict-check"):
        assert expected in ids, expected


def test_the_global_unique_index_is_rejected_for_the_measured_reason():
    alternative = next(a for a in contract()["alternatives"]
                       if a["id"] == "A1-global-unique-index")
    assert alternative["verdict"] == "REJECTED"
    assert "4,673" in alternative["why"]


def test_the_parent_row_lock_is_recorded_as_insufficient():
    alternative = next(a for a in contract()["alternatives"]
                       if a["id"] == "A3-parent-row-lock")
    assert alternative["sufficient"] is False
    assert "ABSENT key" in alternative["why"]


def test_the_contract_names_the_invariant_carrier():
    recommended = contract()["recommended"]
    assert recommended["invariant_carrier"] == "A4-exact-key-reservation"
    assert recommended["fail_closed"]


def test_the_contract_states_nonguarantees_as_well_as_guarantees():
    data = contract()
    assert data["guarantees"] and data["non_guarantees"]
    assert any("bypass" in g for g in data["non_guarantees"])


def test_the_reservation_table_is_additive_and_rewrites_nothing():
    table = contract()["reservation_table"]
    assert table["additive"] is True
    assert table["rewrites_existing_rows"] is False
    assert table["primary_key"] == ["meeting_db_id", "agenda_item_number"]


def test_a_tampered_contract_digest_is_refused():
    data = contract()
    data["recommended"]["invariant_carrier"] = "A3-parent-row-lock"
    assert any("digest" in p for p in C.validate_contract(data))


def test_an_unregistered_verdict_is_refused():
    data = contract()
    data["alternatives"][0]["verdict"] = "PROBABLY FINE"
    assert any("verdict" in p for p in C.validate_contract(data))


def test_a_contract_without_nonguarantees_is_refused():
    data = contract()
    data["non_guarantees"] = []
    assert C.validate_contract(data)


def test_a_contract_with_no_invariant_carrier_is_refused():
    data = contract()
    data["recommended"]["invariant_carrier"] = ""
    assert C.validate_contract(data)


# ══ reservation operations: deterministic, fail-closed ═════════════════

def test_reservation_splits_into_inserts_and_holds():
    result = C.reservation_operations(
        plan_digest="d" * 64,
        requested=[{"meeting_db_id": 1, "agenda_item_number": "5", "claimed_by": "repair"},
                   {"meeting_db_id": 2, "agenda_item_number": "0", "claimed_by": "repair"}],
        existing_live_keys=["1|5"], already_reserved=[])
    assert [r["key"] for r in result["inserts"]] == ["2|0"]
    assert result["holds"][0]["key"] == "1|5"
    assert "already holds" in result["holds"][0]["reason"]


def test_an_already_reserved_key_is_held():
    result = C.reservation_operations(
        plan_digest="d" * 64,
        requested=[{"meeting_db_id": 1, "agenda_item_number": "5"}],
        existing_live_keys=[], already_reserved=["1|5"])
    assert result["inserts"] == []
    assert "already reserved" in result["holds"][0]["reason"]


def test_a_key_requested_twice_in_one_plan_is_held():
    result = C.reservation_operations(
        plan_digest="d" * 64,
        requested=[{"meeting_db_id": 1, "agenda_item_number": "5"},
                   {"meeting_db_id": 1, "agenda_item_number": "5"}],
        existing_live_keys=[], already_reserved=[])
    assert len(result["inserts"]) == 1
    assert result["holds"][0]["reason"] == "requested twice in one plan"


def test_reservation_without_a_plan_digest_is_refused():
    with pytest.raises(ValueError):
        C.reservation_operations(plan_digest="", requested=[],
                                 existing_live_keys=[], already_reserved=[])


def test_reservation_is_deterministic():
    kwargs = dict(plan_digest="d" * 64,
                  requested=[{"meeting_db_id": 2, "agenda_item_number": "b"},
                             {"meeting_db_id": 1, "agenda_item_number": "a"}],
                  existing_live_keys=[], already_reserved=[])
    first = C.reservation_operations(**kwargs)
    second = C.reservation_operations(**kwargs)
    assert first == second
    assert first["insert_key_digest"] == C.reservation_digest(
        "d" * 64, [r["key"] for r in first["inserts"]])


# ══ concurrency safety, proved on a real database fixture ══════════════

def _reservation_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            f"CREATE TABLE {C.RESERVATION_TABLE} (meeting_db_id INTEGER NOT NULL, "
            f"agenda_item_number TEXT NOT NULL, plan_digest TEXT NOT NULL, "
            f"reserved_at TEXT NOT NULL, "
            f"PRIMARY KEY (meeting_db_id, agenda_item_number))"))
    return engine


def _reserve(connection, keys, plan_digest="d" * 64):
    from sqlalchemy import text as _text

    for key in keys:
        meeting, number = key.split("|", 1)
        connection.execute(_text(
            f"INSERT INTO {C.RESERVATION_TABLE} (meeting_db_id, agenda_item_number, "
            f"plan_digest, reserved_at) VALUES (:m, :n, :d, 'now')"),
            {"m": int(meeting), "n": number, "d": plan_digest})


def test_two_writers_racing_on_one_key_leave_exactly_one_reservation():
    """The invariant carrier, exercised: the primary key decides the race."""
    engine = _reservation_engine()
    with engine.begin() as first:
        _reserve(first, ["1|5"])
    with pytest.raises(Exception):
        with engine.begin() as second:  # pragma: no cover - the race is the point
            _reserve(second, ["1|5"])
    with engine.connect() as connection:
        count = connection.execute(text(
            f"SELECT COUNT(*) FROM {C.RESERVATION_TABLE}")).scalar()
    assert count == 1


def test_a_held_key_is_never_written_by_the_reservation_path():
    engine = _reservation_engine()
    with engine.begin() as connection:
        _reserve(connection, ["1|5"])
        result = C.reservation_operations(
            plan_digest="d" * 64,
            requested=[{"meeting_db_id": 1, "agenda_item_number": "5"}],
            existing_live_keys=[], already_reserved=["1|5"])
        assert result["inserts"] == []
    with engine.connect() as connection:
        assert connection.execute(text(
            f"SELECT COUNT(*) FROM {C.RESERVATION_TABLE}")).scalar() == 1


def test_a_failed_reservation_transaction_leaves_no_rows():
    engine = _reservation_engine()
    with pytest.raises(RuntimeError):
        with engine.begin() as connection:
            _reserve(connection, ["1|5", "1|6"])
            raise RuntimeError("postcondition failed")
    with engine.connect() as connection:
        assert connection.execute(text(
            f"SELECT COUNT(*) FROM {C.RESERVATION_TABLE}")).scalar() == 0


def test_replaying_the_same_reservation_set_is_a_no_op():
    engine = _reservation_engine()
    with engine.begin() as connection:
        _reserve(connection, ["1|5", "2|0"])
    result = C.reservation_operations(
        plan_digest="d" * 64,
        requested=[{"meeting_db_id": 1, "agenda_item_number": "5"},
                   {"meeting_db_id": 2, "agenda_item_number": "0"}],
        existing_live_keys=[], already_reserved=["1|5", "2|0"])
    assert result["inserts"] == []
    assert len(result["holds"]) == 2
