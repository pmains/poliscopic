#!/usr/bin/env python3
"""B2 review: discriminating corroboration, classification, artifacts."""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage3_b2_review as RV  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _cand(cid, rules, mid=None):
    return {"counterpart_db_id": cid, "counterpart_meeting_id": mid or str(cid),
            "counterpart_source_url": "https://phoenix.legistar.com/x",
            "fired_rules": list(rules)}


# --- classification ---------------------------------------------------------

def test_no_candidates_is_unavailable():
    v = RV.classify_review({"candidates": []})
    assert v["class"] == "unavailable" and v["reason_code"] == "no_candidate_counterpart"


def test_no_fired_rule_is_insufficient_evidence():
    v = RV.classify_review({"candidates": [_cand(1, [])]})
    assert v["class"] == "insufficient_evidence"
    assert v["reason_code"] == "no_rule_fired"


def test_a_single_discriminating_rule_promotes():
    v = RV.classify_review({"candidates": [_cand(1, ["item_number_set"])]})
    assert v["class"] == "deterministic_crosswalk"
    assert v["reason_code"] == "unique_discriminating_corroboration"
    assert v["target"] == "1" and v["rule"] == ["item_number_set"]


def test_two_candidates_sharing_a_rule_are_not_discriminating():
    v = RV.classify_review({"candidates": [_cand(1, ["official_title_identity"]),
                                           _cand(2, ["official_title_identity"])]})
    assert v["class"] == "contradiction"
    assert v["reason_code"] == "competing_corroborated_candidates"
    assert v["target"] is None


def test_two_candidates_never_promote_even_with_different_rules():
    v = RV.classify_review({"candidates": [_cand(1, ["item_number_set"]),
                                           _cand(2, ["case_number_agreement"])]})
    assert v["class"] == "contradiction"
    assert v["target"] is None


def test_a_contradiction_never_carries_a_target():
    for cands in ([_cand(1, ["item_number_set"]), _cand(2, ["item_number_set"])],
                  [_cand(1, ["item_number_set"]), _cand(2, ["case_number_agreement"])]):
        assert RV.classify_review({"candidates": cands})["target"] is None


def test_every_class_carries_a_declared_reason_code():
    for cands in ([], [_cand(1, [])], [_cand(1, ["item_number_set"])],
                  [_cand(1, ["item_number_set"]), _cand(2, ["item_number_set"])]):
        assert RV.classify_review({"candidates": cands})["reason_code"] in RV.REASON_CODES


def test_rules_are_all_source_grounded_no_proximity_rule_exists():
    assert set(RV.RULES) == {"item_number_set", "case_number_agreement",
                             "official_title_identity", "explicit_identifier"}
    for text_value in RV.RULES.values():
        lowered = text_value.lower()
        assert "fuzzy" not in lowered and "similar" not in lowered
        assert "infer" not in lowered


def test_normalization_helpers():
    assert RV.norm_text("  A  B ") == "a b"
    assert RV.norm_num("C-12.34") == "c1234"
    assert RV.norm_num(None) == ""


# --- live reconciliation ----------------------------------------------------

def _old_crosswalk():
    """The pre-review crosswalk head.  It is now superseded, so read the file directly
    rather than skipping - the superseded artifact is still the authoritative record of
    the 381-meeting scope under review."""
    named = _PLANS / "kg-stage3-b2-crosswalk-20260914T152423Z.json"
    if named.exists():
        return A.load_verified(named)
    pytest.skip("the pre-review crosswalk artifact is not present")


def test_the_review_scope_is_the_381_and_reconciles_once():
    from scripts.db.core import get_engine
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("review reads the live development tier")
    old = _old_crosswalk()
    scope = RV.review_scope(engine.connect(), old)
    assert len(scope) == 381


def test_the_review_artifact_is_read_only_and_reconciled():
    hits = [p for p in sorted(_PLANS.glob("kg-stage3-b2-review-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()
            and "worksheet" not in p.name]
    if not hits:
        pytest.skip("no review artifact yet")
    doc = A.load_verified(hits[-1])
    assert doc["mode"] == "read-only"
    assert doc["write_path"] == "absent by design"
    assert doc["applied"] is False and doc["no_merge"] is True and doc["no_fetch"] is True
    assert doc["classification"]["reconciles"] is True
    assert doc["classification"]["total"] == 381
    assert sum(doc["classification"]["classes"].values()) == 381
    assert doc["producer"]["code_sha256"]


def test_the_worksheet_covers_the_residual_and_has_no_write_path():
    hits = [p for p in sorted(_PLANS.glob("kg-stage3-b2-review-worksheet-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if not hits:
        pytest.skip("no worksheet yet")
    ws = A.load_verified(hits[-1])
    assert ws["no_write_path"] is True
    assert ws["population"] == len(ws["rows"])
    assert all(r["class"] in ("insufficient_evidence", "contradiction") for r in ws["rows"])


def test_the_regenerated_crosswalk_preserves_the_population_and_cohort():
    hits = [p for p in sorted(_PLANS.glob("kg-stage3-b2-crosswalk-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if not hits:
        pytest.skip("no crosswalk artifact yet")
    doc = A.load_verified(hits[-1])
    assert doc["version"] == "kg-stage3-b2-crosswalk/2.0"
    assert doc["classification"]["total"] == 4196, "the original population must stay exact"
    assert sum(doc["classification"]["classes"].values()) == 4196
    cohort = doc["first_crosswalk_cohort"]
    assert cohort["size"] == doc["classification"]["classes"]["deterministic_crosswalk"]
    assert cohort["size"] >= 10, "the original 10 must survive the review"
    assert cohort["no_write_path"] is True
    assert any(p["origin"] == "prior" for p in cohort["pairs"])
    assert doc["bindings"]["prior_crosswalk_digest"]
    assert doc["bindings"]["review_artifact_digest"]
