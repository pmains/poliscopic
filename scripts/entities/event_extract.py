#!/usr/bin/env python3
"""
event_extract.py — Pattern-based event extraction from Meeting Result docs.

Reads supporting_documents (document_type='Meeting Result'), applies regex
patterns to extract candidate events, and writes to meeting_event_extractions.

Pipeline position: Step 1 — writes only to meeting_event_extractions.
The normalizer (Step 2) reads extractions and creates canonical meeting_events.

Usage:
    PYTHONPATH=scripts python3 scripts/entities/event_extract.py
    PYTHONPATH=scripts python3 scripts/entities/event_extract.py --dry-run
    PYTHONPATH=scripts python3 scripts/entities/event_extract.py --limit 100
"""

import logging
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db import get_engine
from docs.layout_extract import load_artifact_for_text
from entities.event_compound_results import attach_compound_result_groups
from entities.event_result_context import non_current_result_reason
from sqlalchemy import text

log = logging.getLogger("event_extract")

WATERMARK_TABLE = "_event_extract_watermark"
BATCH_SIZE = 50
EXTRACTOR_VERSION = "2026-09-20.1-context-classifier"

# ── Action verb patterns ────────────────────────────────────────────────
# Ordered by specificity (longer patterns first to avoid sub-matches)

ACTION_PATTERNS = [
    # Multi-word actions (must come before single-word)
    (r"APPROVED\s+WITH\s+STIPULATIONS",     "approved_with_conditions"),
    (r"APPROVED\s+WITH\s+CONDITIONS",        "approved_with_conditions"),
    (r"APPROVED\s+SUBJECT\s+TO(?:\s+(?:STIPULATIONS|CONDITIONS))?",
                                                "approved_with_conditions"),
    (r"DENIED\s+WITHOUT\s+PREJUDICE",        "denied_without_prejudice"),
    (r"DENIED\s+AS\s+FILED",                 "denied"),
    (r"RECEIVED\s+AND\s+FILED",              "received"),
    (r"CALLED\s+TO\s+ORDER",                 "called_to_order"),

    # Single-word actions
    (r"APPROVED",                            "approved"),
    (r"DENIED",                              "denied"),
    (r"CONTINUED",                           "continued"),
    (r"TABLED",                              "tabled"),
    (r"ADOPTED",                             "adopted"),
    (r"RECEIVED",                            "received"),
    (r"DISCUSSED",                           "discussed"),
    (r"WITHDRAWN",                           "withdrawn"),
    (r"INTRODUCED",                          "introduced"),
    (r"AMENDED",                             "amended"),
    (r"SUSTAINED",                           "sustained"),
    (r"VACATED",                             "vacated"),
    (r"EXTENDED",                            "extended"),
    (r"DEFERRED",                            "deferred"),

    # Discussion/status indicators
    (r"DISCUSSION\s+ONLY",                   "discussed"),
    (r"NO\s+ACTION",                         "no_action"),
    (r"NO\s+RESPONSE",                       "no_action"),
    (r"FOR\s+DISCUSSION",                    "discussed"),
    (r"PRELIMINARY\s+REVIEW",                "discussed"),
    (r"INFO(?:RMATION)?\s+ONLY",             "discussed"),
]

# Build combined pattern: groups of (full_pattern, outcome)
# We'll use a single regex with named groups via alternation
ACTION_PATTERN_PARTS = []
for pat, outcome in ACTION_PATTERNS:
    ACTION_PATTERN_PARTS.append(f"(?P<a{len(ACTION_PATTERN_PARTS)}>{pat})")

ACTION_RE = re.compile(
    "|".join(ACTION_PATTERN_PARTS),
    re.MULTILINE | re.IGNORECASE,
)


def canonical_outcome_for_predicate(predicate: str) -> str:
    """Map an action predicate to vocabulary without applying context guards."""
    match = ACTION_RE.search(str(predicate))
    if match:
        for index, (_pattern, candidate_outcome) in enumerate(ACTION_PATTERNS):
            if match.group(f"a{index}"):
                return candidate_outcome
    return re.sub(r"\s+", "_", str(predicate).strip().casefold())

# ── Case/project number patterns ────────────────────────────────────────
CASE_RE = re.compile(
    r"(?:Z[-/\s]?\d{3,6}|"
    r"PLN\d{4,6}|"
    r"SPL\s*\d{3,6}|"
    r"V[OA]\s*\d{3,6}|"
    r"CASE\s*\d{4,9}|"
    r"Project\s*(?:Number|#|No\.?)\s*[-:.]?\s*\d{3,9}|"
    r"(?<!\w)(\d{2}-\d{4,6})(?!\w))",
    re.IGNORECASE,
)

