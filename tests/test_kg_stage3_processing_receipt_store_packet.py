"""Adversarial tests for the design-only receipt-store packet: reconstruction, rollback
ownership, the backup contract, and the disabled gates.

Bounded by construction: a small synthetic plan from the shared fixtures stands in for
the authoritative plan, so no test re-parses 43 MB.  Nothing here creates a database
object or enables an apply.
"""

import json
import stat
from pathlib import Path

import pytest
from _kg_stage3_processing_fixtures import (
    TARGET, backup_fixture, bounded_plan, now_after, packet_for, resign_plan)
from scripts.kg import stage3_processing_receipt_store_backup as backup_mod
from scripts.kg import stage3_processing_receipt_store_packet as P
from scripts.kg import stage3_processing_receipt_store_schema as S
from scripts.kg.stage2_artifacts import is_obsolete, write_immutable

SUPERSEDED_PLAN = Path("data/kg-plans/kg-stage3-processing-dry-plan-20260914T224845Z.json")


def test_a_built_packet_is_disabled_and_validates_by_reconstruction(tmp_path):
    value = packet_for(tmp_path)
    assert value["kind"] == P.PACKET_KIND and value["enabled"] is False
    assert value["mode"] == "design-only" and value["applied"] is False
    assert value["write_path"] == "absent by design" and value["data_operations"] == []
    assert set(value) == set(P.PACKET_KEYS)
    assert value["writer_supplied_columns"] == ["receipt_body"]
    assert value["generated_columns"] == list(S.DERIVED_FROM_BODY)
    assert [item["phase"] for item in value["migration_order"]] == list(S.MIGRATION_PHASES)
    assert value["rollback"]["statements"] == list(S.ROLLBACK_DDL)
    assert value["rollback"]["owned_objects"] == list(S.OWNED_OBJECTS)
    assert value["runner_interface"]["enabled"] is False
    assert value["accounting"]["data_operations"] == 0
    assert value["backup_requirement"]["freshness_seconds"] == backup_mod.MAX_BACKUP_AGE_SECONDS
    assert P.validate_packet(value) == []


def test_validate_packet_refuses_top_level_key_set_tampering(tmp_path):
    value = packet_for(tmp_path)
    extra = {**value, "extra_block": 1}
    assert any("top-level key set" in problem for problem in P.validate_packet(extra))
    trimmed = {key: item for key, item in value.items() if key != "transaction_contract"}
    assert any("top-level key set" in problem for problem in P.validate_packet(trimmed))


def test_validate_packet_refuses_re_signed_block_tampering(tmp_path):
    """Every security-critical block is reconstructed, so re-signing does not help."""
    value = packet_for(tmp_path)
    cases = (
        lambda item: item.__setitem__("schema_operations", list(item["schema_operations"])[:-1]),
        lambda item: item.__setitem__("migration_order", list(item["migration_order"])[:-1]),
        lambda item: item["transaction_contract"].__setitem__("isolation", "READ COMMITTED"),
        lambda item: item["backup_requirement"].__setitem__("freshness_seconds", 999),
        lambda item: item["target"].__setitem__("database", "poliscopic"),
        lambda item: item["bindings"].__setitem__("plan_digest", "0" * 64),
        lambda item: item["bindings"]["selection_snapshot"].__setitem__(
            "identity_sha256", "0" * 64),
        lambda item: item["bindings"]["receipts"].__setitem__("count", 7),
        lambda item: item["bindings"]["reference_schema"].__setitem__("x", "y"),
        lambda item: item["runner_interface"].__setitem__("enabled", True),
        lambda item: item["rollback"].__setitem__("requires", []),
        lambda item: item["schema_contract"].__setitem__("ddl_sha256", "0" * 64),
        lambda item: item.__setitem__("data_operations", [{"sql": "INSERT"}]),
        lambda item: item["accounting"].__setitem__("data_operations", 1),
        lambda item: item.__setitem__("enabled", True),
        lambda item: item["approval_boundary"].__setitem__("mutations_proposed", 1),
    )
    for mutate in cases:
        tampered = json.loads(json.dumps(value))
        mutate(tampered)
        resign_plan(tampered)
        problems = P.validate_packet(tampered)
        assert problems, "a re-signed tampered packet was accepted"


