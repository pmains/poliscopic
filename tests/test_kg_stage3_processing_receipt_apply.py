"""Offline adversarial gates for the disabled Stage 3 receipt apply."""

from _kg_stage3_processing_fixtures import TARGET
from scripts.kg import stage3_processing_receipt_apply as A
from scripts.kg import stage3_processing_receipt_apply_packet as P


def test_apply_authorization_is_bound_to_the_current_reviewed_artifacts():
    plan = {"digest": P.CURRENT_PLAN_DIGEST, "target": TARGET}
    design = {"digest": P.CURRENT_DESIGN_PACKET_DIGEST}
    value = P.build(plan=plan, design_packet=design, backup_receipt_path="receipt.json",
                    backup_receipt_digest="a" * 64, code_digest=A.code_digest(),
                    approver="Peter Mains", writer_role="poliscopic_writer", batch_size=200)
    assert P.validate(value, plan=plan, design_packet=design, current_code_digest=A.code_digest()) == []
    assert P.validate({**value, "plan_digest": "0" * 64}, plan=plan, design_packet=design)
    assert P.validate({**value, "enabled": False}, plan=plan, design_packet=design)


def test_enabled_runner_still_has_no_false_success_surface():
    assert A.EXECUTION_ENABLED is True
    assert "SERIALIZABLE" in A.apply_batch.__doc__ or "SERIALIZABLE" in open(A.__file__).read()
    assert "pg_advisory_xact_lock" in open(A.__file__).read()
    assert "UPDATE supporting_documents SET swept_at" not in open(A.__file__).read()
    source = open(A.__file__).read()
    assert "authorized_compensating_rollback" in source
    assert "DELETE FROM processing_receipts" not in source
    assert "SELECT receipt_body FROM processing_receipts" in source
    assert "terminal-receipt directory is required" in source
    assert "extract_entities_from_doc" in source
    assert "Callable[[Mapping" not in source
    assert "digest(bytea,text) capability is unavailable" in source
    assert "terminal_dir.is_dir()" in source


def test_cli_cannot_supply_an_alternate_database_target():
    from scripts.kg import stage3_processing_receipt_apply_run as runner

    assert "--database" not in open(runner.__file__).read()
    assert "get_engine()" in open(runner.__file__).read()
