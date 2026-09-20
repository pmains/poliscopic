import copy
import inspect as pyinspect

import pytest

from scripts.kg.stage3_qualified_outcome_migration import (
    LEGACY_PROJECTION_SQL, REPLAY_PROJECTION_SQL, build_plan, classify_event,
    group_projection_rows, validate_plan, write_plan,
)
from scripts.kg.stage3_qualified_outcome_schema_packet import (
    STATEMENTS, build_packet, execute_packet, validate_packet,
)
from scripts.kg.stage3_qualified_outcome_apply import (
    AUTHORIZATION, ApplyRefused, apply, gate_apply, rollback, validate_backup,
)
from scripts.kg.stage3_qualified_outcome_apply_packet import (
    build_apply_packet, validate_apply_packet,
)
from scripts.kg.stage2_artifacts import compute_digest
from scripts.kg.stage2_backup_verify import (
    build_receipt as build_backup_receipt, canonical_sha256,
)


def row(**changes):
    text = "Approved with Stipulations"
    value = {"event_id": 7, "legacy_outcome": "approved_with_stipulations",
             "outcome_base": None, "outcome_qualifier": None, "document_text": text,
             "extractions": [{"id": 70, "extractor": "pattern", "extractor_version": "v1",
                              "action_verb": text, "text_offset_start": 0,
                              "text_offset_end": len(text)}]}
    value.update(changes)
    return value


def test_exact_evidence_plans_precise_pair_and_preserves_provenance():
    record = classify_event(row())
    assert record["disposition"] == "planned"
    assert (record["outcome_base"], record["outcome_qualifier"]) == (
        "approved", "with_stipulations")
    assert record["legacy_outcome"] == "approved_with_stipulations"
    assert record["evidence"][0]["extraction_id"] == 70
    assert record["evidence"][0]["evidence_text_sha256"]
    assert record["document_text_sha256"]
    assert record["evidence"][0]["context_sha256"]


def test_collapsed_conditions_value_is_narrowly_remapped_from_exact_evidence():
    record = classify_event(row(legacy_outcome="approved_with_conditions"))
    assert record["disposition"] == "planned"
    assert record["reason"] == "evidence_driven_legacy_collapse_remap"
    assert record["normalized_legacy_outcome"] == "approved_with_stipulations"


def test_subject_to_requires_governed_nonempty_complement():
    text = "Approved Subject to"
    record = classify_event(row(
        legacy_outcome="approved_subject_to", document_text=text,
        extractions=[{"id": 1, "extractor": "pattern", "extractor_version": "v1",
                      "action_verb": text, "text_offset_start": 0,
                      "text_offset_end": len(text)}],
    ))
    assert record["reason"] == "no_exact_qualified_evidence"
    assert record["evidence"][0]["reason"] == "subject_to_missing_governed_complement"


@pytest.mark.parametrize("changes,reason", [
    ({"document_text": "Approved with Conditions"}, "no_exact_qualified_evidence"),
    ({"extractions": []}, "no_exact_qualified_evidence"),
    ({"outcome_base": "approved"}, "partial_existing_pair"),
    ({"outcome_base": "approved", "outcome_qualifier": "with_conditions"},
     "existing_pair_conflict"),
])
def test_ambiguity_and_existing_conflicts_quarantine(changes, reason):
    assert classify_event(row(**changes))["reason"] == reason


def test_conflicting_exact_evidence_quarantines():
    text = "Approved with Stipulations / Approved with Conditions"
    extractions = [
        {"id": 1, "extractor": "pattern", "extractor_version": "v1",
         "action_verb": "Approved with Stipulations", "text_offset_start": 0,
         "text_offset_end": 26},
        {"id": 2, "extractor": "pattern", "extractor_version": "v1",
         "action_verb": "Approved with Conditions", "text_offset_start": 29,
         "text_offset_end": len(text)},
    ]
    assert classify_event(row(document_text=text, extractions=extractions))["reason"] == (
        "conflicting_qualified_evidence")


def test_existing_exact_pair_is_replay_safe():
    record = classify_event(row(outcome_base="approved", outcome_qualifier="with_stipulations"))
    assert record["disposition"] == "replay"


def test_projection_is_pre_schema_safe_and_grouping_preserves_attestations():
    assert "NULL::text AS outcome_base" in LEGACY_PROJECTION_SQL
    assert "e.outcome_base" in REPLAY_PROJECTION_SQL
    flat = []
    for extraction in row()["extractions"]:
        flat.append({"event_id": 7, "legacy_outcome": "approved_with_stipulations",
                     "outcome_base": None, "outcome_qualifier": None,
                     "document_text": "Approved with Stipulations",
                     "extraction_id": extraction["id"], **{
                         key: extraction[key] for key in (
                             "extractor", "extractor_version", "action_verb",
                             "text_offset_start", "text_offset_end")}})
    grouped = group_projection_rows(flat)
    assert len(grouped) == 1
    assert [item["id"] for item in grouped[0]["extractions"]] == [70]


