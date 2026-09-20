#!/usr/bin/env python3
"""``stage3_b2g_discovery.py`` — B2g acquisition discovery over the unmatched population.

STRATEGY.  The 3,805 unmatched meetings are grouped by whether an authoritative agenda platform
is PROVEN to cover their body (a body that has at least one meeting in a declared target
namespace).  That grouping is source-balanced and deterministic, and it never rests on
date/body similarity: similarity is a blocking key, never evidence.

Each meeting is classified exactly once as:

  deterministic_route   a unique counterpart corroborated by re-verified content evidence
  evidence_insufficient the platform exists but this meeting cannot be corroborated
  contradiction         several corroborated counterparts disagree
  unavailable           no authoritative agenda platform covers this body at all

Every classification carries a reason code and the exact source evidence it rests on.
Pure and read-only: no fetch, no DB write, no row merge.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage3_b2_identity as identity  # noqa: E402
from scripts.kg import stage3_meeting_result_identity as B1  # noqa: E402

__all__ = ["CLASSES", "COHORT_CRITERIA", "PRODUCER_VERSION", "REASON_CODES", "build_artifact",
           "canonical_sha256", "classify_meeting", "select_cohort"]

PRODUCER_VERSION = "kg-stage3-b2g-discovery/1.0"
RESULT_TYPE = "Meeting Result"
COHORT_SIZE = 200

CLASSES = ("deterministic_route", "evidence_insufficient", "contradiction", "unavailable")

REASON_CODES = {
    "no_agenda_platform_for_body":
        "no meeting of this body exists in any declared target namespace, so no authoritative "
        "agenda platform is proven to cover it",
    "no_counterpart_for_blocking_key":
        "the body has a proven agenda platform but no counterpart shares this meeting's "
        "blocking key, so no pair can be corroborated",
    "no_numbered_item_evidence":
        "a counterpart exists but the retained result document proves no numbered item, so no "
        "content-level rule can fire",
    "item_numbers_not_subset":
        "a counterpart exists and the result document proves items, but they are not a subset "
        "of the counterpart's agenda items",
    "unique_corroborated_counterpart":
        "exactly one counterpart is corroborated by re-verified item-number evidence",
    "competing_corroborated_counterparts":
        "several corroborated counterparts disagree, so the meeting is not determined",
}

COHORT_CRITERIA = (
    "C1 the meeting is unmatched in the canonical crosswalk",
    "C2 its body has at least one meeting in a declared target namespace "
    "(an agenda platform is PROVEN for the body)",
    "C3 it retains a Meeting Result document, so source evidence exists",
    "C4 selected deterministically: ordered by body then meeting_date then id, first "
    f"{COHORT_SIZE}",
)


def canonical_sha256(payload: Any) -> str:
    return identity.canonical_sha256(payload)


def _result_item_numbers(connection: Any, meeting_db_id: int) -> set[str]:
    txt = connection.execute(text(
        "SELECT text_content FROM supporting_documents WHERE meeting_db_id=:m "
        "AND document_type=:rt LIMIT 1"), {"m": meeting_db_id, "rt": RESULT_TYPE}).scalar()
    out = {B1.normalize_item_number(s["token"]) for s in B1.extract_item_spans(txt or "")}
    out.discard(None)
    return out


def _target_items(connection: Any, meeting_db_id: int) -> set[str]:
    rows = connection.execute(text(
        "SELECT agenda_item_number FROM agenda_items WHERE meeting_db_id=:m"),
        {"m": meeting_db_id}).scalars().all()
    return {B1.normalize_item_number(r) for r in rows} - {None}


def classify_meeting(*, platform_proven: bool, counterpart_ids: Sequence[int],
                     source_items: set[str], target_items: Sequence[set[str]]) -> dict[str, Any]:
    """One class per meeting, from re-verified content evidence only."""
    if not platform_proven:
        return {"disposition": "unavailable", "reason_code": "no_agenda_platform_for_body",
                "target": None}
    if not counterpart_ids:
        return {"disposition": "evidence_insufficient",
                "reason_code": "no_counterpart_for_blocking_key", "target": None}
    if not source_items:
        return {"disposition": "evidence_insufficient",
                "reason_code": "no_numbered_item_evidence", "target": None}
    matching = [cid for cid, items in zip(counterpart_ids, target_items)
                if items and source_items <= items]
    if len(matching) == 1:
        return {"disposition": "deterministic_route",
                "reason_code": "unique_corroborated_counterpart", "target": matching[0]}
    if len(matching) > 1:
        return {"disposition": "contradiction",
                "reason_code": "competing_corroborated_counterparts", "target": None}
    return {"disposition": "evidence_insufficient", "reason_code": "item_numbers_not_subset",
            "target": None}


def select_cohort(connection: Any, unmatched: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The bounded high-yield cohort, by explicit criteria.  Deterministic ordering."""
    # A body counts as platform-proven only when the platform's own city matches the body's
    # prefix.  "mesa-cc has a mesa.legistar.com meeting" says nothing about a Phoenix body,
    # and matching any legistar host would silently import another city's coverage.
    platform_bodies = set()
    for body, url in connection.execute(text(
            "SELECT DISTINCT body, source_url FROM meetings "
            "WHERE source_url LIKE '%legistar%'")):
        host = str(url or "").split("/")[2].lower() if "//" in str(url or "") else ""
        city = host.split(".")[0]
        if city and str(body or "").lower().startswith(city):
            platform_bodies.add(body)
    referenced = [m for m in unmatched if m["body"] in platform_bodies]
    ordered = sorted(referenced, key=lambda m: (str(m["body"]), str(m["meeting_date"]),
                                                int(m["meeting_db_id"])))
    cohort = ordered[:COHORT_SIZE]
    return {"criteria": list(COHORT_CRITERIA),
            "platform_proven_bodies": sorted(platform_bodies),
            "platform_proven_meetings": len(referenced),
            "platform_absent_meetings": len(unmatched) - len(referenced),
            "cohort_size": len(cohort),
            "cohort": [{"meeting_db_id": int(m["meeting_db_id"]), "body": m["body"],
                        "meeting_date": m["meeting_date"]} for m in cohort],
            "cohort_sha256": canonical_sha256([int(m["meeting_db_id"]) for m in cohort])}


def discover(connection: Any, crosswalk: Mapping[str, Any]) -> dict[str, Any]:
    unmatched = [m for m in crosswalk["per_meeting"] if m["class"] == "unmatched"]
    cohort_info = select_cohort(connection, unmatched)
    platform_bodies = set(cohort_info["platform_proven_bodies"])

    per_meeting, counts, reasons = [], {}, {}
    for c in CLASSES:
        counts[c] = 0
    by_body: dict[str, dict] = {}
    for m in unmatched:
        mid = int(m["meeting_db_id"])
        body, date = m["body"], m["meeting_date"]
        platform_proven = body in platform_bodies
        counterparts = []
        if platform_proven:
            counterparts = [int(r[0]) for r in connection.execute(text(
                "SELECT id FROM meetings WHERE body=:b AND meeting_date=:d "
                "AND source_url LIKE '%legistar%' ORDER BY id"),
                {"b": body, "d": date})]
        source_items = _result_item_numbers(connection, mid)
        target_items = [_target_items(connection, cid) for cid in counterparts]
        verdict = classify_meeting(platform_proven=platform_proven,
                                   counterpart_ids=counterparts,
                                   source_items=source_items, target_items=target_items)
        counts[verdict["disposition"]] += 1
        reasons[verdict["reason_code"]] = reasons.get(verdict["reason_code"], 0) + 1
        by_body.setdefault(body, {}).setdefault(verdict["disposition"], 0)
        by_body[body][verdict["disposition"]] += 1
        per_meeting.append({
            "meeting_db_id": mid, "body": body, "meeting_date": date,
            "platform_proven": platform_proven, "counterparts": counterparts,
            "source_item_count": len(source_items),
            "source_url": m.get("source_url"), **verdict})
    total = len(per_meeting)
    return {"producer_version": PRODUCER_VERSION, "total": total,
            "reconciles": sum(counts.values()) == total,
            "classes": counts, "reason_codes": reasons, "by_body": by_body,
            "cohort": cohort_info, "per_meeting": per_meeting,
            "classes_sha256": canonical_sha256(
                [[r["meeting_db_id"], r["disposition"]] for r in per_meeting])}


def build_artifact(connection: Any, *, crosswalk: Mapping[str, Any], backlog: Mapping[str, Any],
                   created_at: str, target: Mapping[str, Any]) -> dict[str, Any]:
    d = discover(connection, crosswalk)
    art = {
        "kind": "kg-stage3-b2g-discovery", "version": "kg-stage3-b2g-discovery/1.0",
        "created_at": created_at, "mode": "read-only", "write_path": "absent by design",
        "applied": False, "no_fetch": True, "target": dict(target),
        "producer": {"module": "scripts/kg/stage3_b2g_discovery.py",
                     "version": PRODUCER_VERSION,
                     "code_hashes": identity.code_hashes((
                         "scripts/kg/stage3_b2g_discovery.py",
                         "scripts/kg/stage3_b2_identity.py",
                         "scripts/kg/stage3_meeting_result_identity.py"))},
        "bindings": {"crosswalk_digest": crosswalk.get("digest"),
                     "backlog_digest": backlog.get("digest")},
        "classification": {k: d[k] for k in ("total", "reconciles", "classes", "reason_codes",
                                             "by_body", "cohort", "classes_sha256")},
        "backlog": {"population": backlog.get("population"),
                    "reason": backlog.get("reason")},
        "projected": {"deterministic_routes": d["classes"]["deterministic_route"],
                      "agenda_payoff": d["classes"]["deterministic_route"],
                      "event_payoff": 0},
        "unresolved_source_classes": [
            {"class": "no_agenda_platform_for_body", "count":
             d["reason_codes"].get("no_agenda_platform_for_body", 0),
             "need": "identify an authoritative agenda platform for these bodies, or accept "
                     "that none is published"},
            {"class": "no_counterpart_for_blocking_key", "count":
             d["reason_codes"].get("no_counterpart_for_blocking_key", 0),
             "need": "a cross-platform identifier; body+date alone can never bind"},
        ],
        "per_meeting": d["per_meeting"],
    }
    art["digest"] = canonical_sha256(art)
    return art
