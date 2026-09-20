#!/usr/bin/env python3
"""``stage2_subitem_plan.py`` — the dry containment plan and its validator.

Split out of :mod:`stage2_subitem_containment` so both stay readable.  The plan
binds a **containment-specific manifest** (never the generic document-binding
block) and the validator **rederives** its conclusions from the **canonically
loaded baseline artifact**, requiring exact equality rather than a subset.

Three things the validator refuses to take on trust:

* **the baseline is loaded, not assumed.**  The named artifact is loaded through
  the verify-on-read loader, its stored digest and a digest over its **full rows**
  are recomputed, and its row count is checked.  A plan whose binding does not
  describe the artifact on disk is refused.
* **the embedded snapshot is the exact projection.**  ``baseline_snapshot`` must
  equal ``_snapshot(loaded_rows)`` — same rows, same order, same fields.  A
  substituted, reordered, truncated or padded snapshot is refused.
* **collisions and operations are fully rederived.**  The collision population
  (distinct keys, involved rows, excess and both digests) and the canonical
  operation set are recomputed from the loaded rows and compared for exact
  equality, so deleting or altering a collision, or adding a link the numbering
  does not imply, is refused.

There is no write path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_subitem_manifest as manifest_mod  # noqa: E402
from scripts.kg import stage2_subitem_schema as schema_mod  # noqa: E402

__all__ = ["PLAN_KIND", "SNAPSHOT_FIELDS", "build_plan", "plan_digest",
           "replay_digest", "validate_plan"]

PLAN_KIND = "kg-stage2-subitem-containment-plan"
PLAN_VERSION = "kg-stage2-subitem-containment/3.0"

#: The fields the embedded projection carries.  Published so a validator can state
#: exactly what "the exact projection" means rather than gesture at it.
SNAPSHOT_FIELDS = ("item_id", "meeting_db_id", "number", "class", "parent")

DEFAULT_PLAN_DIR = REPO / "data" / "kg-plans"

#: Mirrored from the containment module as literals rather than imported: the
#: containment module re-exports this one, so importing it back would be a cycle.
ATTACHED_TO = "ATTACHED_TO"
PART_OF = "PART_OF"
CLASS_SUBITEM = "subitem"
CLASS_DEEPER = "deeper"
CLASS_COLLISION = "collision_held"
CLASSES = ("root", "subitem", "deeper", "ambiguous", "invalid", "collision_held")

#: Every field of the collision population the validator rederives.
COLLISION_FIELDS = ("distinct_keys", "involved_rows", "excess", "keys",
                    "keys_digest", "row_ids", "row_ids_digest")


def _rederive_collisions(snapshot: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The collision population the snapshot implies, recomputed.

    Delegates to the containment module's own function — imported lazily, because
    that module re-exports this one — so the plan and the validator cannot drift
    into two different definitions of a collision.
    """
    from scripts.kg.stage2_subitem_containment import collision_population

    return collision_population([
        {"id": int(r["item_id"]), "meeting_db_id": int(r["meeting_db_id"]),
         "agenda_item_number": r.get("number")} for r in snapshot])


