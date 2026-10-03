#!/usr/bin/env python3
"""Decide whether a day's post-scrape entity run passed its verification gate.

Used by ``scripts/sync/sync_completion_check.sh`` to keep a shell script from
having to parse JSON (and to avoid a heredoc inside a bash command substitution,
which bash 3.2 on macOS cannot parse).

Prints ``ok`` and exits 0 only when the state is present, well-formed, belongs to
the requested date, and shows a fully successful run. Otherwise prints a precise
refusal reason and exits 1. Fail-closed on every anomaly: missing, unreadable,
empty, malformed, mismatched lineage, failed gate, failed receipt enforcement, or
any non-ok phase.

Usage:
    entity_gate_verdict.py <entity-run-state.json> <YYYY-MM-DD>
"""
from __future__ import annotations

import json
import sys


REQUIRED_PHASES = {
    "graph_builder",
    "sweep_docs",
    "pattern_cascade",
    "role_classifier",
    "resolver",
    "event_pipeline",
}


def refuse(message: str) -> None:
    print(message)
    raise SystemExit(1)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: entity_gate_verdict.py <state.json> <YYYY-MM-DD>", file=sys.stderr)
        return 2

    path, day = argv[1], argv[2]

    try:
        with open(path) as handle:
            raw = handle.read()
    except OSError as exc:
        refuse(f"entity run state unreadable: {exc}")

    if not raw.strip():
        refuse("entity run state is empty")

    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        refuse(f"entity run state is not valid JSON: {exc}")

    if not isinstance(state, dict):
        refuse("entity run state is not a JSON object")

    # Lineage: must be THIS date's run, not a stale file carried over.
    state_file = str(state.get("state_file") or "")
    if not state_file.startswith(f"entity-run-{day}-"):
        refuse(f"entity run state is not this date's run (state_file={state_file!r})")

    started = str(state.get("started_at") or "")
    if not started.startswith(day):
        refuse(f"entity run started_at is not {day} (started_at={started!r})")

    gate = state.get("gate")
    if not isinstance(gate, dict):
        refuse("entity run state has no gate block")
    if gate.get("failed") is not False:
        failing = [f"{c.get('check')}: {c.get('detail')}"
                   for c in gate.get("checks", []) if not c.get("ok")]
        refuse("entity gate failed: " + ("; ".join(failing) or "gate.failed is not False"))

    receipts = state.get("receipt_enforcement")
    if not isinstance(receipts, dict) or receipts.get("ok") is not True:
        reason = receipts.get("reasons") if isinstance(receipts, dict) else receipts
        refuse(f"entity receipt enforcement not ok: {reason}")

    summary = state.get("summary") or {}
    try:
        phases_failed = int(summary.get("phases_failed") or 0)
    except (TypeError, ValueError):
        refuse(f"entity summary phases_failed is malformed: {summary.get('phases_failed')!r}")
    if phases_failed != 0:
        refuse(f"entity summary reports {phases_failed} failed phase(s)")

    phases = state.get("phases")
    if not isinstance(phases, list):
        refuse("entity run state phases is not a list")
    names = [p.get("name") for p in phases if isinstance(p, dict)]
    if len(names) != len(set(names)):
        refuse("entity run state contains duplicate phases")
    present = set(names)
    if present != REQUIRED_PHASES:
        missing = sorted(REQUIRED_PHASES - present)
        unexpected = sorted(present - REQUIRED_PHASES)
        refuse(f"entity run is not the full six-phase pipeline "
               f"(missing={missing}, unexpected={unexpected})")
    try:
        phases_ran = int(summary.get("phases_ran"))
    except (TypeError, ValueError):
        refuse(f"entity summary phases_ran is malformed: {summary.get('phases_ran')!r}")
    if phases_ran != len(REQUIRED_PHASES):
        refuse(f"entity summary reports phases_ran={phases_ran}, expected 6")

    not_ok = [p.get("name") for p in phases if p.get("status") != "ok"]
    if not_ok:
        refuse(f"entity phase(s) not ok: {not_ok}")

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
