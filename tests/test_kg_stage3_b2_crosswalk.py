#!/usr/bin/env python3
"""B2 crosswalk: writer conventions, promotion rules, collisions, artifact."""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage3_b2_crosswalk as X  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _pg():
    from scripts.db.core import get_engine
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("the crosswalk reads the live development tier")
    return engine


def _cand(**over):
    c = {"db_id": 1, "meeting_id": "902122", "corroboration": None}
    c.update(over)
    return c


# --- promotion rules --------------------------------------------------------

def test_no_candidates_is_unmatched():
    v = X.classify_meeting([])
    assert v["class"] == "unmatched" and v["reason_code"] == "no_counterpart"


def test_one_counterpart_with_item_corroboration_promotes():
    v = X.classify_meeting([_cand(corroboration="item_numbers_align")])
    assert v["class"] == "deterministic_crosswalk"
    assert v["reason_code"] == "single_counterpart_with_item_number_corroboration"
    assert v["target"] == "902122"


def test_one_counterpart_with_exact_title_promotes():
    v = X.classify_meeting([_cand(corroboration="exact_title_match")])
    assert v["class"] == "deterministic_crosswalk"
    assert v["reason_code"] == "single_counterpart_with_exact_title_corroboration"


def test_date_and_body_alone_never_promote():
    v = X.classify_meeting([_cand(corroboration=None)])
    assert v["class"] == "candidate_needs_review"
    assert v["reason_code"] == "counterpart_present_without_corroboration"


def test_several_counterparts_are_ambiguous_not_resolved():
    v = X.classify_meeting([_cand(db_id=1), _cand(db_id=2)])
    assert v["class"] == "candidate_needs_review"
    assert v["reason_code"] == "ambiguous_multiple_counterparts"


def test_corroborated_counterparts_that_disagree_are_a_contradiction():
    v = X.classify_meeting([_cand(db_id=1, meeting_id="a", corroboration="item_numbers_align"),
                            _cand(db_id=2, meeting_id="b", corroboration="item_numbers_align")])
    assert v["class"] == "contradiction"
    assert v["reason_code"] == "conflicting_corroboration"


def test_every_class_carries_a_declared_reason_code():
    for cands in ([], [_cand()], [_cand(corroboration="item_numbers_align")],
                  [_cand(db_id=1), _cand(db_id=2)]):
        assert X.classify_meeting(cands)["reason_code"] in X.REASON_CODES


# --- writer conventions -----------------------------------------------------

def test_namespaces_are_distinguished():
    assert X.aem_namespace("publicmeetings-results-2022-june-220607005r") is True
    assert X.aem_namespace("902122") is False
    assert X.AEM_NAMESPACE_RE.startswith("^publicmeetings-")


def test_normalisation_collapses_whitespace_and_case():
    assert X.norm_text("  Site  Plan Review ") == "site plan review"
    assert X.norm_text(None) == ""


# --- live reconciliation ----------------------------------------------------

def test_the_live_crosswalk_reconciles_the_whole_scope():
    with _pg().connect() as c:
        d = X.reconcile(c)
    assert d["reconciles"] is True
    assert sum(d["classes"].values()) == d["total"] == 4196
    ids = [m["meeting_db_id"] for m in d["per_meeting"]]
    assert len(set(ids)) == len(ids)
    # every deterministic pair must carry corroboration beyond date/body
    for m in d["per_meeting"]:
        if m["class"] == "deterministic_crosswalk":
            assert m["corroboration"] in ("item_numbers_align", "exact_title_match")
            assert m["target"]
            assert m["candidates"] == 1


def test_contradictions_never_carry_a_target():
    with _pg().connect() as c:
        d = X.reconcile(c)
    for m in d["per_meeting"]:
        if m["class"] == "contradiction":
            assert m["target"] is None


def test_the_crosswalk_artifact_is_read_only_and_cohort_bound():
    hits = [p for p in sorted(_PLANS.glob("kg-stage3-b2-crosswalk-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if not hits:
        pytest.skip("no crosswalk artifact yet")
    doc = A.load_verified(hits[-1])
    assert doc["mode"] == "read-only"
    assert doc["write_path"] == "absent by design"
    assert doc["applied"] is False
    assert doc["no_merge"] is True and doc["no_fetch"] is True
    assert doc["producer"]["code_sha256"]
    # v1 binds the discovery artifact; v2 binds its predecessor + the review artifact.
    bindings = doc["bindings"]
    assert ("b2p1_discovery_digest" in bindings
            or ("prior_crosswalk_digest" in bindings and "review_artifact_digest" in bindings)), \
        f"the artifact must bind its provenance, got {sorted(bindings)}"
    cohort = doc["first_crosswalk_cohort"]
    assert cohort["size"] == len(cohort["pairs"])
    assert cohort["size"] == doc["classification"]["classes"]["deterministic_crosswalk"]
    assert cohort["no_write_path"] is True
