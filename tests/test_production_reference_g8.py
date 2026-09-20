"""Focused offline tests for the operation-specific G8 gate."""

from __future__ import annotations

import hashlib
import importlib
import os
import stat
import sys
import copy
import json
from types import MappingProxyType
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
sys.path.insert(0, str(OPS))
G = importlib.import_module("production_reference_g8")
NOW = datetime(2026, 9, 19, 17, 0, tzinfo=timezone.utc)


def artifact(path: Path, body: dict) -> dict:
    body = copy.deepcopy(body)
    value = {**body, "digest": G.digest(body)}
    path.write_bytes(G.canonical_bytes(value))
    os.chmod(path, 0o600)
    return value


def pair(tmp_path: Path):
    operations = []
    for table, count in G.EXPECTED_COUNTS["by_table"].items():
        for row_id in range(1, count + 1):
            operations.append({"kind": "UPDATE", "table": table,
                               "primary_key": {"id": row_id},
                               "set": {"public_body_id": 37},
                               "preimage_digest": f"{row_id:064x}"[-64:]})
    g7_path = tmp_path / "g7.json"
    g7 = artifact(g7_path, {
        "schema": G.G7_SCHEMA, "operation": "OP-REPAIR",
        "status": G.READY_STATUS,
        "authorization": "none - this plan authorizes nothing",
        "applies_anything": False, "created_at": "2026-09-19T16:00:00Z",
        "expires_at": "2026-09-19T19:00:00Z", "nonce": "1" * 64,
        "bindings": {"exact_commit": "a" * 40,
                     "g6_proposal_preimages_digest": "c" * 64},
        "target": {"database": "poliscopic", "configured_host": "db.example",
                   "configured_port": 25060, "server_address": "192.0.2.1",
                   "server_port": 25060, "cluster_system_identifier": "123"},
        "schema_sha256": "b" * 64,
        "preimage_digest": "c" * 64, "quarantine_digest": "d" * 64,
        "integrity_before": {"orphans": 7973},
        "integrity_after": {"orphans": 0},
        "operations": operations,
        "operation_counts": G.EXPECTED_COUNTS, "scope": G.EXPECTED_SCOPE,
    })
    text = f"I approve OP-REPAIR for the exact G7 digest {g7['digest']}"
    g8_path = tmp_path / "g8.json"
    g8 = artifact(g8_path, {
        "schema": G.G8_SCHEMA, "operation": "OP-REPAIR",
        "g7_digest": g7["digest"], "nonce": g7["nonce"],
        "exact_commit": g7["bindings"]["exact_commit"],
        "scope": G.EXPECTED_SCOPE, "operation_counts": G.EXPECTED_COUNTS,
        "author": {"kind": "human", "name": "Peter Mains"},
        "approved_at": "2026-09-19T16:30:00Z",
        "expires_at": "2026-09-19T18:00:00Z",
        "approval": {"verbatim": text,
                     "sha256": hashlib.sha256(text.encode()).hexdigest()},
    })
    return g7_path, g8_path, g7, g8


def rewrite(path: Path, value: dict):
    body = {key: item for key, item in value.items() if key != "digest"}
    path.unlink()
    return artifact(path, body)


def test_exact_artifacts_validate(tmp_path):
    g7_path, g8_path, g7, g8 = pair(tmp_path)
    assert G.validate(g7_path, g8_path, NOW) == (g7, g8)


def test_current_g7_v1_candidate_is_refused(tmp_path):
    g7_path, g8_path, _, _ = pair(tmp_path)
    value = __import__("json").loads(g7_path.read_text())
    value["schema"] = "production-reference-op-repair-g7/1"
    rewrite(g7_path, value)
    with pytest.raises(G.Refused, match="schema"):
        G.validate(g7_path, g8_path, NOW)


@pytest.mark.parametrize("defect", ["digest", "nonce", "commit", "scope", "counts",
                                    "author", "operation", "text_hash", "paraphrase",
                                    "denial", "expired"])
