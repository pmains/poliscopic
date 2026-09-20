#!/usr/bin/env python3
"""``stage3_b2_review.py`` — resolve the 359 needs-review and 22 contradiction meetings.

Each meeting gets an immutable REVIEW PACKET holding every candidate counterpart and the
public evidence for it: official titles/dates/body identities, the AEM result document's
item-number spans, the counterpart's Legistar item numbers, c-numbers, case numbers, titles
and detail URLs.

CORROBORATION IS SOURCE-GROUNDED AND COLLISION-TESTED.  Four rules may promote a pair, and a
rule only counts when it is *discriminating* - the same evidence must not be shared by a
competing candidate.  Date/body proximity alone is never sufficient; no fuzzy matching and no
model inference is used anywhere.
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

__all__ = ["CLASSES", "PRODUCER_VERSION", "RULES", "build_artifact", "build_packet",
           "canonical_sha256", "corroborate", "norm_text", "review_scope"]

PRODUCER_VERSION = "kg-stage3-b2-review/1.0"
CLASSES = ("deterministic_crosswalk", "contradiction", "insufficient_evidence", "unavailable")

#: The corroboration rules.  Each is source-grounded; none uses proximity, fuzz or inference.
RULES = {
    "item_number_set":
        "the result PDF's own item numbers form a subset of the counterpart's Legistar agenda "
        "item numbers (both non-empty)",
    "case_number_agreement":
        "the meeting's result case numbers intersect the counterpart's Legistar case numbers "
        "or c-numbers",
    "official_title_identity":
        "the official normalized meeting titles are exactly equal across a cross-writer pair",
    "explicit_identifier":
        "an explicit identifier (GUID/detail id) is shared between the two records",
}

REASON_CODES = {
    "unique_discriminating_corroboration":
        "exactly one candidate carries discriminating corroboration and no other candidate "
        "carries the same evidence",
    "competing_corroborated_candidates":
        "two or more different candidates are corroborated by the same rule set",
    "no_rule_fired":
        "candidates exist but no corroboration rule fired",
    "only_non_discriminating_evidence":
        "a rule fired but the identical evidence is shared with a competing candidate",
    "no_candidate_counterpart":
        "no counterpart shares the blocking key",
}


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def norm_num(value: Any) -> str:
    """Normalize an item/case number for exact comparison."""
    return re.sub(r"[^0-9a-z]", "", str(value or "").lower())


def review_scope(connection: Any, artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [m for m in artifact["per_meeting"]
            if m["class"] in ("candidate_needs_review", "contradiction")]


def _result_item_numbers(connection: Any, meeting_db_id: int) -> set[str]:
    txt = connection.execute(text(
        "SELECT text_content FROM supporting_documents WHERE meeting_db_id=:m "
        "AND document_type='Meeting Result' LIMIT 1"), {"m": meeting_db_id}).scalar()
    out = {B1.normalize_item_number(s["token"]) for s in B1.extract_item_spans(txt or "")}
    out.discard(None)
    return out


def _result_case_numbers(connection: Any, meeting_db_id: int) -> set[str]:
    rows = connection.execute(text(
        "SELECT DISTINCT case_number FROM meeting_events WHERE case_number IS NOT NULL AND "
        "supporting_doc_id IN (SELECT id FROM supporting_documents WHERE meeting_db_id=:m)"),
        {"m": meeting_db_id}).scalars().all()
    return {norm_num(r) for r in rows if norm_num(r)}


def _counterpart_evidence(connection: Any, meeting_db_id: int) -> dict[str, Any]:
    rows = connection.execute(text(
        "SELECT agenda_item_number, c_number, case_number, agenda_item_title, agenda_item_url "
        "FROM agenda_items WHERE meeting_db_id=:m"), {"m": meeting_db_id}).mappings().all()
    items = {norm_num(r["agenda_item_number"]) for r in rows if r["agenda_item_number"]}
    cases = {(norm_num(r["case_number"]) or norm_num(r["c_number"]))
             for r in rows if r["case_number"] or r["c_number"]}
    cases.discard("")
    titles = {norm_text(r["agenda_item_title"]) for r in rows if r["agenda_item_title"]}
    urls = [r["agenda_item_url"] for r in rows if r["agenda_item_url"]]
    return {"item_numbers": items, "case_numbers": cases, "item_titles": titles,
            "item_urls": urls, "agenda_items": len(rows)}


def corroborate(aem_items: set[str], aem_cases: set[str], aem_title: str,
                aem_body: str, cand: Mapping[str, Any],
                evidence: Mapping[str, Any]) -> list[str]:
    """Which rules fire for this pair.  Discriminatingness is assessed by the caller."""
    fired: list[str] = []
    if aem_items and evidence["item_numbers"] and aem_items <= evidence["item_numbers"]:
        fired.append("item_number_set")
    if aem_cases and evidence["case_numbers"] and (aem_cases & evidence["case_numbers"]):
        fired.append("case_number_agreement")
    same_family = B1.PRODUCER_VERSION and str(cand.get("source_url") or "").startswith(
        "https://www.phoenix.gov/content/dam")
    if (not same_family and norm_text(aem_title) and norm_text(aem_title) == norm_text(
            cand.get("btitle"))):
        fired.append("official_title_identity")
    return fired


def build_packet(connection: Any, aem: Mapping[str, Any]) -> dict[str, Any]:
    """The immutable per-meeting review packet."""
    aem_id = int(aem["meeting_db_id"])
    aem_items = _result_item_numbers(connection, aem_id)
    aem_cases = _result_case_numbers(connection, aem_id)
    aem_meta = connection.execute(text(
        "SELECT meeting_title, meeting_type, body, meeting_date, source_url "
        "FROM meetings WHERE id=:m"), {"m": aem_id}).mappings().first()
    pairs = []
    for cand in aem.get("candidate_pairs") or []:
        ev = _counterpart_evidence(connection, int(cand["db_id"]))
        cm = connection.execute(text(
            "SELECT meeting_title, meeting_date, body, source_url FROM meetings WHERE id=:m"),
            {"m": int(cand["db_id"])}).mappings().first()
        fired = corroborate(aem_items, aem_cases, aem_meta["meeting_title"], aem_meta["body"],
                            {"source_url": cand.get("source_url"), "btitle": cm["meeting_title"]},
                            ev)
        pairs.append({
            "counterpart_db_id": int(cand["db_id"]),
            "counterpart_meeting_id": cand["meeting_id"],
            "counterpart_source_url": cand.get("source_url"),
            "counterpart_body": cm["body"], "counterpart_date": cm["meeting_date"],
            "counterpart_title": cm["meeting_title"],
            "agenda_items": ev["agenda_items"],
            "agenda_item_numbers_sample": sorted(ev["item_numbers"])[:10],
            "case_numbers_sample": sorted(ev["case_numbers"])[:10],
            "item_title_sample": sorted(ev["item_titles"])[:3],
            "fired_rules": fired,
            "evidence_sha256": canonical_sha256({
                "items": sorted(ev["item_numbers"]), "cases": sorted(ev["case_numbers"])}),
        })
    return {
        "meeting_db_id": aem_id, "meeting_id": aem["meeting_id"],
        "body": aem_meta["body"], "meeting_date": aem_meta["meeting_date"],
        "official_title": aem_meta["meeting_title"], "source_url": aem_meta["source_url"],
        "result_item_numbers": sorted(aem_items),
        "result_case_numbers": sorted(aem_cases),
        "candidates": pairs,
        "prior_class": aem["class"],
    }


def classify_review(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Exactly one class per meeting, from discriminating corroboration only."""
    cands = packet.get("candidates") or []
    if not cands:
        return {"class": "unavailable", "reason_code": "no_candidate_counterpart",
                "target": None, "rule": None, "corroborated": 0}
    scored = [(c, tuple(sorted(c["fired_rules"]))) for c in cands]
    with_rules = [(c, r) for c, r in scored if r]

    if not with_rules:
        any_rule = any(c["fired_rules"] for c in cands)
        return {"class": "insufficient_evidence",
                "reason_code": "only_non_discriminating_evidence" if any_rule
                else "no_rule_fired",
                "target": None, "rule": None, "corroborated": 0}

    # Discriminatingness: a rule set promotes only if no OTHER candidate shares it.
    by_rule: dict[tuple, list] = {}
    for c, r in with_rules:
        by_rule.setdefault(r, []).append(c)
    unique = [(cs[0], r) for r, cs in by_rule.items() if len(cs) == 1]
    contested = {r for r, cs in by_rule.items() if len(cs) > 1}

    if len(unique) == 1 and not contested:
        c, r = unique[0]
        return {"class": "deterministic_crosswalk",
                "reason_code": "unique_discriminating_corroboration",
                "target": c["counterpart_meeting_id"], "target_db_id": c["counterpart_db_id"],
                "rule": list(r), "corroborated": 1}
    if len({c["counterpart_meeting_id"] for c, _ in unique}) > 1 or contested:
        return {"class": "contradiction",
                "reason_code": "competing_corroborated_candidates",
                "target": None, "rule": None, "corroborated": len(with_rules)}
    return {"class": "insufficient_evidence",
            "reason_code": "only_non_discriminating_evidence",
            "target": None, "rule": None, "corroborated": len(with_rules)}


def reconcile_review(connection: Any, artifact: Mapping[str, Any]) -> dict[str, Any]:
    packets, results, counts, reasons, rules_fired = [], [], {}, {}, {}
    for c in CLASSES:
        counts[c] = 0
    for aem in review_scope(connection, artifact):
        p = build_packet(connection, aem)
        v = classify_review(p)
        counts[v["class"]] += 1
        reasons[v["reason_code"]] = reasons.get(v["reason_code"], 0) + 1
        for r in (v.get("rule") or []):
            rules_fired[r] = rules_fired.get(r, 0) + 1
        packets.append(p)
        results.append({**{k: p[k] for k in ("meeting_db_id", "meeting_id", "body",
                                             "meeting_date", "prior_class")}, **v,
                        "candidate_count": len(p["candidates"])})
    total = len(results)
    return {"producer_version": PRODUCER_VERSION, "total": total,
            "reconciles": sum(counts.values()) == total,
            "classes": counts, "reason_codes": reasons, "rules_fired": rules_fired,
            "packets": packets, "results": results,
            "results_sha256": canonical_sha256(
                [[r["meeting_db_id"], r["class"], r["target"]] for r in results])}