def test_rollback_cannot_be_re_signed_into_arbitrary_drops(tmp_path):
    value = packet_for(tmp_path)
    hostile = json.loads(json.dumps(value))
    hostile["rollback"]["statements"] = ["DROP TABLE supporting_documents",
                                         "DROP TABLE agenda_items"]
    resign_plan(hostile)
    problems = P.validate_packet(hostile)
    assert any("rollback statements are not the canonical rollback DDL" in problem
               for problem in problems)
    assert any("rollback must not touch" in problem for problem in problems)
    foreign_owner = json.loads(json.dumps(value))
    foreign_owner["rollback"]["owned_objects"] = [S.TABLE, "supporting_documents"]
    resign_plan(foreign_owner)
    assert any("rollback claims an object it does not own" in problem
               for problem in P.validate_packet(foreign_owner))
    missing_owner = json.loads(json.dumps(value))
    missing_owner["rollback"]["owned_objects"] = [S.TABLE]
    missing_owner["rollback"]["statements"] = list(S.ROLLBACK_DDL)
    resign_plan(missing_owner)
    assert any("owned objects are not exactly" in problem
               for problem in P.validate_packet(missing_owner))


def test_the_packet_refuses_a_superseded_or_foreign_plan(tmp_path):
    with pytest.raises(P.PacketRefused, match="obsolete"):
        packet_for(tmp_path, plan=SUPERSEDED_PLAN)
    foreign = tmp_path / "not-a-plan.json"
    write_immutable(foreign, {"kind": "something-else"})
    with pytest.raises(P.PacketRefused):
        packet_for(tmp_path, plan=foreign)


def test_backup_contract_accepts_only_a_canonical_fresh_verified_backup(tmp_path):
    value = packet_for(tmp_path)
    target = dict(value["target"])
    path, _receipt = backup_fixture(tmp_path)
    assert backup_mod.backup_problems(path, target=target, now=now_after()) == []
    assert P.backup_problems(path, target=target, now=now_after()) == []
    assert backup_mod.backup_problems(None, target=target) == \
        ["a backup receipt path is required"]
    assert any("absent" in problem for problem in backup_mod.backup_problems(
        tmp_path / "missing.json", target=target))
    stale, _ = backup_fixture(tmp_path / "stale")
    assert any("stale" in problem for problem in backup_mod.backup_problems(
        stale, target=target, now=now_after(hours=48)))
    assert any("future" in problem for problem in backup_mod.backup_problems(
        stale, target=target, now=now_after(hours=-1)))
    mismatched, _ = backup_fixture(tmp_path / "digest", digest="0" * 64)
    assert any("dump digest does not match" in problem for problem in
               backup_mod.backup_problems(mismatched, target=target, now=now_after()))
    other_target, _ = backup_fixture(tmp_path / "other", database="poliscopic_dev_other")
    assert any("differs from the apply target" in problem for problem in
               backup_mod.backup_problems(other_target, target=target, now=now_after()))


def test_backup_target_and_baseline_binding_are_exact(tmp_path):
    value = packet_for(tmp_path)
    target = dict(value["target"])
    no_restore, _ = backup_fixture(tmp_path / "norestore", restore=False)
    assert any("does not prove a clean restore" in problem for problem in
               backup_mod.backup_problems(no_restore, target=target, now=now_after()))
    no_baseline, _ = backup_fixture(tmp_path / "nobaseline", with_baseline=False)
    assert any("baseline" in problem for problem in
               backup_mod.backup_problems(no_baseline, target=target, now=now_after()))
    schema_mismatch, receipt = backup_fixture(tmp_path / "schema")
    baseline_path = Path(str(receipt["stage2_verification"]["baseline_path"]))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["schema_signature"]["schema_sha256"] = "c" * 64
    baseline.pop("digest")
    baseline["digest"] = backup_mod.backup_verify.canonical_sha256(baseline)
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    problems = backup_mod.backup_problems(schema_mismatch, target=target, now=now_after())
    assert any("failed verification" in problem or "another baseline digest" in problem
               for problem in problems)
    assert backup_mod.baseline_binding_problems({"stage2_verification": {}}) == \
        ["the bound backup baseline is absent"]


def test_backup_refuses_a_baseline_for_a_different_target_even_when_receipt_target_is_rebound(
        tmp_path):
    """The receipt target and its verified baseline must describe one database."""
    path, _receipt = backup_fixture(tmp_path / "cross-target",
                                    database="poliscopic_dev_other")
    rebound = json.loads(path.read_text(encoding="utf-8"))
    # Re-emitting through the write-once artifact helper models a forged/re-signed
    # receipt whose top-level target was changed while the bound baseline was not.
    rebound["target"] = dict(TARGET)
    rebound_path = tmp_path / "cross-target-rebound.json"
    write_immutable(rebound_path, rebound)
    problems = backup_mod.backup_problems(
        rebound_path, target=TARGET, now=now_after())
    assert any("baseline target" in problem or "baseline" in problem
               for problem in problems), problems


