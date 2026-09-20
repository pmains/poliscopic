#!/usr/bin/env python3
"""``stage2_subitem_containment.py`` — agenda_subitem identity and PART_OF.

An agenda item's number sometimes encodes a hierarchy: ``4`` contains ``4.A``.
This module decides when that is knowable, and refuses to guess otherwise.

Three commitments, all fail-closed:

* **Identity is normalised numbering.** An ``agenda_subitem`` is identified by
  ``(meeting_db_id, normalised_number)`` where the number parses as ``N`` or
  ``N.L``.  A label that does not parse — ``22C``, ``sec-2``, a bare ``A`` — is
  **not** given a hierarchy; it is held as ambiguous rather than attached to
  whatever happens to precede it.
* **``PART_OF`` is not ``ATTACHED_TO``.**  A document attaches to an item
  (``ATTACHED_TO``).  An item contains another item (``PART_OF``).  They are
  different relations between different things and are never merged.  Roles and
  outcomes are not entities at all, per the Stage 1 registries.
* **Flat linking survives.**  Nothing here is required for a document to link to
  an item: an item with no hierarchy is still a perfectly good target.  Hierarchy
  is an addition, never a precondition.

Document order is never consulted.  A letter-only item is not the child of the
item above it; if its parent is not named, it has no parent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_subitem_manifest as manifest_mod  # noqa: E402
from scripts.kg import stage2_subitem_schema as schema_mod  # noqa: E402

__all__ = [
    "PLAN_KIND",
    "build_plan",
    "plan_digest",
    "replay_digest",
    "validate_plan",
    "BASELINE_KIND",
    "CLASSES",
    "CLASS_AMBIGUOUS",
    "CLASS_DEEPER",
    "CLASS_COLLISION",
    "CLASS_INVALID",
    "CLASS_ROOT",
    "CLASS_SUBITEM",
    "PLAN_KIND",
    "PART_OF",
    "ATTACHED_TO",
    "audit",
    "build_plan",
    "classify_item",
    "normalize",
    "parent_of",
    "parse_hierarchy",
    "plan_digest",
    "replay_digest",
    "validate_plan",
]

BASELINE_KIND = "kg-stage2-subitem-containment-baseline"
PLAN_KIND = "kg-stage2-subitem-containment-plan"
PLAN_VERSION = "kg-stage2-subitem-containment/1.0"

#: The two relations, kept explicitly distinct.
PART_OF = "PART_OF"
ATTACHED_TO = "ATTACHED_TO"

CLASS_ROOT = "root"
CLASS_SUBITEM = "subitem"
CLASS_DEEPER = "deeper"
CLASS_AMBIGUOUS = "ambiguous"
CLASS_INVALID = "invalid"
CLASS_COLLISION = "collision_held"
CLASSES = (CLASS_ROOT, CLASS_SUBITEM, CLASS_DEEPER, CLASS_AMBIGUOUS,
           CLASS_INVALID, CLASS_COLLISION)

#: ``N`` or ``N.L`` or ``N.LL`` — digits, then optional dot-separated letters.
_HIER = re.compile(r"^(\d{1,3})(?:\.([A-Z]{1,3}))?$")


def normalize(number: Any) -> str:
    """Trim, drop a trailing period, upper-case.  No other rewriting."""
    text = str(number or "").strip().upper()
    return text[:-1] if text.endswith(".") else text


def parse_hierarchy(number: Any) -> dict[str, Any] | None:
    """``4.AA`` -> ``{segments: ['4','AA'], depth: 1, prefix: '4'}``; else None."""
    text = normalize(number)
    match = _HIER.match(text)
    if not match:
        return None
    root, tail = match.group(1), match.group(2)
    segments = [root] if tail is None else [root, tail]
    return {
        "normalized": text,
        "segments": segments,
        "depth": len(segments) - 1,
        "prefix": ".".join(segments[:-1]) if len(segments) > 1 else None,
    }


def parent_of(number: Any) -> str | None:
    """The immediate parent number, or None when the item has no hierarchy."""
    parsed = parse_hierarchy(number)
    if parsed is None or parsed["depth"] == 0:
        return None
    return parsed["prefix"]


def classify_item(number: Any, *, siblings: Sequence[str]) -> dict[str, Any]:
    """One item's containment class, reason and proposed parent.  Pure.

    ``siblings`` are the other normalised numbers present in the same meeting.
    """
    text = normalize(number)
    if not text:
        return {"number": "", "class": CLASS_INVALID,
                "reason": "the item carries no identifier", "parent": None}

    parsed = parse_hierarchy(text)
    if parsed is None:
        return {"number": text, "class": CLASS_AMBIGUOUS, "parent": None,
                "reason": f"identifier {text!r} is not hierarchical numbering; it is "
                          f"refused rather than attached by position"}

    depth = parsed["depth"]
    if depth == 0:
        return {"number": text, "class": CLASS_ROOT, "parent": None,
                "reason": "a top-level number contains no other item"}

    parent = parsed["prefix"]
    present = {normalize(s) for s in siblings}
    present.discard(text)
    if parent not in present:
        return {"number": text, "class": CLASS_AMBIGUOUS, "parent": None,
                "reason": f"the parent {parent!r} does not exist in this meeting, so "
                          f"{text!r} is an orphan rather than a subitem"}
    if depth >= 2:
        return {"number": text, "class": CLASS_DEEPER, "parent": parent,
                "reason": f"{text!r} is {depth} levels deep"}
    return {"number": text, "class": CLASS_SUBITEM, "parent": parent,
            "reason": f"{parent!r} exists in this meeting"}


def collision_population(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The governed collision population.

    A collision is two rows in one meeting claiming one normalised number.  It is
    not a footnote: the identity itself is ambiguous, so neither row may be
    classified as a root or a subitem.  The population is reported with a distinct
    key count, the number of rows involved, the excess over one per key, and a
    digest over the exact key set.
    """
    buckets: dict[tuple[int, str], list[int]] = {}
    for item in items:
        number = normalize(item.get("agenda_item_number"))
        if not number:
            continue
        buckets.setdefault((int(item["meeting_db_id"]), number), []).append(int(item["id"]))
    colliding = {k: sorted(v) for k, v in buckets.items() if len(v) > 1}
    keys = sorted(f"{m}|{n}" for (m, n) in colliding)
    rows = sorted(i for ids in colliding.values() for i in ids)
    return {
        "distinct_keys": len(colliding),
        "involved_rows": len(rows),
        "excess": sum(len(ids) - 1 for ids in colliding.values()),
        "keys": keys,
        "keys_digest": manifest_mod.canonical_sha256(keys),
        "row_ids_digest": manifest_mod.canonical_sha256(rows),
        "row_ids": rows,
    }


