#!/usr/bin/env python3
"""``stage2_s2_repair_plan.py`` — the dry repair plan for the held item references.

The plan records what *would* be materialised, backed only by text the source
itself printed.  It is read-only and writes nothing.

Three things this plan refuses to be vague about, because an independent review
found the earlier draft vague:

* **Accounting is set equality, not a count.** The population to account for is
  bound by document id and hashed, and the plan asserts the accounted set equals
  it exactly.  Two populations of the same size are not interchangeable.
* **Replay does not claim stability.** Applying changes the database, so the
  artifact digest is historical.  A post-apply run must be a receipt-bound no-op
  or a refusal, never a re-application.
* **Rollback is owned by the receipt.** A proposed row is given exact, complete
  values and an identity strategy; only surrogate ids the apply receipt records
  may be deleted, and document links are restored from exact preimages stored at
  apply time.  Deleting by ``(meeting, item)`` alone is forbidden.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_evidence_materialize as evidence  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402
from scripts.kg import stage2_s2_reservation_binding as reservation_binding  # noqa: E402
from scripts.kg import stage2_s2_reservation_binding as reservation_binding  # noqa: E402

__all__ = ["PLAN_KIND", "PLAN_VERSION", "build_plan", "plan_digest",
           "replay_digest", "validate_plan"]

PLAN_KIND = "kg-stage2-s2-repair-plan"
PLAN_VERSION = "kg-stage2-s2-repair-plan/2.0"
DIGEST_FIELD = artifacts.DIGEST_FIELD


def plan_digest(plan: Mapping[str, Any]) -> str:
    """The artifact's one canonical digest (``digest``), as written by the store."""
    return artifacts.compute_digest(plan)


def replay_digest(plan: Mapping[str, Any]) -> str:
    """The replay/content hash: the plan's substance, excluding its own metadata.

    Named distinctly from ``digest`` on purpose.  ``digest`` identifies the
    artifact; ``replay_digest`` identifies what would be done, and is what a
    reviewer compares when asking whether two plans describe the same work.
    """
    excluded = (DIGEST_FIELD, "replay_digest", "created_at")
    body = {k: v for k, v in plan.items() if k not in excluded}
    return binding.canonical_sha256(body)


def _row_template(row: Mapping[str, Any], item_number: str, title: str,
                  order: int) -> dict[str, Any]:
    """The exact, complete values a materialised row would carry."""
    return {
        "meeting_db_id": int(row["meeting_db_id"]),
        "body": row.get("body"),
        "agenda_item_number": item_number,
        "agenda_item_title": title,
        "agenda_item_text": title,
        "item_type_category": "item",
        "section_level": 0,
        "sort_order": order,
    }


