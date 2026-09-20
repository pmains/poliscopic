#!/usr/bin/env python3
"""
event_link.py — Link meeting_events to entity graph via event_participants.

Step 3 of the Phase 5 pipeline. Reads canonical meeting_events plus their
extraction raw_text, extracts entity names, matches them to known entities,
infers roles, and writes event_participants records.

Strategy:
    Strategy A — extract name-like tokens from extraction raw_text using patterns
    Strategy B — match extracted names against entities.normalized_name (fuzzy)
    Strategy C — also link through agenda_items via shared meeting context

Usage:
    PYTHONPATH=scripts python3 scripts/entities/event_link.py
    PYTHONPATH=scripts python3 scripts/entities/event_link.py --dry-run
    PYTHONPATH=scripts python3 scripts/entities/event_link.py --limit 1000
    PYTHONPATH=scripts python3 scripts/entities/event_link.py --reprocess
"""

import logging
import json
import os
import re
import sys
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from db import get_engine
from sqlalchemy import text

from scripts.kg.emission import EmissionValidator, emit_validated

from entities.event_link_storage import (
    _classify_candidate_rows,
    _deduplicate_candidates,
    _empty_link_stats,
    _event_id_batch,
    _event_rows,
    _existing_participants,
    _insert_participants,
    _storage_confidence,
    _upgrade_participants,
    load_meeting_entity_lookup,
)

log = logging.getLogger("event_link")

BATCH_SIZE = 500
LINKER_VERSION = "2026-07-27.1"

# ── Name extraction patterns ─────────────────────────────────────────────
#
# Extraction raw_text is extracted from Meeting Result PDFs (via pdftotext).
# It typically contains lines like:
#
#   "APPROVED      2.   Review and approval of items..."
#   "DISCUSSED     4.    ...                        Name, Title"
#   "– Heather Ross, Chair"
#   "Denied    10. Application #: ZA-101-26-5  ...  Applicant: Harminder Singh"
#   "For information, please call Crystal Rosa-Duran, Admin. Assistant"
#
# Person names typically appear as:
#   - "– Name, Title" at end of item line
#   - "Name, Title" as standalone at end of action text
#   - "Applicant: Name" in zoning items
#   - "please call Name, Title" as staff contact
#   - Two CamelCase words separated by whitespace near end of line

# Pattern A: Dash-introduced name with optional title
#   "– Nicole Anderson, MCDI Chair"
#   "– Heather Ross, Chair"
#   "- Name"
DASH_NAME_ROLE_RE = re.compile(
    r"[–-]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"  # Name: First Last or First M Last
    r"(?:,\s*([A-Z][A-Za-z\s.'&-]+))?",           # Optional title
)