def test_projection_grouping_refuses_incoherent_event_rows():
    base = {"event_id": 7, "legacy_outcome": "approved_with_stipulations",
            "outcome_base": None, "outcome_qualifier": None, "document_text": "text",
            "extraction_id": None}
    with pytest.raises(ValueError, match="incoherent"):
        group_projection_rows([base, {**base, "document_text": "drift"}])


def test_unqualified_outcome_is_out_of_scope():
    assert classify_event(row(legacy_outcome="approved"))["disposition"] == "out_of_scope"


def test_plan_is_stable_reconciled_and_development_only():
    source = [row(), row(event_id=8, legacy_outcome="approved")]
    plan = build_plan(source,
                      target="poliscopic_dev", schema_digest="a" * 64)
    assert validate_plan(plan, source) == []
    assert plan == build_plan([row(event_id=8, legacy_outcome="approved"), row()],
                              target="poliscopic_dev", schema_digest="a" * 64)
    assert plan["accounting"] == {"out_of_scope": 1, "planned": 1}
    with pytest.raises(ValueError):
        build_plan([row()], target="production", schema_digest="a" * 64)


def test_plan_tamper_and_duplicate_ids_fail_closed():
    plan = build_plan([row()], target="poliscopic_dev", schema_digest="a" * 64)
    changed = copy.deepcopy(plan); changed["records"][0]["outcome_qualifier"] = "with_conditions"
    assert "plan digest mismatch" in validate_plan(changed)
    assert "source population digest mismatch" in validate_plan(plan, [row(document_text="drift")])
    with pytest.raises(ValueError):
        build_plan([row(), row()], target="poliscopic_dev", schema_digest="a" * 64)


def test_schema_packet_is_additive_disabled_bound_and_has_receipts():
    plan = build_plan([row()], target="poliscopic_dev", schema_digest="a" * 64)
    packet = build_packet(plan)
    assert validate_packet(packet, plan) == []
    assert any("outcome_base TEXT" in statement for statement in STATEMENTS)
    assert any("migration_receipts" in statement for statement in STATEMENTS)
    assert any("append-only" in statement for statement in STATEMENTS)
    assert packet["data_operations"] == []
    with pytest.raises(RuntimeError, match="no executable write path"):
        execute_packet()


def test_packet_tamper_is_refused():
    plan = build_plan([row()], target="poliscopic_dev", schema_digest="a" * 64)
    packet = build_packet(plan); packet["enabled"] = True
    problems = validate_packet(packet, plan)
    assert "packet digest mismatch" in problems
    assert "packet is not disabled design-only" in problems


def backup(plan):
    target = {"dialect": "postgresql", "host": "dev", "port": 5432,
              "database": "poliscopic_dev", "tier": "development"}
    counts = {"meeting_events": 37}
    baseline_body = {"kind": "kg-stage2-backup-baseline", "version": "2.0",
                     "created_at": "2026-09-20T12:00:00+00:00", "target": target,
                     "counts": counts, "counts_sha256": canonical_sha256(counts),
                     "schema_signature": {"schema_sha256": plan["schema_digest"],
                                          "agenda_items": {"digest": "i" * 64}},
                     "integrity": {"ok": 1}}
    baseline = {**baseline_body, "digest": canonical_sha256(baseline_body)}
    return build_backup_receipt(
        baseline=baseline, baseline_path="baseline.json", dump_path="dev.dump",
        dump_sha256="d" * 64, dump_started_at="2026-09-20T12:01:00+00:00",
        comparisons={"counts": True, "schema": True, "integrity": True}, problems=[],
        created_at="2026-09-20T12:02:00+00:00")


def authorities(plan):
    design = build_packet(plan)
    receipt = backup(plan)
    authorized = build_apply_packet(
        design_packet=design, plan=plan, backup_receipt_digest=compute_digest(receipt),
        code_digest=plan["code_digest"], schema_digest=plan["schema_digest"])
    return design, authorized, receipt


