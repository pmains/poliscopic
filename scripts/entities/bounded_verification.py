#!/usr/bin/env python3
"""Run a small, graph-read-only verification of bounded pipeline phases.

This command is an operational safety gate, not an alternate production
pipeline.  It invokes only phases with a native row limit, always in dry-run
mode, and proves that graph row counts and integrity metrics did not change.
Existing integrity debt is reported as a baseline; only drift during the run
fails the gate.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

_ENTITIES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ENTITIES_DIR.parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from db.core import get_engine
from scripts.entities.detect_entities import (
    GATE_COUNT_TABLES,
    _count_table,
    _integrity_snapshot,
    _schema_contract_violations,
    _unmapped_entity_types,
)

log = logging.getLogger("bounded_verification")
PHOENIX_TZ = ZoneInfo("America/Phoenix")
DEFAULT_LIMIT = 256


@dataclass(frozen=True)
class BoundedPhaseAdapter:
    """Describe a pipeline entry point with a trustworthy native row bound."""

    name: str
    module_path: str
    function_name: str
    count_paths: tuple[tuple[str, ...], ...] = ()

    def load(self) -> Callable[..., dict[str, Any]]:
        """Import and return the phase function only when it will be run."""
        module = importlib.import_module(self.module_path)
        function = getattr(module, self.function_name)
        return function


BOUNDED_PHASES: Mapping[str, BoundedPhaseAdapter] = {
    "sweep_docs": BoundedPhaseAdapter(
        "sweep_docs",
        "scripts.entities.sweep_docs",
        "run_sweep_docs",
        (("docs_processed",),),
    ),
    "role_classifier": BoundedPhaseAdapter(
        "role_classifier",
        "scripts.entities.role_classifier",
        "run_role_classifier",
        (("total_scanned",),),
    ),
    "event_pipeline": BoundedPhaseAdapter(
        "event_pipeline",
        "scripts.entities.event_extractor",
        "run_event_pipeline",
        (
            ("accounting", "extract", "docs"),
            ("accounting", "normalize", "extractions_examined"),
            ("accounting", "link", "events_processed"),
        ),
    ),
}


def _graph_counts(engine: Engine) -> dict[str, int]:
    """Return the graph-table counts protected by the Stage 0 gate."""
    return {table: _count_table(engine, table) for table in GATE_COUNT_TABLES}


def _difference(after: Mapping[str, int], before: Mapping[str, int]) -> dict[str, int]:
    """Calculate named integer deltas for two compatible snapshots."""
    return {name: after[name] - before[name] for name in before}


def _reported_count(result: Mapping[str, Any], path: Sequence[str]) -> Any:
    """Read a nested producer counter by its declarative path."""
    current: Any = result
    for component in path:
        if not isinstance(current, Mapping) or component not in current:
            return None
        current = current[component]
    return current


def _bound_observations(
    adapter: BoundedPhaseAdapter, result: Mapping[str, Any], limit: int
) -> tuple[dict[str, Any], bool]:
    """Return producer scan counts and whether all honor the requested bound."""
    observations = {
        ".".join(path): _reported_count(result, path)
        for path in adapter.count_paths
    }
    honored = all(
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= limit
        for value in observations.values()
    )
    return observations, honored


def _write_artifact(artifact: Mapping[str, Any], output_path: Path | None) -> Path:
    """Atomically persist one durable verification artifact."""
    if output_path is None:
        started_at = datetime.fromisoformat(str(artifact["started_at"]))
        output_path = _REPO_ROOT / "data" / "sync" / (
            f"kg-bounded-verification-{started_at:%Y-%m-%d-%H%M%S}-"
            f"{str(artifact['run_id'])[:8]}.json"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as stream:
        json.dump(artifact, stream, indent=2, sort_keys=True, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    temporary_path.replace(output_path)
    return output_path


def run_bounded_verification(
    engine: Engine,
    *,
    phase_names: Sequence[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
    verbose: bool = False,
    output_path: Path | None = None,
    phase_adapters: Mapping[str, BoundedPhaseAdapter] = BOUNDED_PHASES,
) -> dict[str, Any]:
    """Run bounded dry-run phases and verify that the graph did not mutate.

    Args:
        engine: Database engine used for snapshots and phase execution.
        phase_names: Boundable phases to run. Defaults to every registered one.
        limit: Maximum source rows each phase may inspect.
        force: Ignore phase watermarks while retaining dry-run behavior.
        verbose: Enable producer-level diagnostic logging.
        output_path: Optional artifact destination, primarily for tests and ops.
        phase_adapters: Injectable registry used by isolated tests.

    Returns:
        The complete durable artifact. ``passed`` is false if a phase fails,
        taxonomy/schema checks fail, or any protected count/integrity metric
        changes during the run.
    """
    if limit < 1:
        raise ValueError("bounded verification limit must be at least 1")

    selected_names = list(phase_names or phase_adapters.keys())
    unknown_names = [name for name in selected_names if name not in phase_adapters]
    if unknown_names:
        supported = ", ".join(sorted(phase_adapters))
        raise ValueError(
            f"phases lack a safe bounded adapter: {', '.join(unknown_names)}; "
            f"supported phases: {supported}"
        )

    started_at = datetime.now(PHOENIX_TZ)
    before_counts = _graph_counts(engine)
    before_integrity = _integrity_snapshot(engine)
    phase_results: list[dict[str, Any]] = []

    for phase_name in selected_names:
        adapter = phase_adapters[phase_name]
        phase_started = time.monotonic()
        try:
            reported = adapter.load()(
                engine,
                dry_run=True,
                force=force,
                verbose=verbose,
                limit=limit,
            )
            succeeded = bool(reported.get("success", True))
            error = None if succeeded else str(
                reported.get("error") or "phase returned success=False"
            )
        except Exception as exception:  # producer failure belongs in the artifact
            reported = {}
            succeeded = False
            error = f"{type(exception).__name__}: {exception}"
            log.exception("Bounded phase %s failed", phase_name)

        observations, bound_honored = _bound_observations(adapter, reported, limit)

        phase_results.append(
            {
                "name": phase_name,
                "limit": limit,
                "dry_run": True,
                "success": succeeded,
                "bound_honored": bound_honored,
                "observed_counts": observations,
                "duration_s": round(time.monotonic() - phase_started, 3),
                "error": error,
                "reported": reported,
            }
        )

    after_counts = _graph_counts(engine)
    after_integrity = _integrity_snapshot(engine)
    count_deltas = _difference(after_counts, before_counts)
    integrity_deltas = _difference(after_integrity, before_integrity)
    unmapped_types = _unmapped_entity_types(engine)
    schema_violations = _schema_contract_violations(engine)

    checks = [
        {
            "check": "all_phases_succeeded",
            "ok": all(result["success"] for result in phase_results),
            "detail": [
                result["name"] for result in phase_results if not result["success"]
            ],
        },
        {
            "check": "all_phase_bounds_honored",
            "ok": all(result["bound_honored"] for result in phase_results),
            "detail": {
                result["name"]: result["observed_counts"]
                for result in phase_results
                if not result["bound_honored"]
            },
        },
        {
            "check": "graph_counts_unchanged",
            "ok": all(delta == 0 for delta in count_deltas.values()),
            "detail": count_deltas,
        },
        {
            "check": "integrity_unchanged",
            "ok": all(delta == 0 for delta in integrity_deltas.values()),
            "detail": integrity_deltas,
        },
        {
            "check": "entity_type_taxonomy",
            "ok": not unmapped_types,
            "detail": unmapped_types,
        },
        {
            "check": "graph_schema_contract",
            "ok": not schema_violations,
            "detail": schema_violations,
        },
    ]
    artifact: dict[str, Any] = {
        "run_id": uuid.uuid4().hex,
        "mode": "bounded_graph_read_only",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(PHOENIX_TZ).isoformat(),
        "limit_per_phase": limit,
        "force": force,
        "phases": phase_results,
        "before": {"counts": before_counts, "integrity": before_integrity},
        "after": {"counts": after_counts, "integrity": after_integrity},
        "deltas": {"counts": count_deltas, "integrity": integrity_deltas},
        "checks": checks,
        "passed": all(check["ok"] for check in checks),
    }
    artifact_path = _write_artifact(artifact, output_path)
    artifact["artifact_path"] = str(artifact_path)
    return artifact


def main() -> int:
    """Parse command-line options and return a process exit status."""
    parser = argparse.ArgumentParser(
        description="Run the Stage 0 bounded, graph-read-only verification gate"
    )
    parser.add_argument(
        "--phase",
        action="append",
        choices=sorted(BOUNDED_PHASES),
        help="Bounded phase to run; repeat for multiple (default: all supported)",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    artifact = run_bounded_verification(
        get_engine(),
        phase_names=arguments.phase,
        limit=arguments.limit,
        force=arguments.force,
        verbose=arguments.verbose,
        output_path=arguments.output,
    )
    log.info(
        "Bounded verification %s — artifact: %s",
        "PASSED" if artifact["passed"] else "FAILED",
        artifact["artifact_path"],
    )
    return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
