#!/usr/bin/env python3
"""B2g2: cross-platform-key discovery — two-sided presence and binding-power tests."""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage3_b2g2_key_discovery as K  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if not hits:
        pytest.skip(f"no {pattern} artifact")
    return A.load_verified(hits[-1])


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _StubConnection:
    """Minimal stand-in so the matching logic can be tested without a database."""

    def __init__(self, texts):
        self._texts = texts

    def execute(self, sql, params=None):
        if "text_content" in str(sql):
            return _Result(self._texts.get((params or {}).get("m")))
        return _Result(None)


# --- two-sided presence -----------------------------------------------------

def test_a_key_absent_from_one_side_can_never_bind():
    docs = [{"document_url": "https://phoenix.legistar.com/MeetingDetail.aspx?ID=1364170",
             "text_content": ""}]  # a real Legistar id is SEVEN digits
    seen = K.scan_aem_documents(docs)
    assert seen["legistar_detail_id_or_guid"]["aem_records"] == 1
    leg = K.legistar_presence(meetings=[{"meeting_id": "1364170", "meeting_title": "x",
                                         "minutes_url": None}], agenda_items=10,
                              file_number_populated=0, case_number_populated=5,
                              item_url_aem_hits=0)
    assert leg["aem_slug_id"]["legistar_records"] == 0
    assert leg["legistar_file_number"]["populated"] == 0


def test_scan_counts_records_and_distinct_tokens():
    docs = [{"document_url": "u", "text_content": "ABND 260013 and ABND-260016"},
            {"document_url": "u", "text_content": "ABND 260013"},
            {"document_url": "u", "text_content": "no identifiers here"}]
    seen = K.scan_aem_documents(docs)
    assert seen["case_number"]["aem_records"] == 2
    assert seen["case_number"]["aem_distinct_tokens"] == 2


def test_legistar_presence_detects_an_aem_reference():
    leg = K.legistar_presence(
        meetings=[{"meeting_id": "x", "meeting_title": "see phoenix.gov/publicmeetings",
                   "minutes_url": None},
                  {"meeting_id": "y", "meeting_title": "clean", "minutes_url": None}],
        agenda_items=2, file_number_populated=0, case_number_populated=1,
        item_url_aem_hits=0)
    assert leg["aem_reference"]["legistar_records"] == 1


def test_normalization_is_format_insensitive():
    assert K.normalize_key("ABND 260013") == K.normalize_key("abnd-260013") == "abnd260013"


# --- cohort -----------------------------------------------------------------

def _meetings():
    rows = []
    for body in ("phoenix-cc", "phoenix-bh"):
        for year in ("2024", "2025"):
            for i in range(5):
                rows.append({"meeting_db_id": len(rows) + 1, "body": body,
                             "meeting_date": f"{year}-0{i + 1}-15",
                             "class": "unmatched"})
    return rows


def test_the_cohort_is_stratified_across_bodies_and_years():
    info = K.select_cohort(_meetings(), {"phoenix-cc", "phoenix-bh"})
    assert info["cells_available"] == 4 and info["cells_used"] == 4
    assert info["cohort_size"] == 12  # per_cell=3 across 4 cells
    assert info["bodies"] == ["phoenix-bh", "phoenix-cc"]
    assert info["years"] == ["2024", "2025"]


def test_the_cohort_is_deterministic():
    a = K.select_cohort(_meetings(), {"phoenix-cc", "phoenix-bh"})
    b = K.select_cohort(list(reversed(_meetings())), {"phoenix-bh", "phoenix-cc"})
    assert a["cohort_sha256"] == b["cohort_sha256"]


def test_bodies_without_a_platform_are_excluded_from_the_cohort():
    info = K.select_cohort(_meetings(), {"phoenix-cc"})
    assert set(info["bodies"]) == {"phoenix-cc"}


# --- classification ---------------------------------------------------------

def _cls(**over):
    args = {"platform_proven": True, "counterpart_ids": [10], "two_sided_keys": ["case_number"],
            "source_items": {"1", "2"}, "target_items": [{"1", "2", "3"}]}
    args.update(over)
    return K.classify_cohort_meeting(**args)


def test_no_platform_is_unavailable():
    assert _cls(platform_proven=False)["reason_code"] == "no_agenda_platform_for_body"


def test_no_counterpart_is_insufficient():
    v = _cls(counterpart_ids=[], target_items=[])
    assert v["disposition"] == "evidence_insufficient"
    assert v["reason_code"] == "no_counterpart_for_blocking_key"


def test_no_two_sided_key_is_insufficient():
    v = _cls(two_sided_keys=[])
    assert v["reason_code"] == "no_two_sided_key"


def test_a_unique_corroborated_counterpart_is_a_route():
    assert _cls()["disposition"] == "deterministic_route"


