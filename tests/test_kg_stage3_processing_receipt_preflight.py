"""Adversarial offline checks for the reusable Stage 3 receipt preflight."""

from datetime import datetime, timezone
from pathlib import Path

from _kg_stage3_processing_fixtures import TARGET
from scripts.kg import stage3_processing_receipt as receipt
from scripts.kg import stage3_processing_receipt_preflight as P


def _document(*, plan, design, packet, backup, code):
    body = {"kind": P.KIND, "version": P.VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": "read-only", "applied": False, "write_path": "absent by design",
            "plan_digest": plan["digest"], "design_packet_digest": design["digest"],
            "apply_packet_digest": packet["digest"], "code_digest": code, "target": TARGET,
            "writer_role": packet["writer_role"], "backup": backup,
            "receipt_store": {"state": "complete", "schema_sha256": "a" * 64, "objects": {}},
            "source_population": {"records": len(plan["records"]),
                                  "source_ids_sha256": receipt.canonical_sha256(
                                      [row["source_id"] for row in plan["records"]]),
                                  "identities_sha256": "b" * 64,
                                  "read_method": "server_sha256_chunked", "chunk_size": 1000},
            "validation": {"plan": "complete", "design": "complete", "authorization": "complete",
                           "backup": "complete", "source_population": "complete"}}
    return {**body, "digest": receipt.canonical_sha256(body)}


def test_fast_preflight_rejects_every_critical_binding(monkeypatch, tmp_path):
    backup_path = tmp_path / "backup.json"
    backup_path.write_text("{}")
    backup = {"path": str(backup_path.resolve()), "digest": "c" * 64, "size": 2, "mode": 0o600}
    monkeypatch.setattr(P, "_backup_binding", lambda _path: backup)
    plan = {"digest": "p" * 64, "target": {**TARGET, "redacted": "display-only", "url_class": "development"},
            "records": [{"source_id": 7, "processing_identity": ["supporting_document", 7, "0" * 64,
                                                               "native", "sweep_docs", "x"]}]}
    design, packet, code = {"digest": "q" * 64}, {"digest": "r" * 64, "writer_role": "writer"}, "s" * 64
    value = _document(plan=plan, design=design, packet=packet, backup=backup, code=code)
    assert P.validate(value, plan=plan, design_packet=design, apply_packet=packet,
                      backup_path=backup_path, current_code_digest=code) == []
    assert P.validate({**value, "code_digest": "wrong"}, plan=plan, design_packet=design,
                      apply_packet=packet, backup_path=backup_path, current_code_digest=code)
    assert P.validate({**value, "apply_packet_digest": "wrong"}, plan=plan, design_packet=design,
                      apply_packet=packet, backup_path=backup_path, current_code_digest=code)
    assert P.validate({**value, "receipt_store": {"state": "absent"}}, plan=plan,
                      design_packet=design, apply_packet=packet, backup_path=backup_path,
                      current_code_digest=code)
    assert P.validate({**value, "source_population": {**value["source_population"], "records": 0}},
                      plan=plan, design_packet=design, apply_packet=packet, backup_path=backup_path,
                      current_code_digest=code)


def test_preflight_contract_keeps_full_population_and_slice_checks_separate():
    source = open(P.__file__).read()
    assert "server_sha256_chunked" in source
    assert "planned source" in source
    apply_source = open(Path(P.__file__).with_name("stage3_processing_receipt_apply.py")).read()
    assert "preflight.schema_matches" in apply_source
    assert "_source_record(connection, record)" in apply_source
    assert "validator.validate_plan(plan" not in apply_source.split("def gate", 1)[1].split("def apply_batch", 1)[0]
