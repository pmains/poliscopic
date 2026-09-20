#!/usr/bin/env python3
"""``stage2_subitem_data_apply.py`` — apply the 459 PART_OF edges, once.

Every rule the schema could not express as a CHECK is enforced here, inside ONE
SERIALIZABLE transaction: same-meeting endpoints, strict number shortening, no
self-links, no cycles, unique child ownership, exact operation-set equality and
collision disjointness.  Nothing commits unless every postcondition holds.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_subitem_data_plan as planning  # noqa: E402
from scripts.kg import stage2_backup_verify as backup_verify  # noqa: E402

__all__ = ["ApplyRefused", "RECEIPT_KIND", "apply_plan"]

RECEIPT_KIND = "kg-stage2-subitem-data-receipt"
PREIMAGE_KIND = "kg-stage2-subitem-data-preimage"
ADVISORY_LOCK = 0x5A2D_0001


class ApplyRefused(RuntimeError):
    """The apply was refused; nothing was modified."""


class _DryRunRollback(Exception):
    """Internal sentinel: every check passed and the transaction is deliberately undone."""


def _write(out_dir: str | Path, name: str, payload: Mapping[str, Any]):
    path = Path(out_dir) / name
    return path, artifacts.write_immutable(path, dict(payload))


def require_backup(receipt_path: str | Path, *, target: Mapping[str, Any]) -> dict:
    path = Path(receipt_path)
    if not path.exists():
        raise ApplyRefused(f"backup receipt {path.name!r} does not exist")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ApplyRefused("the backup receipt mode is not 0o600")
    receipt = json.loads(path.read_text())
    if dict(receipt.get("target") or {}).get("database") != target.get("database"):
        raise ApplyRefused("the backup was taken from a different database")
    if not receipts.restore_verified(receipt):
        raise ApplyRefused("the backup receipt proves no verified restore")
    stage2_problems = backup_verify.validate_stage2_receipt(receipt)
    if stage2_problems:
        raise ApplyRefused("the backup receipt is not Stage-2 verified: "
                           + "; ".join(stage2_problems[:3]))
    dump = Path(str(receipt.get("dump_path") or ""))
    if not dump.exists():
        raise ApplyRefused("the backup receipt names a missing dump")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    if digest != receipt.get("dump_sha256"):
        raise ApplyRefused("the dump hash does not match the receipt")
    return {"path": path.name,
            "canonical_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "dump_sha256": digest, "restore_proven": True}


def live_containment_plan() -> Path:
    """The single live containment-plan head, by the repo's supersede convention."""
    plans = REPO / "data" / "kg-plans"
    heads = [p for p in sorted(plans.glob("kg-stage2-subitem-containment-plan-*.json"))
             if not p.name.endswith(".obsolete.json")
             and not (plans / (p.name + ".obsolete.json")).exists()]
    if len(heads) != 1:
        raise ApplyRefused(f"expected one containment-plan head, found {len(heads)}")
    return heads[0]


def _non_null_count(connection: Any) -> int:
    return int(connection.execute(text(
        f"SELECT COUNT(*) FROM {planning.TABLE} WHERE parent_item_id IS NOT NULL"
    )).scalar())


def _protected(connection: Any) -> dict[str, int]:
    return {t: int(connection.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar())
            for t in planning.PROTECTED_TABLES}


def _check_cycles(connection: Any, ops: list[dict]) -> None:
    links = {int(o["child_item_id"]): int(o["parent_item_id"]) for o in ops}
    numbers = dict(connection.execute(text(
        f"SELECT id, agenda_item_number FROM {planning.TABLE} "
        "WHERE id = ANY(:ids)"), {"ids": list(links) + list(links.values())}).all())
    for child in links:
        seen = {child}
        node = links[child]
        depth = 0
        while node in links:
            if node in seen:
                raise ApplyRefused(f"cycle detected through item {node}")
            seen.add(node)
            node = links[node]
            depth += 1
            if depth > 8:
                raise ApplyRefused("parent chain is implausibly deep")
        if not (str(numbers[child]).startswith(str(numbers[node]) + ".")
                and len(str(numbers[child])) > len(str(numbers[node]))):
            raise ApplyRefused(f"chain through {child} does not shorten")


def apply_plan(engine: Any, plan: Mapping[str, Any], *, supplied_digest: str,
               backup_receipt: str | Path, out_dir: str | Path,
               dry_run: bool = False) -> dict[str, Any]:
    """Apply the plan.  With ``dry_run`` every gate and update runs, then rolls back."""
    if artifacts.compute_digest(plan) != supplied_digest:
        raise ApplyRefused("the plan digest changed after loading")
    problems = planning.validate_plan(plan)
    if problems:
        raise ApplyRefused("plan refused: " + "; ".join(problems[:4]))
    if engine.dialect.name != "postgresql":
        raise ApplyRefused(f"unsupported dialect: {engine.dialect.name!r}")
    identity = {"dialect": engine.url.get_backend_name(), "host": engine.url.host,
                "port": engine.url.port, "database": engine.url.database}
    for field in ("dialect", "host", "port", "database"):
        if (plan.get("target") or {}).get(field) != identity[field]:
            raise ApplyRefused(f"engine {field} disagrees with the plan")

    ops = list(plan["operations"])
    plan_id = supplied_digest[:16]
    terminal = Path(out_dir) / f"kg-stage2-subitem-data-receipt-{plan_id}.json"

    if terminal.exists():
        with engine.connect() as connection:
            live = {int(r[0]): int(r[1]) for r in connection.execute(text(
                f"SELECT id, parent_item_id FROM {planning.TABLE} "
                "WHERE parent_item_id IS NOT NULL"))}
        expected = {int(o["child_item_id"]): int(o["parent_item_id"]) for o in ops}
        if live != expected:
            raise ApplyRefused("a terminal receipt exists but the live edges disagree")
        receipt = {"kind": RECEIPT_KIND, "stage": "replay-no-op", "writes": 0,
                   "plan_digest": supplied_digest, "replay": True,
                   "recorded_at": datetime.now(timezone.utc).isoformat()}
        path, digest = _write(out_dir,
                              f"kg-stage2-subitem-data-replay-{plan_id}.json", receipt)
        return {**receipt, "receipt_path": str(path), "receipt_digest": digest}

    backup = require_backup(backup_receipt, target=plan.get("target") or {})
    bindings = plan["bindings"]

    from scripts.kg import stage2_subitem_schema_plan as schema_plan

    with engine.connect() as connection:
        live_sig = schema_plan.schema_signature(connection)
        before_counts = _protected(connection)
        before_fp = planning.table_fingerprint(connection)
        before_non_null = _non_null_count(connection)
    if live_sig["digest"] != (bindings.get("schema_signature") or {}).get("digest"):
        raise ApplyRefused("the live schema is not the schema the plan recorded")
    if before_fp != bindings.get("table_fingerprint"):
        raise ApplyRefused("the identity columns have changed since the plan was built")
    if before_non_null != 0:
        raise ApplyRefused(f"{before_non_null} rows already carry a parent_item_id")

    # Re-verify the containment plan itself, then exclude colliding identities.
    containment = artifacts.load_verified(live_containment_plan())
    if artifacts.recorded_digest(containment) != bindings.get("containment_plan_digest"):
        raise ApplyRefused("the containment plan digest has drifted")
    held = (containment.get("collision_population") or {}).get("row_ids") or []
    collision_ids = {int(i) for i in held}
    if planning.canonical_sha256(sorted(collision_ids)) != (
            bindings.get("collision_population") or {}).get("row_ids_sha256"):
        raise ApplyRefused("the collision population has drifted")

    with engine.connect() as connection:
        derived = planning.rederive_operations(connection,
                                               collision_row_ids=collision_ids)
    planned = sorted((int(o["child_item_id"]), int(o["parent_item_id"])) for o in ops)
    live_pairs = sorted((o["child_item_id"], o["parent_item_id"])
                        for o in derived["operations"])
    if planned != live_pairs:
        raise ApplyRefused(
            f"operation set drift: planned={len(planned)} live={len(live_pairs)}")
    collision_rows = sorted(collision_ids)
    preimage, preimage_digest = _write(
        out_dir, f"kg-stage2-subitem-data-preimage-{plan_id}.json", {
            "kind": PREIMAGE_KIND, "plan_digest": supplied_digest,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schema_signature": live_sig, "protected_row_counts": before_counts,
            "table_fingerprint": before_fp, "non_null_parent_item_id": before_non_null,
            "operation_count": len(ops), "collision_rows": collision_rows,
            "operations": ops, "backup_receipt": backup})

    applied = 0
    rolled_back = False
    connection = engine.connect().execution_options(isolation_level="SERIALIZABLE")
    try:
        with connection.begin():
            connection.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                               {"k": ADVISORY_LOCK})
            for op in ops:
                child, parent = int(op["child_item_id"]), int(op["parent_item_id"])
                if child == parent:
                    raise ApplyRefused(f"self-link refused for item {child}")
                rows = {int(r["id"]): dict(r) for r in connection.execute(text(
                    f"SELECT id, meeting_db_id, agenda_item_number, parent_item_id "
                    f"FROM {planning.TABLE} WHERE id IN (:c, :p)"),
                    {"c": child, "p": parent}).mappings().all()}
                if child not in rows or parent not in rows:
                    raise ApplyRefused(f"operation {child} names a missing row")
                c_row, p_row = rows[child], rows[parent]
                if c_row["parent_item_id"] is not None:
                    raise ApplyRefused(f"item {child} is already parented")
                if c_row["meeting_db_id"] != p_row["meeting_db_id"]:
                    raise ApplyRefused(f"operation {child} crosses meetings")
                cnum, pnum = str(c_row["agenda_item_number"]), str(p_row["agenda_item_number"])
                if not (cnum.startswith(pnum + ".") and len(cnum) > len(pnum)):
                    raise ApplyRefused(f"operation {child} does not shorten")
                if int(c_row["id"]) in collision_ids:
                    raise ApplyRefused(f"item {child} is a colliding identity")
            owned = [int(o["child_item_id"]) for o in ops]
            if len(set(owned)) != len(owned):
                raise ApplyRefused("a child is claimed by more than one operation")
            for op in ops:
                result = connection.execute(text(
                    f"UPDATE {planning.TABLE} SET parent_item_id = :p WHERE id = :c"),
                    {"p": int(op["parent_item_id"]), "c": int(op["child_item_id"])})
                applied += int(result.rowcount or 0)
            if applied != len(ops):
                raise ApplyRefused(f"updated {applied} rows, expected {len(ops)}")
            after_non_null = _non_null_count(connection)
            if after_non_null != len(ops):
                raise ApplyRefused(
                    f"non-null parent count is {after_non_null}, expected {len(ops)}")
            _check_cycles(connection, ops)
            if planning.table_fingerprint(connection) != before_fp:
                raise ApplyRefused("identity columns changed during the apply")
            after_counts = _protected(connection)
            if after_counts != before_counts:
                raise ApplyRefused(f"protected counts changed: {before_counts} -> {after_counts}")
            signature_after = schema_plan.schema_signature(connection)
            if dry_run:
                raise _DryRunRollback()
    except _DryRunRollback:
        rolled_back = True
    finally:
        connection.close()

    if rolled_back:
        with engine.connect() as check:
            residue = _non_null_count(check)
        if residue != before_non_null:
            raise ApplyRefused("the dry run left rows modified")
        return {"kind": RECEIPT_KIND, "stage": "dry-run-rolled-back", "writes": 0,
                "plan_digest": supplied_digest, "operations": len(ops),
                "would_update": applied, "non_null_before": before_non_null,
                "non_null_after": residue, "replay": False}

    receipt = {"kind": RECEIPT_KIND, "stage": "terminal", "writes": applied,
               "plan_digest": supplied_digest, "relation": planning.RELATION,
               "applied_at": datetime.now(timezone.utc).isoformat(),
               "engine_target": identity, "backup_receipt": backup,
               "preimage_artifact": preimage.name, "preimage_digest": preimage_digest,
               "operations": len(ops), "updated_rows": applied,
               "non_null_before": before_non_null, "non_null_after": len(ops),
               "protected_row_counts_before": before_counts,
               "protected_row_counts_after": after_counts,
               "table_fingerprint_unchanged": True,
               "schema_signature_after_digest": signature_after["digest"],
               "child_item_ids_sha256": hashlib.sha256(
                   ",".join(str(i) for i in sorted(owned)).encode()).hexdigest(),
               "postconditions": {"updated_rows": applied,
                                  "non_null_equals_operations": True,
                                  "identity_columns_unchanged": True,
                                  "protected_unchanged": True, "no_cycles": True},
               "replay": False}
    path, digest = _write(out_dir,
                          f"kg-stage2-subitem-data-receipt-{plan_id}.json", receipt)
    return {**receipt, "receipt_path": str(path), "receipt_digest": digest}
