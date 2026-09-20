#!/usr/bin/env python3
"""``stage3_b2_crosswalk.py`` — two Phoenix ingestion conventions, and the crosswalk.

CONVENTION A (AEM/publicmeetings-results).  Written to ``meetings`` with
``meeting_id = publicmeetings-results-{year}-{month}-{id}r``, ``body = phoenix-*``,
``source_url = www.phoenix.gov/.../publicmeetings/results/{yyyy}/{month}/{id}.pdf``, one
``Meeting Result`` document, ``agenda_item_number = '0'``, and **no agenda items at all**.

CONVENTION B (Legistar).  Written with a numeric ``meeting_id`` plus a GUID inside
``MeetingDetail.aspx?ID=…&GUID=…``, ``source_url = phoenix.legistar.com``, documents typed
``Agenda``/``Attachment``, and real agenda items.

BLOCKING KEYS ARE NOT PROOF.  Normalized body + date (+ time/title) only *nominate* a pair.
A pair is promoted to a canonical crosswalk only when corroborated beyond date/body by
**content-level evidence**: the result PDF's own item numbers aligning with the counterpart's
agenda-item numbers, or an exact normalized official meeting-title match.  Every candidate is
collision-analysed; a meeting with several counterparts is never silently resolved.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage3_meeting_result_identity as B1  # noqa: E402

__all__ = ["CLASSES", "PRODUCER_VERSION", "REASON_CODES", "build_artifact",
           "canonical_sha256", "classify_meeting", "norm_text", "reconcile"]

PRODUCER_VERSION = "kg-stage3-b2-crosswalk/1.0"
AEM_PREFIX = "publicmeetings-results-"
NOTICES_PREFIX = "publicmeetings-notices-"
AEM_NAMESPACE_RE = "^publicmeetings-(results|notices)-"
AEM_WRITER = "scripts/sync/extract_results_pdfs.py"
CLASSES = ("deterministic_crosswalk", "candidate_needs_review", "contradiction", "unmatched")

REASON_CODES = {
    "single_counterpart_with_item_number_corroboration":
        "exactly one counterpart and the result PDF's item numbers align with its agenda "
        "items",
    "single_counterpart_with_exact_title_corroboration":
        "exactly one counterpart and the official normalized meeting titles match exactly",
    "counterpart_present_without_corroboration":
        "a blocking-key counterpart exists but nothing beyond date/body corroborates it",
    "ambiguous_multiple_counterparts":
        "several counterparts share the blocking key, so the pair is not determined",
    "conflicting_corroboration":
        "several counterparts are corroborated and they disagree",
    "no_counterpart":
        "no counterpart shares the blocking key",
}


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def aem_namespace(meeting_id: str) -> bool:
    return str(meeting_id).startswith(AEM_PREFIX)


def classify_meeting(candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One crosswalk class per AEM meeting, with an exact reason code."""
    if not candidates:
        return {"class": "unmatched", "reason_code": "no_counterpart",
                "target": None, "candidates": 0, "corroborated": 0}
    corroborated = [c for c in candidates if c.get("corroboration")]
    if len(corroborated) > 1:
        targets = {c["meeting_id"] for c in corroborated}
        if len(targets) > 1:
            return {"class": "contradiction", "reason_code": "conflicting_corroboration",
                    "target": None, "candidates": len(candidates),
                    "corroborated": len(corroborated)}
    if len(corroborated) == 1 and len(candidates) == 1:
        c = corroborated[0]
        reason = ("single_counterpart_with_item_number_corroboration"
                  if c["corroboration"] == "item_numbers_align"
                  else "single_counterpart_with_exact_title_corroboration")
        return {"class": "deterministic_crosswalk", "reason_code": reason,
                "target": c["meeting_id"], "target_db_id": c["db_id"],
                "corroboration": c["corroboration"],
                "candidates": 1, "corroborated": 1}
    if len(candidates) > 1:
        return {"class": "candidate_needs_review",
                "reason_code": "ambiguous_multiple_counterparts",
                "target": None, "candidates": len(candidates),
                "corroborated": len(corroborated)}
    return {"class": "candidate_needs_review",
            "reason_code": "counterpart_present_without_corroboration",
            "target": None, "candidates": len(candidates), "corroborated": 0}


def _item_numbers(connection: Any, meeting_db_id: int) -> set[str]:
    txt = connection.execute(text(
        "SELECT text_content FROM supporting_documents WHERE meeting_db_id=:m "
        "AND document_type='Meeting Result' LIMIT 1"), {"m": meeting_db_id}).scalar()
    nums = {B1.normalize_item_number(s["token"]) for s in B1.extract_item_spans(txt or "")}
    nums.discard(None)
    return nums


