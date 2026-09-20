"""Focused tests for offline operation-specific G3/G7 preparation."""

from __future__ import annotations

import copy
import importlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
for candidate in (ROOT, ROOT / "scripts", OPS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

P = importlib.import_module("production_preflight")
G = importlib.import_module("production_reference_g7")

NOW = datetime(2026, 9, 19, 17, 0, tzinfo=timezone.utc)
TARGET = {"database": "poliscopic", "configured_host": "db.example",
          "configured_port": 25060, "server_address": "192.0.2.1",
          "server_port": 25060, "cluster_system_identifier": "123"}


def test_reviewed_candidate_digest_is_pinned():
    assert G.EXPECTED_CANDIDATE_DIGEST == (
        "13dba3455e03a423619405d75647143acef9bab9aca0b6879726fd919c0085bc")


def test_live_reviewed_candidate_has_exact_operations_and_quarantine_binding():
    path = ROOT / "data/audit/20260919T164500Z-production-reference-repair-candidate.json"
    candidate = G._load(path, "candidate", G.CANDIDATE_SCHEMA)
    operations, quarantine_digest = G._validate_candidate(candidate)
    assert len(operations) == 7973
    assert quarantine_digest == P.digest(candidate["quarantine"])


def artifact(path: Path, body: dict) -> dict:
    value = {**body, "digest": P.digest(body)}
    path.write_bytes(P.canonical_bytes(value))
    os.chmod(path, 0o600)
    return value


def population():
    proposals = []
    for table, count in G.EXPECTED.items():
        for row_id in range(1, count + 1):
            actual_id = row_id if table == "meetings" else 100000 + row_id
            proposals.append({"category": f"{table}.public_body_id.dangling_or_null",
                              "table": table, "primary_key": {"id": actual_id},
                              "before": {"id": actual_id, "public_body_id": None,
                                         "body": "mesa-pz"},
                              "set": {"public_body_id": 37}})
    quarantine = [{"category": "meetings.body.sentinel", "table": None,
                   "primary_key": {"id": 200000 + row_id},
                   "reason": "category_not_safe_for_automatic_repair"}
                  for row_id in range(G.EXPECTED_QUARANTINE)]
    return proposals, quarantine


def chain(tmp_path: Path):
    proposals, quarantine = population()
    g5_path = tmp_path / "g5.json"
    g5 = artifact(g5_path, {"schema": G.G5_SCHEMA, "operation": "OP-PREFLIGHT",
                            "status": "VALID", "target": TARGET,
                            "schema_snapshot": {"sha256": "s" * 64}})
    evidence_path = tmp_path / "evidence.json"
    evidence = artifact(evidence_path, {
        "schema": G.EVIDENCE_SCHEMA, "operation": "OP-PREFLIGHT",
        "status": "VALID", "target": TARGET,
        "g5_binding": {"path": str(g5_path), "digest": g5["digest"]}})
    candidate_path = tmp_path / "candidate.json"
    candidate = artifact(candidate_path, {
        "schema": G.CANDIDATE_SCHEMA, "operation": "OP-REPAIR",
        "status": "CANDIDATE-NOT-AUTHORIZABLE", "apply_blocked": True,
        "target": TARGET,
        "bindings": {"reference_evidence": {"digest": evidence["digest"]},
                     "g5": {"digest": g5["digest"], "path": str(g5_path)}},
        "proposals": proposals, "quarantine": quarantine,
        "counts": {"proposals": G.EXPECTED_TOTAL,
                   "quarantine": G.EXPECTED_QUARANTINE}})
    G.EXPECTED_CANDIDATE_DIGEST = candidate["digest"]
    baseline_path = tmp_path / "baseline.json"
    baseline = artifact(baseline_path, {
        "schema": "production-g6-baseline/1", "target": TARGET,
        "schema_sha256": "s" * 64,
        "proposal_preimages": {"count": G.EXPECTED_TOTAL,
                               "by_table": G.EXPECTED, "digest": "p" * 64}})
    dump_path = tmp_path / "fresh.dump"
    dump_path.write_bytes(b"fresh production custom dump")
    dump_sha = G.hashlib.sha256(dump_path.read_bytes()).hexdigest()
    g6_path = tmp_path / "g6.json"
    g6 = artifact(g6_path, {
        "schema": G.G6_SCHEMA, "operation": "OP-REPAIR", "status": "VALID",
        "created_at": "2026-09-19T16:00:00Z",
        "expires_at": "2026-09-19T20:00:00Z", "target": TARGET,
        "candidate_binding": {
            "semantic_digest": candidate["digest"],
            "raw_sha256": G.hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            "upstream_bindings": candidate["bindings"]},
        "baseline_path": str(baseline_path), "baseline_digest": baseline["digest"],
        "proposal_preimages": baseline["proposal_preimages"],
        "dump": {"path": str(dump_path), "bytes": dump_path.stat().st_size,
                 "sha256": dump_sha},
        "off_volume": {"host": "development-host", "machine": "DEVHOST",
                       "volume": "volume-1", "retained": True,
                       "bytes": dump_path.stat().st_size, "sha256": dump_sha},
        "scratch": {key: True for key in
                    ("restored_from_off_volume", "counts_match", "schema_match",
                     "integrity_match", "force_dropped", "absence_proved")},
        "comparisons": {name: True for name in G.COMPARISON_KEYS},
        "problems": []})
    manifest_path = tmp_path / "manifest.json"
    manifest = artifact(manifest_path, {
        "schema": G.MANIFEST_SCHEMA, "operation": "OP-REPAIR",
        "exact_commit": "a" * 40, "authority": "preparation-surface-only",
        "content_authority": "committed-blobs-only",
        "mutable_paths_authority": False, "files": [], "file_count": 0,
        "executor_included": False, "applies_anything": False})
    return {"candidate_path": candidate_path, "evidence_path": evidence_path,
            "g5_path": g5_path, "g6_path": g6_path,
            "manifest_path": manifest_path, "candidate": candidate,
            "evidence": evidence, "g5": g5, "g6": g6, "manifest": manifest}


@pytest.fixture
def git_clean(monkeypatch):
    def fake(*args):
        if args[:2] == ("status", "--porcelain=v1"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return "a" * 40 + "\n"
        raise AssertionError(args)
    monkeypatch.setattr(G, "_run_git", fake)
    monkeypatch.setattr(G, "_verify_manifest_checkout", lambda manifest: None)


def build(tmp_path, git_clean, **overrides):
    paths = chain(tmp_path)
    output = tmp_path / "g7.json"
    kwargs = {key: paths[key] for key in
              ("candidate_path", "evidence_path", "g5_path", "g6_path",
               "manifest_path")}
    kwargs.update({"output": output, "rollback_owner": "Peter Mains",
                   "expires_at": "2026-09-19T19:00:00Z", "now": NOW,
                   "nonce": "1" * 64})
    kwargs.update(overrides)
    return paths, output, G.build_g7(**kwargs)


def test_builds_exact_update_population_and_remains_g8_blocked(tmp_path, git_clean):
    paths, output, result = build(tmp_path, git_clean)
    assert result["status"] == "G7-CANDIDATE-EXECUTOR-MISSING"
    assert result["apply_blocked"] is True
    assert result["authorization"] == "none - this plan authorizes nothing"
    assert result["applies_anything"] is False
    assert result["operation_counts"] == {"total": 7973, "by_table": G.EXPECTED}
    assert len(result["operations"]) == 7973
    assert all(op["kind"] == "UPDATE" and set(op["set"]) == {"public_body_id"}
               for op in result["operations"])
    assert result["quarantine"]["count"] == 3621
    assert result["quarantine"]["excluded_from_operations"] is True
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert G.verify_g7(output, **{key: paths[key] for key in
        ("candidate_path", "evidence_path", "g5_path", "g6_path",
         "manifest_path")}, now=NOW)["digest"] == result["digest"]


@pytest.mark.parametrize("owner", ["", "TBD", "unknown", "Owner"])
def test_placeholder_rollback_owner_refused(tmp_path, git_clean, owner):
    with pytest.raises(G.Refused, match="rollback owner"):
        build(tmp_path, git_clean, rollback_owner=owner)


def test_expiry_over_four_hours_refused(tmp_path, git_clean):
    with pytest.raises(G.Refused, match="expiry"):
        build(tmp_path, git_clean, expires_at="2026-09-19T21:01:00Z")


def test_stale_or_expired_g6_refused(tmp_path, git_clean):
    paths = chain(tmp_path)
    g6 = json.loads(paths["g6_path"].read_text())
    body = {key: value for key, value in g6.items() if key != "digest"}
    body["created_at"] = "2026-09-19T12:00:00Z"
    paths["g6_path"].unlink()
    artifact(paths["g6_path"], body)
    with pytest.raises(G.Refused, match="not fresh"):
        G.build_g7(**{key: paths[key] for key in
            ("candidate_path", "evidence_path", "g5_path", "g6_path",
             "manifest_path")}, output=tmp_path / "out.json",
            rollback_owner="Peter Mains", expires_at="2026-09-19T19:00:00Z",
            now=NOW, nonce="2" * 64)


@pytest.mark.parametrize("defect", [
    "zero_dump", "off_volume_hash", "off_volume_retained", "same_host",
    "scratch_restore", "scratch_drop", "comparison_keys", "preimage_count",
    "upstream_bindings",
])
def test_each_required_g6_proof_is_fail_closed(tmp_path, git_clean, defect):
    paths = chain(tmp_path)
    g6 = json.loads(paths["g6_path"].read_text())
    body = {key: value for key, value in g6.items() if key != "digest"}
    if defect == "zero_dump":
        Path(body["dump"]["path"]).write_bytes(b"")
        body["dump"]["bytes"] = 0
        body["dump"]["sha256"] = G.hashlib.sha256(b"").hexdigest()
    elif defect == "off_volume_hash":
        body["off_volume"]["sha256"] = "0" * 64
    elif defect == "off_volume_retained":
        body["off_volume"]["retained"] = False
    elif defect == "same_host":
        body["off_volume"]["host"] = TARGET["configured_host"]
    elif defect == "scratch_restore":
        body["scratch"]["restored_from_off_volume"] = False
    elif defect == "scratch_drop":
        body["scratch"]["absence_proved"] = False
    elif defect == "comparison_keys":
        body["comparisons"].pop("proposal_preimages")
    elif defect == "preimage_count":
        body["proposal_preimages"]["count"] -= 1
    else:
        body["candidate_binding"]["upstream_bindings"] = {}
    paths["g6_path"].unlink()
    artifact(paths["g6_path"], body)
    with pytest.raises(G.Refused):
        G.build_g7(**{key: paths[key] for key in
            ("candidate_path", "evidence_path", "g5_path", "g6_path",
             "manifest_path")}, output=tmp_path / "out.json",
            rollback_owner="Peter Mains", expires_at="2026-09-19T19:00:00Z",
            now=NOW, nonce="4" * 64)


def test_operation_duplicate_and_quarantine_overlap_refused(tmp_path, git_clean):
    paths = chain(tmp_path)
    candidate = json.loads(paths["candidate_path"].read_text())
    body = {key: value for key, value in candidate.items() if key != "digest"}
    body["proposals"][-1] = copy.deepcopy(body["proposals"][0])
    paths["candidate_path"].unlink()
    changed = artifact(paths["candidate_path"], body)
    G.EXPECTED_CANDIDATE_DIGEST = changed["digest"]
    with pytest.raises(G.Refused, match="duplicate"):
        G.build_g7(**{key: paths[key] for key in
            ("candidate_path", "evidence_path", "g5_path", "g6_path",
             "manifest_path")}, output=tmp_path / "out.json",
            rollback_owner="Peter Mains", expires_at="2026-09-19T19:00:00Z",
            now=NOW, nonce="3" * 64)


def test_legacy_general_release_manifest_is_refused(tmp_path):
    legacy = tmp_path / "manifest.json"
    artifact(legacy, {"schema": "release-manifest-v1", "files": []})
    with pytest.raises(G.Refused, match="operation-specific"):
        G._load(legacy, "manifest", G.MANIFEST_SCHEMA)


def test_manifest_hashes_exact_committed_blobs_and_requires_clean_tree(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    relative = "scripts/ops/tool.py"
    path = repo / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"committed content\n")
    commit = "a" * 40

    def fake_git(*args):
        if args[:2] == ("status", "--porcelain=v1"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return commit + "\n"
        if args == ("ls-tree", commit, "--", relative):
            return f"100644 blob {'b' * 40}\t{relative}\n"
        raise AssertionError(args)

    monkeypatch.setattr(G, "REPO", repo)
    monkeypatch.setattr(G, "SURFACE", (relative,))
    monkeypatch.setattr(G, "_run_git", fake_git)
    monkeypatch.setattr(G, "_git_bytes", lambda *a: b"committed content\n")
    output = tmp_path / "manifest.json"
    result = G.build_manifest(output)
    assert result["exact_commit"] == commit
    assert result["files"][0]["sha256"] == G.hashlib.sha256(
        b"committed content\n").hexdigest()
    assert result["mutable_paths_authority"] is False
    assert result["authority"] == "preparation-surface-only"
    assert result["executor_included"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_manifest_refuses_dirty_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "_run_git", lambda *a: "?? new-file\n")
    with pytest.raises(G.Refused, match="not exactly clean"):
        G.build_manifest(tmp_path / "manifest.json")


def test_tampering_any_upstream_digest_breaks_independent_verification(
        tmp_path, git_clean):
    paths, output, _ = build(tmp_path, git_clean)
    evidence = json.loads(paths["evidence_path"].read_text())
    evidence["target"]["database"] = "other"
    paths["evidence_path"].write_text(json.dumps(evidence))
    with pytest.raises(G.Refused, match="digest mismatch"):
        G.verify_g7(output, **{key: paths[key] for key in
            ("candidate_path", "evidence_path", "g5_path", "g6_path",
             "manifest_path")}, now=NOW)


def test_no_apply_authorization_network_or_database_surface():
    source = (OPS / "production_reference_g7.py").read_text()
    assert "--apply" not in source
    assert "def apply" not in source
    assert "create_engine" not in source
    assert "requests" not in source
    assert '"authorization": "none - this plan authorizes nothing"' in source
