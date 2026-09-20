#!/usr/bin/env python3
"""B2g: parser correction, cohort selection and discovery classification tests."""

from __future__ import annotations

import re
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage3_b2g_discovery as G  # noqa: E402
from scripts.kg import stage3_meeting_result_identity as B1  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if not hits:
        pytest.skip(f"no {pattern} artifact")
    return A.load_verified(hits[-1])


# --- the reusable parser correction ----------------------------------------

def test_the_item_regex_no_longer_caps_leading_whitespace():
    """The defect: legitimately indented item lines were discarded."""
    deeply_indented = " " * 30 + "2. Variance to allow\n" + " " * 24 + "3. Another item\n"
    spans = B1.extract_item_spans(deeply_indented)
    assert [s["token"] for s in spans] == ["2", "3"], \
        "an item line indented more than 8 spaces must still be recognised"


def test_the_old_whitespace_cap_would_have_missed_those_lines():
    legacy = re.compile(r"(?m)^[\s\x0c]{0,8}(\d{1,3}(?:\.[A-Za-z0-9]{1,3}){0,2})[.)]\s")
    text = " " * 30 + "2. Variance to allow\n"
    assert legacy.findall(text) == [], "the legacy regex is the defect being fixed"
    assert [s["token"] for s in B1.extract_item_spans(text)] == ["2"]


def test_the_correction_is_purely_additive_on_representative_text():
    """Everything the old rule matched, the new rule still matches."""
    legacy = re.compile(r"(?m)^[\s\x0c]{0,8}(\d{1,3}(?:\.[A-Za-z0-9]{1,3}){0,2})[.)]\s")
    samples = ["1. a\n2. b\n", " 3) c\n", "2.A sub item\n", "no numbers here\n",
               "\x0c5. form feed item\n", "   10. indented\n"]
    for text in samples:
        old = {m.group(1) for m in legacy.finditer(text)}
        new = {s["token"] for s in B1.extract_item_spans(text)}
        assert old <= new, f"the correction dropped {old - new!r} from {text!r}"


def test_prose_is_still_never_an_item():
    assert B1.extract_item_spans("we met on 2. That was fine.\n") == []
    assert B1.extract_item_spans("see section 5. For details\n") == []
    assert B1.extract_item_spans("") == []


def test_duplicates_introduced_by_wider_matching_are_still_held():
    """A duplicate number must hold the document, never be guessed."""
    text = "   1. first\n" + " " * 40 + "1. duplicate\n"
    verdict = B1.classify_document(text_content=text, quarantined=False,
                                   existing_identity="result-a-b",
                                   canonical_numbers={"1"})
    assert verdict["class"] == "hold_ambiguous_identity"
    assert verdict["items"] == []


def test_normalization_is_unchanged_by_the_correction():
    assert B1.normalize_item_number("01") == "1"
    assert B1.normalize_item_number("2.a") == "2.A"
    assert B1.normalize_item_number("0") is None


# --- discovery classification ----------------------------------------------

def _cls(**over):
    args = {"platform_proven": True, "counterpart_ids": [10], "source_items": {"1", "2"},
            "target_items": [{"1", "2", "3"}]}
    args.update(over)
    return G.classify_meeting(**args)


def test_no_platform_is_unavailable():
    v = _cls(platform_proven=False)
    assert v["disposition"] == "unavailable"
    assert v["reason_code"] == "no_agenda_platform_for_body"


def test_no_counterpart_is_evidence_insufficient():
    v = _cls(counterpart_ids=[], target_items=[])
    assert v["disposition"] == "evidence_insufficient"
    assert v["reason_code"] == "no_counterpart_for_blocking_key"


def test_no_numbered_item_evidence_is_insufficient():
    v = _cls(source_items=set())
    assert v["reason_code"] == "no_numbered_item_evidence"


def test_a_not_subset_relation_is_insufficient():
    v = _cls(source_items={"1", "9"}, target_items=[{"1", "2"}])
    assert v["reason_code"] == "item_numbers_not_subset"


def test_a_unique_corroborated_counterpart_is_a_deterministic_route():
    v = _cls()
    assert v["disposition"] == "deterministic_route"
    assert v["reason_code"] == "unique_corroborated_counterpart" and v["target"] == 10


def test_two_corroborated_counterparts_are_a_contradiction():
    v = _cls(counterpart_ids=[10, 11], target_items=[{"1", "2"}, {"1", "2"}])
    assert v["disposition"] == "contradiction"
    assert v["target"] is None


def test_every_class_carries_a_declared_reason_code():
    for kwargs in ({"platform_proven": False}, {"counterpart_ids": [], "target_items": []},
                   {"source_items": set()}, {"source_items": {"9"}, "target_items": [{"1"}]},
                   {}, {"counterpart_ids": [10, 11],
                        "target_items": [{"1", "2"}, {"1", "2"}]}):
        assert _cls(**kwargs)["reason_code"] in G.REASON_CODES


def test_the_classification_never_uses_similarity_alone():
    """An empty evidence set can never promote, however well the dates and bodies line up."""
    v = _cls(source_items=set(), counterpart_ids=[10], target_items=[{"1", "2"}])
    assert v["disposition"] != "deterministic_route"


# --- cohort + artifact ------------------------------------------------------

def test_the_cohort_criteria_are_explicit():
    assert len(G.COHORT_CRITERIA) == 4
    joined = " ".join(G.COHORT_CRITERIA).lower()
    assert "unmatched" in joined and "namespace" in joined and "result" in joined


def test_the_live_artifact_reconciles_and_has_no_write_path():
    from scripts.db.core import get_engine
    doc = _live("kg-stage3-b2g-discovery-*.json")
    assert doc["mode"] == "read-only"
    assert doc["write_path"] == "absent by design"
    assert doc["applied"] is False and doc["no_fetch"] is True
    cls = doc["classification"]
    assert cls["reconciles"] is True
    assert cls["total"] == 3805
    assert sum(cls["classes"].values()) == 3805
    assert cls["cohort"]["cohort_size"] == len(cls["cohort"]["cohort"])
    assert doc["bindings"]["crosswalk_digest"] and doc["bindings"]["backlog_digest"]
    assert doc["producer"]["code_hashes"]


def test_platform_proven_bodies_match_their_own_city():
    doc = _live("kg-stage3-b2g-discovery-*.json")
    for body in doc["classification"]["cohort"]["platform_proven_bodies"]:
        city = body.split("-")[0]
        assert body.startswith(city)
