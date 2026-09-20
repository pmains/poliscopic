#!/usr/bin/env python3
"""``stage2_s2_plan_binding.py`` — the authority every Stage 2 plan binds to.

A plan is only as trustworthy as the things it names.  A plan that records a row
count but not *which* rows, or a proposal but not *which* proposal, cannot be
reviewed: two different populations of the same size would be indistinguishable.

This module produces the binding block both plans embed:

* the current S2 **plan head** and **aggregate head** digests;
* the **five human decisions**, each with its decision id, digest, adjudicator,
  time and the relationship it establishes to the proposal;
* the **authoritative hold population** — its count, its sha256 and its exact
  document ids, so accounting can assert set equality rather than cardinality;
* the **target baseline**;
* the **code hashes**, which include each plan's own builder and validator so a
  plan cannot be validated by code other than the code that built it.

Everything here is read-only and produces plain data.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

__all__ = [
    "CANDIDATE_FIELDS",
    "EXECUTION_MODULES",
    "missing_code_modules",
    "assert_decisions_equal",
    "backup_binding",
    "baseline_binding",
    "CODE_MODULES",
    "canonical_sha256",
    "code_hashes",
    "decision_bindings",
    "hold_population",
    "lineage_binding",
    "target_baseline",
]

#: Everything a Stage 2 plan's behaviour depends on, including the plan builders
#: and validators themselves: a plan must not be buildable by one revision of the
#: code and validatable by another.
#:
#: This is the **complete transitive execution set**, not only the planning half.
#: A plan whose manifest bound the builders but not the admission, the transaction
#: owner, the target check, the live-state capture, the collision proof, the receipt,
#: the rollback or the write body could be executed by code the plan never named —
#: the plan would describe the work and the hashes would describe something else.
CODE_MODULES = (
    # planning and evidence
    "scripts/kg/stage2_s2_plan_binding.py",
    "scripts/kg/stage2_s2_repair_plan.py",
    "scripts/kg/stage2_s2_label_correction.py",
    "scripts/kg/stage2_s2_row_derivation.py",
    "scripts/kg/stage2_s2_evidence_materialize.py",
    "scripts/kg/stage2_s2_gap_reconcile.py",
    "scripts/kg/stage2_s2_classify.py",
    "scripts/kg/stage2_s2_ai_lineage.py",
    "scripts/kg/stage2_s2_human_decision.py",
    "scripts/kg/stage2_s2_ai_proposal.py",
    "scripts/kg/stage2_artifacts.py",
    # execution: admission and its transaction
    "scripts/kg/stage2_s2_apply_runner.py",
    "scripts/kg/stage2_s2_admission_binding.py",
    "scripts/kg/stage2_s2_admission_tx.py",
    "scripts/kg/stage2_s2_apply_checks.py",
    # execution: target, state, collision and the reservation contract
    "scripts/kg/stage2_s2_apply_target.py",
    "scripts/kg/stage2_s2_reservation_binding.py",
    "scripts/kg/stage2_reservation.py",
    "scripts/kg/stage2_s2_current_state.py",
    "scripts/kg/stage2_s2_collision.py",
    # the link-column schema change: the contract, its plan and its apply
    "scripts/kg/stage2_link_column.py",
    "scripts/kg/stage2_link_column_plan.py",
    "scripts/kg/stage2_link_column_apply.py",
    # execution: receipt, rollback, the write body and the one public write entry
    "scripts/kg/stage2_s2_receipt.py",
    "scripts/kg/stage2_s2_receipt_rollback.py",
    "scripts/kg/stage2_s2_write_body.py",
    "scripts/kg/stage2_s2_execute.py",
)

#: The subset of ``CODE_MODULES`` that can WRITE.  Bound separately so a reader can
#: see at a glance which modules an apply would actually execute, and asserted to be
#: a subset of ``CODE_MODULES`` so the two lists cannot drift apart.
EXECUTION_MODULES = (
    "scripts/kg/stage2_s2_apply_runner.py",
    "scripts/kg/stage2_s2_admission_binding.py",
    "scripts/kg/stage2_s2_admission_tx.py",
    "scripts/kg/stage2_s2_apply_checks.py",
    "scripts/kg/stage2_s2_apply_target.py",
    "scripts/kg/stage2_s2_current_state.py",
    "scripts/kg/stage2_s2_collision.py",
    "scripts/kg/stage2_s2_receipt.py",
    "scripts/kg/stage2_s2_receipt_rollback.py",
    "scripts/kg/stage2_s2_write_body.py",
    "scripts/kg/stage2_s2_execute.py",
)

#: The five approved decisions this work is accountable to.
EXPECTED_APPROVED_DECISIONS = 5

#: Every field of the candidate a decision approved.  The database id and the item
#: number alone are not the candidate: two candidates can share a number in
#: different meetings, and the stored item id and its fingerprint pin the exact row.
CANDIDATE_FIELDS = ("agenda_item_db_id", "agenda_item_id", "agenda_item_number",
                    "meeting_db_id", "agenda_item_fingerprint")


def canonical_sha256(payload: Any) -> str:
    """sha256 over a canonical JSON rendering."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


