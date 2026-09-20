#!/usr/bin/env python3
"""``event_normalize_gate.py`` — pure gate contract: paths, parsing, evaluation.

No connection, no process, no file write.  Everything here is testable in
isolation, and every rule is stated once.

Three responsibilities:

1. :func:`attempt_paths` / :func:`assert_paths_free` — immutable, attempt-specific
   artifact paths and refusal to overwrite.
2. :func:`parse_child_stdout` — exactly one valid normalize child envelope,
   delegating contract validation to the existing event-extractor parser so no
   competing envelope semantics exist.
3. :func:`evaluate_result` — the acceptance decision, computed from real
   preflight/postflight snapshots.  Missing metrics are failures, not skips.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.entities.event_normalize_accounting import (  # noqa: E402
    CLASSIFICATION_EQUATION,
    classification_error,
)

__all__ = [
    "ARTIFACT_EXTENSIONS",
    "CLASSIFICATION_EQUATION",
    "EXPECTED_ALL_REPLAY",
    "GateCheck",
    "GateEvaluation",
    "PathCollisionError",
    "active_writer_blockers",
    "assert_paths_free",
    "attempt_paths",
    "evaluate_result",
    "launch_decision",
    "new_run_id",
    "parse_child_stdout",
]

#: The exact acceptance shape of a clean all-linked full-population force replay.
EXPECTED_ALL_REPLAY = {
    "events_planned": 0,
    "extraction_links_planned": 0,
    "events_inserted": 0,
    "extraction_links_updated": 0,
    "rows_committed": 0,
    "assertions_inconsistent": 0,
    "assertions_unresolved": 0,
    "assertions_refused": 0,
    "read_failures": 0,
    "errors": 0,
    "rows_rolled_back": 0,
    "skipped": 0,
}

#: Every artifact one attempt owns, mapped to its full on-disk suffix.
#: Attempt-specific: nothing is hard-coded, each name is unique, and no suffix is
#: duplicated (`<stem>.plan.json`, `<stem>.stdout.log`, `<stem>.log`).
ARTIFACT_SUFFIXES = {
    "plan": "plan.json",
    "preflight": "preflight.json",
    "postflight": "postflight.json",
    "envelope": "envelope.json",
    "result": "result.json",
    "log": "log",
    "stdout": "stdout.log",
    "stderr": "stderr.log",
}
#: Back-compatible alias for the previous name.
ARTIFACT_EXTENSIONS = ARTIFACT_SUFFIXES


class PathCollisionError(RuntimeError):
    """An artifact path for this attempt already exists."""


@dataclass(frozen=True)
class GateCheck:
    """One named PASS/FAIL condition with its supporting detail."""

    check: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class GateEvaluation:
    """The full checklist outcome for one finished attempt."""

    checks: tuple[GateCheck, ...] = ()

    @property
    def failures(self) -> tuple[GateCheck, ...]:
        return tuple(c for c in self.checks if not c.ok)

    @property
    def passed(self) -> bool:
        return not self.failures

    def by_name(self) -> dict[str, GateCheck]:
        return {c.check: c for c in self.checks}


def new_run_id(now: Any = None) -> str:
    """An immutable, sortable attempt id: UTC timestamp plus a short entropy tag."""
    import os
    from datetime import datetime, timezone

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.urandom(3).hex()}"


def attempt_paths(run_id: str, *, base: str = "data") -> dict[str, str]:
    """Every artifact path for one attempt, derived from its run id."""
    stem = f"kg-stage1-event-normalize-gate-{run_id}"
    return {
        name: f"{base}/{stem}.{suffix}"
        for name, suffix in ARTIFACT_SUFFIXES.items()
    }


def assert_paths_free(paths: Mapping[str, str] | Sequence[str]) -> None:
    """Refuse any attempt whose artifacts already exist.

    Called before a file is opened or a child is spawned, so a collision can
    never overwrite evidence or half-run the producer.
    """
    candidates = list(paths.values()) if isinstance(paths, Mapping) else list(paths)
    existing = [p for p in candidates if Path(p).exists()]
    if existing:
        raise PathCollisionError(
            "refusing to overwrite existing gate artifacts: " + ", ".join(sorted(existing))
        )


def parse_child_stdout(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Exactly one valid normalize child envelope, or an error.

    The child *contract* is validated by the existing event-extractor parser, so
    there is only one definition of what a valid envelope is.  This wrapper adds
    only the strictness the gate needs: exactly one JSON object, no extras.
    """
    objects: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            objects.append(candidate)

    if not objects:
        return None, "no JSON result envelope on stdout"
    if len(objects) > 1:
        steps = [str(o.get("step")) for o in objects]
        return None, f"expected exactly one envelope, found {len(objects)}: {steps}"

    from scripts.entities.event_extractor import _parse_step_result  # lazy: no import side effects

    return _parse_step_result("normalize", stdout)


