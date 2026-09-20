#!/usr/bin/env python3
"""
event_extractor — Three-stage event extraction pipeline.

Orchestrates:
  Step 1: event_extract.py   — Pattern extraction from Meeting Result docs
  Step 2: event_normalize.py — Normalize extractions to canonical events
  Step 3: event_link.py      — Link events to entity graph

Each step is idempotent and watermark-tracked. Unprocessed records from
previous runs are picked up automatically.

Usage:
    PYTHONPATH=scripts python3 scripts/entities/event_extractor.py
    PYTHONPATH=scripts python3 scripts/entities/event_extractor.py --dry-run
    PYTHONPATH=scripts python3 scripts/entities/event_extractor.py --step extract
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import get_engine
from entities.accounting import invalid_counter_fields
from entities.event_normalize_accounting import child_accounting_error
from sqlalchemy import text

log = logging.getLogger("event_extractor")

#: Child CLI flags each step actually supports.  A flag is passed to a step only
#: when that step declares it, so ``--force`` reaches normalize alone: extract and
#: link have no replay semantics and would reject an unsupported argument.
STEPS = {
    "extract": {
        "script": "scripts/entities/event_extract.py",
        "description": "Pattern extraction from Meeting Result docs",
        "flags": frozenset({"--dry-run", "--limit"}),
    },
    "normalize": {
        "script": "scripts/entities/event_normalize.py",
        "description": "Normalize extractions to canonical events",
        "flags": frozenset({"--dry-run", "--force", "--limit"}),
    },
    "link": {
        "script": "scripts/entities/event_link.py",
        "description": "Link events to entity graph",
        "flags": frozenset({"--dry-run", "--limit"}),
    },
}

REQUIRED_STEP_FIELDS = {
    "extract": {"docs", "events_found", "events_inserted", "skipped_existing"},
    "normalize": {
        "extractions_examined", "normalizable", "events_planned",
        "events_inserted", "extraction_links_updated", "skipped_unmapped_type",
    },
    "link": {
        "events_attempted", "participant_attempts", "participants_inserted",
        "participants_updated", "participants_written", "participants_mutated",
        "participant_replay_collisions", "unresolved_names",
        "participants_planned_insert", "participants_planned_update",
    },
}


def _accounting_invariant_error(
    step_name: str, stats: dict, dry_run: bool = False
) -> str | None:
    """Return an error when valid counters describe an impossible outcome."""
    if step_name == "extract":
        classified = stats["events_inserted"] + stats["skipped_existing"]
        if classified > stats["events_found"]:
            return ("extract accounting exceeds events_found: "
                    f"inserted + skipped = {classified}, "
                    f"found = {stats['events_found']}")
    elif step_name == "normalize":
        classified = stats["normalizable"] + stats["skipped_unmapped_type"]
        if classified != stats["extractions_examined"]:
            return ("normalize extraction accounting does not balance: "
                    f"normalizable + skipped = {classified}, "
                    f"examined = {stats['extractions_examined']}")
        work_error = child_accounting_error(stats)
        if work_error:
            return work_error
        if stats["events_inserted"] > stats["events_planned"]:
            return ("normalize inserted events exceed planned events: "
                    f"inserted = {stats['events_inserted']}, "
                    f"planned = {stats['events_planned']}")
        if stats["extraction_links_updated"] != stats["events_inserted"]:
            return ("normalize event/link accounting does not balance: "
                    f"links updated = {stats['extraction_links_updated']}, "
                    f"events inserted = {stats['events_inserted']}")
    elif step_name == "link":
        planned_mutations = (
            stats["participants_planned_insert"]
            + stats["participants_planned_update"]
        )
        classified = planned_mutations + stats["participant_replay_collisions"]
        actual_mutations = (
            stats["participants_inserted"] + stats["participants_updated"]
        )
        if classified != stats["participant_attempts"]:
            return ("link participant accounting does not balance: "
                    f"classified outcomes = {classified}, "
                    f"attempts = {stats['participant_attempts']}")
        if stats["participants_written"] != actual_mutations:
            return ("link written participant accounting does not balance: "
                    f"written = {stats['participants_written']}, "
                    f"actual mutations = {actual_mutations}")
        if stats["participants_mutated"] != actual_mutations:
            return ("link mutated participant accounting does not balance: "
                    f"mutated = {stats['participants_mutated']}, "
                    f"actual mutations = {actual_mutations}")
        if dry_run and actual_mutations != 0:
            return ("link dry-run reported participant mutations: "
                    f"actual mutations = {actual_mutations}")
        if not dry_run and actual_mutations != planned_mutations:
            return ("link live participant accounting does not balance: "
                    f"actual mutations = {actual_mutations}, "
                    f"planned mutations = {planned_mutations}")
    return None


def _parse_step_result(step_name: str, stdout: str) -> tuple[dict | None, str | None]:
    """Parse and validate the final JSON envelope emitted by an event step."""
    envelope = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            envelope = candidate
            break
    if envelope is None:
        return None, "missing JSON result envelope"
    if envelope.get("step") != step_name:
        return None, f"result step mismatch: expected {step_name!r}, got {envelope.get('step')!r}"
    if envelope.get("success") is not True:
        return None, "step result did not report success=true"
    stats = envelope.get("stats")
    if not isinstance(stats, dict):
        return None, "result envelope has no stats object"
    missing = sorted(REQUIRED_STEP_FIELDS[step_name] - set(stats))
    if missing:
        return None, f"result missing required accounting fields: {missing}"
    invalid = invalid_counter_fields(stats, REQUIRED_STEP_FIELDS[step_name])
    if invalid:
        return None, f"result has invalid accounting counters: {invalid}"
    invariant_error = _accounting_invariant_error(
        step_name, stats, dry_run=bool(envelope.get("dry_run", False))
    )
    if invariant_error:
        return None, invariant_error
    return envelope, None


def _stderr_log_level(line: str) -> int:
    """Return the embedded standard log level, defaulting unstructured text to warning."""
    match = re.search(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]", line)
    if match is None:
        return logging.WARNING
    return getattr(logging, match.group(1))


def run_step(step_name: str, extra_args: list[str] | None = None) -> dict:
    """Run one step via subprocess. Returns elapsed time and return code."""
    step = STEPS[step_name]
    script = os.path.join(
        os.path.dirname(__file__), "..", "..", step["script"]
    )
    cmd = [sys.executable, "-u", script]
    if extra_args:
        cmd.extend(extra_args)

    log.info("Starting step '%s': %s", step_name, step["description"])
    start = time.time()

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(script))

    elapsed = time.time() - start
    for line in result.stdout.strip().split("\n"):
        if line:
            log.info("  [%s] %s", step_name, line)
    if result.stderr.strip():
        for line in result.stderr.strip().split("\n"):
            if line:
                log.log(_stderr_log_level(line), "  [%s] %s", step_name, line)

    envelope, contract_error = _parse_step_result(step_name, result.stdout)
    ok = result.returncode == 0 and contract_error is None
    status = "OK" if ok else f"FAILED (exit {result.returncode})"
    if contract_error:
        status = f"FAILED (result contract: {contract_error})"
        log.error("Step '%s' result contract failed: %s", step_name, contract_error)
    log.info("Step '%s': %s — %.1fs", step_name, status, elapsed)

    return {
        "step": step_name,
        "ok": ok,
        "elapsed": elapsed,
        "returncode": result.returncode,
        "result": envelope,
        "contract_error": contract_error,
    }


def count_pending(engine) -> dict:
    """Count pending work for each step."""
    counts = {}

    # Each query in its own connection to avoid transaction poisoning
    # Step 1: supporting_docs not yet extracted
    try:
        with engine.connect() as c:
            wm = c.execute(text("""
                SELECT COALESCE(MAX(last_doc_id), 0)
                FROM _event_extract_watermark
            """)).scalar()
            r = c.execute(text("""
                SELECT COUNT(*) FROM supporting_documents
                WHERE id > :wm
                  AND document_type = 'Meeting Result'
                  AND text_content IS NOT NULL AND text_content != ''
            """), {"wm": wm}).scalar()
            counts["extract"] = r
    except Exception:
        counts["extract"] = "?"  # Table may not exist yet, watermarked not started

    # Step 2: extractions without meeting_event_id
    with engine.connect() as c:
        r = c.execute(text("""
            SELECT COUNT(*) FROM meeting_event_extractions
            WHERE meeting_event_id IS NULL
        """)).scalar()
        counts["normalize"] = r

    # Step 3: events without participants
    with engine.connect() as c:
        r = c.execute(text("""
            SELECT COUNT(*) FROM meeting_events e
            WHERE NOT EXISTS (
                SELECT 1 FROM event_participants ep
                WHERE ep.meeting_event_id = e.id
            )
        """)).scalar()
        counts["link"] = r

    return counts


def run_event_pipeline(
    engine,
    steps: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = False,
    limit: int | None = None,
    **kwargs,
) -> dict:
    """Run event extraction pipeline. Returns structured result dict.

    Runs sub-steps sequentially: extract → normalize → link.
    Each sub-step is still called via subprocess (the sub-step scripts have
    their own complex logic and model artifacts). This function provides
    the orchestration layer as a library call instead of shelling out.
    """
    pending = count_pending(engine)
    step_results = []
    accounting = {}
    total_elapsed = 0.0

    if steps is None:
        steps = list(STEPS.keys())

    # Flags are requested once, then filtered per step, so each child receives
    # only what it declares support for.
    requested: list[tuple[str, list[str]]] = []
    if dry_run:
        requested.append(("--dry-run", []))
    if force:
        requested.append(("--force", []))
    if limit:
        requested.append(("--limit", [str(limit)]))

    for step_name in steps:
        supported = STEPS[step_name].get("flags", frozenset())
        step_extra: list[str] = []
        for flag, values in requested:
            if flag in supported:
                step_extra.append(flag)
                step_extra.extend(values)
        result = run_step(step_name, extra_args=step_extra or None)
        step_results.append(result)
        if result.get("result"):
            accounting[step_name] = result["result"]["stats"]
        total_elapsed += result.get("elapsed", 0)
        if not result.get("ok"):
            log.error("Pipeline failed at step '%s'", step_name)
            return {
                "success": False,
                "duration_s": round(total_elapsed, 1),
                "steps": step_results,
                "accounting": accounting,
                "failed_at": step_name,
                "dry_run": dry_run,
            }

    receipts = {
        name: payload["validation_receipt"]
        for name, payload in accounting.items()
        if isinstance(payload, dict) and payload.get("validation_receipt")
    }

    from scripts.kg import registries as kg_registries
    from scripts.kg.phase_envelope import (
        REQUIRED_COMPONENT_STEPS,
        build_envelope,
    )
    from scripts.kg.producer_versions import declared_producer_version

    # The phase exposes exactly ONE orchestration-consumable receipt: an envelope
    # binding the per-domain component receipts.  Normalization and linking count
    # different things over different vocabularies, so their totals are never
    # summed into one assertion stream.
    component_steps = [
        step for step in REQUIRED_COMPONENT_STEPS if step in steps
    ]
    envelope = build_envelope(
        producer="event_pipeline",
        producer_version=declared_producer_version("event_pipeline") or "unknown",
        dry_run=dry_run,
        model_version=kg_registries.MODEL_VERSION,
        registry_snapshot=kg_registries.snapshot_sha256(),
        selected_steps=component_steps,
        components=[(name, receipts[name]) for name in sorted(receipts)],
    )

    return {
        "success": True,
        "duration_s": round(total_elapsed, 1),
        "steps": step_results,
        "accounting": accounting,
        "pending": pending,
        "dry_run": dry_run,
        # Phase-level: one envelope for the orchestrator.
        "validation_receipt": envelope,
        # Additive: the per-step receipts stay available as component evidence.
        "validation_receipts": receipts,
    }


def main():
    parser = argparse.ArgumentParser(description="Event extraction pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pending work without running")
    parser.add_argument("--step", choices=list(STEPS.keys()),
                        help="Run only this step")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit per step (passes through)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.dry_run else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    engine = get_engine()

    # Show pending work
    pending = count_pending(engine)
    log.info("Pending work:")
    for step_name in STEPS:
        log.info("  %s: %s pending", step_name, pending[step_name])

    if args.dry_run:
        log.info("Dry run — no steps executed")
        print(json.dumps({
            "phase": "event_pipeline",
            "success": True,
            "dry_run": True,
            "pending": pending,
            "steps": [],
        }))
        return

    # Determine which steps to run
    steps = [args.step] if args.step else None

    result = run_event_pipeline(engine, steps=steps, dry_run=args.dry_run, limit=args.limit)

    if result["success"]:
        log.info("Pipeline complete — all steps OK, %.1fs total", result["duration_s"])
    else:
        log.error("Pipeline failed at step '%s'", result["failed_at"])

    print(json.dumps({"phase": "event_pipeline", **result}))


if __name__ == "__main__":
    main()
