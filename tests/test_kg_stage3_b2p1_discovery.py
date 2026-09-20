#!/usr/bin/env python3
"""B2-P1 discovery: promotion rules, collision proof and artifact tests."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage3_b2p1_discovery as P1  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _pg():
    from scripts.db.core import get_engine
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("discovery reads the live development tier")
    return engine


def _cls(**over):
    args = {"binding_count": 0, "key_unique": True, "key_resolving": False,
            "candidate_platform": True, "substitutions_all_404": True}
    args.update(over)
    return P1.classify({}, **args)


# --- promotion rules --------------------------------------------------------

def test_an_authoritative_binding_promotes_to_a_deterministic_route():
    v = _cls(binding_count=1)
    assert v["disposition"] == "deterministic_route"
    assert v["reason_code"] == "authoritative_binding_proven"


def test_a_unique_key_only_promotes_when_it_also_resolves():
    assert _cls(key_unique=True, key_resolving=True)["disposition"] == "deterministic_route"
    # unique but NOT resolving must never be promoted
    assert _cls(key_unique=True, key_resolving=False)["disposition"] != "deterministic_route"


def test_a_platform_candidate_without_binding_needs_review():
    v = _cls(candidate_platform=True, key_unique=True)
    assert v["disposition"] == "candidate_route_needs_review"
    assert v["reason_code"] == "platform_candidate_without_identity_binding"


def test_rejected_substitutions_are_recorded_but_never_promote():
    v = _cls(candidate_platform=False, key_unique=False, substitutions_all_404=True)
    assert v["disposition"] == "candidate_route_needs_review"
    assert v["reason_code"] == "only_guessed_substitutions_rejected"


def test_nothing_identified_is_unresolved():
    v = _cls(candidate_platform=False, key_unique=False, substitutions_all_404=False)
    assert v["disposition"] == "unresolved"


def test_every_class_has_a_reason_code():
    for kwargs in ({"binding_count": 1}, {"key_resolving": True},
                   {"candidate_platform": False, "substitutions_all_404": True},
                   {"candidate_platform": False, "substitutions_all_404": False}):
        assert _cls(**kwargs)["reason_code"] in P1.REASON_CODES


# --- collision analysis -----------------------------------------------------

def test_the_candidate_key_strips_the_results_suffix():
    assert P1.candidate_key("publicmeetings-results-2023-january-230117012r") == \
        "publicmeetings-results-2023-january-230117012"
    assert P1.candidate_key("ABC123R") == "abc123"


def test_collision_analysis_proves_uniqueness():
    r = P1.collision_analysis(["a1r", "a2r", "a3R"])
    assert r["unique"] is True and r["collisions"] == 0 and r["candidate_keys"] == 3


def test_collision_analysis_detects_a_collision():
    r = P1.collision_analysis(["a1r", "a1R", "a2r"])
    assert r["unique"] is False and r["collisions"] == 1
    assert r["collision_examples"]


def test_empty_input_is_never_unique():
    assert P1.collision_analysis([])["unique"] is False


# --- evidence ---------------------------------------------------------------

def test_no_authoritative_agenda_binding_is_recorded():
    assert P1.authoritative_bindings() == []


def test_every_agenda_substitution_probe_failed():
    subs = [p for a in P1.source_adapters() if str(a.get("id", "")).startswith("A2")
            for p in (a.get("probe") or [])]
    assert subs, "the substitution probes must be recorded"
    assert all(int(p["status"]) != 200 for p in subs)


def test_the_adapters_cover_the_named_sources():
    ids = {a["id"] for a in P1.source_adapters()}
    for expected in ("A1-results-dam-family", "A2-agenda-family-substitution",
                     "A3-city-clerk-index", "A4-site-sitemap",
                     "A5-destiny-agenda-publish", "A6-legistar"):
        assert expected in ids


# --- live reconciliation + artifact ----------------------------------------

def test_the_live_discovery_reconciles_every_meeting_once():
    with _pg().connect() as c:
        d = P1.discover(c)
    assert d["reconciles"] is True
    assert sum(d["classes"].values()) == d["total"] == 4196
    ids = [m["meeting_db_id"] for m in d["per_meeting"]]
    assert len(set(ids)) == len(ids)
    assert d["collisions"]["unique"] is True
    assert d["authoritative_bindings"] == 0
    assert d["classes"]["deterministic_route"] == 0


def test_the_discovery_artifact_is_immutable_read_only_and_cohort_safe():
    hits = [p for p in sorted(_PLANS.glob("kg-stage3-b2p1-discovery-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if not hits:
        pytest.skip("no discovery artifact yet")
    doc = A.load_verified(hits[-1])
    assert doc["mode"] == "read-only"
    assert doc["write_path"] == "absent by design"
    assert doc["applied"] is False and doc["no_fetch"] is True
    assert doc["producer"]["code_sha256"]
    assert doc["bindings"]["b2_baseline_digest"]
    cohort = doc["first_cohort"]
    assert cohort["size"] == len(cohort["meeting_db_ids"])
    if doc["classification"]["classes"]["deterministic_route"] == 0:
        assert cohort["size"] == 0
        assert cohort["expected_b2_payoff"] == 0
    assert cohort["no_write_path"] is True
