#!/usr/bin/env python3
"""
Pattern cascade — Phase 2 of the information extraction pipeline.

EXTRACT role-labeled actors from agenda item headers using per-body patterns.
STRATEGY: Only match items where the role label appears as a LINE-LEVEL header
field (e.g. "Applicant: John Smith" on its own line). Inline mentions
("the applicant is...") are left for the ML role classifier.

VALIDATION: Any captured name > 100 characters is discarded.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

from sqlalchemy import text

# CWD-independent path bootstrap (see detect_entities.py).
_ENTITIES_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_ENTITIES_DIR)
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in (_REPO_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db.core import get_engine
from entities.entity_utils import clean_normalized_name, normalize_entity_name

log = logging.getLogger("pattern_cascade")

from .pattern_cascade_patterns import (
    BODY_PATTERNS,
    EVIDENCE_ONLY_PATTERNS,
    ROLE_EDGE_MAP,
)

WATERMARK_TABLE = "_pattern_cascade_watermark"
MAX_MATCH_LEN = 100
BATCH_SIZE = 30

# ── Body resolution ────────────────────────────────────────────────────

def find_body_group(body_code: str) -> str:
    """Map a body code to its pattern group."""
    if body_code in BODY_PATTERNS:
        return body_code
    # Fallback: check for phoenix-* prefix
    for pattern_body in BODY_PATTERNS:
        if pattern_body == "pz" and body_code.endswith("-pz"):
            return "pz"
        if body_code.startswith(pattern_body.split("-")[0]):
            return pattern_body
    return None  # No patterns for this body


# ── Helpers ────────────────────────────────────────────────────────────

def normalize_name(name: str, entity_type: str | None = None) -> str:
    """Normalize an entity name for dedup.
    For person entities, strips titles for cleaner merging.
    """
    if entity_type == 'person':
        return clean_normalized_name(name)
    name = re.sub(r"\s+", " ", name.strip())
    return name.lower()


def is_probable_person(name: str) -> bool:
    n = name.lower()
    firm_kw = ["llc", "inc", "plc", "ltd", "group", "firm", "corporation",
               "company", "partnership", "consulting", "planning", "engineering",
               "law office", "pa", "pc", "llp", "association", "incorporated",
               "hospital", "university", "district", "city of", "town of",
               "county of", "department", "committee", "commission", "board of",
               "design", "architect", "construction", "development", "properties",
               "management", "services", "church", "assembly of god", "llp"]
    has_firm = any(kw in n for kw in firm_kw)
    has_comma = "," in name
    has_space = " " in name
    is_short = len(name.split()) <= 1
    return has_space and not has_firm and not has_comma and not is_short


def classify_entity_type(name: str) -> str:
    return "person" if is_probable_person(name) else "organization"


def validate_name(name: str) -> bool:
    """Reject matched names that are too long or contain junk."""
    if len(name) > MAX_MATCH_LEN:
        return False
    if len(name) < 2:
        return False
    # Reject if it starts with filler words that indicate bad match
    filler_starts = ["the ", "and ", "to ", "for ", "of ", "a ",
                     "an ", "in ", "on ", "is ", "it ", "be "]
    if any(name.lower().startswith(f) for f in filler_starts):
        # Allow "The Church of..." or "The City of..." but not "the applicant..."
        if not any(kw in name.lower() for kw in ["church", "city of", "town of", "county of", "state of"]):
            return False
    return True


# ── Batch Queries ──────────────────────────────────────────────────────

QUERY = """
    SELECT ai.id, ai.body, ai.meeting_db_id, ai.agenda_item_id,
           ai.agenda_item_number, ai.agenda_item_text,
           ai.agenda_item_title, ai.case_number, ai.c_number
    FROM agenda_items ai
    WHERE ai.id > :wm AND ai.body = :body
      AND ai.agenda_item_text IS NOT NULL
      AND length(ai.agenda_item_text) > 0
    ORDER BY ai.id
    LIMIT 10000
"""


def _bulk_insert(conn, table: str, columns: list[str], vals: list[dict],
                 entity_cache: dict, returning: str = "") -> list:
    """Bulk INSERT using batched VALUES clauses. Returns list of row dicts."""
    if not vals:
        return []
    all_results = []
    for batch_start in range(0, len(vals), BATCH_SIZE):
        batch = vals[batch_start:batch_start + BATCH_SIZE]
        col_list = ", ".join(columns)
        val_parts = []
        params = {}
        for bi, row in enumerate(batch):
            i = batch_start + bi
            val_parts.append(f"({', '.join(f':{col}{i}' for col in columns)})")
            for col in columns:
                params[f"{col}{i}"] = row.get(col)
        val_clause = ", ".join(val_parts)
        ret = " RETURNING *" if returning else ""
        rows = conn.execute(
            text(f"INSERT INTO {table} ({col_list}) SELECT {', '.join(columns)} "
                 f"FROM (VALUES {val_clause}) AS v({', '.join(columns)}){ret}"),
            params,
        ).fetchall() if returning else conn.execute(
            text(f"INSERT INTO {table} ({col_list}) SELECT {', '.join(columns)} "
                 f"FROM (VALUES {val_clause}) AS v({', '.join(columns)})"),
            params,
        )
        if returning:
            all_results.extend(rows)
    return all_results


# ── Process ────────────────────────────────────────────────────────────

def process_body(conn, body: str, wm: int, entity_cache: dict,
                 dry_run: bool = False, verbose: bool = False) -> dict:
    """Process one chunk of items for a body. Returns stats dict."""
    body_group = find_body_group(body)
    if not body_group:
        return {"processed": 0, "matches": 0, "entities": 0, "edges": 0, "max_id": wm}

    patterns = BODY_PATTERNS[body_group]
    rows = conn.execute(text(QUERY), {"wm": wm, "body": body}).fetchall()
    if not rows:
        return {"processed": 0, "matches": 0, "entities": 0, "edges": 0, "max_id": wm}

    # Phase 1: Scan
    new_entities: dict[tuple[str, str], str] = {}
    new_mention_rows: list[dict] = []
    new_edge_rows: list[dict] = []
    max_id = wm
    total_matches = 0
    # Exact proposal classification.  Every proposal considered below receives
    # exactly one classification, identically in dry and live runs.
    entity_replay = 0
    mention_replay = 0
    mention_unresolved = 0
    edge_replay = 0
    edge_unresolved = 0
    emitted_values: list[tuple[str, str]] = []

    for r in rows:
        item_id = int(r[0])
        item_body = str(r[1])
        text_content = str(r[5] or "")
        case_number = str(r[7] or "").strip()
        c_number = str(r[8] or "").strip()
        max_id = max(max_id, item_id)

        identifier = case_number or c_number
        case_norm = normalize_name(identifier) if identifier else None

        roles_found = set()
        for field_name, role, pattern in patterns:
            if role in roles_found:
                continue
            src = text_content if field_name == "text" else ""
            m = pattern.search(src)
            if not m:
                continue
            actor_name = m.group(1).strip()
            if not validate_name(actor_name):
                continue
            roles_found.add(role)
            total_matches += 1

            # Proposals are collected in dry and live runs alike, so dry mode
            # classifies exactly what live mode would have written.  Nothing is
            # written until the explicit `not dry_run` guards below.
            etype = classify_entity_type(actor_name)
            norm = normalize_name(actor_name, etype)
            entity_key = (norm, etype)
            emitted_values.append(("entity_type", etype))
            emitted_values.append(("role", role))

            if entity_key not in entity_cache and entity_key not in new_entities:
                new_entities[entity_key] = actor_name
            else:
                entity_replay += 1

            new_mention_rows.append({
                "entity_id": None,  # resolved after entity creation
                "source_type": "agenda_item", "source_id": item_id,
                "mention_text": actor_name[:500],
                "context_snippet": actor_name[:300],
                "confidence": 90, "extracted_by": "pattern_cascade",
                "role_in_context": role,
            })

            edge_type = ROLE_EDGE_MAP.get(role, "")
            if case_norm and edge_type:
                case_key = (case_norm, "case")
                if case_key not in entity_cache and case_key not in new_entities:
                    new_entities[case_key] = identifier
                else:
                    entity_replay += 1
                emitted_values.append(("entity_type", "case"))
                emitted_values.append(("relationship", edge_type))
                # Store entity keys (norm + type) for resolution in Phase 4
                actor_type = classify_entity_type(actor_name)
                actor_norm = normalize_name(actor_name, actor_type)
                new_edge_rows.append({
                    "from_norm": actor_norm,
                    "from_type": actor_type,
                    "to_norm": case_norm,
                    "to_type": "case",
                    "relationship": edge_type,
                    "provenance_type": "agenda_item",
                    "provenance_id": item_id,
                    "source_label": f"Pattern: {edge_type}",
                    "edge_kind": "relational",
                    "confidence": 0.9,
                })

    # Phases 2-4: classify every proposal exactly once, then persist only the
    # proposals classified as inserts.  Classification and persistence live in
    # pattern_cascade_persistence so this module stays focused on scanning.
    from scripts.entities.pattern_cascade_persistence import (
        classify_and_write_edges,
        classify_and_write_mentions,
        write_entity_proposals,
    )

    planned_keys = set(new_entities)
    write_entity_proposals(conn, new_entities, entity_cache, dry_run=dry_run)
    mention_replay, mention_unresolved = classify_and_write_mentions(
        conn, new_mention_rows, entity_cache, planned_keys, dry_run=dry_run,
    )
    edge_inserted, edge_replay, edge_unresolved = classify_and_write_edges(
        conn, new_edge_rows, entity_cache, planned_keys, dry_run=dry_run,
    )

    return {
        "processed": len(rows), "matches": total_matches,
        "entities": len(new_entities), "edges": edge_inserted,
        # ``edges_skipped`` is preserved with its historical meaning (every
        # skipped edge); the two exact counters below say *why* each was skipped.
        "edges_skipped": edge_replay + edge_unresolved, "max_id": max_id,
        "entity_replay_collisions": entity_replay,
        "mention_replay_collisions": mention_replay,
        "mentions_unresolved_entity": mention_unresolved,
        "edge_replay_collisions": edge_replay,
        "edges_unresolved_endpoint": edge_unresolved,
        "mentions_planned": len(new_mention_rows),
        "edges_planned": len(new_edge_rows),
        "emitted_values": emitted_values,
    }


def run_pattern_cascade(
    engine,
    body_filter: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Run pattern cascade phase. Returns structured result dict.

    Scans agenda items for role-labeled headers ("Applicant:", "Staff:", etc.)
    and creates entity mentions and entities for each match.
    """
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                body VARCHAR(64) PRIMARY KEY, last_run_at TIMESTAMPTZ DEFAULT now(),
                last_processed_id INTEGER DEFAULT 0, items_processed INTEGER DEFAULT 0,
                entities_created INTEGER DEFAULT 0, edges_created INTEGER DEFAULT 0
            );
        """))

    watermarks = {}
    if not force:
        with engine.connect() as conn:
            rows = conn.execute(text(f"SELECT body, last_processed_id FROM {WATERMARK_TABLE}")).fetchall()
            watermarks = {r[0]: int(r[1]) for r in rows}

    # Load entity cache
    with engine.connect() as conn:
        entity_cache = {}
        rows = conn.execute(text("SELECT normalized_name, entity_type, id FROM entities")).fetchall()
        for r in rows:
            entity_cache[(str(r[0]), str(r[1]))] = int(r[2])

    # Get all body codes
    with engine.connect() as conn:
        all_bodies = [r[0] for r in conn.execute(
            text("SELECT DISTINCT body FROM agenda_items ORDER BY body")
        ).fetchall()]

    if body_filter:
        all_bodies = [b for b in all_bodies if body_filter in b]

    # Pre-filter: only process bodies with patterns, skip watermarked
    targeted_bodies = [b for b in all_bodies if find_body_group(b)]
    if not force:
        targeted_bodies = [b for b in targeted_bodies if b not in watermarks]

    total = {"processed": 0, "matches": 0, "entities": 0, "edges": 0,
             "entity_replay_collisions": 0, "mention_replay_collisions": 0,
             "mentions_unresolved_entity": 0, "edge_replay_collisions": 0,
             "edges_unresolved_endpoint": 0, "mentions_planned": 0,
             "edges_planned": 0}
    emitted_values: list[tuple[str, str]] = []

    for body in sorted(targeted_bodies):
        wm = watermarks.get(body, 0)

        try:
            with engine.begin() as conn:
                stats = process_body(conn, body, wm, entity_cache,
                                     dry_run=dry_run, verbose=verbose)
                if not dry_run and stats["max_id"] > wm:
                    conn.execute(
                        text(f"""
                            INSERT INTO {WATERMARK_TABLE} (body, last_run_at, last_processed_id,
                                items_processed, entities_created, edges_created)
                            VALUES (:body, now(), :mid, :ip, :ec, :edc)
                            ON CONFLICT (body) DO UPDATE SET
                                last_run_at = now(), last_processed_id = :mid,
                                items_processed = {WATERMARK_TABLE}.items_processed + :ip,
                                entities_created = {WATERMARK_TABLE}.entities_created + :ec,
                                edges_created = {WATERMARK_TABLE}.edges_created + :edc
                        """),
                        {"body": body, "mid": stats["max_id"], "ip": stats["processed"],
                         "ec": stats["entities"], "edc": stats["edges"]},
                    )
            for k in total:
                total[k] += stats.get(k, 0)
            emitted_values.extend(stats.get("emitted_values", []))
        except Exception as e:
            log.error("  ✗ %s: %s", body, e, exc_info=verbose)
            if not force:
                raise

    from scripts.entities.pattern_cascade_persistence import (
        pattern_cascade_accounting,
    )
    from scripts.entities.phase_receipt import build_phase_receipt

    return {
        "success": True,
        "items_processed": total["processed"],
        "matches": total["matches"],
        "entities_created": total["entities"],
        "edges_created": total["edges"],
        "bodies_processed": len(targeted_bodies),
        "dry_run": dry_run,
        **{k: v for k, v in total.items()
           if k not in ("processed", "matches", "entities", "edges")},
        "validation_receipt": build_phase_receipt(
            "pattern_cascade",
            dry_run=dry_run,
            values=emitted_values,
            rows=pattern_cascade_accounting(total, dry_run=dry_run),
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--body", type=str)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    engine = get_engine()
    result = run_pattern_cascade(
        engine,
        body_filter=args.body,
        dry_run=args.dry_run,
        force=args.force,
        verbose=args.verbose,
    )

    mode = "DRY RUN" if result["dry_run"] else "DONE"
    log.info("%s — %d items, %d matches, %d entities, %d edges",
             mode, result["items_processed"], result["matches"],
             result["entities_created"], result["edges_created"])

    print(json.dumps({"phase": "pattern_cascade", **result}))


if __name__ == "__main__":
    main()