def test_apply_gate_requires_every_backup_target_schema_code_and_source_binding():
    source = [row()]
    plan = build_plan(source, target="poliscopic_dev", schema_digest="a" * 64,
                      code_digest="c" * 64)
    design, authorized, receipt = authorities(plan)
    assert gate_apply(design_packet=design, apply_packet=authorized,
                      plan=plan, source_rows=source,
                      backup_receipt=receipt, authorization=AUTHORIZATION,
                      target="poliscopic_dev", schema_digest="a" * 64,
                      current_code_digest="c" * 64) == []
    problems = gate_apply(design_packet=design, apply_packet=authorized,
                          plan=plan, source_rows=[row(document_text="drift")],
                          backup_receipt={}, authorization="wrong", target="production",
                          schema_digest="b" * 64, current_code_digest="x")
    assert any("source population" in problem for problem in problems)
    assert any("backup" in problem for problem in problems)
    assert any("authorization" in problem for problem in problems)
    assert any("target" in problem for problem in problems)
    assert any("schema" in problem for problem in problems)
    assert any("code" in problem for problem in problems)


def test_pre_plan_has_true_zero_write_replay_from_exact_applied_post_state():
    source = [row()]
    plan = build_plan(source, target="poliscopic_dev", schema_digest="a" * 64,
                      code_digest="c" * 64)
    design, authorized, receipt = authorities(plan)
    record = plan["records"][0]
    receipt_row = {
        "event_id": record["event_id"], "disposition": "applied",
        "legacy_outcome": record["legacy_outcome"],
        "normalized_outcome": record["normalized_legacy_outcome"],
        "outcome_base": record["outcome_base"],
        "outcome_qualifier": record["outcome_qualifier"],
        "current_outcome": record["normalized_legacy_outcome"],
        "current_base": record["outcome_base"],
        "current_qualifier": record["outcome_qualifier"],
        "evidence": {"document_text_sha256": record["document_text_sha256"],
                     "attestations": record["evidence"]},
        "current_document_text": source[0]["document_text"],
    }
    class Result:
        def __init__(self, rows): self.rows = rows
        def mappings(self): return self
        def one(self): return self.rows[0]
        def __iter__(self): return iter(self.rows)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, statement, *_args, **_kwargs):
            sql = str(statement)
            if "information_schema.columns" in sql:
                return Result([{"has_receipts": True, "has_base": True,
                                "has_qualifier": True}])
            return Result([receipt_row])
    class ReplayEngine:
        def connect(self): return Connection()
        def begin(self):
            raise AssertionError("replay must not open a transaction")
    result = apply(ReplayEngine(), design_packet=design, apply_packet=authorized,
                   plan=plan, source_rows=source,
                   backup_receipt=receipt, authorization=AUTHORIZATION,
                   target="poliscopic_dev", schema_digest="post-schema-different",
                   current_code_digest="c" * 64)
    assert result["outcome"] == "replay" and result["writes"] == 0
    receipt_row["disposition"] = "rolled_back"
    with pytest.raises(ApplyRefused, match="rolled-back"):
        apply(ReplayEngine(), design_packet=design, apply_packet=authorized,
              plan=plan, source_rows=source, backup_receipt=receipt,
              authorization=AUTHORIZATION, target="poliscopic_dev",
              schema_digest="post-schema-different", current_code_digest="c" * 64)


def test_design_packet_is_never_apply_authority():
    plan = build_plan([row()], target="poliscopic_dev", schema_digest="a" * 64,
                      code_digest="c" * 64)
    design = build_packet(plan)
    assert "not an authorized apply packet" in validate_apply_packet(
        design, design_packet=design, plan=plan)


def test_real_stage2_backup_receipt_contract_is_required():
    plan = build_plan([row()], target="poliscopic_dev", schema_digest="a" * 64,
                      code_digest="c" * 64)
    design, authorized, receipt = authorities(plan)
    assert validate_backup(receipt, plan, authorized) == []
    bad = {"kind": "restore-verified-backup", "verified": True}
    assert validate_backup(bad, plan, authorized)


def test_full_37_row_fixture_plans_every_collapsed_row():
    rows = [row(event_id=index, legacy_outcome="approved_with_conditions")
            for index in range(1, 38)]
    plan = build_plan(rows, target="poliscopic_dev", schema_digest="a" * 64,
                      code_digest="c" * 64)
    assert plan["accounting"] == {"planned": 37}
    assert len(plan["records"]) == 37


def test_normal_rollback_retains_schema_and_appends_history():
    source = pyinspect.getsource(rollback)
    assert "rolled_back" in source
    assert "INSERT INTO meeting_event_outcome_migration_receipts" in source
    assert "DROP TABLE" not in source
    assert "DROP COLUMN" not in source


def test_plan_writer_is_immutable_and_digest_bound(tmp_path):
    path = tmp_path / "qualified-plan.json"
    plan, digest = write_plan(path, [row()], target="poliscopic_dev",
                              schema_digest="a" * 64, code_digest="c" * 64)
    assert digest == plan["digest"]
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(Exception, match="already exists"):
        write_plan(path, [row()], target="poliscopic_dev",
                   schema_digest="a" * 64, code_digest="c" * 64)