def audit(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Classify the whole agenda-item population, exactly once each.

    ``items`` are mappings with ``id``, ``meeting_db_id``, ``body`` and
    ``agenda_item_number``.
    """
    by_meeting: dict[int, list[str]] = {}
    for item in items:
        by_meeting.setdefault(int(item["meeting_db_id"]), []).append(
            str(item.get("agenda_item_number") or ""))

    collisions = collision_population(items)
    colliding_rows = set(collisions["row_ids"])

    rows: list[dict[str, Any]] = []
    for item in items:
        item_id = int(item["id"])
        meeting = int(item["meeting_db_id"])
        number = normalize(item.get("agenda_item_number"))
        if item_id in colliding_rows:
            # The identity is ambiguous, so containment is not proposed for this
            # row at all: it is held with an explicit collision disposition.
            verdict = {
                "number": number, "class": CLASS_COLLISION, "parent": None,
                "reason": f"{number!r} is claimed by more than one row in meeting "
                          f"{meeting}; the identity is ambiguous so no containment is "
                          f"proposed",
            }
        else:
            verdict = classify_item(item.get("agenda_item_number"),
                                    siblings=by_meeting.get(meeting, []))
        verdict.update({"item_id": item_id, "meeting_db_id": meeting,
                        "body": item.get("body")})
        rows.append(verdict)

    counts = {name: 0 for name in CLASSES}
    for row in rows:
        counts[row["class"]] += 1
    by_body: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_body.setdefault(str(row["body"]), {name: 0 for name in CLASSES})
        bucket[row["class"]] += 1

    links = [{"item_id": row["item_id"], "meeting_db_id": row["meeting_db_id"],
              "number": row["number"], "parent": row["parent"],
              "relation": PART_OF}
             for row in rows if row["class"] in (CLASS_SUBITEM, CLASS_DEEPER)]
    parent_numbers = {(l["meeting_db_id"], l["parent"]) for l in links}
    present = {(row["meeting_db_id"], row["number"]) for row in rows
               if row["class"] != CLASS_COLLISION}
    orphans = sorted(f"{m}/{p}" for m, p in parent_numbers - present)
    cycles = [l["number"] for l in links if l["parent"] == l["number"]]

    ids = sorted(row["item_id"] for row in rows)
    return {
        "kind": BASELINE_KIND,
        "version": "kg-stage2-subitem-containment-baseline/2.0",
        "counts": counts,
        "total": len(rows),
        "population": {
            "count": len(ids),
            "item_ids_sha256": manifest_mod.canonical_sha256(ids),
            "identity": "(meeting_db_id, normalized_number)",
            "source": "agenda_items",
            "deterministic": True,
            "mutually_exclusive": True,
            "reconciles": (sum(counts.values()) == len(rows) == len(set(ids))),
        },
        "by_body": {k: dict(sorted(v.items())) for k, v in sorted(by_body.items())},
        "relations": {PART_OF: len(links), ATTACHED_TO: 0},
        "collision_population": collisions,
        "containment": {
            "proposed_links": len(links),
            "orphans": orphans[:20],
            "orphan_count": len(orphans),
            "cycles": cycles,
        },
        "rules": {
            "identity": "(meeting_db_id, normalized_number) where the number parses N or N.L",
            "parent": "N.L belongs to N only when N exists in the same meeting",
            "refused": "non-hierarchical identifiers (22C, sec-2, a bare A) get no parent",
            "collision": "a number claimed twice makes both rows ambiguous; held, not classified",
            "document_order": "never consulted",
            "attached_to_is_distinct": "documents ATTACH to items; items are PART_OF items",
            "roles_and_outcomes_are_not_entities": True,
        },
        "rows": rows,
    }


# The plan builder and validator live in :mod:`stage2_subitem_plan`; they are
# re-exported here so callers and tests keep one entry point.
from scripts.kg.stage2_subitem_plan import (  # noqa: E402
    PLAN_KIND, build_plan, plan_digest, replay_digest, validate_plan,
)
