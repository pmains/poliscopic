#!/usr/bin/env python3
"""
phase5_normalizer.py — Normalize extractions into canonical meeting_events.

Step 2 of the Phase 5 pipeline. Reads raw extractions from
meeting_event_extractions, maps action_verb to canonical event_type + outcome,
looks up meeting context from supporting_documents, and writes meeting_events.

Usage:
    PYTHONPATH=scripts python3 scripts/entities/phase5_normalizer.py --dry-run
    POLISCOPIC_ALLOW_SUPERSEDED_PHASE5_NORMALIZER=1 \\
        PYTHONPATH=scripts python3 scripts/entities/phase5_normalizer.py

SUPERSEDED (2026-09-11).  This normalizer paired each extraction with a generated
``meeting_events`` id **by position** (``zip(rows, event_ids)``), so a candidate
that was skipped shifted every later pair.  That is how extraction ids
24195-24606 were mislinked.  It is retained for forensic inspection only: a live
run is refused unless ``POLISCOPIC_ALLOW_SUPERSEDED_PHASE5_NORMALIZER=1`` is set.
Use ``scripts/entities/event_normalize.py`` for real work.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db import get_engine
from sqlalchemy import text

log = logging.getLogger("phase5_normalizer")

BATCH_SIZE = 500
NORMALIZER_VERSION = "2026-07-27.1"

#: Explicit opt-in required before this retired normalizer may write anything.
SUPERSEDED_ENV = "POLISCOPIC_ALLOW_SUPERSEDED_PHASE5_NORMALIZER"


class PairingCardinalityError(RuntimeError):
    """Extraction candidates and inserted event ids cannot be paired one-to-one."""


class PairingOrderError(RuntimeError):
    """Inserted event ids do not follow the order the candidates were submitted in."""


def assert_superseded_execution_allowed() -> None:
    """Refuse a live run of this retired normalizer without an explicit override."""
    if os.environ.get(SUPERSEDED_ENV) != "1":
        raise RuntimeError(
            "phase5_normalizer is superseded: its positional pairing mislinked "
            "meeting_event_extractions rows 24195-24606. Use "
            "scripts/entities/event_normalize.py. Set "
            f"{SUPERSEDED_ENV}=1 only for forensic inspection."
        )


def pair_events_with_extractions(
    extraction_ids: Sequence[int],
    event_ids: Sequence[int],
) -> list[tuple[int, int]]:
    """Pair each extraction id with the event id created for it.

    The sequences are paired positionally *only* after proving they describe the
    same batch: equal length, no duplicates on either side, and strictly
    increasing event ids.  Any mismatch raises instead of shifting every later
    pair, which is the defect that mislinked extraction ids 24195-24606.
    """
    if len(extraction_ids) != len(event_ids):
        raise PairingCardinalityError(
            f"{len(extraction_ids)} extraction candidate(s) but "
            f"{len(event_ids)} inserted event id(s); refusing to pair by position"
        )
    if len(set(extraction_ids)) != len(extraction_ids):
        raise PairingCardinalityError(
            "duplicate extraction ids in batch; refusing to pair by position"
        )
    if len(set(event_ids)) != len(event_ids):
        raise PairingCardinalityError(
            "duplicate event ids returned by insert; refusing to pair by position"
        )
    if any(later <= earlier for earlier, later in zip(event_ids, event_ids[1:])):
        raise PairingOrderError(
            "inserted event ids are not strictly increasing; the batch was "
            "reordered, so positional pairing would mislink"
        )
    return [(int(ext), int(eid)) for ext, eid in zip(extraction_ids, event_ids)]

# ── Action verb → event_type / outcome mapping ─────────────────────────
# Maps the extractor's outcome (lowered action_verb) to canonical values.

# ── Verb key normalization ────────────────────────────────────────────
def normalize_verb(raw: str) -> str:
    """Normalize action_verb to a lookup key.

    Handles newlines, multiple spaces, weird whitespace from pdftotext -layout.
    """
    # Collapse whitespace (including newlines) to single spaces
    import re
    collapsed = re.sub(r'\s+', ' ', raw).strip().lower()
    return collapsed.replace(' ', '_')


VERB_MAP = {
    # Decision — approval
    "approved":                       ("decision.approval",       "approved"),
    "approved_with_conditions":       ("decision.approval",       "approved_with_conditions"),

    # Decision — denial
    "denied":                         ("decision.denial",         "denied"),
    "denied_without_prejudice":       ("decision.denial",         "denied_without_prejudice"),

    # Decision — continuation
    "continued":                      ("decision.continuation",   "continued"),
    "tabled":                         ("decision.continuation",   "tabled"),
    "deferred":                       ("decision.continuation",   "deferred"),
    "extended":                       ("decision.continuation",   "extended"),

    # Legislation — adoption / introduction / amendment
    "adopted":                        ("legislation.adoption",    "adopted"),
    "introduced":                     ("legislation.introduction","introduced"),
    "amended":                        ("legislation.amendment",   "amended"),

    # Procedure — receipt / discussion
    "received":                       ("procedure.receipt",       "received"),
    "received_and_filed":             ("procedure.receipt",       "received"),
    "discussed":                      ("procedure.discussion",    "discussed"),
    "discussion_only":                ("procedure.discussion",    "discussed"),
    "for_discussion":                 ("procedure.discussion",    "discussed"),
    "preliminary_review":             ("procedure.discussion",    "reviewed"),

    # Other decisions
    "withdrawn":                      ("decision.continuation",   "withdrawn"),
    "sustained":                      ("decision.approval",       "sustained"),
    "vacated":                        ("decision.approval",       "vacated"),

    # Procedural — low value, but still events
    "called_to_order":                ("procedure.discussion",    "called_to_order"),
    "no_action":                      ("procedure.discussion",    "no_action"),
    "no_response":                    ("procedure.discussion",    "no_response"),
    "discussion":                     ("procedure.discussion",    "discussed"),
    "for_discussion":                 ("procedure.discussion",    "discussed"),
    "approved_with_stipulations":     ("decision.approval",       "approved_with_conditions"),
    "approved_with_conditions":       ("decision.approval",       "approved_with_conditions"),
    "approved_subject_to":            ("decision.approval",       "approved_with_conditions"),
}

# Outcomes that are procedural boilerplate — still written to events but
# can be filtered downstream.
PROCEDURAL_OUTCOMES = {"called_to_order", "no_action", "no_response"}


def resolve_event_type_id(conn, verb: str) -> int | None:
    """Look up meeting_event_types.id from a slug like 'decision.approval'."""
    slug, _ = VERB_MAP.get(verb, (None, None))
    if not slug:
        return None
    row = conn.execute(
        text("SELECT id FROM meeting_event_types WHERE slug = :slug"),
        {"slug": slug},
    ).fetchone()
    return int(row[0]) if row else None


def normalize(engine, limit: int = None, dry_run: bool = False) -> dict:
    """Read unprocessed extractions and write canonical meeting_events."""
    if not dry_run:
        assert_superseded_execution_allowed()
    stats = {"extractions": 0, "events": 0, "skipped": 0, "errors": 0}
    done = False

    while not done:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT e.id, e.supporting_doc_id, e.action_verb,
                           e.text_offset_start, e.text_offset_end,
                           e.case_number, e.confidence,
                           sd.meeting_id, sd.id AS sd_id
                    FROM meeting_event_extractions e
                    LEFT JOIN supporting_documents sd ON sd.id = e.supporting_doc_id
                    WHERE e.meeting_event_id IS NULL
                    ORDER BY e.id
                    LIMIT :limit
                """),
                {"limit": BATCH_SIZE},
            ).fetchall()

        if not rows:
            break

        # Pre-resolve event_type_ids for this batch
        type_cache = {}
        with engine.connect() as conn:
            for row in rows:
                vk = normalize_verb(str(row[2] or ""))
                if vk not in type_cache:
                    type_cache[vk] = resolve_event_type_id(conn, vk)

        # Build batch of events to insert
        batch_event_rows = []
        batch_extraction_ids = []  # extraction id per candidate, in insert order

        for row in rows:
            ext_id = int(row[0])
            doc_id = int(row[1]) if row[1] else None
            verb_raw = str(row[2] or "").strip()
            verb_key = normalize_verb(verb_raw)
            offset_start = int(row[3]) if row[3] else None
            offset_end = int(row[4]) if row[4] else None
            case_no = str(row[5]) if row[5] else None
            confidence = float(row[6]) if row[6] else 0.0
            meeting_id = str(row[7] or "")
            sd_id = int(row[8]) if row[8] else None

            type_id = type_cache.get(verb_key)
            canonical_outcome = verb_key  # default: use verb_key

            if verb_key in VERB_MAP:
                _, canonical_outcome = VERB_MAP[verb_key]

            if type_id is None and verb_key not in PROCEDURAL_OUTCOMES:
                log.warning("  No event_type for verb: %s (ext_id=%d)", verb_raw, ext_id)
                stats["errors"] += 1
                continue

            # Skip procedural noise? No — still write them, let downstream filter.
            # But we can mark them with low confidence.
            if verb_key in PROCEDURAL_OUTCOMES:
                confidence = min(confidence, 0.3)

            batch_event_rows.append((
                meeting_id,
                sd_id,
                type_id,
                canonical_outcome,
                verb_raw,
                offset_start,
                offset_end,
                case_no,
            ))
            batch_extraction_ids.append(ext_id)
            stats["extractions"] += 1

        if not batch_event_rows:
            break

        if not dry_run:
            with engine.begin() as conn:
                raw_conn = conn.connection
                if hasattr(raw_conn, 'driver_connection'):
                    pg_conn = raw_conn.driver_connection
                else:
                    pg_conn = raw_conn

                from psycopg2.extras import execute_values

                # Insert events and capture their IDs
                # Use RETURNING id to get the generated IDs
                result = execute_values(
                    pg_conn.cursor(),
                    """
                        INSERT INTO meeting_events
                            (meeting_id, supporting_doc_id, event_type_id,
                             outcome, action_verb,
                             text_offset_start, text_offset_end,
                             case_number, created_at)
                        VALUES %s
                        RETURNING id
                    """,
                    [(m, sd, tid, out, verb, s, e, case)
                     for m, sd, tid, out, verb, s, e, case in batch_event_rows],
                    template="(%s, %s, %s, %s, %s, %s, %s, %s, now())",
                    fetch=True,
                )

                # Collect generated event IDs
                event_ids = [r[0] for r in result]

                # Pair each extraction with the event created for it, keyed by the
                # extraction id.  A shortened insert result, a skipped candidate, or
                # a reordered batch raises here instead of silently shifting every
                # later pair (the defect at extraction ids 24195-24606).
                pairs = pair_events_with_extractions(batch_extraction_ids, event_ids)
                update_values = [(eid, ext) for ext, eid in pairs]

                if update_values:
                    execute_values(
                        pg_conn.cursor(),
                        """
                            UPDATE meeting_event_extractions
                            SET meeting_event_id = v.event_id
                            FROM (VALUES %s) AS v(event_id, extraction_id)
                            WHERE id = v.extraction_id
                        """,
                        update_values,
                        template="(%s, %s)",
                    )

            stats["events"] += len(event_ids)
        else:
            stats["events"] += len(batch_event_rows)

        if len(rows) < BATCH_SIZE:
            break

        if limit and stats["extractions"] >= limit:
            break

    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 5 normalizer")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if not args.dry_run:
        assert_superseded_execution_allowed()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    engine = get_engine()
    start = time.time()

    # Count unprocessed extractions
    with engine.connect() as conn:
        pending = conn.execute(
            text("SELECT COUNT(*) FROM meeting_event_extractions WHERE meeting_event_id IS NULL")
        ).scalar()
    log.info("Unprocessed extractions: %d", pending)

    stats = normalize(engine, limit=args.limit, dry_run=args.dry_run)
    elapsed = time.time() - start

    mode = "DRY RUN" if args.dry_run else "DONE"
    log.info(
        "%s — %d extractions → %d events, %d skipped, %d errors, %.1fs",
        mode, stats["extractions"], stats["events"],
        stats["skipped"], stats["errors"], elapsed,
    )


if __name__ == "__main__":
    main()
