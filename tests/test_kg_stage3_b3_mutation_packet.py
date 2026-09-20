from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from scripts.kg import stage3_b3_mutation_packet as packet
from scripts.kg import stage3_b3_schema_contract as schema

BASELINE = Path("data/kg-plans/kg-stage3-b3-span-baseline-20260914T204038Z.json")
DRY_PLAN = Path("data/kg-plans/kg-stage3-b3-span-dry-plan-20260914T204038Z.json")


def built():
    return packet.build_packet(
        baseline_path=BASELINE, dry_plan_path=DRY_PLAN,
        created_at="2026-09-14T21:00:00Z")


def test_schema_is_additive_versioned_and_has_total_fk_lineage():
    value = schema.schema_contract()
    assert schema.validate_schema_contract(value) == []
    ddl = "\n".join(value["ddl"])
    assert "evidence_content_sha256" in ddl
    assert "coordinate_system = 'unicode_codepoint_half_open'" in ddl
    assert "result_document_id = supporting_doc_id" in ddl
    assert "REFERENCES meeting_events(id) ON DELETE RESTRICT" in ddl
    assert "agenda_item_db_id integer NOT NULL" in ddl


def test_packet_is_disabled_zero_operation_and_exactly_bound():
    value = built()
    assert value["enabled"] is False
    assert value["applied"] is False
    assert value["write_path"] == "absent by design"
    assert value["data_operations"] == []
    assert value["accounting"]["q3_held"] == 36635
    assert value["digest"] == packet.canonical_sha256(
        {k: v for k, v in value.items() if k != "digest"})
    assert packet.validate_packet(
        value, baseline_path=BASELINE, dry_plan_path=DRY_PLAN) == []


def test_every_safety_critical_module_is_hash_bound():
    value = built()
    assert set(value["bindings"]["code_hashes"]) == set(packet.CODE_MODULES)


@pytest.mark.parametrize("field,value", [
    ("enabled", True), ("write_path", "callable"),
    ("data_operations", [{"sql": "INSERT"}]),
])
def test_packet_tamper_fails_exact_rebuild(field, value):
    changed = deepcopy(built())
    changed[field] = value
    changed["digest"] = packet.canonical_sha256(
        {k: v for k, v in changed.items() if k != "digest"})
    problems = packet.validate_packet(
        changed, baseline_path=BASELINE, dry_plan_path=DRY_PLAN)
    assert problems


def test_code_drift_fails_exact_rebuild():
    value = deepcopy(built())
    value["bindings"]["code_hashes"][packet.CODE_MODULES[0]] = "0" * 64
    value["digest"] = packet.canonical_sha256(
        {k: v for k, v in value.items() if k != "digest"})
    problems = packet.validate_packet(
        value, baseline_path=BASELINE, dry_plan_path=DRY_PLAN)
    assert "exact canonical current rebuild" in problems[0]


def test_obsolete_q3_artifact_is_refused():
    old = Path("data/kg-plans/kg-stage3-b3-span-baseline-20260914T203907Z.json")
    with pytest.raises(packet.PacketRefused, match="obsolete"):
        packet.build_packet(
            baseline_path=old, dry_plan_path=DRY_PLAN, created_at="now")


def test_schema_preimage_refuses_existing_table_and_drift():
    assert schema.validate_schema_preimage(
        present_tables=[], expected_schema_sha256="a", current_schema_sha256="a") == []
    assert len(schema.validate_schema_preimage(
        present_tables=[schema.TABLE], expected_schema_sha256="a",
        current_schema_sha256="b")) == 2


def test_replay_is_noop_only_for_exact_empty_receipt_owned_schema():
    assert packet.replay_check(
        table_present=False, table_rows=0, schema_matches=False,
        canonical_receipt_verified=False)["status"] == "not-applied"
    assert packet.replay_check(
        table_present=True, table_rows=0, schema_matches=True,
        canonical_receipt_verified=True) == {
            "status": "no-op-already-applied", "would_write": 0}
    assert packet.replay_check(
        table_present=True, table_rows=1, schema_matches=True,
        canonical_receipt_verified=True)["status"] == "refused-drift"


def test_rollback_refuses_rows_schema_dependents_and_unverified_receipt():
    problems = packet.rollback_preflight(
        packet=built(), table_rows=1, schema_matches=False,
        dependent_objects=2, canonical_receipt_verified=False)
    assert len(problems) == 4
    assert packet.rollback_preflight(
        packet=built(), table_rows=0, schema_matches=True,
        dependent_objects=0, canonical_receipt_verified=True) == []


def test_no_executable_apply_or_database_surface_exists():
    assert packet.ENABLED is False
    assert not hasattr(packet, "apply")
    assert not hasattr(packet, "engine")
    assert not hasattr(packet, "connection")
    assert not hasattr(schema, "apply_schema")
