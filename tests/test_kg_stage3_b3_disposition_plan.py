"""Adversarial tests for the read-only B3 held-span disposition record."""

from copy import deepcopy
from pathlib import Path

import pytest

from scripts.kg import stage3_b3_disposition_plan as D
from scripts.kg import stage3_b3_baseline as B
from scripts.kg import stage3_b3_span_contract as C
from scripts.kg.stage2_artifacts import load_verified, write_immutable


REPO = Path(__file__).resolve().parents[1]
REAL_BASELINE = REPO / "data/kg-plans/kg-stage3-b3-span-baseline-20260914T204038Z.json"
REAL_DRY_PLAN = REPO / "data/kg-plans/kg-stage3-b3-span-dry-plan-20260914T204038Z.json"


def _write_inputs(tmp_path):
    target = {"tier": "test", "database": "test_db", "dialect": "postgresql"}
    schema = {"sha256": "a" * 64}
    inputs = {"documents": 3, "events": 0, "extractions": 0, "agenda_items": 0}
    holds = [
        {"index": 0, "identity": [1, "stage1_meeting_event", 0, 1],
         "disposition": "hold_unlinked_container", "problems": []},
        {"index": 1, "identity": [2, "stage1_meeting_event", 1, 2],
         "disposition": "hold_unlinked_container", "problems": []},
        {"index": 2, "identity": [3, "stage3_result_item", 2, 3],
         "disposition": "hold_unlinked_container", "problems": []},
        {"index": 3, "identity": [99, "stage3_result_item", 3, 4],
         "disposition": "hold_unlinked_container", "problems": []},
        {"index": 4, "identity": [1, "stage1_meeting_event", -1, 1],
         "disposition": "hold_unlinked_container", "problems": []},
    ]
    baseline = {
        "kind": D.BASELINE_KIND, "version": B.PRODUCER_VERSION,
        "mode": "read-only", "applied": False, "write_path": "absent by design",
        "code_hashes": B.code_hashes(),
        "target": target, "schema": schema, "inputs": inputs,
        "coverage": {"proposed": 5, "accepted": 0, "held": 5, "reconciles": True},
        "documents": [
            {"id": 1, "meeting_db_id": 70, "agenda_item_db_id": 700,
             "text_sha256": "1" * 64, "text_extraction_method": "fixture"},
            {"id": 2, "meeting_db_id": 71, "agenda_item_db_id": None,
             "text_sha256": "2" * 64, "text_extraction_method": "fixture"},
            {"id": 3, "meeting_db_id": 0, "agenda_item_db_id": None,
             "text_sha256": "3" * 64, "text_extraction_method": "fixture"},
        ],
        "holds": holds,
    }
    baseline_path = tmp_path / "baseline.json"
    write_immutable(baseline_path, baseline)
    baseline = load_verified(baseline_path)
    dry = {
        "kind": D.DRY_PLAN_KIND, "version": C.PRODUCER_VERSION,
        "created_at": "2026-09-15T02:00:00Z", "mode": "dry-run",
        "applied": False, "write_path": "absent by design", "target": target,
        "producer": {"module": "scripts/kg/stage3_b3_span_contract.py",
                      "version": C.PRODUCER_VERSION, "code_hashes": B.code_hashes()},
        "baseline_artifact": {"path": str(baseline_path), "digest": baseline["digest"]},
        "baseline_digest": baseline["digest"], "schema_sha256": schema["sha256"],
        "input_bindings": inputs,
        "accounting": {"proposed": 5, "held": 5, "would_insert": 0, "reconciles": True},
        "operations": [], "operations_sha256": C.canonical_sha256([]),
    }
    dry_path = tmp_path / "dry.json"
    write_immutable(dry_path, dry)
    return baseline_path, dry_path


def _plan(tmp_path):
    baseline, dry = _write_inputs(tmp_path)
    return D.build_plan(baseline_path=baseline, dry_plan_path=dry,
                        created_at="2026-09-15T02:00:00Z")


def test_all_outcomes_are_exclusive_and_have_evidence(tmp_path):
    plan = _plan(tmp_path)
    assert plan["accounting"] == {
        "population": 5,
        "by_outcome": {"item_linked": 1, "meeting_linked": 1, "document_only": 1,
                       "source_gap": 1, "invalid_span": 1},
        "classified": 5, "reconciles": True, "item_container_fabricated": False,
        "data_operations_proposed": 0,
    }
    records = plan["dispositions"]
    assert [row["outcome"] for row in records] == list(D.OUTCOMES)
    assert records[0]["agenda_item_db_id"] == 700
    assert "meeting_db_id" not in records[0]
    assert records[1]["meeting_db_id"] == 71
    assert "agenda_item_db_id" not in records[1]
    assert records[1]["evidence_identity"]["supporting_doc_id"] == 2
    assert all(row["reason_code"] == D.REASON_CODES[row["outcome"]] for row in records)
    assert all("baseline_digest" in row["evidence_identity"] for row in records)
    assert D.validate_plan(plan) == []


