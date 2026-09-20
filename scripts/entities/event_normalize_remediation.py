#!/usr/bin/env python3
"""``event_normalize_remediation.py`` — live remediation state for the gate.

The gate's launch decision needs to know whether the approved civic-chain
remediation has actually been applied.  That used to be a hardcoded string, which
went stale the moment the repair committed: the gate kept refusing for work that was
already done.

This module answers the question from **observed state** instead:

* the approved body remediation is *recognised* when it is genuinely committed —
  the three canonical bodies exist and their adjudicated meetings are parented;
* the **sentinel quarantine** is recognised by a *semantic invariant* over the
  surviving rows, not by a row count.  A count goes stale the moment a later,
  approved operation (for example the joint dedup) legitimately retires duplicate
  sentinel rows; the invariant does not, because duplicates are recognised through
  the committed dedup evidence rather than counted as missing;
* **approved scope decision** — unparented meetings block only when they take part
  in the producer's *eligible, non-quarantined* evidence chain.  Extraction-less
  meetings are a separate **parentage backlog**: reported, never a launch blocker;
* refusal is **not** weakened: unexpected extra sentinel rows, wrong metadata,
  broken lineage, and any quarantine leakage into the eligible population all block,
  fail-closed;
* every number is read live and returned alongside the blocker text, so the decision
  is auditable rather than asserted.

Read-only: the caller supplies an engine that is already guarded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import bindparam, text

from scripts.kg.quarantine import quarantine_selection_clause
from scripts.kg.stage1_adjudication import ADJUDICATION

__all__ = [
    "APPROVED_BODY_CODES",
    "ADJUDICATED_SENTINEL_IDS",
    "EXPECTED_BODIES",
    "EXPECTED_MEETINGS",
    "SENTINEL_REASON",
    "committed_dedup_evidence",
    "parentage_backlog",
    "remediation_detail",
    "remediation_state",
    "sentinel_invariant",
]

#: The body codes the approved adjudication registers.
APPROVED_BODY_CODES = ("phoenix-dab", "phoenix-dr", "phoenix-ds")

#: The adjudicated sentinel identity set — the single approved source of truth.
ADJUDICATED_SENTINEL_IDS = frozenset(int(i) for i in ADJUDICATION["quarantine_extraction_ids"])

#: The approved quarantine reason and lineage for those rows.
SENTINEL_REASON = str(ADJUDICATION["quarantine_reason"])
SENTINEL_ADJUDICATOR = str(ADJUDICATION["adjudicator"])
SENTINEL_DECISION_ID = str(ADJUDICATION["decision_id"])
SENTINEL_DOCUMENT_ID = int(ADJUDICATION["document_id"])

#: A meeting participates in the eligible chain when a NON-quarantined extraction is
#: reachable through extraction -> meeting_event -> supporting_document -> meeting.
_ELIGIBLE_UNPARENTED_SQL = """
SELECT COUNT(*) FROM meetings m
 WHERE m.public_body_id IS NULL
   AND EXISTS (SELECT 1 FROM meeting_event_extractions x
                 JOIN meeting_events e ON e.id = x.meeting_event_id
                 JOIN supporting_documents d ON d.id = e.supporting_doc_id
                WHERE d.meeting_db_id = m.id
                  AND x.quarantined_at IS NULL)
"""

#: Everything else that is unparented: extraction-less, or only quarantined rows.
_BACKLOG_UNPARENTED_SQL = """
SELECT COUNT(*) FROM meetings m
 WHERE m.public_body_id IS NULL
   AND NOT EXISTS (SELECT 1 FROM meeting_event_extractions x
                     JOIN meeting_events e ON e.id = x.meeting_event_id
                     JOIN supporting_documents d ON d.id = e.supporting_doc_id
                    WHERE d.meeting_db_id = m.id
                      AND x.quarantined_at IS NULL)
