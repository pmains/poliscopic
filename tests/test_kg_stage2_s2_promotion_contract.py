#!/usr/bin/env python3
"""Promotion/apply contract: fingerprint binding and the 2026 -> 2.C refusal."""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_s2_promotion_contract as P  # noqa: E402

_TARGET = {"dialect": "postgresql", "host": "192.0.2.10", "port": 5432,
           "database": "poliscopic_dev"}

#: meeting 10428 as it exists NOW: 4.I and 2026, but no 2.C.
_BASE_ITEMS = [
    {"meeting_db_id": 10428, "agenda_item_db_id": 278759, "agenda_item_number": "4.I",
     "id": 278759},
    {"meeting_db_id": 10428, "agenda_item_db_id": 278749, "agenda_item_number": "2026",
     "id": 278749},
    {"meeting_db_id": 10428, "agenda_item_db_id": 278750, "agenda_item_number": "2.A",
     "id": 278750},
]
_WITH_2C = _BASE_ITEMS + [
    {"meeting_db_id": 10428, "agenda_item_db_id": 278760, "agenda_item_number": "2.C",
     "id": 278760, "agenda_item_fingerprint": "f" * 64},
]


def _decision(document_id, candidate, number, stated, mismatch):
    return {
        "decision_id": f"kg-s2-dec-20260913T030300Z-doc{document_id}",
        "decided_at": "2026-09-12T20:03:00-07:00",
        "adjudicator": "Peter Mains",
        "decision": "approve",
        "document_id": document_id,
        "document_fingerprint": "d" * 64,
        "unlinked_state_fingerprint": "u" * 64,
        "document_role": "Item Report",
        "human_stated_item": stated,
        "item_number_mismatch": mismatch,
        "proposal": {"path": f"proposal-{document_id}.json", "digest": "r" * 64},
        "candidate": {"agenda_item_db_id": candidate, "meeting_db_id": 10428,
                      "agenda_item_number": number,
                      "agenda_item_fingerprint": "c" * 64},
    }


def _five():
    return [
        _decision(107938, 278759, "4.I", "4.I", False),
        _decision(107939, 278759, "4.I", "4.I", False),
        _decision(107915, 278749, "2026", "2.C", True),
        _decision(107916, 278749, "2026", "2.C", True),
        _decision(107917, 278749, "2026", "2.C", True),
    ]


def _contract(items):
    return P.build_contract(_five(), canonical_items=items,
                            created_at="2026-09-12T20:11:00+00:00", target=_TARGET)


# ── fingerprint binding ────────────────────────────────────────────────


def test_the_contract_binds_all_five_decisions():
    contract = _contract(_BASE_ITEMS)
    assert contract["counts"]["decisions"] == 5
    assert contract["counts"]["approved"] == 5
    assert {e["document_id"] for e in contract["decisions"]} == \
        {107938, 107939, 107915, 107916, 107917}


def test_every_decision_binds_document_and_target_fingerprints():
    for entry in _contract(_BASE_ITEMS)["decisions"]:
        assert entry["document_fingerprint"]
        assert entry["unlinked_state_fingerprint"]
        assert entry["proposal_digest"]
        assert entry["candidate"]["agenda_item_fingerprint"]
        assert entry["decided_at"] and entry["adjudicator"]


def test_a_missing_proposal_digest_is_refused():
    contract = _contract(_BASE_ITEMS)
    contract["decisions"][0]["proposal_digest"] = None
    assert any("proposal digest" in p for p in P.validate_contract(contract))


def test_a_missing_candidate_fingerprint_is_refused():
    contract = _contract(_BASE_ITEMS)
    contract["decisions"][0]["candidate"]["agenda_item_fingerprint"] = None
    assert any("candidate fingerprint" in p for p in P.validate_contract(contract))


def test_a_missing_document_fingerprint_is_refused():
    contract = _contract(_BASE_ITEMS)
    contract["decisions"][0]["document_fingerprint"] = ""
    assert any("document_fingerprint" in p for p in P.validate_contract(contract))


def test_a_contract_that_claims_promotion_is_refused():
    contract = _contract(_BASE_ITEMS)
    contract["promoted"] = True
    assert any("promoted=false" in p for p in P.validate_contract(contract))


