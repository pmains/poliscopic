#!/usr/bin/env python3
"""Execution-mode binding: an upsert authorization must never authorize a delete.

Scope alone cannot separate an insert-or-update from a delete or a schema change —
a plain `sync_prod.py --reconcile` declares exactly the same operation, entry
point, target and table scope as a routine upsert. These tests prove the mode is
bound end to end, using a real plan and a real recorded authorization in a
temporary release directory.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The interlock is normally entered as a CLI (script dir on sys.path) or from
# sync_prod.py, which inserts scripts/ops itself. `production_interlock.check`
# imports the validator by its top-level name, so these tests must place both dirs
# on sys.path exactly as a production caller does.
for _path in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "scripts" / "ops")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

ENTRY = "scripts/db/sync_prod.py"
SCOPE = ["entities", "meetings", "public_bodies"]
READ_ONLY_OPS = {"OP-STATUS", "OP-PREFLIGHT", "OP-DEV"}


@pytest.fixture()
def authorized(tmp_path, monkeypatch):
    """A real standing, mode=upsert authorization in a throwaway release dir."""
    monkeypatch.setenv("POLISCOPIC_RELEASE_DIR", str(tmp_path / "release"))
    monkeypatch.setenv("POLISCOPIC_AUDIT_DIR", str(tmp_path / "audit"))
    from scripts.ops import operation_authorization as oa

    now = datetime.now(timezone.utc)
    plan = oa.build_plan(
        operation="OP-RECON", operation_id="mode-test", entry_point=ENTRY,
        scope=SCOPE, code_paths=[], rollback_owner="Tester",
        not_before=now - timedelta(minutes=1), not_after=now + timedelta(days=365),
        mode="upsert")
    oa.write_plan(plan)
    oa.record_authorization(plan, verbatim_approval="Approved for this test",
                            author="Tester", source="human", mode="standing",
                            max_uses=400)
    return oa


# ── the core requirement ─────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["reconcile", "reconcile-only", "schema-only",
                                  "bootstrap-schema"])
def test_upsert_authorization_cannot_satisfy_other_modes(authorized, mode):
    """The whole point of the fix: a standing upsert auth must not cover these."""
    verdict = authorized.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                                  target="production", mode=mode)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == authorized.MODE_MISMATCH


def test_upsert_authorization_satisfies_upsert(authorized):
    verdict = authorized.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                                  target="production", mode="upsert")
    assert verdict["status"] == "ALLOWED", verdict


def test_missing_mode_refuses(authorized):
    for missing in (None, "", "   "):
        verdict = authorized.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                                      target="production", mode=missing)
        assert verdict["code"] == authorized.MODE_MISSING


def test_unknown_mode_refuses(authorized):
    for unknown in ("delete", "wipe", "UPSERT", "upsert "):
        verdict = authorized.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                                      target="production", mode=unknown)
        assert verdict["code"] == authorized.MODE_UNKNOWN


def test_scope_enforcement_is_preserved(authorized):
    """Mode binding must not weaken the existing scope check."""
    verdict = authorized.validate("OP-RECON", entry_point=ENTRY,
                                  scope=SCOPE[:-1], target="production",
                                  mode="upsert")
    assert verdict["code"] == authorized.SCOPE_MISMATCH


# ── the same rules through the real interlock path ───────────────────────


def test_interlock_enforces_mode(authorized):
    from production_interlock import check

    allowed = check("OP-RECON", entry_point=ENTRY, scope=SCOPE, mode="upsert")
    assert allowed["status"] == "ALLOWED", allowed
    for mode in ("reconcile", "reconcile-only", "schema-only", "bootstrap-schema"):
        verdict = check("OP-RECON", entry_point=ENTRY, scope=SCOPE, mode=mode)
        assert verdict["code"] == "MODE_MISMATCH", verdict


def test_interlock_refuses_missing_or_unknown_mode(authorized):
    from production_interlock import check

    assert check("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                 mode=None)["code"] == "MODE_MISSING"
    assert check("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                 mode="nonsense")["code"] == "MODE_UNKNOWN"


def test_read_only_operations_need_no_mode(authorized):
    from production_interlock import check

    verdict = check("OP-STATUS", mode=None)
    assert verdict["status"] == "ALLOWED"
    assert verdict["mutates_production"] is False


# ── a plan predating mode binding ────────────────────────────────────────


@pytest.fixture()
def legacy(tmp_path, monkeypatch):
    """A plan written before mode binding: it declares no mode at all."""
    monkeypatch.setenv("POLISCOPIC_RELEASE_DIR", str(tmp_path / "release"))
    monkeypatch.setenv("POLISCOPIC_AUDIT_DIR", str(tmp_path / "audit"))
    from scripts.ops import operation_authorization as oa

    now = datetime.now(timezone.utc)
    plan = oa.build_plan(
        operation="OP-RECON", operation_id="legacy-test", entry_point=ENTRY,
        scope=SCOPE, code_paths=[], rollback_owner="Tester",
        not_before=now - timedelta(minutes=1), not_after=now + timedelta(days=365),
        mode="upsert")
    del plan["mode"]                      # as written before mode binding existed
    plan["digest"] = oa.plan_digest(plan)
    oa.write_plan(plan)
    oa.record_authorization(plan, verbatim_approval="Approved for this test",
                            author="Tester", source="human", mode="standing",
                            max_uses=400)
    return oa


def test_legacy_plan_reads_as_upsert_only(legacy):
    """Strictly more restrictive than before: it can no longer cover a delete."""
    assert legacy.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                           mode="upsert")["status"] == "ALLOWED"
    for mode in ("reconcile", "reconcile-only", "schema-only", "bootstrap-schema"):
        verdict = legacy.validate("OP-RECON", entry_point=ENTRY, scope=SCOPE,
                                  mode=mode)
        assert verdict["code"] == legacy.MODE_MISMATCH


def test_legacy_mode_normalisation_is_documented():
    from scripts.ops import operation_authorization as oa

    assert oa.LEGACY_MODE == "upsert"
    assert oa.LEGACY_MODE in oa.KNOWN_MODES


# ── the sync entry point derives its mode truthfully ──────────────────────


def test_execution_mode_for_maps_each_flag():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from db.sync_prod import execution_mode_for

    assert execution_mode_for() == "upsert"
    assert execution_mode_for(reconcile=True) == "reconcile"
    assert execution_mode_for(reconcile_only=True) == "reconcile-only"
    assert execution_mode_for(schema_only=True) == "schema-only"
    assert execution_mode_for(bootstrap_schema=True) == "bootstrap-schema"
    assert execution_mode_for(reconcile_dry_run=True) is None


def test_execution_mode_for_refuses_conflicting_flags():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from db.sync_prod import execution_mode_for

    for kwargs in ({"reconcile": True, "schema_only": True},
                   {"reconcile": True, "reconcile_only": True},
                   {"schema_only": True, "bootstrap_schema": True}):
        with pytest.raises(ValueError):
            execution_mode_for(**kwargs)


def test_known_modes_cover_the_sync_modes():
    from scripts.ops import operation_authorization as oa

    for mode in ("upsert", "reconcile", "reconcile-only", "schema-only",
                 "bootstrap-schema"):
        assert mode in oa.KNOWN_MODES


# ── every production-mutation entry point declares its mode ───────────────


MUTATING_ENTRY_POINTS = (
    "scripts/db/sync_prod.py",
    "scripts/editorial_sync.py",
    "scripts/body_code_merge_prod.py",
    "scripts/db/cleanup_prod_db.py",
    "scripts/db/backfill_supporting_documents.py",
    "scripts/db/migrate_prod_db.py",
)


@pytest.mark.parametrize("relative", MUTATING_ENTRY_POINTS)
def test_mutating_entry_point_declares_a_mode(relative):
    """No production-mutation entry point may reach the interlock without a mode.

    Read-only calls (OP-STATUS / OP-PREFLIGHT) legitimately pass no mode, and are
    skipped — the requirement is about MUTATING paths.
    """
    tree = ast.parse((ROOT / relative).read_text())
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in {
                "require_production_interlock", "_interlock_check"}:
            calls.append(node)
        elif (isinstance(func, ast.Attribute) and func.attr == "check"
              and isinstance(func.value, ast.Name)
              and "interlock" in func.value.id):
            calls.append(node)
    assert calls, f"{relative} does not call the interlock at all"

    def always_read_only(argument) -> bool:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value in READ_ONLY_OPS
        if isinstance(argument, ast.IfExp):
            return always_read_only(argument.body) and always_read_only(argument.orelse)
        return False

    for call in calls:
        if call.args and always_read_only(call.args[0]):
            continue
        keywords = {kw.arg for kw in call.keywords}
        assert "mode" in keywords, (
            f"{relative} calls the interlock without declaring a mode "
            f"(line {call.lineno})")
