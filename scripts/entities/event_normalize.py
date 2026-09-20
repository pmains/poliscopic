#!/usr/bin/env python3
"""event_normalize.py — CLI facade over the event normalization runtime.

This module is deliberately thin.  It owns three things and nothing else:

1. the producer's verb vocabulary, re-exported from
   :mod:`scripts.entities.event_vocabulary` so ``producer_vocabulary`` and
   ``event_normalize_models`` keep importing it from here;
2. the compatibility entry point ``normalize(engine, limit, dry_run, force)``,
   which delegates entirely to
   :func:`scripts.entities.event_normalize_runtime.normalize`;
3. the CLI: argument parsing, one JSON child envelope, and the process exit code.

Page iteration, atomic bundle validation, classification, transactional writes
and the sealed validation receipt all live in the runtime.  There is no SQL, no
verb→type resolution and no write loop here.

Usage:
    PYTHONPATH=scripts python3 scripts/entities/event_normalize.py
    PYTHONPATH=scripts python3 scripts/entities/event_normalize.py --dry-run
    PYTHONPATH=scripts python3 scripts/entities/event_normalize.py --limit 1000
    PYTHONPATH=scripts python3 scripts/entities/event_normalize.py --force
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

_ENTITIES_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_ENTITIES_DIR)
_ROOT_DIR = os.path.dirname(_SCRIPTS_DIR)
for _path in (_ROOT_DIR, _SCRIPTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from db import get_engine  # noqa: E402
from scripts.entities.event_normalize_runtime import (  # noqa: E402
    NormalizationRunError,
    normalize as run_normalize,
)
from scripts.entities.event_vocabulary import (  # noqa: E402
    PROCEDURAL_OUTCOMES,
    VERB_MAP,
    normalize_verb,
)

__all__ = [
    "PROCEDURAL_OUTCOMES",
    "VERB_MAP",
    "normalize",
    "normalize_verb",
]

log = logging.getLogger("event_normalize")

#: Version reported alongside the runtime's receipts.
NORMALIZER_VERSION = "2026-07-27.1"

#: Retained for import compatibility with callers that read the old batch size.
BATCH_SIZE = 500

#: The child envelope's step name, and the orchestrator's phase step label.
STEP_NAME = "normalize"


def normalize(
    engine,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Normalize extractions into canonical events, returning cumulative stats.

    All behavior is delegated to the runtime, which owns page iteration, atomic
    bundle validation, row classification and the single sealed receipt.  On any
    ordinary failure the runtime raises ``NormalizationRunError`` carrying the
    cumulative stats and the sealed receipt; process-control signals are not
    wrapped.
    """
    return run_normalize(engine, limit=limit, dry_run=dry_run, force=force)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 5 normalizer")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and account without writing")
    parser.add_argument("--force", action="store_true",
                        help="Also revalidate already-linked extractions")
    parser.add_argument("--limit", type=int, default=None,
                        help="Bound how many extractions are examined")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    engine = get_engine()
    try:
        stats = normalize(
            engine, limit=args.limit, dry_run=args.dry_run, force=args.force
        )
    except NormalizationRunError as error:
        # The runtime seals exactly one receipt and carries it on the error, so
        # the failure envelope is as complete as the success envelope.
        log.error("normalize failed: %s", error)
        log.error("normalize stats: %s", json.dumps(error.stats, default=str))
        print(json.dumps(
            {"step": STEP_NAME, "success": False, "stats": error.stats},
            default=str,
        ))
        return 1

    log.info(
        "normalize done: %d examined, %d planned, %d inserted, %d links updated",
        stats["extractions_examined"], stats["events_planned"],
        stats["events_inserted"], stats["extraction_links_updated"],
    )
    print(json.dumps(
        {"step": STEP_NAME, "success": True, "stats": stats}, default=str
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
