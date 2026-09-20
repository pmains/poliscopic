from pathlib import Path

from _kg_stage3_processing_fixtures import TARGET
from scripts.kg import stage3_processing_receipt_continue as C
from scripts.kg.stage2_artifacts import write_immutable


def _terminal(*, offset=0, selected=100):
    body = {"kind": "kg-stage3-processing-receipt-apply-terminal", "version": "1.0",
            "authorized_packet_digest": "a" * 64, "plan_digest": "b" * 64, "offset": offset,
            "selected": selected, "success": selected, "failed": 0, "held": 0, "replay": 0,
            "swept_at_updates": 0, "outcome": "applied"}
    body["digest"] = C.apply.receipt.canonical_sha256(body)
    return body


def test_terminal_validator_rejects_bad_accounting_and_swept_updates():
    packet, plan = {"digest": "a" * 64}, {"digest": "b" * 64, "target": TARGET}
    assert C._valid_terminal(_terminal(), packet=packet, plan=plan, offset=0, selected=100) == []
    assert C._valid_terminal({**_terminal(), "swept_at_updates": 1}, packet=packet, plan=plan,
                             offset=0, selected=100)
    assert C._valid_terminal({**_terminal(), "success": 99}, packet=packet, plan=plan,
                             offset=0, selected=100)


def test_existing_terminal_is_verified_and_duplicate_offsets_refuse(tmp_path):
    packet, plan = {"digest": "a" * 64}, {"digest": "b" * 64}
    path = tmp_path / f"kg-stage3-processing-receipt-apply-{_terminal()['digest']}.json"
    write_immutable(path, _terminal())
    assert C._existing(tmp_path, packet=packet, plan=plan, offset=0, selected=100)["offset"] == 0
    another = _terminal()
    another["digest"] = "c" * 64
    # Artifact digest is validated before semantic checks; a forged duplicate cannot be skipped.
    (tmp_path / "kg-stage3-processing-receipt-apply-forged.json").write_text("{}")
    try:
        C._existing(tmp_path, packet=packet, plan=plan, offset=0, selected=100)
    except C.ContinuationRefused:
        pass
    else:
        raise AssertionError("corrupt terminal must refuse continuation")


def test_prior_terminal_requires_same_target_plan_backup_and_batch_contract(tmp_path):
    terminal = _terminal()
    path = tmp_path / "prior.json"
    write_immutable(path, terminal)
    old = {"digest": "a" * 64, "target": TARGET, "plan_digest": "b" * 64,
           "backup_receipt_digest": "c" * 64, "batch_size": 100}
    new = dict(old)
    assert C._prior_terminal(path, prior_packet=old, packet=new, plan={"digest": "b" * 64},
                             selected=100)["offset"] == 0
    assert_raises = False
    try:
        C._prior_terminal(path, prior_packet=old, packet={**new, "batch_size": 200},
                          plan={"digest": "b" * 64}, selected=100)
    except C.ContinuationRefused:
        assert_raises = True
    assert assert_raises


def test_continuation_driver_is_serial_batch_only_and_never_sweeps():
    source = open(C.__file__).read()
    assert "apply.apply_batch" in source
    assert "load_verified(terminal" in source
    assert "more than one terminal receipt" in source
    assert "terminal_failure" in source
    assert "prior terminal cannot be imported" in source
    assert "UPDATE supporting_documents SET swept_at" not in source
