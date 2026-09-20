"""Step 5 correction tests: guarded read-only snapshot, strict parsing, evaluation.

Isolated: read-contract tests use a throwaway in-memory SQLite database; runner
tests inject every provider, so no child is ever spawned and no real database or
network is touched.
"""

from __future__ import annotations

import copy
import json

import pytest
from sqlalchemy import text

from _kg_event_normalize_sqlite import build_engine, seed
from scripts.entities.event_normalize_gate import (
    EXPECTED_ALL_REPLAY,
    PathCollisionError,
    assert_paths_free,
    attempt_paths,
    evaluate_result,
    launch_decision,
    new_run_id,
    parse_child_stdout,
)
from scripts.entities.event_normalize_gate_runner import (
    ChildResult,
    plan_digest,
    run_attempt,
)
from scripts.entities.event_normalize_preflight import (
    GateTablesMissing,
    PreflightError,
    ReadOnlyViolation,
    assert_read_only_target,
    collect_population,
    gate_table_counts,
    guard_engine,
    statement_audit,
)

POPULATION = 5
DEV_URL = "postgresql://devuser:***@dev-host.internal:5432/poliscopic_dev"
PASSWORD = "***"

TABLES = ("meetings", "meeting_events")
INTEGRITY = {"orphan_extractions": 0, "graph_builder_repeat_excess": 7}


def make_fingerprint(sha="deadbeef"):
    return {
        "code_evidence_complete": True,
        "code_sha256": sha,
        "module_errors": {},
        "module_count": 20,
        "modules": {"scripts.entities.event_normalize": "abc"},
    }


def make_target(tier="development"):
    return {
        "tier": tier,
        "url_class": "development",
        "dialect": "postgresql",
        "host": "dev-host.internal",
        "port": 5432,
        "database": "poliscopic_dev",
        "redacted": "development postgresql dev-host.internal:5432/poliscopic_dev",
    }


def make_preflight(**overrides):
    record = {
        "target": make_target(),
        "target_is_development": True,
        "eligible_population": POPULATION,
        "eligible_work_items": POPULATION,
        "failures_by_reason": {},
        "failures_total": 0,
        "coverage_complete": True,
        "cursor_advanced_strictly": True,
        "gate_tables": {t: 10 for t in TABLES},
        "integrity": dict(INTEGRITY),
        "fingerprint": make_fingerprint(),
        "active_writers": [],
        "extractions_total": POPULATION,
        "quarantined_excluded": 0,
        "quarantine_reconciles": True,
        "population_drift": None,
        "fingerprint_drift": None,
        "target_drift": None,
        "remediation_unapplied": None,
    }
    record.update(overrides)
    return record


def make_postflight(preflight):
    return {
        "target": copy.deepcopy(preflight["target"]),
        "gate_tables": copy.deepcopy(preflight["gate_tables"]),
        "integrity": copy.deepcopy(preflight["integrity"]),
        "extractions_total": preflight["extractions_total"],
        "quarantined_excluded": preflight["quarantined_excluded"],
    }


def make_envelope(population=POPULATION, **stats_overrides):
    stats = {
        "extractions_examined": population,
        "normalizable": population,
        "events_planned": 0,
        "extraction_links_planned": 0,
        "events_replay_noop": population,
        "extraction_links_replay_noop": population,
        "assertions_unresolved": 0,
        "assertions_refused": 0,
        "assertions_inconsistent": 0,
        "events_inserted": 0,
        "extraction_links_updated": 0,
        "rows_committed": 0,
        "read_failures": 0,
        "errors": 0,
        "rows_rolled_back": 0,
        "skipped": 0,
        "skipped_unmapped_type": 0,
        "accounting_mode": "force",
        "classification_reconciles": True,
        "validation_receipt": {
            "state": "sealed",
            "failure": None,
            "dry_run": True,
            "values": {"reconciles": True},
            "rows": {"reconciles": True, "classification_reconciles": True, "committed": 0},
        },
    }
    stats.update(stats_overrides)
    return {"step": "normalize", "success": True, "stats": stats}


def envelope_line(envelope):
    return json.dumps(envelope)


class FakeSpawn:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.envs = []

    def __call__(self, command, *, timeout, env=None, cwd=None):
        self.calls.append(list(command))
        self.envs.append(dict(env) if env is not None else None)
        return self.result


def fake_snapshot_factory(preflight, postflight):
    seen = {"n": 0}

    def _snapshot(engine, *, page_size=512):
        seen["n"] += 1
        return copy.deepcopy(preflight if seen["n"] == 1 else postflight)

    return _snapshot


