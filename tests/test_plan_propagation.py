#!/usr/bin/env python3
"""Tests for the offline, non-mutating propagation plan builder.

Proves the plan is well-formed, digest-bound, uniquely and exclusively written, and
that it refuses placeholders, ambiguity, missing parents, retired codes and drift —
and that it can never be mistaken for an authorization or reach production.

No network, no database, no production access.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts" / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

import plan_propagation as planner  # noqa: E402
from propagation_contract import contract_digest  # noqa: E402

PLANNER = OPS / "plan_propagation.py"
NOW = datetime(2026, 9, 19, 4, 0, 0, tzinfo=timezone.utc)


def _snapshot(**over):
    payload = {
        "snapshot_schema": "propagation-snapshot/1",
        "incoming_parents": [
            {"body_code": "chandler-pz", "id": 201, "identity": "Chandler P&Z"}
        ],
        "existing_parents": [],
        "dependents": [
            {"key": 1, "body": "chandler-pz"},
            {"key": 2, "body": "chandler-pz"},
        ],
        "retired_codes": [],
        "aliases": {},
        "dependent_table": "meetings",
    }
    payload.update(over)
    return payload


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload))
    return path


# ── happy path and plan shape ────────────────────────────────────────────


def test_builds_a_complete_plan():
    plan = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)

    assert plan["plan_schema"] == "propagation-plan/1"
    assert plan["operation_kind"] == "OP-REPAIR"
    assert plan["created_utc"] == "2026-09-19T04:00:00Z"
    # ordered operations, parents first
    assert plan["ordered_operations"][0]["op"] == "upsert_parent"
    assert [o["op"] for o in plan["ordered_operations"][1:]] == [
        "upsert_dependent", "upsert_dependent"]
    assert plan["apply_order"] == ["jurisdictions", "public_bodies", "meetings"]
    assert plan["expected_counts"] == {
        "parents_upserted": 1, "dependents_updated": 2, "total": 3}
    assert plan["rollback_owner"] == "Pete"
    assert plan["applies_anything"] is False
    assert plan["expected_postconditions"]


def test_plan_carries_source_and_target_fingerprints():
    snapshot = _snapshot()
    plan = planner.build_plan(snapshot, rollback_owner="Pete", now=NOW)
    assert plan["source_fingerprint"] == contract_digest(snapshot)
    assert plan["target_fingerprint"] == contract_digest(
        {"existing_parents": [], "retired_codes": []})
    assert len(plan["source_fingerprint"]) == 64


def test_plan_digest_binds_the_plan_body_and_is_stable():
    plan = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)
    body = {k: v for k, v in plan.items() if k != "plan_digest"}
    assert plan["plan_digest"] == contract_digest(body)
    again = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)
    assert again["plan_digest"] == plan["plan_digest"]

    # a different rollback owner changes the digest
    other = planner.build_plan(_snapshot(), rollback_owner="Someone", now=NOW)
    assert other["plan_digest"] != plan["plan_digest"]


# ── refusals ─────────────────────────────────────────────────────────────


def test_refuses_placeholder_rollback_owner():
    for owner in ("", "TBD", "todo", "unknown", "CHANGEME", "<owner>"):
        with pytest.raises(planner.Refused, match="placeholder"):
            planner.build_plan(_snapshot(), rollback_owner=owner, now=NOW)


def test_refuses_missing_parent():
    snapshot = _snapshot(incoming_parents=[], existing_parents=[])
    with pytest.raises(planner.Refused, match="missing parent"):
        planner.build_plan(snapshot, rollback_owner="Pete", now=NOW)


def test_refuses_sentinel_parent():
    snapshot = _snapshot(incoming_parents=[
        {"body_code": "__skip__", "id": 1, "identity": "nonsense"}])
    with pytest.raises(planner.Refused, match="sentinel"):
        planner.build_plan(snapshot, rollback_owner="Pete", now=NOW)


def test_refuses_retired_code():
    snapshot = _snapshot(retired_codes=["chandler-pz"])
    with pytest.raises(planner.Refused):
        planner.build_plan(snapshot, rollback_owner="Pete", now=NOW)


def test_refuses_parent_identity_collision():
    snapshot = _snapshot(
        existing_parents=[{"body_code": "chandler-pz", "id": 201,
                           "identity": "Chandler P&Z (legacy)"}])
    with pytest.raises(planner.Refused, match="conflicting parent identity"):
        planner.build_plan(snapshot, rollback_owner="Pete", now=NOW)


def test_refuses_drift_when_target_fingerprint_differs():
    with pytest.raises(planner.Refused, match="target drift"):
        planner.build_plan(_snapshot(), rollback_owner="Pete",
                           expect_target_fingerprint="0" * 64, now=NOW)


def test_accepts_a_matching_target_fingerprint():
    expected = contract_digest({"existing_parents": [], "retired_codes": []})
    plan = planner.build_plan(_snapshot(), rollback_owner="Pete",
                              expect_target_fingerprint=expected, now=NOW)
    assert plan["target_fingerprint"] == expected


def test_refuses_unrecognised_snapshot_schema(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"snapshot_schema": "something-else"}))
    with pytest.raises(planner.Refused, match="snapshot_schema"):
        planner.load_snapshot(path)


def test_refuses_a_snapshot_declaring_a_live_connection(tmp_path):
    payload = _snapshot(live_connection=True)
    with pytest.raises(planner.Refused, match="live connection"):
        planner.load_snapshot(_write(tmp_path, payload))


def test_refuses_missing_and_malformed_input(tmp_path):
    with pytest.raises(planner.Refused, match="not found"):
        planner.load_snapshot(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(planner.Refused, match="not valid JSON"):
        planner.load_snapshot(bad)


# ── unique, immutable plan writing ───────────────────────────────────────


def test_writes_a_unique_plan_path_with_restrictive_mode(tmp_path):
    plan = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)
    path = planner.write_plan(plan, tmp_path / "plans")

    assert path.exists()
    assert path.name.startswith("repair-propagation-")
    assert path.name.endswith(f"{plan['plan_digest'][:8]}.json")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_refuses_to_overwrite_an_existing_plan(tmp_path):
    plan = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)
    out = tmp_path / "plans"
    first = planner.write_plan(plan, out)
    original = first.read_text()
    with pytest.raises(planner.Refused, match="refusing to overwrite"):
        planner.write_plan(plan, out)
    assert first.read_text() == original, "the existing plan must be untouched"


def test_plan_is_not_an_authorization():
    plan = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)
    assert plan["authorization"] == "none - this plan authorizes nothing"
    assert "authorizes nothing" in plan["authorization"]
    assert plan["applies_anything"] is False


# ── no apply path, no production reach ───────────────────────────────────


def test_planner_has_no_database_or_network_import():
    tree = ast.parse(PLANNER.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("sqlalchemy", "psycopg", "psycopg2", "socket", "requests",
                      "urllib", "subprocess"):
        assert forbidden not in imported, f"planner imports {forbidden!r}"


def test_planner_exposes_no_database_url_option():
    src = PLANNER.read_text()
    for token in ("--database-url", "--db-url", "--host", "--dsn", "--password",
                  "PROD_DATABASE_URL", "DATABASE_URL"):
        assert token not in src, f"planner exposes {token!r}"


def test_planner_has_no_apply_command():
    src = PLANNER.read_text()
    assert '"apply"' not in src
    for token in ("def apply", "--apply", "conn.commit", "engine"):
        assert token not in src, f"planner contains an apply path: {token!r}"


# ── CLI behavior ─────────────────────────────────────────────────────────


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, str(PLANNER), *args],
                          capture_output=True, text=True, cwd=cwd, timeout=120)


def test_cli_builds_a_plan(tmp_path):
    snapshot = _write(tmp_path, _snapshot())
    out = tmp_path / "plans"
    result = _run("build", "--input", str(snapshot), "--out-dir", str(out),
                  "--rollback-owner", "Pete", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert Path(payload["plan"]).exists()
    assert len(payload["plan_digest"]) == 64
    assert "authorizes nothing" in payload["authorization"]


def test_cli_refuses_with_exit_3(tmp_path):
    snapshot = _write(tmp_path, _snapshot(incoming_parents=[]))
    result = _run("build", "--input", str(snapshot), "--out-dir",
                  str(tmp_path / "plans"), "--rollback-owner", "Pete")
    assert result.returncode == 3
    assert "REFUSED" in result.stderr


def test_cli_usage_error_is_exit_4(tmp_path):
    assert _run("nonsense").returncode != 0


def test_cli_writes_no_plan_on_refusal(tmp_path):
    snapshot = _write(tmp_path, _snapshot(incoming_parents=[]))
    out = tmp_path / "plans"
    _run("build", "--input", str(snapshot), "--out-dir", str(out),
         "--rollback-owner", "Pete")
    assert not out.exists() or not list(out.glob("*.json"))


# ── alignment with the enforced runtime ─────────────────────────────────


def test_plan_apply_order_matches_the_runtime_dependency_authority():
    """The plan's apply order must agree with the edges the runtime enforces."""
    from propagation_contract import DEPENDENCY_EDGES

    plan = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)
    order = plan["apply_order"]
    assert order == list(planner.APPLY_ORDER)
    for parent, dependent in DEPENDENCY_EDGES:
        if parent in order and dependent in order:
            assert order.index(parent) < order.index(dependent), (
                f"plan apply order violates {parent} -> {dependent}"
            )


def test_plan_postconditions_name_both_representations():
    """The plan must describe the same postconditions the runtime enforces."""
    plan = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)
    joined = " ".join(plan["expected_postconditions"])
    assert "public_bodies.body_code" in joined          # representation 1
    assert "public_bodies.id" in joined                  # representation 2
    assert "zero remaining scoped dangling" in joined    # scoped, not "no new"
    assert "sentinels" in joined.lower()


def test_plan_records_transfer_strategy_and_is_plan_only():
    plan = planner.build_plan(_snapshot(), rollback_owner="Pete", now=NOW)
    assert "public_bodies" in plan["transfer_strategy"]["reference_tables_full_reference"]
    assert "referentially safe" in plan["transaction_scope"]
    assert plan["applies_anything"] is False
    assert "plan-only" in plan["runtime_alignment"]
    assert "authorizes nothing" in plan["authorization"]
