#!/usr/bin/env python3
"""
detect_entities — Unified entity detection step for the daily pipeline.

Runs all entity pipeline phases in order:

  Phase 1: graph_builder.py     — Structured triples from DB tables
  Phase 2: sweep_docs.py        — Entity extraction from supporting_documents text
  Phase 3: pattern_cascade.py   — Semi-structured header regex on agenda items
  Phase 4: role_classifier.py   — ML role classification (6-signal XGBoost)
  Phase 5: resolver.py          — Entity resolution / dedup
  Phase 6: event_extractor.py   — 3-stage event extraction pipeline

Phases run in dependency order: entity detection (1-4), then dedup (5),
then event extraction + linking (6), which needs clean entities.


Each phase is watermark-tracked and idempotent. Phases that have already
run for today are skipped. Use --force to re-run.

Usage (normal — one step in the pipeline):
    python3 scripts/entities/detect_entities.py

Usage (debugging a specific phase):
    python3 scripts/entities/detect_entities.py --phase pattern_cascade
    python3 scripts/entities/detect_entities.py --phase resolver --verbose
    python3 scripts/entities/detect_entities.py --phase sweep_docs --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

# Phase modules are imported as "scripts.entities.X", which requires the repo
# ROOT on sys.path (so `scripts` is a package). `db.core` requires the scripts
# dir itself. Derive both from __file__ so the pipeline works regardless of CWD.
_ENTITIES_DIR = os.path.dirname(os.path.abspath(__file__))   # .../scripts/entities
_SCRIPTS_DIR = os.path.dirname(_ENTITIES_DIR)                 # .../scripts
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)                    # repo root
for _p in (_REPO_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db.core import get_engine
from scripts.entities.accounting import (
    invalid_counter_fields,
    is_nonnegative_integer,
)
from scripts.entities import failure_classification
from scripts.entities import producer_manifest
from scripts.entities.producer_manifest import PHASE_CODE_MODULES
from scripts.kg import orchestration_receipts

log = logging.getLogger("detect_entities")
WATERMARK_TABLE = "_detect_entities_watermark"

# ── Phase definitions ──────────────────────────────────────────────────────

# Each phase specifies:
#   module_path: dotted Python path to the phase module
#   run_fn_name: name of the library function to call within that module
#   run_fn is lazily resolved by _resolve_phase_fn()

PHASES = [
    {
        "name": "graph_builder",
        "description": "Structured triples from DB tables",
        "module": "scripts.entities.graph_builder",
        # graph_builder's executable behavior is deliberately split across
        # these modules.  Keep this manifest explicit so run evidence captures
        # every implementation component rather than only the CLI façade.
        "code_modules": PHASE_CODE_MODULES["graph_builder"],
        "run_fn_name": "run_phase",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "sweep_docs",
        "description": "Entity extraction from supporting_documents text content",
        "module": "scripts.entities.sweep_docs",
        "code_modules": PHASE_CODE_MODULES["sweep_docs"],
        "run_fn_name": "run_sweep_docs",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "pattern_cascade",
        "description": "Semi-structured header regex (Applicant:, Attorney:, etc.)",
        "module": "scripts.entities.pattern_cascade",
        "run_fn_name": "run_pattern_cascade",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "role_classifier",
        "description": "ML role classification — fastText on entity name + context",
        "module": "scripts.entities.role_classifier",
        "run_fn_name": "run_role_classifier",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "resolver",
        "description": "Entity resolution / dedup",
        "module": "scripts.entities.resolver",
        "run_fn_name": "run_resolver",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "event_pipeline",
        "description": "3-stage event extraction: extract → normalize → link",
        "module": "scripts.entities.event_extractor",
        # The orchestrator directly executes these three step implementations.
        # Keep the complete manifest in run evidence, not just the façade.
        "code_modules": PHASE_CODE_MODULES["event_pipeline"],
        "run_fn_name": "run_event_pipeline",
        "allow_skip": True,
        "critical": False,
    },
]


PHOENIX_TZ = ZoneInfo("America/Phoenix")

# ── Step 2 orchestration knobs (Brief 016) ────────────────────────────────
# Retry a phase up to this many ADDITIONAL attempts after a failure/exception.
# Phases are idempotent + watermark-guarded, so a retry re-runs only what the
# phase's own incremental cursor says is pending. Default 1 retry (2 attempts)
# — phases like resolver can take ~20 min, so retrying 3x is not free.
PHASE_RETRIES = int(os.environ.get("ENTITY_PHASE_RETRIES", "1"))
PHASE_RETRY_BACKOFF_S = int(os.environ.get("ENTITY_PHASE_RETRY_BACKOFF_S", "10"))

# Tables verified by the post-run gate (row deltas + unmapped-type audit).
GATE_COUNT_TABLES = ["entities", "entity_mentions", "entity_relationships",
                     "event_participants", "meeting_events",
                     "meeting_event_extractions"]

# Historical replay debt is checked for non-increase. A relationship pointing
# at a missing provenance source is an active integrity failure and must be
# fully resolved before a live run can pass the gate.
ABSOLUTE_ZERO_INTEGRITY_CHECKS = frozenset({
    "unresolved_relationship_provenance",
})


def _phase_accounting_checks(phase_name: str, raw: dict,
                             deltas: dict[str, int]) -> list[dict]:
    """Compare producer-reported inserts with committed per-phase row deltas.

    Insert counters are reconciled with table deltas. Event participant updates
    are validated from the link-step producer counters because row-count deltas
    cannot measure confidence upgrades.
    """
    mappings = {
        "graph_builder": {
            "entities_inserted": "entities",
            "edges_inserted": "entity_relationships",
            "mentions_inserted": "entity_mentions",
        },
        "sweep_docs": {
            "entities_created": "entities",
            "mentions_created": "entity_mentions",
        },
        "pattern_cascade": {
            "entities_created": "entities",
            "edges_created": "entity_relationships",
        },
    }
    checks = []
    if phase_name == "event_pipeline":
        accounting = raw.get("accounting") or {}
        event_mappings = (
            ("extract", "events_inserted", "meeting_event_extractions"),
            ("normalize", "events_inserted", "meeting_events"),
        )
        for step, reported_field, table in event_mappings:
            stats = accounting.get(step)
            check_name = f"accounting:event_pipeline:{step}:{reported_field}"
            if not isinstance(stats, dict) or reported_field not in stats:
                checks.append({"check": check_name, "ok": False,
                               "detail": "step did not report required count"})
                continue
            reported = stats[reported_field]
            if not is_nonnegative_integer(reported):
                checks.append({"check": check_name, "ok": False,
                               "detail": "producer reported an invalid nonnegative integer count"})
                continue
            committed = int(deltas.get(table, 0))
            checks.append({
                "check": check_name,
                "ok": reported == committed,
                "detail": f"reported {reported}, committed {table} delta {committed:+d}",
            })
        link_stats = accounting.get("link")
        link_insert_check = "accounting:event_pipeline:link:participants_inserted"
        if not isinstance(link_stats, dict):
            checks.append({"check": link_insert_check, "ok": False,
                           "detail": "step did not report required count"})
            return checks

        link_fields = (
            "participant_attempts", "participants_inserted",
            "participants_updated", "participants_written",
            "participants_mutated", "participants_planned_insert",
            "participants_planned_update", "participant_replay_collisions",
        )
        invalid_link = invalid_counter_fields(link_stats, link_fields)
        if invalid_link:
            checks.append({
                "check": "accounting:event_pipeline:link:participant_balance",
                "ok": False,
                "detail": ("missing or invalid nonnegative integer counters: "
                           + ", ".join(invalid_link)),
            })
            return checks

        reported_inserts = link_stats["participants_inserted"]
        committed_inserts = int(deltas.get("event_participants", 0))
        checks.append({
            "check": link_insert_check,
            "ok": reported_inserts == committed_inserts,
            "detail": (f"reported {reported_inserts}, committed "
                       f"event_participants delta {committed_inserts:+d}"),
        })

        planned_mutations = (
            link_stats["participants_planned_insert"]
            + link_stats["participants_planned_update"]
        )
        classified = planned_mutations + link_stats["participant_replay_collisions"]
        actual_mutations = (
            link_stats["participants_inserted"]
            + link_stats["participants_updated"]
        )
        dry_result = bool(raw.get("dry_run", False))
        expected_actual = 0 if dry_result else planned_mutations
        checks.append({
            "check": "accounting:event_pipeline:link:participant_balance",
            "ok": classified == link_stats["participant_attempts"],
            "detail": (f"attempted {link_stats['participant_attempts']}, "
                       f"classified outcomes {classified}"),
        })
        checks.append({
            "check": "accounting:event_pipeline:link:participants_written",
            "ok": (link_stats["participants_written"] == actual_mutations
                   and link_stats["participants_mutated"] == actual_mutations),
            "detail": (f"written {link_stats['participants_written']}, "
                       f"mutated {link_stats['participants_mutated']}, "
                       f"actual mutations {actual_mutations}"),
        })
        checks.append({
            "check": "accounting:event_pipeline:link:participants_updated",
            "ok": actual_mutations == expected_actual,
            "detail": (f"reported {link_stats['participants_updated']} confidence "
                       "updates; row-count delta does not measure updates"),
        })
        return checks
    if phase_name == "graph_builder":
        entity_fields = ("entities_attempted", "entities_inserted",
                         "entities_planned")
        invalid_entities = invalid_counter_fields(raw, entity_fields)
        if invalid_entities:
            checks.append({
                "check": "accounting:graph_builder:entities_balance",
                "ok": False,
                "detail": ("missing or invalid nonnegative integer counters: "
                           + ", ".join(invalid_entities)),
            })
        else:
            attempted = raw["entities_attempted"]
            inserted = raw["entities_inserted"]
            planned = raw["entities_planned"]
            checks.append({
                "check": "accounting:graph_builder:entities_balance",
                "ok": inserted <= attempted and planned == inserted,
                "detail": (f"attempted {attempted}, planned {planned}, "
                           f"inserted {inserted}"),
            })
        balance_specs = (
            ("edges", "edges_attempted", "edges_inserted",
             "edge_replay_collisions", "edges_unresolved_endpoint"),
            ("mentions", "mentions_attempted", "mentions_inserted",
             "mention_replay_collisions", "mentions_unresolved_entity"),
        )
        for (
            output_class, attempted_field, inserted_field, collision_field,
            unresolved_field,
        ) in balance_specs:
            fields = (attempted_field, inserted_field, collision_field, unresolved_field)
            invalid = invalid_counter_fields(raw, fields)
            check_name = f"accounting:graph_builder:{output_class}_balance"
            if invalid:
                checks.append({
                    "check": check_name,
                    "ok": False,
                    "detail": ("missing or invalid nonnegative integer counters: "
                               + ", ".join(invalid)),
                })
                continue
            attempted = raw[attempted_field]
            outcomes = (raw[inserted_field] + raw[collision_field]
                        + raw[unresolved_field])
            checks.append({
                "check": check_name,
                "ok": attempted == outcomes,
                "detail": f"attempted {attempted}, classified outcomes {outcomes}",
            })
        for output_class, field in (
            ("edges", "edges_unresolved_endpoint"),
            ("mentions", "mentions_unresolved_entity"),
        ):
            value = raw.get(field)
            valid = is_nonnegative_integer(value)
            checks.append({
                "check": f"accounting:graph_builder:{output_class}_resolved",
                "ok": valid and value == 0,
                "detail": (f"{field} = {value}" if valid
                           else f"missing or invalid counter: {field}"),
            })
    for reported_field, table in mappings.get(phase_name, {}).items():
        if reported_field not in raw:
            checks.append({
                "check": f"accounting:{phase_name}:{reported_field}",
                "ok": False,
                "detail": "producer did not report required count",
            })
            continue
        reported = raw[reported_field]
        if not is_nonnegative_integer(reported):
            checks.append({
                "check": f"accounting:{phase_name}:{reported_field}",
                "ok": False,
                "detail": "producer reported an invalid nonnegative integer count",
            })
            continue
        committed = int(deltas.get(table, 0))
        checks.append({
            "check": f"accounting:{phase_name}:{reported_field}",
            "ok": reported == committed,
            "detail": f"reported {reported}, committed {table} delta {committed:+d}",
        })
    return checks


_REPLAY_ZERO_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "graph_builder": (
        ("entities_planned",), ("edges_planned",), ("mentions_planned",),
        ("entities_inserted",), ("edges_inserted",), ("mentions_inserted",),
    ),
    "sweep_docs": (("entities_created",), ("mentions_created",)),
    "pattern_cascade": (("entities_created",), ("edges_created",)),
    "role_classifier": (("total_updated",),),
    "resolver": (
        ("phase1_type_conflicts",),
        ("phase2_composites",),
        ("phase3_name_variations",),
    ),
    "event_pipeline": (
        ("accounting", "extract", "events_inserted"),
        # Replay-aware normalize expectations.  A clean all-linked force replay
        # plans no writes and classifies every assertion as a replay no-op, so
        # both planned write counters and the unresolved counter must be zero.
        ("accounting", "normalize", "events_planned"),
        ("accounting", "normalize", "extraction_links_planned"),
        ("accounting", "normalize", "events_inserted"),
        ("accounting", "normalize", "extraction_links_updated"),
        ("accounting", "normalize", "assertions_inconsistent"),
        ("accounting", "normalize", "assertions_refused"),
        ("accounting", "link", "participants_planned_insert"),
        ("accounting", "link", "participants_planned_update"),
        ("accounting", "link", "participants_inserted"),
        ("accounting", "link", "participants_updated"),
        ("accounting", "link", "participants_written"),
        ("accounting", "link", "participants_mutated"),
    ),
}


def _replay_zero_report_checks(phase_name: str, raw: dict) -> list[dict]:
    """Require every phase-specific planned and reported write count to be zero."""
    checks: list[dict] = []
    for path in _REPLAY_ZERO_FIELDS.get(phase_name, ()):
        value: object = raw
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        field = ".".join(path)
        valid = is_nonnegative_integer(value)
        checks.append({
            "check": f"replay_zero:{phase_name}:reported:{field}",
            "ok": valid and value == 0,
            "detail": (
                f"reported {value} (requires zero)"
                if valid else "missing or invalid nonnegative integer count"
            ),
        })
    return checks


def _replay_zero_delta_checks(
    phase_name: str, deltas: dict[str, int]
) -> list[dict]:
    """Require every tracked table count to remain stable within one phase."""
    return [{
        "check": f"replay_zero:{phase_name}:delta:{table}",
        "ok": delta == 0,
        "detail": f"committed delta {delta:+d} (requires zero)",
    } for table, delta in deltas.items()]


def _phase_expected_output_check(phase_name: str, raw: dict,
                                 deltas: dict[str, int], force: bool) -> dict:
    """Classify zero output using producer evidence instead of intuition."""
    invalid_deltas = [name for name, value in deltas.items()
                      if not is_nonnegative_integer(value)]
    if invalid_deltas:
        return {"check": f"expected_output:{phase_name}", "ok": False,
                "detail": ("invalid committed delta counters: "
                           + ", ".join(invalid_deltas))}
    output = sum(max(0, value) for value in deltas.values())
    if phase_name == "event_pipeline":
        return _event_pipeline_expected_output_check(raw, output, force)
    if output:
        return {"check": f"expected_output:{phase_name}", "ok": True,
                "detail": f"committed {output} graph rows"}
    if force:
        return {"check": f"expected_output:{phase_name}", "ok": True,
                "detail": "zero writes on forced replay (expected idempotency)"}

    if phase_name == "pattern_cascade":
        # A normal incremental run can rediscover only assertions that already
        # exist. That is successful idempotent work, not a silent zero-output
        # failure, when every planned mention and edge is terminally accounted
        # for as an insert, replay collision, or unresolved endpoint.
        fields = (
            "mentions_planned", "mentions_created", "mention_replay_collisions",
            "mentions_unresolved_entity", "edges_planned", "edges_created",
            "edge_replay_collisions", "edges_unresolved_endpoint",
        )
        present = [field for field in fields if field in raw]
        invalid = invalid_counter_fields(raw, present)
        if invalid:
            return {"check": f"expected_output:{phase_name}", "ok": False,
                    "detail": ("invalid replay accounting counters: "
                               + ", ".join(invalid))}
        if len(present) == len(fields):
            mentions_accounted = raw["mentions_planned"] == (
                raw["mentions_created"] + raw["mention_replay_collisions"]
                + raw["mentions_unresolved_entity"]
            )
            edges_accounted = raw["edges_planned"] == (
                raw["edges_created"] + raw["edge_replay_collisions"]
                + raw["edges_unresolved_endpoint"]
            )
            replays = (raw["mention_replay_collisions"]
                       + raw["edge_replay_collisions"])
            if mentions_accounted and edges_accounted and replays:
                return {"check": f"expected_output:{phase_name}", "ok": True,
                        "detail": f"zero writes with {replays} replay collisions"}

    input_fields = ("docs_processed", "items_processed", "total_scanned")
    present_input_fields = [field for field in input_fields if field in raw]
    if "matches" in raw:
        present_input_fields.append("matches")
    invalid_inputs = invalid_counter_fields(raw, present_input_fields)
    if invalid_inputs:
        return {"check": f"expected_output:{phase_name}", "ok": False,
                "detail": ("invalid expected-output counters: "
                           + ", ".join(invalid_inputs))}
    observed_inputs = max((raw.get(k, 0) for k in input_fields), default=0)
    match_count = raw.get("matches", 0)
    if observed_inputs == 0:
        return {"check": f"expected_output:{phase_name}", "ok": True,
                "detail": "zero writes with zero reported input"}
    if "matches" in raw and match_count == 0:
        return {"check": f"expected_output:{phase_name}", "ok": True,
                "detail": f"zero writes from {observed_inputs} inputs with zero matches"}
    # Role classification can scan records yet legitimately make no changes.
    if phase_name in {"role_classifier", "resolver"}:
        return {"check": f"expected_output:{phase_name}", "ok": True,
                "detail": "zero inserts permitted for update/resolution phase"}
    return {"check": f"expected_output:{phase_name}", "ok": False,
            "detail": f"unexpected zero writes from {observed_inputs} inputs"}


def _event_pipeline_expected_output_check(
    raw: dict, committed_output: int, force: bool
) -> dict:
    """Classify event-pipeline output using step accounting, including updates."""
    accounting = raw.get("accounting") or {}
    pending = raw.get("pending") or {}

    pending_fields = [field for field in ("extract", "normalize", "link")
                      if field in pending]
    invalid_pending = invalid_counter_fields(pending, pending_fields)
    if invalid_pending:
        return {"check": "expected_output:event_pipeline", "ok": False,
                "detail": "invalid pending event counts: "
                + ", ".join(invalid_pending)}

    link_stats = accounting.get("link")
    if isinstance(link_stats, dict):
        link_fields = (
            "participant_attempts", "participants_inserted",
            "participants_updated", "participants_planned_insert",
            "participants_planned_update", "participant_replay_collisions",
        )
        invalid_link = invalid_counter_fields(link_stats, link_fields)
        if invalid_link:
            return {"check": "expected_output:event_pipeline", "ok": False,
                    "detail": "invalid link accounting counters: "
                    + ", ".join(invalid_link)}
        actual_participant_mutations = (
            link_stats["participants_inserted"]
            + link_stats["participants_updated"]
        )
        planned_participant_mutations = (
            link_stats["participants_planned_insert"]
            + link_stats["participants_planned_update"]
        )
        if committed_output or actual_participant_mutations:
            return {
                "check": "expected_output:event_pipeline",
                "ok": True,
                "detail": (f"committed {committed_output} inserted rows; "
                           f"reported {actual_participant_mutations} "
                           "participant mutations"),
            }
        if planned_participant_mutations:
            return {
                "check": "expected_output:event_pipeline",
                "ok": False,
                "detail": (f"zero mutations reported despite "
                           f"{planned_participant_mutations} planned "
                           "participant mutations"),
            }
        if link_stats["participant_attempts"]:
            return {
                "check": "expected_output:event_pipeline",
                "ok": True,
                "detail": (f"zero writes with "
                           f"{link_stats['participant_attempts']} "
                           "participant replay collisions"),
            }

    pending_work = {name: value for name, value in pending.items()
                    if name in pending_fields and value > 0}
    if pending_work:
        return {"check": "expected_output:event_pipeline", "ok": False,
                "detail": f"zero writes with pending event work: {pending_work}"}
    if force:
        return {"check": "expected_output:event_pipeline", "ok": True,
                "detail": "zero writes on forced replay (zero reported event input)"}
    return {"check": "expected_output:event_pipeline", "ok": True,
            "detail": "zero writes with zero reported event input"}


def _phoenix_today_start() -> datetime:
    """Start of today in Phoenix local time (tz-aware)."""
    now = datetime.now(PHOENIX_TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _get_watermarks(engine, force: bool = False) -> set[str]:
    """Return set of phase names that have already run TODAY (Phoenix local).

    Phases are skipped only when they completed for the current day, so the
    daily sync actually re-runs each phase every morning. Per-phase watermark
    tables (sweep_docs last_processed_id, pattern_cascade per-body cursor,
    etc.) keep those re-runs incremental and idempotent. --force bypasses.
    """
    if force:
        return set()
    # engine.begin() — NOT connect() — so the CREATE TABLE commits. Otherwise
    # the autobegin transaction rolls back on close and the watermark table
    # never persists (latent bug: _mark_watermark would hit UndefinedTable).
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                    phase VARCHAR(32) PRIMARY KEY,
                    last_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    duration_s NUMERIC(8,1) DEFAULT 0,
                    entities_created INTEGER DEFAULT 0,
                    edges_created INTEGER DEFAULT 0
                )
            """)
        )
        # Also ensure resolver has its watermark table
        conn.execute(
            text("""
                CREATE TABLE IF NOT EXISTS _resolver_watermark (
                    phase VARCHAR(32) PRIMARY KEY,
                    last_run_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        )
        conn.execute(
            text("""
                CREATE TABLE IF NOT EXISTS _graph_builder_watermark (
                    source_name VARCHAR(64) PRIMARY KEY,
                    last_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    entities_created INTEGER NOT NULL DEFAULT 0,
                    edges_created INTEGER NOT NULL DEFAULT 0
                )
            """)
        )
        conn.execute(
            text("""
                CREATE TABLE IF NOT EXISTS _pattern_cascade_watermark (
                    body VARCHAR(64) PRIMARY KEY,
                    last_run_at TIMESTAMPTZ DEFAULT now(),
                    last_processed_id INTEGER DEFAULT 0,
                    items_processed INTEGER DEFAULT 0,
                    entities_created INTEGER DEFAULT 0,
                    edges_created INTEGER DEFAULT 0
                )
            """)
        )
        rows = conn.execute(
            text(f"SELECT phase FROM {WATERMARK_TABLE} WHERE last_run_at >= :day_start"),
            {"day_start": _phoenix_today_start()},
        ).fetchall()
        return {r[0] for r in rows}


def _mark_watermark(engine, phase: str, duration_s: float,
                    entities: int = 0, edges: int = 0) -> None:
    """Record that a phase completed."""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                INSERT INTO {WATERMARK_TABLE} (phase, last_run_at, duration_s,
                                               entities_created, edges_created)
                VALUES (:p, now(), :dur, :ec, :edc)
                ON CONFLICT (phase) DO UPDATE SET
                    last_run_at = now(),
                    duration_s = :dur,
                    entities_created = :ec,
                    edges_created = :edc
            """),
            {"p": phase, "dur": duration_s, "ec": entities, "edc": edges},
        )


