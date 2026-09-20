#!/usr/bin/env python3
"""``stage2_s2_classify.py`` — how a supporting document is attached.

Pure classification: read the current population, decide what each document
attaches to, and group the result.  Nothing here writes.

Linking is a deterministic cascade, never a guess.  ``agenda_item_id`` is a
**source-system key**, not an item number, and writers store the literal ``"0"``
for a document that has no key of its own while the real reference sits in
``agenda_item_number`` (``scraper/platforms/civicclerk.py``,
``scraper/platforms/onbase.py``).  All 31,046 such rows carry a non-blank item
number, so ``"0"`` means "no document key", never "item zero".
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import inspect, text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = [
    "CLASSES",
    "DISJOINT_CLASSES",
    "HELD_CLASSES",
    "HOLD_REASONS",
    "MEETING_LEVEL_KEY_PREFIX",
    "PLACEHOLDER_SOURCE_KEY",
    "PROTECTED_TABLES",
    "protected_counts",
    "STRATEGIES",
    "adjudication",
    "agenda_item_fingerprint",
    "classify",
    "document_fingerprint",
    "populations",
]



#: Documents that receive a canonical identity (the two deterministic links).
DISJOINT_CLASSES = ("attached_item_number", "attached_source_key")


#: Documents that must stay NULL and are carried with an explicit reason.
HELD_CLASSES = ("held_ambiguous", "gap_missing_target", "meeting_level_only",
                "unassigned_placeholder")
CLASSES = DISJOINT_CLASSES + HELD_CLASSES


HOLD_REASONS = {
    "held_ambiguous": "item number is ambiguous and the source key does not "
                      "identify exactly one candidate",
    "gap_missing_target": "item number references an agenda item that was never acquired",
    "meeting_level_only": "document is meeting-level: it carries a result key or "
                          "no item number at all",
    "unassigned_placeholder": "both the source key and the item number are the "
                              "\"0\" placeholder, so the document records no item "
                              "reference at all: it keeps its meeting association "
                              "and is left unassigned",
}


#: A source key of nothing but digits is a reference to an item number.
#: The literal key a writer stores when a document has no source key of
#: its own.  The real item reference then lives in ``agenda_item_number``.
PLACEHOLDER_SOURCE_KEY = "0"


#: Meeting-result documents are keyed ``result-{body}-{meeting_id}`` by
#: ``scripts/sync/extract_results_pdfs.py``.  They are meeting-level artefacts,
#: not item references, so they are never treated as a missing item.
MEETING_LEVEL_KEY_PREFIX = "result-"


#: Link the item number normally, and fall back to the document's own source key
#: when the item number alone is ambiguous.
STRATEGIES = ("meeting_item_number", "source_key_disambiguation", "alternate_source_key")


#: Tables whose row counts must be identical before and after an apply, and
#: which a backup receipt must therefore cover.
PROTECTED_TABLES = (
    "agenda_items",
    "entities",
    "entity_mentions",
    "entity_relationships",
    "event_participants",
    "meeting_event_extractions",
    "meeting_events",
    "meetings",
    "supporting_documents",
)


def protected_counts(connection: Any) -> dict[str, int]:
    """Row counts for every protected table that exists in this database.

    Presence is resolved from the catalogue so the same call works against a
    partial mechanics fixture.  On PostgreSQL every protected table exists, so
    the recorded baseline is complete.
    """
    available = set(inspect(connection).get_table_names())
    return {
        table: int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar())
        for table in PROTECTED_TABLES
        if table in available
    }


def document_fingerprint(row: Mapping[str, Any]) -> str:
    """Stable identity of a supporting-document row as it stands today."""
    body = {
        "id": int(row["id"]),
        "meeting_db_id": int(row["meeting_db_id"]),
        "agenda_item_id": row["agenda_item_id"],
        "agenda_item_number": row["agenda_item_number"],
        "document_url": row["document_url"],
        "updated_at": str(row["updated_at"]),
    }
    return hashlib.sha256(artifacts.canonical_json(body).encode("utf-8")).hexdigest()


def agenda_item_fingerprint(row: Mapping[str, Any]) -> str:
    """Stable identity of a canonical agenda item row."""
    body = {
        "id": int(row["id"]),
        "meeting_db_id": int(row["meeting_db_id"]),
        "agenda_item_number": row["agenda_item_number"],
        "agenda_item_id": row["agenda_item_id"],
    }
    return hashlib.sha256(artifacts.canonical_json(body).encode("utf-8")).hexdigest()


_SELECT = f"""
SELECT sd.id                AS id,
       sd.meeting_db_id     AS meeting_db_id,
       sd.body              AS body,
       sd.agenda_item_id    AS agenda_item_id,
       sd.agenda_item_number AS agenda_item_number,
       sd.document_url      AS document_url,
       sd.updated_at        AS updated_at,
       COALESCE(a.n, 0)     AS item_number_candidates,
       a_one.ai_id          AS item_number_item_id,
       COALESCE(b.n, 0)     AS source_key_candidates,
       b.one_id             AS source_key_item_id,
       COALESCE(a_key.n, 0) AS key_match_candidates,
       a_key.ai_id          AS key_match_item_id
