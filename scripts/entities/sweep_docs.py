#!/usr/bin/env python3
"""
sweep_docs.py — Entity extraction from supporting_documents text_content.

Public facade for the document sweep.  The implementation is decomposed into
sibling modules, all re-exported here so existing imports keep working:

  sweep_docs_extraction.py  extraction patterns, known organizations, candidate
                            extraction, name normalization and text cleaning
  sweep_docs_planning.py    assertion identities and pure row classification
  sweep_docs_payloads.py    deterministic write-payload selection
  sweep_docs_storage.py     snapshot loading, planned persistence, swept-state
  sweep_docs_batch.py       the per-batch pipeline

This module owns the public names, the watermark/runtime loop, and the CLI.

Sweeps all supporting_documents that have extracted text (text_content IS NOT NULL)
and haven't been swept yet (swept_at IS NULL). Runs the same extractors as
sweep_meetings.py (patterns + known org matching) but against document text.

Idempotent: skips docs that already have swept_at set, or where text_content
is NULL/empty.

Usage:
  DATABASE_URL=postgresql://... python scripts/entities/sweep_docs.py
  DATABASE_URL=postgresql://... python scripts/entities/sweep_docs.py --dry-run
  DATABASE_URL=postgresql://... python scripts/entities/sweep_docs.py --limit 500
"""

from __future__ import annotations

# Direct CLI execution only (``python scripts/entities/sweep_docs.py``): re-enter
# through the canonical ``scripts.entities`` namespace, so no sibling is ever
# loaded a second time under ``entities.*``.
if __package__ in (None, ""):  # pragma: no cover - CLI entry point only
    import os as _os
    import sys as _sys

    _REPO_ROOT = _os.path.dirname(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    )
    if _REPO_ROOT not in _sys.path:
        _sys.path.insert(0, _REPO_ROOT)
    from scripts.entities.sweep_docs import main as _main

    _main()
    raise SystemExit(0)

import argparse
import json
import logging
import sys

from .sweep_docs_batch import run_batch
from .sweep_docs_extraction import (
    BOS_CASE_RE,
    CASE_NUMBER_RE,
    GENERAL_PATTERNS,
    KNOWN_ORGANIZATIONS,
    KNOWN_ORG_RE,
    MAX_MATCH_LEN,
    clean_text,
    extract_entities_from_doc,
    is_garbage_text,
    normalize_name,
    validate_name,
)
from .sweep_docs_planning import (
    DEFAULT_SOURCE_TYPE,
    ClassificationPlan,
    EntityAssertion,
    ExtractedCandidate,
    MentionAssertion,
    PlanInvariantError,
    build_classification_plan,
)
from .sweep_docs_payloads import (
    EntityPayload,
    MentionPayload,
    select_write_payloads,
)
from .sweep_docs_storage import (
    SOURCE_TYPE,
    _load_existing_entity_assertions,
    _load_existing_mention_assertions,
    _write_entities,
    _write_mentions,
    mark_docs_swept,
    record_batch_failure,
)

__all__ = [
    "BATCH_SIZE",
    "BOS_CASE_RE",
    "CASE_NUMBER_RE",
    "ClassificationPlan",
    "DEFAULT_SOURCE_TYPE",
    "EntityAssertion",
    "EntityPayload",
    "ExtractedCandidate",
    "GENERAL_PATTERNS",
    "KNOWN_ORGANIZATIONS",
    "KNOWN_ORG_RE",
    "MAX_MATCH_LEN",
    "MentionAssertion",
    "MentionPayload",
    "PlanInvariantError",
    "SOURCE_TYPE",
    "WATERMARK_TABLE",
    "_load_existing_entity_assertions",
    "_load_existing_mention_assertions",
    "_write_entities",
    "_write_mentions",
    "build_classification_plan",
    "clean_text",
    "extract_entities_from_doc",
    "is_garbage_text",
    "main",
    "mark_docs_swept",
    "normalize_name",
    "process_batch",
    "record_batch_failure",
    "run_batch",
    "run_sweep_docs",
    "select_write_payloads",
    "validate_name",
]

log = logging.getLogger("sweep_docs")
WATERMARK_TABLE = "_sweep_docs_watermark"
BATCH_SIZE = 200


def process_batch(conn, wm: int, entity_cache: dict,
                  dry_run: bool = False, verbose: bool = False,
                  validator=None) -> dict:
    """Run one batch of supporting documents. Returns a stats dict.

    Thin, seam-preserving entry point.  ``extract_entities_from_doc`` and
    ``_write_mentions`` are resolved from this module's globals at call time, so
    ``monkeypatch.setattr(sweep_docs, "extract_entities_from_doc", ...)`` and
    ``monkeypatch.setattr(sweep_docs, "_write_mentions", ...)`` keep working
    after the decomposition.
    """
    return run_batch(
        conn, wm, entity_cache,
        batch_size=BATCH_SIZE,
        extractor=extract_entities_from_doc,
        mention_writer=_write_mentions,
        dry_run=dry_run, verbose=verbose, validator=validator,
    )


# ── Library Entry Point ──────────────────────────────────────────────────

def run_sweep_docs(
    engine,
    dry_run: bool = False,
    verbose: bool = False,
    force: bool = False,
    limit: int | None = None,
    batch_size: int = 200,
    **kwargs,
) -> dict:
    """Run sweep_docs phase. Returns structured result dict.

    Sweeps supporting_documents.text_content for entity mentions,
    creates entities and entity_mentions, marks docs as swept.
    """
    global BATCH_SIZE

    from scripts.kg.emission import EmissionValidator
    from scripts.kg.producer_versions import declared_producer_version

    # Bind the receipt to the authoritative declaration rather than repeating the
    # version here: the orchestrator checks the sealed value against this same
    # registry, so a literal would be a second source of truth to drift.
    producer_version = declared_producer_version("sweep_docs")
    if producer_version is None:
        raise RuntimeError(
            "sweep_docs has no declared producer version; declare it in "
            "scripts.kg.producer_versions.PRODUCER_VERSIONS"
        )
    validator = EmissionValidator("sweep_docs", producer_version, dry_run=dry_run)
    if batch_size < 1:
        raise ValueError("sweep-docs batch size must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("sweep-docs limit must be at least 1")
    BATCH_SIZE = batch_size

    # Ensure watermark table
    with engine.begin() as conn:
        conn.execute(__import__("sqlalchemy").text(f"""
            CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                last_run_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                last_processed_id INTEGER DEFAULT 0,
                docs_processed INTEGER DEFAULT 0,
                entities_created INTEGER DEFAULT 0,
                mentions_created INTEGER DEFAULT 0
            );
        """))

    # Load entity cache
    entity_cache: dict[str, int] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT normalized_name, entity_type, id FROM entities"
            )
        ).fetchall()
        for r in rows:
            entity_cache[f"{str(r[0])}|{str(r[1])}"] = int(r[2])

    # Load watermark
    wm = 0
    if not dry_run:
        with engine.connect() as conn:
            row = conn.execute(
                __import__("sqlalchemy").text(
                    f"SELECT last_processed_id FROM {WATERMARK_TABLE} ORDER BY last_run_at DESC LIMIT 1"
                )
            ).fetchone()
            if row:
                wm = int(row[0])

    # Get total unscanned
    with engine.connect() as conn:
        total_unscanned = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM supporting_documents "
                "WHERE swept_at IS NULL "
                "AND text_content IS NOT NULL AND text_content != ''"
            )
        ).scalar()
        total_all = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM supporting_documents "
                "WHERE text_content IS NOT NULL AND text_content != ''"
            )
        ).scalar()
    log.info("Supporting docs: %d/%d with text unscanned (watermark id=%d)",
             total_unscanned, total_all, wm)

    if limit:
        total_unscanned = min(total_unscanned, limit)
        log.info("  (limited to %d docs)", total_unscanned)

    grand_total = {"processed": 0, "matches": 0, "entities": 0, "mentions": 0,
                   "unrepresentable_existing_entities": 0,
                   "unrepresentable_existing_mentions": 0,
                   "blank_identity_candidates": 0}
    done = False
    loops = 0

    while not done:
        if limit and grand_total["processed"] >= limit:
            log.info("  Reached limit of %d docs", limit)
            break

        # The SQL query is batch-bounded. Shrink the final batch to the exact
        # remaining allowance so --limit never silently processes a full
        # configured batch beyond the requested verification boundary.
        if limit is not None:
            remaining = limit - grand_total["processed"]
            BATCH_SIZE = min(batch_size, remaining)
        else:
            BATCH_SIZE = batch_size

        try:
            with engine.begin() as conn:
                stats = process_batch(conn, wm, entity_cache,
                                      dry_run=dry_run, verbose=verbose,
                                      validator=validator)
            if not dry_run:
                committed = (int(stats.get("entities", 0))
                             + int(stats.get("mentions", 0)))
                # Only rows actually persisted are committed.  A row that turned
                # out to already exist was classified as a replay no-op by the
                # plan, or reclassified by exact assertion identity when the
                # write revealed a conflict -- never by an aggregate
                # proposed-minus-committed reconciliation.
                if committed:
                    validator.commit(committed)

            for k in grand_total:
                grand_total[k] += stats.get(k, 0)

            done = stats.get("done", True)
            wm = stats.get("max_id", wm)

            # Update watermark
            if not dry_run and stats["max_id"] > 0:
                with engine.begin() as conn:
                    conn.execute(
                        __import__("sqlalchemy").text(f"""
                            INSERT INTO {WATERMARK_TABLE}
                                (last_run_at, last_processed_id, docs_processed,
                                 entities_created, mentions_created)
                            VALUES (now(), :mid, :dp, :ec, :mc)
                        """),
                        {"mid": stats["max_id"],
                         "dp": stats["processed"],
                         "ec": stats["entities"],
                         "mc": stats["mentions"]},
                    )

            loops += 1
            if loops > 0 and loops % 50 == 0:
                log.info("  Progress: %d docs, %d matches, %d entities, %d mentions",
                         grand_total["processed"], grand_total["matches"],
                         grand_total["entities"], grand_total["mentions"])

        except Exception as e:
            # A failed batch fails the phase.  It must never log an exception and
            # then report success.
            #
            # Rollback accounting applies only when writes were actually
            # attempted.  In dry mode no statement is ever issued, so nothing can
            # have been rolled back: the planned-but-uncommitted totals are the
            # *expected* state of a preview, not lost mutations.  Recording them
            # as ``rows_rolled_back`` would claim a mutation that never happened
            # (and is exactly what the dry-mode mutation check refuses).
            log.error("  Batch error at wm=%d: %s", wm, e, exc_info=verbose)
            reason = f"{type(e).__name__}: {e}"
            # Dry mode must record *no* mutation: nothing was ever attempted.
            record_batch_failure(validator, dry_run, reason)
            return {
                "success": False,
                "error": reason,
                "failed_at_watermark": wm,
                "docs_processed": grand_total["processed"],
                "matches": grand_total["matches"],
                "entities_created": grand_total["entities"],
                "mentions_created": grand_total["mentions"],
                "unrepresentable_existing_entities":
                    grand_total["unrepresentable_existing_entities"],
                "unrepresentable_existing_mentions":
                    grand_total["unrepresentable_existing_mentions"],
                "blank_identity_candidates":
                    grand_total["blank_identity_candidates"],
                "dry_run": dry_run,
                "validation_receipt": validator.seal().serialize(),
            }

    return {
        "success": True,
        "docs_processed": grand_total["processed"],
        "matches": grand_total["matches"],
        "entities_created": grand_total["entities"],
        "mentions_created": grand_total["mentions"],
        "unrepresentable_existing_entities":
            grand_total["unrepresentable_existing_entities"],
        "unrepresentable_existing_mentions":
            grand_total["unrepresentable_existing_mentions"],
        "blank_identity_candidates":
            grand_total["blank_identity_candidates"],
        "dry_run": dry_run,
        "validation_receipt": validator.seal().serialize(),
    }


# ── CLI Entry Point ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sweep supporting documents for entity extraction")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max documents to process (default: all)")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="Docs per batch (default 200)")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S",
                        stream=sys.stdout,
                        force=True)

    from db.config import DATABASE_URL as _DB_URL
    from sqlalchemy import create_engine

    engine = create_engine(
        _DB_URL,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        future=True,
    )

    result = run_sweep_docs(
        engine,
        dry_run=args.dry_run,
        verbose=args.verbose,
        limit=args.limit,
        batch_size=args.batch_size or 200,
    )

    mode = "DRY RUN" if result["dry_run"] else "DONE"
    log.info("%s — %d docs, %d matches, %d entities, %d mentions",
             mode, result["docs_processed"], result["matches"],
             result["entities_created"], result["mentions_created"])

    print(json.dumps({"phase": "sweep_docs", **result}))


if __name__ == "__main__":
    main()