def active_writer_blockers(ps_output: str, *, own_pid: int | None = None) -> list[str]:
    """Local pipeline processes that would race the gate.  Local and read-only."""
    markers = (
        "event_normalize.py",
        "event_extractor.py",
        "detect_entities.py",
        "bounded_verification.py",
    )
    blockers: list[str] = []
    for line in ps_output.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid, command = parts[0].strip(), parts[1]
        if own_pid is not None and pid.isdigit() and int(pid) == own_pid:
            continue
        if "grep" in command:
            continue
        for marker in markers:
            if marker in command:
                blockers.append(f"pid {pid}: {command.strip()[:120]}")
                break
    return blockers


def launch_decision(
    preflight: Mapping[str, Any], *, target_ok: bool | None = None
) -> tuple[bool, list[str]]:
    """Whether an attempt may spawn the producer, plus every reason it may not."""
    reasons: list[str] = []

    if preflight.get("refused"):
        reasons.append(f"target refused: {preflight['refused']}")
    if not preflight.get("target_is_development"):
        reasons.append("target is not a verified development target")
    if target_ok is False:
        reasons.append("target recheck before spawn disagreed with the preflight target")

    failures = preflight.get("failures_by_reason") or {}
    if failures:
        total = sum(int(v) for v in failures.values())
        reasons.append(
            f"{total} civic-chain failure(s) remain: "
            + ", ".join(f"{k}={v}" for k, v in sorted(failures.items()))
        )

    if preflight.get("coverage_complete") is not True:
        reasons.append("eligible population was not read to completion")

    if preflight.get("quarantine_reconciles") is not True:
        reasons.append(
            "quarantine accounting does not reconcile "
            "(eligible + quarantined != total extractions)"
        )

    fingerprint = preflight.get("fingerprint") or {}
    if fingerprint.get("code_evidence_complete") is not True:
        reasons.append("fingerprint manifest is incomplete")
    if fingerprint.get("module_errors"):
        reasons.append(f"fingerprint module errors: {fingerprint['module_errors']}")
    if not fingerprint.get("modules"):
        reasons.append("fingerprint manifest carries no module hashes")

    for key, label in (
        ("population_drift", "population drift"),
        ("fingerprint_drift", "fingerprint drift"),
        ("target_drift", "target drift"),
    ):
        if preflight.get(key):
            reasons.append(f"{label}: {preflight[key]}")

    writers = preflight.get("active_writers") or []
    if writers:
        reasons.append(f"competing pipeline process(es): {writers}")

    if preflight.get("remediation_unapplied"):
        reasons.append(f"remediation not applied: {preflight['remediation_unapplied']}")

    return (not reasons), reasons