def test_duplicate_source_index_and_nonempty_dry_operations_fail_closed(tmp_path):
    baseline_path, dry_path = _write_inputs(tmp_path)
    baseline = load_verified(baseline_path)
    baseline["holds"][1]["index"] = 0
    duplicate = tmp_path / "duplicate-baseline.json"
    write_immutable(duplicate, baseline)
    dry = load_verified(dry_path)
    dry["baseline_artifact"] = {"path": str(duplicate), "digest": load_verified(duplicate)["digest"]}
    dry["baseline_digest"] = load_verified(duplicate)["digest"]
    rebound = tmp_path / "rebound-dry.json"
    write_immutable(rebound, dry)
    with pytest.raises(D.PlanRefused, match="unique source index"):
        D.build_plan(baseline_path=duplicate, dry_plan_path=rebound, created_at="now")

    dry["operations"] = [{"fabricated": True}]
    dry["operations_sha256"] = C.canonical_sha256(dry["operations"])
    operations = tmp_path / "operations-dry.json"
    write_immutable(operations, dry)
    with pytest.raises(D.PlanRefused, match="no-operation held population"):
        D.build_plan(baseline_path=duplicate, dry_plan_path=operations, created_at="now")


@pytest.mark.parametrize("field,needle", [
    ("version", "baseline producer version"),
    ("code_hashes", "baseline producer code hashes"),
])
def test_resigned_baseline_upstream_contract_is_not_accepted(tmp_path, field, needle):
    baseline_path, dry_path = _write_inputs(tmp_path)
    baseline = load_verified(baseline_path)
    baseline[field] = "tampered" if field == "version" else {"forged.py": "0" * 64}
    forged_path = tmp_path / "forged-baseline.json"
    write_immutable(forged_path, baseline)
    dry = load_verified(dry_path)
    dry["baseline_artifact"] = {"path": str(forged_path),
                                 "digest": load_verified(forged_path)["digest"]}
    dry["baseline_digest"] = load_verified(forged_path)["digest"]
    rebound_path = tmp_path / "rebound-dry.json"
    write_immutable(rebound_path, dry)
    with pytest.raises(D.PlanRefused, match=needle):
        D.build_plan(baseline_path=forged_path, dry_plan_path=rebound_path, created_at="now")


@pytest.mark.parametrize("field,needle", [
    ("version", "dry plan producer version"),
    ("producer", "dry plan producer contract"),
    ("operations_sha256", "operations digest"),
])
def test_resigned_dry_plan_upstream_contract_is_not_accepted(tmp_path, field, needle):
    baseline_path, dry_path = _write_inputs(tmp_path)
    dry = load_verified(dry_path)
    if field == "version":
        dry[field] = "tampered"
    elif field == "producer":
        dry[field]["code_hashes"] = {"forged.py": "0" * 64}
    else:
        dry[field] = "0" * 64
    forged_path = tmp_path / "forged-dry.json"
    write_immutable(forged_path, dry)
    with pytest.raises(D.PlanRefused, match=needle):
        D.build_plan(baseline_path=baseline_path, dry_plan_path=forged_path, created_at="now")


def test_exact_reconstruction_rejects_a_resigned_outcome_tamper(tmp_path):
    plan = _plan(tmp_path)
    forged = deepcopy(plan)
    forged["dispositions"][1]["outcome"] = "document_only"
    forged["dispositions"][1]["reason_code"] = D.REASON_CODES["document_only"]
    forged["accounting"]["by_outcome"]["meeting_linked"] -= 1
    forged["accounting"]["by_outcome"]["document_only"] += 1
    forged["digest"] = D.canonical_sha256({key: value for key, value in forged.items()
                                            if key != "digest"})
    assert D.validate_plan(forged) == ["plan differs from the exact bound-artifact reconstruction"]


def test_authoritative_held_population_is_exactly_meeting_linked():
    plan = D.build_plan(baseline_path=REAL_BASELINE, dry_plan_path=REAL_DRY_PLAN,
                        created_at="2026-09-15T02:00:00Z")
    assert plan["accounting"]["population"] == 36_635
    assert plan["accounting"]["by_outcome"] == {
        "item_linked": 0, "meeting_linked": 36_635, "document_only": 0,
        "source_gap": 0, "invalid_span": 0,
    }
    assert plan["accounting"]["reconciles"] is True
    assert all("agenda_item_db_id" not in row for row in plan["dispositions"])
    assert D.validate_plan(plan) == []


def test_module_has_no_apply_or_database_surface():
    source = Path(D.__file__).read_text(encoding="utf-8")
    assert not hasattr(D, "apply")
    assert not hasattr(D, "engine")
    assert "sqlalchemy" not in source
    assert "get_engine" not in source