# ── Item number pattern ────────────────────────────────────────────────
ITEM_NO_RE = re.compile(
    r"(?<!\w)(\d+)\.\s+",
)


def _non_result_reason(action: str, row_text: str, start: int, end: int) -> str | None:
    """Reject only high-confidence lexical uses that are not meeting results."""
    before = row_text[max(0, start - 45):start].lower()
    after = row_text[end:min(len(row_text), end + 70)].lower()
    whole = row_text.lower()
    normalized = re.sub(r"\s+", " ", action.lower()).strip()
    if normalized == "continued" and re.search(r"continued\s+page\s+\d", whole):
        return "pagination"
    if normalized == "continued" and (
        re.search(r"continued\s+from\b", whole)
        or re.search(r"unless\s+continued\b", whole)
    ):
        return "historical_or_conditional"
    if normalized == "deferred" and re.match(
        r"\s+(?:compensation|retirement\s+option\s+plan)\b", after
    ):
        return "noun_phrase"
    if normalized == "introduced" and re.match(r"\s+in\s+(?:19|20)\d{2}\b", after):
        return "historical_reference"
    if normalized in {"discussed", "for discussion"} and re.search(
        r"\bnot\s+(?:for\s+)?$", before
    ):
        return "negated"
    if normalized == "approved" and (
        re.search(r"\bany\s+$", before)
        or re.search(r"\bas\s+$", before)
    ):
        return "conditional_or_attributive"
    if normalized == "approved" and (
        (before.endswith("(") and re.search(
            r"\b(?:zoning|district|pcd|pud|rh|r1|r-\d|c-\d)\b", after
        ))
        or (
            re.search(
                r"\b(?:pcd|pud|zoning|district|residence)\b.{0,70}$", before,
                re.DOTALL,
            )
            and re.match(
                r"\s*(?:single-family|multi-?family|resort|ranch|residence)", after
            )
        )
        or (
            re.search(
                r"\b(?:residence|district|commercial|industrial|pcd|pud),\s*$",
                before,
            )
            and re.match(
                r"\s*(?:intermediate\s+commercial|resort\s+district|"
                r"single-family|multi-?family|planned\s+community|"
                r"residential|commercial|industrial)\b",
                after,
            )
        )
    ):
        return "zoning_descriptor"
    if normalized in {"received", "received and filed"} and (
        re.search(r"\b(?:federal\s+)?funding\s+has\s+been\s+$", before)
        or re.search(
            r"in\s+accordance\s+with\s+a\s+request.{0,100}received\s+and\s+filed\s+with\s+the\s+city",
            whole,
        )
    ):
        return "narrative_or_notice"
    if normalized == "extended" and re.match(
        r"\s+(?:his|her|their|its)\s+appreciation\b", after
    ):
        return "narrative_verb"
    if normalized == "adopted" and re.search(r"\bthe\s+$", before) and re.match(
        r"\s+[a-z0-9][^.]{0,80}\b(?:plan|code|policy|ordinance)\b", after
    ):
        return "adjectival_reference"
    if normalized == "preliminary review" and re.match(r"\s+of\b", after):
        return "agenda_item_title"
    if normalized == "amended" and re.match(r"\s+meeting\s+minutes\b", after):
        return "agenda_item_title"
    return None


def _evidence_scopes(text_content: str, artifact: dict | None):
    """Yield exact row scopes when geometry exists, otherwise whole text."""
    if artifact:
        yielded = False
        for page in artifact.get("pages", []):
            for row in page.get("rows", []):
                start, end = row.get("text_start"), row.get("text_end")
                if isinstance(start, int) and isinstance(end, int) and end > start:
                    yielded = True
                    yield text_content[start:end], start, row, page.get("regions", [])
        if yielded:
            return
    yield text_content, 0, None, []