def build_plan(
    holds: Sequence[Mapping[str, Any]],
    *,
    canonical_items: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    created_at: str,
    target: Mapping[str, Any],
    plan_dir: str | Path | None = None,
    backup: Mapping[str, Any] | None = None,
    current_state_sha256: str | None = None,
    collision_control: Mapping[str, Any] | None = None,
    reservation_receipt: Mapping[str, Any] | None = None,
    live_keys: Sequence[str] = (),
    reserved_keys: Sequence[str] = (),
    depends_on: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the dry repair plan over every held reference."""
    existing: dict[int, set[str]] = {}
    for item in canonical_items:
        existing.setdefault(int(item["meeting_db_id"]), set()).add(
            str(item["agenda_item_number"]).strip())

    rows: list[dict[str, Any]] = []
    verdicts = evidence.materialization_verdicts(holds)
    for verdict in verdicts:
        meeting = int(verdict["meeting_db_id"])
        number = str(verdict["item_number"])
        present = number in existing.get(meeting, set())
        documents = sorted(int(d) for d in verdict["documents"])
        if verdict["verdict"] == "materialisable" and not present:
            witness_row = next(r for r in holds
                               if int(r["id"]) == int(verdict["witness_document_id"]))
            witness = evidence.evidence_for(witness_row)
            coordinates = witness["coordinates"] or {}
            proposed = _row_template(witness_row, number, verdict["title"],
                                     len(rows) + 1)
            rows.append({
                "action": "materialise",
                "meeting_db_id": meeting,
                "agenda_item_number": number,
                "proposed_title": verdict["title"],
                "witness": {
                    "document_id": int(verdict["witness_document_id"]),
                    "witness_kind": verdict["witness_kind"],
                    "document_row_fingerprint": witness["document_row_fingerprint"],
                    "content_sha256": witness["content_sha256"],
                    "number_span": witness["number_span"],
                    "title_span": witness["title_span"],
                    "revision_span": witness["revision_span"],
                    "listing_span": witness["listing_span"],
                    "coordinates": {k: coordinates.get(k) for k in
                                    ("kind", "start", "end", "span", "sha256")},
                    "evidence_sha256": coordinates.get("sha256"),
                },
                "proposed_row": proposed,
                "row_fingerprint": binding.canonical_sha256(proposed),
                "identity": {
                    "strategy": "surrogate id returned by INSERT ... RETURNING id",
                    "natural_key": [meeting, number],
                    "rollback_owner": "apply receipt inserted ids only",
                },
                "resolves_documents": documents,
                "resolves_count": len(documents),
                "collision": False,
            })
            continue
        reason = "; ".join(verdict["reasons"]) or "no witness states the label"
        if present:
            reason = "the item already exists for this meeting"
        rows.append({
            "action": "hold", "meeting_db_id": meeting,
            "agenda_item_number": number, "proposed_title": None, "witness": None,
            "proposed_row": None, "row_fingerprint": None, "identity": None,
            "resolves_documents": documents, "resolves_count": 0,
            "collision": present, "already_materialised": present, "reason": reason,
        })

    rows.sort(key=lambda r: (r["meeting_db_id"], r["agenda_item_number"]))
    bindings = binding.build_bindings(
        holds=holds, decisions=decisions, target=target, plan_dir=plan_dir,
        backup=backup, current_state_sha256=current_state_sha256,
        collision_control=collision_control, reservation_receipt=reservation_receipt)
    accounted = sorted(d for row in rows for d in row["resolves_documents"])
    population = bindings["hold_population"]["document_ids"]
    approved = [d["document_id"] for d in bindings["decisions"]
                if d["decision"] == "approve"]
    witnesses = [row["witness"]["document_id"] for row in rows
                 if row["action"] == "materialise"]

    plan = {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "created_at": created_at,
        "mode": "dry-run",
        "bindings": bindings,
        "rows": rows,
        "counts": {
            "groups": len(rows),
            "materialise": sum(1 for r in rows if r["action"] == "materialise"),
            "hold": sum(1 for r in rows if r["action"] == "hold"),
            "documents_accounted": len(accounted),
            "documents_resolved": sum(r["resolves_count"] for r in rows),
        },
        "accounting": {
            "authoritative_population_sha256": bindings["hold_population"]["sha256"],
            "accounted_sha256": binding.canonical_sha256([{"id": d} for d in accounted]),
            "accounted_document_ids": accounted,
            "set_equality": accounted == population,
            "missing_from_accounting": sorted(set(population) - set(accounted)),
            "unexpected_in_accounting": sorted(set(accounted) - set(population)),
        },
        "accountability": {
            "deterministic_evidence_repairs": {
                "count": len(witnesses), "document_ids": sorted(witnesses),
                "basis": "text the source printed",
            },
            "approved_proposal_documents": {
                "count": len(approved), "document_ids": sorted(approved),
                "basis": "human adjudication of a model proposal",
            },
            "overlap": sorted(set(witnesses) & set(approved)),
            "statement": "the deterministic repairs rest on printed evidence; the "
                         "approved documents rest on human adjudication. The two are "
                         "separable and are never merged into one authority.",
        },
        "collision_policy": {
            "rule": "an agenda item number that already exists for the meeting is "
                    "never re-created; the group becomes a hold",
            "checked_against": "agenda_items (meeting_db_id, agenda_item_number)",
            "locking": "the check and the insert share one transaction; the check "
                       "takes SELECT ... FOR UPDATE on the meeting's agenda_items "
                       "rows before any insert, so a concurrent writer cannot slip "
                       "an item in between",
        },
        "replay": {
            "no_stability_claim": True,
            "statement": "applying this plan CHANGES the database; the artifact digest "
                         "is historical and is not claimed to describe the database "
                         "afterwards",
            "post_apply_state": "every operation's (meeting_db_id, agenda_item_number) "
                                "exists",
            "on_post_apply_run": "the runner detects that state and writes a "
                                 "receipt-bound no-op with writes=0, or refuses naming "
                                 "the already-applied items. It never re-inserts.",
        },
        "rollback": {
            "ownership": "only surrogate ids recorded in the apply receipt",
            "never": "never delete by (meeting_db_id, agenda_item_number) alone",
            "inserted_row_fingerprints": "recorded in the receipt for each surrogate id",
            "attachment_preimages": "exact stored values per resolved document, captured "
                                    "at apply time and bound in the receipt",
            "restore": "exact preimage values, never recomputed ones",
            "reversible": True,
        },
        "preconditions": {
            "read_only_snapshot": True,
            "no_database_write_by_this_plan": True,
            "promotion_not_implied": True,
        },
        "applied": False,
        "promoted": False,
    }
    # Ordering is a property of the PLAN, not of the operator's memory.  The repair
    # plan is derived from the population the classifier sees NOW; the correction
    # plan changes that population, so this artifact must never be reused after the
    # correction apply -- it must be regenerated from the post-correction state.
    plan["dependency"] = {
        "depends_on": "correction",
        "correction_plan": dict(depends_on or {}),
        "ordering": "the correction plan must be applied BEFORE the repair plan",
        "state_binding": "bound to the current-state digest; the correction apply "
                         "changes it, so this artifact is stale the moment correction "
                         "lands",
        "reuse_rule": "NEVER reuse this digest after the correction apply; regenerate "
                      "from the post-correction state and obtain a fresh review",
        "currently_applyable": False,
        "why_not_applyable_now": "the ordering requires the correction postimage "
                                 "first, and applying them in the other order would "
                                 "defeat the collision guarantee the reservation "
                                 "contract exists to provide",
        "regeneration_recipe": {
            "command": "nohup .venv/bin/python -u scripts/kg/stage2_s2_plan_regen.py",
            "inputs": ["the post-correction agenda_items population",
                       "the bound hold population recomputed from the classifier",
                       "the accepted backup receipt",
                       "the accepted reservation schema receipt",
                       "the governed reservation contract"],
            "invariants": ["the proposed keys must stay disjoint from the correction "
                           "postimage's keys",
                           "every proposed key must receive exactly one reservation "
                           "operation",
                           "the new artifact must supersede this one by sidecar"],
            "may_not": "invent a digest, or reuse this artifact's digest",
        },
    }
    # The reservation operations bind to the plan's IDENTITY: the plan's substance
    # excluding the reservation fields themselves and the artifact metadata.  That
    # breaks the circularity (the ops carry the digest, the digest covers the ops)
    # without weakening either: the final replay_digest still covers the operations.
    identity = binding.canonical_sha256({
        k: v for k, v in plan.items()
        if k not in (DIGEST_FIELD, "replay_digest", "created_at",
                     "reservation_operations", "transaction_model")})
    plan["reservation_operations"] = reservation_binding.reservation_operations(
        plan, role="repair", plan_digest=identity,
        live_keys=live_keys, reserved_keys=reserved_keys)
    plan["transaction_model"] = reservation_binding.transaction_model(
        identity, role="repair")
    plan["replay_digest"] = replay_digest(plan)
    plan[DIGEST_FIELD] = plan_digest(plan)
    problems = validate_plan(plan, holds=holds)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return plan


def validate_plan(plan: Mapping[str, Any],
                  *, holds: Sequence[Mapping[str, Any]] | None = None) -> list[str]:
    """Structural, binding and set-equality checks for a repair plan."""
    problems: list[str] = []
    if plan.get("kind") != PLAN_KIND:
        problems.append(f"kind must be {PLAN_KIND!r}")
    if plan.get("mode") != "dry-run":
        problems.append("the plan must be dry-run only")
    if plan.get("applied") is not False or plan.get("promoted") is not False:
        problems.append("a plan must record applied=false and promoted=false")

    bindings = plan.get("bindings") or {}
    for key in ("lineage", "decisions", "hold_population", "target", "code_hashes"):
        if not bindings.get(key):
            problems.append(f"binding {key!r} is missing")
    lineage = bindings.get("lineage") or {}
    if not (lineage.get("plan") or {}).get("digest"):
        problems.append("the plan head digest is not bound")
    if not (lineage.get("aggregate") or {}).get("digest"):
        problems.append("the aggregate digest is not bound")
    decisions = bindings.get("decisions") or []
    if len(decisions) != binding.EXPECTED_APPROVED_DECISIONS:
        problems.append(f"expected {binding.EXPECTED_APPROVED_DECISIONS} bound decisions, "
                        f"found {len(decisions)}")
    for decision in decisions:
        for field in ("decision_id", "digest", "adjudicator", "decided_at",
                      "proposal_digest"):
            if not decision.get(field):
                problems.append(f"decision {decision.get('document_id')} is missing {field!r}")
    hashes = bindings.get("code_hashes") or {}
    # The COMPLETE transitive execution set, not only the planning trio: a plan that
    # bound the builders but not the admission, transaction, target, state, collision,
    # receipt, rollback or write body could be executed by code it never named.
    for required in binding.CODE_MODULES:
        if required not in hashes:
            problems.append(f"code hashes do not cover {required}")
    for required in binding.EXECUTION_MODULES:
        if required not in hashes:
            problems.append(f"execution code hashes do not cover {required}")
    # Coverage is not enough: a plan whose bound code has DRIFTED is describing a
    # revision that no longer exists, so it must fail validation rather than wait
    # for the admission path to notice.
    live = binding.code_hashes(tuple(hashes))
    drifted = sorted(r for r, d in hashes.items() if live.get(r) != d)
    if drifted:
        problems.append(f"code has drifted for {drifted}")
    absent = binding.missing_code_modules()
    if absent:
        problems.append(f"declared execution modules are absent from disk: {absent}")

    # A plan that only demanded a global unique index would be assuming a
    # uniqueness the data cannot provide.  It must bind the GOVERNED contract and
    # the exact reservation operation for every key it would create.
    control = bindings.get("collision_control") or {}
    if not control:
        problems.append("the plan binds no governed collision control")
    else:
        if control.get("mode") != "exact_key_reservation":
            problems.append(f"the collision control mode is {control.get('mode')!r}")
        if control.get("historical_key_is_unique") is not False:
            problems.append("the plan must record that the historical key is not unique")
    receipt = bindings.get("reservation_receipt") or {}
    if not receipt.get("path") or not receipt.get("digest"):
        problems.append("the plan binds no accepted reservation schema receipt")
    reservation_problems = reservation_binding.validate_reservation_operations(
        plan.get("reservation_operations") or {}, plan, role="repair")
    problems.extend(reservation_problems)
    problems.extend(reservation_binding.validate_transaction_model(
        plan.get("transaction_model") or {}))

    accounting = plan.get("accounting") or {}
    # Set equality is RECOMPUTED from the bound population.  Trusting a stored
    # boolean would let a tampered accounting pass: the flag and the ids could be
    # edited together, and a population of the same size would look correct.
    bound = ((bindings.get("hold_population") or {}).get("document_ids")) or []
    accounted_ids = list(accounting.get("accounted_document_ids") or [])
    if sorted(accounted_ids) != sorted(int(d) for d in bound):
        problems.append("accounted documents do not equal the bound hold population")
    if len(accounted_ids) != len(set(accounted_ids)):
        problems.append("accounting names a document more than once")
    if accounting.get("set_equality") is not True:
        problems.append("accounting does not claim set equality with the bound population")
    if accounting.get("missing_from_accounting") or accounting.get("unexpected_in_accounting"):
        problems.append("accounting records a missing or unexpected document")
    if accounting.get("authoritative_population_sha256") != \
            (bindings.get("hold_population") or {}).get("sha256"):
        problems.append("the accounting population digest does not match the bound population")
    if holds is not None:
        population = binding.hold_population(holds)["document_ids"]
        if sorted(accounting.get("accounted_document_ids") or []) != population:
            problems.append("accounted documents do not equal the supplied hold population")

    for row in plan.get("rows") or []:
        where = f"{row.get('meeting_db_id')}/{row.get('agenda_item_number')}"
        if row.get("action") == "materialise":
            witness = row.get("witness") or {}
            for field in ("document_id", "document_row_fingerprint",
                          "content_sha256", "number_span"):
                if not witness.get(field):
                    problems.append(f"{where}: witness missing {field!r}")
            if not witness.get("title_span"):
                problems.append(f"{where}: witness binds no title span")
            if not (witness.get("coordinates") or {}).get("sha256"):
                problems.append(f"{where}: witness binds no evidence sha256")
            if not row.get("proposed_row") or not row.get("row_fingerprint"):
                problems.append(f"{where}: no exact proposed row or row fingerprint")
            if not (row.get("identity") or {}).get("strategy"):
                problems.append(f"{where}: no identity strategy")
            if row.get("collision"):
                problems.append(f"{where}: materialise on a colliding item number")
        elif row.get("action") == "hold":
            if not row.get("reason"):
                problems.append(f"{where}: hold without a reason")
            if row.get("proposed_row"):
                problems.append(f"{where}: hold must not propose a row")
        else:
            problems.append(f"{where}: unknown action {row.get('action')!r}")

    rollback = plan.get("rollback") or {}
    if "never delete by" not in str(rollback.get("never", "")):
        problems.append("rollback does not forbid deleting by meeting and number alone")
    if rollback.get("restore") != "exact preimage values, never recomputed ones":
        problems.append("rollback does not require exact preimages")
    replay = plan.get("replay") or {}
    if replay.get("no_stability_claim") is not True:
        problems.append("replay must not claim the artifact digest stays valid after apply")
    if not plan.get("replay_digest"):
        problems.append("replay_digest is missing")
    if plan.get(DIGEST_FIELD) != artifacts.compute_digest(plan):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems
