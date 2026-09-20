#!/usr/bin/env python3
"""Offline, non-mutating propagation PLAN builder — operation-specific G7 step.

WHAT THIS IS
    A plan builder for exactly one operation kind: ``OP-REPAIR`` of the
    dev→prod reference propagation described by
    ``scripts/ops/propagation_contract.py``.

    It consumes EXPLICIT fixture/snapshot inputs (JSON files), computes the ordered
    operations and their expected postconditions, and writes a unique, immutable,
    digest-bound plan.

WHAT THIS IS NOT
    * It is **not** an authorization and cannot be mistaken for one. The emitted
      plan carries ``"authorization": "none"`` and states that it authorizes nothing.
    * It is **not** a general planner. It handles this one operation kind only;
      checklist blocker B2 (a general immutable operation-plan builder) remains open.
    * It **applies nothing**. There is no apply path here at all.
    * It **never connects to production**. There is no URL/host/DSN option and no
      database import; the only inputs are local files.

GUARANTEES
    * refuses placeholders (including a placeholder rollback owner),
    * refuses ambiguity, missing parents, sentinels and retired codes (delegated to
      the contract, which fails closed),
    * refuses drift when the observed target fingerprint differs from the expected
      one supplied by the caller,
    * refuses to overwrite an existing plan (exclusive creation, mode 0600),
    * writes a unique path: ``<out-dir>/<operation>-<UTCSTAMP>-<digest8>.json``.

Exit codes: 0 plan written · 3 refused (fail-closed) · 4 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from propagation_contract import (  # noqa: E402
    OPERATION_KIND,
    PLACEHOLDERS,
    contract_digest,
    evaluate_propagation,
    ordering_problems,
)

PLAN_SCHEMA = "propagation-plan/1"
INPUT_SCHEMA = "propagation-snapshot/1"
AUTHORIZATION_NONE = "none - this plan authorizes nothing"

# The dependency authority for this operation: parents before dependents.
APPLY_ORDER = ("jurisdictions", "public_bodies", "meetings")


class Refused(Exception):
    """Fail-closed refusal."""


def _looks_like_placeholder(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return True
    return any(marker in text for marker in PLACEHOLDERS)


def load_snapshot(path: Path) -> dict:
    """Read an explicit local snapshot. Never opens a connection."""
    if not path.exists():
        raise Refused(f"snapshot input not found: {path}")
    try:
        raw = path.read_text()
    except OSError as exc:
        raise Refused(f"snapshot unreadable: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refused(f"snapshot is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise Refused("snapshot must be a JSON object")
    if payload.get("snapshot_schema") != INPUT_SCHEMA:
        raise Refused(
            f"snapshot_schema must be {INPUT_SCHEMA!r}; refusing an unrecognised input"
        )
    if payload.get("live_connection") in (True, "true", "yes"):
        raise Refused(
            "snapshot declares a live connection; this builder consumes explicit "
            "fixture/snapshot inputs only"
        )
    for key in ("incoming_parents", "dependents"):
        if not isinstance(payload.get(key), list):
            raise Refused(f"snapshot field {key!r} must be a list")
    return payload


def build_plan(snapshot: dict, *, rollback_owner: str,
               expect_target_fingerprint: str | None = None,
               now: datetime | None = None) -> dict:
    """Validate and build the plan body. Raises Refused on any problem."""
    now = now or datetime.now(timezone.utc)

    if _looks_like_placeholder(rollback_owner):
        raise Refused(f"rollback owner is a placeholder: {rollback_owner!r}")

    source_fingerprint = contract_digest(snapshot)
    target_state = {
        "existing_parents": snapshot.get("existing_parents", []),
        "retired_codes": sorted(snapshot.get("retired_codes", [])),
    }
    target_fingerprint = contract_digest(target_state)

    if expect_target_fingerprint and expect_target_fingerprint != target_fingerprint:
        raise Refused(
            "target drift: expected fingerprint "
            f"{expect_target_fingerprint} but observed {target_fingerprint}"
        )

    result = evaluate_propagation(
        incoming_parents=snapshot["incoming_parents"],
        dependents=snapshot["dependents"],
        existing_parents=snapshot.get("existing_parents", []),
        retired_codes=snapshot.get("retired_codes", []),
        aliases=snapshot.get("aliases", {}),
        dependent_table=snapshot.get("dependent_table", "meetings"),
    )
    if not result.ok:
        raise Refused("; ".join(result.problems))

    order_problems = ordering_problems(APPLY_ORDER)
    if order_problems:
        raise Refused("; ".join(order_problems))

    parents = [op for op in result.operations if op["op"] == "upsert_parent"]
    dependents = [op for op in result.operations if op["op"] == "upsert_dependent"]

    body = {
        "plan_schema": PLAN_SCHEMA,
        "operation_kind": OPERATION_KIND,
        "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "authorization": AUTHORIZATION_NONE,
        "source_fingerprint": source_fingerprint,
        "target_fingerprint": target_fingerprint,
        "apply_order": list(APPLY_ORDER),
        "ordered_operations": parents + dependents,
        "expected_counts": {
            "parents_upserted": len(parents),
            "dependents_updated": len(dependents),
            "total": len(parents) + len(dependents),
        },
        "expected_postconditions": [
            "zero remaining scoped dangling references (repair scope) — matches the "
            "runtime's scoped postcondition, not merely 'no new' dangling rows",
            "unrelated rows unchanged",
            "every propagated dependent has a satisfied parent in BOTH "
            "representations: body/body_code -> public_bodies.body_code AND "
            "public_body_id -> public_bodies.id",
            "sentinels ('', '__skip__') are invalid/unresolved and are never promoted",
        ],
        "transfer_strategy": {
            "reference_tables_full_reference": ["jurisdictions", "public_bodies"],
            "note": "reference parents are re-sent in full every sync, so a parent "
                    "older than the checkpoint is still transferred",
            "audit_stamping": "separate concern: body-code writes must still bump "
                              "updated_at (defense in depth)",
        },
        "runtime_alignment": (
            "plan-only: this plan describes what db.sync_reference enforces at "
            "runtime (parent-first apply, fail-closed abort on parent skip, "
            "postconditions for both representations). It does NOT apply or "
            "enforce anything itself."
        ),
        "transaction_scope": (
            "ordinary sync commits per chunk and per table; referentially safe "
            "STAGED execution is what is implemented, not cross-table atomicity"
        ),
        "rollback_owner": rollback_owner,
        "applies_anything": False,
    }
    body["plan_digest"] = contract_digest(body)
    return body


def write_plan(plan: dict, out_dir: Path) -> Path:
    """Write the plan once, exclusively. Refuse to overwrite anything."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = plan["created_utc"].replace("-", "").replace(":", "")
    name = f"{OPERATION_KIND.lower().replace('op-', '')}-propagation-{stamp}-" \
           f"{plan['plan_digest'][:8]}.json"
    path = out_dir / name
    payload = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise Refused(f"plan already exists; refusing to overwrite: {path}") from exc
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an offline, non-mutating propagation plan (OP-REPAIR only)")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a plan from a local snapshot")
    build.add_argument("--input", required=True, help="path to a snapshot JSON file")
    build.add_argument("--out-dir", default="data/plans")
    build.add_argument("--rollback-owner", required=True)
    build.add_argument("--expect-target-fingerprint", default=None)
    build.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command != "build":
        return 4

    try:
        snapshot = load_snapshot(Path(args.input))
        plan = build_plan(
            snapshot,
            rollback_owner=args.rollback_owner,
            expect_target_fingerprint=args.expect_target_fingerprint,
        )
        path = write_plan(plan, Path(args.out_dir))
    except Refused as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 3

    if args.json:
        print(json.dumps({"plan": str(path), "plan_digest": plan["plan_digest"],
                          "authorization": AUTHORIZATION_NONE},
                         sort_keys=True))
    else:
        print(f"plan written: {path}")
        print(f"plan digest:  {plan['plan_digest']}")
        print(f"operations:   {plan['expected_counts']['total']}")
        print("authorization: none - this plan authorizes nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
