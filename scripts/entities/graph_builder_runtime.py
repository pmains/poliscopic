"""Phase-level watermarking and result aggregation for the graph builder."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text

from scripts.entities.graph_builder_materialization import load_all_entity_ids, run_source
from scripts.entities.graph_builder_models import Source, SourceStats
from scripts.entities.phase_receipt import RowAccounting, build_phase_receipt

log = logging.getLogger("graph_builder")
WATERMARK_TABLE = "_graph_builder_watermark"
PHOENIX_TZ = ZoneInfo("America/Phoenix")


def run_phase(
    engine: Any,
    *,
    sources: Sequence[Source],
    source_filter: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run supplied sources, honoring current-day Phoenix watermarks unless forced."""
    _ensure_watermark_table(engine)
    watermarks = {} if force else _today_watermarks(engine)
    with engine.connect() as connection:
        entity_cache = load_all_entity_ids(connection)
    totals = SourceStats()
    skipped = 0
    for source in sources:
        if source_filter and source_filter.lower() not in source.name.lower():
            continue
        if source.name in watermarks:
            log.info("[SKIP] %s — last run at %s", source.name, watermarks[source.name])
            skipped += 1
            continue
        try:
            log.info("%s — %s", source.name, source.description)
            with engine.begin() as connection:
                stats = run_source(
                    source,
                    connection,
                    entity_cache,
                    dry_run=dry_run,
                    verbose=verbose,
                )
                if not dry_run:
                    _upsert_watermark(connection, source.name, stats)
            # The source transaction has committed, so sharing its new ids is safe.
            entity_cache.update(stats.new_ids)
            totals.add(stats)
            log.info(
                "  ✓ %d entities, %d edges, %d mentions",
                stats.entities_inserted,
                stats.edges_inserted,
                stats.mentions_inserted,
            )
        except Exception as error:
            log.error("  ✗ Failed: %s", error, exc_info=verbose)
            raise
    return _result(totals, len(sources), skipped, dry_run)


def _ensure_watermark_table(engine: Any) -> None:
    """Create the portable graph-builder watermark table when absent."""
    with engine.begin() as connection:
        connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
            source_name VARCHAR(64) PRIMARY KEY,
            last_run_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            entities_created INTEGER NOT NULL DEFAULT 0,
            edges_created INTEGER NOT NULL DEFAULT 0
        );"""))


def _today_watermarks(engine: Any) -> dict[str, object]:
    """Load only watermarks written on the current Phoenix-local day."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT source_name, last_run_at FROM {WATERMARK_TABLE} "
                "WHERE last_run_at >= :day_start"
            ),
            {"day_start": _phoenix_today_start()},
        ).fetchall()
    return {str(row[0]): row[1] for row in rows}


def _phoenix_today_start() -> datetime:
    """Return today at midnight in the Phoenix timezone."""
    return datetime.now(PHOENIX_TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def _upsert_watermark(connection: Any, source_name: str, stats: SourceStats) -> None:
    """Record committed legacy source totals after a successful source run."""
    connection.execute(text(f"""INSERT INTO {WATERMARK_TABLE}
        (source_name, last_run_at, entities_created, edges_created)
        VALUES (:name, CURRENT_TIMESTAMP, :entities, :edges)
        ON CONFLICT (source_name) DO UPDATE SET last_run_at = CURRENT_TIMESTAMP,
            entities_created = :entities, edges_created = :edges"""), {
        "name": source_name, "entities": stats.entities_inserted,
        "edges": stats.edges_inserted,
    })


def _result(totals: SourceStats, source_count: int, skipped: int, dry_run: bool) -> dict[str, Any]:
    """Build the stable public result shape used by phase verification.

    The per-phase ontology-emission receipt is derived from ``SourceStats``,
    which stays the row-accounting authority for this producer, and from the
    ontology-bearing values the sources actually emitted.
    """
    return {
        "success": True,
        "entities_created": totals.entities_inserted,
        "edges_created": totals.edges_inserted,
        **totals.as_dict(),
        "sources_total": source_count,
        "sources_skipped": skipped,
        "dry_run": dry_run,
        "validation_receipt": build_phase_receipt(
            "graph_builder",
            dry_run=dry_run,
            values=totals.emitted_values,
            rows=RowAccounting(**totals.proposal_accounting(dry_run=dry_run)),
        ),
    }