def test_authorization_mismatches_refuse(tmp_path, defect):
    g7_path, g8_path, g7, g8 = pair(tmp_path)
    body = {key: value for key, value in g8.items() if key != "digest"}
    if defect == "digest": body["g7_digest"] = "0" * 64
    elif defect == "nonce": body["nonce"] = "2" * 64
    elif defect == "commit": body["exact_commit"] = "b" * 40
    elif defect == "scope": body["scope"]["other_changes"] = True
    elif defect == "counts": body["operation_counts"]["total"] = 7972
    elif defect == "author": body["author"] = {"kind": "agent", "name": "Some Agent"}
    elif defect == "operation": body["operation"] = "OP-CODE"
    elif defect == "text_hash": body["approval"]["sha256"] = "0" * 64
    elif defect == "paraphrase":
        body["approval"]["verbatim"] = "Approved as discussed"
        body["approval"]["sha256"] = hashlib.sha256(b"Approved as discussed").hexdigest()
    elif defect == "denial":
        text = f"I do not approve OP-REPAIR for {g7['digest']}"
        body["approval"] = {"verbatim": text,
                            "sha256": hashlib.sha256(text.encode()).hexdigest()}
    else: body["expires_at"] = "2026-09-19T17:00:00Z"
    rewrite(g8_path, body)
    with pytest.raises(G.Refused):
        G.validate(g7_path, g8_path, NOW)


@pytest.mark.parametrize("kind", ["mode", "symlink", "noncanonical"])
def test_artifact_files_fail_closed(tmp_path, kind):
    g7_path, g8_path, _, _ = pair(tmp_path)
    if kind == "mode":
        os.chmod(g8_path, 0o644)
    elif kind == "symlink":
        real = tmp_path / "real.json"
        g8_path.rename(real)
        g8_path.symlink_to(real)
    else:
        value = __import__("json").loads(g8_path.read_text())
        g8_path.write_text(__import__("json").dumps(value, indent=2))
        os.chmod(g8_path, 0o600)
    with pytest.raises(G.Refused):
        G.validate(g7_path, g8_path, NOW)


def test_claim_is_atomic_durable_mode_0600_and_single_use(tmp_path):
    g7_path, g8_path, g7, _ = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    claim = G.claim(g7_path, g8_path, state, NOW)
    path = state / f"{g7['digest']}.{g7['nonce']}.CLAIMED.json"
    assert claim["state"] == "CLAIMED"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(G.Refused, match="already consumed"):
        G.claim(g7_path, g8_path, state, NOW)


@pytest.mark.parametrize("outcome", sorted(G.TERMINALS))
def test_exactly_one_terminal_is_append_only(tmp_path, outcome):
    g7_path, g8_path, g7, g8 = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    G.claim(g7_path, g8_path, state, NOW)
    terminal = G.write_terminal(state, g7, g8["digest"], outcome,
                                {"safe_code": "TEST"}, NOW)
    assert terminal["state"] == outcome
    assert G.inspect_state(state, g7) == outcome
    with pytest.raises(G.Refused):
        G.failed(state, g7, g8["digest"], {"safe_code": "SECOND"}, NOW)
    with pytest.raises(G.Refused):
        G.claim(g7_path, g8_path, state, NOW)


def test_ambiguous_competing_terminals_refuse(tmp_path):
    g7_path, g8_path, g7, g8 = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    G.claim(g7_path, g8_path, state, NOW)
    G.success(state, g7, g8["digest"], {}, NOW)
    stem = f"{g7['digest']}.{g7['nonce']}"
    (state / f"{stem}.FAILED.json").write_text("competing")
    with pytest.raises(G.Refused, match="ambiguous"):
        G.inspect_state(state, g7)


def test_terminal_without_claim_refuses(tmp_path):
    _, _, g7, g8 = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    with pytest.raises(G.Refused, match="claim"):
        G.success(state, g7, g8["digest"], {}, NOW)


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o770])
def test_insecure_state_directory_mode_refuses(tmp_path, mode):
    g7_path, g8_path, _, _ = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(mode)
    with pytest.raises(G.Refused, match="0700"):
        G.claim(g7_path, g8_path, state, NOW)


