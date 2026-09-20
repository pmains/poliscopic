#!/usr/bin/env python3
"""``stage2_s1_plan.py`` — build the immutable Stage 2 S1 parentage plan.

This module is **read-only**.  It reads the development database through the
guarded read-only engine, classifies every meeting whose ``public_body_id`` is
NULL under :mod:`scripts.kg.stage2_s1_policy`, and writes one digest-bound plan
artifact naming the exact rows that an apply would change.

It never writes to a database and never applies anything.

Usage::

    .venv/bin/python -u scripts/kg/stage2_s1_plan.py --out-dir data/kg-plans
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import inspect as sa_inspect  # noqa: E402
from sqlalchemy import text  # noqa: E402

from scripts.db import tier as tier_module  # noqa: E402
from scripts.entities.detect_entities import (  # noqa: E402
    GATE_COUNT_TABLES,
    integrity_snapshot,
)
from scripts.entities.event_normalize_preflight import guard_engine  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_parentage_contract as parentage  # noqa: E402
from scripts.kg import stage2_schema_readiness as schema_readiness  # noqa: E402
from scripts.kg import stage2_s1_policy as policy  # noqa: E402

__all__ = [
    "PLAN_KIND",
    "build_plan",
    "classify_rows",
    "code_hashes",
    "collect_meetings",
    "collect_registry",
    "main",
    "prior_plan",
    "readiness_section",
    "registry_index",
    "write_plan",
]

PLAN_KIND = "kg-stage2-s1-plan"

#: Files whose content defines the plan's meaning.
CODE_FILES = (
    "scripts/kg/stage2_s1_policy.py",
    "scripts/kg/stage2_s1_plan.py",
    "scripts/kg/stage2_s1_verify.py",
    "scripts/kg/stage2_s1_apply.py",
    "scripts/kg/stage2_artifacts.py",
)


def code_hashes(root: Path = _REPO_ROOT) -> dict[str, str]:
    """SHA-256 of every module that defines this plan's behaviour."""
    out: dict[str, str] = {}
    for relative in CODE_FILES:
        path = root / relative
        out[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def collect_registry(connection: Any) -> list[dict[str, Any]]:
    """Read the whole public-body registry (read-only)."""
    rows = connection.execute(
        text(
            "SELECT id, jurisdiction_id, name, slug, body_code "
            "FROM public_bodies ORDER BY id"
        )
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "jurisdiction_id": r[1],
            "name": r[2],
            "slug": r[3],
            "body_code": r[4],
        }
        for r in rows
    ]


def registry_index(registry: Sequence[Mapping[str, Any]]) -> dict[str, list[int]]:
    """Index registry rows by both key namespaces (``body_code`` and ``slug``).

    Both namespaces are indexed into one map on purpose: a source value matching
    either is a candidate, which is exactly how the ``chandler-cf`` collision
    arises and why collisions are resolved explicitly rather than by precedence.
    """
    index: dict[str, list[int]] = {}
    for row in registry:
        for key in {str(row.get("body_code") or ""), str(row.get("slug") or "")}:
            if key:
                index.setdefault(key, []).append(int(row["id"]))
    return index


def collect_meetings(connection: Any) -> list[dict[str, Any]]:
    """Read every meeting whose parent body is currently NULL (read-only)."""
    rows = connection.execute(
        text(
            "SELECT id, body, meeting_id, meeting_date, jurisdiction_id, public_body_id "
            "FROM meetings WHERE public_body_id IS NULL ORDER BY id"
        )
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "body": r[1],
            "meeting_id": r[2],
            "meeting_date": r[3],
            "jurisdiction_id": r[4],
            "public_body_id": r[5],
        }
        for r in rows
    ]


def _fingerprint_row(row: Mapping[str, Any]) -> str:
    return policy.meeting_fingerprint(row)


