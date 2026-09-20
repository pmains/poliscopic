#!/usr/bin/env python3
"""``stage3_b2_acquisition.py`` — B2 source-eligibility and acquisition planning.

Scope: the 4,196 meetings that B1 proved hold a Meeting Result document and no canonical
agenda item.  This module reconciles them exactly once, assigns each a mutually exclusive
acquisition disposition with an exact reason code, and builds the dependency-ordered DRY
acquisition plan.

It reads only.  A URL substituted from a pattern is NEVER proof: only a probe-verified
reachable artifact, or a retained artifact, counts as evidence.
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

__all__ = ["DISPOSITIONS", "PLAN_KIND", "PRODUCER_VERSION", "REASON_CODES", "build_baseline",
           "build_plan", "canonical_sha256", "reconcile"]

PRODUCER_VERSION = "kg-stage3-b2-acquisition/1.0"
PLAN_KIND = "kg-stage3-b2-acquisition-plan"
RESULT_TYPE = "Meeting Result"
AGENDA_TYPES = ("Agenda", "Attachment", "Minutes", "Packet")

#: Mutually exclusive acquisition dispositions.  Order is the decision order.
DISPOSITIONS = (
    "existing_unlinked_artifact",
    "reachable_reconstructable",
    "requires_scraper_correction",
    "requires_parser_correction",
    "unavailable_no_public_source",
)

#: Exact reason codes, one per disposition outcome.  Keyed by the CODE, so a caller
#: can assert that the code a classifier emitted is one it is allowed to emit.
REASON_CODES = {
    "artifact_retained_without_item_link":
        "an agenda/packet artifact is retained for this meeting but carries no canonical "
        "item link",
    "probe_verified_reachable":
        "a public source URL is derivable from retained metadata AND a probe-verified "
        "fetch succeeded",
    "agenda_route_not_enumerated_and_no_join_key":
        "the platform publishes agenda artifacts on a route this pipeline never enumerates, "
        "and no deterministic join key connects the two platforms",
    "agenda_route_not_enumerated":
        "the platform publishes agenda artifacts on a route this pipeline never enumerates, "
        "although a deterministic join key exists",
    "retained_text_without_agenda_item_target":
        "an artifact is retained and text-bearing but names no agenda item that exists",
    "no_artifact_and_no_public_route":
        "no artifact is retained and no public route exists",
}

#: Human wording for each disposition.
DISPOSITION_MEANINGS = {
    "existing_unlinked_artifact": "an agenda artifact exists unlinked",
    "reachable_reconstructable": "a source URL is reconstructable and reachable",
    "requires_scraper_correction": "a discovery route must be written",
    "requires_parser_correction": "the parser must be corrected",
    "unavailable_no_public_source": "no public source exists",
}

#: Rate/retry/accounting policy the dry plan declares.  Nothing here executes.
RATE_LIMITS = {"requests_per_second": 1, "burst": 2, "concurrency": 1,
               "per_host_min_interval_ms": 1000, "daily_request_cap": 5000}
RETRY_POLICY = {"max_attempts": 3, "backoff": "exponential",
                "backoff_base_seconds": 5, "backoff_cap_seconds": 120,
                "retry_on": ["429", "500", "502", "503", "504", "timeout"],
                "honor_retry_after": True, "give_up_reason_code": "retry_exhausted"}
RECEIPT_DESIGN = {
    "kind": "kg-stage3-b2-acquisition-receipt",
    "per_run_fields": ["run_id", "cohort_id", "target", "plan_digest", "cohort_digest",
                       "requested", "fetched", "bytes", "content_sha256",
                       "parsed_items", "canonical_targets", "holds", "started_at",
                       "finished_at", "producer_version"],
    "per_artifact_fields": ["url", "http_status", "content_sha256", "content_length",
                            "meeting_db_id", "document_type", "disposition"],
    "immutability": "O_CREAT|O_EXCL, mode 0600, digest recorded",
    "no_write_before_receipt": True,
}

#: Dependency-ordered phases.  Each is a separate governed execution.
PHASES = (
    {"id": "P1", "name": "discovery-map", "depends_on": [],
     "work": "enumerate the platform's agenda route for the in-scope meetings; prove a "
             "deterministic join key exists for each meeting before any fetch",
     "executes": False},
    {"id": "P2", "name": "fetch-cohort", "depends_on": ["P1"],
     "work": "bounded cohort fetch of discovered agenda artifacts under the rate limits",
     "executes": False},
    {"id": "P3", "name": "parse-items", "depends_on": ["P2"],
     "work": "deterministic agenda-item extraction using the B1 identity contract",
     "executes": False},
    {"id": "P4", "name": "link-and-hold", "depends_on": ["P3"],
     "work": "attach canonical items to their meetings; hold the rest with reason codes",
     "executes": False},
)


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def load_probe_evidence() -> dict[str, Any]:
    p = REPO / "tests" / "fixtures" / "b2_probes" / "reachability-20260914.json"
    return json.loads(p.read_text()) if p.exists() else {"results": []}


def classify(meeting: Mapping[str, Any], *, retained_agenda_docs: int,
             reachable_agenda_urls: int, join_keys: int,
             retained_result_docs: int, platform_publishes_agendas: bool) -> dict[str, str]:
    """One disposition per meeting.  Evidence only; a substituted URL is never proof."""
    if retained_agenda_docs > 0:
        return {"disposition": "existing_unlinked_artifact",
                "reason_code": "artifact_retained_without_item_link"}
    if reachable_agenda_urls > 0:
        return {"disposition": "reachable_reconstructable",
                "reason_code": "probe_verified_reachable"}
    if platform_publishes_agendas and join_keys == 0:
        return {"disposition": "requires_scraper_correction",
                "reason_code": "agenda_route_not_enumerated_and_no_join_key"}
    if platform_publishes_agendas and join_keys > 0:
        return {"disposition": "requires_scraper_correction",
                "reason_code": "agenda_route_not_enumerated"}
    if retained_result_docs > 0:
        return {"disposition": "requires_parser_correction",
                "reason_code": "retained_text_without_agenda_item_target"}
    return {"disposition": "unavailable_no_public_source",
            "reason_code": "no_artifact_and_no_public_route"}


def reconcile(connection: Any, *, platform_publishes_agendas: bool = True) -> dict[str, Any]:
    probe = load_probe_evidence()
    reachable_agenda = sum(1 for r in probe.get("results", [])
                           if r.get("family") == "agenda_substitution"
                           and int(r.get("status") or 0) == 200)
    rows = connection.execute(text("""
        SELECT m.id, m.body, m.meeting_id, m.meeting_date,
               substring(m.meeting_date, 1, 4) AS year, m.source_url,
               j.name AS jurisdiction,
               (SELECT COUNT(*) FROM supporting_documents sd
                 WHERE sd.meeting_db_id = m.id AND sd.document_type IN
                       ('Agenda','Attachment','Minutes','Packet')) AS agenda_docs,
               (SELECT COUNT(*) FROM supporting_documents sd
                 WHERE sd.meeting_db_id = m.id AND sd.document_type = :rt) AS result_docs,
               (SELECT COUNT(*) FROM agenda_items a WHERE a.meeting_db_id = m.id) AS items,
               CASE WHEN m.meeting_id ~ '^[0-9]+$' OR m.source_url LIKE '%legistar%'
                    THEN 1 ELSE 0 END AS join_keys
        FROM meetings m
        LEFT JOIN jurisdictions j ON j.id = m.jurisdiction_id
        WHERE EXISTS (SELECT 1 FROM supporting_documents sd
                      WHERE sd.meeting_db_id = m.id AND sd.document_type = :rt)
          AND NOT EXISTS (SELECT 1 FROM agenda_items a WHERE a.meeting_db_id = m.id)
        ORDER BY m.id"""), {"rt": RESULT_TYPE}).mappings().all()

    per_meeting, counts, reason_counts = [], {}, {}
    for d in DISPOSITIONS:
        counts[d] = 0
    by_body: dict[str, dict] = {}
    by_year: dict[str, int] = {}
    for m in rows:
        verdict = classify(dict(m), retained_agenda_docs=int(m["agenda_docs"]),
                           reachable_agenda_urls=reachable_agenda,
                           join_keys=int(m["join_keys"]),
                           retained_result_docs=int(m["result_docs"]),
                           platform_publishes_agendas=platform_publishes_agendas)
        counts[verdict["disposition"]] += 1
        reason_counts[verdict["reason_code"]] = reason_counts.get(verdict["reason_code"], 0) + 1
        by_body.setdefault(m["body"], {}).setdefault(verdict["disposition"], 0)
        by_body[m["body"]][verdict["disposition"]] += 1
        by_year[m["year"]] = by_year.get(m["year"], 0) + 1
        per_meeting.append({"meeting_db_id": int(m["id"]), "body": m["body"],
                            "meeting_id": m["meeting_id"], "year": m["year"],
                            "jurisdiction": m["jurisdiction"],
                            "source_url": m["source_url"], **verdict})
    total = len(per_meeting)
    return {"producer_version": PRODUCER_VERSION, "total": total,
            "reconciles": sum(counts.values()) == total,
            "dispositions": counts, "reason_codes": reason_counts,
            "by_body": by_body, "by_year": by_year,
            "platform": {"publishes_agendas": platform_publishes_agendas,
                         "result_docs_retained": sum(int(m["result_docs"]) for m in rows),
                         "agenda_docs_retained": sum(int(m["agenda_docs"]) for m in rows),
                         "join_keys_found": sum(int(m["join_keys"]) for m in rows),
                         "probe_reachable_agenda_urls": reachable_agenda},
            "per_meeting": per_meeting,
            "dispositions_sha256": canonical_sha256(
                [[r["meeting_db_id"], r["disposition"]] for r in per_meeting]),
            "slices_sha256": canonical_sha256({"body": by_body, "year": by_year})}


def build_baseline(connection: Any) -> dict[str, Any]:
    body = reconcile(connection)
    return {**body, "kind": "kg-stage3-b2-acquisition-baseline",
            "version": "kg-stage3-b2-acquisition-baseline/1.0",
            "mode": "read-only", "write_path": "absent by design", "applied": False}


def build_plan(connection: Any, *, created_at: str, stage2_gate: Mapping[str, Any],
               b1_baseline: Mapping[str, Any]) -> dict[str, Any]:
    base = reconcile(connection)
    if not base["reconciles"]:
        raise ValueError("the acquisition reconciliation does not cover every meeting exactly once")
    cohort = [r["meeting_db_id"] for r in base["per_meeting"]
              if r["disposition"] == "existing_unlinked_artifact"][:50]
    plan = {
        "kind": PLAN_KIND, "version": "kg-stage3-b2-acquisition-plan/1.0",
        "created_at": created_at, "mode": "dry-run", "applied": False,
        "write_path": "absent by design",
        "producer": {"module": "scripts/kg/stage3_b2_acquisition.py",
                     "version": PRODUCER_VERSION},
        "bindings": {
            "stage2_exit_digest": stage2_gate.get("digest"),
            "b1_baseline_digest": b1_baseline.get("digest"),
            "reconciliation": {k: base[k] for k in
                               ("total", "reconciles", "dispositions", "reason_codes",
                                "platform", "dispositions_sha256", "slices_sha256")},
        },
        "phases": [dict(p) for p in PHASES],
        "rate_limits": dict(RATE_LIMITS), "retry_policy": dict(RETRY_POLICY),
        "receipt_design": dict(RECEIPT_DESIGN),
        "first_cohort": {"size": len(cohort), "meeting_db_ids": cohort,
                         "selection": "meetings whose agenda artifact is already retained "
                                      "but unlinked (zero-fetch cohort)",
                         "expected_event_hold_payoff": 0,
                         "payoff_reason": "no meeting in scope holds a retained agenda "
                                          "artifact, so the first cohort is empty until P1 "
                                          "discovers a route"},
        "no_write_path": True,
    }
    plan["digest"] = canonical_sha256(plan)
    return plan
