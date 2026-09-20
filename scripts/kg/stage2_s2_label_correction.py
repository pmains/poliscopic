#!/usr/bin/env python3
"""``stage2_s2_label_correction.py`` — the truncated-label correction plan.

Some documents record the agenda item number ``4`` when the document's own
retained text says ``4.AA``.  The stored value is a **truncated label**.

The independent review applied to the materialisation plan applies here too, and
this module is deliberately shaped like it: same binding block, same set-equality
accounting, same replay and rollback contract, same separation of deterministic
evidence from human approval.  Nothing about a second plan makes the failures
less likely.

The correction depends on what already exists: a **renumbered** row when the
truncated number is present, a **new row** when it is not.  Nothing is inferred
from document order — a group that splits into several labels gives an unlabelled
attachment no single home, so it is held.
"""

from __future__ import annotations

import hashlib
import json
import sys
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
from scripts.kg import stage2_s2_row_derivation as row_derivation  # noqa: E402
from scripts.kg import stage2_s2_reservation_binding as reservation_binding  # noqa: E402
from scripts.kg import stage2_s2_reservation_binding as reservation_binding  # noqa: E402

__all__ = ["PLAN_KIND", "PLAN_VERSION", "build_plan", "plan_digest",
           "replay_digest", "validate_plan"]

PLAN_KIND = "kg-stage2-s2-label-correction-plan"
PLAN_VERSION = "kg-stage2-s2-label-correction-plan/2.0"
DIGEST_FIELD = artifacts.DIGEST_FIELD


def plan_digest(plan: Mapping[str, Any]) -> str:
    """The artifact's one canonical digest (``digest``)."""
    return artifacts.compute_digest(plan)


def replay_digest(plan: Mapping[str, Any]) -> str:
    """The replay/content hash, named distinctly from the artifact digest."""
    excluded = (DIGEST_FIELD, "replay_digest", "created_at")
    return binding.canonical_sha256({k: v for k, v in plan.items() if k not in excluded})


def _is_truncation(recorded: str, stated: str) -> bool:
    """Is *recorded* a strict prefix of *stated* at a label boundary?"""
    recorded, stated = recorded.strip(), stated.strip()
    if not recorded or not stated or recorded == stated:
        return False
    # The character after the prefix must be the label separator.  Without this,
    # "40" would read as a truncation of "4", which would renumber an unrelated
    # item onto a label it never had.
    return stated.startswith(recorded) and stated[len(recorded)] == "."