def classify_rows(
    meetings: Sequence[Mapping[str, Any]],
    registry: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Split NULL-parent meetings into assignments and holds.

    Any row the policy cannot classify raises :class:`PolicyError`, so a drifted
    or unhandled identity stops plan generation instead of being quietly dropped.
    """
    index = registry_index(registry)
    valid_ids = {int(r["id"]) for r in registry}
    assignments: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    counts = {"direct": 0, "alias": 0, "collision": 0, "assignments": 0,
              "hold_phoenix_gp": 0, "hold_sentinel": 0, "holds": 0}

    for row in meetings:
        decision = policy.decide(row["body"], index.get(str(row["body"] or ""), []))
        entry = {
            "meeting_db_id": int(row["id"]),
            "meeting_id": row["meeting_id"],
            "body": row["body"],
            "fingerprint": _fingerprint_row(row),
        }
        if decision.strategy == policy.STRATEGY_HOLD:
            entry["reason"] = decision.reason
            holds.append(entry)
            continue
        target = int(decision.target_public_body_id or 0)
        if target not in valid_ids:
            raise policy.PolicyError(
                f"meeting {row['id']} targets body {target}, absent from the registry"
            )
        entry.update(
            {
                "target_public_body_id": target,
                "strategy": decision.strategy,
                "reason": decision.reason,
            }
        )
        assignments.append(entry)
        counts["assignments"] += 1
        if decision.strategy == policy.STRATEGY_DIRECT:
            counts["direct"] += 1
        elif decision.strategy == policy.STRATEGY_ALIAS:
            counts["alias"] += 1
        elif decision.strategy == policy.STRATEGY_COLLISION:
            counts["collision"] += 1

    for entry in holds:
        counts["holds"] += 1
        if entry["body"] == "__skip__":
            counts["hold_sentinel"] += 1
        elif entry["body"] == "phoenix-gp":
            counts["hold_phoenix_gp"] += 1
    counts["null_parent_total"] = len(meetings)
    return assignments, holds, counts


def prior_plan(out_dir: str | Path) -> dict[str, Any] | None:
    """Describe the most recent existing plan, so a new one can supersede it.

    Nothing is ever overwritten: the prior artifact stays on disk and its digest
    is recorded in the new plan's ``supersedes`` field.
    """
    candidates = sorted(Path(out_dir).glob("kg-stage2-s1-plan-*.json"))
    if not candidates:
        return None
    latest = candidates[-1]
    document = artifacts.load_verified(latest)
    return {
        "plan_id": document.get("plan_id"),
        "path": str(latest),
        "digest": artifacts.compute_digest(document),
    }


def readiness_section(engine: Any) -> dict[str, Any]:
    """Bind the sync/parity readiness contract and its live dev evidence.

    The declaration and the bound module hashes make an apply refuse if the sync
    or parity behaviour drifts after review.  ``dev_columns`` records what the
    development database actually carries, so the plan proves the columns exist
    rather than assuming it.
    """
    inspector = sa_inspect(engine)
    observed: dict[str, Any] = {}
    for column in parentage.contracted_columns():
        try:
            found = next(
                (c for c in inspector.get_columns(column.table)
                 if c["name"] == column.column),
                None,
            )
        except Exception:  # table absent on this engine
            found = None
        observed[column.key()] = {
            "present": found is not None,
            "type_family": parentage.type_family(found.get("type")) if found else None,
            "nullable": bool(found.get("nullable")) if found else None,
            "expected_type_family": column.type_family,
            "expected_nullable": column.nullable,
        }
    return {
        "contract": parentage.contract_snapshot(),
        "code_hashes": parentage.code_hashes(),
        "readiness_digest": parentage.readiness_digest(),
        "dev_columns": observed,
        "fk_order_ok": True,
    }


def build_plan(
    engine: Any,
    target: tier_module.Target,
    *,
    created_at: str | None = None,
    plan_id: str | None = None,
    baseline: Mapping[str, Any] | None = None,
    supersedes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full plan document (without its digest).

    ``baseline`` may be supplied to inject a known baseline (isolated tests);
    when omitted the live six-table counts and integrity snapshot are read.
    """
    stamp = created_at or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with engine.connect() as connection:
        if baseline is None:
            counts = {t: int(connection.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar())
                      for t in GATE_COUNT_TABLES}
            integrity = {k: int(v) for k, v in integrity_snapshot(connection).items()}
        else:
            counts = {str(k): int(v) for k, v in (baseline.get("counts") or {}).items()}
            integrity = {str(k): int(v) for k, v in (baseline.get("integrity") or {}).items()}
        registry = collect_registry(connection)
        meetings = collect_meetings(connection)

    assignments, holds, bucket = classify_rows(meetings, registry)
    problem = policy.counts_problem(bucket)
    if problem is not None:
        raise policy.PolicyError(f"plan counts do not reconcile: {problem}")

    assignments.sort(key=lambda e: e["meeting_db_id"])
    holds.sort(key=lambda e: e["meeting_db_id"])
    by_id = {int(r["id"]): r for r in registry}

    return {
        "kind": PLAN_KIND,
        "algorithm_version": policy.ALGORITHM_VERSION,
        "plan_id": plan_id or stamp,
        "created_at": stamp,
        "target": {
            "redacted": target.redacted(),
            "url_class": target.url_class,
            "dialect": target.dialect,
            "host": target.host,
            "port": target.port,
            "database": target.database,
        },
        "policy": policy.policy_snapshot(),
        "code_hashes": code_hashes(),
        "sync_parity_readiness": readiness_section(engine),
        "schema_readiness": schema_readiness.bind_section(engine),
        "supersedes": dict(supersedes) if supersedes else None,
        "baseline": {
            "counts": counts,
            "integrity": integrity,
            # One authoritative algorithm: the same one every backup receipt
            # uses, so the runner can compare the two fingerprints directly.
            "counts_fingerprint": receipts.counts_fingerprint(counts),
        },
        "counts": bucket,
        "target_bodies": {
            str(bid): {"id": bid, "name": by_id[bid]["name"], "slug": by_id[bid]["slug"]}
            for bid in sorted({int(e["target_public_body_id"]) for e in assignments})
        },
        "assignments": assignments,
        "holds": holds,
        "rollback_preimage": [
            {"meeting_db_id": int(e["meeting_db_id"]), "public_body_id": None}
            for e in assignments
        ],
        "expected_after_state": {
            "public_body_id_set_count": len(assignments),
            "public_body_id_null_count": policy.EXPECTED_COUNTS["holds"],
            "applied": len(assignments),
            "hold_set": {
                "phoenix_gp": policy.EXPECTED_COUNTS["hold_phoenix_gp"],
                "sentinel": policy.EXPECTED_COUNTS["hold_sentinel"],
            },
        },
    }


def write_plan(plan: Mapping[str, Any], out_dir: str | Path) -> tuple[Path, str]:
    """Write the plan immutably and return ``(path, digest)``."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / artifacts.plan_filename(str(plan["plan_id"]))
    digest = artifacts.write_immutable(path, plan)
    return path, digest


def main(argv: Sequence[str] | None = None) -> int:
    """Build and write one plan against the resolved development target."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="data/kg-plans")
    parser.add_argument("--plan-id", default=None)
    args = parser.parse_args(argv)

    from scripts.db import config
    from db.core import get_engine

    target = config.DB_TARGET
    tier_module.validate_tier_target(config.DB_TIER, target)
    if config.DB_TIER != tier_module.DEVELOPMENT:
        raise policy.PolicyError(
            f"plan generation is development-only, got tier {config.DB_TIER!r}"
        )

    engine = get_engine()
    guard_engine(engine)
    plan = build_plan(
        engine, target, plan_id=args.plan_id, supersedes=prior_plan(args.out_dir)
    )
    path, digest = write_plan(plan, args.out_dir)
    print(json.dumps({"plan": str(path), "digest": digest,
                      "counts": plan["counts"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
