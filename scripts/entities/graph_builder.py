#!/usr/bin/env python3
"""Public CLI façade for structured knowledge-graph materialization.

Implementation is separated into models, structured sources, persistence, and
runtime orchestration.  This module deliberately owns mutable ``SOURCES`` so
existing callers and tests can replace it before calling :func:`run_phase`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

# Support both ``python scripts/entities/graph_builder.py`` and package imports.
_ENTITIES_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_ENTITIES_DIR)
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from db.core import get_engine
from scripts.entities.graph_builder_materialization import load_all_entity_ids, run_source
from scripts.entities.graph_builder_models import (
    EdgeSpec,
    EntitySpec,
    MentionSpec,
    Source,
    SourceStats,
)
from scripts.entities.graph_builder_runtime import WATERMARK_TABLE, run_phase as _run_phase
from scripts.entities.graph_builder_sources import (
    BodyMembershipSource,
    MeetingAttendanceSource,
    PZItemDetailsSource,
    default_sources,
)

log = logging.getLogger("graph_builder")

# Compatibility seam: test code and callers may replace this exact list.
SOURCES: list[Source] = default_sources()


def run_phase(
    engine: Any,
    source_filter: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run every current source and return stable phase accounting."""
    return _run_phase(
        engine,
        sources=SOURCES,
        source_filter=source_filter,
        dry_run=dry_run,
        force=force,
        verbose=verbose,
    )


def main() -> None:
    """Run the graph-builder CLI and emit a single JSON result."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=str)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    result = run_phase(
        get_engine(), source_filter=args.source, dry_run=args.dry_run,
        force=args.force, verbose=args.verbose,
    )
    mode = "DRY RUN" if result["dry_run"] else "DONE"
    log.info(
        "%s — %d entities, %d edges (%d sources, %d skipped)",
        mode, result["entities_created"], result["edges_created"],
        result["sources_total"], result["sources_skipped"],
    )
    print(json.dumps({"phase": "graph_builder", **result}))


if __name__ == "__main__":
    main()
