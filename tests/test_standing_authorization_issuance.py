#!/usr/bin/env python3
"""Tests for standing-authorization issuance: fail-closed, no broadening.

The recorder must revalidate the human's approval independently and must refuse
to issue while the production interlock cannot distinguish an upsert from a
delete or a schema request. Every one of these tests is a refusal path.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.db.sync_runtime import select_sync_tables
from scripts.ops import issue_standing_authorization as issuer
from scripts.ops import standing_authorization_proposal as proposal_mod

NOW = datetime(2026, 9, 23, 23, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
REAL_PROPOSAL = issuer.PROPOSAL_PATH
#: The approval the human gave for the PREVIOUS proposal digest. It was superseded
#: when execution-mode binding changed the bound code hashes, and must never be
#: inherited by the regenerated proposal.
SUPERSEDED_APPROVAL = (ROOT / "data" / "standing-approvals" / "superseded-f3d338fc" /
                       "OP-RECON-standing-f3d338fcad5cef3f.approval.json")


def _proposal(**overrides):
    proposal = proposal_mod.build_proposal(
        code_paths=proposal_mod.DEFAULT_CODE_PATHS,
        rollback_owner="Pete Mains",
        now=NOW,
    )
    proposal.update(overrides)
    if "digest" not in overrides:
        proposal["digest"] = proposal_mod.proposal_digest(proposal)
    return proposal


def _record(proposal, **overrides):
    record = {
        "kind": "standing-authorization-approval-record",
        "schema": "poliscopic.standing-authorization-approval/v1",
        "proposal_digest": proposal["digest"],
        "operation": proposal["operation"],
        "entry_point": proposal["entry_point"],
        "target": proposal["target"],
        "execution_mode": proposal["execution_mode"],
        "validity_days": proposal["validity_days"],
        "max_uses": proposal["max_uses"],
        "scope": list(proposal["scope"]),
        "code_hashes": dict(proposal["code_hashes"]),
        "gate_ids": [gate["id"] for gate in proposal["mandatory_gates"]],
        "reviewer": "Peter Mains",
        "approved_at": NOW.isoformat(),
        "acknowledged_recurring_upserts_only_while_gates_pass": True,
        "executed": False,
        "authorizes_operation": False,
    }
    record.update(overrides)
    record["record_digest"] = issuer.record_digest(record)
    return record


def _write(tmp_path, proposal=None, record=None):
    proposal_path = tmp_path / "proposal.json"
    approval_path = tmp_path / "approval.json"
    if proposal is not None:
        proposal_path.write_text(json.dumps(proposal))
    if record is not None:
        approval_path.write_text(json.dumps(record))
    return proposal_path, approval_path


def _check(tmp_path, proposal, record, **kwargs):
    proposal_path, approval_path = _write(tmp_path, proposal, record)
    _, _, refusal = issuer.revalidate(proposal_path=proposal_path,
                                      approval_path=approval_path, now=NOW, **kwargs)
    return refusal


# ── the happy path (revalidation only — no write) ─────────────────────────


def test_independently_valid_approval_revalidates(tmp_path):
    proposal = _proposal()
    refusal = _check(tmp_path, proposal, _record(proposal))
    assert refusal == {}


#: The plan digest of the DELIBERATELY issued standing authorization. The three
#: tests below were written before issuance; they are updated deliberately (manager
#: decision 2026-09-24) from absence assumptions to post-issuance invariants.
ISSUED_STANDING_PLAN_DIGEST = (
    "33647419cf146a793770097b6776a2a2544aae0eed837916a733ea26914103e5")


def test_regenerated_proposal_still_refuses_for_want_of_a_FRESH_approval(tmp_path):
    """A proposal whose digest nobody approved must still refuse — and the refusal
    names THAT digest's record path, so a regenerated proposal can never inherit an
    earlier approval.

    Tested against an ISOLATED proposal so the assertion holds independently of
    whether the live standing authorization exists.
    """
    proposal = _proposal()
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal))
    missing = tmp_path / issuer.approval_path_for(proposal["digest"]).name
    _, _, refusal = issuer.revalidate(proposal_path=proposal_path,
                                      approval_path=missing, now=NOW)
    assert refusal["code"] == issuer.APPROVAL_MISSING
    assert missing.name in refusal["reason"]
    assert not missing.exists()


def test_superseded_approval_is_not_inherited(tmp_path):
    """Placing the OLD record at the NEW path must still be refused.

    This is the "do not inherit approval" requirement, tested adversarially.
    """
    proposal = _proposal()
    if not SUPERSEDED_APPROVAL.is_file():
        pytest.skip("superseded approval not present on this checkout")
    # Copy the superseded record to where the new proposal's approval would live.
    target = tmp_path / issuer.approval_path_for(proposal["digest"]).name
    target.write_text(SUPERSEDED_APPROVAL.read_text())
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal))
    _, _, refusal = issuer.revalidate(proposal_path=proposal_path,
                                      approval_path=target, now=NOW)
    assert refusal["code"] == issuer.APPROVAL_NOT_BOUND


def test_superseded_artifacts_are_preserved_byte_identically():
    """Supersession must preserve evidence, not delete it."""
    if not SUPERSEDED_APPROVAL.is_file():
        pytest.skip("superseded approval not present on this checkout")
    record = json.loads(SUPERSEDED_APPROVAL.read_text())
    assert record["proposal_digest"] == (
        "f3d338fcad5cef3f21fd03698ef0834ec69d6f39d181c18b0174bd92ff926689")
    assert record["record_digest"] == issuer.record_digest(record), (
        "the superseded record's own digest must still verify: bytes preserved")
    markers = list(SUPERSEDED_APPROVAL.parent.glob("SUPERSEDED.txt"))
    assert markers, "a supersession marker must accompany the preserved record"


# ── refusals: the artifacts ───────────────────────────────────────────────


def test_missing_proposal_refuses(tmp_path):
    _, approval_path = _write(tmp_path, None, None)
    _, _, refusal = issuer.revalidate(proposal_path=tmp_path / "absent.json",
                                      approval_path=approval_path, now=NOW)
    assert refusal["code"] == issuer.PROPOSAL_MISSING


def test_tampered_proposal_refuses(tmp_path):
    proposal = _proposal()
    proposal["max_uses"] = 100000  # widened after approval, digest not recomputed
    assert _check(tmp_path, proposal, _record(_proposal()))["code"] == \
        issuer.PROPOSAL_TAMPERED


@pytest.mark.parametrize("field,value", [
    ("executable", True),
    ("approver", "someone"),
    ("authorization", {"mode": "standing"}),
])
def test_authorization_shaped_proposal_refuses(tmp_path, field, value):
    proposal = _proposal(**{field: value})
    assert _check(tmp_path, proposal, _record(proposal))["code"] == \
        issuer.PROPOSAL_EXECUTABLE


def test_missing_approval_record_refuses(tmp_path):
    _, _, refusal = issuer.revalidate(proposal_path=tmp_path / "absent.json",
                                      approval_path=tmp_path / "absent2.json", now=NOW)
    assert refusal["code"] == issuer.PROPOSAL_MISSING


def test_tampered_approval_record_refuses(tmp_path):
    proposal = _proposal()
    record = _record(proposal)
    record["max_uses"] = 100000  # digest NOT recomputed
    assert _check(tmp_path, proposal, record)["code"] == issuer.APPROVAL_TAMPERED


def test_record_not_binding_proposal_refuses(tmp_path):
    proposal = _proposal()
    record = _record(proposal, proposal_digest="0" * 64)
    assert _check(tmp_path, proposal, record)["code"] == issuer.APPROVAL_NOT_BOUND


@pytest.mark.parametrize("field,value", [
    ("executed", True),
    ("authorizes_operation", True),
])
def test_record_claiming_execution_refuses(tmp_path, field, value):
    proposal = _proposal()
    assert _check(tmp_path, proposal, _record(proposal, **{field: value}))["code"] == \
        issuer.APPROVAL_EXECUTED


def test_unnamed_reviewer_refuses(tmp_path):
    proposal = _proposal()
    assert _check(tmp_path, proposal, _record(proposal, reviewer=""))["code"] == \
        issuer.APPROVAL_NO_REVIEWER


def test_missing_acknowledgement_refuses(tmp_path):
    proposal = _proposal()
    record = _record(proposal,
                     acknowledged_recurring_upserts_only_while_gates_pass=False)
    assert _check(tmp_path, proposal, record)["code"] == issuer.APPROVAL_NOT_ACKNOWLEDGED


# ── refusals: the approved terms must not drift ───────────────────────────


def test_scope_drift_refuses(tmp_path):
    proposal = _proposal()
    record = _record(proposal, scope=list(proposal["scope"])[:-1])
    assert _check(tmp_path, proposal, record)["code"] == issuer.SCOPE_MISMATCH


def test_gate_set_drift_refuses(tmp_path):
    proposal = _proposal()
    record = _record(proposal, gate_ids=[g["id"] for g in proposal["mandatory_gates"]][:-1])
    assert _check(tmp_path, proposal, record)["code"] == issuer.GATE_MISMATCH


@pytest.mark.parametrize("field,value", [("max_uses", 999), ("validity_days", 30)])
def test_limit_drift_refuses(tmp_path, field, value):
    proposal = _proposal()
    assert _check(tmp_path, proposal, _record(proposal, **{field: value}))["code"] == \
        issuer.LIMIT_MISMATCH


def test_code_hash_drift_refuses(tmp_path):
    proposal = _proposal()
    drifted = dict(proposal["code_hashes"])
    drifted[sorted(drifted)[0]] = "0" * 64
    record = _record(proposal, code_hashes=drifted)
    assert _check(tmp_path, proposal, record)["code"] == issuer.CODE_DRIFT


def test_bound_code_changed_on_disk_refuses(tmp_path, monkeypatch):
    """Nothing may have changed since the human approved."""
    proposal = _proposal()
    record = _record(proposal)
    monkeypatch.setattr(issuer, "code_hashes",
                        lambda paths: {p: "f" * 64 for p in paths})
    assert _check(tmp_path, proposal, record)["code"] == issuer.CODE_DRIFT


def test_expired_proposal_window_refuses(tmp_path):
    proposal = _proposal(not_before=(NOW - timedelta(days=400)).isoformat(),
                         not_after=(NOW - timedelta(days=35)).isoformat())
    record = _record(proposal)
    assert _check(tmp_path, proposal, record)["code"] == issuer.WINDOW_EXPIRED


def test_scope_no_longer_the_routine_scope_refuses(tmp_path):
    proposal = _proposal(scope=["entities", "meetings"])
    proposal["digest"] = proposal_mod.proposal_digest(proposal)
    record = _record(proposal)
    assert _check(tmp_path, proposal, record)["code"] == issuer.SCOPE_MISMATCH


# ── the plan binds exactly what was approved ──────────────────────────────


def test_standing_plan_binds_the_approved_envelope():
    proposal = _proposal()
    plan = issuer.build_standing_plan(proposal)
    assert issuer.verify_plan_binding(plan, proposal) == {}
    assert plan["operation"] == "OP-RECON"
    assert plan["entry_point"] == "scripts/db/sync_prod.py"
    assert plan["target"] == "production"
    assert sorted(plan["scope"]) == sorted(select_sync_tables(None))
    assert len(plan["scope"]) == 24
    assert plan["code_hashes"] == proposal["code_hashes"]
    # The canonical builder serializes to whole seconds; the windows are the same
    # approved instants to within that serializer's precision.
    assert issuer.same_instant(plan["not_before"], proposal["not_before"])
    assert issuer.same_instant(plan["not_after"], proposal["not_after"])
    assert len(plan["scope"]) == 24


@pytest.mark.parametrize("field,value", [
    ("target", "staging"),
    ("entry_point", "scripts/db/other.py"),
    ("operation", "OP-REPAIR"),
])
def test_plan_binding_mismatch_refuses(field, value):
    proposal = _proposal()
    plan = issuer.build_standing_plan(proposal)
    plan[field] = value
    plan["digest"] = issuer.plan_digest(plan)
    assert issuer.verify_plan_binding(plan, proposal)["code"] == \
        issuer.PLAN_BINDING_MISMATCH


def test_plan_scope_mismatch_refuses():
    proposal = _proposal()
    plan = issuer.build_standing_plan(proposal)
    plan["scope"] = plan["scope"][:-1]
    plan["digest"] = issuer.plan_digest(plan)
    assert issuer.verify_plan_binding(plan, proposal)["code"] == \
        issuer.PLAN_BINDING_MISMATCH


def test_plan_window_mismatch_refuses():
    proposal = _proposal()
    plan = issuer.build_standing_plan(proposal)
    plan["not_after"] = (datetime.fromisoformat(plan["not_after"])
                         + timedelta(days=1)).isoformat()
    plan["digest"] = issuer.plan_digest(plan)
    assert issuer.verify_plan_binding(plan, proposal)["code"] == \
        issuer.PLAN_BINDING_MISMATCH


# ── the mode gap, and the fail-closed consequence ─────────────────────────


def test_upsert_and_reconcile_declare_the_same_scope():
    """Root cause of the gap: mode is not visible in the interlock request."""
    routine = select_sync_tables(None)
    # `_interlock_verdict(False, None)` — the call a plain --reconcile makes —
    # declares exactly this, and so does a plain --schema-only run.
    assert sorted(issuer.build_standing_plan(_proposal())["scope"]) == sorted(routine)


def test_interlock_request_now_IS_mode_aware():
    """The tripwire from the previous revision has flipped: mode is bound.

    The earlier revision asserted `"mode" not in signature` as a documented gap.
    That gap is now closed — this test records the flip, and the standing package
    was regenerated afterwards (new digest) because bound code changed.
    """
    from production_interlock import check

    assert "mode" in inspect.signature(check).parameters
    from scripts.ops.production_interlock_guard import require_production_interlock

    assert "mode" in inspect.signature(require_production_interlock).parameters


def test_issuer_no_longer_refuses_for_mode():
    """MODE_UNBOUND must be gone: the mode is bindable now."""
    result = issuer.issue(dry_run=True)
    assert result["code"] != issuer.MODE_UNBOUND


def test_issuer_still_fails_closed_in_apply_mode(tmp_path, monkeypatch):
    """Apply mode must never overwrite or re-record an existing authorization.

    UPDATED DELIBERATELY (manager decision 2026-09-24): the standing authorization
    HAS been issued, so the fail-closed assertion is now the SPECIFIC refusal
    ALREADY_ISSUED rather than "no file exists". A refusal that wrote nothing and
    left the existing artifact intact is a STRONGER property than absence.
    """
    import scripts.ops.operation_authorization as authorization_mod

    proposal = _proposal()
    proposal_path, approval_path = _write(tmp_path, proposal, _record(proposal))
    directory = tmp_path / "release" / issuer.OPERATION_ID
    directory.mkdir(parents=True)
    authorization_path = directory / "authorization.json"
    authorization_path.write_bytes(b'{"existing":true}\n')
    before = authorization_path.read_bytes()
    verbatim = tmp_path / "verbatim.txt"
    verbatim.write_text("I approve this bounded standing authorization.")
    monkeypatch.setattr(authorization_mod, "operation_dir", lambda *_args: directory)

    result = issuer.issue(
        proposal_path=proposal_path,
        approval_path=approval_path,
        verbatim_file=verbatim,
        now=NOW,
        dry_run=False)
    assert result["status"] == "REFUSED"
    assert result["code"] == issuer.ALREADY_ISSUED
    # the existing authorization was not rewritten or replaced
    assert authorization_path.read_bytes() == before


def test_exactly_one_authorization_exists_for_the_approved_plan_and_is_unused():
    """POST-ISSUANCE INVARIANT (replaces the pre-issuance absence check).

    Previously this asserted that NO authorization existed. That deliberately
    stopped being true when the standing authorization was issued on the user's
    instruction, which is exactly what the old message anticipated:
    "an authorization was issued; this test must be updated deliberately".
    """
    from scripts.ops.operation_authorization import count_uses, operation_dir

    directory = operation_dir("OP-RECON", issuer.OPERATION_ID)
    authorization_path = directory / "authorization.json"
    plan_path = directory / "plan.json"
    if not authorization_path.is_file() or not plan_path.is_file():
        pytest.skip("live standing-authorization artifacts are operational state")

    plan = json.loads(plan_path.read_text())
    authorization = json.loads(authorization_path.read_text())

    # exactly the approved plan, and the authorization binds it
    assert plan["digest"] == ISSUED_STANDING_PLAN_DIGEST
    assert authorization["plan_digest"] == plan["digest"]
    assert plan["operation"] == authorization["operation"] == "OP-RECON"
    assert plan["target"] == authorization["target"] == "production"
    assert authorization["source"] == "human"
    assert authorization["author"] == "Peter Mains"
    assert authorization["mode"] == "standing"
    assert authorization["max_uses"] == 400
    assert authorization["verbatim_approval"].strip()

    # unused before execution
    assert count_uses(issuer.OPERATION_ID) == 0


def test_plan_would_bind_the_approved_execution_mode():
    """The plan the recorder would write must carry the approved mode."""
    proposal = _proposal()
    plan = issuer.build_standing_plan(proposal)
    assert plan["mode"] == proposal["mode"] == "upsert"
    assert issuer.verify_plan_binding(plan, proposal) == {}


def test_recorder_never_hand_computes_an_authorization_digest():
    """Issuance must go through the canonical machinery, not a local digest."""
    source = Path(issuer.__file__).read_text()
    assert "record_authorization" in source
    assert "authorization_digest" not in source
    assert "_write_exclusive" not in source  # uses the canonical writer