def _derive_operations(snapshot: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The canonical PART_OF operations a baseline snapshot implies.

    Rederived, never trusted: the validator calls this on the bound snapshot and
    compares the result to the plan's own operations for exact equality.  A
    missing, duplicated or substituted operation is a refusal, not a diff to
    reconcile.
    """
    by_meeting: dict[int, dict[str, int]] = {}
    for row in snapshot:
        if str(row.get("number") or ""):
            by_meeting.setdefault(int(row["meeting_db_id"]), {})[
                str(row["number"])] = int(row["item_id"])
    operations: list[dict[str, Any]] = []
    for row in sorted(snapshot, key=lambda r: int(r["item_id"])):
        if row.get("class") not in (CLASS_SUBITEM, CLASS_DEEPER):
            continue
        parent = row.get("parent")
        if not parent:
            continue
        parent_id = by_meeting.get(int(row["meeting_db_id"]), {}).get(str(parent))
        if parent_id is None:
            continue
        operations.append({
            "action": "contain",
            "relation": PART_OF,
            "child_item_id": int(row["item_id"]),
            "child_number": str(row["number"]),
            "parent_item_id": int(parent_id),
            "parent_number": str(parent),
            "meeting_db_id": int(row["meeting_db_id"]),
            "evidence": {"kind": "normalized_numbering", "child": str(row["number"]),
                         "parent": str(parent),
                         "basis": "the parent exists as a canonical item in the same meeting"},
            "evidence_sha256": manifest_mod.canonical_sha256(
                {"child": str(row["number"]), "parent": str(parent),
                 "meeting_db_id": int(row["meeting_db_id"])}),
            "identity": {"strategy": "UPDATE agenda_items SET parent_item_id WHERE id = <child>",
                         "natural_key": [int(row["item_id"])],
                         "rollback_owner": "apply receipt listed child ids only"},
        })
    return operations


def _snapshot(baseline: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The exact projection of a baseline's rows the plan embeds."""
    return [{"item_id": int(r["item_id"]), "meeting_db_id": int(r["meeting_db_id"]),
             "number": str(r["number"]), "class": r["class"], "parent": r.get("parent")}
            for r in baseline["rows"]]


def validate_projection(plan: Mapping[str, Any],
                        baseline: Mapping[str, Any]) -> list[str]:
    """The embedded snapshot must be exactly the baseline's projection."""
    problems: list[str] = []
    expected = _snapshot(baseline)
    embedded = plan.get("baseline_snapshot")
    if embedded is None:
        return ["the plan embeds no baseline snapshot"]
    if list(embedded) != expected:
        problems.append(
            "the embedded snapshot is not the exact projection of the loaded baseline "
            "(substituted, reordered, truncated or padded)")
    if manifest_mod.canonical_sha256(list(embedded)) != plan.get("baseline_rows_digest"):
        problems.append("the embedded snapshot digest does not match its own binding")
    if manifest_mod.canonical_sha256(expected) != plan.get("baseline_rows_digest"):
        problems.append("the projection digest does not match the plan's rows digest")
    projection = plan.get("baseline_projection") or {}
    if projection.get("projection_is_exact") is not True:
        problems.append("the plan does not claim an exact projection")
    if projection.get("source_rows_digest") != manifest_mod.canonical_sha256(
            baseline.get("rows") or []):
        problems.append("the projection's source rows digest is not the loaded baseline's")
    if sorted(projection.get("projection_fields") or []) != sorted(SNAPSHOT_FIELDS):
        problems.append("the projection does not name every projected field")
    return problems


def validate_collisions(plan: Mapping[str, Any],
                        snapshot: Sequence[Mapping[str, Any]]) -> list[str]:
    """The collision population must be exactly the one the rows imply."""
    problems: list[str] = []
    expected = _rederive_collisions(snapshot)
    bound = plan.get("collision_population") or {}
    for field in COLLISION_FIELDS:
        if field not in bound:
            problems.append(f"the collision population omits {field!r}")
        elif bound.get(field) != expected.get(field):
            problems.append(
                f"the collision population's {field!r} is not the rederived one "
                f"(deleted or altered)")
    if bound.get("keys_digest") and \
            bound["keys_digest"] != manifest_mod.canonical_sha256(bound.get("keys") or []):
        problems.append("the collision key digest does not cover the bound keys")
    if bound.get("row_ids_digest") and \
            bound["row_ids_digest"] != manifest_mod.canonical_sha256(bound.get("row_ids") or []):
        problems.append("the collision row digest does not cover the bound rows")
    return problems


def plan_digest(plan: Mapping[str, Any]) -> str:
    return artifacts.compute_digest(plan)


def replay_digest(plan: Mapping[str, Any]) -> str:
    excluded = (artifacts.DIGEST_FIELD, "replay_digest", "created_at")
    return manifest_mod.canonical_sha256({k: v for k, v in plan.items() if k not in excluded})


def load_baseline(baseline_path: str | Path, *,
                  plan_dir: str | Path | None = None) -> dict[str, Any]:
    """Canonically load the named baseline artifact (verifies its stored digest)."""
    name = Path(baseline_path).name
    path = Path(baseline_path)
    if not path.exists() and plan_dir is not None:
        path = Path(plan_dir) / name
    if not path.exists():
        raise ValueError(f"the baseline artifact {name!r} does not exist")
    if artifacts.is_obsolete(path) is not None:
        raise ValueError(f"the baseline artifact {name!r} is obsolete")
    return artifacts.load_verified(path)


def build_plan(
    baseline: Mapping[str, Any],
    *,
    baseline_path: str | Path,
    created_at: str,
    target: Mapping[str, Any],
    schema_signature: Mapping[str, Any] | None = None,
    agenda_items_schema: Mapping[str, Any] | None = None,
    column_present: bool = False,
) -> dict[str, Any]:
    """Build the dry containment plan over the canonically loaded baseline.

    The plan is built from the **artifact**, not from the caller's mapping: the
    named path is loaded through the verify-on-read loader and the caller's mapping
    must describe that same artifact by digest and by full rows digest.
    """
    loaded = load_baseline(baseline_path)
    if artifacts.recorded_digest(loaded) != artifacts.recorded_digest(baseline):
        raise ValueError("the supplied baseline is not the named artifact")
    if manifest_mod.canonical_sha256(loaded.get("rows") or []) != \
            manifest_mod.canonical_sha256(baseline.get("rows") or []):
        raise ValueError("the supplied baseline rows are not the named artifact's rows")

    manifest = manifest_mod.build_manifest(
        baseline_path=baseline_path, baseline=loaded, target=target,
        schema_signature=schema_signature)
    schema_contract = schema_mod.build_contract(
        target=target, schema_signature=schema_signature, column_present=column_present,
        agenda_items_schema=agenda_items_schema)

    snapshot = _snapshot(loaded)
    operations = _derive_operations(snapshot)
    collisions = _rederive_collisions(snapshot)

    # The proposed edges must be provably disjoint from the collision population.
    collision_keys = set(collisions.get("keys") or [])
    collision_rows = set(collisions.get("row_ids") or [])
    overlapping = [op["child_item_id"] for op in operations
                   if op["child_item_id"] in collision_rows]
    if overlapping:
        raise ValueError(f"a collision row is proposed for containment: {overlapping[:5]}")
    for op in operations:
        if f"{op['meeting_db_id']}|{op['child_number']}" in collision_keys:
            raise ValueError(f"a collision key is proposed for containment: {op['child_number']}")

    plan = {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "created_at": created_at,
        "mode": "dry-run",
        "manifest": manifest,
        "schema_readiness": schema_contract,
        "baseline_snapshot": snapshot,
        "baseline_rows_digest": manifest_mod.canonical_sha256(snapshot),
        "baseline_projection": {
            "source_path": Path(baseline_path).name,
            "source_artifact_digest": artifacts.recorded_digest(loaded),
            "source_rows_digest": manifest_mod.canonical_sha256(loaded.get("rows") or []),
            "source_row_count": len(loaded.get("rows") or []),
            "snapshot_digest": manifest_mod.canonical_sha256(snapshot),
            "projection_fields": list(SNAPSHOT_FIELDS),
            "projection_is_exact": True,
            "rule": "the embedded snapshot is exactly _snapshot(loaded.rows): the same "
                    "rows in the same order, carrying exactly these fields",
        },
        "operations": operations,
        "counts": dict(loaded["counts"]),
        "relations": {PART_OF: len(operations), ATTACHED_TO: 0},
        "population": loaded["population"],
        "collision_population": collisions,
        "containment": loaded["containment"],
        "accounting": {
            "operation_set_digest": manifest_mod.canonical_sha256(operations),
            "baseline_rows_digest": manifest_mod.canonical_sha256(snapshot),
            "baseline_artifact_digest": artifacts.recorded_digest(loaded),
            "operations": len(operations),
            "collision_keys": collisions.get("distinct_keys"),
            "collision_rows": collisions.get("involved_rows"),
            "collision_excess": collisions.get("excess"),
            "collision_keys_digest": collisions.get("keys_digest"),
            "collision_row_ids_digest": collisions.get("row_ids_digest"),
            "operations_disjoint_from_collisions": True,
        },
        "policy": {
            "document_order_is_never_consulted": True,
            "ambiguous_identifiers_are_held": True,
            "colliding_rows_are_held": True,
            "flat_linking_does_not_require_hierarchy": True,
            "attached_to_remains_distinct_from_part_of": True,
            "roles_and_outcomes_are_not_entities": True,
        },
        "collision_policy": {
            "rule": "an item that already carries a parent is never re-parented",
            "collision_rule": "a number claimed by more than one row is ambiguous; both "
                              "rows are held and no containment is proposed for either",
            "locking": "the check and the update share one transaction; the parent row "
                       "is locked with SELECT ... FOR UPDATE before the child is updated",
        },
        "replay": {
            "no_stability_claim": True,
            "statement": "applying changes the database; the artifact digest is historical",
            "post_apply_state": "every operation's child carries its parent_item_id",
            "on_post_apply_run": "receipt-bound no-op with writes=0, or a refusal",
        },
        "rollback": {
            "ownership": "only child item ids recorded in the apply receipt",
            "never": "never clear parent_item_id for items the receipt does not own",
            "restore": "exact preimage values, never recomputed ones",
            "reversible": True,
        },
        "preconditions": {
            "read_only_snapshot": True,
            "no_database_write_by_this_plan": True,
            "schema_readiness_required": True,
            "schema_column_present": bool(column_present),
        },
        "write_path": "absent by design",
        "applied": False,
        "promoted": False,
    }
    plan["replay_digest"] = replay_digest(plan)
    plan[artifacts.DIGEST_FIELD] = plan_digest(plan)
    problems = validate_plan(plan, baseline=loaded, baseline_path=baseline_path,
                             authoritative_schema=agenda_items_schema)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return plan


def validate_plan(plan: Mapping[str, Any],
                  *, baseline: Mapping[str, Any] | None = None,
                  baseline_path: str | Path | None = None,
                  plan_dir: str | Path | None = None,
                  authoritative_schema: Mapping[str, Any] | None = None,
                  authoritative_target: Mapping[str, Any] | None = None) -> list[str]:
    """Load the baseline, prove the projection, rederive collisions and operations."""
    problems: list[str] = []
    if plan.get("kind") != PLAN_KIND:
        problems.append(f"kind must be {PLAN_KIND!r}")
    if plan.get("mode") != "dry-run":
        problems.append("the plan must be dry-run only")
    if plan.get("write_path") != "absent by design":
        problems.append("the plan must declare no write path")
    if plan.get("applied") is not False or plan.get("promoted") is not False:
        problems.append("a plan must record applied=false and promoted=false")

    manifest = plan.get("manifest") or {}
    if manifest.get("kind") != manifest_mod.MANIFEST_KIND:
        problems.append("the plan does not bind a containment manifest")
    else:
        problems.extend(manifest_mod.validate_manifest(manifest))
    if (manifest.get("source_authority") or {}).get("model_output_used") is not False:
        problems.append("containment must not rest on model output")
    for forbidden in ("decisions", "lineage", "aggregate"):
        if forbidden in manifest:
            problems.append(f"containment must not bind {forbidden!r}")

    problems.extend(schema_mod.validate_contract(
        plan.get("schema_readiness") or {},
        authoritative_schema=authoritative_schema,
        authoritative_target=authoritative_target))

    counts = plan.get("counts") or {}
    if sorted(counts) != sorted(CLASSES):
        problems.append("the counts do not cover every class")
    if sum(counts.values()) != (plan.get("population") or {}).get("count"):
        problems.append("the class counts do not reconcile to the population")

    # ── the baseline is loaded canonically, never assumed ───────────────
    loaded = baseline
    bound = manifest.get("baseline") or {}
    if loaded is None:
        name = bound.get("path")
        if not name:
            problems.append("the manifest names no baseline artifact")
        else:
            folder = Path(plan_dir) if plan_dir is not None else DEFAULT_PLAN_DIR
            try:
                loaded = load_baseline(baseline_path or name, plan_dir=folder)
            except Exception as exc:  # noqa: BLE001 - any failure is a refusal
                problems.append(f"the named baseline artifact could not be loaded: {exc}")
                loaded = None
    if loaded is not None:
        if artifacts.recorded_digest(loaded) != bound.get("canonical_digest"):
            problems.append("the loaded baseline's artifact digest is not the bound one")
        rows_digest = manifest_mod.canonical_sha256(loaded.get("rows") or [])
        if rows_digest != bound.get("rows_digest"):
            problems.append("the loaded baseline's rows digest is not the bound one")
        if len(loaded.get("rows") or []) != bound.get("row_count"):
            problems.append("the loaded baseline's row count is not the bound one")

        # ── the projection must be exact ────────────────────────────────
        problems.extend(validate_projection(plan, loaded))
        snapshot = plan.get("baseline_snapshot") or []
        if not snapshot:
            problems.append("the plan binds no baseline snapshot to rederive from")
        else:
            problems.extend(validate_collisions(plan, snapshot))

            # ── operation equality, rederived ───────────────────────────
            expected = _derive_operations(snapshot)
            operations = plan.get("operations") or []
            key = lambda o: int(o.get("child_item_id", -1))
            if sorted(map(key, expected)) != sorted(map(key, operations)):
                problems.append(
                    "the operation set is not exactly the rederived canonical set "
                    "(missing, duplicated or substituted)")
            if manifest_mod.canonical_sha256(operations) != \
                    (plan.get("accounting") or {}).get("operation_set_digest"):
                problems.append("the operation-set digest does not match the operations")
            expected_by_child = {key(o): o for o in expected}
            for op in operations:
                where = f"{op.get('meeting_db_id')}/{op.get('child_number')}"
                canonical = expected_by_child.get(key(op))
                if canonical is None:
                    problems.append(f"{where}: not in the rederived canonical set")
                    continue
                for field in ("parent_item_id", "parent_number", "meeting_db_id",
                              "evidence_sha256", "relation"):
                    if op.get(field) != canonical.get(field):
                        problems.append(f"{where}: {field!r} does not match the rederived set")
            children = [key(o) for o in operations]
            if len(children) != len(set(children)):
                problems.append("a child item appears in more than one operation")
            meeting_by_item = {int(r["item_id"]): int(r["meeting_db_id"])
                               for r in snapshot}
            for op in operations:
                where = f"{op.get('meeting_db_id')}/{op.get('child_number')}"
                child = int(op.get("child_item_id", -1))
                parent = int(op.get("parent_item_id", -1))
                if child == parent:
                    problems.append(f"{where}: an item cannot contain itself")
                if meeting_by_item.get(child) != meeting_by_item.get(parent):
                    problems.append(f"{where}: the endpoints are in different meetings")
                if not _is_parent(str(op.get("parent_number")), str(op.get("child_number"))):
                    problems.append(f"{where}: {op.get('parent_number')!r} is not the "
                                    f"parent of {op.get('child_number')!r}")
                if not (op.get("evidence") or {}).get("kind"):
                    problems.append(f"{where}: no evidence")

    # ── the edges must be disjoint from the collision population ────────
    collisions = plan.get("collision_population") or {}
    collision_keys = set(collisions.get("keys") or [])
    collision_rows = set(collisions.get("row_ids") or [])
    if not collisions.get("keys_digest"):
        problems.append("the collision population carries no key digest")
    for name in ("distinct_keys", "involved_rows", "excess"):
        if collisions.get(name) is None:
            problems.append(f"the collision population is missing {name!r}")
    for op in plan.get("operations") or []:
        if int(op.get("child_item_id", -1)) in collision_rows or \
                int(op.get("parent_item_id", -1)) in collision_rows:
            problems.append("a proposed edge touches a colliding row")
        if f"{op.get('meeting_db_id')}|{op.get('child_number')}" in collision_keys:
            problems.append("a proposed edge touches a collision key")
    for row in plan.get("baseline_snapshot") or []:
        if row.get("class") == CLASS_COLLISION and row.get("parent"):
            problems.append("a colliding row carries a parent")

    rollback = plan.get("rollback") or {}
    if "does not own" not in str(rollback.get("never", "")):
        problems.append("rollback does not restrict ownership to the receipt")
    if rollback.get("restore") != "exact preimage values, never recomputed ones":
        problems.append("rollback does not require exact preimages")
    if (plan.get("replay") or {}).get("no_stability_claim") is not True:
        problems.append("replay must not claim the artifact digest stays valid")
    if not plan.get("replay_digest"):
        problems.append("replay_digest is missing")
    if plan.get(artifacts.DIGEST_FIELD) != artifacts.compute_digest(plan):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems


def _is_parent(parent: str, child: str) -> bool:
    # Imported here rather than at module scope: the containment module re-exports
    # this one, so a top-level import back would be a cycle.
    from scripts.kg.stage2_subitem_containment import parse_hierarchy

    parsed = parse_hierarchy(child)
    return bool(parsed) and parsed["prefix"] == parent