def code_hashes(modules: Sequence[str] = CODE_MODULES) -> dict[str, str]:
    """sha256 of every module the plan's behaviour depends on.

    A declared module that is absent is **not** silently skipped: see
    :func:`missing_code_modules`, which the validators consult so a plan can never
    claim coverage of a module that does not exist.
    """
    out: dict[str, str] = {}
    for relative in modules:
        path = REPO / relative
        if path.exists():
            out[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def missing_code_modules(modules: Sequence[str] = CODE_MODULES) -> list[str]:
    """Declared modules that are not on disk, so the bound set is complete."""
    return sorted(relative for relative in modules if not (REPO / relative).exists())


def _row_fingerprint(row: Mapping[str, Any]) -> str:
    """The witness row fingerprint, imported lazily to avoid a module cycle.

    The evidence module imports this one, so importing it back at module scope
    would be circular.  The field list is the evidence module's, never a second
    copy that could drift from it.
    """
    from scripts.kg.stage2_s2_evidence_materialize import document_row_fingerprint

    return document_row_fingerprint(row)


def hold_population(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The authoritative hold population, identified exactly.

    ``document_ids`` is the set the accounting must equal.  ``sha256`` covers the
    same set plus each row's meeting, recorded item and **recomputed row
    fingerprint**, so a population that swapped one document for another of the
    same size — or whose row changed under its id — changes the digest.

    ``entries`` carries the per-document fingerprint so a live capture can compare
    each bound hold document one by one, not only in aggregate.
    """
    entries = sorted(
        ({"id": int(r["id"]), "meeting_db_id": int(r["meeting_db_id"]),
          "recorded_item": str(r.get("item_number") or ""),
          "row_fingerprint": _row_fingerprint(r)} for r in rows),
        key=lambda e: e["id"])
    ids = [e["id"] for e in entries]
    return {
        "count": len(entries),
        "document_ids": ids,
        "sha256": canonical_sha256(entries),
        "entries": entries,
        "identity": "supporting_documents.id, ordered",
    }


def _file_stat(path: Path) -> dict[str, Any]:
    """Mode and ownership of a live file, so the plan binds the real file.

    A digest proves the bytes; it says nothing about who can read them.  A world
    readable backup receipt is a leak even if its hash matches, so the mode and
    the owning uid/gid are bound alongside it.
    """
    import os

    if not path.exists():
        return {"mode": None, "uid": None, "gid": None, "exists": False,
                "size": None}
    info = os.stat(path)
    return {"mode": oct(info.st_mode & 0o777), "uid": int(info.st_uid),
            "gid": int(info.st_gid), "exists": True, "size": int(info.st_size)}


def backup_binding(backup_path: str | Path, *,
                   receipt: Mapping[str, Any],
                   plan_baseline_counts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Bind the protected backup into a plan, completely.

    Not just its hash: the exact path, the canonical digest of the receipt file,
    its **live mode and ownership**, the target it was taken from, the dump's own
    path, hash, mode and ownership, the restore proof and its evidence, the source
    counts and schema fingerprint, and the current baseline the plan was built
    against.  A plan that names a backup it cannot fully describe has not really
    named one, and the runner compares every one of these fields for exact
    equality against the live file.
    """
    import hashlib

    path = Path(backup_path)
    problems: list[str] = []
    stat_block = _file_stat(path)
    if not stat_block["exists"]:
        problems.append("the backup receipt does not exist")
    elif stat_block["mode"] != "0o600":
        problems.append(f"the backup receipt mode is {stat_block['mode']}, not 0o600")
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat_block["exists"] else None

    dump_path = Path(str(receipt.get("dump_path") or ""))
    dump_stat = _file_stat(dump_path) if str(receipt.get("dump_path") or "") else \
        {"mode": None, "uid": None, "gid": None, "exists": False, "size": None}
    restore = receipt.get("pg_restore") or {}
    signatures = receipt.get("signatures") or {}
    return {
        "path": path.name,
        "canonical_digest": digest,
        "receipt": stat_block,
        # Kept for the older comparison, and now derived from the same stat.
        "mode": stat_block["mode"],
        "target": {f: (receipt.get("target") or {}).get(f)
                   for f in ("dialect", "host", "port", "database", "tier")},
        "dump_path": str(receipt.get("dump_path") or "") or None,
        "dump_sha256": receipt.get("dump_sha256"),
        "dump": dump_stat,
        "restore_proof": {
            "exit_code": restore.get("exit_code"),
            "evidence_present": bool(str(restore.get("evidence") or "").strip()),
            "evidence": str(restore.get("evidence") or ""),
            "evidence_sha256": (hashlib.sha256(str(restore.get("evidence") or "")
                                               .encode("utf-8")).hexdigest()
                                if restore.get("evidence") else None),
            "verified": bool(restore.get("verified")),
        },
        "source_counts_sha256": signatures.get("counts_sha256"),
        "source_schema_sha256": signatures.get("schema_sha256"),
        "source_counts": dict(receipt.get("counts") or {}),
        # The plan's OWN recorded baseline counts, bound so the runner can compare
        # the receipt's counts against plan data one way.  It is never compared to
        # itself, and no live value is ever substituted for it.
        "plan_baseline_counts": dict(plan_baseline_counts or {}),
        "evidence_source": "the backup receipt and the dump file on disk",
        "problems": problems,
    }


def baseline_binding(population: Mapping[str, Any]) -> dict[str, Any]:
    """The plan's baseline: the population it was built against, by digest."""
    return {
        "population_count": population.get("count"),
        "population_sha256": population.get("sha256") or population.get("item_ids_sha256"),
        "identity": population.get("identity"),
    }


def assert_decisions_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> None:
    """Both plans must bind byte-identical decision sets.

    A repair plan and a correction plan applied together describe one act, so they
    must agree about who approved what.  Comparing only the count would let one
    plan bind a different reviewer, time or proposal digest.
    """
    a = (left.get("bindings") or {}).get("decisions") or []
    b = (right.get("bindings") or {}).get("decisions") or []
    if a != b:
        raise AssertionError("the two plans bind different decision sets")


def _resolve_decision_path(document_id: int, plan_dir: str | Path | None) -> str | None:
    """Find a decision artifact by the document id its filename encodes."""
    if plan_dir is None:
        return None
    root = Path(plan_dir)
    hits = sorted(root.glob(f"kg-stage2-s2-decision-*doc{document_id}.json"))
    live = [h for h in hits
            if not h.name.endswith(".obsolete.json")
            and not (root / (h.name + ".obsolete.json")).exists()]
    return live[0].name if len(live) == 1 else None


def decision_bindings(decisions: Sequence[Mapping[str, Any]], *,
                      plan_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """The human decisions, with the relationship each establishes.

    A decision approves a **proposal** about a document.  It is not the evidence
    that repairs an item, and the two must never be conflated: the evidence is
    what a packet printed, the decision is what a person approved.
    """
    out = []
    for record in decisions:
        candidate = record.get("candidate") or {}
        out.append({
            "document_id": int(record["document_id"]),
            "decision_id": record.get("decision_id"),
            "decision": record.get("decision"),
            "adjudicator": record.get("adjudicator"),
            "decided_at": record.get("decided_at"),
            "digest": record.get("digest"),
            "path": record.get("path") or _resolve_decision_path(
                int(record["document_id"]), plan_dir),
            "document_role": record.get("document_role"),
            "document_fingerprint": record.get("document_fingerprint"),
            "proposal_path": (record.get("proposal") or {}).get("path"),
            "proposal_digest": (record.get("proposal") or {}).get("digest"),
            # The adjudication unit the proposal was filed under, so the anchor's
            # own membership can be checked unit-for-unit.
            "proposal_unit": (record.get("proposal") or {}).get("decision_unit_id"),
            # The FULL candidate identity, not just a db id and a number.
            "candidate": {field: candidate.get(field) for field in CANDIDATE_FIELDS},
            # Kept for readability, and derived from the same block above.
            "candidate_agenda_item_db_id": candidate.get("agenda_item_db_id"),
            "candidate_agenda_item_number": candidate.get("agenda_item_number"),
            "human_stated_item": record.get("human_stated_item"),
            "item_number_mismatch": bool(record.get("item_number_mismatch")),
            # The heads and the proposal the decision was recorded against.
            "lineage": {key: dict((record.get("lineage") or {}).get(key) or {})
                        for key in ("plan", "aggregate", "proposal")},
            "relationship": "human approval of a model proposal, not evidence",
        })
    return sorted(out, key=lambda e: e["document_id"])


def lineage_binding(plan_dir: str | Path | None = None) -> dict[str, Any]:
    """The current S2 plan head and aggregate head."""
    from scripts.kg import stage2_s2_ai_lineage as lineage

    plan_path, plan, plan_digest = lineage.current_plan(plan_dir)
    aggregate_path, aggregate, aggregate_digest = lineage.current_aggregate(
        plan_dir, plan_digest)
    return {
        "plan": {"path": plan_path.name, "digest": plan_digest},
        "aggregate": {"path": aggregate_path.name, "digest": aggregate_digest,
                      "approved": (aggregate.get("counts") or {}).get("approved"),
                      "promoted": (aggregate.get("counts") or {}).get("promoted"),
                      "applied": (aggregate.get("counts") or {}).get("applied")},
    }


def target_baseline(target: Mapping[str, Any]) -> dict[str, Any]:
    """The target as recorded, so the plan states where it would act."""
    return {
        "dialect": target.get("dialect"),
        "host": target.get("host"),
        "port": target.get("port"),
        "database": target.get("database"),
        "tier": target.get("tier"),
    }


def build_bindings(
    *,
    holds: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
    plan_dir: str | Path | None = None,
    backup: Mapping[str, Any] | None = None,
    current_state_sha256: str | None = None,
    collision_control: Mapping[str, Any] | None = None,
    reservation_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:  # noqa: D401 - documented below
    """The whole binding block, assembled once for both plans."""
    population = hold_population(holds)
    return {
        "lineage": lineage_binding(plan_dir),
        "decisions": decision_bindings(decisions, plan_dir=plan_dir),
        "hold_population": population,
        "baseline": baseline_binding(population),
        "backup": dict(backup) if backup else None,
        # The governed collision control that replaces the impossible global unique
        # index, together with the accepted reservation schema receipt.  A plan that
        # did not bind these would be assuming a uniqueness the data cannot provide.
        "collision_control": dict(collision_control) if collision_control else None,
        "reservation_receipt": (dict(reservation_receipt)
                                if reservation_receipt else None),
        "current_state_sha256": current_state_sha256,
        "target": target_baseline(target),
        "code_hashes": code_hashes(),
    }