# ── the 278749 / 2026 mismatch ─────────────────────────────────────────


def test_the_mismatch_blocks_promotion_until_2c_exists():
    contract = _contract(_BASE_ITEMS)
    assert contract["counts"]["blocked"] == 3
    assert contract["counts"]["rebind_required"] == 3
    assert contract["counts"]["promotable"] == 2
    with pytest.raises(P.PromotionRefused) as exc:
        P.assert_promotable(contract)
    assert "not promotable" in str(exc.value)


def test_the_blocked_reason_names_both_numbers():
    blocked = _contract(_BASE_ITEMS)["blocked"]
    assert [b["document_id"] for b in blocked] == [107915, 107916, 107917]
    reason = blocked[0]["reason"]
    assert "2026" in reason and "2.C" in reason and "does not exist" in reason


def test_promotion_is_clean_once_2c_is_materialised():
    contract = _contract(_WITH_2C)
    assert contract["counts"]["blocked"] == 0
    assert contract["counts"]["promotable"] == 5
    assert contract["counts"]["rebind_available"] == 3
    P.assert_promotable(contract)


def test_the_mismatch_is_still_recorded_after_repair():
    """2.C existing makes the rebind available; it does not erase the history."""
    entry = next(e for e in _contract(_WITH_2C)["decisions"] if e["document_id"] == 107915)
    assert entry["item_number_mismatch"] is True
    assert entry["candidate"]["agenda_item_number"] == "2026"
    assert entry["human_stated_item"] == "2.C"


def test_a_document_that_becomes_source_supported_is_out_of_scope_here():
    """The contract only covers approved link decisions; nothing promotes by itself."""
    contract = _contract(_BASE_ITEMS)
    assert contract["promoted"] is False and contract["applied"] is False
    assert contract["counts"]["promoted"] == 0 and contract["counts"]["applied"] == 0


# ── rebinding is conditional on parser repair ──────────────────────────


def _rebind(canonical_item):
    decision = [d for d in _five() if d["document_id"] == 107915][0]
    return P.rebind_candidate(decision, canonical_item=canonical_item)


def test_rebinding_refuses_while_the_canonical_item_is_absent():
    with pytest.raises(P.PromotionRefused) as exc:
        _rebind(None)
    assert "no canonical item '2.C' exists yet" in str(exc.value)


def test_rebinding_refuses_a_differently_numbered_item():
    with pytest.raises(P.PromotionRefused) as exc:
        _rebind({"meeting_db_id": 10428, "agenda_item_number": "2.D",
                 "agenda_item_db_id": 1})
    assert "numbered '2.D', not '2.C'" in str(exc.value)


def test_rebinding_refuses_an_item_from_another_meeting():
    with pytest.raises(P.PromotionRefused) as exc:
        _rebind({"meeting_db_id": 9999, "agenda_item_number": "2.C",
                 "agenda_item_db_id": 1})
    assert "different meeting" in str(exc.value)


def test_rebinding_maps_2026_onto_2c_and_says_why():
    out = _rebind({"meeting_db_id": 10428, "agenda_item_number": "2.C",
                   "agenda_item_db_id": 278760, "agenda_item_fingerprint": "f" * 64})
    assert out["from"]["agenda_item_number"] == "2026"
    assert out["to"]["agenda_item_number"] == "2.C"
    assert out["human_assertion_alone_is_insufficient"] is True
    assert "parser" in out["requires"]


def test_a_decision_without_a_mismatch_needs_no_rebind():
    decision = [d for d in _five() if d["document_id"] == 107938][0]
    with pytest.raises(P.PromotionRefused):
        P.rebind_candidate(decision, canonical_item=None)


def test_the_recorded_contract_artifact_blocks_the_mismatch():
    path = _REPO / "data" / "kg-plans" / \
        "kg-stage2-s2-promotion-contract-20260912T201100Z.json"
    assert path.exists()
    import json
    contract = json.loads(path.read_text())
    assert P.validate_contract(contract) == []
    assert contract["counts"]["blocked"] == 3
    with pytest.raises(P.PromotionRefused):
        P.assert_promotable(contract)
