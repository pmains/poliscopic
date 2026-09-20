#!/usr/bin/env python3
"""``stage2_s2_reservation_binding.py`` — reservation operations for a plan.

A plan is not applyable just because it lists rows.  Every key it would **create**
must have an exact reservation operation, decided before the apply runs, and the
transaction that would carry it out must be written down in order.  Both live here.

The operations are derived **only from the plan**: a key the plan does not propose
cannot appear, and the plan digest is carried on every operation so a reservation
can never be attributed to a different plan.

Keys are split into three outcomes, and only the first is written:

* ``reserve`` — the key is free: reserve it;
* ``hold_occupied`` — a live agenda item already holds the key: a person decides;
* ``hold_reserved`` — an earlier apply already reserved it: a person decides.

No outcome overwrites anything.
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

from scripts.kg import stage2_reservation as reservation  # noqa: E402

__all__ = [
    "OUTCOMES",
    "reservation_digest",
    "TRANSACTION_STEPS",
    "proposed_keys",
    "reservation_operations",
    "transaction_model",
    "validate_reservation_operations",
    "validate_transaction_model",
]

#: The only three outcomes a proposed key can have.
OUTCOMES = ("reserve", "hold_occupied", "hold_reserved")

#: The ordered transaction model every apply must follow.  The advisory lock comes
#: first because the occupancy read and the reservation insert must not interleave;
#: the receipt is last because it may only be written once the postconditions hold.
TRANSACTION_STEPS = (
    {"step": 1, "name": "advisory_lock",
     "detail": "pg_advisory_xact_lock on each proposed key, before any read"},
    {"step": 2, "name": "occupied_live_key_check",
     "detail": "read agenda_items for each key; an occupied key becomes a HOLD, "
               "never an overwrite"},
    {"step": 3, "name": "insert_reservation",
     "detail": "insert the exact reservation bound to the plan digest and approver; "
               "the primary key decides a race"},
    {"step": 4, "name": "typed_row_ops",
     "detail": "create or update ONLY the typed rows the plan declares"},
    {"step": 5, "name": "postconditions",
     "detail": "verify the exact after-state inside the same transaction"},
    {"step": 6, "name": "receipt",
     "detail": "write the immutable receipt after the postconditions pass"},
    {"step": 7, "name": "commit_or_rollback",
     "detail": "one commit; any failure rolls back reservations and rows together"},
    {"step": 8, "name": "replay",
     "detail": "a re-run is a bound no-op with writes=0, or a refusal"},
)


def reservation_digest(plan_digest: str, keys: Iterable[str]) -> str:
    """A stable digest over exactly the keys a plan would reserve.

    Defined here rather than in the reservation module so the applied reservation
    schema plan keeps binding the code it was built and applied with.
    """
    return hashlib.sha256(
        json.dumps({"plan": plan_digest, "keys": sorted(keys)},
                   sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def proposed_keys(plan: Mapping[str, Any], *, role: str) -> list[dict[str, Any]]:
    """Every natural key the plan would CREATE, taken only from the plan."""
    out: list[dict[str, Any]] = []
    if role == "repair":
        for row in plan.get("rows") or []:
            if row.get("action") != "materialise":
                continue
            out.append({"meeting_db_id": int(row["meeting_db_id"]),
                        "agenda_item_number": str(row["agenda_item_number"]),
                        "claimed_by": f"repair:{row['meeting_db_id']}/"
                                      f"{row['agenda_item_number']}",
                        "row_fingerprint": row.get("row_fingerprint")})
    elif role == "correction":
        for operation in plan.get("operations") or []:
            if operation.get("action") not in ("new_item_row", "renumber_existing_row"):
                continue
            number = (operation.get("to_label")
                      if operation.get("action") == "renumber_existing_row"
                      else (operation.get("proposed_row") or {}).get("agenda_item_number"))
            out.append({"meeting_db_id": int(operation["meeting_db_id"]),
                        "agenda_item_number": str(number),
                        "claimed_by": f"correction:{operation['meeting_db_id']}/{number}",
                        "row_fingerprint": operation.get("row_fingerprint")})
    else:
        raise ValueError(f"role {role!r} has no defined key set")
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for entry in sorted(out, key=lambda e: (e["meeting_db_id"],
                                            e["agenda_item_number"])):
        key = reservation.reservation_key(entry["meeting_db_id"],
                                          entry["agenda_item_number"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def reservation_operations(plan: Mapping[str, Any], *, role: str,
                           plan_digest: str,
                           live_keys: Iterable[str] = (),
                           reserved_keys: Iterable[str] = ()) -> dict[str, Any]:
    """The exact reservation operations for a plan, decided before the apply."""
    from scripts.kg import stage2_artifacts as artifacts

    digest = plan_digest or artifacts.recorded_digest(plan)
    if not digest:
        raise ValueError("the plan carries no digest to bind its reservations to")
    live = set(live_keys)
    reserved = set(reserved_keys)
    operations: list[dict[str, Any]] = []
    counts = {outcome: 0 for outcome in OUTCOMES}
    for entry in proposed_keys(plan, role=role):
        key = reservation.reservation_key(entry["meeting_db_id"],
                                          entry["agenda_item_number"])
        if key in live:
            outcome, reason = "hold_occupied", "a live agenda item already holds this key"
        elif key in reserved:
            outcome, reason = "hold_reserved", "an earlier apply already reserved this key"
        else:
            outcome, reason = "reserve", "the key is free"
        counts[outcome] += 1
        operations.append({
            "key": key, "meeting_db_id": entry["meeting_db_id"],
            "agenda_item_number": entry["agenda_item_number"],
            "claimed_by": entry["claimed_by"], "row_fingerprint": entry.get(
                "row_fingerprint"),
            "outcome": outcome, "reason": reason,
            "plan_digest": digest if outcome == "reserve" else None,
        })
    body = {"plan_digest": digest, "role": role, "operations": operations,
            "counts": counts,
            "reserve_key_digest": reservation_digest(
                digest, [o["key"] for o in operations if o["outcome"] == "reserve"]),
            "derived_from_the_plan_only": True}
    return body


def validate_reservation_operations(binding: Mapping[str, Any],
                                    plan: Mapping[str, Any], *, role: str) -> list[str]:
    """Every proposed key must have exactly one operation, and the sets must agree."""
    problems: list[str] = []
    expected = {reservation.reservation_key(e["meeting_db_id"],
                                            e["agenda_item_number"])
                for e in proposed_keys(plan, role=role)}
    operations = binding.get("operations")
    if operations is None:
        return ["the plan binds no reservation operations"]
    seen = [o.get("key") for o in operations]
    if len(seen) != len(set(seen)):
        problems.append("a key has more than one reservation operation")
    if set(seen) != expected:
        problems.append(
            f"the reservation operations do not cover the plan's proposed keys "
            f"(missing {sorted(expected - set(seen))[:3]}, "
            f"extra {sorted(set(seen) - expected)[:3]})")
    for operation in operations:
        if operation.get("outcome") not in OUTCOMES:
            problems.append(f"{operation.get('key')}: unregistered outcome")
        if operation["outcome"] == "reserve" and \
                operation.get("plan_digest") != binding.get("plan_digest"):
            problems.append(f"{operation.get('key')}: a reservation is not bound to "
                            f"the plan digest")
        if operation["outcome"] != "reserve" and operation.get("plan_digest") is not None:
            problems.append(f"{operation.get('key')}: a held key carries a plan digest")
    if binding.get("counts") != {outcome: sum(
            1 for o in operations if o["outcome"] == outcome) for outcome in OUTCOMES}:
        problems.append("the outcome counts do not match the operations")
    reserves = [o["key"] for o in operations if o["outcome"] == "reserve"]
    if binding.get("reserve_key_digest") != reservation_digest(
            binding.get("plan_digest") or "", reserves):
        problems.append("the reserve-key digest does not cover the reserved keys")
    if binding.get("derived_from_the_plan_only") is not True:
        problems.append("the operations must be derived from the plan only")
    return problems


def transaction_model(plan_digest: str, *, role: str) -> dict[str, Any]:
    """The ordered transaction model this plan's apply must follow."""
    return {
        "role": role,
        "plan_digest": plan_digest,
        "steps": [dict(s) for s in TRANSACTION_STEPS],
        "one_transaction": True,
        "invariant_carrier": "the reservation primary key",
        "supporting": ["pg_advisory_xact_lock per key",
                       "SERIALIZABLE with whole-unit retry"],
        "conflict_rule": "an occupied live key and an already-reserved key are HELD, "
                         "never overwritten",
        "receipt_ownership": "the receipt owns exactly the rows and reservations the "
                             "transaction created",
        "replay_rule": "a re-run is a bound no-op with writes=0, or a refusal",
        "rollback_rule": "reservations and rows roll back together; never partially",
        "write_path": "absent by design",
    }


def validate_transaction_model(model: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    steps = [s.get("name") for s in model.get("steps") or []]
    if steps != [s["name"] for s in TRANSACTION_STEPS]:
        problems.append("the transaction model is not the declared ordered model")
    for field, expected in (("one_transaction", True),
                            ("invariant_carrier",
                             "the reservation primary key")):
        if model.get(field) != expected:
            problems.append(f"the transaction model must record {field!r}")
    for field in ("conflict_rule", "receipt_ownership", "replay_rule",
                  "rollback_rule"):
        if not model.get(field):
            problems.append(f"the transaction model omits {field!r}")
    if model.get("write_path") != "absent by design":
        problems.append("the transaction model must declare no write path")
    return problems