"""

EXPECTED_BODIES = 3
EXPECTED_MEETINGS = 55

DEFAULT_BASE = Path(__file__).resolve().parents[2] / "data"


def _scalar(connection, sql: str, **params: Any) -> int:
    return int(connection.execute(text(sql), params).scalar() or 0)


def committed_dedup_evidence(base: Path | str | None = None) -> dict[str, Any]:
    """Retired sentinel duplicates, recognised from the committed apply evidence.

    A duplicate sentinel row that an approved, committed operation retired is **not**
    missing: it is accounted for here.  The evidence is the immutable apply receipt
    plus the rollback artifact belonging to the very plan that receipt committed.

    Fails closed: an applied receipt whose rollback evidence cannot be read is
    reported as incomplete rather than treated as "nothing was retired".
    """
    root = Path(base) if base is not None else DEFAULT_BASE
    applied: list[tuple[Path, dict]] = []
    for path in sorted(root.glob("kg-stage1-apply-receipt-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("status") == "applied":
            applied.append((path, data))
    if not applied:
        return {"applied": False, "plan_path": None, "retired_extraction_ids": frozenset(),
                "sources": [], "complete": True}

    receipt_path, receipt = applied[-1]
    plan_path = Path(str(receipt.get("plan_path") or ""))
    rollback_path = Path(str(plan_path).replace("-plan-", "-rollback-"))
    sources = [str(receipt_path)]
    if not rollback_path.is_file():
        return {"applied": True, "plan_path": str(plan_path),
                "retired_extraction_ids": frozenset(), "sources": sources,
                "complete": False,
                "incomplete_reason": f"rollback evidence missing: {rollback_path.name}"}
    rollback = json.loads(rollback_path.read_text())
    retired = {int(row["id"]) for row in rollback.get("extractions", []) if "id" in row}
    sources.append(str(rollback_path))
    return {
        "applied": True,
        "plan_path": str(plan_path),
        "plan_sha256": (hashlib.sha256(plan_path.read_bytes()).hexdigest()
                        if plan_path.is_file() else None),
        "retired_count": len(retired),
        "retired_extraction_ids": frozenset(retired),
        "sources": sources,
        "complete": True,
    }


def sentinel_invariant(engine: Any, *, base: Path | str | None = None) -> dict[str, Any]:
    """The fingerprinted semantic invariant over the surviving sentinel rows.

    The adjudicated identity set is fixed.  Which of those rows are *expected to
    survive* is derived by removing the duplicates the committed apply evidence
    retired.  Every expected survivor must then be present, quarantined, and carry
    the approved metadata and lineage.  Anything else blocks.
    """
    evidence = committed_dedup_evidence(base)
    retired = ADJUDICATED_SENTINEL_IDS & set(evidence["retired_extraction_ids"])
    expected = sorted(ADJUDICATED_SENTINEL_IDS - retired)
    blockers: list[str] = []
    observed: list[dict[str, Any]] = []

    if not evidence["complete"]:
        blockers.append(f"dedup evidence incomplete: {evidence.get('incomplete_reason')}")

    with engine.connect() as connection:
        if expected:
            rows = connection.execute(
                text("SELECT id, quarantined_at, quarantine_reason, quarantined_by, "
                     "decision_id, model_version, supporting_doc_id "
                     "FROM meeting_event_extractions WHERE id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": expected},
            ).mappings().all()
        else:
            rows = []
        observed = [dict(row) for row in rows]

        extras = connection.execute(
            text("SELECT id FROM meeting_event_extractions "
                 "WHERE quarantine_reason = :reason AND id NOT IN :ids ORDER BY id")
            .bindparams(bindparam("ids", expanding=True)),
            {"reason": SENTINEL_REASON, "ids": sorted(ADJUDICATED_SENTINEL_IDS) or [0]},
        ).scalars().all()

    seen = {int(row["id"]) for row in observed}

    missing = sorted(set(expected) - seen)
    if missing:
        blockers.append(
            f"{len(missing)} expected sentinel survivor(s) absent and not accounted "
            f"for by committed dedup evidence: {missing[:8]}"
        )

    unquarantined = [int(r["id"]) for r in observed if r["quarantined_at"] is None]
    if unquarantined:
        blockers.append(
            f"{len(unquarantined)} adjudicated sentinel row(s) are not quarantined: "
            f"{unquarantined[:8]}"
        )

    wrong_reason = [int(r["id"]) for r in observed
                    if str(r["quarantine_reason"]) != SENTINEL_REASON]
    if wrong_reason:
        blockers.append(f"{len(wrong_reason)} sentinel row(s) carry the wrong reason")

    wrong_adjudicator = [int(r["id"]) for r in observed
                         if str(r["quarantined_by"]) != SENTINEL_ADJUDICATOR]
    if wrong_adjudicator:
        blockers.append(
            f"{len(wrong_adjudicator)} sentinel row(s) name the wrong adjudicator"
        )

    wrong_decision = [int(r["id"]) for r in observed
                      if str(r["decision_id"]) != SENTINEL_DECISION_ID]
    if wrong_decision:
        blockers.append(f"{len(wrong_decision)} sentinel row(s) cite the wrong decision")

    missing_model = [int(r["id"]) for r in observed
                     if not str(r["model_version"] or "").strip()]
    if missing_model:
        blockers.append(f"{len(missing_model)} sentinel row(s) lack a model version")

    broken_lineage = [int(r["id"]) for r in observed
                      if r["supporting_doc_id"] is None
                      or int(r["supporting_doc_id"]) != SENTINEL_DOCUMENT_ID]
    if broken_lineage:
        blockers.append(
            f"{len(broken_lineage)} sentinel row(s) do not carry the adjudicated "
            f"document lineage ({SENTINEL_DOCUMENT_ID})"
        )

    if extras:
        blockers.append(
            f"{len(extras)} unexpected extra row(s) carry the sentinel reason outside "
            f"the adjudicated identity set: {[int(i) for i in extras][:8]}"
        )

    # Quarantine leakage: the eligible contract must select exactly the
    # non-quarantined rows.  Reuses the authoritative selection clause and the
    # authoritative population reader rather than restating eligibility.
    with engine.connect() as connection:
        total = _scalar(connection, "SELECT COUNT(*) FROM meeting_event_extractions")
        non_quarantined = _scalar(
            connection,
            "SELECT COUNT(*) FROM meeting_event_extractions e WHERE "
            + quarantine_selection_clause("e"),
        )
    from scripts.entities.event_normalize_preflight import collect_population

    population = collect_population(engine, page_size=512, force=True)
    eligible = int(population["examined"])
    if eligible != non_quarantined:
        blockers.append(
            f"quarantine leakage: eligible population {eligible} does not equal the "
            f"{non_quarantined} non-quarantined extractions"
        )

    fingerprint_body = {
        "adjudicated": sorted(ADJUDICATED_SENTINEL_IDS),
        "retired": sorted(retired),
        "expected": expected,
        "observed": sorted(
            (int(r["id"]), str(r["quarantine_reason"]), str(r["quarantined_by"]),
             str(r["decision_id"]), str(r["model_version"]),
             int(r["supporting_doc_id"]) if r["supporting_doc_id"] is not None else None,
             str(r["quarantined_at"])) for r in observed),
        "extras": sorted(int(i) for i in extras),
        "eligible_population": eligible,
        "non_quarantined": non_quarantined,
        "total_extractions": total,
    }
    return {
        "adjudicated_count": len(ADJUDICATED_SENTINEL_IDS),
        "retired_count": len(retired),
        "retired_ids": sorted(retired),
        "expected_survivors": expected,
        "observed_survivors": len(observed),
        "unexpected_extras": sorted(int(i) for i in extras),
        "eligible_population": eligible,
        "non_quarantined": non_quarantined,
        "total_extractions": total,
        "dedup_evidence_applied": evidence["applied"],
        "dedup_evidence_sources": evidence["sources"],
        "fingerprint": hashlib.sha256(
            json.dumps(fingerprint_body, sort_keys=True).encode("utf-8")).hexdigest(),
        "blockers": blockers,
    }


def remediation_state(engine: Any, *, base: Path | str | None = None) -> str | None:
    """The live remediation blocker, or ``None`` when nothing remains.

    The approved remediation is recognised from committed state; anything still
    undispositioned is reported exactly and continues to block launch.
    """
    counts = observe(engine)
    blockers: list[str] = []

    if counts["approved_bodies"] < EXPECTED_BODIES:
        blockers.append(
            f"approved body remediation incomplete: {counts['approved_bodies']}/"
            f"{EXPECTED_BODIES} canonical bodies registered"
        )
    if counts["parented_meetings"] < EXPECTED_MEETINGS:
        blockers.append(
            f"approved body remediation incomplete: {counts['parented_meetings']}/"
            f"{EXPECTED_MEETINGS} adjudicated meetings parented"
        )
    if counts["eligible_unparented_meetings"]:
        # Fail closed: these meetings are inside the producer's evidence chain.
        blockers.append(
            f"{counts['eligible_unparented_meetings']} unparented meetings participate "
            "in the eligible, non-quarantined evidence chain"
        )
    blockers.extend(sentinel_invariant(engine, base=base)["blockers"])
    return "; ".join(blockers) if blockers else None


def observe(engine: Any) -> dict[str, int]:
    """Observe the remediation facts.  Read-only; raises if the schema is absent."""
    with engine.connect() as connection:
        bodies = int(connection.execute(
            text("SELECT COUNT(*) FROM public_bodies WHERE body_code IN :codes")
            .bindparams(bindparam("codes", expanding=True)),
            {"codes": list(APPROVED_BODY_CODES)},
        ).scalar() or 0)
        parented = int(connection.execute(
            text(
                "SELECT COUNT(*) FROM meetings WHERE public_body_id IN "
                "(SELECT id FROM public_bodies WHERE body_code IN :codes)"
            ).bindparams(bindparam("codes", expanding=True)),
            {"codes": list(APPROVED_BODY_CODES)},
        ).scalar() or 0)
        quarantined = _scalar(
            connection,
            "SELECT COUNT(*) FROM meeting_event_extractions "
            "WHERE quarantined_at IS NOT NULL",
        )
        unparented = _scalar(
            connection, "SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL"
        )
        total = _scalar(connection, "SELECT COUNT(*) FROM meetings")
        eligible_unparented = _scalar(connection, _ELIGIBLE_UNPARENTED_SQL)
        backlog_unparented = _scalar(connection, _BACKLOG_UNPARENTED_SQL)
    return {
        "approved_bodies": bodies,
        "parented_meetings": parented,
        "quarantined_extractions": quarantined,
        "unparented_meetings": unparented,
        "eligible_unparented_meetings": eligible_unparented,
        "backlog_unparented_meetings": backlog_unparented,
        "total_meetings": total,
    }


def parentage_backlog(engine: Any) -> dict[str, Any]:
    """The extraction-less unparented population: tracked, never a launch blocker."""
    counts = observe(engine)
    return {
        "meetings": counts["backlog_unparented_meetings"],
        "scope": "outside the event_normalize eligible evidence chain",
        "blocking": False,
    }


def remediation_detail(engine: Any, *, base: Path | str | None = None) -> Mapping[str, Any]:
    """Observed counts plus the derived blocker, for evidence and fingerprints."""
    counts = observe(engine)
    return {
        "counts": counts,
        "sentinel_invariant": sentinel_invariant(engine, base=base),
        "blocker": remediation_state(engine, base=base),
        "parentage_backlog": parentage_backlog(engine),
    }
