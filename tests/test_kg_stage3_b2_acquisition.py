#!/usr/bin/env python3
"""B2 source-eligibility, disposition and dry-plan tests."""

from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage3_b2_acquisition as B2  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _pg():
    from scripts.db.core import get_engine
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("the reconciliation reads the live development tier")
    return engine


def _cls(**over):
    args = {"retained_agenda_docs": 0, "reachable_agenda_urls": 0, "join_keys": 0,
            "retained_result_docs": 1, "platform_publishes_agendas": True}
    args.update(over)
    return B2.classify({}, **args)


# --- mutually exclusive dispositions ---------------------------------------

def test_a_retained_agenda_artifact_is_existing_unlinked():
    v = _cls(retained_agenda_docs=1)
    assert v["disposition"] == "existing_unlinked_artifact"


def test_a_probe_verified_reachable_url_is_reachable_reconstructable():
    v = _cls(reachable_agenda_urls=1)
    assert v["disposition"] == "reachable_reconstructable"
    assert v["reason_code"] == "probe_verified_reachable"


def test_a_platform_without_enumerated_route_and_no_join_key_needs_scraper_correction():
    v = _cls(platform_publishes_agendas=True, join_keys=0)
    assert v["disposition"] == "requires_scraper_correction"
    assert v["reason_code"] == "agenda_route_not_enumerated_and_no_join_key"


def test_nothing_retained_and_no_route_is_unavailable():
    v = _cls(retained_result_docs=0, platform_publishes_agendas=False)
    assert v["disposition"] == "unavailable_no_public_source"


def test_every_disposition_carries_a_declared_reason_code():
    for kwargs in ({"retained_agenda_docs": 1}, {"reachable_agenda_urls": 1},
                   {"platform_publishes_agendas": True}, {"retained_result_docs": 0,
                   "platform_publishes_agendas": False}):
        v = _cls(**kwargs)
        assert v["reason_code"] in B2.REASON_CODES


def test_precedence_is_exclusive_and_ordered():
    # a retained artifact beats a reachable URL and a scraper gap
    v = _cls(retained_agenda_docs=1, reachable_agenda_urls=1)
    assert v["disposition"] == "existing_unlinked_artifact"
    # a reachable URL beats a scraper gap
    v = _cls(reachable_agenda_urls=1, platform_publishes_agendas=True)
    assert v["disposition"] == "reachable_reconstructable"


# --- no URL guessing is treated as proof -----------------------------------

def test_a_substituted_url_is_never_treated_as_proof():
    probe = B2.load_probe_evidence()
    agenda_substitutions = [r for r in probe["results"]
                            if r["family"] == "agenda_substitution"]
    assert agenda_substitutions, "the probe must record the substitution attempts"
    assert all(int(r["status"]) != 200 for r in agenda_substitutions), \
        "a 404 substitution must never read as reachable"
    reachable = sum(1 for r in probe["results"]
                    if r["family"] == "agenda_substitution" and int(r["status"]) == 200)
    assert reachable == 0


def test_the_results_family_is_probe_verified_reachable():
    probe = B2.load_probe_evidence()
    live = [r for r in probe["results"] if r["family"] == "results"]
    assert live and all(int(r["status"]) == 200 for r in live)


# --- live reconciliation ----------------------------------------------------

def test_the_live_reconciliation_covers_every_meeting_exactly_once():
    with _pg().connect() as c:
        r = B2.reconcile(c)
    assert r["reconciles"] is True
    assert sum(r["dispositions"].values()) == r["total"]
    ids = [m["meeting_db_id"] for m in r["per_meeting"]]
    assert len(set(ids)) == len(ids) == r["total"]
    assert sum(r["by_year"].values()) == r["total"]
    assert sum(sum(v.values()) for v in r["by_body"].values()) == r["total"]


def test_no_meeting_in_scope_holds_an_agenda_artifact():
    with _pg().connect() as c:
        r = B2.reconcile(c)
    assert r["platform"]["agenda_docs_retained"] == 0
    assert r["platform"]["join_keys_found"] == 0
    assert r["dispositions"]["existing_unlinked_artifact"] == 0


# --- immutable artifacts ----------------------------------------------------

def _live_doc(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if not hits:
        pytest.skip(f"no {pattern} artifact yet")
    return A.load_verified(hits[-1])


def test_the_b2_baseline_is_read_only_and_reconciled():
    doc = _live_doc("kg-stage3-b2-acquisition-baseline-*.json")
    assert doc["mode"] == "read-only"
    assert doc["write_path"] == "absent by design"
    assert doc["applied"] is False
    assert doc["reconciles"] is True
    assert doc["stage2_exit_binding"]["digest"]
    assert doc["b1_binding"]["digest"]
    assert doc["digest"] == A.recorded_digest(doc)


def test_the_b2_dry_plan_has_no_write_path_and_ordered_phases():
    doc = _live_doc("kg-stage3-b2-acquisition-plan-*.json")
    assert doc["mode"] == "dry-run" and doc["applied"] is False
    assert doc["write_path"] == "absent by design"
    assert doc["no_write_path"] is True
    seen = []
    for phase in doc["phases"]:
        for dep in phase["depends_on"]:
            assert dep in seen, f"{phase['id']} depends on {dep} which is not ordered before it"
        assert phase["executes"] is False
        seen.append(phase["id"])
    assert doc["rate_limits"]["concurrency"] == 1
    assert doc["retry_policy"]["max_attempts"] >= 2
    assert doc["receipt_design"]["no_write_before_receipt"] is True
    assert doc["first_cohort"]["size"] == len(doc["first_cohort"]["meeting_db_ids"])
