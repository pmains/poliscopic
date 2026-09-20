#!/usr/bin/env python3
"""``stage2_dupkey_contract.py`` — the proposed collision-control contract.

The Stage 2 plans require absent-key serialization.  The current requirement — a
global unique index on ``agenda_items(meeting_db_id, agenda_item_number)`` — **cannot
be satisfied**: the live data holds 4,673 excess rows on that key, all of them
legacy placeholder or multi-body numbering, so ``CREATE UNIQUE INDEX`` fails.  This
module evaluates the alternatives honestly and states a contract that is
concurrency-safe **without** rewriting valid data.

What each alternative actually guarantees is written down, including the ones that
do not work, because "we chose not to use it" and "it does not do what it looks
like it does" are different statements.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ALTERNATIVES",
    "CONTRACT_KIND",
    "CONTRACT_VERSION",
    "RESERVATION_TABLE",
    "build_contract",
    "reservation_key",
    "reservation_operations",
    "validate_contract",
]

#: The artifact store stamps its own digest field on write; it is not part of the
#: contract's substance and must not enter the contract digest.
ARTIFACT_DIGEST_FIELD = "digest"

CONTRACT_KIND = "kg-stage2-collision-control-contract"
CONTRACT_VERSION = "kg-stage2-collision-control-contract/1.0"

#: An additive table.  The uniqueness Stage 2 needs lives here, so no existing row
#: has to be renumbered to make the uniqueness true.
RESERVATION_TABLE = "agenda_item_key_reservation"

#: Every alternative considered, with an honest verdict.  ``sufficient`` means "on
#: its own it makes a duplicate insert impossible"; several are useful only in
#: combination, and that is recorded rather than glossed.
ALTERNATIVES = (
    {
        "id": "A1-global-unique-index",
        "mechanism": "CREATE UNIQUE INDEX ON agenda_items (meeting_db_id, "
                     "agenda_item_number)",
        "sufficient": True,
        "viable_here": False,
        "verdict": "REJECTED",
        "why": "it cannot be created: 4,673 excess rows violate it today. Making it "
               "constructible means rewriting 7,563 valid rows first, which the "
               "conservation rule forbids without a separately adjudicated plan.",
    },
    {
        "id": "A2-scoped-or-partial-uniqueness",
        "mechanism": "a partial unique index excluding placeholder and multi-body "
                     "numbering, e.g. WHERE agenda_item_number !~ '^(0|a|A)$'",
        "sufficient": True,
        "viable_here": False,
        "verdict": "REJECTED",
        "why": "the predicate would have to encode today's placeholder vocabulary, so "
               "a new sentinel from any scraper silently falls outside the index; it "
               "also cannot distinguish 'legitimate same number under two bodies' "
               "from 'duplicate ingest' without the body column in the predicate, "
               "which then excludes genuinely distinct items too.",
    },
    {
        "id": "A3-parent-row-lock",
        "mechanism": "SELECT ... FOR UPDATE over the meeting's existing rows before "
                     "inserting",
        "sufficient": False,
        "viable_here": False,
        "verdict": "INSUFFICIENT",
        "why": "it locks rows that exist. The collision this plan can cause is two "
               "writers inserting the SAME ABSENT key, and no row lock can lock a row "
               "that is not there yet.",
    },
    {
        "id": "A4-exact-key-reservation",
        "mechanism": f"an additive {RESERVATION_TABLE}(meeting_db_id, "
                     f"agenda_item_number) with a primary key; the apply inserts a "
                     f"reservation before materialising",
        "sufficient": True,
        "viable_here": True,
        "verdict": "ADOPTED",
        "why": "the uniqueness lives in a new table, so not one existing row has to "
               "change. Two concurrent writers racing on the same key collide on the "
               "reservation primary key and one aborts; the reservation is additive, "
               "reversible and exactly plan-bound.",
    },
    {
        "id": "A5-advisory-lock",
        "mechanism": "pg_advisory_xact_lock over the hashed (meeting, number) key "
                     "before the occupancy read",
        "sufficient": False,
        "viable_here": True,
        "verdict": "ADOPTED (in combination)",
        "why": "it serializes writers per key so the occupancy check and the insert "
               "cannot interleave, but it enforces nothing on its own: a writer that "
               "skips the lock, or a direct SQL client, is unaffected. Paired with A4 "
               "it removes the retry storm without being the guarantee.",
    },
    {
        "id": "A6-serializable-conflict-check",
        "mechanism": "read the natural key inside a SERIALIZABLE transaction, then "
                     "insert; on 40001 rerun the whole unit",
        "sufficient": False,
        "viable_here": True,
        "verdict": "ADOPTED (already in force)",
        "why": "PostgreSQL SSI does detect the read-write conflict and aborts one "
               "side, so this is real protection for a read-then-insert unit. It is "
               "NOT sufficient alone: two transactions that never read the other's "
               "key, or a non-SERIALIZABLE client, can still both insert. It is the "
               "retry path, not the invariant.",
    },
)

#: The adopted combination, and exactly what carries the invariant.
RECOMMENDED = {
    "invariant_carrier": "A4-exact-key-reservation",
    "supporting": ("A5-advisory-lock", "A6-serializable-conflict-check"),
    "reasoning": "The uniqueness Stage 2 needs is NOT the uniqueness of the historical "
                 "data — it is the uniqueness of the keys this apply would CREATE. "
                 "A reservation table expresses exactly that, additively, so the "
                 "legacy placeholder and multi-body numbering that already exists "
                 "stays untouched and correct.",
    "fail_closed": "Every path still refuses: an already-reserved key, an occupied "
                   "live key, an unresolved group, a missing plan digest and a "
                   "missing postcondition all raise rather than proceeding.",
}


def reservation_key(meeting_db_id: int, agenda_item_number: str) -> str:
    """A stable advisory-lock and reservation name for one natural key."""
    return f"{int(meeting_db_id)}|{agenda_item_number}"


def reservation_digest(plan_digest: str, keys: Iterable[str]) -> str:
    return hashlib.sha256(
        json.dumps({"plan": plan_digest, "keys": sorted(keys)},
                   sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def reservation_operations(*, plan_digest: str,
                           requested: Sequence[Mapping[str, Any]],
                           existing_live_keys: Iterable[str],
                           already_reserved: Iterable[str]) -> dict[str, Any]:
    """Split requested keys into deterministic inserts and human holds.

    A key that a live row already occupies, or that is already reserved, is a
    **hold**: it is a decision for a person, never something to overwrite.  The
    result is deterministic given the same inputs, so it can be diffed between runs.
    """
    if not plan_digest:
        raise ValueError("the reservation must be bound to a plan digest")
    live = set(existing_live_keys)
    reserved = set(already_reserved)
    inserts: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in requested:
        key = reservation_key(int(entry["meeting_db_id"]),
                              str(entry["agenda_item_number"]))
        record = {"key": key, "meeting_db_id": int(entry["meeting_db_id"]),
                  "agenda_item_number": str(entry["agenda_item_number"]),
                  "claimed_by": str(entry.get("claimed_by") or "")}
        if key in seen:
            holds.append({**record, "reason": "requested twice in one plan"})
            continue
        seen.add(key)
        if key in reserved:
            holds.append({**record, "reason": "already reserved by an earlier apply"})
        elif key in live:
            holds.append({**record, "reason": "a live agenda item already holds this key"})
        else:
            inserts.append(record)
    return {
        "plan_digest": plan_digest,
        "inserts": sorted(inserts, key=lambda r: r["key"]),
        "holds": sorted(holds, key=lambda r: r["key"]),
        "insert_key_digest": reservation_digest(plan_digest,
                                                [r["key"] for r in inserts]),
        "deterministic": True,
    }


def build_contract(*, measured: Mapping[str, Any]) -> dict[str, Any]:
    """The reviewed contract, bound to what the investigation measured."""
    contract = {
        "kind": CONTRACT_KIND,
        "version": CONTRACT_VERSION,
        "alternatives": [dict(a) for a in ALTERNATIVES],
        "recommended": dict(RECOMMENDED),
        "reservation_table": {
            "name": RESERVATION_TABLE,
            "columns": {"meeting_db_id": "integer NOT NULL",
                        "agenda_item_number": "varchar NOT NULL",
                        "plan_digest": "varchar NOT NULL",
                        "reserved_at": "timestamptz NOT NULL"},
            "primary_key": ["meeting_db_id", "agenda_item_number"],
            "additive": True,
            "rewrites_existing_rows": False,
        },
        "evidence": dict(measured),
        "guarantees": [
            "two concurrent writers racing on one key: exactly one reservation wins, "
            "the other aborts on the primary key",
            "a key a live row already occupies is HELD, never overwritten",
            "an already-reserved key is HELD, so a replay cannot double-reserve",
            "the apply still fails closed on any unresolved group",
        ],
        "non_guarantees": [
            "a client that bypasses the reservation table is not constrained by it",
            "the contract does not make the historical data unique, and does not "
            "claim to",
        ],
        "write_path": "absent by design",
    }
    contract["contract_digest"] = hashlib.sha256(
        json.dumps({k: v for k, v in contract.items()
                    if k not in ("contract_digest", ARTIFACT_DIGEST_FIELD)},
                   sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()
    problems = validate_contract(contract)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return contract


def validate_contract(contract: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if contract.get("kind") != CONTRACT_KIND:
        problems.append(f"kind must be {CONTRACT_KIND!r}")
    if contract.get("version") != CONTRACT_VERSION:
        problems.append(f"version must be {CONTRACT_VERSION!r}")
    if contract.get("write_path") != "absent by design":
        problems.append("the contract must declare no write path")
    alternatives = contract.get("alternatives") or []
    ids = [a.get("id") for a in alternatives]
    if sorted(ids) != sorted(a["id"] for a in ALTERNATIVES):
        problems.append("the alternatives are not the full evaluated set")
    for alternative in alternatives:
        for field in ("mechanism", "verdict", "why"):
            if not alternative.get(field):
                problems.append(f"{alternative.get('id')}: {field!r} is missing")
        if alternative.get("verdict") not in ("ADOPTED", "REJECTED", "INSUFFICIENT",
                                              "ADOPTED (in combination)",
                                              "ADOPTED (already in force)"):
            problems.append(f"{alternative.get('id')}: unregistered verdict")
    recommended = contract.get("recommended") or {}
    if not recommended.get("invariant_carrier"):
        problems.append("the contract names no invariant carrier")
    if not recommended.get("fail_closed"):
        problems.append("the contract does not state how it fails closed")
    guarantees = contract.get("guarantees") or []
    non_guarantees = contract.get("non_guarantees") or []
    if not guarantees or not non_guarantees:
        problems.append("the contract must state both guarantees and non-guarantees")
    body = {k: v for k, v in contract.items()
            if k not in ("contract_digest", ARTIFACT_DIGEST_FIELD)}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()
    if contract.get("contract_digest") != digest:
        problems.append("the recorded contract digest is not canonical")
    return problems