def extract_events_from_text(
    doc_id: int, text_content: str, layout_artifact: dict | None = None
) -> list[dict]:
    """Extract candidate events from a meeting result document.

    Returns list of dicts with keys: raw_text, action_verb, confidence,
    text_offset_start, text_offset_end, case_number (nullable).
    """
    events = []

    for scoped_text, scope_start, row, regions in _evidence_scopes(
        text_content, layout_artifact
    ):
        scope_events = []
        for match in ACTION_RE.finditer(scoped_text):
            action_verb = match.group(0).strip()
            action_start = scope_start + match.start()
            action_end = scope_start + match.end()
            if non_current_result_reason(
                action_verb, scoped_text, match.start(), match.end()
            ):
                continue
            semantic_start = max(0, action_start - 80)
            semantic_end = min(len(text_content), action_end + 120)
            semantic_context = text_content[semantic_start:semantic_end]
            semantic_action_start = action_start - semantic_start
            semantic_action_end = action_end - semantic_start
            if _non_result_reason(
                action_verb, semantic_context,
                semantic_action_start, semantic_action_end,
            ):
                continue
            region = next((
                candidate for candidate in regions
                if any(
                    isinstance(token.get("text_start"), int)
                    and token["text_end"] > action_start
                    and token["text_start"] < action_end
                    for token in candidate.get("tokens", [])
                )
            ), None)
            if re.fullmatch(r"info(?:rmation)?\s+only", action_verb, re.I) and not (
                region
                and region.get("role") == "result"
                and region.get("basis") in {
                    "explicit_result_header_same_visual_row_item",
                    "explicit_results_visual_column_same_row_item",
                }
                and region.get("item_number")
            ):
                continue

            outcome = canonical_outcome_for_predicate(action_verb)

            if region is not None:
                raw_text = str(region.get("text", "")).strip()
            elif row is not None:
                raw_text = scoped_text.strip()
            else:
                context_end = min(action_end + 300, len(text_content))
                raw_text = text_content[action_start:context_end].strip()

            context_window = text_content[
                max(0, action_start - 100):min(len(text_content), action_end + 200)
            ]
            case_match = CASE_RE.search(context_window)
            case_number = case_match.group(0) if case_match else None
            if case_number:
                case_number = case_number.replace(" ", "").upper()

            line_start = text_content.rfind("\n", 0, action_start) + 1
            column = action_start - line_start
            if row is not None and row.get("bbox") is not None:
                confidence = 0.95 if match.start() < 25 else 0.8
            else:
                confidence = 0.9 if column < 15 else 0.7

            scope_events.append({
                "raw_text": raw_text[:1000],
                "action_verb": action_verb,
                "outcome": outcome,
                "confidence": confidence,
                "text_offset_start": action_start,
                "text_offset_end": action_end,
                "case_number": case_number,
                "layout_region_id": region.get("region_id") if region else None,
                "layout_role": region.get("role") if region else None,
                "layout_item_number": region.get("item_number") if region else None,
                # Preserve the exact predicate span in ``raw_text`` while
                # exposing its already-bounded visual row for diagnostics
                # that must distinguish repeated statuses in one document.
                "layout_context": scoped_text.strip()[:1000] if region else None,
            })

        events.extend(attach_compound_result_groups(
            scope_events,
            document_id=doc_id,
            evidence_text=scoped_text,
            evidence_start=scope_start,
            row_id=str(row.get("row_id")) if row and row.get("row_id") else None,
        ))

    return events