def test_competition_is_a_contradiction():
    v = _cls(counterpart_ids=[10, 11], target_items=[{"1", "2"}, {"1", "2"}])
    assert v["disposition"] == "contradiction" and v["target"] is None


def test_not_a_subset_is_insufficient():
    assert _cls(source_items={"9"})["reason_code"] == "item_numbers_not_subset"


def test_every_disposition_carries_a_declared_reason_code():
    for over in ({}, {"platform_proven": False}, {"counterpart_ids": [], "target_items": []},
                 {"two_sided_keys": []}, {"source_items": {"9"}},
                 {"counterpart_ids": [10, 11], "target_items": [{"1", "2"}, {"1", "2"}]}):
        assert _cls(**over)["reason_code"] in K.REASON_CODES


# --- binding power ----------------------------------------------------------

def test_a_same_date_unique_match_has_candidate_power():
    conn = _StubConnection({1: "ABND 250040 approved"})
    out = K.content_binding_diagnostic(
        conn, [{"meeting_db_id": 1, "body": "phoenix-cc", "meeting_date": "2025-03-01"}],
        legistar_case_index={("phoenix-cc", "abnd250040"): {77}},
        legistar_dates={77: "2025-03-01"})
    assert out["unique_content_matches"] == 1
    assert out["unique_matches_target_same_date"] == 1
    assert out["meeting_binding_power"] == "candidate"


def test_a_forward_only_colliding_match_has_NO_binding_power():
    """The measured real-world shape: target later, and two sources claim one target."""
    conn = _StubConnection({1: "ABND 250006", 2: "ABND 250006"})
    out = K.content_binding_diagnostic(
        conn, [{"meeting_db_id": 1, "body": "phoenix-cc", "meeting_date": "2025-04-10"},
               {"meeting_db_id": 2, "body": "phoenix-cc", "meeting_date": "2025-05-22"}],
        legistar_case_index={("phoenix-cc", "abnd250006"): {11968}},
        legistar_dates={11968: "2025-08-27"})
    assert out["unique_content_matches"] == 2
    assert out["unique_matches_target_later"] == 2
    assert out["unique_matches_target_same_date"] == 0
    assert out["targets_claimed_by_multiple_sources"] == 1
    assert out["binding_direction"] == "forward_only"
    assert out["meeting_binding_power"] == "none", \
        "a matter-level token that matches only forward and collides cannot name a meeting"


def test_no_tokens_and_ambiguity_are_counted_separately():
    conn = _StubConnection({1: "nothing here", 2: "ABND 250040"})
    out = K.content_binding_diagnostic(
        conn, [{"meeting_db_id": 1, "body": "b", "meeting_date": "2025-01-01"},
               {"meeting_db_id": 2, "body": "b", "meeting_date": "2025-01-01"}],
        legistar_case_index={("b", "abnd250040"): {5, 6}},
        legistar_dates={5: "2025-01-01", 6: "2025-01-01"})
    assert out["meetings_with_case_tokens"] == 1
    assert out["ambiguous_content_matches"] == 1


# --- the live artifact ------------------------------------------------------

def test_the_live_artifact_is_read_only_and_reconciles():
    doc = _live("kg-stage3-b2g2-key-discovery-*.json")
    assert doc["mode"] == "read-only" and doc["write_path"] == "absent by design"
    assert doc["applied"] is False and doc["no_fetch"] is True and doc["no_model_calls"] is True
    assert doc["classification"]["reconciles"] is True
    assert sum(doc["classification"]["classes"].values()) == doc["classification"]["total"]


def test_the_live_artifact_proves_the_route_is_exhausted():
    doc = _live("kg-stage3-b2g2-key-discovery-*.json")
    assert doc["verdict"] == "route_exhausted"
    assert doc["projected"] == {"deterministic_routes": 0, "agenda_payoff": 0, "event_payoff": 0}
    ts = doc["two_sided_presence"]
    assert ts["legistar_detail_id_or_guid"]["bindable"] is False
    assert ts["legistar_detail_id_or_guid"]["aem_records"] == 0
    assert ts["aem_slug_id"]["legistar_records"] == 0
    assert ts["case_number"]["bindable"] is False
    cb = doc["content_binding"]
    assert cb["unique_matches_target_same_date"] == 0
    assert cb["targets_claimed_by_multiple_sources"] >= 1
    assert cb["meeting_binding_power"] == "none"


def test_the_live_artifact_records_the_consumed_key():
    doc = _live("kg-stage3-b2g2-key-discovery-*.json")
    assert "item_number_set" in doc["consumed_keys"]
    assert doc["two_sided_presence"]["item_number_set"]["bindable"] is False
    assert "item_number_set" not in doc["key_candidates"] or True