def _counter(stats: Mapping[str, Any], name: str) -> int | None:
    value = stats.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def evaluate_result(
    envelope: Mapping[str, Any],
    preflight: Mapping[str, Any],
    postflight: Mapping[str, Any],
    *,
    fingerprint_after: Mapping[str, Any] | None = None,
) -> GateEvaluation:
    """Judge one finished attempt from real snapshots.

    Gate-table deltas and integrity equality are **computed** from the preflight
    and postflight snapshots.  A metric missing from either side is a failure, and
    a caller-supplied "integrity is fine" flag is not accepted.
    """
    checks: list[GateCheck] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append(GateCheck(name, bool(ok), detail))

    add("step_is_normalize", envelope.get("step") == "normalize",
        f"step={envelope.get('step')!r}")
    add("success_is_true", envelope.get("success") is True,
        f"success={envelope.get('success')!r}")

    stats = envelope.get("stats")
    if not isinstance(stats, Mapping):
        add("stats_present", False, "envelope has no stats object")
        return GateEvaluation(tuple(checks))
    add("stats_present", True, "stats object present")

    # -- exactly one sealed, reconciled receipt ------------------------------
    receipt = stats.get("validation_receipt")
    add("receipt_present", isinstance(receipt, Mapping), f"type={type(receipt).__name__}")
    if isinstance(receipt, Mapping):
        add("receipt_state_sealed", receipt.get("state") == "sealed",
            f"state={receipt.get('state')!r}")
        add("receipt_has_no_failure", receipt.get("failure") is None,
            f"failure={receipt.get('failure')!r}")
        add("receipt_dry_run_true", receipt.get("dry_run") is True,
            f"dry_run={receipt.get('dry_run')!r}")
        values = receipt.get("values") or {}
        rows = receipt.get("rows") or {}
        add("receipt_values_reconcile", values.get("reconciles") is True,
            f"values.reconciles={values.get('reconciles')!r}")
        add("receipt_rows_reconcile", rows.get("reconciles") is True,
            f"rows.reconciles={rows.get('reconciles')!r}")
        add("receipt_classification_reconciles",
            rows.get("classification_reconciles") is True,
            f"rows.classification_reconciles={rows.get('classification_reconciles')!r}")
        add("receipt_committed_zero", rows.get("committed") in (0, None),
            f"rows.committed={rows.get('committed')!r}")

    accounting = classification_error(stats, mode=stats.get("accounting_mode"))
    add("accounting_reconciles", accounting is None, accounting or "classification balanced")
    add("classification_reconciles_flag", stats.get("classification_reconciles") is True,
        f"classification_reconciles={stats.get('classification_reconciles')!r}")

    # -- full-population proof against the FRESH preflight capture -----------
    population = preflight.get("eligible_work_items")
    examined = _counter(stats, "extractions_examined")
    normalizable = _counter(stats, "normalizable")
    add("population_matches_preflight",
        population is not None and examined == population,
        f"examined={examined} preflight={population}")
    add("work_items_match_preflight",
        population is not None and normalizable == population,
        f"normalizable={normalizable} preflight={population}")
    add("normalizable_greater_than_zero",
        normalizable is not None and normalizable > 0, f"normalizable={normalizable}")

    for name, expected in EXPECTED_ALL_REPLAY.items():
        value = _counter(stats, name)
        add(f"zero:{name}", value == expected, f"{name}={value} expected={expected}")

    replays = _counter(stats, "events_replay_noop")
    link_replays = _counter(stats, "extraction_links_replay_noop")
    add("all_events_replay_noop",
        normalizable is not None and replays == normalizable,
        f"events_replay_noop={replays} normalizable={normalizable}")
    add("all_links_replay_noop",
        normalizable is not None and link_replays == normalizable,
        f"extraction_links_replay_noop={link_replays} normalizable={normalizable}")

    # -- fingerprint must be identical before and after --------------------
    before_fp = preflight.get("fingerprint") or {}
    after_fp = dict(fingerprint_after) if fingerprint_after is not None else None
    if after_fp is None:
        add("fingerprint_after_present", False, "no postflight fingerprint captured")
    else:
        add("fingerprint_after_present", True, "postflight fingerprint captured")
        add("fingerprint_before_complete",
            before_fp.get("code_evidence_complete") is True
            and bool(before_fp.get("modules")),
            f"complete={before_fp.get('code_evidence_complete')!r} "
            f"modules={len(before_fp.get('modules') or {})}")
        add("fingerprint_after_complete",
            after_fp.get("code_evidence_complete") is True and bool(after_fp.get("modules")),
            f"complete={after_fp.get('code_evidence_complete')!r} "
            f"modules={len(after_fp.get('modules') or {})}")
        add("fingerprint_unchanged",
            before_fp.get("code_sha256") == after_fp.get("code_sha256")
            and before_fp.get("modules") == after_fp.get("modules"),
            f"before={before_fp.get('code_sha256')} after={after_fp.get('code_sha256')}")

    # -- target recorded consistently --------------------------------------
    pre_target = preflight.get("target") or {}
    post_target = postflight.get("target") or {}
    add("target_present_postflight", bool(post_target), f"target={post_target!r}")
    add("target_unchanged",
        bool(pre_target) and pre_target == post_target,
        f"pre={pre_target.get('redacted')} post={post_target.get('redacted')}")
    add("target_is_development", pre_target.get("tier") == "development",
        f"tier={pre_target.get('tier')!r}")

    # -- quarantine accounting: explicit, reconciled, never silently omitted --
    pre_q = preflight.get("quarantined_excluded")
    post_q = postflight.get("quarantined_excluded")
    pre_t = preflight.get("extractions_total")
    post_t = postflight.get("extractions_total")
    quarantine_present = all(value is not None for value in (pre_q, post_q, pre_t, post_t))
    add(
        "quarantine_counts_present",
        quarantine_present,
        f"quarantined {pre_q}->{post_q}, total {pre_t}->{post_t}",
    )
    if quarantine_present:
        add(
            "quarantine_counts_unchanged",
            pre_q == post_q and pre_t == post_t,
            f"quarantined {pre_q}->{post_q}, total {pre_t}->{post_t}",
        )
        add(
            "quarantine_reconciles",
            population is not None and int(pre_q) + int(population) == int(pre_t),
            f"eligible {population} + quarantined {pre_q} vs total {pre_t}",
        )

    # -- gate-table deltas, computed from both snapshots --------------------
    pre_counts = preflight.get("gate_tables")
    post_counts = postflight.get("gate_tables")
    if not isinstance(pre_counts, Mapping) or not pre_counts:
        add("gate_tables_present", False, "preflight recorded no gate tables")
    elif not isinstance(post_counts, Mapping) or not post_counts:
        add("gate_tables_present", False, "postflight recorded no gate tables")
    else:
        add("gate_tables_present", True, f"{len(pre_counts)} tables")
        missing = sorted(set(pre_counts) - set(post_counts))
        add("gate_tables_all_observed", not missing, f"missing={missing}")
        for table in sorted(pre_counts):
            if table not in post_counts:
                add(f"delta:{table}", False, "table absent from the postflight snapshot")
                continue
            delta = int(post_counts[table]) - int(pre_counts[table])
            add(f"delta:{table}", delta == 0,
                f"{table} {pre_counts[table]} -> {post_counts[table]} (delta {delta:+d})")

    # -- integrity metrics, compared key by key -----------------------------
    pre_integrity = preflight.get("integrity")
    post_integrity = postflight.get("integrity")
    if not isinstance(pre_integrity, Mapping) or not pre_integrity:
        add("integrity_present", False, "preflight recorded no integrity metrics")
    elif not isinstance(post_integrity, Mapping) or not post_integrity:
        add("integrity_present", False, "postflight recorded no integrity metrics")
    else:
        add("integrity_present", True, f"{len(pre_integrity)} metrics")
        missing = sorted(set(pre_integrity) - set(post_integrity))
        add("integrity_metrics_all_observed", not missing, f"missing={missing}")
        for metric in sorted(pre_integrity):
            if metric not in post_integrity:
                add(f"integrity:{metric}", False, "metric absent from the postflight snapshot")
                continue
            add(f"integrity:{metric}", pre_integrity[metric] == post_integrity[metric],
                f"{metric} {pre_integrity[metric]} -> {post_integrity[metric]}")

    return GateEvaluation(tuple(checks))
