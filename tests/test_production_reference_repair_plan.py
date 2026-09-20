"""Focused tests for the immutable, evidence-only production repair plan."""

from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
for candidate in (ROOT, ROOT / "scripts", OPS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

P = importlib.import_module("production_preflight")
R = importlib.import_module("production_reference_repair_plan")


def _row(*, row_id=1, candidate_count=1, jurisdiction_id=5,
         upstream_parent=37):
    candidates = ([{"id": 37, "body_code": "mesa-pz",
                    "name": "Mesa Planning & Zoning Board", "jurisdiction_id": 5}]
                  if candidate_count == 1 else
                  ([{"id": 1}, {"id": 2}] if candidate_count == 2 else []))
    return {"id": row_id, "body": "mesa-pz", "public_body_id": None,
            "meeting_db_id": 826, "meeting_id": "1415343",
            "agenda_item_id": "1415343_2-a", "agenda_item_number": "2-a",
            "jurisdiction_id": jurisdiction_id,
            "candidate_parent_count": candidate_count,
            "candidate_parents": candidates,
            "upstream_identity": {"meeting_db_id": 826, "meeting_id": "1415343",
                                  "body": "mesa-pz", "jurisdiction_id": 5,
                                  "public_body_id": upstream_parent}}


def _evidence(rows_by_category):
    categories = {}
    for name, rows in rows_by_category.items():
        categories[name] = {"total": len(rows), "sample_count": len(rows),
                            "truncated": False, "rows": rows}
    return {"target": {"database": "poliscopic"}, "categories": categories}


def _bindings():
    return {"path": "g5.json", "digest": "g" * 64,
            "evidence_digest": "e" * 64}


def test_only_one_candidate_with_fully_consistent_identity_is_proposed():
    good = _row()
    wrong_jurisdiction = _row(row_id=2, jurisdiction_id=99)
    wrong_upstream_parent = _row(row_id=3, upstream_parent=99)
    plan = R.build_plan(_evidence({
        "agenda_items.public_body_id.dangling_or_null":
            [good, wrong_jurisdiction, wrong_upstream_parent]}), _bindings())
    assert [entry["primary_key"] for entry in plan["proposals"]] == [{"id": 1}]
    assert plan["proposals"][0]["before"]["public_body_id"] is None
    assert plan["proposals"][0]["set"] == {"public_body_id": 37}
    assert plan["apply_blocked"] is True
    assert plan["status"] == "CANDIDATE-NOT-AUTHORIZABLE"
    assert "fresh_backup_receipt" in plan["missing_g7_bindings"]
    assert {entry["reason"] for entry in plan["quarantine"]} == {
        "row_identity_disagrees_with_candidate",
        "upstream_parent_disagrees_with_candidate",
    }


@pytest.mark.parametrize("count,reason", [
    (0, "zero_candidate_parents"), (2, "multiple_candidate_parents")])
def test_zero_and_multiple_candidate_rows_are_quarantined(count, reason):
    plan = R.build_plan(_evidence({
        "agenda_items.public_body_id.dangling_or_null":
            [_row(candidate_count=count)]}), _bindings())
    assert not plan["proposals"]
    assert plan["quarantine"][0]["reason"] == reason


def test_unsafe_categories_are_never_automatic_repairs():
    categories = {
        "meetings.body.sentinel": [_row()],
        "member_votes.body.sentinel": [_row(row_id=2)],
        "agenda_items.body.dangling": [_row(row_id=3, candidate_count=0)],
    }
    plan = R.build_plan(_evidence(categories), _bindings())
    assert not plan["proposals"]
    assert len(plan["quarantine"]) == 3


def test_incomplete_evidence_is_refused():
    evidence = _evidence({"meetings.public_body_id.dangling_or_null": [_row()]})
    evidence["categories"]["meetings.public_body_id.dangling_or_null"]["truncated"] = True
    with pytest.raises(R.Refused, match="truncated"):
        R.build_plan(evidence, _bindings())


def test_plan_digest_binds_exact_primary_keys_and_before_values():
    plan = R.build_plan(_evidence({
        "agenda_items.public_body_id.dangling_or_null": [_row()]}), _bindings())
    assert plan["digest"] == P.digest({k: v for k, v in plan.items() if k != "digest"})
    tampered = copy.deepcopy(plan)
    tampered["proposals"][0]["before"]["meeting_id"] = "changed"
    assert tampered["digest"] != P.digest(
        {k: v for k, v in tampered.items() if k != "digest"})


def test_live_evidence_yields_only_the_strictly_consistent_cohorts(monkeypatch):
    path = ROOT / "data/audit/20260919T161500Z-production-reference-evidence.json"
    evidence_document = json.loads(path.read_text(encoding="utf-8"))
    g5_path = ROOT / evidence_document["g5_binding"]["path"]
    g5_document = json.loads(g5_path.read_text(encoding="utf-8"))
    from scripts import body_code_merge_runtime as runtime
    monkeypatch.setitem(
        runtime.PRODUCTION_TARGET, "host", g5_document["pinned_target"]["host"])
    evidence, bindings = R.load_bound_evidence(path)
    plan = R.build_plan(evidence, bindings)
    assert plan["counts"] == {"proposals": 7973, "quarantine": 3621}
    by_category = {}
    for proposal in plan["proposals"]:
        by_category[proposal["category"]] = by_category.get(proposal["category"], 0) + 1
    assert by_category == {
        "meetings.public_body_id.dangling_or_null": 1124,
        "agenda_items.public_body_id.dangling_or_null": 6849,
    }
    assert all(entry["before"]["body"] not in {"glendale-cc", "phoenix-gp"}
               for entry in plan["proposals"])


def test_no_database_network_or_apply_surface():
    source = (OPS / "production_reference_repair_plan.py").read_text()
    assert "create_engine" not in source
    assert "requests" not in source
    assert "def apply" not in source
    assert "--apply" not in source
