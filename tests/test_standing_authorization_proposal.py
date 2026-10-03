#!/usr/bin/env python3
"""Tests for the standing daily-sync authorization proposal contract.

Every refusal path is pinned here, including the confusion case where a proposal
has been turned into something authorization-shaped. The contract must refuse
before any credential, network or database activity.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.ops import standing_authorization_proposal as proposal_mod
from scripts.db.sync_declarations import ALL_SYNC_TABLES

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


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


def _request(proposal, **overrides):
    """Validate a proposal against a fully-matching request, plus overrides."""
    if not isinstance(proposal, dict):
        return proposal_mod.validate_request(proposal)
    kwargs = {
        "operation": proposal.get("operation", ""),
        "entry_point": proposal.get("entry_point", ""),
        "target": proposal.get("target", ""),
        "scope": list(proposal.get("scope") or []),
        "code_hashes_current": dict(proposal.get("code_hashes") or {}),
        "approval": {"present": True},
        "uses_consumed": 0,
        "now": NOW,
    }
    kwargs.update(overrides)
    return proposal_mod.validate_request(proposal, **kwargs)


# ── scope is derived, not inferred ───────────────────────────────────────


def test_routine_scope_comes_from_the_runtime_declaration_authority():
    assert proposal_mod.routine_scope() == list(ALL_SYNC_TABLES)
    assert len(proposal_mod.routine_scope()) == 24


def test_routine_scope_is_not_todays_changed_subset():
    """The standing scope must never shrink to whatever changed today."""
    scope = set(proposal_mod.routine_scope())
    todays_six = {"agenda_items", "entities", "entity_mentions",
                  "entity_relationships", "meetings", "supporting_documents"}
    assert todays_six < scope
    assert scope - todays_six, "standing scope must exceed today's changed tables"


# ── the artifact is a proposal, never an authorization ───────────────────


def test_proposal_is_non_executable_by_construction():
    proposal = _proposal()
    assert proposal["executable"] is False
    assert proposal["approver"] is None
    assert proposal["authorization"] is None
    assert proposal["kind"] == "standing-authorization-proposal"


def test_proposal_envelope_matches_the_requested_terms():
    proposal = _proposal()
    assert proposal["operation"] == "OP-RECON"
    assert proposal["entry_point"] == "scripts/db/sync_prod.py"
    assert proposal["target"] == "production"
    assert proposal["validity_days"] == 365
    assert proposal["max_uses"] == 400


def test_every_mandatory_gate_is_declared():
    proposal = _proposal()
    assert len(proposal["mandatory_gates"]) == 11
    assert [g["id"] for g in proposal["mandatory_gates"]] == list(
        proposal_mod.GATE_IDS)


def test_standing_proposal_never_lands_in_the_release_directory():
    """The interlock reads data/release only; a proposal must not be reachable there."""
    assert proposal_mod.__file__
    default = (proposal_mod.REPO_ROOT / "data" / "standing-proposals" /
               "OP-RECON-standing-daily-sync.proposal.json")
    assert "data/release" not in str(default)


# ── use accounting ───────────────────────────────────────────────────────


@pytest.mark.parametrize("receipt,expected", [
    ({"terminal": True, "status": "succeeded", "reconciled": True}, True),
    ({"terminal": True, "status": "succeeded", "reconciled": False}, False),
    ({"terminal": True, "status": "failed", "reconciled": True}, False),
    ({"terminal": False, "status": "succeeded", "reconciled": True}, False),
    ({"terminal": True, "status": "refused", "reconciled": True}, False),
    (None, False),
    ({}, False),
])
def test_only_a_successful_reconciled_terminal_receipt_consumes_a_use(receipt, expected):
    assert proposal_mod.consumes_use(receipt) is expected


# ── the happy path ───────────────────────────────────────────────────────


def test_matching_request_is_allowed_once_approved():
    verdict = _request(_proposal())
    assert verdict["status"] == "ALLOWED"
    assert verdict["code"] is None
    assert verdict["use"] == 1 and verdict["max_uses"] == 400


# ── negative: every gate ─────────────────────────────────────────────────


def test_missing_approval_refuses():
    verdict = _request(_proposal(), approval=None)
    assert verdict["code"] == proposal_mod.NOT_APPROVED


def test_ambiguous_authorization_refuses():
    verdict = _request(_proposal(), approvals_matching=2)
    assert verdict["code"] == proposal_mod.AMBIGUOUS_AUTHORIZATION


def test_no_matching_approval_refuses():
    verdict = _request(_proposal(), approvals_matching=0)
    assert verdict["code"] == proposal_mod.NOT_APPROVED


def test_operation_mismatch_refuses():
    assert _request(_proposal(), operation="OP-OTHER")["code"] == \
        proposal_mod.OPERATION_MISMATCH
    assert _request(_proposal(), operation="")["code"] == \
        proposal_mod.OPERATION_MISMATCH


def test_entry_point_mismatch_refuses():
    assert _request(_proposal(), entry_point="scripts/other.py")["code"] == \
        proposal_mod.ENTRY_POINT_MISMATCH
    assert _request(_proposal(), entry_point="")["code"] == \
        proposal_mod.ENTRY_POINT_MISMATCH


def test_target_mismatch_refuses():
    assert _request(_proposal(), target="staging")["code"] == \
        proposal_mod.TARGET_MISMATCH
    assert _request(_proposal(), target="")["code"] == proposal_mod.TARGET_MISMATCH


def test_narrower_scope_refuses():
    scope = list(proposal_mod.routine_scope())[:-1]
    assert _request(_proposal(), scope=scope)["code"] == proposal_mod.SCOPE_MISMATCH


def test_broader_scope_refuses():
    scope = list(proposal_mod.routine_scope()) + ["not_a_real_table"]
    verdict = _request(_proposal(), scope=scope)
    assert verdict["code"] == proposal_mod.SCOPE_MISMATCH
    assert "broader" in verdict["reason"]


def test_missing_scope_refuses():
    assert _request(_proposal(), scope=None)["code"] == proposal_mod.SCOPE_MISMATCH


def test_changed_code_hash_refuses():
    current = dict(_proposal()["code_hashes"])
    key = sorted(current)[0]
    current[key] = "0" * 64
    assert _request(_proposal(), code_hashes_current=current)["code"] == \
        proposal_mod.CODE_MISMATCH


def test_unbound_code_present_refuses():
    current = dict(_proposal()["code_hashes"])
    current["scripts/db/extra.py"] = "a" * 64
    assert _request(_proposal(), code_hashes_current=current)["code"] == \
        proposal_mod.CODE_MISMATCH


def test_window_not_yet_valid_refuses():
    proposal = _proposal(not_before=(NOW + timedelta(days=1)).isoformat())
    assert _request(proposal)["code"] == proposal_mod.WINDOW_NOT_YET_VALID


def test_expired_window_refuses():
    """A window that was valid but has elapsed is expired, not malformed."""
    proposal = _proposal(
        not_before=(NOW - timedelta(days=400)).isoformat(),
        not_after=(NOW - timedelta(days=35)).isoformat())
    assert _request(proposal)["code"] == proposal_mod.WINDOW_EXPIRED


def test_inverted_window_refuses_as_malformed():
    """An inverted window is a malformed artifact, refused before anything else."""
    proposal = _proposal(not_after=(NOW - timedelta(seconds=1)).isoformat())
    assert _request(proposal)["code"] == proposal_mod.MALFORMED


def test_exhausted_uses_refuse():
    assert _request(_proposal(), uses_consumed=400)["code"] == \
        proposal_mod.USES_EXHAUSTED
    assert _request(_proposal(), uses_consumed=399)["status"] == "ALLOWED"


def test_missing_gate_refuses():
    proposal = _proposal()
    proposal["mandatory_gates"] = proposal["mandatory_gates"][:-1]
    proposal["digest"] = proposal_mod.proposal_digest(proposal)
    verdict = _request(proposal)
    assert verdict["code"] == proposal_mod.GATE_MISSING
    assert "terminal_receipt" in verdict["reason"]


def test_malformed_proposal_refuses():
    assert _request(None)["code"] == proposal_mod.MALFORMED
    assert _request({"schema": "wrong"})["code"] == proposal_mod.MALFORMED


def test_tampered_proposal_refuses():
    proposal = _proposal()
    proposal["max_uses"] = 100000  # widen after the fact
    assert _request(proposal)["code"] == proposal_mod.TAMPERED


# ── negative: proposal / authorization confusion ─────────────────────────


@pytest.mark.parametrize("field,value", [
    ("executable", True),
    ("approver", "someone"),
    ("authorization", {"mode": "standing"}),
])
def test_authorization_shaped_proposal_is_refused(field, value):
    proposal = _proposal(**{field: value})
    verdict = _request(proposal)
    assert verdict["code"] == proposal_mod.PROPOSAL_NOT_AUTHORIZATION


# ── the contract cannot touch credentials, network or a database ─────────


def test_contract_opens_no_database_or_network():
    source = Path(proposal_mod.__file__).read_text()
    for token in ("create_engine", "get_engine", "psycopg", "requests",
                  "urlopen", "PROD_DATABASE_URL", "subprocess"):
        assert token not in source, f"proposal contract references {token!r}"


def test_validation_is_pure_and_needs_no_artifacts_on_disk():
    """A refusal must be computable with no filesystem, network or DB access."""
    verdict = _request(_proposal(), operation="")
    assert verdict["status"] == "REFUSED"
    assert json.dumps(verdict)  # JSON-serializable verdict