def process_docs(engine, limit: int = None, dry_run: bool = False,
                 doc_id: int | None = None) -> dict:
    """Process supporting_documents and write extractions.

    Returns stats dict.
    """
    # Get watermark
    watermark = 0
    if not dry_run:
        with engine.connect() as conn:
            try:
                row = conn.execute(
                    text(f"SELECT COALESCE(MAX(last_doc_id), 0) FROM {WATERMARK_TABLE}")
                ).scalar()
                watermark = row or 0
            except Exception:
                watermark = 0
    log.info("Watermark last_doc_id=%d (dry_run=%s)", watermark, dry_run)

    grand = {"docs": 0, "events_found": 0, "events_inserted": 0,
             "skipped_existing": 0}
    done = False

    while not done:
        with engine.connect() as conn:
            if doc_id is not None:
                rows = conn.execute(text("""
                    SELECT id, text_content, meeting_id, body, content_hash,
                           text_extraction_method
                    FROM supporting_documents
                    WHERE id = :doc_id AND document_type = 'Meeting Result'
                      AND text_content IS NOT NULL AND text_content != ''
                """), {"doc_id": doc_id}).fetchall()
            else:
                rows = conn.execute(text("""
                    SELECT id, text_content, meeting_id, body, content_hash,
                           text_extraction_method
                    FROM supporting_documents
                    WHERE id > :wm
                      AND document_type = 'Meeting Result'
                      AND text_content IS NOT NULL AND text_content != ''
                    ORDER BY id
                    LIMIT :limit
                """), {"wm": watermark, "limit": BATCH_SIZE}).fetchall()

        if not rows:
            break

        # Collect all events for this batch
        batch_events = []  # list of (doc_id, events_list)
        for row in rows:
            current_doc_id = int(row[0])
            text_content = str(row[1] or "")

            source_hash = str(row[4] or "") or None
            extraction_method = str(row[5] or "") or None
            layout_artifact = load_artifact_for_text(
                text_content, source_hash, method=extraction_method
            )
            if layout_artifact is None:
                # Historical rows may not have a source hash. Unique retained
                # text remains a safe fallback; ambiguity fails closed.
                layout_artifact = load_artifact_for_text(text_content)
            events = extract_events_from_text(
                current_doc_id, text_content, layout_artifact=layout_artifact
            )

            grand["docs"] += 1
            grand["events_found"] += len(events)
            batch_events.append((current_doc_id, events))
            watermark = current_doc_id

            if limit and grand["docs"] >= limit:
                done = True
                break

        # Single transaction for the batch — multi-row INSERT via executemany
        if not dry_run and batch_events:
            last_doc_id = batch_events[-1][0]
            total_events = sum(len(evts) for _, evts in batch_events)
            now_ts = datetime.now(timezone.utc)
            with engine.begin() as conn:
                raw_conn = conn.connection
                if hasattr(raw_conn, 'driver_connection'):
                    pg_conn = raw_conn.driver_connection
                else:
                    pg_conn = raw_conn
                
                # Collect all event params
                event_rows = []
                for batch_doc_id, events_list in batch_events:
                    for ev in events_list:
                        event_rows.append((
                            EXTRACTOR_VERSION,
                            ev["raw_text"],
                            ev["confidence"],
                            now_ts,
                            batch_doc_id,
                            ev["action_verb"],
                            ev["text_offset_start"],
                            ev["text_offset_end"],
                            ev.get("case_number"),
                        ))
                
                if event_rows:
                    # Use psycopg2.extras.execute_values for safe multi-row INSERT
                    from psycopg2.extras import execute_values
                    # Producer-local replay guard: same pattern extractor/version,
                    # source document, and exact source span. This does not merge
                    # similar actions or evidence from independent sources.
                    existing = set(conn.execute(text("""
                        SELECT supporting_doc_id, text_offset_start, text_offset_end,
                               action_verb, extractor_version
                        FROM meeting_event_extractions
                        WHERE extractor = 'pattern'
                          AND supporting_doc_id = ANY(:doc_ids)
                    """), {"doc_ids": list({r[4] for r in event_rows})}).fetchall())
                    filtered = [r for r in event_rows
                                if (r[4], r[6], r[7], r[5], r[0]) not in existing]
                    grand["skipped_existing"] += len(event_rows) - len(filtered)
                    if filtered:
                        execute_values(
                            pg_conn.cursor(),
                            """
                            INSERT INTO meeting_event_extractions
                                (meeting_event_id, extractor, extractor_version,
                                 raw_text, confidence, created_at,
                                 supporting_doc_id, action_verb, text_offset_start,
                                 text_offset_end, case_number)
                            VALUES %s
                            """,
                            filtered,
                            template="(NULL, 'pattern', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        )
                    grand["events_inserted"] += len(filtered)
                
                # Single watermark for the batch
                if doc_id is None:
                    conn.execute(
                        text(
                            f"INSERT INTO {WATERMARK_TABLE} "
                            f"(last_doc_id, docs_processed, events_found, run_at) "
                            f"VALUES (:doc_id, :docs, :events, :now)"
                        ),
                        {"doc_id": last_doc_id,
                         "docs": len(batch_events),
                         "events": total_events,
                         "now": now_ts},
                    )

        if grand["docs"] % 100 == 0:
            log.info("  Progress: %d docs, %d events found, %d inserted, %d existing",
                     grand["docs"], grand["events_found"],
                     grand["events_inserted"], grand["skipped_existing"])

        if done:
            break
        if doc_id is not None:
            break

    return grand


def ensure_watermark_table(engine):
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                    id SERIAL PRIMARY KEY,
                    last_doc_id INTEGER NOT NULL DEFAULT 0,
                    docs_processed INTEGER NOT NULL DEFAULT 0,
                    events_found INTEGER NOT NULL DEFAULT 0,
                    run_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Phase 5 pattern-based event extraction")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--doc-id", type=int, default=None,
                        help="Process one document without advancing the watermark")
    args = parser.parse_args()

    level = logging.DEBUG if args.dry_run else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")

    engine = get_engine()

    if not args.dry_run:
        ensure_watermark_table(engine)

    start = time.time()
    stats = process_docs(engine, limit=args.limit, dry_run=args.dry_run,
                         doc_id=args.doc_id)
    elapsed = time.time() - start

    mode = "DRY RUN" if args.dry_run else "DONE"
    log.info(
        "%s — %d docs, %d events found, %d inserted, %d existing, %.1fs",
        mode, stats["docs"], stats["events_found"], stats["events_inserted"],
        stats["skipped_existing"], elapsed,
    )
    print(json.dumps({"step": "extract", "success": True, "stats": stats}))


if __name__ == "__main__":
    main()