def build_plan(
    rows: Sequence[Mapping[str, Any]],
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
    derived_rows: Mapping[str, Mapping[str, Any]] | None = None,
    derivation_connection: Any = None,
) -> dict[str, Any]:
    """Build the dry correction plan over truncated-label groups."""
    existing: dict[int, set[str]] = {}
    for item in canonical_items:
        existing.setdefault(int(item["meeting_db_id"]), set()).add(
            str(item["agenda_item_number"]).strip())

    by_group: dict[tuple[int, str], list[dict]] = {}
    row_by_id: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        row_by_id[int(row["id"])] = row
        ev = evidence.evidence_for(row)
        by_group.setdefault((ev["meeting_db_id"], ev["recorded_item"]), []).append(ev)

    operations: list[dict[str, Any]] = []
    held_documents: list[dict[str, Any]] = []

    def _operation(meeting: int, recorded: str, label: str, witness: dict,
                   resolves: Sequence[int], order: int) -> dict[str, Any]:
        source = row_by_id[int(witness["document_id"])]
        present = label in existing.get(meeting, set())
        truncated_row = recorded in existing.get(meeting, set())
        action = ("already_materialised" if present
                  else "renumber_existing_row" if truncated_row else "new_item_row")
        # The complete, evidenced row when the caller derived one; otherwise the
        # legacy shape (the real ``item_type`` column, never ``item_type_category``).
        key = f"{meeting}|{label}"
        complete = None
        if derivation_connection is not None and action == "new_item_row":
            ev = row_derivation.meeting_evidence(derivation_connection, [meeting]).get(meeting)
            if ev is not None:
                candidate = row_derivation.derive_row(
                    ev, number=label, title=witness["title"], text=witness["title"],
                    sort_order=order)
                if not row_derivation.validate_row(candidate):
                    complete = candidate
                else:
                    raise ValueError(
                        f"{key}: the derived row is not evidenced: "
                        + "; ".join(row_derivation.validate_row(candidate)[:3]))
        if complete is not None:
            proposed = complete
        elif derived_rows is not None and key in derived_rows:
            proposed = dict(derived_rows[key])
        else:
            proposed = {
                "meeting_db_id": meeting,
                "body": source.get("body"),
                "agenda_item_number": label,
                "agenda_item_title": witness["title"],
                "agenda_item_text": witness["title"],
                "item_type": "",
                "section_level": 0,
                "sort_order": order,
            }
        renamed = None
        if action == "renumber_existing_row":
            renamed = {"from": recorded, "to": label,
                       "preimage": "the row's complete previous values, captured at "
                                   "apply time and bound in the receipt"}
        return {
            "action": action,
            "meeting_db_id": meeting,
            "recorded_item": recorded,
            "corrected_item": label,
            "from_label": recorded,
            "to_label": label,
            "proposed_title": witness["title"],
            "witness": {
                "document_id": int(witness["document_id"]),
                "witness_kind": witness["kind"],
                "document_row_fingerprint": witness["document_row_fingerprint"],
                "content_sha256": witness["content_sha256"],
                "number_span": witness["number_span"],
                "title_span": witness["title_span"],
            },
            "proposed_row": proposed,
            "row_fingerprint": binding.canonical_sha256(proposed),
            "renumber": renamed,
            "identity": {
                "strategy": ("surrogate id returned by INSERT ... RETURNING id"
                             if action != "renumber_existing_row" else
                             "existing row id, recorded in the receipt"),
                "natural_key": [meeting, label],
                "rollback_owner": "apply receipt recorded ids only",
            },
            "renumbers_an_existing_row": action == "renumber_existing_row",
            "creates_a_new_row": action == "new_item_row",
            "resolves_documents": sorted(int(d) for d in resolves),
            "resolves_count": len(resolves),
        }

    def _hold(meeting: int, recorded: str, document: dict, reason: str) -> None:
        held_documents.append({
            "document_id": document["document_id"], "meeting_db_id": meeting,
            "recorded_item": recorded, "stated_label": document["states_label"],
            "reason": reason,
        })

    for (meeting, recorded), documents in sorted(by_group.items()):
        witnesses = [d for d in documents
                     if d["states_label"] and _is_truncation(recorded, d["states_label"])]
        print_labels = sorted({d["states_label"] for d in documents if d["states_label"]})
        plain = [d for d in documents if not d["states_label"]]

        if not print_labels:
            for d in documents:
                _hold(meeting, recorded, d, "no document states a label")
            continue

        if len(print_labels) == 1 and witnesses:
            primary = sorted(witnesses, key=lambda d: d["document_id"])[0]
            operations.append(_operation(meeting, recorded, print_labels[0], primary,
                                         [d["document_id"] for d in documents],
                                         len(operations) + 1))
            continue

        for witness in sorted(witnesses, key=lambda d: d["document_id"]):
            operations.append(_operation(meeting, recorded, witness["states_label"],
                                         witness, [witness["document_id"]],
                                         len(operations) + 1))
        for d in plain:
            _hold(meeting, recorded, d,
                  f"attachment with no label of its own; the group names "
                  f"{len(print_labels)} label(s) so it cannot be assigned without "
                  f"inferring from document order")

    bindings = binding.build_bindings(
        holds=rows, decisions=decisions, target=target, plan_dir=plan_dir,
        backup=backup, current_state_sha256=current_state_sha256,
        collision_control=collision_control, reservation_receipt=reservation_receipt)
    accounted = sorted([int(d) for op in operations for d in op["resolves_documents"]]
                       + [h["document_id"] for h in held_documents])
    population = bindings["hold_population"]["document_ids"]
    approved = [d["document_id"] for d in bindings["decisions"]
                if d["decision"] == "approve"]
    witnesses = [op["witness"]["document_id"] for op in operations]

    plan = {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "created_at": created_at,
        "mode": "dry-run",
        "bindings": bindings,
        "operations": operations,
        "held_documents": held_documents,
        "counts": {
            "groups": len(by_group),
            "operations": len(operations),
            "new_item_row": sum(1 for o in operations if o["action"] == "new_item_row"),
            "renumber_existing_row": sum(
                1 for o in operations if o["action"] == "renumber_existing_row"),
            "already_materialised": sum(
                1 for o in operations if o["action"] == "already_materialised"),
            "held_documents": len(held_documents),
            "documents_accounted": len(accounted),
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
                         "approved documents rest on human adjudication.",
        },
        "policy": {
            "never_renumber_from_document_order": True,
            "a_truncated_label_never_becomes_a_new_item_by_position": True,
            "attachments_in_a_multi_label_group_are_held": True,
        },
        "collision_policy": {
            "rule": "an agenda item number that already exists for the meeting is "
                    "reported as already_materialised and never created again",
            "checked_against": "agenda_items (meeting_db_id, agenda_item_number)",
            "locking": "the check and the insert share one transaction; the check "
                       "takes SELECT ... FOR UPDATE on the meeting's agenda_items "
                       "rows before any insert",
        },
        "replay": {
            "no_stability_claim": True,
            "statement": "applying this plan CHANGES the database; the artifact digest "
                         "is historical",
            "post_apply_state": "every operation's (meeting_db_id, corrected_item) "
                                "exists",
            "on_post_apply_run": "the runner detects that state and writes a "
                                 "receipt-bound no-op with writes=0, or refuses naming "
                                 "the already-applied items. It never re-inserts.",
        },
        "rollback": {
            "ownership": "only ids recorded in the apply receipt",
            "never": "never delete by (meeting_db_id, agenda_item_number) alone",
            "inserted_row_fingerprints": "recorded in the receipt for each inserted id",
            "renumbered_preimages": "the complete previous values of any renumbered row, "
                                    "captured at apply time",
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
    # The reservation operations bind to the plan's IDENTITY: the plan's substance
    # excluding the reservation fields themselves and the artifact metadata.  That
    # breaks the circularity (the ops carry the digest, the digest covers the ops)
    # without weakening either: the final replay_digest still covers the operations.
    identity = binding.canonical_sha256({
        k: v for k, v in plan.items()
        if k not in (DIGEST_FIELD, "replay_digest", "created_at",
                     "reservation_operations", "transaction_model")})
    plan["reservation_operations"] = reservation_binding.reservation_operations(
        plan, role="correction", plan_digest=identity,
        live_keys=live_keys, reserved_keys=reserved_keys)
    plan["transaction_model"] = reservation_binding.transaction_model(
        identity, role="correction")
    plan["replay_digest"] = replay_digest(plan)
    plan[DIGEST_FIELD] = plan_digest(plan)
    problems = validate_plan(plan, rows=rows)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return plan


def validate_plan(plan: Mapping[str, Any],
                  *, rows: Sequence[Mapping[str, Any]] | None = None) -> list[str]:
    """Structural, binding and set-equality checks for a correction plan."""
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
    decisions = bindings.get("decisions") or []
    if len(decisions) != binding.EXPECTED_APPROVED_DECISIONS:
        problems.append(f"expected {binding.EXPECTED_APPROVED_DECISIONS} bound decisions, "
                        f"found {len(decisions)}")
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
        plan.get("reservation_operations") or {}, plan, role="correction")
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
    if rows is not None:
        population = binding.hold_population(rows)["document_ids"]
        if sorted(accounting.get("accounted_document_ids") or []) != population:
            problems.append("accounted documents do not equal the supplied rows")

    for op in plan.get("operations") or []:
        where = f"{op.get('meeting_db_id')} {op.get('from_label')}->{op.get('to_label')}"
        if op.get("action") == "renumber_existing_row" and op.get("creates_a_new_row"):
            problems.append(f"{where}: cannot both renumber and create")
        if op.get("action") == "new_item_row" and op.get("renumbers_an_existing_row"):
            problems.append(f"{where}: cannot both create and renumber")
        witness = op.get("witness") or {}
        for field in ("document_id", "document_row_fingerprint", "content_sha256",
                      "number_span", "title_span"):
            if not witness.get(field):
                problems.append(f"{where}: witness missing {field!r}")
        if not op.get("proposed_row") or not op.get("row_fingerprint"):
            problems.append(f"{where}: no exact proposed row or row fingerprint")
        if not (op.get("identity") or {}).get("strategy"):
            problems.append(f"{where}: no identity strategy")
        if not _is_truncation(str(op.get("from_label")), str(op.get("to_label"))):
            problems.append(f"{where}: {op.get('to_label')!r} is not a truncation of "
                            f"{op.get('from_label')!r}")

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
