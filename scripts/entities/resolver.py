#!/usr/bin/env python3
"""
Entity resolution — deduplicate and merge entity records.

Runs as Phase 4 of the entity detection pipeline. Three sub-phases:

1. TYPE_CONFLICT — Merge same-normalized-name duplicates across types.
   When "Brandon McNeil" exists as both person(id=463) and organization(id=18399),
   the person wins and the org re-points to it.

2. COMPOSITE_SPLIT — Detect "Person, Firm" entity names from pattern_cascade
   overmatching. Split into separate person + firm entities and create a
   HAS_ATTORNEY / HAS_APPLICANT relationship.

3. NAME_VARIATION — Block candidates by Soundex + token similarity and merge
   probable duplicates like "Hitt-Zollars" ↔ "Huitt-Zollars".

Each sub-phase is idempotent with its own watermark.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

from sqlalchemy import text

# CWD-independent path bootstrap (see detect_entities.py).
_ENTITIES_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_ENTITIES_DIR)
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in (_REPO_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db.core import get_engine

from scripts.entities.resolver_accounting import (
    SubphaseProposals,
    aggregate_proposals,
    is_canonical_emission,
    subphase_result,
    seal_resolver_receipt,
)
from scripts.entities.resolver_persistence import (
    apply_composite_splits,
    merge_entities,
)
from scripts.entities.resolver_proposals import (
    build_composite_ops,
    build_name_variation_candidates,
    type_priority,
)
from scripts.entities.resolver_similarity import (
    acronym_match as _acronym_match,
)
from scripts.entities.resolver_similarity import (
    make_block_key as _make_block_key,
)
from scripts.entities.resolver_similarity import (
    substring_match as _substring_match,
)
from scripts.entities.resolver_similarity import (
    token_normalize as _token_normalize,
)
from scripts.entities.resolver_similarity import (
    token_set_similarity as _token_set_similarity,
)
from scripts.entities.resolver_similarity import (
    token_sort_similarity as _token_sort_similarity,
)

log = logging.getLogger("resolver")
WATERMARK_TABLE = "_resolver_watermark"
BATCH_SIZE = 100

# ── Phase 1: Type Conflict Resolution ──────────────────────────────────────

PHASE1_SAME_NAME_DUPES = """
    SELECT e1.id AS id1, e1.name AS name1, e1.entity_type AS type1,
           e1.mention_count AS cnt1,
           e2.id AS id2, e2.name AS name2, e2.entity_type AS type2,
           e2.mention_count AS cnt2
    FROM entities e1
    JOIN entities e2 ON e1.normalized_name = e2.normalized_name
                     AND e1.id < e2.id
    WHERE e1.resolution_status = 'unresolved'
      AND e2.resolution_status = 'unresolved'
      AND e1.entity_type != e2.entity_type
      AND e1.canonical_entity_id IS NULL
      AND e2.canonical_entity_id IS NULL