# Pattern B: "Applicant: Name"  or "Applicant: Name, Title"
APPLICANT_RE = re.compile(
    r"Applicant:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r"(?:,\s*([A-Za-z][A-Za-z\s.'&-]+))?",
)

# Pattern C: "please call Name, Title" (staff contact)
STAFF_CALL_RE = re.compile(
    r"please\s+call\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r"(?:,\s*([A-Za-z][A-Za-z\s.'&-]+))?",
    re.IGNORECASE,
)

# Pattern D: "Presented by Name" / "Presentation by Name"
PRESENTED_BY_RE = re.compile(
    r"(?:Presented|Presentation)\s+by\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
    re.IGNORECASE,
)

# Pattern E: End-of-line name — the most common pattern in extraction texts
#   "...Trails/Heat Update                  Jarod Rogers"
#   "...Park Steward Update                                Josh Parnell"
#   "JUNE 16, 2026               Announcement of future meeting...                     Board"
EOL_NAME_RE = re.compile(
    r"\s{5,}([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*$",
    re.MULTILINE,
)

# Pattern F: Role line — "Name, Title" at line start after indent
#   "Carrie Brown, Interim Director"
#   "Debra Larson, Vice-Chair"
LINE_ROLE_RE = re.compile(
    r"(?:^|\n)\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r",\s*(Chair|Vice Chair|Vice-Chair|Director|Interim Director|"
    r"Deputy Director|Committee Chair|Team Leader|Manager|"
    r"Administrative Assistant|Commissioner|President|Secretary|Treasurer|"
    r"Member|Board Member|Subcommittee Chair|"
    r"Assistant Director|Planning Director|Development Director|"
    r"Project Manager|Senior Planner|Planner|Principal Planner)",
)

# Pattern G: "For Information: Name – Role"  (Phoenix meeting docs)
INFO_NAME_RE = re.compile(
    r"For\s+(?:Information|Discussion|Action):\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r"(?:\s+[–-]\s+([A-Za-z][A-Za-z\s.'&-]+))?",
    re.IGNORECASE,
)


# ── Role inference ───────────────────────────────────────────────────────

def infer_role(pattern: str, title: str | None = None) -> str:
    """Map extraction pattern + title to canonical role."""
    if pattern == "applicant":
        return "applicant"
    if pattern == "staff_call":
        return "staff"

    if title:
        tl = title.lower().strip()
        if "chair" in tl:
            return "chair"
        if "vice" in tl:
            return "vice_chair"
        if "commissioner" in tl:
            return "commissioner"
        if "member" in tl:
            return "board_member"
        if any(w in tl for w in ("director", "manager", "planner", "leader",
                                  "assistant", "staff", "president", "secretary",
                                  "treasurer")):
            return "staff"
        if "applicant" in tl:
            return "applicant"

    # Default by pattern
    default_roles = {
        "dash_name_role": "presenter",
        "presented_by": "presenter",
        "line_role": "board_member",
        "info_name": "presenter",
        "eol": "presenter",
    }
    return default_roles.get(pattern, "participant")


# ── Name normalization ───────────────────────────────────────────────────

def normalize_name(raw: str) -> str:
    """Normalize a name for entity lookup."""
    return re.sub(r'\s+', ' ', raw.strip().lower())


# ── Entity matching ──────────────────────────────────────────────────────

def load_entity_lookup(engine) -> list[dict]:
    """Load active entities."""
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, name, normalized_name, entity_type
            FROM entities
            WHERE resolution_status IS NULL OR resolution_status = 'canonical'
        """)).fetchall()
    return [
        {"id": int(r[0]), "name": str(r[1] or ""),
         "normalized_name": str(r[2] or ""),
         "entity_type": str(r[3] or "")}
        for r in rows
    ]


def match_entity(name: str, entities: list[dict]) -> list[tuple]:
    """Match extracted name against entities. Returns [(entity, confidence, method)]."""
    norm = normalize_name(name)
    if not norm or len(norm) < 3:
        return []

    matches = []
    for ent in entities:
        en = ent["normalized_name"]
        if not en or len(en) < 3:
            continue

        # Exact match
        if norm == en:
            matches.append((ent, 0.95, "exact"))
            continue

        # Name is contained in entity name or vice versa
        if norm in en or en in norm:
            shorter = min(len(norm), len(en))
            longer = max(len(norm), len(en))
            ratio = shorter / longer
            if ratio >= 0.6:
                matches.append((ent, 0.7 * ratio, "contained"))
                continue

        # Word overlap (at least 2 significant words)
        nw = {w for w in norm.split() if len(w) > 2}
        ew = {w for w in en.split() if len(w) > 2}
        overlap = nw & ew
        if len(overlap) >= 2:
            score = 0.6 * len(overlap) / max(len(nw | ew), 1)
            matches.append((ent, score, "partial"))

    # Deduplicate by entity_id, keep highest confidence
    best = {}
    for ent, conf, method in matches:
        if ent["id"] not in best or conf > best[ent["id"]][1]:
            best[ent["id"]] = (ent, conf, method)

    return sorted(best.values(), key=lambda x: x[1], reverse=True)


# ── Name extraction ──────────────────────────────────────────────────────

def extract_names(raw_text: str) -> list[dict]:
    """Extract entity names from extraction raw_text.

    Returns list of dicts: name, role, pattern, title, confidence.
    """
    results = []
    seen = set()  # (normalized_name, role) dedup

    def add(name: str, role: str, pattern: str, title: str | None, confidence: float):
        key = (normalize_name(name), role)
        if key not in seen and len(name) > 3:
            seen.add(key)
            results.append({
                "name": name, "role": role, "pattern": pattern,
                "title": title, "confidence": confidence,
            })

    # Pattern A: Dash-introduced names
    for m in DASH_NAME_ROLE_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, infer_role("dash_name_role", title), "dash_name_role", title, 0.8)

    # Pattern B: Applicant
    for m in APPLICANT_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, "applicant", "applicant", title, 0.95)

    # Pattern C: Staff call
    for m in STAFF_CALL_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, "staff", "staff_call", title, 0.9)

    # Pattern D: Presented by
    for m in PRESENTED_BY_RE.finditer(raw_text):
        add(m.group(1).strip(), "presenter", "presented_by", None, 0.85)

    # Pattern E: End-of-line names
    for m in EOL_NAME_RE.finditer(raw_text):
        name = m.group(1).strip()
        # Skip if it looks like a month name, day, or number
        if name.split()[0] in ("January", "February", "March", "April", "May", "June",
                                "July", "August", "September", "October", "November",
                                "December", "Monday", "Tuesday", "Wednesday", "Thursday",
                                "Friday", "Saturday", "Sunday"):
            continue
        add(name, "presenter", "eol", None, 0.6)

    # Pattern F: Line-start role lines
    for m in LINE_ROLE_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, infer_role("line_role", title), "line_role", title, 0.75)

    # Pattern G: For Information/Name-Role
    for m in INFO_NAME_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, infer_role("info_name", title), "info_name", title, 0.7)

    return results


# ── Main processing ──────────────────────────────────────────────────────

def link_events(engine, entity_lookup: list[dict],
                meeting_entity_lookup: dict,
                limit: int = None, dry_run: bool = False,
                reprocess: bool = False) -> dict:
    """Process logical meeting events and write event_participants."""
    stats = _empty_link_stats()
    validator = EmissionValidator("event_pipeline", LINKER_VERSION, dry_run=dry_run)
    cursor_id = 0  # Cursor-based pagination: last processed event ID

    while True:
        remaining = None if limit is None else max(limit - stats["events_attempted"], 0)
        if remaining == 0:
            break
        fetch_size = (
            min(BATCH_SIZE, remaining) if remaining is not None else BATCH_SIZE
        )
        event_ids = _event_id_batch(engine, cursor_id, fetch_size)
        if not event_ids:
            break
        rows = _event_rows(engine, event_ids)
        records = {event_id: {"meeting_id": "", "raw_texts": []}
                   for event_id in event_ids}
        for row in rows:
            event_id = int(row[0])
            records[event_id]["meeting_id"] = str(row[1] or "")
            if row[4] is not None:
                records[event_id]["raw_texts"].append(str(row[4]))

        batch_participants = []
        derived_excluded = []

        for event_id in event_ids:
            meeting_id = records[event_id]["meeting_id"]
            cursor_id = event_id
            stats["events_processed"] += 1
            stats["events_attempted"] += 1
            unresolved = set()
            for raw_text in records[event_id]["raw_texts"]:
                if not raw_text or len(raw_text) < 20:
                    continue

                # Strategy A: Extract names from raw text
                names = extract_names(raw_text)
                stats["names_from_text"] += len(names)

                for ni in names:
                    matches = match_entity(ni["name"], entity_lookup)
                    if not matches:
                        unresolved.add((normalize_name(ni["name"]), ni["role"]))
                    for ent, conf, method in matches:
                        batch_participants.append((event_id, ent["id"], ni["role"], conf))
                        stats["matched_via_text"] += 1

            stats["unresolved_names"] += len(unresolved)

            # Strategy C: meeting-context cross-reference is co-occurrence, not
            # observed participation.  It is derived at best, and the canonical
            # store cannot yet represent derived participation, so it is
            # withheld from canonical output and reported on the receipt.
            if meeting_id and meeting_id in meeting_entity_lookup:
                for eid, info in meeting_entity_lookup[meeting_id].items():
                    derived_excluded.append((event_id, eid, info["role"]))
                    stats["names_via_meeting"] += 1
                    stats["matched_via_meeting"] += 1

        batch_participants = _deduplicate_candidates(batch_participants)
        stats["participant_attempts"] += len(batch_participants)

        # Validate EVERY proposed emission, replay/no-op candidates included,
        # before mutation classification and before any write (dry or live).
        validator.start_batch()
        validator.note_derived_exclusion(
            len(derived_excluded),
            "meeting-context co-occurrence is derived, not source-supported "
            "participation; withheld until the approved representation exists",
        )
        validated = emit_validated(
            validator,
            "role",
            batch_participants,
            to_value=lambda candidate: candidate[2],
            source="event_participants",
        )
        batch_participants = [
            (candidate[0], candidate[1], canonical, candidate[3])
            for candidate, canonical in validated
        ]
        validator.complete_validation()

        # Classify and write in one transaction. Exceptions intentionally propagate.
        if batch_participants:
            inserted = 0
            updated = 0
            attempted_mutations = 0
            try:
                with engine.begin() as c:
                    existing = _existing_participants(c, batch_participants)
                    outcomes, insert_rows, update_rows = _classify_candidate_rows(
                        existing, batch_participants
                    )
                    attempted_mutations = len(insert_rows) + len(update_rows)
                    if not dry_run:
                        validator.begin_writes()
                        if insert_rows:
                            inserted = _insert_participants(c, insert_rows)
                        if update_rows:
                            updated = _upgrade_participants(c, update_rows)
            except Exception as error:
                # Classify the attempted mutations first so the mutation
                # equation still balances, then explain them as rolled back.
                # Prior committed batches stay visible on the receipt.
                if attempted_mutations:
                    validator.classify_rows(would_insert=attempted_mutations)
                validator.rollback(
                    attempted_mutations, f"{type(error).__name__}: {error}"
                )
                stats["validation_receipt"] = validator.seal().serialize()
                raise
            if dry_run:
                validator.classify_rows(
                    would_insert=len(insert_rows),
                    would_update=len(update_rows),
                    replay_noop=outcomes["participant_replay_collisions"],
                )
            else:
                # Rows that did not actually insert were already present.
                validator.classify_rows(
                    would_insert=inserted,
                    would_update=updated,
                    replay_noop=(
                        outcomes["participant_replay_collisions"]
                        + max(0, len(insert_rows) - inserted)
                    ),
                )
                validator.commit(inserted + updated)
            stats["participants_planned_insert"] += outcomes["participants_inserted"]
            stats["participants_planned_update"] += outcomes["participants_updated"]
            if not dry_run:
                stats["participants_inserted"] += inserted
                stats["participants_updated"] += updated
                stats["participants_written"] += inserted + updated
                stats["participants_mutated"] += inserted + updated
            stats["participant_replay_collisions"] += outcomes["participant_replay_collisions"]

        if len(event_ids) < BATCH_SIZE:
            break
        if stats["events_processed"] % 500 == 0:
            log.info("  Progress: %d events, %d participants written",
                     stats["events_processed"], stats["participants_written"])

    stats["validation_receipt"] = validator.seal().serialize()
    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 5 entity linker")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reprocess", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.dry_run else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    engine = get_engine()

    # Load entity lookup
    log.info("Loading entity lookup...")
    entities = load_entity_lookup(engine)
    log.info("Loaded %d active entities", len(entities))

    # Brief 016 Step 3 will replace this union with taxonomy traversal; that is a
    # separate behavior change and must not ride along with Brief 018.
    orgs = [e for e in entities if e["entity_type"] in ("organization", "developer",
                                                         "planning_firm", "law_firm")]
    people = [e for e in entities if e["entity_type"] == "person"]
    log.info("  %d organizations/firms, %d people", len(orgs), len(people))

    # Load meeting→entity cross-reference
    log.info("Loading meeting→entity cross-reference...")
    meeting_entities = load_meeting_entity_lookup(engine)
    total_linked = sum(len(v) for v in meeting_entities.values())
    log.info("  %d meetings with entity mentions, %d total entity links",
             len(meeting_entities), total_linked)

    # Count pending
    if not args.reprocess:
        with engine.connect() as c:
            pending = c.execute(text("""
                SELECT COUNT(*) FROM meeting_events e
                WHERE NOT EXISTS (
                    SELECT 1 FROM event_participants ep
                    WHERE ep.meeting_event_id = e.id
                )
            """)).scalar()
        log.info("Events without participants: %d", pending)

    # Run
    start = time.time()
    stats = link_events(engine, entities, meeting_entities,
                        limit=args.limit, dry_run=args.dry_run,
                        reprocess=args.reprocess)
    elapsed = time.time() - start

    mode = "DRY RUN" if args.dry_run else "DONE"
    log.info(
        "%s — %d events, %d names from text (%d matched), "
        "%d via meeting context (%d matched), "
        "%d participants written, %.1fs",
        mode, stats["events_processed"],
        stats["names_from_text"], stats["matched_via_text"],
        stats["names_via_meeting"], stats["matched_via_meeting"],
        stats["participants_written"], elapsed,
    )
    print(json.dumps({
        "step": "link",
        "success": True,
        "dry_run": args.dry_run,
        "stats": stats,
    }))


if __name__ == "__main__":
    main()