def test_wrong_owner_state_directory_refuses(tmp_path, monkeypatch):
    g7_path, g8_path, _, _ = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    monkeypatch.setattr(G.os, "getuid", lambda: state.stat().st_uid + 1)
    with pytest.raises(G.Refused, match="current-user-owned"):
        G.claim(g7_path, g8_path, state, NOW)


def test_authorize_returns_frozen_context_only_after_claim(tmp_path):
    g7_path, g8_path, g7, _ = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    context = G.authorize(g7_path, g8_path, state, NOW)
    assert type(context) is G.AuthorizedRepairContext
    assert context.attempt_id
    assert len(context.operations) == 7973
    assert G.inspect_state(state, g7) == "CLAIMED"
    with pytest.raises(TypeError):
        context.bindings["exact_commit"] = "b" * 40
    with pytest.raises(G.Refused, match="already consumed"):
        G.authorize(g7_path, g8_path, state, NOW)


def test_context_direct_construction_refuses(tmp_path):
    with pytest.raises(G.Refused, match="only be created"):
        G.AuthorizedRepairContext({}, {}, "x", "OP-REPAIR", (), "x", "x", {},
                                  {}, {}, {}, "x", "x", {}, {}, tmp_path)


@pytest.mark.parametrize("field", ["preimage_digest", "quarantine_digest",
                                    "integrity_before", "integrity_after"])
def test_required_executor_plan_fields_refuse_when_absent(tmp_path, field):
    g7_path, g8_path, _, g8 = pair(tmp_path)
    value = json.loads(g7_path.read_text())
    value.pop(field)
    changed = rewrite(g7_path, value)
    g8_body = {key: item for key, item in g8.items() if key != "digest"}
    g8_body["g7_digest"] = changed["digest"]
    text = f"I approve OP-REPAIR for the exact G7 digest {changed['digest']}"
    g8_body["approval"] = {"verbatim": text,
                           "sha256": hashlib.sha256(text.encode()).hexdigest()}
    rewrite(g8_path, g8_body)
    with pytest.raises(G.Refused, match=field):
        G.validate(g7_path, g8_path, NOW)


def test_preimage_must_equal_bound_g6_digest(tmp_path):
    g7_path, g8_path, _, g8 = pair(tmp_path)
    value = json.loads(g7_path.read_text())
    value["bindings"]["g6_proposal_preimages_digest"] = "e" * 64
    changed = rewrite(g7_path, value)
    g8_body = {key: item for key, item in g8.items() if key != "digest"}
    g8_body["g7_digest"] = changed["digest"]
    text = f"I approve OP-REPAIR for the exact G7 digest {changed['digest']}"
    g8_body["approval"] = {"verbatim": text,
                           "sha256": hashlib.sha256(text.encode()).hexdigest()}
    rewrite(g8_path, g8_body)
    with pytest.raises(G.Refused, match="bound G6"):
        G.validate(g7_path, g8_path, NOW)


def test_assert_claimed_and_safe_context_terminal_adapter(tmp_path):
    g7_path, g8_path, g7, _ = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    context = G.authorize(g7_path, g8_path, state, NOW)
    assert G.assert_claimed_context(context) is context
    body = {"schema": "production-reference-repair-terminal/1",
            "operation": "OP-REPAIR", "attempt_id": context.attempt_id,
            "nonce": context.nonce, "terminal": "SUCCESS",
            "target": dict(context.target), "bindings": dict(context.bindings)}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    values = G._freeze({**body, "digest": hashlib.sha256(encoded).hexdigest()})
    receipt = type("Receipt", (), {"values": values})()
    assert context.write_terminal(receipt)["state"] == "SUCCESS"
    with pytest.raises(G.Refused, match="claim"):
        G.assert_claimed_context(context)


def test_context_terminal_rejects_mutable_receipt(tmp_path):
    g7_path, g8_path, _, _ = pair(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    context = G.authorize(g7_path, g8_path, state, NOW)
    receipt = type("Receipt", (), {"values": {"terminal": "FAILED"}})()
    with pytest.raises(G.Refused, match="frozen"):
        G.write_context_terminal(context, receipt)
