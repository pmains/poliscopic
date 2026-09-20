#!/usr/bin/env python3
"""``stage2_schema_plan.py`` — readiness plan, production template, sequencing.

Split out of :mod:`scripts.kg.stage2_schema_readiness` so each module stays
focused: that one declares and observes, this one decides what to do about the
difference and how development and production are ordered.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.db import tier as tier_module  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_schema_readiness as readiness  # noqa: E402
from scripts.kg.stage2_schema_readiness import (
    COLUMN_SPECS,
    FK_SPECS,
    INDEX_SPECS,
    READINESS_VERSION,
    TABLE,
    blocking_problems,
    code_hashes,
    contract_snapshot,
    is_ready,
    observe,
    operations_for,
    readiness_digest,
    signature_digest,
)

__all__ = [
    "baseline_counts",
    "prior_artifact",
    "build_plan",
    "main",
    "production_template",
    "rollback_contract",
    "sequencing",
    "write_artifact",
]


def sequencing() -> dict[str, Any]:
    """The dev-first, then production, then parity ordering.

    Stated explicitly so a sync cannot be run against a target whose schema has
    not yet been brought to the same shape.
    """
    return {
        "steps": [
            "1. development: apply the readiness plan; verify postconditions",
            "2. development: capture the resulting schema signature",
            "3. production: capture the live signature under separate approval",
            "4. production: apply the same declared DDL from the template",
            "5. both: re-capture signatures and require them equal (parity)",
            "6. only then: run sync, whose parentage guard refuses a divergence",
        ],
        "rule": "no sync until dev and production parentage signatures are equal",
    }


def baseline_counts(engine: Any) -> dict[str, Any]:
    """Count evidence a protected backup receipt must reproduce.

    The runner compares the receipt's restored counts against these, so a
    receipt that is structurally valid but describes a different database is
    refused rather than accepted on shape alone.
    """
    from sqlalchemy import text
    from scripts.entities.detect_entities import GATE_COUNT_TABLES

    with readiness.reader(engine) as connection:
        counts = {
            table: int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar())
            for table in GATE_COUNT_TABLES
        }
    return {"counts": counts, "counts_fingerprint": receipts.counts_fingerprint(counts)}


def rollback_contract() -> dict[str, str]:
    """How the pre-plan shape is restored.  Deliberately not absolutist.

    PostgreSQL DDL is transactional, so a failure *inside* the apply transaction
    rolls every operation back.  Once committed, the pre-plan shape is restored
    by dropping only what this plan introduced -- and because every constraint
    here is introduced by this plan, dropping it is a complete revert.  The one
    asymmetry is that a validated constraint cannot be returned to ``NOT VALID``
    in place; reverting means dropping it.
    """
    return {
        "add_column": "DROP COLUMN (only for a column this plan introduced)",
        "create_index": "DROP INDEX <name>",
        "add_fk_not_valid": "ALTER TABLE ... DROP CONSTRAINT <name>",
        "validate_fk": "no in-place un-validate exists; DROP CONSTRAINT <name> "
                       "reverts the pre-plan shape because this plan introduced "
                       "the constraint",
        "transaction": "PostgreSQL DDL, including ADD CONSTRAINT and VALIDATE "
                       "CONSTRAINT, is transactional: a failure inside the apply "
                       "transaction rolls back every operation atomically",
        "after_commit": "after commit, the pre-plan shape is restored by dropping "
                        "only the constraints, indexes and columns this plan added",
        "limitation": "validation cannot be reversed to NOT VALID in place; "
                      "reverting means dropping the constraint this plan added, "
                      "which is a complete revert for that operation",
    }


def prior_artifact(out_dir: str | Path, pattern: str) -> dict[str, Any] | None:
    """Describe the most recent matching artifact so a new one can supersede it.

    Nothing is ever overwritten: the prior artifact stays on disk and its digest
    is recorded in the new artifact's ``supersedes`` field.
    """
    candidates = sorted(Path(out_dir).glob(pattern))
    if not candidates:
        return None
    latest = candidates[-1]
    document = artifacts.load_verified(latest)
    return {
        "path": str(latest),
        "digest": artifacts.compute_digest(document),
        "created_at": document.get("created_at"),
    }


def build_plan(engine: Any, target: Any, *,
               supersedes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the readiness plan (without digest)."""
    observed = observe(engine)
    problems = blocking_problems(observed)
    operations = operations_for(observed)
    baseline = baseline_counts(engine)
    return {
        "kind": "kg-stage2-schema-readiness-plan",
        "version": READINESS_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "redacted": target.redacted(),
            "url_class": target.url_class,
            "dialect": target.dialect,
            "host": target.host,
            "database": target.database,
        },
        "contract": contract_snapshot(),
        "code_hashes": code_hashes(),
        "readiness_digest": readiness_digest(),
        "observed": observed,
        "signature_digest": signature_digest(observed),
        "baseline": baseline,
        "supersedes": dict(supersedes) if supersedes else None,
        "blocking_problems": problems,
        "operations": operations,
        "expected_ddl": [op["sql"] for op in operations],
        "ready": is_ready(observed),
        "idempotent": not operations,
        "sequencing": sequencing(),
        "rollback": rollback_contract(),
        "backup": {
            "required": True,
            "receipt": "a validated, permission-protected restore receipt whose "
                       "restored counts match this plan's baseline",
            "baseline_counts_fingerprint": baseline["counts_fingerprint"],
        },
        "postconditions": {
            "columns_present": [c.column for c in COLUMN_SPECS],
            "indexes_satisfied": [",".join(i.columns) for i in INDEX_SPECS],
            "foreign_keys_validated": [f.column for f in FK_SPECS],
            "dangling_zero": True,
        },
    }