def run_two_phases(tmp_path, preflight, spawn, run_plan, run_exec):
    """Plan then execute, each with its own freshly-primed snapshot provider."""

    def kwargs():
        return dict(
            base=str(tmp_path),
            spawn=spawn,
            snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
            fingerprint=make_fingerprint,
            target_check=lambda e: make_target(),
        )

    plan = run_attempt(None, run_id=run_plan, execute=False, **kwargs())
    outcome = run_attempt(
        None,
        run_id=run_exec,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        **kwargs(),
    )
    return plan, outcome


# -- 1. no spawn in plan mode or on a blocked preflight -----------------------


def test_plan_mode_never_spawns(tmp_path):
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    outcome = run_attempt(
        None,
        run_id="planmode",
        base=str(tmp_path),
        spawn=spawn,
        snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
        fingerprint=make_fingerprint,
        target_check=lambda e: make_target(),
    )
    assert outcome["mode"] == "plan"
    assert outcome["spawned"] is False
    assert spawn.calls == []


def test_execute_refuses_and_does_not_spawn_when_preflight_is_blocked(tmp_path):
    preflight = make_preflight(
        failures_by_reason={"missing_public_body": 392},
        remediation_unapplied="phoenix-dr plan not applied",
    )
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    outcome = run_attempt(
        None,
        run_id="blocked",
        base=str(tmp_path),
        execute=True,
        expected_plan_digest=None,
        spawn=spawn,
        snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
        fingerprint=make_fingerprint,
        target_check=lambda e: make_target(),
    )
    assert outcome["spawned"] is False
    assert spawn.calls == []
    assert outcome["launchable"] is False
    assert any("392" in b for b in outcome["launch_blockers"])


def test_current_392_blocker_state_stays_not_launchable():
    """The recorded current state must remain refused."""
    launchable, reasons = launch_decision(
        make_preflight(
            failures_by_reason={"missing_public_body": 392},
            remediation_unapplied="phoenix-dr plan produced but not applied",
        )
    )
    assert launchable is False
    assert any("civic-chain failure" in r for r in reasons)
    assert any("remediation not applied" in r for r in reasons)


# -- 2. collision refusal before spawn ----------------------------------------


def test_collision_refused_before_spawn(tmp_path):
    paths = attempt_paths("collide", base=str(tmp_path))
    with open(paths["plan"], "w", encoding="utf-8") as handle:
        handle.write("previous attempt")

    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    preflight = make_preflight()
    with pytest.raises(PathCollisionError):
        run_attempt(
            None,
            run_id="collide",
            base=str(tmp_path),
            execute=True,
            expected_plan_digest="whatever",
            spawn=spawn,
            snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
            fingerprint=make_fingerprint,
            target_check=lambda e: make_target(),
        )
    assert spawn.calls == []


def test_attempt_paths_are_attempt_specific():
    a = attempt_paths("run-a")
    b = attempt_paths("run-b")
    assert set(a.values()).isdisjoint(b.values())
    assert all("run-a" in p for p in a.values())
    assert new_run_id() != new_run_id()


def test_paths_free_passes_when_nothing_exists(tmp_path):
    assert_paths_free(attempt_paths("fresh", base=str(tmp_path)))


# -- 3. envelope parsing ------------------------------------------------------


def test_exactly_one_envelope_is_accepted():
    envelope = make_envelope()
    parsed, error = parse_child_stdout("noise\n" + envelope_line(envelope) + "\n")
    assert error is None
    assert parsed is not None
    assert parsed["step"] == "normalize"


def test_zero_envelopes_refused():
    parsed, error = parse_child_stdout("just human output\nno json here\n")
    assert parsed is None
    assert "no JSON result envelope" in error


def test_multiple_envelopes_refused():
    stdout = envelope_line(make_envelope()) + "\n" + envelope_line(make_envelope())
    parsed, error = parse_child_stdout(stdout)
    assert parsed is None
    assert "exactly one envelope" in error


def test_malformed_only_refused():
    parsed, error = parse_child_stdout("{not valid json\n")
    assert parsed is None
    assert "no JSON result envelope" in error


def test_contract_violation_refused():
    bad = {"step": "normalize", "success": True, "stats": {"normalizable": 1}}
    parsed, error = parse_child_stdout(envelope_line(bad))
    assert parsed is None
    assert "missing required accounting fields" in error


# -- 4. nonzero child exit ----------------------------------------------------


