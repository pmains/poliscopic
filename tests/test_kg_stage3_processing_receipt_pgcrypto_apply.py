from _kg_stage3_processing_fixtures import TARGET
from scripts.kg import stage3_processing_receipt_apply_packet as packet
from scripts.kg import stage3_processing_receipt_pgcrypto_apply as pgcrypto


def test_extension_plan_is_bound_to_exact_reviewed_artifacts():
    processing = {"digest": packet.CURRENT_PLAN_DIGEST, "target": TARGET}
    design = {"digest": packet.CURRENT_DESIGN_PACKET_DIGEST}
    plan = pgcrypto.build(processing_plan=processing, design_packet=design,
                          approver="Peter Mains", writer_role="poliscopic")
    assert pgcrypto.validate(plan, processing_plan=processing, design_packet=design) == []
    assert pgcrypto.validate({**plan, "operation": "CREATE TABLE x"},
                             processing_plan=processing, design_packet=design)


def test_extension_runner_contains_only_the_authorized_schema_operation():
    source = open(pgcrypto.__file__).read()
    assert 'CREATE EXTENSION pgcrypto' in source
    assert 'CREATE TABLE' not in source.replace('"CREATE TABLE"', '')
    assert "public.digest(bytea,text)" in source
    assert "pg_advisory_xact_lock" in source
    assert "SERIALIZABLE" in source
