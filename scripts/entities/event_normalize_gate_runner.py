#!/usr/bin/env python3
"""``event_normalize_gate_runner.py`` — the single approval-gated gate command.

Sequence, in this order and no other:

    atomic reservation -> collision check -> guarded preflight -> launch decision
      -> (execute only) target binding + fingerprint recheck -> pinned child spawn
      -> guarded postflight -> evaluation -> immutable result artifact

Two modes: **plan** (default) never spawns the producer; **execute** requires both
``--execute`` and a matching ``--plan-digest``.

Every terminal path — blocker, digest mismatch, drift, nonzero exit, parse error,
snapshot/postflight exception, timeout, failure, success — writes exactly one
labelled result artifact.  Contracts and artifact semantics live in
:mod:`event_normalize_child_contract` and :mod:`event_normalize_artifacts`; this
module only orchestrates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.entities.event_normalize_accounting import (  # noqa: E402
    CLASSIFICATION_EQUATION,
)
from scripts.entities.event_normalize_artifacts import (  # noqa: E402
    ArtifactCollision,
    finish,
    reserve_attempt,
    write_evidence,
)
from scripts.entities.event_normalize_child_contract import (  # noqa: E402
    GUARANTEE,
    PRODUCER_COMMAND,
    PRODUCER_CWD,
    PRODUCER_EXECUTABLE,
    PRODUCER_SCRIPT,
    READ_ONLY_GUC,
    READ_ONLY_PGOPTION,
    ChildContractError,
    ChildResult,
    child_environment,
    child_read_only_contract,
    compose_pgoptions,
    default_spawn,
    parse_pgoptions,
)
from scripts.entities.event_normalize_gate import (  # noqa: E402
    EXPECTED_ALL_REPLAY,
    PathCollisionError,
    assert_paths_free,
    attempt_paths,
    evaluate_result,
    launch_decision,
    new_run_id,
    parse_child_stdout,
)

__all__ = [
    "GUARANTEE",
    "PRODUCER_COMMAND",
    "PRODUCER_CWD",
    "PRODUCER_EXECUTABLE",
    "PRODUCER_SCRIPT",
    "READ_ONLY_GUC",
    "READ_ONLY_PGOPTION",
    "ChildContractError",
    "ChildResult",
    "RunnerRefused",
    "build_plan",
    "child_environment",
    "child_read_only_contract",
    "compose_pgoptions",
    "default_spawn",
    "parse_pgoptions",
    "plan_digest",
    "run_attempt",
]

DEFAULT_TIMEOUT_SECONDS = 3600.0


class RunnerRefused(RuntimeError):
    """The attempt was refused before the producer could be spawned."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def plan_digest(plan: Mapping[str, Any]) -> str:
    """SHA-256 over the plan's approval body (attempt-independent)."""
    return hashlib.sha256(
        canonical_json(dict(plan.get("approval_body") or {})).encode("utf-8")
    ).hexdigest()


