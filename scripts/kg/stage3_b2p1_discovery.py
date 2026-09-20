#!/usr/bin/env python3
"""``stage3_b2p1_discovery.py`` — B2-P1: find a deterministic mapping from the 4,196
Phoenix Destiny/publicmeetings Meeting Result meetings to their agenda/packet sources.

PROMOTION RULE.  A mapping is promotable ONLY when an authoritative source explicitly binds
the result-meeting identity to an agenda identity or URL, or when a versioned deterministic
key is proven unique with collision analysis AND that key is shown to resolve.

A unique key alone is NOT enough: uniqueness says the key *could* identify one meeting, not
that any source publishes an agenda under it.  Guessed URL substitutions and date/body-only
joins are never promotable.

Read-only: this module performs no fetch and writes nothing.
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

__all__ = ["CLASSES", "PRODUCER_VERSION", "REASON_CODES", "build_artifact",
           "canonical_sha256", "classify", "collision_analysis", "discover",
           "load_evidence", "source_adapters"]

PRODUCER_VERSION = "kg-stage3-b2p1-discovery/1.0"
RESULT_TYPE = "Meeting Result"

#: Mutually exclusive discovery outcomes.
CLASSES = ("deterministic_route", "candidate_route_needs_review", "unavailable",
           "unresolved")

REASON_CODES = {
    "authoritative_binding_proven":
        "an authoritative source explicitly binds this result meeting to an agenda identity "
        "or URL",
    "deterministic_key_proven_and_resolving":
        "a versioned deterministic key is proven unique by collision analysis AND is shown "
        "to resolve to an agenda artifact",
    "platform_candidate_without_identity_binding":
        "a reachable candidate platform exists and the derived key is unique, but no source "
        "binds it to an agenda artifact",
    "only_guessed_substitutions_rejected":
        "the only candidate URLs are substitutions derived from the results path, and every "
        "probe returned 404",
    "no_source_identified":
        "no candidate source was identified at all",
}


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def load_evidence() -> dict[str, Any]:
    p = REPO / "tests" / "fixtures" / "b2_probes" / "discovery-20260914.json"
    return json.loads(p.read_text()) if p.exists() else {"adapters": []}


def source_adapters() -> list[dict[str, Any]]:
    return list(load_evidence().get("adapters") or [])


def authoritative_bindings() -> list[dict[str, Any]]:
    """Adapters that bind a result meeting to an agenda identity/URL.  Evidence only."""
    return [a for a in source_adapters() if a.get("binds_agenda_identity") is True]


def candidate_key(meeting_id: str) -> str:
    """The versioned deterministic key hypothesis: the results id minus its r/R suffix."""
    return str(meeting_id).rstrip("rR").lower()


def collision_analysis(meeting_ids: Sequence[str]) -> dict[str, Any]:
    """Prove (or disprove) that the derived key identifies exactly one meeting."""
    seen: dict[str, list[str]] = {}
    for mid in meeting_ids:
        seen.setdefault(candidate_key(mid), []).append(mid)
    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    return {"candidate_keys": len(seen), "meetings": len(meeting_ids),
            "collisions": len(collisions),
            "collision_examples": sorted(collisions)[:5],
            "unique": bool(meeting_ids) and not collisions,
            "note": "uniqueness is necessary but NOT sufficient: it proves the key could "
                    "name one meeting, not that any source publishes an agenda under it"}


def classify(meeting: Mapping[str, Any], *, binding_count: int,
             key_unique: bool, key_resolving: bool, candidate_platform: bool,
             substitutions_all_404: bool) -> dict[str, str]:
    """One discovery class per meeting, with an exact reason code."""
    if binding_count > 0:
        return {"disposition": "deterministic_route",
                "reason_code": "authoritative_binding_proven"}
    if key_unique and key_resolving:
        return {"disposition": "deterministic_route",
                "reason_code": "deterministic_key_proven_and_resolving"}
    if candidate_platform and key_unique:
        return {"disposition": "candidate_route_needs_review",
                "reason_code": "platform_candidate_without_identity_binding"}
    if substitutions_all_404:
        return {"disposition": "candidate_route_needs_review",
                "reason_code": "only_guessed_substitutions_rejected"}
    return {"disposition": "unresolved", "reason_code": "no_source_identified"}


def discover(connection: Any) -> dict[str, Any]:
    rows = connection.execute(text("""
        SELECT m.id, m.body, m.meeting_id, substring(m.meeting_date,1,4) AS year,
               m.source_url, j.name AS jurisdiction
        FROM meetings m
        LEFT JOIN jurisdictions j ON j.id = m.jurisdiction_id
        WHERE EXISTS (SELECT 1 FROM supporting_documents sd
                      WHERE sd.meeting_db_id = m.id AND sd.document_type = :rt)
          AND NOT EXISTS (SELECT 1 FROM agenda_items a WHERE a.meeting_db_id = m.id)
        ORDER BY m.id"""), {"rt": RESULT_TYPE}).mappings().all()

    collisions = collision_analysis([m["meeting_id"] for m in rows])
    evidence = load_evidence()
    bindings = authoritative_bindings()
    adapters = source_adapters()
    candidate_platform = any("destinyhosted" in str(a.get("source", "")) for a in adapters)
    subs = [p for a in adapters if a.get("id", "").startswith("A2")
            for p in (a.get("probe") or [])]
    substitutions_all_404 = bool(subs) and all(int(p.get("status") or 0) != 200 for p in subs)

    per_meeting, counts, reasons = [], {}, {}
    for c in CLASSES:
        counts[c] = 0
    by_body, by_year = {}, {}
    for m in rows:
        verdict = classify(dict(m), binding_count=len(bindings),
                           key_unique=collisions["unique"], key_resolving=False,
                           candidate_platform=candidate_platform,
                           substitutions_all_404=substitutions_all_404)
        counts[verdict["disposition"]] += 1
        reasons[verdict["reason_code"]] = reasons.get(verdict["reason_code"], 0) + 1
        by_body.setdefault(m["body"], {}).setdefault(verdict["disposition"], 0)
        by_body[m["body"]][verdict["disposition"]] += 1
        by_year[m["year"]] = by_year.get(m["year"], 0) + 1
        per_meeting.append({"meeting_db_id": int(m["id"]), "body": m["body"],
                            "meeting_id": m["meeting_id"], "year": m["year"],
                            "jurisdiction": m["jurisdiction"],
                            "candidate_key": candidate_key(m["meeting_id"]),
                            "source_url": m["source_url"], **verdict})
    total = len(per_meeting)
    return {"producer_version": PRODUCER_VERSION, "total": total,
            "reconciles": sum(counts.values()) == total,
            "classes": counts, "reason_codes": reasons, "collisions": collisions,
            "by_body": by_body, "by_year": by_year,
            "adapters": adapters, "authoritative_bindings": len(bindings),
            "substitutions_all_404": substitutions_all_404,
            "per_meeting": per_meeting,
            "classes_sha256": canonical_sha256(
                [[r["meeting_db_id"], r["disposition"]] for r in per_meeting]),
            "conclusion": evidence.get("conclusion")}


def build_artifact(connection: Any, *, created_at: str, target: Mapping[str, Any],
                   b2_baseline_digest: str, code_hash: str) -> dict[str, Any]:
    d = discover(connection)
    cohort = [r["meeting_db_id"] for r in d["per_meeting"]
              if r["disposition"] == "deterministic_route"]
    art = {
        "kind": "kg-stage3-b2p1-discovery",
        "version": "kg-stage3-b2p1-discovery/1.0",
        "created_at": created_at, "mode": "read-only",
        "write_path": "absent by design", "applied": False, "no_fetch": True,
        "target": dict(target),
        "producer": {"module": "scripts/kg/stage3_b2p1_discovery.py",
                     "version": PRODUCER_VERSION, "code_sha256": code_hash},
        "bindings": {"b2_baseline_digest": b2_baseline_digest},
        "classification": {k: d[k] for k in ("total", "reconciles", "classes",
                                             "reason_codes", "collisions", "by_year",
                                             "classes_sha256", "conclusion")},
        "adapters": d["adapters"],
        "authoritative_bindings": d["authoritative_bindings"],
        "first_cohort": {
            "size": len(cohort), "meeting_db_ids": cohort,
            "hashes": {str(m["meeting_db_id"]): canonical_sha256(
                {"meeting_id": m["meeting_id"], "candidate_key": m["candidate_key"]})
                for m in d["per_meeting"] if m["disposition"] == "deterministic_route"},
            "rate_limits": {"requests_per_second": 1, "concurrency": 1,
                            "per_host_min_interval_ms": 1000},
            "no_write_path": True,
            "expected_b2_payoff": 0,
            "payoff_reason": "no deterministic route is proven, so there is no cohort to fetch",
        },
        "per_meeting": d["per_meeting"],
    }
    art["digest"] = canonical_sha256(art)
    return art
