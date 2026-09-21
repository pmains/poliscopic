"""Negative tests for Stage 3 authorization safety.

These exist because a real defect shipped: a proposal was built through the
authorization builder, so it carried the authorized apply kind, `state: authorized`,
`enabled: true` and a placeholder approver.  The filename said "proposal" and the prose
said "not an authorization", and neither was enforcement — the validator accepted it.

The tests below pin the correction: a proposal is a distinct contract refused by content
at every apply-facing gate, and only a packet carrying an approver that was actually
supplied may be authorized and enabled.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from scripts.kg import stage3_processing_receipt_apply as apply
from scripts.kg import stage3_processing_receipt_apply_packet as authorization
from scripts.kg import stage3_processing_receipt_apply_proposal as proposal
from scripts.kg import stage3_processing_receipt_continue as continuation
from scripts.kg import stage3_processing_receipt_serenity_runner as serenity
from scripts.kg.stage2_artifacts import load_verified, write_immutable

CURRENT_PROPOSAL = Path("data/kg-plans/kg-stage3-processing-receipt-proposal-20260921T195816Z.json")
TARGET = {"tier": "development", "database": "poliscopic_dev"}


def _plan():
    return {"digest": authorization.CURRENT_PLAN_DIGEST, "target": dict(TARGET)}


def _design():
    return {"digest": authorization.CURRENT_DESIGN_PACKET_DIGEST}


def _record(*, plan=None, design=None):
    return proposal.build(plan=plan or _plan(), design_packet=design or _design(),
                          backup_receipt_path="data/backups/backup.json",
                          backup_receipt_digest="b" * 64, code_digest="c" * 64,
                          writer_role="poliscopic", batch_size=500)


def _authorized(approver):
    """An authorized-shaped packet bound to the current constants."""
    plan, design = _plan(), _design()
    body = {"kind": authorization.KIND, "version": authorization.VERSION,
            "state": "authorized", "enabled": True, "plan_digest": plan["digest"],
            "design_packet_digest": design["digest"], "target": dict(plan["target"]),
            "backup_receipt_path": "data/backups/backup.json",
            "backup_receipt_digest": "b" * 64, "code_digest": "c" * 64,
            "approver": approver, "writer_role": "poliscopic", "batch_size": 500,
            "contract": {"no_swept_at_rewrite": True, "receipt_history": "append_only",
                         "replay": "zero_write_exact_receipt_replay"}}
    return {**body, "digest": authorization.digest(body)}


# --------------------------------------------------------------------------- #
# The placeholder may never act as an approver
# --------------------------------------------------------------------------- #


def test_the_placeholder_approver_cannot_validate():
    problems = authorization.validate(_authorized(authorization.PROPOSAL_APPROVER_PLACEHOLDER),
                                      plan=_plan(), design_packet=_design())
    assert any("placeholder approver" in problem for problem in problems)


def test_a_pending_review_approver_cannot_validate_however_it_is_cased():
    for value in ("pending manager review", "  Pending   Manager Review  (NOT AN AUTHORIZATION)  "):
        problems = authorization.validate(_authorized(value), plan=_plan(),
                                          design_packet=_design())
        assert any("not an authorization" in problem for problem in problems), value


def test_an_ordinary_approver_still_validates():
    problems = authorization.validate(_authorized("Peter Mains"), plan=_plan(),
                                      design_packet=_design())
    assert problems == []


def test_only_a_supplied_approver_can_be_authorized_and_enabled():
    """The proposal builder has no approver parameter, so it cannot authorize anything."""
    assert "approver" not in inspect.signature(proposal.build).parameters
    record = _record()
    assert record["state"] == "proposed"
    assert record["enabled"] is False
    assert "approver" not in record


# --------------------------------------------------------------------------- #
# A proposal is refused by content, not by where it lives
# --------------------------------------------------------------------------- #


def test_a_proposal_is_refused_even_under_an_apply_style_filename(tmp_path):
    record = _record()
    apply_style = tmp_path / "kg-stage3-processing-receipt-apply-0000000000000000.json"
    write_immutable(apply_style, record)
    loaded = load_verified(apply_style)
    assert proposal.is_proposal(loaded)
    problems = authorization.validate(loaded, plan=_plan(), design_packet=_design())
    assert any("a proposal is not an authorization" in problem for problem in problems)


def test_the_shipped_proposal_artifact_refuses_as_an_authorization():
    record = load_verified(CURRENT_PROPOSAL)
    # Bind the checks to the artifact's own declared bindings rather than a stand-in.
    plan = {"digest": record["plan_digest"], "target": dict(record["target"])}
    design = {"digest": record["design_packet_digest"]}
    assert proposal.is_proposal(record)
    assert proposal.validate_proposal(record, plan=plan, design_packet=design) == []
    refused = authorization.proposal_problems(record)
    assert len(refused) >= 3
    assert authorization.validate(record, plan=plan, design_packet=design)


# --------------------------------------------------------------------------- #
# Every gate refuses before it can touch a database
# --------------------------------------------------------------------------- #


def test_apply_gate_refuses_a_proposal_before_any_db_work():
    problems = apply.gate(engine=None, plan=_plan(), design_packet=_design(),
                          apply_packet=_record(), backup_path=None,
                          authorization_token=apply.AUTHORIZATION_TOKEN,
                          preflight_document=None)
    assert problems and any("proposal" in problem for problem in problems)


def test_continuation_refuses_a_proposal_before_any_db_work(tmp_path):
    with pytest.raises(continuation.ContinuationRefused, match="proposal"):
        continuation.continue_batches(None, plan=_plan(), design_packet=_design(),
                                      apply_packet=_record(),
                                      backup_path=tmp_path / "b.json", token="t",
                                      terminal_dir=tmp_path, aggregate_out=tmp_path / "a.json",
                                      start_offset=0, max_batches=1)


def test_serenity_refuses_a_proposal_before_any_db_work(tmp_path):
    with pytest.raises(serenity.SerenityRefused, match="proposal"):
        serenity.run(None, plan=_plan(), design=_design(), packet=_record(),
                     backup=tmp_path / "b.json", token="t", terminal_dir=tmp_path,
                     preflight_dir=tmp_path, checkpoint_dir=tmp_path, seed_paths=[],
                     renewal_seconds=60, report_out=tmp_path / "r.pending")


def test_apply_batch_refuses_a_proposal_before_any_db_work(tmp_path):
    with pytest.raises(apply.ApplyRefused, match="proposal"):
        apply.apply_batch(None, plan=_plan(), design_packet=_design(),
                          apply_packet=_record(), backup_path=tmp_path / "b.json",
                          authorization_token=apply.AUTHORIZATION_TOKEN,
                          terminal_dir=tmp_path)


# --------------------------------------------------------------------------- #
# The proposal contract is genuinely non-executable
# --------------------------------------------------------------------------- #


def test_proposal_declares_a_non_executable_contract():
    record = _record()
    assert record["kind"] == proposal.KIND
    assert record["kind"] != authorization.KIND
    assert record["state"] == proposal.STATE
    for field in ("enabled", "applied", "executable"):
        assert record[field] is False
    assert record["proposed"] is True
    assert record["authorization_required"] is True
    assert record["write_path"] == "absent by design"


def test_proposal_validator_refuses_a_tampered_proposal():
    record = dict(_record(), enabled=True)
    record["digest"] = proposal.digest(record)
    problems = proposal.validate_proposal(record, plan=_plan(), design_packet=_design())
    assert any("enabled" in problem for problem in problems)


def test_proposal_validator_refuses_a_proposal_carrying_an_approver():
    record = dict(_record(), approver="Peter Mains")
    record["digest"] = proposal.digest(record)
    problems = proposal.validate_proposal(record, plan=_plan(), design_packet=_design())
    assert any("no approver field" in problem for problem in problems)


def test_proposal_bindings_must_match_the_supplied_plan_and_design():
    record = _record()
    assert proposal.validate_proposal(record, plan={"digest": "0" * 64, "target": dict(TARGET)},
                                      design_packet=_design())
    assert proposal.validate_proposal(record, plan=_plan(),
                                      design_packet={"digest": "0" * 64})