def test_missing_plan_digest_refuses_before_spawn(tmp_path):
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    outcome = run_attempt(
        None,
        run_id="nodigest",
        base=str(tmp_path),
        execute=True,
        expected_plan_digest=None,
        spawn=spawn,
        snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
        fingerprint=make_fingerprint,
        target_check=lambda e: make_target(),
    )
    assert outcome["spawned"] is False
    assert spawn.calls == []
    assert "plan-digest" in outcome["refused"]


def test_nonzero_child_exit_refused_even_with_success_json(tmp_path):
    """A success envelope cannot rescue a nonzero exit code."""
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(1, envelope_line(make_envelope()), "boom"))
    _, outcome = run_two_phases(tmp_path, preflight, spawn, "exit-plan", "exit-exec")
    assert outcome["spawned"] is True
    assert outcome["passed"] is False
    assert "child exited 1" in outcome["refused"]


def test_runner_refuses_a_child_with_no_envelope(tmp_path):
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(0, "human noise only\n", ""))
    _, outcome = run_two_phases(tmp_path, preflight, spawn, "noenv-plan", "noenv-exec")
    assert outcome["spawned"] is True
    assert outcome["passed"] is False
    assert "envelope" in outcome["refused"]


def test_nonzero_exit_is_refused_with_the_matching_digest(tmp_path):
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(1, envelope_line(make_envelope()), "boom"))
    _, outcome = run_two_phases(tmp_path, preflight, spawn, "exit2-plan", "exit2-exec")
    assert outcome["spawned"] is True
    assert outcome["passed"] is False
    assert "child exited 1" in outcome["refused"]
    # the refusal is recorded in the immutable result artifact
    result_path = attempt_paths("exit2-exec", base=str(tmp_path))["result"]
    assert '"passed": false' in open(result_path, encoding="utf-8").read()


# -- 5. stream separation and credential safety -------------------------------


def test_streams_are_kept_separate(tmp_path):
    preflight = make_preflight()
    stdout_text = envelope_line(make_envelope())
    stderr_text = "WARNING something\n"
    spawn = FakeSpawn(ChildResult(0, stdout_text, stderr_text))
    run_two_phases(tmp_path, preflight, spawn, "streams-plan", "streams-exec")

    paths = attempt_paths("streams-exec", base=str(tmp_path))
    stdout_file = open(paths["stdout"], encoding="utf-8").read()
    stderr_file = open(paths["stderr"], encoding="utf-8").read()
    assert "normalize" in stdout_file
    assert "WARNING" not in stdout_file
    assert stderr_file == stderr_text
    assert "WARNING" not in stdout_file


def test_target_record_never_contains_credentials():
    class NoConnect:
        url = DEV_URL
        dialect = type("D", (), {"name": "postgresql"})()

    target = assert_read_only_target(NoConnect())
    rendered = repr(target)
    assert PASSWORD not in rendered
    assert "devuser" not in rendered


# -- 6/7. deltas and integrity metrics ----------------------------------------


def test_clean_snapshots_pass():
    preflight = make_preflight()
    evaluation = evaluate_result(
        make_envelope(),
        preflight,
        make_postflight(preflight),
        fingerprint_after=make_fingerprint(),
    )
    assert evaluation.passed is True, [c.detail for c in evaluation.failures]


@pytest.mark.parametrize("table", TABLES)
def test_nonzero_delta_fails(table):
    preflight = make_preflight()
    postflight = make_postflight(preflight)
    postflight["gate_tables"][table] += 1
    evaluation = evaluate_result(
        make_envelope(), preflight, postflight, fingerprint_after=make_fingerprint()
    )
    assert evaluation.passed is False
    assert evaluation.by_name()[f"delta:{table}"].ok is False