def build_plan(
    run_id: str,
    preflight: Mapping[str, Any],
    paths: Mapping[str, str],
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The immutable plan an execution must name by digest."""
    approval_body = {
        "step": 5,
        "brief": "docs/briefs/019-stage-1-event-normalize-gate-remediation.md",
        "producer_command": list(PRODUCER_COMMAND),
        "producer_cwd": PRODUCER_CWD,
        "env_prefix": {"POLISCOPIC_DB_TIER": "development"},
        "coverage_strategy": {
            "chosen": "unbounded deterministic keyset pagination (no --limit)",
            "compared_against": "preflight eligible_work_items (freshly captured)",
        },
        "accounting": {
            "equation": CLASSIFICATION_EQUATION,
            "expected_all_replay": EXPECTED_ALL_REPLAY,
            "quarantine_identity": "eligible_read + quarantined_excluded == extractions_total",
        },
        "expected_population": preflight.get("eligible_work_items"),
        "expected_fingerprint": (preflight.get("fingerprint") or {}).get("code_sha256"),
        "expected_target": preflight.get("target"),
        "prerequisites": {
            "remediation_unapplied": preflight.get("remediation_unapplied"),
            "failures_by_reason": preflight.get("failures_by_reason") or {},
        },
        "child_read_only": dict(contract or {}),
        "execution_requires": {
            "flag": "--execute",
            "plan_digest": "must equal this approval body's digest",
        },
    }
    plan = {
        "plan_id": f"kg-step5-gate-{run_id}",
        "run_id": run_id,
        "artifact_paths": dict(paths),
        "approval_body": approval_body,
    }
    plan["digest"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON of approval_body (run id and paths excluded)",
        "value": plan_digest(plan),
    }
    return plan


def _base_outcome(run_id: str, paths: Mapping[str, str], execute: bool) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "mode": "execute" if execute else "plan",
        "artifact_paths": dict(paths),
        "producer_command": list(PRODUCER_COMMAND),
        "producer_cwd": PRODUCER_CWD,
        "producer_executable": PRODUCER_EXECUTABLE,
        "producer_script": PRODUCER_SCRIPT,
        "spawned": False,
    }


def run_attempt(
    engine: Any,
    *,
    run_id: str | None = None,
    execute: bool = False,
    expected_plan_digest: str | None = None,
    base: str = "data",
    page_size: int = 512,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    spawn: Callable[..., ChildResult] | None = None,
    snapshot: Callable[..., dict] | None = None,
    fingerprint: Callable[[], dict] | None = None,
    target_check: Callable[[Any], dict] | None = None,
    target_url: str | None = None,
) -> dict[str, Any]:
    """Run the sequence.  In plan mode the producer is never spawned."""
    from scripts.entities.event_normalize_preflight import (
        assert_read_only_target,
        code_fingerprint,
        run_snapshot,
    )
    from db.tier import classify_target

    snapshot = snapshot or run_snapshot
    fingerprint = fingerprint or code_fingerprint
    target_check = target_check or assert_read_only_target
    spawn = spawn or default_spawn

    run_id = run_id or new_run_id()
    paths = attempt_paths(run_id, base=base)
    outcome = _base_outcome(run_id, paths, execute)

    # 1. Atomic reservation, then the collision check: before any artifact is
    #    opened and before any child can be launched.
    try:
        reserve_attempt(paths["result"][: -len(".result.json")])
    except ArtifactCollision as exc:
        outcome["refused"] = str(exc)
        outcome.setdefault("status", "blocked")
        return outcome
    # A collision cannot be turned into a result artifact (the artifact already
    # exists), so it raises rather than pretending to report.
    assert_paths_free(paths)

    # 2. Guarded preflight.
    try:
        preflight = snapshot(engine, page_size=page_size)
    except Exception as exc:  # a refusal is evidence too
        outcome["refused"] = f"preflight snapshot failed: {exc}"
        return finish(outcome, paths, "snapshot_error", detail=str(exc))
    try:
        write_evidence(paths, "preflight", preflight)
    except ArtifactCollision as exc:
        outcome["refused"] = str(exc)
        return finish(outcome, paths, "blocked", detail=str(exc))

    # 3. Resolve the child target from the engine, never from a bare parameter.
    engine_url: str | None = None
    if engine is not None and getattr(engine, "url", None) is not None:
        # Pass the URL *object*: str(url) masks the password as '***', which the
        # child cannot authenticate with.  The credential is rendered exactly once,
        # inside the child-environment boundary.
        engine_url = engine.url
    if target_url is not None:
        if engine_url is not None and classify_target(target_url) != classify_target(engine_url):
            outcome["refused"] = "target_url disagrees with the verified engine target"
            return finish(outcome, paths, "target_mismatch", detail=outcome["refused"])
        engine_url = engine_url or target_url

    preflight_target = dict(preflight.get("target") or {})
    if engine_url is not None:
        try:
            child_env, contract = child_environment(engine_url, os.environ)
        except ChildContractError as exc:
            outcome["refused"] = str(exc)
            return finish(outcome, paths, "child_contract", detail=str(exc))
        if contract.get("target_redacted") != preflight_target.get("redacted"):
            outcome["refused"] = "child target identity does not equal the preflight target"
            return finish(outcome, paths, "target_mismatch", detail=outcome["refused"])
    else:
        # Test path: no engine or URL was supplied, so nothing can be bound.  The
        # contract still reports the full, honest shape derived from the recorded
        # preflight target rather than omitting fields.
        target_class = preflight_target.get("url_class")
        child_env = dict(os.environ)
        if target_class == "local":
            contract = {
                "url_class": "local",
                "target_redacted": preflight_target.get("redacted"),
                "enforced_by": "isolated-local-target",
                "database_enforced_read_only": False,
                "dialect": "sqlite",
                "pgoptions": None,
                "env_keys_bound": ["DATABASE_URL"],
                "binding": "unbound (test path)",
                "guarantee": dict(GUARANTEE),
            }
        else:
            try:
                composed = compose_pgoptions(os.environ.get("PGOPTIONS"))
            except ChildContractError as exc:
                outcome["refused"] = str(exc)
                return finish(outcome, paths, "child_contract", detail=str(exc))
            child_env["PGOPTIONS"] = composed
            child_env["POLISCOPIC_DB_TIER"] = "development"
            contract = {
                "url_class": target_class or "development",
                "target_redacted": preflight_target.get("redacted"),
                "enforced_by": "postgresql default_transaction_read_only",
                "database_enforced_read_only": True,
                "dialect": "postgresql",
                "pgoptions": composed,
                "env_keys_bound": ["DATABASE_URL", "POLISCOPIC_DB_TIER", "PGOPTIONS"],
                "binding": "unbound (test path)",
                "guarantee": dict(GUARANTEE),
            }

    # 4. Launch decision and the plan it authorises.
    launchable, blockers = launch_decision(preflight)
    plan = build_plan(run_id, preflight, paths, contract)
    try:
        write_evidence(paths, "plan", plan)
    except ArtifactCollision as exc:
        outcome["refused"] = str(exc)
        return finish(outcome, paths, "blocked", detail=str(exc))
    outcome.update(
        {
            "launchable": launchable,
            "launch_blockers": blockers,
            "plan_digest": plan["digest"]["value"],
            "child_read_only_contract": contract,
        }
    )

    if not execute:
        return finish(
            outcome, paths, "plan",
            detail="plan mode never spawns the producer",
        )
    if expected_plan_digest != plan["digest"]["value"]:
        outcome["refused"] = "refusing to execute: --plan-digest does not match"
        return finish(outcome, paths, "digest_mismatch", detail=outcome["refused"])
    if not launchable:
        outcome["refused"] = "refusing to execute: launch blockers present"
        return finish(outcome, paths, "blocked", detail="; ".join(blockers))

    # 5. Re-verify target and fingerprint immediately before spawning.
    try:
        target_now = target_check(engine if engine is not None else _NullEngine())
        fingerprint_now = fingerprint()
    except Exception as exc:
        outcome["refused"] = f"pre-spawn recheck failed: {exc}"
        return finish(outcome, paths, "snapshot_error", detail=str(exc))
    if engine is not None and target_now != preflight_target:
        outcome["refused"] = f"target changed before spawn ({target_now.get('redacted')})"
        return finish(outcome, paths, "target_drift", detail=outcome["refused"])
    if fingerprint_now.get("code_sha256") != (preflight.get("fingerprint") or {}).get("code_sha256"):
        outcome["refused"] = "fingerprint drifted before spawn"
        return finish(outcome, paths, "fingerprint_drift", detail=outcome["refused"])

    # 6. Spawn with streams separate, in a pinned cwd and target-bound environment.
    try:
        child = spawn(list(PRODUCER_COMMAND), timeout=timeout, env=child_env, cwd=PRODUCER_CWD)
    except subprocess.TimeoutExpired as exc:
        outcome["refused"] = f"child exceeded {timeout}s"
        return finish(outcome, paths, "timeout", detail=str(exc))
    except Exception as exc:
        outcome["refused"] = f"child could not be started: {exc}"
        return finish(outcome, paths, "spawn_error", detail=str(exc))
    outcome["spawned"] = True
    outcome["child_exit_code"] = child.exit_code
    for name, text in (("stdout", child.stdout), ("stderr", child.stderr),
                       ("log", "# stdout\n" + child.stdout + "\n# stderr\n" + child.stderr)):
        try:
            from scripts.entities.event_normalize_artifacts import write_exclusive

            write_exclusive(paths[name], text)
        except ArtifactCollision:
            pass

    if child.exit_code != 0:
        outcome["refused"] = f"child exited {child.exit_code}"
        return finish(outcome, paths, "child_exit", detail=outcome["refused"])

    envelope, parse_error = parse_child_stdout(child.stdout)
    if parse_error is not None or envelope is None:
        outcome["refused"] = f"child envelope rejected: {parse_error}"
        return finish(outcome, paths, "parse_error", detail=outcome["refused"])
    try:
        write_evidence(paths, "envelope", envelope)
    except ArtifactCollision:
        pass

    # 7. Guarded postflight, then evaluation.
    try:
        postflight = snapshot(engine, page_size=page_size)
        fingerprint_after = fingerprint()
        write_evidence(paths, "postflight", postflight)
    except Exception as exc:
        return finish(outcome, paths, "postflight_error", detail=str(exc))

    evaluation = evaluate_result(
        envelope, preflight, postflight, fingerprint_after=fingerprint_after
    )
    outcome["checks"] = [
        {"check": c.check, "ok": c.ok, "detail": c.detail} for c in evaluation.checks
    ]
    outcome["failed_checks"] = [c.check for c in evaluation.failures]
    outcome["passed"] = evaluation.passed
    return finish(
        outcome, paths,
        "success" if evaluation.passed else "checks_failed",
        detail=None if evaluation.passed else f"{len(evaluation.failures)} check(s) failed",
    )


class _NullEngine:
    """Placeholder for the test path where no engine is supplied."""

    url = "sqlite://"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Approval-gated event_normalize development dry gate (brief 019, Step 5)"
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--execute", action="store_true",
                        help="actually spawn the producer; requires --plan-digest")
    parser.add_argument("--plan-digest", default=None)
    parser.add_argument("--base", default="data")
    parser.add_argument("--page-size", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    from db.core import get_engine

    try:
        outcome = run_attempt(
            get_engine(), run_id=args.run_id, execute=args.execute,
            expected_plan_digest=args.plan_digest, base=args.base,
            page_size=args.page_size, timeout=args.timeout,
        )
    except PathCollisionError as exc:
        print(json.dumps({"refused": str(exc)}, indent=2))
        return 4
    print(json.dumps(outcome, indent=2, sort_keys=True, default=str))
    status = outcome.get("status")
    if status == "plan":
        return 0 if outcome.get("launchable") else 2
    return 0 if status == "success" else 3


if __name__ == "__main__":
    # Conventional entry point.  Without it the approved CLI silently did nothing
    # and still exited 0, which reads as success — so a missing guard is itself a
    # safety defect, not a cosmetic one.
    raise SystemExit(main())