def production_template(dev_plan: Mapping[str, Any]) -> dict[str, Any]:
    """An unexecuted production template derived from the same declaration.

    The template deliberately carries no production signature: that must be
    captured live under separate approval, so the template records where it goes.
    """
    return {
        "kind": "kg-stage2-schema-readiness-template",
        "version": READINESS_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "derived_from": {
            "plan_digest": dev_plan.get("digest"),
            "signature_digest": dev_plan.get("signature_digest"),
            "target": (dev_plan.get("target") or {}).get("redacted"),
        },
        "target": {
            "tier": "production",
            "signature": None,
            "note": "live production signature must be captured under separate approval "
                    "before this template is parameterised",
        },
        "contract": contract_snapshot(),
        "code_hashes": code_hashes(),
        "readiness_digest": readiness_digest(),
        "expected_ddl": list(dev_plan.get("expected_ddl") or ()),
        "blocking_problems_at_derivation": list(dev_plan.get("blocking_problems") or ()),
        "executed": False,
        "sequencing": sequencing(),
        "rollback": dev_plan.get("rollback"),
    }


def write_artifact(out_dir: str | Path, name: str, payload: Mapping[str, Any]) -> tuple[Path, str]:
    """Write one immutable artifact, returning ``(path, digest)``."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    digest = artifacts.write_immutable(path, payload)
    return path, digest


def main(argv: Sequence[str] | None = None) -> int:
    """Write the development readiness plan and the production template.

    Read-only against the development database.  The production template is
    produced unexecuted and without a production signature: that must be
    captured live under separate approval.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="data/kg-plans")
    args = parser.parse_args(argv)

    from scripts.db import config
    from db.core import get_engine
    from scripts.entities.event_normalize_preflight import guard_engine

    tier_module.validate_tier_target(config.DB_TIER, config.DB_TARGET)
    if config.DB_TIER != tier_module.DEVELOPMENT:
        raise SystemExit(f"schema planning is development-only, got {config.DB_TIER!r}")

    engine = get_engine()
    guard_engine(engine)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    plan = build_plan(
        engine, config.DB_TARGET,
        supersedes=prior_artifact(args.out_dir, "kg-stage2-schema-readiness-2*.json"),
    )
    plan_path, plan_digest = write_artifact(
        args.out_dir, f"kg-stage2-schema-readiness-{stamp}.json", plan
    )
    template = production_template({**plan, "digest": plan_digest})
    template_path, template_digest = write_artifact(
        args.out_dir, f"kg-stage2-schema-readiness-template-{stamp}.json", template
    )
    print(json.dumps({
        "plan": str(plan_path),
        "plan_digest": plan_digest,
        "template": str(template_path),
        "template_digest": template_digest,
        "ready": plan["ready"],
        "operations": len(plan["operations"]),
        "blocking_problems": plan["blocking_problems"],
        "signature_digest": plan["signature_digest"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
