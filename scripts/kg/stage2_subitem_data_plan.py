#!/usr/bin/env python3
"""``stage2_subitem_data_plan.py`` — immutable plan for the 459 PART_OF edges.

Binds the containment baseline, the containment plan, the live post-schema signature, the
schema receipt, the target, a fresh restore-verified backup, the safety-critical code, the
child/parent row fingerprints and the collision population — then rederives the operation
set independently from the live table.  If the rederivation disagrees with the containment
plan by even one edge, the plan refuses to build.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = ["CODE_MODULES", "NUMBERED", "PLAN_KIND", "build_plan", "code_hashes",
           "rederive_operations", "row_fingerprint", "table_fingerprint",
           "validate_plan"]

PLAN_KIND = "kg-stage2-subitem-data-plan"
PLAN_VERSION = "kg-stage2-subitem-data-plan/1.0"
TABLE = "agenda_items"
RELATION = "PART_OF"
NUMBERED = re.compile(r"^(?P<base>\w+)\.(?P<leaf>\w+)$")

CODE_MODULES = (
    "scripts/kg/stage2_subitem_data_plan.py",
    "scripts/kg/stage2_subitem_data_apply.py",
    "scripts/kg/stage2_subitem_containment.py",
    "scripts/kg/stage2_subitem_plan.py",
    "scripts/kg/stage2_subitem_schema_plan.py",
    "scripts/kg/stage2_backup_verify.py",
    "scripts/kg/stage2_artifacts.py",
)

TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")
PROTECTED_TABLES = ("agenda_items", "supporting_documents", "meetings")
UNCHANGED_COLUMNS = ("id", "meeting_db_id", "agenda_item_number")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def code_hashes(modules: Sequence[str] = CODE_MODULES) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in modules:
        p = REPO / rel
        if p.exists():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def row_fingerprint(row: Mapping[str, Any]) -> str:
    return canonical_sha256({c: row.get(c) for c in UNCHANGED_COLUMNS})


def table_fingerprint(connection: Any) -> str:
    """Identity columns of every row, ordered — proves nothing else moved."""
    rows = connection.execute(text(
        f"SELECT id, meeting_db_id, agenda_item_number FROM {TABLE} ORDER BY id"
    )).all()
    return canonical_sha256([list(r) for r in rows])


def _number_index(connection: Any) -> dict[tuple, list]:
    index: dict[tuple, list] = {}
    for r in connection.execute(text(
            f"SELECT id, meeting_db_id, agenda_item_number FROM {TABLE}")):
        index.setdefault((r[1], r[2]), []).append(r[0])
    return index


def rederive_operations(connection: Any, *, collision_row_ids: set[int]) -> dict:
    """Independently recompute the eligible PART_OF edges from the live table.

    A candidate child is ``N.L``; its parent is the single ``N`` in the same meeting.
    Rows whose own number is claimed twice, or whose base is claimed twice, are ambiguous
    and held.  Colliding identities are excluded, never guessed.
    """
    index = _number_index(connection)
    ops: list[dict] = []
    held_ambiguous = held_missing = held_collision = 0
    for (meeting, number), ids in index.items():
        m = NUMBERED.match(str(number))
        if not m:
            continue
        if len(ids) != 1:
            held_ambiguous += 1
            continue
        child_id = ids[0]
        if child_id in collision_row_ids:
            held_collision += 1
            continue
        parents = index.get((meeting, m.group("base")), [])
        if len(parents) == 0:
            held_missing += 1
            continue
        if len(parents) > 1:
            held_ambiguous += 1
            continue
        parent_id = parents[0]
        if parent_id == child_id:
            held_ambiguous += 1
            continue
        ops.append({"child_item_id": child_id, "parent_item_id": parent_id,
                    "meeting_db_id": meeting, "child_number": number,
                    "parent_number": m.group("base")})
    ops.sort(key=lambda o: o["child_item_id"])
    return {"operations": ops, "held": {"ambiguous": held_ambiguous,
                                        "missing_parent": held_missing,
                                        "collision": held_collision}}


def build_plan(connection: Any, *, target: Mapping[str, Any], created_at: str,
               baseline: Mapping[str, Any], baseline_digest: str,
               containment: Mapping[str, Any], containment_digest: str,
               schema_receipt_digest: str, backup: Mapping[str, Any],
               safety_modules: Sequence[str] | None = None) -> dict[str, Any]:
    from scripts.kg import stage2_subitem_schema_plan as schema_plan

    signature = schema_plan.schema_signature(connection)
    if not signature["column_present"]:
        raise ValueError("the containment column is absent; the schema must land first")

    collision = dict(containment.get("collision_population") or {})
    collision_ids = set(collision.get("row_ids") or [])
    if not collision_ids:
        raise ValueError("the containment plan records no collision population to exclude")

    derived = rederive_operations(connection, collision_row_ids=collision_ids)
    planned = containment.get("operations") or []
    planned_pairs = sorted(
        (int(o["child_item_id"]), int(o["parent_item_id"])) for o in planned)
    derived_pairs = sorted(
        (o["child_item_id"], o["parent_item_id"]) for o in derived["operations"])
    if planned_pairs != derived_pairs:
        only_planned = sorted(set(planned_pairs) - set(derived_pairs))[:5]
        only_derived = sorted(set(derived_pairs) - set(planned_pairs))[:5]
        raise ValueError(
            f"rederivation disagrees with the containment plan: planned={len(planned_pairs)} "
            f"derived={len(derived_pairs)} extra_in_plan={only_planned} "
            f"extra_in_live={only_derived}")

    fps: dict[str, dict] = {}
    for child_id, parent_id in derived_pairs:
        rows = connection.execute(text(
            f"SELECT id, meeting_db_id, agenda_item_number FROM {TABLE} "
            "WHERE id IN (:c, :p)"), {"c": child_id, "p": parent_id}).mappings().all()
        by_id = {int(r["id"]): dict(r) for r in rows}
        if child_id not in by_id or parent_id not in by_id:
            raise ValueError(f"operation {child_id}->{parent_id} names a missing row")
        child, parent = by_id[child_id], by_id[parent_id]
        if child["meeting_db_id"] != parent["meeting_db_id"]:
            raise ValueError(f"operation {child_id}->{parent_id} crosses meetings")
        cnum, pnum = str(child["agenda_item_number"]), str(parent["agenda_item_number"])
        if not (cnum.startswith(pnum + ".") and len(cnum) > len(pnum)):
            raise ValueError(f"operation {child_id}->{parent_id} does not shorten")
        fps[str(child_id)] = {"child": row_fingerprint(child),
                              "parent": row_fingerprint(parent)}

    operations = [{**o, "child_row_sha256": fps[str(o["child_item_id"])]["child"],
                   "parent_row_sha256": fps[str(o["child_item_id"])]["parent"]}
                  for o in derived["operations"]]
    modules = tuple(safety_modules or ()) + CODE_MODULES
    plan = {
        "kind": PLAN_KIND, "version": PLAN_VERSION, "created_at": created_at,
        "mode": "dry-run", "applied": False, "relation": RELATION,
        "target": {f: target.get(f) for f in TARGET_FIELDS},
        "operations": operations,
        "counts": {"operations": len(operations), "held": derived["held"]},
        "operation_set_digest": canonical_sha256(
            [[o["child_item_id"], o["parent_item_id"]] for o in operations]),
        "bindings": {
            "baseline_digest": baseline_digest,
            "containment_plan_digest": containment_digest,
            "schema_receipt_digest": schema_receipt_digest,
            "schema_signature": signature,
            "code_hashes": code_hashes(modules),
            "row_fingerprints": fps,
            "row_fingerprints_digest": canonical_sha256(fps),
            "collision_population": {
                "distinct_keys": collision.get("distinct_keys"),
                "involved_rows": collision.get("involved_rows"),
                "excess": collision.get("excess"),
                "row_ids_sha256": canonical_sha256(sorted(collision_ids)),
                "row_ids_count": len(collision_ids)},
            "baseline_rows_sha256": baseline.get("rows_sha256"),
            "table_fingerprint": table_fingerprint(connection),
            "fresh_backup_receipt": dict(backup),
        },
        "rules": {
            "same_meeting_endpoints": True, "strict_number_shortening": "N.L -> N",
            "no_self_links": True, "no_cycles": True,
            "unique_child_ownership": True, "exact_operation_set_equality": True,
            "collision_disjointness": True,
            "enforcement": "transactional preconditions inside one governed transaction",
        },
        "preconditions": {"column_present": True, "no_existing_parent_links": True,
                          "backup_restore_verified": True,
                          "rederivation_matches_containment_plan": True},
        "postconditions": {"updated_rows": len(operations),
                           "non_null_parent_item_id": len(operations),
                           "identity_columns_unchanged": True,
                           "protected_row_counts_unchanged": True},
        "rollback": {"statements": [
            f"UPDATE {TABLE} SET parent_item_id = NULL WHERE id = ANY(:child_ids)"]},
        "write_path": "absent by design",
    }
    plan["replay_digest"] = canonical_sha256(
        {k: v for k, v in plan.items()
         if k not in (artifacts.DIGEST_FIELD, "replay_digest", "created_at")})
    plan[artifacts.DIGEST_FIELD] = artifacts.compute_digest(plan)
    problems = validate_plan(plan)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return plan


def validate_plan(plan: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if plan.get("kind") != PLAN_KIND:
        problems.append(f"kind must be {PLAN_KIND!r}")
    if plan.get("mode") != "dry-run" or plan.get("applied") is not False:
        problems.append("the plan must be dry-run and unapplied")
    ops = plan.get("operations") or []
    if len(ops) != (plan.get("counts") or {}).get("operations"):
        problems.append("the operation count disagrees with the operations")
    recorded = (plan.get("bindings") or {}).get("code_hashes") or {}
    live = code_hashes(tuple(recorded))
    drift = sorted(r for r, d in recorded.items() if live.get(r) != d)
    if drift:
        problems.append(f"code has drifted for {drift}")
    bindings = plan.get("bindings") or {}
    for field in ("baseline_digest", "containment_plan_digest", "schema_receipt_digest",
                  "row_fingerprints_digest", "table_fingerprint"):
        if not bindings.get(field):
            problems.append(f"the plan binds no {field}")
    if not (bindings.get("schema_signature") or {}).get("column_present"):
        problems.append("the bound schema signature does not show the containment column")
    if int((bindings.get("collision_population") or {}).get("row_ids_count") or 0) <= 0:
        problems.append("the plan binds no collision population")
    if (bindings.get("fresh_backup_receipt") or {}).get("restore_proven") is not True:
        problems.append("the bound backup does not prove a restore")
    if (plan.get("target") or {}).get("tier") != "development":
        problems.append("the plan target is not development")
    fps = bindings.get("row_fingerprints") or {}
    if len(fps) != len(ops):
        problems.append("the row fingerprints do not cover every operation")
    if plan.get(artifacts.DIGEST_FIELD) != artifacts.compute_digest(plan):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems
