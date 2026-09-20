"""Focused tests for the Stage 1 gate-safety hardening fixes (P0-P2).

Isolated: no database, no network, no child process.
"""

from __future__ import annotations

import copy
import builtins
import io
import json
import os
import pathlib
import subprocess

import pytest

from scripts.entities import event_normalize_artifacts as artifacts
from scripts.entities.event_normalize_artifacts import (
    ARTIFACT_STATUSES,
    ArtifactCollision,
    finish,
    reserve_attempt,
    write_exclusive,
)
from scripts.entities.event_normalize_child_contract import (
    GUARANTEE,
    PRODUCER_COMMAND,
    PRODUCER_CWD,
    PRODUCER_EXECUTABLE,
    PRODUCER_SCRIPT,
    ChildContractError,
    child_environment,
    child_read_only_contract,
    compose_pgoptions,
    parse_pgoptions,
)
from scripts.entities.event_normalize_gate import attempt_paths
from scripts.entities.event_normalize_gate_runner import (
    ChildResult,
    plan_digest,
    run_attempt,
)
from scripts.entities.event_normalize_preflight import (
    ReadOnlyViolation,
    statement_write_problem,
)

DEV_URL = "postgresql://devuser:***@dev-host.internal:5432/poliscopic_dev"
PROD_URL = "postgresql://produser:***@db.b.db.ondigitalocean.com:25060/poliscopic"
PASSWORD = "***"
GUC = "default_transaction_read_only"


# -- P0: PGOPTIONS must yield exactly one authoritative assignment -------------


@pytest.mark.parametrize(
    "raw",
    [
        f"-c {GUC}=on -c {GUC}=off",                       # late off (the bypass)
        f"-c {GUC}=off -c {GUC}=on",                       # reversed
        f"-c {GUC}=on -c {GUC}=on",                        # duplicate identical
        f"--{GUC}=on --{GUC}=off",                         # alternate syntax
        f"-c{GUC}=on -c {GUC}=off",                        # attached -c form
        f"-c {GUC}=on -c {GUC}=maybe",                     # unrecognised value
    ],
)
def test_ambiguous_or_duplicate_pgoptions_fail_closed(raw):
    with pytest.raises(ChildContractError):
        compose_pgoptions(raw)


@pytest.mark.parametrize("raw", [f"-c {GUC}=off", f"-c {GUC}=false", "--default_transaction_read_only=no"])
def test_weakening_value_is_refused(raw):
    with pytest.raises(ChildContractError):
        compose_pgoptions(raw)


def test_exactly_one_assignment_is_emitted():
    composed = compose_pgoptions("-c statement_timeout=5000")
    assert composed.count(f"{GUC}=") == 1
    assert f"-c {GUC}=on" in composed
    assert "statement_timeout=5000" in composed          # unrelated option preserved


def test_already_read_only_is_normalised_not_duplicated():
    composed = compose_pgoptions(f"-c {GUC}=on")
    assert composed.count(f"{GUC}=") == 1


@pytest.mark.parametrize("raw", ["-c", "-c not_an_assignment", '-c x=" a"', "-c x=a;b"])
def test_malformed_or_ambiguous_syntax_fails_closed(raw):
    with pytest.raises(ChildContractError):
        parse_pgoptions(raw)


def test_empty_pgoptions_compose_to_a_single_assignment():
    assert compose_pgoptions("") == f"-c {GUC}=on"
    assert compose_pgoptions(None) == f"-c {GUC}=on"


# -- P1: target binding --------------------------------------------------------


def test_child_environment_pins_the_database_url():
    env, contract = child_environment(DEV_URL, {"PATH": "/usr/bin"})
    assert env["DATABASE_URL"] == DEV_URL
    assert env["POLISCOPIC_DB_TIER"] == "development"
    assert contract["target_redacted"] == "development postgresql dev-host.internal:5432/poliscopic_dev"


def test_contract_derives_class_from_the_url_only():
    assert child_read_only_contract(DEV_URL, {})["url_class"] == "development"
    assert child_read_only_contract("sqlite:////tmp/x.sqlite", {})["url_class"] == "local"
    with pytest.raises(ChildContractError):
        child_read_only_contract(PROD_URL, {})
    with pytest.raises(ChildContractError):
        child_read_only_contract("mysql://u:***@h/db", {})


def test_test_isolated_target_has_no_postgres_options():
    env, contract = child_environment("sqlite:////tmp/x.sqlite", {"PATH": "/usr/bin"})
    assert "PGOPTIONS" not in env
    assert env["DATABASE_URL"] == "sqlite:////tmp/x.sqlite"
    assert contract["database_enforced_read_only"] is False


def test_contract_records_an_honest_guarantee():
    contract = child_read_only_contract(DEV_URL, {})
    recorded = json.dumps(contract["guarantee"])
    assert "not a security boundary" in recorded or "NOT a security boundary" in recorded
    assert "non-bypassable" in GUARANTEE["strength"].lower()


def test_contract_never_carries_credentials():
    contract = child_read_only_contract(DEV_URL, {})
    rendered = json.dumps(contract)
    assert PASSWORD not in rendered
    assert "devuser" not in rendered


# -- P1: parent guard ----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) DELETE FROM meetings",
        "WITH x AS (SELECT 1) INSERT INTO meetings (id) VALUES (1)",
        "SELECT 1; DELETE FROM meetings",
        "CALL do_something()",
        "SELECT nextval('seq')",
        "SELECT setval('seq', 1)",
        "DO $$ BEGIN DELETE FROM meetings; END $$",
        "COPY meetings TO '/tmp/x'",
        "/* c */ UPDATE meetings SET public_body_id = 1",
        "REINDEX TABLE meetings",
    ],
)
def test_write_bearing_statements_are_detected(sql):
    assert statement_write_problem(sql) is not None


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1", "SELECT * FROM meetings WHERE id = 1", "WITH x AS (SELECT 1) SELECT * FROM x",
     "SHOW transaction_read_only", "PRAGMA table_info(x)"],
)
def test_read_only_statements_pass(sql):
    assert statement_write_problem(sql) is None


def test_pre_used_pooled_connection_cannot_bypass_the_guard():
    """guard_engine disposes the pool, so stale connections cannot be reused."""
    from sqlalchemy import create_engine
    from scripts.entities.event_normalize_preflight import guard_engine

    engine = create_engine("sqlite://")
    guard_engine(engine)
    assert engine.pool.status() is not None          # pool exists and was reset
    with pytest.raises(ReadOnlyViolation):
        with engine.begin() as conn:
            conn.execute(__import__("sqlalchemy").text("DELETE FROM t"))


# -- P2: pinned execution identity ---------------------------------------------


def test_execution_identity_is_absolute_and_pinned():
    for path in (PRODUCER_EXECUTABLE, PRODUCER_SCRIPT, PRODUCER_CWD):
        assert os.path.isabs(path), path
    assert PRODUCER_COMMAND[0] == PRODUCER_EXECUTABLE
    assert PRODUCER_COMMAND[2] == PRODUCER_SCRIPT
    assert PRODUCER_SCRIPT.endswith("scripts/entities/event_normalize.py")


# -- P2: atomic artifacts ------------------------------------------------------


def test_write_exclusive_refuses_to_overwrite(tmp_path):
    target = tmp_path / "a.json"
    write_exclusive(target, "first")
    with pytest.raises(ArtifactCollision):
        write_exclusive(target, "second")
    assert target.read_text() == "first"


def test_reserve_attempt_is_atomic(tmp_path):
    reserve_attempt(str(tmp_path / "run"))
    with pytest.raises(ArtifactCollision):
        reserve_attempt(str(tmp_path / "run"))


def test_finish_labels_partial_and_writes_the_result(tmp_path):
    paths = attempt_paths("t", base=str(tmp_path))
    outcome = finish({"run_id": "t"}, paths, "digest_mismatch")
    assert outcome["status"] == "digest_mismatch"
    assert outcome["partial"] is True
    assert json.loads(open(paths["result"]).read())["status"] == "digest_mismatch"


def test_finish_never_overwrites_an_existing_result(tmp_path):
    paths = attempt_paths("t2", base=str(tmp_path))
    finish({"run_id": "t2"}, paths, "blocked")
    second = finish({"run_id": "t2"}, paths, "success")
    assert second["result_artifact"] == "existing"


def test_every_terminal_status_is_known():
    for status in ("plan", "blocked", "digest_mismatch", "child_exit", "parse_error",
                   "postflight_error", "timeout", "success", "checks_failed"):
        assert status in ARTIFACT_STATUSES


# -- fingerprint ---------------------------------------------------------------


def test_fingerprint_manifest_includes_the_quarantine_module():
    """The behaviour carrier outside scripts/entities is fingerprinted, too."""
    from scripts.entities import detect_entities as detector
    from scripts.entities import producer_manifest

    phase = next(p for p in detector.PHASES if p["name"] == "event_pipeline")
    modules = tuple(phase["code_modules"])
    repo_root = pathlib.Path(__file__).resolve().parents[1]

    assert "scripts.kg.quarantine" in modules
    assert list(modules) == sorted(modules)
    assert len(set(modules)) == len(modules)
    assert all((repo_root / (m.replace(".", "/") + ".py")).exists() for m in modules)
    assert producer_manifest.undocumented_imports(phase, repo_root) == ()


def test_manifest_hash_changes_when_the_quarantine_module_changes(monkeypatch):
    """Mutating the module outside scripts/entities must move the aggregate."""
    from scripts.entities import detect_entities as detector

    phase = next(p for p in detector.PHASES if p["name"] == "event_pipeline")
    path = pathlib.Path(PRODUCER_CWD) / "scripts" / "kg" / "quarantine.py"
    original = path.read_bytes()
    baseline = detector._producer_metadata(phase)["code_sha256"]
    real_open = builtins.open

    def probe_open(file, mode="r", *args, **kwargs):
        if pathlib.Path(file) == path and mode == "rb":
            return io.BytesIO(original + b"\n# fingerprint probe\n")
        return real_open(file, mode, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "open", probe_open)
        mutated = detector._producer_metadata(phase)["code_sha256"]
    assert mutated != baseline
    assert detector._producer_metadata(phase)["code_sha256"] == baseline


# -- happy path stays non-spawning --------------------------------------------


class FakeSpawn:
    def __init__(self):
        self.calls = []

    def __call__(self, command, *, timeout, env=None, cwd=None):
        self.calls.append({"command": list(command), "env": dict(env or {}), "cwd": cwd})
        return ChildResult(1, "", "")          # nonzero: never a success path


def _preflight():
    return {
        "target": {"tier": "development", "url_class": "development",
                   "redacted": "development postgresql dev-host.internal:5432/poliscopic_dev"},
        "target_is_development": True, "eligible_work_items": 1, "failures_by_reason": {},
        "coverage_complete": True, "gate_tables": {"meetings": 1}, "integrity": {"o": 0},
        "extractions_total": 1, "quarantined_excluded": 0, "quarantine_reconciles": True,
        "fingerprint": {"code_evidence_complete": True, "modules": {"a": "b"}, "code_sha256": "s"},
        "active_writers": [], "remediation_unapplied": None,
    }


def _providers(pre):
    def snapshot(engine, *, page_size=512):
        return copy.deepcopy(pre)
    return snapshot, (lambda: dict(pre["fingerprint"])), (lambda e: dict(pre["target"]))


def test_plan_mode_never_spawns_even_when_clean(tmp_path):
    pre = _preflight()
    snapshot, fingerprint, target = _providers(pre)
    spawn = FakeSpawn()
    outcome = run_attempt(None, run_id="plan-only", base=str(tmp_path), snapshot=snapshot,
                          fingerprint=fingerprint, target_check=target, spawn=spawn,
                          target_url=DEV_URL)
    assert outcome["status"] == "plan"
    assert outcome["spawned"] is False
    assert spawn.calls == []


def test_execute_requires_the_matching_digest(tmp_path):
    pre = _preflight()
    snapshot, fingerprint, target = _providers(pre)
    spawn = FakeSpawn()
    outcome = run_attempt(None, run_id="digest-no", base=str(tmp_path), execute=True,
                          expected_plan_digest="wrong", snapshot=snapshot,
                          fingerprint=fingerprint, target_check=target, spawn=spawn,
                          target_url=DEV_URL)
    assert outcome["status"] == "digest_mismatch"
    assert spawn.calls == []
    assert os.path.exists(outcome["artifact_paths"]["result"])


def test_target_url_disagreeing_with_the_engine_is_refused(tmp_path):
    pre = _preflight()
    snapshot, fingerprint, target = _providers(pre)

    class Engine:
        url = "sqlite:////tmp/x.sqlite"

    spawn = FakeSpawn()
    outcome = run_attempt(Engine(), run_id="mismatch", base=str(tmp_path), snapshot=snapshot,
                          fingerprint=fingerprint, target_check=target, spawn=spawn,
                          target_url=DEV_URL)
    assert outcome["status"] == "target_mismatch"
    assert spawn.calls == []


def test_child_target_identity_must_equal_the_preflight_target(tmp_path):
    pre = _preflight()
    pre["target"] = {"tier": "development", "url_class": "development",
                     "redacted": "development postgresql other-host:5432/other_dev"}
    snapshot, fingerprint, target = _providers(pre)
    spawn = FakeSpawn()
    outcome = run_attempt(None, run_id="identity", base=str(tmp_path), snapshot=snapshot,
                          fingerprint=fingerprint, target_check=target, spawn=spawn,
                          target_url=DEV_URL)
    assert outcome["status"] == "target_mismatch"
    assert spawn.calls == []


def test_every_refusal_writes_a_labelled_result_artifact(tmp_path):
    pre = _preflight()
    snapshot, fingerprint, target = _providers(pre)
    spawn = FakeSpawn()
    outcome = run_attempt(None, run_id="evidence", base=str(tmp_path), execute=True,
                          expected_plan_digest="wrong", snapshot=snapshot,
                          fingerprint=fingerprint, target_check=target, spawn=spawn,
                          target_url=DEV_URL)
    written = json.loads(open(outcome["artifact_paths"]["result"]).read())
    assert written["status"] == "digest_mismatch"
    assert written["partial"] is True
    assert PRODUCER_CWD == written["producer_cwd"]


def test_snapshot_exception_still_produces_a_result(tmp_path):
    def failing(engine, *, page_size=512):
        raise RuntimeError("boom")

    outcome = run_attempt(None, run_id="snap", base=str(tmp_path), snapshot=failing,
                          fingerprint=lambda: {}, target_check=lambda e: {},
                          spawn=FakeSpawn(), target_url=DEV_URL)
    assert outcome["status"] == "snapshot_error"
    assert os.path.exists(outcome["artifact_paths"]["result"])


def test_timeout_is_recorded_as_its_own_status(tmp_path):
    pre = _preflight()
    snapshot, fingerprint, target = _providers(pre)

    def timing_out(command, *, timeout, env=None, cwd=None):
        raise subprocess.TimeoutExpired(cmd=list(command), timeout=timeout)

    # reach the spawn path by presenting a matching digest
    probe = run_attempt(None, run_id="timeout-p", base=str(tmp_path), snapshot=snapshot,
                        fingerprint=fingerprint, target_check=target, spawn=FakeSpawn(),
                        target_url=DEV_URL)
    digest = probe["plan_digest"]
    other = tmp_path / "b"
    other.mkdir()
    outcome = run_attempt(None, run_id="timeout", base=str(other), execute=True,
                          expected_plan_digest=digest,
                          snapshot=_providers(pre)[0], fingerprint=fingerprint,
                          target_check=target, spawn=timing_out, target_url=DEV_URL)
    assert outcome["status"] == "timeout"


def test_no_secret_leakage_in_any_artifact(tmp_path):
    pre = _preflight()
    snapshot, fingerprint, target = _providers(pre)
    spawn = FakeSpawn()
    outcome = run_attempt(None, run_id="secrets", base=str(tmp_path), snapshot=snapshot,
                          fingerprint=fingerprint, target_check=target, spawn=spawn,
                          target_url=DEV_URL)
    for name, path in outcome["artifact_paths"].items():
        if os.path.exists(path):
            text = open(path, encoding="utf-8").read()
            assert PASSWORD not in text, name
            assert "devuser" not in text, name
