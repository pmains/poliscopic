#!/usr/bin/env python3
"""Regenerated newsletter editorial authorization: envelope + mode refusal proof.

The superseded authorization (plan 0953220a…, auth 523a2359…) was voided when
bound code changed. This pins the replacement: same entry point, same four-table
scope, same 90-day/150-use policy, every prohibition intact, explicit
mode="upsert" — and proves a delete/schema/repair/deploy/restart/scheduler/alert or
changed-code request cannot satisfy it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.ops import editorial_authorization_proposal as editorial

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = (ROOT / "data" / "standing-proposals" /
            "OP-RECON-newsletter-editorial-sync.proposal.json")

ENTRY = "scripts/editorial_sync.py"
SCOPE = ["article_sources", "article_tags", "articles", "tags"]


# ── the proposal envelope ───────────────────────────────────────────────

def test_envelope_preserves_the_superseded_policy():
    proposal = editorial.build_proposal()
    assert proposal["operation"] == "OP-RECON"
    assert proposal["entry_point"] == ENTRY
    assert proposal["target"] == "production"
    assert sorted(proposal["scope"]) == sorted(SCOPE)
    assert proposal["validity_days"] == 90        # unchanged policy
    assert proposal["max_uses"] == 150            # unchanged policy
    assert proposal["rollback_owner"] == "Pete Mains"
    assert proposal["mode"] == "upsert"           # the whole point
    assert proposal["executable"] is False
    assert proposal["approver"] is None
    assert proposal["authorization"] is None


def test_every_prior_prohibition_is_intact():
    proposal = editorial.build_proposal()
    prohibited = " | ".join(proposal["prohibited_actions"]).lower()
    for term in ("reconcile", "schema", "deploy", "restart", "scheduler", "alert",
                 "repair", "delete"):
        assert term in prohibited, f"prohibition about {term!r} was dropped"
    for mode in ("reconcile", "schema-only", "bootstrap-schema"):
        assert mode in proposal["prohibited_modes"]


def test_code_binding_is_narrow_and_durable():
    """Only an edit to the production writer should revoke this approval."""
    proposal = editorial.build_proposal()
    assert tuple(proposal["code_hashes"]) == ("scripts/editorial_sync.py",)
    policy = proposal["code_binding_policy"]
    assert policy["bound"] == ["scripts/editorial_sync.py"]
    assert policy["writer_change_effect"].endswith("(CODE_CHANGED)")
    assert policy["unrelated_change_effect"] == "authorization remains valid"
    for unrelated in (
        "scripts/ops/production_interlock.py",
        "scripts/ops/operation_authorization.py",
        "scripts/publish_newsletter_article.py",
        "workflows/workflow-runner.py",
    ):
        assert unrelated not in proposal["code_hashes"]


def test_proposal_does_not_inherit_the_old_approval():
    proposal = editorial.build_proposal()
    supersedes = proposal["supersedes"]
    assert supersedes["inherits_approval"] is False
    assert supersedes["plan_digest"].startswith("0953220a")
    assert supersedes["authorization_digest"].startswith("523a2359")
    assert "release-superseded" in supersedes["archived_at"]


def test_built_artifact_matches_and_is_not_executable():
    if not ARTIFACT.is_file():
        pytest.skip("proposal not generated on this checkout")
    artifact = json.loads(ARTIFACT.read_text())
    assert artifact["digest"] == editorial.proposal_digest(artifact)
    assert artifact["executable"] is False
    assert artifact["mode"] == "upsert"
    assert sorted(artifact["scope"]) == sorted(SCOPE)


# ── mode refusal proof, against a real plan + authorization ──────────────

@pytest.fixture()
def authorized(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_RELEASE_DIR", str(tmp_path / "release"))
    monkeypatch.setenv("POLISCOPIC_AUDIT_DIR", str(tmp_path / "audit"))
    from scripts.ops import operation_authorization as oa

    now = datetime.now(timezone.utc)
    plan = oa.build_plan(
        operation="OP-RECON", operation_id="newsletter-mode-test", entry_point=ENTRY,
        scope=list(SCOPE), code_paths=[], rollback_owner="Pete Mains",
        not_before=now - timedelta(minutes=1), not_after=now + timedelta(days=90),
        mode="upsert")
    oa.write_plan(plan)
    oa.record_authorization(plan, verbatim_approval="Approved for this test",
                            author="Tester", source="human", mode="standing",
                            max_uses=150)
    return oa


def test_upsert_matches(authorized):
    verdict = authorized.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                                  target="production", mode="upsert")
    assert verdict["status"] == "ALLOWED", verdict


@pytest.mark.parametrize("mode", ["reconcile", "reconcile-only", "schema-only",
                                  "bootstrap-schema", "repair", "cleanup",
                                  "backfill", "schema"])
def test_every_other_mode_refuses(authorized, mode):
    """Delete/schema/repair must never be satisfiable by the editorial authority."""
    verdict = authorized.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                                  target="production", mode=mode)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == authorized.MODE_MISMATCH


def test_missing_mode_refuses(authorized):
    assert authorized.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                               mode=None)["code"] == authorized.MODE_MISSING


@pytest.mark.parametrize("operation", ["OP-CODE", "OP-RESTORE", "OP-REPAIR",
                                       "OP-SCHEMA", "OP-STATUS"])
def test_other_operations_cannot_use_this_authorization(authorized, operation):
    """Deploy, restart/restore, repair and schema requests are different kinds."""
    verdict = authorized.validate(operation, entry_point=ENTRY, scope=SCOPE,
                                  target="production", mode="upsert")
    assert verdict["status"] == "REFUSED"


def test_unknown_kind_refuses_before_anything_else():
    from scripts.ops.production_interlock import check

    verdict = check("OP-SCHEDULER", entry_point=ENTRY, scope=SCOPE, mode="upsert")
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == "UNKNOWN_OPERATION"
    verdict = check("OP-ALERT", entry_point=ENTRY, scope=SCOPE, mode="upsert")
    assert verdict["code"] == "UNKNOWN_OPERATION"


def test_changed_code_refuses(tmp_path, monkeypatch):
    """The exact failure that stopped the newsletter: bound code changed."""
    monkeypatch.setenv("POLISCOPIC_RELEASE_DIR", str(tmp_path / "release"))
    monkeypatch.setenv("POLISCOPIC_AUDIT_DIR", str(tmp_path / "audit"))
    from scripts.ops import operation_authorization as oa
    from scripts.ops.editorial_authorization_proposal import CODE_PATHS

    now = datetime.now(timezone.utc)
    plan = oa.build_plan(
        operation="OP-RECON", operation_id="newsletter-code-test", entry_point=ENTRY,
        scope=list(SCOPE), code_paths=list(CODE_PATHS), rollback_owner="Pete Mains",
        not_before=now - timedelta(minutes=1), not_after=now + timedelta(days=90),
        mode="upsert")
    oa.write_plan(plan)
    oa.record_authorization(plan, verbatim_approval="Approved for this test",
                            author="Tester", source="human", mode="standing",
                            max_uses=150)
    assert oa.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE, mode="upsert")[
        "status"] == "ALLOWED"

    # Drift the bound hashes WITHOUT touching any real file: the validator must
    # re-hash and notice. This is what CODE_CHANGED means.
    real = oa.code_hashes
    monkeypatch.setattr(oa, "code_hashes",
                        lambda paths: {p: "0" * 64 for p in paths})
    try:
        verdict = oa.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                              target="production", mode="upsert")
        assert verdict["code"] == oa.CODE_CHANGED
    finally:
        monkeypatch.setattr(oa, "code_hashes", real)