def test_missing_gate_table_fails():
    preflight = make_preflight()
    postflight = make_postflight(preflight)
    del postflight["gate_tables"]["meetings"]
    evaluation = evaluate_result(
        make_envelope(), preflight, postflight, fingerprint_after=make_fingerprint()
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["gate_tables_all_observed"].ok is False
    assert evaluation.by_name()["delta:meetings"].ok is False


def test_missing_integrity_metric_fails():
    preflight = make_preflight()
    postflight = make_postflight(preflight)
    del postflight["integrity"]["orphan_extractions"]
    evaluation = evaluate_result(
        make_envelope(), preflight, postflight, fingerprint_after=make_fingerprint()
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["integrity_metrics_all_observed"].ok is False
    assert evaluation.by_name()["integrity:orphan_extractions"].ok is False


def test_changed_integrity_metric_fails():
    preflight = make_preflight()
    postflight = make_postflight(preflight)
    postflight["integrity"]["orphan_extractions"] = 3
    evaluation = evaluate_result(
        make_envelope(), preflight, postflight, fingerprint_after=make_fingerprint()
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["integrity:orphan_extractions"].ok is False


def test_absent_integrity_block_fails():
    preflight = make_preflight()
    postflight = make_postflight(preflight)
    postflight["integrity"] = {}
    evaluation = evaluate_result(
        make_envelope(), preflight, postflight, fingerprint_after=make_fingerprint()
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["integrity_present"].ok is False


# -- 8. fingerprint drift -----------------------------------------------------


def test_incomplete_fingerprint_blocks_launch():
    launchable, reasons = launch_decision(
        make_preflight(fingerprint={"code_evidence_complete": False, "modules": {}})
    )
    assert launchable is False
    assert any("fingerprint" in r for r in reasons)


def test_fingerprint_drift_before_spawn_blocks_launch():
    launchable, reasons = launch_decision(make_preflight(fingerprint_drift="moved"))
    assert launchable is False
    assert any("fingerprint drift" in r for r in reasons)


def test_fingerprint_changed_after_run_fails_evaluation():
    preflight = make_preflight()
    evaluation = evaluate_result(
        make_envelope(),
        preflight,
        make_postflight(preflight),
        fingerprint_after=make_fingerprint("different"),
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["fingerprint_unchanged"].ok is False


def test_missing_postflight_fingerprint_fails():
    preflight = make_preflight()
    evaluation = evaluate_result(
        make_envelope(), preflight, make_postflight(preflight), fingerprint_after=None
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["fingerprint_after_present"].ok is False


# -- 9. target drift ----------------------------------------------------------


def test_target_drift_blocks_launch():
    launchable, reasons = launch_decision(make_preflight(target_drift="database changed"))
    assert launchable is False
    assert any("target drift" in r for r in reasons)


def test_target_changed_between_snapshots_fails():
    preflight = make_preflight()
    postflight = make_postflight(preflight)
    postflight["target"] = make_target()
    postflight["target"]["database"] = "other_dev"
    evaluation = evaluate_result(
        make_envelope(), preflight, postflight, fingerprint_after=make_fingerprint()
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["target_unchanged"].ok is False


def test_non_development_target_fails():
    preflight = make_preflight(target=make_target(tier="test-isolated"))
    evaluation = evaluate_result(
        make_envelope(), preflight, make_postflight(preflight), fingerprint_after=make_fingerprint()
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["target_is_development"].ok is False


# -- 10. enforced read-only protection ---------------------------------------


def test_guarded_engine_refuses_a_mutation():
    engine = build_engine()
    seed(engine)
    guard_engine(engine)
    with pytest.raises(ReadOnlyViolation):
        with engine.begin() as conn:
            conn.execute(text("UPDATE meetings SET public_body_id = NULL"))


def test_guarded_engine_still_allows_reads():
    engine = build_engine()
    seed(engine)
    statements = guard_engine(engine)
    population = collect_population(engine, page_size=64)
    assert population["examined"] == 1
    assert statement_audit(statements)["select_only"] is True


def test_every_read_path_shares_the_guard():
    """The guard covers provider reads too, not just the population scan."""
    engine = build_engine()
    seed(engine)
    guard_engine(engine)
    with pytest.raises(ReadOnlyViolation):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM meetings"))


def test_gate_table_counts_is_fatal_on_a_missing_table():
    engine = build_engine()
    with pytest.raises(GateTablesMissing) as exc:
        gate_table_counts(engine, tables=["definitely_absent_table"])
    assert "definitely_absent_table" in str(exc.value)


def test_preflight_issues_only_read_only_statements():
    engine = build_engine()
    seed(engine)
    statements = guard_engine(engine)
    collect_population(engine, page_size=64)
    assert statement_audit(statements)["mutating_statements"] == []


def test_statement_audit_rejects_mutating_statements():
    audit = statement_audit(["SELECT 1", "UPDATE meetings SET x = 1"])
    assert audit["select_only"] is False


# -- 11. clean synthetic run end to end --------------------------------------


def test_clean_synthetic_run_passes_end_to_end(tmp_path):
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    plan, outcome = run_two_phases(
        tmp_path, preflight, spawn, "clean-plan", "clean-exec"
    )
    assert plan["launchable"] is True

    assert outcome["spawned"] is True
    assert outcome["child_exit_code"] == 0
    assert outcome["passed"] is True, outcome["failed_checks"]
    assert outcome["failed_checks"] == []
    assert len(spawn.calls) == 1


def test_execute_requires_the_matching_plan_digest(tmp_path):
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    outcome = run_attempt(
        None,
        run_id="wrongdigest",
        base=str(tmp_path),
        execute=True,
        expected_plan_digest="not-the-digest",
        spawn=spawn,
        snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
        fingerprint=make_fingerprint,
        target_check=lambda e: make_target(),
    )
    assert outcome["spawned"] is False
    assert spawn.calls == []
    assert "plan-digest" in outcome["refused"]


def test_plan_digest_is_stable_and_covers_the_approval_body():
    from scripts.entities.event_normalize_gate_runner import build_plan

    paths = attempt_paths("digest")
    plan = build_plan("digest", make_preflight(), paths)
    assert plan_digest(plan) == plan["digest"]["value"]
    plan["approval_body"]["producer_command"] = ["tampered"]
    assert plan_digest(plan) != plan["digest"]["value"]


def test_plan_digest_is_independent_of_the_run_id():
    """An approval must survive a fresh attempt but not a material drift."""
    from scripts.entities.event_normalize_gate_runner import build_plan

    a = build_plan("run-a", make_preflight(), attempt_paths("run-a"))
    b = build_plan("run-b", make_preflight(), attempt_paths("run-b"))
    assert a["digest"]["value"] == b["digest"]["value"]

    drifted = build_plan(
        "run-c", make_preflight(eligible_work_items=POPULATION + 1), attempt_paths("run-c")
    )
    assert drifted["digest"]["value"] != a["digest"]["value"]


def test_all_expected_replay_counters_are_enforced():
    preflight = make_preflight()
    evaluation = evaluate_result(
        make_envelope(),
        preflight,
        make_postflight(preflight),
        fingerprint_after=make_fingerprint(),
    )
    names = evaluation.by_name()
    for field in EXPECTED_ALL_REPLAY:
        assert f"zero:{field}" in names


@pytest.mark.parametrize(
    ("field", "value"),
    [("events_inserted", 1), ("extraction_links_updated", 1), ("rows_committed", 1),
     ("read_failures", 1), ("assertions_inconsistent", 1), ("events_planned", 1)],
)
def test_any_unexpected_counter_fails(field, value):
    preflight = make_preflight()
    evaluation = evaluate_result(
        make_envelope(**{field: value}),
        preflight,
        make_postflight(preflight),
        fingerprint_after=make_fingerprint(),
    )
    assert evaluation.passed is False


def test_population_shortfall_fails():
    preflight = make_preflight()
    evaluation = evaluate_result(
        make_envelope(population=POPULATION - 1),
        preflight,
        make_postflight(preflight),
        fingerprint_after=make_fingerprint(),
    )
    assert evaluation.passed is False
    assert evaluation.by_name()["population_matches_preflight"].ok is False


# -- cross-process enforced read-only contract --------------------------------


def test_child_spawn_receives_enforced_read_only_settings(tmp_path):
    """The child is spawned with PostgreSQL read-only enforcement, not just a flag."""
    preflight = make_preflight()          # url_class = development
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    run_two_phases(tmp_path, preflight, spawn, "ro-plan", "ro-exec")

    assert len(spawn.envs) == 1
    env = spawn.envs[0]
    assert "default_transaction_read_only=on" in env["PGOPTIONS"]
    assert env["POLISCOPIC_DB_TIER"] == "development"


def test_conflicting_pgoptions_cannot_disable_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c default_transaction_read_only=off")
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))

    outcome = run_attempt(
        None,
        run_id="ro-conflict",
        base=str(tmp_path),
        spawn=spawn,
        snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
        fingerprint=make_fingerprint,
        target_check=lambda e: make_target(),
    )

    assert outcome["spawned"] is False
    assert "weakening value" in outcome["refused"]
    assert spawn.calls == []
    assert spawn.envs == []


@pytest.mark.parametrize("target_url", [
    "postgresql://produser:***@tenant.example.ondigitalocean.com:25060/poliscopic",
    "mysql://u:***@h/db", "nonsense"])
def test_contract_refuses_unsafe_target_urls(target_url):
    from scripts.entities.event_normalize_gate_runner import (
        ChildContractError,
        child_read_only_contract,
    )

    with pytest.raises(ChildContractError):
        child_read_only_contract(target_url, {})


def test_contract_composes_with_a_benign_pgoptions():
    from scripts.entities.event_normalize_gate_runner import child_read_only_contract

    contract = child_read_only_contract(DEV_URL, {"PGOPTIONS": "-c statement_timeout=5000"})

    assert "statement_timeout=5000" in contract["pgoptions"]
    assert "default_transaction_read_only=on" in contract["pgoptions"]
    assert contract["database_enforced_read_only"] is True


def test_contract_does_not_duplicate_an_existing_read_only_option():
    from scripts.entities.event_normalize_gate_runner import child_read_only_contract

    contract = child_read_only_contract(
        DEV_URL, {"PGOPTIONS": "-c default_transaction_read_only=on"}
    )

    assert contract["pgoptions"].count("default_transaction_read_only=on") == 1


def test_test_isolated_target_needs_no_postgres_options():
    from scripts.entities.event_normalize_gate_runner import child_read_only_contract

    contract = child_read_only_contract("sqlite:////tmp/x.sqlite", {})

    assert contract["pgoptions"] is None
    assert contract["env_keys_bound"] == ["DATABASE_URL"]
    assert contract["database_enforced_read_only"] is False


def test_test_isolated_target_env_has_no_postgres_options():
    """The SQLite path must stay usable: no PostgreSQL-only options are injected.

    (A local/test-isolated target is deliberately *not* launchable as a
    development gate; what matters here is that the contract does not force
    PostgreSQL options onto it.)
    """
    from scripts.entities.event_normalize_gate_runner import child_environment

    env, contract = child_environment("sqlite:////tmp/x.sqlite", {"PATH": "/usr/bin"})

    assert "PGOPTIONS" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["DATABASE_URL"] == "sqlite:////tmp/x.sqlite"
    assert contract["database_enforced_read_only"] is False
    assert contract["enforced_by"] == "isolated-local-target"


def test_plan_and_result_record_the_effective_child_contract(tmp_path):
    preflight = make_preflight()
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    _, outcome = run_two_phases(tmp_path, preflight, spawn, "ev-plan", "ev-exec")

    plan_doc = json.loads(
        open(attempt_paths("ev-plan", base=str(tmp_path))["plan"], encoding="utf-8").read()
    )
    assert plan_doc["approval_body"]["child_read_only"]["database_enforced_read_only"] is True
    assert outcome["child_read_only_contract"]["database_enforced_read_only"] is True


def test_blockers_prevent_spawn_and_environment_construction(tmp_path):
    preflight = make_preflight(failures_by_reason={"missing_public_body": 392})
    spawn = FakeSpawn(ChildResult(0, envelope_line(make_envelope()), ""))
    direct = dict(
        base=str(tmp_path),
        spawn=spawn,
        fingerprint=make_fingerprint,
        target_check=lambda e: make_target(),
    )
    plan = run_attempt(
        None,
        run_id="blk-plan",
        snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
        **direct,
    )
    assert plan["launchable"] is False

    outcome = run_attempt(
        None,
        run_id="blk-exec",
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        snapshot=fake_snapshot_factory(preflight, make_postflight(preflight)),
        **direct,
    )

    assert outcome["spawned"] is False
    assert spawn.calls == []
    assert spawn.envs == []


def test_child_side_mutation_is_rejected_where_expressible():
    """Behavioural proof available in-process; the PG GUC itself is by construction."""
    engine = build_engine()
    seed(engine)
    guard_engine(engine)
    with pytest.raises(ReadOnlyViolation):
        with engine.begin() as conn:
            conn.execute(text("UPDATE supporting_documents SET text_content = 'x'"))


# -- artifact suffix correctness ---------------------------------------------


def test_artifact_suffixes_are_not_duplicated():
    paths = attempt_paths("suffix")
    assert paths["log"].endswith(".log") and not paths["log"].endswith(".log.log")
    assert paths["stdout"].endswith(".stdout.log")
    assert paths["stderr"].endswith(".stderr.log")
    assert paths["plan"].endswith(".plan.json")
    assert paths["preflight"].endswith(".preflight.json")
    assert paths["postflight"].endswith(".postflight.json")
    assert paths["envelope"].endswith(".envelope.json")
    assert paths["result"].endswith(".result.json")


def test_artifact_paths_remain_unique_per_attempt():
    paths = attempt_paths("unique")
    assert len(set(paths.values())) == len(paths)
    other = attempt_paths("other")
    assert set(paths.values()).isdisjoint(other.values())
