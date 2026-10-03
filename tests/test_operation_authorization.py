#!/usr/bin/env python3
"""Tests for the one-operation plan / authorization validator (G7/G8 enforcement).

The validator is a production-safety authority, so the bulk of this file proves it
REFUSES. A validator that only ever allows would pass a happy-path test suite, so
every binding gets a negative test: tampered plan, tampered authorization, changed
code, expired window, wrong operation, wrong entry point, widened scope, wrong
target, non-human source, and exhausted uses.

No network, no database, no production access: everything runs in tmp_path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = PROJECT_ROOT / "scripts" / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import operation_authorization as oa  # noqa: E402
import production_interlock as interlock  # noqa: E402

OP = "OP-RECON"
OID = "newsletter-daily-test"
ENTRY = "pub.py"
SCOPE = ["tags", "articles"]
#: A code path that exists relative to the REAL project root — required for the
#: CLI tests, which run in a subprocess and so cannot see the monkeypatched root.
REAL_CODE = "scripts/ops/production_interlock.py"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated release/audit dirs plus fake code files inside a fake root."""
    monkeypatch.setenv("POLISCOPIC_RELEASE_DIR", str(tmp_path / "release"))
    monkeypatch.setenv("POLISCOPIC_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(oa, "PROJECT_ROOT", tmp_path)
    (tmp_path / ENTRY).write_text("v1\n")
    (tmp_path / "gate.py").write_text("gate\n")
    return tmp_path


def _plan(days_from_now: float = 90.0, scope=None, entry: str = ENTRY,
          code_paths=("pub.py", "gate.py"), target: str = "production") -> dict:
    now = datetime.now(timezone.utc)
    return oa.build_plan(
        operation=OP,
        operation_id=OID,
        entry_point=entry,
        scope=list(scope if scope is not None else SCOPE),
        code_paths=list(code_paths),
        rollback_owner="Pete",
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=days_from_now),
        target=target,
    )


def _authorize(plan, mode="standing", max_uses=5, author="Pete",
               source="human", text="I approve this operation."):
    return oa.record_authorization(plan, verbatim_approval=text, author=author,
                                   source=source, mode=mode, max_uses=max_uses)


def _validate(scope=None, entry: str = ENTRY, target: str = "production", now=None,
              mode: str = "upsert"):
    """Validate a matching request.

    A mode is REQUIRED now: a table scope alone cannot separate an upsert from a
    delete or a schema change, so every production-mutating request must declare
    one. ``_plan`` builds plans with mode="upsert", so that is the default here.
    """
    return oa.validate(OP, entry_point=entry, scope=scope if scope is not None else SCOPE,
                       target=target, now=now, mode=mode)


def _rewrite(path: Path, mutate) -> None:
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload))


# ── the refusals ─────────────────────────────────────────────────────────


def test_no_artifacts_refuses(sandbox):
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_MISSING


def test_plan_only_without_authorization_refuses(sandbox):
    oa.write_plan(_plan())
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_MISSING


def test_tampered_plan_digest_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    # widen the plan's scope after the fact — the digest must catch it
    _rewrite(oa.operation_dir(OP, OID) / "plan.json",
             lambda p: p.update(scope=["tags", "articles", "meetings"]))
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.PLAN_DIGEST_MISMATCH


def test_tampered_authorization_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan, mode="single-use", max_uses=1)
    # quietly upgrade single-use to standing and extend the window
    _rewrite(oa.operation_dir(OP, OID) / "authorization.json",
             lambda a: a.update(mode="standing", max_uses=99))
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_TAMPERED