def reconcile(connection: Any) -> dict[str, Any]:
    scope = connection.execute(text("""
        SELECT m.id, m.body, m.meeting_id, m.meeting_date, m.meeting_title
        FROM meetings m
        WHERE m.meeting_id ~ :p
          AND EXISTS (SELECT 1 FROM supporting_documents sd WHERE sd.meeting_db_id = m.id
                      AND sd.document_type = 'Meeting Result')
          AND NOT EXISTS (SELECT 1 FROM agenda_items a WHERE a.meeting_db_id = m.id)
        ORDER BY m.id"""), {"p": AEM_NAMESPACE_RE}).mappings().all()
    rows = connection.execute(text("""
        SELECT a.id AS aid, a.meeting_id AS amid, a.body, a.meeting_date, a.meeting_title,
               b.id AS bid, b.meeting_id AS bmid, b.source_url AS burl,
               b.meeting_title AS btitle,
               (SELECT COUNT(*) FROM agenda_items x WHERE x.meeting_db_id = b.id) AS b_items
        FROM meetings a
        JOIN meetings b ON b.id <> a.id AND b.body = a.body AND b.meeting_date = a.meeting_date
        WHERE a.meeting_id ~ :p AND b.meeting_id !~ :p
        ORDER BY a.id"""), {"p": AEM_NAMESPACE_RE}).mappings().all()
    by_aem: dict[int, list] = {}
    for r in rows:
        by_aem.setdefault(int(r["aid"]), []).append(dict(r))

    per_meeting, counts, reasons = [], {}, {}
    for c in CLASSES:
        counts[c] = 0
    corroboration_kinds: dict[str, int] = {}
    for m in scope:
        cands = []
        for r in by_aem.get(int(m["id"]), []):
            kind = None
            if int(r["b_items"]) > 0:
                mine = _item_numbers(connection, int(m["id"]))
                theirs = {norm_text(x[0]) for x in connection.execute(text(
                    "SELECT agenda_item_number FROM agenda_items WHERE meeting_db_id=:m"),
                    {"m": int(r["bid"])})}
                if mine and theirs and (mine <= theirs or theirs <= mine):
                    kind = "item_numbers_align"
            if kind is None and norm_text(m["meeting_title"]) and \
                    norm_text(m["meeting_title"]) == norm_text(r["btitle"]):
                kind = "exact_title_match"
            # A None kind is "no corroboration"; keep the counter key a string so the
            # artifact stays sortable and comparable.
            label = kind or "no_corroboration"
            corroboration_kinds[label] = corroboration_kinds.get(label, 0) + 1
            cands.append({"db_id": int(r["bid"]), "meeting_id": r["bmid"],
                          "source_url": r["burl"], "agenda_items": int(r["b_items"]),
                          "corroboration": kind})
        verdict = classify_meeting(cands)
        counts[verdict["class"]] += 1
        reasons[verdict["reason_code"]] = reasons.get(verdict["reason_code"], 0) + 1
        # NOTE: verdict carries a *count* named "candidates"; the pair list is kept
        # under candidate_pairs so the two can never clobber each other.
        per_meeting.append({"meeting_db_id": int(m["id"]), "body": m["body"],
                            "meeting_id": m["meeting_id"], "meeting_date": m["meeting_date"],
                            "candidate_pairs": cands, **verdict})
    total = len(per_meeting)
    multi = sum(1 for r in per_meeting if int(r["candidates"]) > 1)
    return {"producer_version": PRODUCER_VERSION, "total": total,
            "reconciles": sum(counts.values()) == total,
            "classes": counts, "reason_codes": reasons,
            "corroboration_kinds": corroboration_kinds,
            "candidates_total": sum(len(r["candidate_pairs"]) for r in per_meeting),
            "meetings_with_multiple_candidates": multi,
            "writers": {"aem": AEM_WRITER,
                        "legistar": "scripts/scraper/platforms/ (Legistar)",
                        "aem_namespace_count": len(scope)},
            "per_meeting": per_meeting,
            "classes_sha256": canonical_sha256(
                [[r["meeting_db_id"], r["class"], r["target"]] for r in per_meeting])}


def build_artifact(connection: Any, *, created_at: str, target: Mapping[str, Any],
                   b2p1_digest: str, code_hash: str) -> dict[str, Any]:
    d = reconcile(connection)
    cohort = [{"meeting_db_id": r["meeting_db_id"], "target_db_id": r["target_db_id"],
               "target_meeting_id": r["target"], "corroboration": r["corroboration"]}
              for r in d["per_meeting"] if r["class"] == "deterministic_crosswalk"]
    art = {
        "kind": "kg-stage3-b2-crosswalk-identity-contract",
        "version": "kg-stage3-b2-crosswalk/1.0", "created_at": created_at,
        "mode": "read-only", "write_path": "absent by design", "applied": False,
        "no_merge": True, "no_fetch": True, "target": dict(target),
        "producer": {"module": "scripts/kg/stage3_b2_crosswalk.py",
                     "version": PRODUCER_VERSION, "code_sha256": code_hash},
        "bindings": {"b2p1_discovery_digest": b2p1_digest},
        "identity_contract": {
            "aem": {"namespace": AEM_PREFIX, "writer": AEM_WRITER,
                    "keys": ["meeting_id", "body", "meeting_date", "source_url",
                             "document_url"],
                    "agenda_items": 0, "document_type": "Meeting Result"},
            "legistar": {"namespace": "numeric meeting_id + GUID",
                         "keys": ["meeting_id", "GUID", "source_url", "document_url"],
                         "document_types": ["Agenda", "Attachment"]},
            "promotion_rule": "a pair is canonical only with corroboration beyond "
                              "date/body: item-number alignment or exact normalized title",
            "non_promotable": ["date/body/time similarity alone", "guessed URL substitutions",
                               "document order", "fuzzy title similarity", "AI inference"],
        },
        "classification": {k: d[k] for k in
                           ("total", "reconciles", "classes", "reason_codes",
                            "corroboration_kinds", "candidates_total",
                            "meetings_with_multiple_candidates", "writers",
                            "classes_sha256")},
        "first_crosswalk_cohort": {
            "size": len(cohort), "pairs": cohort, "no_write_path": True,
            "rate_limits": {"requests_per_second": 1, "concurrency": 1},
            "projected_agenda_payoff": len(cohort),
            "projected_event_payoff": 0,
            "payoff_note": "a proven crosswalk supplies the agenda side for these meetings, "
                           "but event resolution also needs the B1 parse to resolve item "
                           "numbers, which remains 0 until the agenda items exist",
        },
        "per_meeting": d["per_meeting"],
    }
    art["digest"] = canonical_sha256(art)
    return art
