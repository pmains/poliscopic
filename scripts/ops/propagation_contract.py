#!/usr/bin/env python3
"""Authoritative reference-propagation contract for dev→prod reconciliation.

WHY THIS EXISTS
    Development consolidated Chandler and Mesa body codes; production received
    meeting rows without all corresponding ``public_bodies`` reference rows, leaving
    meetings pointing at absent body codes. This module makes that state impossible
    to produce silently, as ONE general invariant rather than a Chandler/Mesa
    special case. The four observed codes are regression examples only and are
    deliberately NOT hard-coded here.

ESTABLISHED REFERENCE SEMANTICS (traced from schema and sync code, not assumed)
    There are TWO live representations of "a dependent row's body", and the contract
    covers both:

      1. CODE STRING — ``<table>.body`` / ``<table>.body_code`` (VARCHAR) referencing
         ``public_bodies.body_code``. ``meetings.body`` is NOT NULL with default "".
      2. INTEGER FK — ``<table>.public_body_id`` (Integer) referencing
         ``public_bodies.id``. Nullable on meetings/agenda_items; NOT NULL on
         body_seats/body_memberships.

    No ForeignKey constraint is declared for ``public_body_id`` anywhere in the
    models, so the database does NOT enforce representation (2). This contract does.

    Sentinels ``"__skip__"`` and ``""`` are scrape-level placeholders that must never
    be promoted into ``public_bodies``.

ORDERING
    Upserts are parents-first; reconcile deletes are children-first. Both directions
    are declared here as the single dependency authority.

SCOPE
    Pure computation plus a fixture-only transactional applier. No network, no
    production connection, no credentials, and no non-fixture apply path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# Make the shared db modules importable for the reconcile-order derivation.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# ── the dependency authority (one declaration, used everywhere) ──────────

#: Values that must never be promoted into ``public_bodies``.
SENTINELS: frozenset[str] = frozenset({"__skip__", ""})

#: Columns that carry a body CODE (representation 1).
BODY_CODE_COLUMNS: tuple[str, ...] = ("body", "body_code")

#: Columns that carry the INTEGER FK (representation 2).
PUBLIC_BODY_ID_COLUMN = "public_body_id"

#: Every SYNCED table that carries a public-body reference, and WHICH
#: representation(s) it uses. Traced from the live models and reviewed:
#:
#:   body / body_code   -> public_bodies.body_code   (code string)
#:   public_body_id     -> public_bodies.id          (integer FK)
#:
#: ``_ingest_failures`` is excluded: it is local-only and never propagated.
#: ``public_bodies`` is the PARENT here, not a dependent of itself.
#:
#: This is the runtime authority. A new reference-bearing synced table that is not
#: listed here must fail the metadata parity test rather than silently sync without
#: its parent.
PUBLIC_BODY_DEPENDENTS: dict[str, tuple[str, ...]] = {
    "agenda_items": ("body", "public_body_id"),
    "agenda_item_votes": ("body",),
    "body_memberships": ("public_body_id",),
    "body_seats": ("public_body_id",),
    "case_events": ("body",),
    "executive_session_participants": ("body",),
    "meeting_attendance": ("body",),
    "meeting_members": ("body",),
    "meetings": ("body", "public_body_id"),
    "member_votes": ("body",),
    "pz_item_details": ("body",),
    "supporting_documents": ("body",),
}

#: The public-body registry and the jurisdiction registry it depends on.
PUBLIC_BODY_PARENT = "public_bodies"
PUBLIC_BODY_GRANDPARENT = "jurisdictions"

#: (parent, dependent) — parents must be applied before dependents.
#: Derived from the reviewed map above so the two can never disagree.
DEPENDENCY_EDGES: tuple[tuple[str, str], ...] = (
    (PUBLIC_BODY_GRANDPARENT, PUBLIC_BODY_PARENT),
) + tuple((PUBLIC_BODY_PARENT, t) for t in sorted(PUBLIC_BODY_DEPENDENTS))

#: Reference tables this contract reasons about, in children-first order.
_RECONCILE_SCOPE: tuple[str, ...] = (
    "body_memberships", "body_seats", "meeting_members", "agenda_items",
    "agenda_item_votes", "case_events", "executive_session_participants",
    "meeting_attendance", "member_votes", "pz_item_details",
    "supporting_documents", "meetings", "public_bodies", "jurisdictions",
)


def _reconcile_order() -> tuple[str, ...]:
    """Derived projection of the SINGLE reconcile authority.

    ``db.sync_declarations.RECONCILE_ORDER`` owns the operational children-first
    order for every synced table. This module previously duplicated a 7-entry
    subset, which could drift from it. It is now DERIVED from that authority and
    filtered to the reference tables above, so there is one source of truth.

    A parity test asserts the projection stays consistent with the authority
    (shared tables appear in the same relative order).
    """
    try:
        from db.sync_declarations import RECONCILE_ORDER as declared
    except Exception:  # standalone import, i.e. scripts/ not on sys.path
        return _RECONCILE_SCOPE
    scope = set(_RECONCILE_SCOPE)
    return tuple(t for t in declared if t in scope)


def __getattr__(name: str):
    """Derive ``RECONCILE_ORDER`` LAZILY.

    Deliberately lazy: importing ``db.sync_declarations`` pulls in the shared db
    config, which prints a startup banner to STDOUT at import time. Deriving this
    eagerly at module import therefore polluted the stdout of every CLI that
    imports this module — it broke ``plan_propagation.py --json`` by prepending a
    banner line to the JSON payload. Deferring the import until the attribute is
    actually requested keeps importing this module side-effect free.
    """
    if name == "RECONCILE_ORDER":
        return _reconcile_order()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

OPERATION_KIND = "OP-REPAIR"

PLACEHOLDERS = ("tbd", "todo", "placeholder", "xxx", "unknown", "<", ">", "changeme")


def is_sentinel(value: Any) -> bool:
    """True when ``value`` is a placeholder rather than a real code."""
    if value is None:
        return True
    return str(value) in SENTINELS or not str(value).strip()


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _identity(record: Mapping[str, Any]) -> str:
    """The identity a parent must match on. Prefer an explicit identity field."""
    for key in ("identity", "name", "display_name"):
        if key in record and not is_sentinel(record.get(key)):
            return _norm(record[key])
    return ""


# ── problems ─────────────────────────────────────────────────────────────


@dataclass
class PropagationResult:
    problems: list[str] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _parent_index(existing: Sequence[Mapping[str, Any]],
                  incoming: Sequence[Mapping[str, Any]]) -> tuple[
                      dict[str, list[Mapping[str, Any]]],
                      dict[int, Mapping[str, Any]]]:
    by_code: dict[str, list[Mapping[str, Any]]] = {}
    by_id: dict[int, Mapping[str, Any]] = {}
    for record in list(existing) + list(incoming):
        code = _norm(record.get("body_code"))
        if code:
            by_code.setdefault(code, []).append(record)
        pid = record.get("id")
        if isinstance(pid, int):
            if pid in by_id and by_id[pid] is not record:
                # same integer id declared twice — ambiguous
                by_id[pid] = record
            else:
                by_id[pid] = record
    return by_code, by_id


def evaluate_propagation(
    *,
    incoming_parents: Sequence[Mapping[str, Any]],
    dependents: Sequence[Mapping[str, Any]],
    existing_parents: Sequence[Mapping[str, Any]] = (),
    retired_codes: Iterable[str] = (),
    aliases: Mapping[str, str] | None = None,
    dependent_table: str = "meetings",
) -> PropagationResult:
    """Evaluate the propagation contract. Fail-closed: any problem blocks.

    ``incoming_parents`` / ``existing_parents`` records carry ``body_code``, ``id``
    and some identity (``identity``/``name``). ``dependents`` carry a row key plus
    ``body`` and/or ``public_body_id``.
    """
    result = PropagationResult()
    retired = {_norm(c) for c in retired_codes if not is_sentinel(c)}
    alias_map = {_norm(k): _norm(v) for k, v in (aliases or {}).items()}
    by_code, by_id = _parent_index(existing_parents, incoming_parents)

    # ── duplicate aliases / ambiguous mappings ───────────────────────────
    for code, records in by_code.items():
        identities = {_identity(r) for r in records}
        ids = {r.get("id") for r in records if isinstance(r.get("id"), int)}
        if len(ids) > 1:
            result.problems.append(
                f"duplicate alias: body_code {code!r} is declared with multiple "
                f"ids {sorted(ids)} — ambiguous mapping"
            )
        if len(identities) > 1:
            result.problems.append(
                f"conflicting parent identity for body_code {code!r}: "
                f"{sorted(identities)}"
            )
    for src, dst in alias_map.items():
        if src in by_code and dst in by_code and src != dst:
            result.problems.append(
                f"ambiguous mapping: alias {src!r}->{dst!r} where both are registered"
            )

    # ── parent-side sanity ───────────────────────────────────────────────
    for record in incoming_parents:
        code = _norm(record.get("body_code"))
        if is_sentinel(code):
            result.problems.append(
                f"refusing to promote sentinel/empty body_code {code!r} "
                f"into public_bodies"
            )
        if code in retired:
            result.problems.append(
                f"refusing to promote retired body_code {code!r}"
            )

    # ── dependent-side checks ────────────────────────────────────────────
    needs_parent: list[tuple[Mapping[str, Any], str]] = []
    for row in dependents:
        key = row.get("key", row.get("id", "?"))
        raw_code = row.get("body", row.get("body_code"))
        fk = row.get("public_body_id")

        if is_sentinel(raw_code) and fk in (None, ""):
            result.problems.append(
                f"{dependent_table} {key}: sentinel/empty body with no public_body_id "
                f"— cannot resolve a parent"
            )
            continue

        code = _norm(raw_code)
        if code:
            # follow an alias only when it is unambiguous
            resolved = alias_map.get(code)
            if resolved is not None:
                if code in by_code and resolved in by_code and code != resolved:
                    continue  # already reported as ambiguous
                code = resolved
            if code in retired:
                result.problems.append(
                    f"{dependent_table} {key}: references retired code {code!r}"
                )
                continue
            if code not in by_code:
                result.problems.append(
                    f"{dependent_table} {key}: missing parent for code {code!r} "
                    f"— no public_bodies row selected or present"
                )
                continue
            # A clean replay is a genuine no-op: if the dependent already carries
            # the resolved code, no operation is emitted for it.
            current = _norm(row.get("current_body", row.get("current_body_code")))
            if current and current == code:
                continue
            needs_parent.append((row, code))

        if isinstance(fk, int):
            if fk not in by_id:
                result.problems.append(
                    f"{dependent_table} {key}: public_body_id {fk} has no parent row "
                    f"— integer FK representation is unsatisfied"
                )

    if result.problems:
        return result  # fail closed: no operations

    # ── ordered operations: parents first, then dependents ───────────────
    for record in incoming_parents:
        code = _norm(record.get("body_code"))
        if not code:
            continue
        result.operations.append({
            "op": "upsert_parent",
            "table": "public_bodies",
            "body_code": code,
            "identity": _identity(record),
        })
    for row, code in needs_parent:
        result.operations.append({
            "op": "upsert_dependent",
            "table": dependent_table,
            "key": row.get("key", row.get("id")),
            "requires_parent": code,
        })
    return result


def deletion_problems(
    *,
    parents_to_delete: Sequence[Mapping[str, Any]],
    remaining_dependents: Sequence[Mapping[str, Any]],
) -> list[str]:
    """A parent may not be removed while any dependent still references it."""
    problems: list[str] = []
    for parent in parents_to_delete:
        code = _norm(parent.get("body_code"))
        pid = parent.get("id")
        blockers = [
            row for row in remaining_dependents
            if _norm(row.get("body", row.get("body_code"))) == code
            or (isinstance(pid, int) and row.get("public_body_id") == pid)
        ]
        if blockers:
            keys = [row.get("key", row.get("id")) for row in blockers][:5]
            problems.append(
                f"cannot delete public_bodies {code!r}: {len(blockers)} dependent "
                f"row(s) still reference it (e.g. {keys})"
            )
    return problems


def postcondition_problems(
    *,
    dangling_before: Mapping[str, int],
    dangling_after: Mapping[str, int],
    unrelated_before: int | None = None,
    unrelated_after: int | None = None,
) -> list[str]:
    """Zero NEWLY introduced dangling references; unrelated state preserved."""
    problems: list[str] = []
    for code, count in dangling_after.items():
        if count > dangling_before.get(code, 0):
            problems.append(
                f"postcondition failed: {count} dangling reference(s) to {code!r} "
                f"(was {dangling_before.get(code, 0)})"
            )
    if unrelated_before is not None and unrelated_after is not None:
        if unrelated_before != unrelated_after:
            problems.append(
                f"postcondition failed: unrelated rows changed "
                f"({unrelated_before} -> {unrelated_after})"
            )
    return problems


def ordering_problems(tables: Sequence[str]) -> list[str]:
    """Parents must precede dependents in an apply order."""
    problems: list[str] = []
    for parent, dependent in DEPENDENCY_EDGES:
        if parent in tables and dependent in tables:
            if tables.index(parent) > tables.index(dependent):
                problems.append(
                    f"{parent!r} must precede {dependent!r} in the apply order"
                )
    return problems


# ── fixture-only transactional applier ───────────────────────────────────


def apply_transactionally(conn: sqlite3.Connection,
                          operations: Sequence[Mapping[str, Any]],
                          *,
                          fail_after: int | None = None) -> dict[str, Any]:
    """Apply operations to a DISPOSABLE SQLite fixture, atomically.

    Fixture-only by construction: the only accepted argument is a live
    ``sqlite3.Connection``, so this can never be aimed at PostgreSQL or production
    — there is no URL, path, or host parameter. ``fail_after`` simulates a
    mid-operation failure to prove zero partial state.

    Returns a receipt dict. On any failure the transaction is rolled back and the
    fixture is left exactly as it was.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError(
            "apply_transactionally is fixture-only and accepts only a sqlite3 "
            "Connection; refusing to apply elsewhere"
        )
    receipt: dict[str, Any] = {"applied": 0, "rolled_back": False, "error": None}
    try:
        with conn:  # commits on success, rolls back on exception
            for index, op in enumerate(operations):
                if fail_after is not None and index >= fail_after:
                    raise RuntimeError(f"simulated mid-operation failure at step {index}")
                if op.get("op") == "upsert_parent":
                    conn.execute(
                        "INSERT OR REPLACE INTO public_bodies (body_code, name) "
                        "VALUES (?, ?)",
                        (op["body_code"], op.get("identity", "")),
                    )
                elif op.get("op") == "upsert_dependent":
                    conn.execute(
                        "UPDATE meetings SET body = ? WHERE id = ?",
                        (op["requires_parent"], op["key"]),
                    )
                else:
                    raise RuntimeError(f"unknown operation {op.get('op')!r}")
                receipt["applied"] += 1
    except Exception as exc:  # noqa: BLE001 - surfaced in the receipt
        receipt["rolled_back"] = True
        receipt["error"] = str(exc)
        receipt["applied"] = 0
    return receipt


def contract_digest(payload: Mapping[str, Any]) -> str:
    """Stable SHA-256 over canonical JSON (used to bind plans)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