def test_backup_refuses_a_baseline_with_a_different_dialect(tmp_path):
    """Exact target binding includes the connection dialect, not only host/database."""
    path, _receipt = backup_fixture(tmp_path / "dialect")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    source_baseline = Path(receipt["stage2_verification"]["baseline_path"])
    baseline = json.loads(source_baseline.read_text(encoding="utf-8"))
    baseline["target"]["dialect"] = "mysql"
    baseline.pop("digest")
    baseline_digest = backup_mod.backup_verify.canonical_sha256(baseline)
    baseline["digest"] = baseline_digest
    baseline_path = tmp_path / "dialect-baseline.json"
    write_immutable(baseline_path, baseline)
    receipt["stage2_verification"]["baseline_path"] = str(baseline_path)
    receipt["stage2_verification"]["baseline_digest"] = baseline_digest
    rebound_path = tmp_path / "dialect-rebound.json"
    write_immutable(rebound_path, receipt)
    problems = backup_mod.backup_problems(
        rebound_path, target=TARGET, now=now_after())
    assert any("baseline target dialect" in problem for problem in problems), problems


def test_the_backup_receipt_must_be_a_digest_bound_artifact(tmp_path):
    value = packet_for(tmp_path)
    target = dict(value["target"])
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({"kind": P.BACKUP_RECEIPT_KIND}), encoding="utf-8")
    plain.chmod(0o600)
    assert any("failed canonical verification" in problem for problem in
               backup_mod.backup_problems(plain, target=target))
    path, _receipt = backup_fixture(tmp_path / "loose")
    loose = tmp_path / "loose.json"
    loose.write_bytes(path.read_bytes())
    loose.chmod(0o644)
    assert any("mode is not 0o600" in problem for problem in
               backup_mod.backup_problems(loose, target=target, now=now_after()))


def test_apply_gate_is_disabled_and_enumerates_every_refusal(tmp_path):
    value = packet_for(tmp_path)
    target = dict(value["target"])
    backup_path, _receipt = backup_fixture(tmp_path)
    complete = P.apply_gate(value, authorization=P.AUTHORIZATION_TOKEN, target=target,
                            packet_digest_value=value["digest"], backup_path=backup_path,
                            writer_role="poliscopic_writer", transactional=True,
                            pgcrypto_available=True, now=now_after())
    assert complete["allowed"] is False and complete["enabled"] is False
    assert complete["gate_passed"] is False
    assert complete["refusals"] == [
        "the receipt-store apply is DISABLED by design (ENABLED is False)"]
    assert complete["rows_written"] == 0 and complete["data_operations"] == 0
    assert complete["writer_role_bound"] is True

    def refusals(**overrides):
        arguments = {"authorization": P.AUTHORIZATION_TOKEN, "target": target,
                     "packet_digest_value": value["digest"], "backup_path": backup_path,
                     "writer_role": "poliscopic_writer", "transactional": True,
                     "pgcrypto_available": True, "now": now_after()}
        arguments.update(overrides)
        return P.apply_gate(value, **arguments)["refusals"]

    assert any("authorization token" in item for item in refusals(authorization="nope"))
    assert any("exact packet path and digest" in item
               for item in refusals(packet_digest_value="0" * 64))
    assert any("not the development database" in item
               for item in refusals(target={"tier": "production", "database": "poliscopic"}))
    assert any("does not match the packet target" in item
               for item in refusals(target={**target, "database": "elsewhere"}))
    assert any("writer role must" in item for item in refusals(writer_role=None))
    assert any("writer role" in item for item in refusals(writer_role="writer; DROP TABLE x"))
    assert any("pgcrypto" in item for item in refusals(pgcrypto_available=False))
    assert any("single transaction is mandatory" in item for item in refusals(transactional=False))
    assert any("a backup receipt path is required" in item
               for item in refusals(backup_path=None))
    assert any("partial application" in item
               for item in refusals(applied_statements=list(S.DDL)[:2]))
    assert P.apply_gate("not a packet", target=target)["allowed"] is False


def test_rollback_gate_requires_an_owning_apply_receipt(tmp_path):
    value = packet_for(tmp_path)
    owned = {"kind": P.APPLY_RECEIPT_KIND, "packet_digest": value["digest"],
             "statements": list(value["schema_operations"]),
             "objects_created": list(S.OWNED_OBJECTS), "approver": "Peter Mains",
             "writer_role": "poliscopic_writer"}
    clean = P.rollback_gate(value, authorization=P.AUTHORIZATION_TOKEN, applied_receipt=owned)
    assert clean["allowed"] is False and clean["statements"] == list(S.ROLLBACK_DDL)
    assert any("DISABLED" in item for item in clean["refusals"])
    assert len(clean["refusals"]) == 1, clean["refusals"]
    assert S.FOREIGN_KEY_SOURCE in owned["objects_created"]
    for name in (S.PRIMARY_KEY, S.UNIQUE_IDENTITY_DIGEST, S.INDEX_HISTORY,
                 S.INDEX_STATUS, S.INDEX_INSERTED, S.CANONICAL_DIGEST_FUNCTION):
        assert name in owned["objects_created"]
    for mutate, fragment in (
        (lambda item: item.__setitem__("packet_digest", "0" * 64),
         "does not own this packet digest"),
        (lambda item: item.__setitem__("kind", "other"), "apply receipt kind"),
        (lambda item: item.__setitem__("statements", []), "does not record this packet"),
        (lambda item: item.__setitem__("objects_created", [S.TABLE]),
         "does not own exactly the packet's objects"),
        (lambda item: item.__setitem__("approver", ""), "records no approver"),
        (lambda item: item.__setitem__("writer_role", ""), "records no bound writer role"),
    ):
        broken = json.loads(json.dumps(owned))
        mutate(broken)
        found = P.rollback_gate(value, authorization=P.AUTHORIZATION_TOKEN,
                                applied_receipt=broken)["refusals"]
        assert any(fragment in item for item in found), fragment
    assert any("not explained by the receipt" in item for item in P.rollback_gate(
        value, authorization=P.AUTHORIZATION_TOKEN, applied_receipt=owned,
        unexplained_rows=3)["refusals"])
    assert any("apply receipt is required" in item for item in P.rollback_gate(
        value, authorization=P.AUTHORIZATION_TOKEN, applied_receipt=None)["refusals"])
    hostile = json.loads(json.dumps(value))
    hostile["rollback"]["statements"] = ["DROP TABLE supporting_documents"]
    resign_plan(hostile)
    assert any("does not validate" in item for item in P.rollback_gate(
        hostile, authorization=P.AUTHORIZATION_TOKEN, applied_receipt=owned)["refusals"])


def test_there_is_no_executable_write_path(tmp_path):
    value = packet_for(tmp_path)
    with pytest.raises(P.WritePathNotImplemented):
        P.execute_packet(packet=value, engine=None, authorization=P.AUTHORIZATION_TOKEN)
    with pytest.raises(P.PacketRefused):
        P.execute_packet()
    for forbidden in ("apply", "insert_rows", "create_table", "ddl_execute", "enable"):
        assert not hasattr(P, forbidden), f"{forbidden} must not exist in the packet module"
    assert P.ENABLED is False


def test_packet_artifact_round_trips(tmp_path):
    value = packet_for(tmp_path)
    path = tmp_path / "packet.json"
    digest = write_immutable(path, value)
    assert digest == value["digest"]
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert set(reloaded) == set(P.PACKET_KEYS)
    assert P.validate_packet(reloaded) == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_generated_packet_artifact_binds_the_authoritative_plan():
    """The current design packet names one current, immutable dry plan."""
    candidates = [path for path in sorted(Path("data/kg-plans").glob(
        "kg-stage3-processing-receipt-store-packet-*.json")) if not is_obsolete(path)]
    if not candidates:
        pytest.skip("no current receipt-store packet artifact exists yet")
    document = json.loads(candidates[-1].read_text(encoding="utf-8"))
    assert document["enabled"] is False and document["applied"] is False
    assert document["data_operations"] == []
    plan_path = Path(document["bindings"]["plan"]["path"])
    assert plan_path.is_file() and not is_obsolete(plan_path)
    assert document["bindings"]["plan_digest"] == document["bindings"]["plan"]["digest"]
    assert len(document["bindings"]["plan_digest"]) == 64
    assert document["rollback"]["statements"] == list(S.ROLLBACK_DDL)
    assert document["writer_supplied_columns"] == ["receipt_body"]
    assert TARGET["tier"] == document["target"]["tier"]