_PHASE_CACHE: dict[str, object] = {}


def _resolve_phase_fn(phase: dict) -> object | None:
    """Lazily import a phase module and return its run function."""
    module_path = phase.get("module")
    fn_name = phase.get("run_fn_name")
    if not module_path or not fn_name:
        log.warning("  [NOT IMPLEMENTED] %s — no module/run_fn_name", phase["description"])
        return None
    cache_key = f"{module_path}:{fn_name}"
    if cache_key in _PHASE_CACHE:
        return _PHASE_CACHE[cache_key]
    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, fn_name, None)
        if fn is None:
            log.warning("  [MISSING] %s — function %s not found in %s",
                        phase["description"], fn_name, module_path)
            return None
        _PHASE_CACHE[cache_key] = fn
        return fn
    except ImportError as e:
        log.warning("  [MISSING] %s — import error: %s", phase["description"], e)
        return None


def _producer_metadata(phase: dict) -> dict[str, object]:
    """Producer identity and source evidence for ``phase``.

    Thin wrapper so existing importers keep working; the manifest mechanism
    itself lives in :mod:`scripts.entities.producer_manifest`.
    """
    return producer_manifest.producer_metadata(phase)

def _count_table(engine, table: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar())


def _unmapped_entity_types(engine) -> list[tuple[str, int]]:
    """Return entity_type values with no taxonomy leaf (Brief 015 audit)."""
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT e.entity_type, count(*) FROM entities e
            LEFT JOIN entity_types t ON t.entity_type = e.entity_type
            WHERE t.id IS NULL
            GROUP BY e.entity_type ORDER BY 2 DESC
        """)).fetchall()
        return [(r[0], int(r[1])) for r in rows]


#: The Stage 0 integrity invariants, owned here and evaluated on demand.
INTEGRITY_QUERIES = {
        "graph_builder_repeat_excess": """
            SELECT COALESCE(SUM(n - 1), 0) FROM (
              SELECT COUNT(*) n FROM entity_mentions
              WHERE extracted_by='graph_builder'
              GROUP BY entity_id, source_type, source_id, NULLIF(role_in_context, '')
              HAVING COUNT(*) > 1) d""",
        "pattern_extraction_repeat_excess": """
            SELECT COALESCE(SUM(n - 1), 0) FROM (
              SELECT COUNT(*) n FROM meeting_event_extractions
              WHERE extractor='pattern'
              GROUP BY supporting_doc_id, text_offset_start, text_offset_end,
                       action_verb, extractor_version
              HAVING COUNT(*) > 1) d""",
        "orphan_mentions": """SELECT COUNT(*) FROM entity_mentions m
            LEFT JOIN entities e ON e.id=m.entity_id WHERE e.id IS NULL""",
        "orphan_relationships": """SELECT COUNT(*) FROM entity_relationships r
            LEFT JOIN entities f ON f.id=r.from_entity_id
            LEFT JOIN entities t ON t.id=r.to_entity_id
            WHERE f.id IS NULL OR t.id IS NULL""",
        "orphan_extractions": """SELECT COUNT(*) FROM meeting_event_extractions x
            WHERE x.meeting_event_id IS NOT NULL AND NOT EXISTS
              (SELECT 1 FROM meeting_events e WHERE e.id=x.meeting_event_id)""",
        "orphan_participants": """SELECT COUNT(*) FROM event_participants p
            WHERE NOT EXISTS (SELECT 1 FROM meeting_events e WHERE e.id=p.meeting_event_id)
               OR NOT EXISTS (SELECT 1 FROM entities n WHERE n.id=p.entity_id)""",
        "unresolved_relationship_provenance": """
            SELECT COUNT(*) FROM entity_relationships r
            WHERE CASE r.provenance_type
              WHEN 'agenda_item' THEN NOT EXISTS
                (SELECT 1 FROM agenda_items s WHERE s.id=r.provenance_id)
              WHEN 'entity_mention' THEN NOT EXISTS
                (SELECT 1 FROM entity_mentions s WHERE s.id=r.provenance_id)
              WHEN 'public_bodies' THEN NOT EXISTS
                (SELECT 1 FROM public_bodies s WHERE s.id=r.provenance_id)
              WHEN 'meetings' THEN NOT EXISTS
                (SELECT 1 FROM meetings s WHERE s.id=r.provenance_id)
              WHEN 'entity_resolution' THEN NOT EXISTS
                (SELECT 1 FROM entities s WHERE s.id=r.provenance_id)
              WHEN 'body_membership' THEN NOT EXISTS
                (SELECT 1 FROM body_memberships s WHERE s.id=r.provenance_id)
              WHEN 'meeting_member' THEN NOT EXISTS
                (SELECT 1 FROM meeting_members s WHERE s.id=r.provenance_id)
              WHEN 'pz_item_detail' THEN NOT EXISTS
                (SELECT 1 FROM pz_item_details s WHERE s.id=r.provenance_id)
              ELSE FALSE
            END""",
        "unknown_relationship_provenance_type": """
            SELECT COUNT(*) FROM entity_relationships
            WHERE provenance_type NOT IN
              ('agenda_item', 'entity_mention', 'public_bodies', 'meetings',
               'entity_resolution', 'body_membership', 'meeting_member',
               'pz_item_detail')""",
    }


def integrity_snapshot(connection) -> dict[str, int]:
    """Stage 0 invariants evaluated on a caller-supplied connection.

    Taking a connection rather than an engine lets a caller evaluate the invariants
    inside the same transaction that performed a mutation, so uncommitted changes
    are visible to the check.
    """
    return {name: int(connection.execute(text(sql)).scalar() or 0)
            for name, sql in INTEGRITY_QUERIES.items()}


def _integrity_snapshot(engine) -> dict[str, int]:
    """Conservative Stage 0 invariants; measures replay debt, never deletes it."""
    with engine.connect() as c:
        return integrity_snapshot(c)


def _schema_contract_violations(engine) -> list[str]:
    from scripts.entities.schema_parity import contract_violations, schema_signature
    return contract_violations(schema_signature(engine))


def _write_run_state(run_state: dict) -> None:
    """Persist per-phase run state + gate results as JSON (durable state).

    Written next to the daily entity log in data/sync/ — the same directory
    the scrape error report and digest already read, so per-phase failures
    surface without a new delivery mechanism.
    """
    try:
        run_dir = os.path.join(_REPO_ROOT, "data", "sync")
        os.makedirs(run_dir, exist_ok=True)
        def write_atomic(path: str) -> None:
            temporary = f"{path}.{run_state['run_id'][:8]}.tmp"
            with open(temporary, "w") as f:
                json.dump(run_state, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)

        path = os.path.join(run_dir, run_state["state_file"])
        write_atomic(path)
        log.info("Run state written → %s", path)
        if not run_state.get("dry_run"):
            latest_path = os.path.join(run_dir, run_state["latest_file"])
            write_atomic(latest_path)
            log.info("Latest live run state updated → %s", latest_path)
    except Exception as e:
        log.error("  Could not write run state: %s", e)


def _run_phase(phase: dict, engine, args) -> dict:
    """Execute one phase as a direct library call, with retry + backoff.

    Returns stats dict including attempts. A phase is retried only when it
    raises or returns success=False; watermark is marked only on final success
    (caller), so a failed phase re-runs next sync rather than being skipped.
    """
    run_fn = _resolve_phase_fn(phase)
    if run_fn is None:
        return {"skipped": True}

    attempts = 0
    last_error = None
    while attempts <= PHASE_RETRIES:
        attempts += 1
        log.info("  → %s.%s() (attempt %d/%d)", phase["module"], phase["run_fn_name"],
                 attempts, PHASE_RETRIES + 1)
        start = time.time()
        try:
            result = run_fn(
                engine,
                dry_run=args.dry_run,
                force=args.force,
                verbose=args.verbose,
            )
        except Exception as e:
            elapsed = time.time() - start
            last_error = str(e)
            kind = failure_classification.classify_failure(e)
            log.error("  ✗ %s (%.1fs, attempt %d): %s — %s",
                      phase["description"], elapsed, attempts, e,
                      failure_classification.describe(kind))
            if (failure_classification.is_retryable(kind)
                    and attempts <= PHASE_RETRIES):
                time.sleep(PHASE_RETRY_BACKOFF_S * attempts)
                continue
            return {
                "success": False,
                "duration_s": round(elapsed, 1),
                "entities_created": 0,
                "edges_created": 0,
                "attempts": attempts,
                "error": last_error,
                "failure_kind": kind,
            }

        elapsed = time.time() - start
        success = result.get("success", True)
        status = "✓" if success else "✗"
        entities_created = result.get("entities_created", 0)
        edges_created = result.get("edges_created", 0)
        log.info("  %s %s (%.1fs, attempt %d)", status, phase["description"],
                 elapsed, attempts)

        if not success:
            last_error = result.get("error") or "phase returned success=False"
            kind = failure_classification.classify_result_payload(result)
            log.error("  Phase failed — %s (%s)",
                      failure_classification.describe(kind), last_error)

        return {
            "success": success,
            "duration_s": round(elapsed, 1),
            "entities_created": entities_created,
            "edges_created": edges_created,
            "attempts": attempts,
            "error": None if success else last_error,
            "failure_kind": None if success else failure_classification.DETERMINISTIC,
            "raw_result": result,
        }

    # All attempts exhausted (exception path)
    return {
        "success": False,
        "duration_s": 0.0,
        "entities_created": 0,
        "edges_created": 0,
        "attempts": attempts,
        "error": last_error,
    }


def run_detection(engine, phases: list[str] | None = None,
                  dry_run: bool = False, force: bool = False,
                  verbose: bool = False,
                  expect_no_writes: bool = False) -> dict:
    """Run the entity detection pipeline. Returns summary dict.

    Step 2 (Brief 016): collects per-phase state (incl. attempts/errors),
    snapshots pre/post row counts, and runs a verification gate at the end.
    """
    run_started_at = datetime.now(PHOENIX_TZ)
    run_id = uuid.uuid4().hex
    completed = set()
    if not force:
        completed = _get_watermarks(engine)

    # Pre-run snapshot for the verification gate (skip in dry-run: no writes)
    pre_counts = {}
    pre_integrity = {}
    if not dry_run:
        pre_counts = {t: _count_table(engine, t) for t in GATE_COUNT_TABLES}
        pre_integrity = _integrity_snapshot(engine)

    results = {
        "run_id": run_id,
        "phases": [],
        "phase_checks": [],
        "total_duration_s": 0,
        "total_entities": 0,
        "total_edges": 0,
        "errors": [],
    }

    for phase in PHASES:
        if phases and phase["name"] not in phases:
            continue

        if phase["name"] in completed and phase.get("allow_skip", True):
            log.info("  [SKIP] %s — already completed", phase["description"])
            results["phases"].append({
                "name": phase["name"],
                "producer": _producer_metadata(phase),
                "status": "skipped",
            })
            continue

        phase_pre_counts = ({t: _count_table(engine, t) for t in GATE_COUNT_TABLES}
                            if not dry_run else {})
        phase_result = _run_phase(phase, engine, argparse.Namespace(
            dry_run=dry_run, force=force, verbose=verbose,
        ))

        if phase_result.get("skipped"):
            results["phases"].append({
                "name": phase["name"],
                "producer": _producer_metadata(phase),
                "status": "skipped",
                "error": phase_result.get("error"),
            })
            continue

        entry = {
            "name": phase["name"],
            "producer": _producer_metadata(phase),
            "status": "ok" if phase_result["success"] else "failed",
            "duration_s": phase_result["duration_s"],
            "entities_created": phase_result["entities_created"],
            "edges_created": phase_result["edges_created"],
        }
        raw_result = phase_result.get("raw_result") or {}
        if raw_result:
            entry["reported"] = raw_result

        # Orchestration-level receipt enforcement (Brief 018 Step 4).  Evaluated
        # for every phase that ran, dry or live, so it cannot be bypassed.
        enforcement = orchestration_receipts.enforce_phase_receipt(
            phase["name"], raw_result=raw_result, dry_run=dry_run)
        entry["receipt_ok"] = enforcement["ok"]
        entry["receipt_checks"] = enforcement["checks"]
        if enforcement["reasons"]:
            entry["receipt_reasons"] = enforcement["reasons"]
        if not enforcement["ok"]:
            for reason in enforcement["reasons"]:
                log.error("  [GATE] ✗ receipt %s: %s", phase["name"], reason)
        if not dry_run:
            phase_post_counts = {t: _count_table(engine, t) for t in GATE_COUNT_TABLES}
            phase_deltas = {
                t: phase_post_counts[t] - phase_pre_counts[t]
                for t in GATE_COUNT_TABLES
            }
            entry["committed_deltas"] = phase_deltas
            if phase_result["success"]:
                results["phase_checks"].extend(
                    _phase_accounting_checks(phase["name"], raw_result, phase_deltas)
                )
                results["phase_checks"].append(
                    _phase_expected_output_check(
                        phase["name"], raw_result, phase_deltas, force
                    )
                )
                if expect_no_writes:
                    results["phase_checks"].extend(
                        _replay_zero_report_checks(phase["name"], raw_result)
                    )
                    results["phase_checks"].extend(
                        _replay_zero_delta_checks(phase["name"], phase_deltas)
                    )
        if phase_result.get("attempts"):
            entry["attempts"] = phase_result["attempts"]
        if phase_result.get("error"):
            entry["error"] = phase_result["error"]
        results["phases"].append(entry)
        results["total_duration_s"] += phase_result["duration_s"]
        results["total_entities"] += phase_result["entities_created"]
        results["total_edges"] += phase_result["edges_created"]

        # Central fail-closed rule.  A deterministic failure — which includes a
        # refused orchestration receipt — aborts the remaining phases instead of
        # letting later phases run on top of a broken contract.  This is one
        # policy, deliberately not six per-phase "critical" flags that could
        # drift apart.
        kind = phase_result.get("failure_kind")
        refusal = not enforcement["ok"]
        if refusal or (not phase_result["success"]
                       and kind == failure_classification.DETERMINISTIC):
            reason = ("receipt refused" if refusal
                      else failure_classification.describe(kind or ""))
            log.error("  Aborting — %s failed: %s", phase["name"], reason)
            results["errors"].append({
                "phase": phase["name"],
                "error": f"deterministic failure: {reason}",
                "stderr": phase_result.get("stderr", ""),
            })
            break

        if not phase_result["success"] and phase.get("critical"):
            log.error("  Aborting — critical phase %s failed", phase["name"])
            results["errors"].append({
                "phase": phase["name"],
                "error": "critical phase failed",
                "stderr": phase_result.get("stderr", ""),
            })
            break

        # Record watermark (only on success, never in dry-run)
        if phase_result["success"] and not dry_run:
            _mark_watermark(engine, phase["name"],
                            phase_result["duration_s"],
                            phase_result["entities_created"],
                            phase_result["edges_created"])

    # ── Verification gate (Step 2, Brief 016) ────────────────────────────
    gate = {"checks": [], "failed": False}
    if not dry_run:
        post_counts = {t: _count_table(engine, t) for t in GATE_COUNT_TABLES}
        gate["deltas"] = {
            t: post_counts[t] - pre_counts.get(t, 0) for t in GATE_COUNT_TABLES
        }
        for t in GATE_COUNT_TABLES:
            d = gate["deltas"][t]
            if expect_no_writes and d != 0:
                gate["failed"] = True
                msg = f"{t} delta {d:+d}; replay mode requires zero"
                gate["checks"].append({"check": t, "ok": False, "detail": msg})
                log.error("  [GATE] ✗ %s", msg)
            elif d < 0:
                gate["failed"] = True
                msg = f"{t} count DECREASED by {-d} during the run"
                gate["checks"].append({"check": t, "ok": False, "detail": msg})
                log.error("  [GATE] ✗ %s", msg)
            else:
                gate["checks"].append({"check": t, "ok": True,
                                       "detail": f"delta {d:+d}"})
                log.info("  [GATE] ✓ %s delta %+d", t, d)

        for check in results["phase_checks"]:
            gate["checks"].append(check)
            if check["ok"]:
                log.info("  [GATE] ✓ %s: %s", check["check"], check["detail"])
            else:
                gate["failed"] = True
                log.error("  [GATE] ✗ %s: %s", check["check"], check["detail"])

        # Unmapped entity_type audit (Brief 015 taxonomy)
        unmapped = _unmapped_entity_types(engine)
        if unmapped:
            gate["failed"] = True
            gate["checks"].append({
                "check": "entity_type_taxonomy", "ok": False,
                "detail": f"unmapped entity types: {unmapped}",
            })
            log.error("  [GATE] ✗ unmapped entity_type values: %s", unmapped)
        else:
            gate["checks"].append({"check": "entity_type_taxonomy", "ok": True,
                                   "detail": "zero unmapped"})
            log.info("  [GATE] ✓ entity_type taxonomy: zero unmapped")

        post_integrity = _integrity_snapshot(engine)
        gate["integrity"] = post_integrity
        for name, value in post_integrity.items():
            before = pre_integrity.get(name, 0)
            requires_zero = (
                name.startswith("orphan_")
                or name in ABSOLUTE_ZERO_INTEGRITY_CHECKS
            )
            ok = value == 0 if requires_zero else value <= before
            requirement = "requires zero; " if requires_zero else ""
            detail = (
                f"{value} ({requirement}before {before}, "
                f"delta {value-before:+d})"
            )
            gate["checks"].append({"check": name, "ok": ok, "detail": detail})
            if not ok:
                gate["failed"] = True
                log.error("  [GATE] ✗ %s: %s", name, detail)
            else:
                log.info("  [GATE] ✓ %s: %s", name, detail)

        schema_problems = _schema_contract_violations(engine)
        schema_ok = not schema_problems
        gate["checks"].append({"check": "graph_schema_contract", "ok": schema_ok,
                               "detail": "ok" if schema_ok else "; ".join(schema_problems)})
        if not schema_ok:
            gate["failed"] = True
            log.error("  [GATE] ✗ graph schema contract: %s", schema_problems)
        else:
            log.info("  [GATE] ✓ graph schema contract")

    # ── Ontology-emission receipt enforcement (Brief 018 Step 4) ────────
    # Applied to every run, dry or live, and deliberately OUTSIDE the
    # `if not dry_run` Stage 0 block: a phase that cannot produce exactly one
    # trustworthy receipt fails the orchestration gate.  There is no flag that
    # turns this off, so a caller cannot bypass it.
    enforcement = orchestration_receipts.enforce_run_receipts(
        results["phases"], dry_run=dry_run,
        expected_phases=[phase["name"] for phase in PHASES
                         if not phases or phase["name"] in phases])
    results["receipt_enforcement"] = enforcement
    for check in enforcement["checks"]:
        gate["checks"].append(check)
        if check["ok"]:
            log.info("  [GATE] ✓ receipt %s: %s", check["check"], check["detail"])
        else:
            gate["failed"] = True
            log.error("  [GATE] ✗ receipt %s: %s", check["check"], check["detail"])
    for reason in enforcement["reasons"]:
        log.error("  receipt enforcement: %s", reason)
    if not enforcement["ok"]:
        results["errors"].append({"error": "receipt enforcement failed",
                                  "reasons": enforcement["reasons"]})
    log.info("  [GATE] receipt enforcement: %s (%d phase(s) required)",
             "PASS" if enforcement["ok"] else "FAIL",
             len(enforcement["required_phases"]))

    results["gate"] = gate

    # Durable per-phase state + gate (JSON next to the daily entity log)
    state_file = (f"entity-run-{run_started_at:%Y-%m-%d-%H%M%S}-"
                  f"{run_id[:8]}.json")
    latest_file = f"entity-run-{run_started_at:%Y-%m-%d}.json"
    results["state_file"] = state_file
    run_state = {
        "run_id": run_id,
        "state_file": state_file,
        "latest_file": latest_file,
        "started_at": run_started_at.isoformat(),
        "finished_at": datetime.now(PHOENIX_TZ).isoformat(),
        "dry_run": dry_run,
        "force": force,
        "expect_no_writes": expect_no_writes,
        "phases": results["phases"],
        "receipt_enforcement": results.get("receipt_enforcement"),
        "gate": gate,
        "summary": {
            "phases_ran": len([p for p in results["phases"]
                               if p["status"] not in ("skipped",)]),
            "phases_failed": len([p for p in results["phases"]
                                  if p["status"] == "failed"]),
            "total_entities": results["total_entities"],
            "total_edges": results["total_edges"],
            "total_duration_s": round(results["total_duration_s"], 1),
        },
    }
    _write_run_state(run_state)

    if gate["failed"]:
        results["errors"].append({"error": "verification gate failed"})

    return results


def main():
    parser = argparse.ArgumentParser(description="Unified entity detection pipeline")
    parser.add_argument("--phase", type=str, help="Run only one phase (name)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--force", action="store_true", help="Force re-run all phases")
    parser.add_argument(
        "--expect-no-writes",
        action="store_true",
        help="fail unless every phase reports, plans, and commits zero writes",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()
    if args.expect_no_writes and args.dry_run:
        parser.error("--expect-no-writes requires a live verification run")
    if args.expect_no_writes and not args.force:
        parser.error("--expect-no-writes requires --force")

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    phases = [args.phase] if args.phase else None
    engine = get_engine()
    results = run_detection(engine, phases=phases,
                            dry_run=args.dry_run, force=args.force,
                            verbose=args.verbose,
                            expect_no_writes=args.expect_no_writes)

    # Summary
    dry = " (DRY RUN)" if args.dry_run else ""
    ran = [p for p in results["phases"] if p["status"] not in ("skipped",)]
    skipped = [p for p in results["phases"] if p["status"] == "skipped"]
    failed = [p for p in results["phases"] if p["status"] == "failed"]

    parts = [f"{len(ran)} phase(s) ran"]
    if skipped:
        parts.append(f"{len(skipped)} skipped")
    if results["total_entities"]:
        parts.append(f"{results['total_entities']} entities")
    if results["total_edges"]:
        parts.append(f"{results['total_edges']} edges")
    parts.append(f"{results['total_duration_s']:.0f}s")

    log.info("DONE%s — %s", dry, " | ".join(parts))

    # Per-phase alerting: log failed phases loudly AND append to the run-state
    # errors list so the daily error report / digest can surface them. Exit
    # non-zero on phase failure or a failed verification gate (Step 2, Brief 016).
    gate_failed = bool(results.get("gate", {}).get("failed"))
    if failed:
        log.error("Failed phases: %s", ", ".join(p["name"] for p in failed))
    if gate_failed:
        log.error("Verification gate FAILED — see state file for check details")
    if failed or gate_failed:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