"""

#: The ontology values a composite split may emit.  The organisation carries
#: the weakest truthful role ("mentioned") and no relationship is emitted.
SPLIT_EMITTED_VALUES = (
    ("entity_type", "person"),
    ("entity_type", "organization"),
    ("role", "mentioned"),
)


def _resolve_type_conflicts(conn, dry_run: bool = False, verbose: bool = False,
             validator=None) -> dict:
    """Merge same-normalized-name entities where one has the wrong type."""
    rows = conn.execute(text(PHASE1_SAME_NAME_DUPES)).fetchall()
    if verbose:
        log.info("  Phase 1: %d type-conflict pairs found", len(rows))

    merged = 0
    committed = 0
    for r in rows:
        id1, type1, cnt1 = r[0], r[2], r[3]
        id2, type2, cnt2 = r[4], r[6], r[7]
        # Determine survivor by priority, but allow mention-count override.
        # If the higher-priority entity has <= 2 mentions and the lower-priority
        # has significantly more, the lower-priority (more specific) entity wins.
        # This prevents junk "person" entries from absorbing real organization records.
        p1 = type_priority(type1)
        p2 = type_priority(type2)

        if p1 < p2:
            # type1 is higher priority (lower number)
            if cnt1 <= 2 and cnt2 > cnt1 * 3:
                survivor, victim = id2, id1
            else:
                survivor, victim = id1, id2
        elif p2 < p1:
            # type2 is higher priority
            if cnt2 <= 2 and cnt1 > cnt2 * 3:
                survivor, victim = id1, id2
            else:
                survivor, victim = id2, id1
        else:
            # Same priority — keep the one with more mentions
            if cnt1 >= cnt2:
                survivor, victim = id1, id2
            else:
                survivor, victim = id2, id1

        # Classified exactly once.  A conflicting pair is a real merge, so it is
        # a would-update in dry mode too; dry mode simply writes nothing.
        merged += 1
        if dry_run:
            # The pair is (id1, type1) vs (id2, type2); report whichever type
            # belongs to the victim and the survivor actually chosen.
            victim_type = type2 if victim == id2 else type1
            survivor_type = type1 if survivor == id1 else type2
            log.info("    Would merge %d(%s) → %d(%s)",
                     victim, victim_type, survivor, survivor_type)
            continue

        merge_entities(conn, victim, survivor, "type_conflict", 0.99)
        committed += 1

    return subphase_result(
        "type_conflict",
        SubphaseProposals(
            subphase="type_conflict",
            would_update=merged,
            committed=0 if dry_run else committed,
            merged_entities=0 if dry_run else committed,
        ),
        phase1_type_conflicts=merged,
    )


# ── Phase 2: Composite Entity Split ────────────────────────────────────────

def _resolve_composites(conn, dry_run: bool = False, verbose: bool = False,
             validator=None) -> dict:
    """Find and split composite 'Person, Firm' entities."""
    # Load entity cache once
    cache_rows = conn.execute(
        text("SELECT normalized_name, entity_type, id FROM entities")
    ).fetchall()
    entity_cache = {(str(r[0]), str(r[1])): int(r[2]) for r in cache_rows}
    if verbose:
        log.info("  Phase 2: loaded %d entities into cache", len(entity_cache))

    # Phase A: identify every composite split proposal.  Proposal building
    # is identical in dry and live runs (see resolver_proposals), so a dry
    # run describes exactly what a live run would write.
    ops = build_composite_ops(conn)
    if verbose:
        log.info("  Phase 2: %d composite split proposal(s)", len(ops))

    # Validate the ontology values this split may emit BEFORE any write.  A
    # comma proves only that a person and an organisation were named in one
    # occurrence: it is not affiliation evidence, so no relationship is
    # emitted and no predicate is inferred.  Non-canonical values are refused
    # rather than written.
    refused = tuple(
        f"{category}:{value}"
        for category, value in SPLIT_EMITTED_VALUES
        if not is_canonical_emission(category, value)
    )

    if refused:
        log.warning(
            "  Phase 2: refusing %d composite split(s) — non-canonical emission: %s",
            len(ops), ", ".join(refused),
        )
        return subphase_result(
            "composite_split",
            SubphaseProposals(
                subphase="composite_split",
                unresolved=len(ops),
                refused_values=refused,
            ),
            phase2_composites=len(ops),
        )

    from scripts.kg import registries as _registries

    outcome = apply_composite_splits(
        conn, ops, entity_cache, dry_run=dry_run, validator=validator,
        model_version=_registries.MODEL_VERSION,
    )
    mutations = outcome["would_insert"] + outcome["would_update"]
    return subphase_result(
        "composite_split",
        SubphaseProposals(
            subphase="composite_split",
            would_insert=outcome["would_insert"],
            would_update=outcome["would_update"],
            unresolved=outcome["unresolved"],
            committed=0 if dry_run else mutations,
            created_entities=0 if dry_run else outcome["created_entities"],
            merged_entities=0 if dry_run else outcome["merged_entities"],
            emitted_values=SPLIT_EMITTED_VALUES,
        ),
        phase2_composites=len(ops),
    )



# ── Phase 3: Name Variation Matching ───────────────────────────────────────

def _resolve_name_variations(conn, dry_run: bool = False, verbose: bool = False,
             validator=None) -> dict:
    """Block and merge similar organization names."""
    entities = build_name_variation_candidates(conn)
    if verbose:
        log.info("  Phase 3: %d organization entities to scan", len(entities))

    merged = 0
    committed = 0
    compared = 0

    for i in range(len(entities)):
        e1 = entities[i]
        if e1.get("_dead"):
            continue
        for j in range(i + 1, len(entities)):
            e2 = entities[j]
            if e2.get("_dead"):
                continue

            # Skip same-type same-name (already caught by Phase 1)
            if e1["norm"] == e2["norm"] and e1["type"] == e2["type"]:
                continue

            # Only compare same-resolution-block candidates
            block_key = _make_block_key(e1["norm"], e2["norm"])
            if not block_key:
                continue

            compared += 1

            # Compute similarity scores
            token_sim = _token_set_similarity(e1["name"], e2["name"])
            sort_sim = _token_sort_similarity(e1["norm"], e2["norm"])
            sub_sim = _substring_match(e1["norm"], e2["norm"])
            acr_sim = _acronym_match(e1["name"], e2["name"])

            # Combined score — weighted
            score = max(token_sim, sort_sim, sub_sim, acr_sim)

            if score >= 0.85:
                if dry_run:
                    log.info("    MATCH (%.2f): '%s'(%d, %s) ↔ '%s'(%d, %s)",
                             score, e1["name"], e1["id"], e1["type"],
                             e2["name"], e2["id"], e2["type"])
                    merged += 1
                    e2["_dead"] = True
                    continue

                # Merge lower-mention into higher-mention
                cnt1 = conn.execute(
                    text("SELECT mention_count FROM entities WHERE id = :id"),
                    {"id": e1["id"]},
                ).scalar() or 0
                cnt2 = conn.execute(
                    text("SELECT mention_count FROM entities WHERE id = :id"),
                    {"id": e2["id"]},
                ).scalar() or 0

                if cnt1 >= cnt2:
                    survivor, victim = e1["id"], e2["id"]
                    survivor_name, victim_name = e1["name"], e2["name"]
                else:
                    survivor, victim = e2["id"], e1["id"]
                    survivor_name, victim_name = e2["name"], e1["name"]

                if verbose:
                    log.info("    MERGE (%.2f): '%s'(%d) ← '%s'(%d)",
                             score, survivor_name, survivor, victim_name, victim)

                merge_entities(conn, victim, survivor, "name_variation", score)
                merged += 1
                committed += 1
                e2["_dead"] = True

    # A name-variation merge re-points existing rows and marks the victim; it
    # introduces no new ontology values, so there is nothing to validate here.
    return subphase_result(
        "name_variation",
        SubphaseProposals(
            subphase="name_variation",
            would_update=merged,
            committed=0 if dry_run else committed,
            merged_entities=0 if dry_run else committed,
            compared=compared,
        ),
        phase3_name_variations=merged,
        compared=compared,
    )


# ── Orchestrator ───────────────────────────────────────────────────────────

PHASES = {
    "type_conflict": (_resolve_type_conflicts, "Type conflict resolution"),
    "composite_split": (_resolve_composites, "Composite entity split"),
    "name_variation": (_resolve_name_variations, "Name variation matching"),
}

PHASE_ORDER = ["type_conflict", "composite_split", "name_variation"]


def run_resolver(engine, phases: list[str] | None = None,
                 dry_run: bool = False, force: bool = False,
                 verbose: bool = False) -> dict:
    """Run entity resolution. Returns summary dict."""
    results = {"errors": []}

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                phase VARCHAR(32) PRIMARY KEY,
                last_run_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))

    watermarks = {}
    if not force:
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT phase, last_run_at FROM {WATERMARK_TABLE}")
            ).fetchall()
            watermarks = {r[0]: r[1] for r in rows}

    # Check if there are any unresolved entities to process
    with engine.connect() as conn:
        pending = conn.execute(text("""
            SELECT COUNT(*) FROM entities
            WHERE resolution_status IS NULL OR resolution_status = 'unresolved'
        """)).scalar()

    from scripts.kg.emission import EmissionValidator
    from scripts.kg.producer_versions import declared_producer_version

    # One validator for the whole phase: every emitted bundle is validated
    # through this single canonical boundary, and it is sealed into exactly
    # one resolver receipt at the end.
    validator = EmissionValidator(
        "resolver", declared_producer_version("resolver") or "unknown",
        dry_run=dry_run,
    )
    validator.start_batch()

    target_phases = phases or PHASE_ORDER
    subphase_proposals: list[SubphaseProposals] = []
    for phase_name in target_phases:
        if phase_name not in PHASES:
            log.warning("  Unknown phase: %s", phase_name)
            continue

        if phase_name in watermarks and pending == 0:
            log.info("  [SKIP] %s — no pending entities", PHASES[phase_name][1])
            continue
        elif phase_name in watermarks:
            log.info("  %s — watermark exists but %d entities pending",
                     PHASES[phase_name][1], pending)

        log.info("  %s", PHASES[phase_name][1])
        try:
            with engine.begin() as conn:
                phase_fn = PHASES[phase_name][0]
                phase_results = phase_fn(conn, dry_run=dry_run,
                                         verbose=verbose, validator=validator)
                proposals = phase_results.pop("_proposals", None)
                if proposals is not None:
                    subphase_proposals.append(proposals)
                if not dry_run:
                    conn.execute(
                        text(f"""
                            INSERT INTO {WATERMARK_TABLE} (phase, last_run_at)
                            VALUES (:pn, now())
                            ON CONFLICT (phase) DO UPDATE SET last_run_at = now()
                        """),
                        {"pn": phase_name},
                    )
                results.update(phase_results)
        except Exception as e:
            log.error("  ✗ Phase %s failed: %s", phase_name, e, exc_info=verbose)
            results["errors"].append({"phase": phase_name, "error": str(e)})
            if not force:
                raise

    errors = results.get("errors") or []
    if errors:
        validator.fail("; ".join(
            f"{err['phase']}: {err['error']}" for err in errors))
        results["validation_receipt"] = validator.seal().serialize()
    else:
        results["validation_receipt"] = seal_resolver_receipt(
            validator, subphase_proposals, dry_run=dry_run)
    # Per-subphase truth is preserved rather than collapsed into the total.
    results["subphase_accounting"] = [
        {
            "subphase": item.subphase,
            "proposed": item.proposed,
            "would_insert": item.would_insert,
            "would_update": item.would_update,
            "replay_noop": item.replay_noop,
            "unresolved": item.unresolved,
            "committed": item.committed,
            "created_entities": item.created_entities,
            "merged_entities": item.merged_entities,
            "compared": item.compared,
            "refused_values": list(item.refused_values),
            "emitted_values": [list(v) for v in item.emitted_values],
        }
        for item in subphase_proposals
    ]
    return results


def main():
    parser = argparse.ArgumentParser(description="Entity resolution pipeline")
    parser.add_argument("--phase", type=str, help="Run only one phase")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--force", action="store_true", help="Force re-run")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    engine = get_engine()

    phases = [args.phase] if args.phase else None
    results = run_resolver(engine, phases=phases,
                           dry_run=args.dry_run, force=args.force,
                           verbose=args.verbose)

    # Summary
    dry = " (DRY RUN)" if args.dry_run else ""
    parts = []
    for k, v in results.items():
        if k == "errors":
            continue
        if isinstance(v, int) and v > 0:
            parts.append(f"{k}={v}")
    log.info("DONE%s — %s", dry, " | ".join(parts))

    has_errors = bool(results.get("errors"))
    if has_errors:
        log.error("Errors: %d", len(results["errors"]))

    print(json.dumps({
        "phase": "resolver",
        "success": not has_errors,
        **{k: v for k, v in results.items() if isinstance(v, (int, float)) and k != "errors"},
    }))

    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