def test_code_change_voids_authorization(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    assert _validate()["status"] == "ALLOWED"
    (sandbox / ENTRY).write_text("v2 — publishing code edited\n")
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.CODE_CHANGED


def test_code_path_missing_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    (sandbox / "gate.py").unlink()
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.CODE_CHANGED


def test_expired_window_refuses(sandbox):
    plan = _plan(days_from_now=1.0)
    oa.write_plan(plan)
    _authorize(plan)
    verdict = _validate(now=datetime.now(timezone.utc) + timedelta(days=5))
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_EXPIRED


def test_not_yet_valid_refuses(sandbox):
    now = datetime.now(timezone.utc)
    plan = oa.build_plan(OP, OID, ENTRY, SCOPE, [ENTRY], "Pete",
                         now + timedelta(days=1), now + timedelta(days=10))
    oa.write_plan(plan)
    _authorize(plan)
    verdict = _validate(now=now)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_NOT_YET_VALID


def test_operation_mismatch_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    verdict = oa.validate("OP-RESTORE", entry_point=ENTRY, scope=SCOPE)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_MISSING


def test_entry_point_mismatch_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    verdict = _validate(entry="scripts/editorial_sync.py")
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.ENTRY_POINT_MISMATCH


def test_scope_broadening_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    verdict = _validate(scope=["tags", "articles", "meetings"])
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.SCOPE_MISMATCH


def test_target_mismatch_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    verdict = _validate(target="staging")
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.TARGET_MISMATCH


def test_planless_directory_does_not_block_a_valid_authorization(sandbox):
    """SUPERSEDED PREMISE (revised 2026-09-23, deliberately).

    The original test asserted that ANY second directory for the operation kind
    makes the request ambiguous. That was too coarse: it meant one new
    authorization for a kind (e.g. a data-sync OP-RECON) refused EVERY other
    authorization for that kind, including the live newsletter one. Ambiguity
    now means "more than one authorization matches THIS request".

    A directory holding no readable plan is not an authorization for anything, so
    it must not block a valid one. Genuine ambiguity is still refused — see
    test_two_authorizations_matching_the_same_request_are_ambiguous.
    """
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    (oa.release_dir() / f"{OP}-another").mkdir(parents=True)
    verdict = _validate()
    assert verdict["status"] == "ALLOWED", verdict


def test_malformed_plan_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    (oa.operation_dir(OP, OID) / "plan.json").write_text("{not json")
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_MISSING


def test_unbound_plan_digest_refuses(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    # drop the digest from the artifact AFTER it was written (the write itself
    # needs the digest for the audit record)
    path = oa.operation_dir(OP, OID) / "plan.json"
    _rewrite(path, lambda p: p.pop("digest", None))
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.PLAN_DIGEST_UNBOUND


# ── recording rules ──────────────────────────────────────────────────────


def test_authorization_is_never_overwritten(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    with pytest.raises(FileExistsError):
        _authorize(plan)


def test_record_refuses_non_human_source(sandbox):
    plan = _plan()
    with pytest.raises(ValueError):
        oa.record_authorization(plan, verbatim_approval="x", author="Pete",
                                source="agent")


def test_record_refuses_empty_verbatim(sandbox):
    plan = _plan()
    with pytest.raises(ValueError):
        oa.record_authorization(plan, verbatim_approval="   ", author="Pete")


def test_record_refuses_unnamed_author(sandbox):
    plan = _plan()
    with pytest.raises(ValueError):
        oa.record_authorization(plan, verbatim_approval="x", author="  ")


def test_record_refuses_single_use_with_wide_max_uses(sandbox):
    plan = _plan()
    with pytest.raises(ValueError):
        oa.record_authorization(plan, verbatim_approval="x", author="Pete",
                                mode="single-use", max_uses=10)


def test_build_plan_rejects_inverted_window(sandbox):
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        oa.build_plan(OP, OID, ENTRY, SCOPE, [ENTRY], "Pete",
                      now + timedelta(days=2), now + timedelta(days=1))


def test_build_plan_rejects_empty_scope(sandbox):
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        oa.build_plan(OP, OID, ENTRY, [], [ENTRY], "Pete", now, now + timedelta(days=1))


def test_build_plan_rejects_unnamed_rollback_owner(sandbox):
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        oa.build_plan(OP, OID, ENTRY, SCOPE, [ENTRY], "   ", now, now + timedelta(days=1))


# ── the allowances (and their limits) ────────────────────────────────────


def test_standing_authorization_allows_within_use_budget(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan, mode="standing", max_uses=3)
    for expected in (1, 2, 3):
        verdict = _validate()
        assert verdict["status"] == "ALLOWED"
        assert verdict["receipt"]["use_number"] == expected
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.USES_EXHAUSTED


def test_single_use_authorization_allows_exactly_once(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan, mode="single-use", max_uses=1)
    assert _validate()["status"] == "ALLOWED"
    second = _validate()
    assert second["status"] == "REFUSED"
    assert second["code"] == oa.USES_EXHAUSTED


def test_every_allowance_appends_a_receipt(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan, mode="standing", max_uses=5)
    _validate()
    _validate()
    assert oa.count_uses(OID) == 2


def test_terminal_accounting_does_not_consume_at_allowance(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    oa.record_authorization(
        plan, verbatim_approval="I approve this operation.", author="Pete",
        mode="standing", max_uses=3, use_accounting="successful-terminal")

    verdict = oa.validate(
        OP, entry_point=ENTRY, scope=SCOPE, mode="upsert",
        attempt_id="daily-2026-10-03")

    assert verdict["status"] == "ALLOWED"
    assert verdict["use_accounting"] == "successful-terminal"
    assert oa.count_uses(OID) == 0


def test_terminal_accounting_requires_attempt_id(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    oa.record_authorization(
        plan, verbatim_approval="I approve this operation.", author="Pete",
        mode="standing", max_uses=3, use_accounting="successful-terminal")

    verdict = _validate()

    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.ATTEMPT_ID_MISSING


def test_only_successful_digest_bound_terminal_consumes_deferred_use(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    auth_path = oa.record_authorization(
        plan, verbatim_approval="I approve this operation.", author="Pete",
        mode="standing", max_uses=3, use_accounting="successful-terminal")
    auth = json.loads(auth_path.read_text())
    body = {
        "terminal": True,
        "status": "succeeded",
        "reconciled": True,
        "operation": OP,
        "operation_id": OID,
        "attempt_id": "daily-2026-10-03",
        "plan_digest": plan["digest"],
        "authorization_digest": auth["auth_digest"],
    }
    terminal = {**body, "digest": oa.hashlib.sha256(
        oa.canonical_json(body).encode("utf-8")).hexdigest()}

    path = oa.consume_successful_terminal(OID, terminal)

    assert path.is_file()
    assert oa.count_uses(OID) == 1
    with pytest.raises(FileExistsError):
        oa.consume_successful_terminal(OID, terminal)


@pytest.mark.parametrize("status,reconciled", [
    ("failed", True), ("refused", True), ("succeeded", False),
])
def test_failed_or_unreconciled_terminal_never_consumes(sandbox, status, reconciled):
    plan = _plan()
    oa.write_plan(plan)
    auth_path = oa.record_authorization(
        plan, verbatim_approval="I approve this operation.", author="Pete",
        mode="standing", max_uses=3, use_accounting="successful-terminal")
    auth = json.loads(auth_path.read_text())
    body = {
        "terminal": True, "status": status, "reconciled": reconciled,
        "operation": OP, "operation_id": OID,
        "attempt_id": "daily-2026-10-03", "plan_digest": plan["digest"],
        "authorization_digest": auth["auth_digest"],
    }
    terminal = {**body, "digest": oa.hashlib.sha256(
        oa.canonical_json(body).encode("utf-8")).hexdigest()}

    with pytest.raises(ValueError):
        oa.consume_successful_terminal(OID, terminal)
    assert oa.count_uses(OID) == 0


def test_scope_order_does_not_matter(sandbox):
    plan = _plan(scope=["tags", "articles"])
    oa.write_plan(plan)
    _authorize(plan)
    verdict = _validate(scope=["articles", "tags"])
    assert verdict["status"] == "ALLOWED"


def test_authorization_cannot_be_satisfied_by_omitting_the_entry_point(sandbox):
    """An undeclared entry point must REFUSE, not skip the check.

    Found 2026-09-23 while auditing live use receipts: the check was
    ``if entry_point and ...``, so a caller declaring nothing skipped it and was
    ALLOWED — meaning a newsletter authorization could satisfy an unrelated
    caller. Fail closed instead.
    """
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    verdict = oa.validate(OP, entry_point="", scope=SCOPE)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.ENTRY_POINT_MISSING


def test_authorization_cannot_be_satisfied_by_omitting_the_scope(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    verdict = oa.validate(OP, entry_point=ENTRY, scope=None)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.SCOPE_MISSING


def test_interlock_with_default_arguments_is_not_a_way_in(sandbox):
    """The CLI defaults (no --entry-point/--scope) must not open the gate."""
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    verdict = interlock.check(OP, mode="upsert")
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.ENTRY_POINT_MISSING


# ── interlock integration ────────────────────────────────────────────────


def test_interlock_refuses_without_authorization(sandbox):
    verdict = interlock.check(OP, entry_point=ENTRY, scope=SCOPE, mode="upsert")
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_MISSING
    assert verdict["authorization_issuance"] == "disabled"


def test_interlock_allows_with_valid_authorization(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    verdict = interlock.check(OP, entry_point=ENTRY, scope=SCOPE, mode="upsert")
    assert verdict["status"] == "ALLOWED"
    assert verdict["authorization_issuance"] == "validated"


def test_interlock_allows_exact_code_release_mode(sandbox):
    now = datetime.now(timezone.utc)
    plan = oa.build_plan(
        operation="OP-CODE",
        operation_id="code-release-test",
        entry_point="scripts/ops/deploy_release.sh",
        scope=["app.py"],
        code_paths=["pub.py", "gate.py"],
        rollback_owner="Pete",
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=1),
        target="production",
        mode="code",
    )
    oa.write_plan(plan)
    oa.record_authorization(
        plan,
        verbatim_approval="I approve this code release.",
        author="Pete",
        mode="single-use",
        max_uses=1,
    )

    verdict = interlock.check(
        "OP-CODE",
        entry_point="scripts/ops/deploy_release.sh",
        scope=["app.py"],
        mode="code",
        authorization_id="code-release-test",
    )

    assert verdict["status"] == "ALLOWED"
    assert verdict["authorization"]["operation_id"] == "code-release-test"


def test_terminal_accounted_interlock_refuses_without_daily_evidence(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    oa.record_authorization(
        plan, verbatim_approval="I approve this operation.", author="Pete",
        mode="standing", max_uses=3, use_accounting="successful-terminal")

    verdict = interlock.check(
        OP, entry_point=ENTRY, scope=SCOPE, mode="upsert",
        attempt_id="daily-2026-10-03")

    assert verdict["status"] == "REFUSED"
    assert verdict["code"] in {"COMPLETION_INCOMPLETE", "DAILY_GATE_UNAVAILABLE"}


def test_terminal_accounted_interlock_allows_only_after_daily_gate(sandbox, monkeypatch):
    plan = _plan()
    oa.write_plan(plan)
    oa.record_authorization(
        plan, verbatim_approval="I approve this operation.", author="Pete",
        mode="standing", max_uses=3, use_accounting="successful-terminal")
    import daily_sync_gate
    monkeypatch.setattr(daily_sync_gate, "validate_pre_sync", lambda **_: {
        "status": "ALLOWED", "code": None, "backup_digest": "b" * 64,
        "preflight_digest": "p" * 64,
    })

    verdict = interlock.check(
        OP, entry_point=ENTRY, scope=SCOPE, mode="upsert",
        authorization_id=OID, attempt_id="daily-2026-10-03",
        run_date="2026-10-03", preflight_path="preflight.json",
        backup_receipt_path="backup.json")

    assert verdict["status"] == "ALLOWED"
    assert verdict["daily_gate"]["backup_digest"] == "b" * 64
    assert oa.count_uses(OID) == 0


def test_interlock_still_allows_read_only(sandbox):
    verdict = interlock.check("OP-STATUS")
    assert verdict["status"] == "ALLOWED"
    assert verdict["mutates_production"] is False


def test_interlock_still_refuses_every_other_mutating_kind(sandbox):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    for other in ("OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RESTORE"):
        verdict = interlock.check(other, entry_point=ENTRY, scope=SCOPE, mode="upsert")
        assert verdict["status"] == "REFUSED", other


def test_interlock_still_refuses_unknown_kind(sandbox):
    verdict = interlock.check("OP-NONSENSE")
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == interlock.UNKNOWN_OPERATION


def test_interlock_refuses_environment_bypass(sandbox, monkeypatch):
    plan = _plan()
    oa.write_plan(plan)
    _authorize(plan)
    monkeypatch.setenv("POLISCOPIC_FORCE_PROD", "1")
    verdict = interlock.check(OP, entry_point=ENTRY, scope=SCOPE)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == interlock.BYPASS_ATTEMPT


def test_interlock_authorization_cannot_cross_entry_points(sandbox):
    """An authorization for one entry point must not unlock another."""
    plan = _plan(entry=ENTRY)
    oa.write_plan(plan)
    _authorize(plan)
    assert interlock.check(OP, entry_point=ENTRY, scope=SCOPE,
                           mode="upsert")["status"] == "ALLOWED"
    other = interlock.check(OP, entry_point="scripts/editorial_sync.py", scope=SCOPE,
                            mode="upsert")
    assert other["status"] == "REFUSED"
    assert other["code"] == oa.ENTRY_POINT_MISMATCH


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_plan_then_authorize_then_validate(sandbox, tmp_path):
    env = {
        "POLISCOPIC_RELEASE_DIR": str(tmp_path / "release"),
        "POLISCOPIC_AUDIT_DIR": str(tmp_path / "audit"),
        "PATH": "/usr/bin:/bin",
    }
    cli = str(OPS_DIR / "plan_operation.py")
    build = subprocess.run(
        [sys.executable, cli, "plan", "--operation", OP, "--operation-id", OID,
         "--entry-point", REAL_CODE, "--scope", "tags,articles",
         "--code-path", REAL_CODE, "--rollback-owner", "Pete", "--days", "30"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )
    assert build.returncode == 0, build.stderr
    assert "DIGEST:" in build.stdout

    approval = tmp_path / "approval.txt"
    approval.write_text("I approve this operation.\n")
    record = subprocess.run(
        [sys.executable, cli, "authorize", "--operation", OP, "--operation-id", OID,
         "--verbatim-file", str(approval), "--author", "Pete",
         "--mode", "standing", "--max-uses", "5"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )
    assert record.returncode == 0, record.stderr

    check = subprocess.run(
        [sys.executable, cli, "validate", "--operation", OP,
         "--entry-point", REAL_CODE, "--scope", "tags,articles",
         "--mode", "upsert"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert '"status": "ALLOWED"' in check.stdout

    # Omitting the mode must NOT be a way in: the refusal is explicit.
    omitted = subprocess.run(
        [sys.executable, cli, "validate", "--operation", OP,
         "--entry-point", REAL_CODE, "--scope", "tags,articles"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )
    assert omitted.returncode == 3
    assert "MODE_MISSING" in omitted.stdout


def test_cli_authorize_refuses_missing_verbatim_file(sandbox, tmp_path):
    env = {
        "POLISCOPIC_RELEASE_DIR": str(tmp_path / "release"),
        "POLISCOPIC_AUDIT_DIR": str(tmp_path / "audit"),
    }
    cli = str(OPS_DIR / "plan_operation.py")
    planned = subprocess.run(
        [sys.executable, cli, "plan", "--operation", OP, "--operation-id", OID,
         "--entry-point", REAL_CODE, "--scope", "tags", "--code-path", REAL_CODE,
         "--rollback-owner", "Pete"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )
    assert planned.returncode == 0, planned.stderr
    result = subprocess.run(
        [sys.executable, cli, "authorize", "--operation", OP, "--operation-id", OID,
         "--verbatim-file", str(tmp_path / "nope.txt"), "--author", "Pete"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )
    assert result.returncode != 0


# ── multiple authorizations for one operation KIND ───────────────────────


def test_a_kind_may_hold_two_authorizations_for_different_entry_points(sandbox):
    """Regression: a second OP-RECON plan must not break the first one.

    Found 2026-09-23: writing a data-sync OP-RECON plan made the live newsletter
    authorization REFUSE with AUTHORIZATION_AMBIGUOUS, because the validator
    required exactly one directory per operation KIND. That would have broken
    daily newsletter publication. Ambiguity must mean "more than one matches this
    request", not "more than one exists".
    """
    now = datetime.now(timezone.utc)
    plan_a = oa.build_plan(OP, OID, ENTRY, SCOPE, [ENTRY], "Pete",
                           now - timedelta(minutes=1), now + timedelta(days=30))
    oa.write_plan(plan_a)
    _authorize(plan_a)

    other_entry = "other.py"
    (sandbox / other_entry).write_text("v1\n")
    plan_b = oa.build_plan(OP, "other-op", other_entry, ["other_table"],
                           [other_entry], "Pete",
                           now - timedelta(minutes=1), now + timedelta(days=30))
    oa.write_plan(plan_b)

    # the original request still resolves to its own authorization
    first = _validate()
    assert first["status"] == "ALLOWED", first
    assert "use 1/" in first["reason"]

    # the second artifact exists but carries no authorization
    second = oa.validate(OP, entry_point=other_entry, scope=["other_table"])
    assert second["status"] == "REFUSED"
    assert second["code"] == oa.AUTHORIZATION_MISSING


def test_request_matching_no_artifact_is_missing_not_ambiguous(sandbox):
    """With several artifacts present, a non-matching request is MISSING."""
    now = datetime.now(timezone.utc)
    for oid, entry in (("a", ENTRY), ("b", "other.py")):
        (sandbox / entry).write_text("v1\n")
        oa.write_plan(oa.build_plan(OP, oid, entry, SCOPE, [entry], "Pete",
                                    now - timedelta(minutes=1),
                                    now + timedelta(days=30)))
    verdict = oa.validate(OP, entry_point="third.py", scope=SCOPE)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_MISSING


def test_two_authorizations_matching_the_same_request_are_ambiguous(sandbox):
    """Genuine ambiguity — two artifacts for the SAME request — must refuse."""
    now = datetime.now(timezone.utc)
    for oid in ("dup-a", "dup-b"):
        plan = oa.build_plan(OP, oid, ENTRY, SCOPE, [ENTRY], "Pete",
                             now - timedelta(minutes=1), now + timedelta(days=30))
        oa.write_plan(plan)
        _authorize(plan)
    verdict = _validate()
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_AMBIGUOUS


def test_explicit_operation_id_resolves_matching_authorization_ambiguity(sandbox):
    now = datetime.now(timezone.utc)
    for oid in ("dup-a", "dup-b"):
        plan = oa.build_plan(OP, oid, ENTRY, SCOPE, [ENTRY], "Pete",
                             now - timedelta(minutes=1), now + timedelta(days=30))
        oa.write_plan(plan)
        _authorize(plan)

    verdict = oa.validate(OP, entry_point=ENTRY, scope=SCOPE, mode="upsert",
                          operation_id="dup-b")
    assert verdict["status"] == "ALLOWED", verdict
    assert verdict["operation_id"] == "dup-b"


def test_missing_explicit_operation_id_refuses(sandbox):
    verdict = oa.validate(OP, entry_point=ENTRY, scope=SCOPE, mode="upsert",
                          operation_id="does-not-exist")
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == oa.AUTHORIZATION_MISSING