FROM supporting_documents sd
LEFT JOIN (
    SELECT sd2.id AS sd_id, COUNT(ai.id) AS n
    FROM supporting_documents sd2
    LEFT JOIN agenda_items ai
      ON ai.meeting_db_id = sd2.meeting_db_id
     AND ai.agenda_item_number = sd2.agenda_item_number
    GROUP BY sd2.id
) a ON a.sd_id = sd.id
LEFT JOIN (
    SELECT sd3.id AS sd_id, MIN(ai.id) AS ai_id
    FROM supporting_documents sd3
    JOIN agenda_items ai
      ON ai.meeting_db_id = sd3.meeting_db_id
     AND ai.agenda_item_number = sd3.agenda_item_number
    GROUP BY sd3.id
) a_one ON a_one.sd_id = sd.id
LEFT JOIN (
    SELECT sd5.id AS sd_id, MIN(ai.id) AS ai_id, COUNT(*) AS n
    FROM supporting_documents sd5
    JOIN agenda_items ai
      ON ai.meeting_db_id = sd5.meeting_db_id
     AND ai.agenda_item_number = sd5.agenda_item_number
     AND ai.agenda_item_id = sd5.agenda_item_id
    GROUP BY sd5.id
) a_key ON a_key.sd_id = sd.id
LEFT JOIN (
    SELECT sd4.id AS sd_id, COUNT(ai.id) AS n, MIN(ai.id) AS one_id
    FROM supporting_documents sd4
    LEFT JOIN agenda_items ai ON ai.agenda_item_id = sd4.agenda_item_id
    GROUP BY sd4.id
) b ON b.sd_id = sd.id
ORDER BY sd.id
"""


def classify(connection: Any) -> list[dict[str, Any]]:
    """Classify every supporting document.  Deterministic cascade, no guessing."""
    files: list[dict[str, Any]] = []
    for row in connection.execute(text(_SELECT)).mappings():
        entry = {
            "document_id": int(row["id"]),
            "meeting_db_id": int(row["meeting_db_id"]),
            "body": row.get("body"),
            "source_key": row["agenda_item_id"],
            "item_number": row["agenda_item_number"],
            "fingerprint": document_fingerprint(row),
            "agenda_item_db_id": None,
            "strategy": None,
            "class": None,
        }
        source_key = str(row["agenda_item_id"] or "")
        item_number = str(row["agenda_item_number"] or "").strip()
        # A placeholder item number is not an item number.  Matching it against a
        # canonical item that happens to be numbered "0" would link a document
        # that records no item reference at all — a placeholder matching a
        # placeholder.  Such rows are never used for a link.
        placeholder_number = item_number in ("", PLACEHOLDER_SOURCE_KEY)
        placeholder_key = source_key == PLACEHOLDER_SOURCE_KEY
        if placeholder_number and placeholder_key:
            entry["class"] = "unassigned_placeholder"
            files.append(entry)
            continue
        if placeholder_number and not placeholder_key and int(row["source_key_candidates"]) == 1:
            # The item number says nothing, but the document's own source key
            # names a canonical item exactly.
            entry["class"] = "attached_source_key"
            entry["strategy"] = "alternate_source_key"
            entry["agenda_item_db_id"] = int(row["source_key_item_id"])
            files.append(entry)
            continue
        if placeholder_number:
            entry["class"] = (
                "meeting_level_only" if source_key.startswith(MEETING_LEVEL_KEY_PREFIX)
                else "unassigned_placeholder"
            )
            files.append(entry)
            continue
        item_number_candidates = int(row["item_number_candidates"])
        if item_number_candidates == 1:
            entry["class"] = "attached_item_number"
            entry["strategy"] = "meeting_item_number"
            entry["agenda_item_db_id"] = int(row["item_number_item_id"])
        elif item_number_candidates > 1 and int(row["key_match_candidates"]) == 1:
            # The item number is ambiguous, but the document's own source key
            # names exactly one of the candidates.  agenda_items.agenda_item_id
            # is unique, so that is an identity, not a guess.
            entry["class"] = "attached_item_number"
            entry["strategy"] = "source_key_disambiguation"
            entry["agenda_item_db_id"] = int(row["key_match_item_id"])
        elif item_number_candidates > 1:
            entry["class"] = "held_ambiguous"
        elif int(row["source_key_candidates"]) == 1:
            entry["class"] = "attached_source_key"
            entry["strategy"] = "alternate_source_key"
            entry["agenda_item_db_id"] = int(row["source_key_item_id"])
        elif source_key.startswith(MEETING_LEVEL_KEY_PREFIX) or not str(
            row["agenda_item_number"] or ""
        ).strip():
            # A meeting-result document carries no item reference at all.
            entry["class"] = "meeting_level_only"
        else:
            entry["class"] = "gap_missing_target"
        files.append(entry)
    return files


def populations(files: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in CLASSES}
    for entry in files:
        counts[str(entry["class"])] += 1
    counts["total_documents"] = len(files)
    counts["deterministic_links"] = sum(counts[c] for c in DISJOINT_CLASSES)
    counts["held_total"] = sum(counts[c] for c in HELD_CLASSES)
    return counts


def adjudication(files: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A bounded grouping of what was decided and what was not.

    Only aggregates are recorded: the per-row classification already lives in
    ``attachments`` and ``holds``, so repeating every row here would double the
    artifact for no reviewer benefit.
    """
    by_strategy: dict[str, int] = {}
    by_class_reason: dict[str, int] = {}
    holds_by_body: dict[str, int] = {}
    gaps_by_key_shape: dict[str, int] = {}
    for entry in files:
        klass = str(entry["class"])
        if entry.get("strategy"):
            by_strategy[str(entry["strategy"])] = by_strategy.get(str(entry["strategy"]), 0) + 1
        if klass in HELD_CLASSES:
            by_class_reason[klass] = by_class_reason.get(klass, 0) + 1
            body = str(entry.get("body") or "unknown")
            holds_by_body[body] = holds_by_body.get(body, 0) + 1
        if klass == "gap_missing_target":
            source_key = str(entry.get("source_key") or "")
            if source_key.startswith(MEETING_LEVEL_KEY_PREFIX):
                shape = "meeting-result key"
            elif source_key == PLACEHOLDER_SOURCE_KEY:
                shape = "placeholder key"
            else:
                shape = "other key"
            gaps_by_key_shape[shape] = gaps_by_key_shape.get(shape, 0) + 1
    return {
        "links_by_strategy": dict(sorted(by_strategy.items())),
        "holds_by_class": dict(sorted(by_class_reason.items())),
        "holds_by_body": dict(sorted(holds_by_body.items(), key=lambda kv: (-kv[1], kv[0]))[:20]),
        "gaps_by_key_shape": dict(sorted(gaps_by_key_shape.items())),
        "deterministic_rules": [
            "meeting_item_number: the item number matches exactly one canonical item",
            "source_key_disambiguation: the item number is ambiguous, but the "
            "document's agenda_item_id equals exactly one candidate's "
            "agenda_item_id, which is unique",
            "alternate_source_key: on the residue, the source key equals a "
            "canonical agenda_item_id",
            "meeting_level_key: a result-{body}-{meeting_id} key, or no item "
            "number at all, is meeting-level",
        ],
        "human_decisions_required": [
            "held_ambiguous rows carrying the placeholder key: the item number "
            "matches several canonical items and the source key picks none",
            "gap_missing_target rows: the item number matches no canonical item",
        ],
    }
