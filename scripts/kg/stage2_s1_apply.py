#!/usr/bin/env python3
"""``stage2_s1_apply.py`` — digest-bound Stage 2 S1 apply runner (development only).

**Do not execute this runner without explicit apply authorization.**  It is the
single authority that turns a reviewed S1 plan into database changes.

Contract
--------
* **Saved plan + exact digest only.**  Both the artifact's own digest and the
  caller-supplied ``--digest`` must match; a re-hashed forgery is still refused
  because the plan's scope, targets, policy and counts are re-derived and
  re-checked against the live database.
* **Proven development target.**  Refused before anything else, via the tier
  resolver and :func:`scripts.kg.phoenix_dr_adjudication.assert_development_target`.
* **Exact target binding.**  The plan's recorded target, the live engine and the
  backup receipt must agree on dialect, host, port and database.  A different
  development database with an identical schema, or a drifted host or port, is
  refused before anything is written.
* **Protected verified backup.**  A receipt proving the development database was
  backed up *and restored* must exist, validate, and reproduce **every** plan
  baseline count — a receipt that merely omits a table cannot pass.
* **Immutable pre-operation backup artifact.**  The rollback preimage is written
  write-once *before* the transaction opens, so it cannot be produced after
  something went wrong.
* **One transaction.**  Scope, fingerprints, target bodies and collisions are
  locked and re-checked inside the transaction; the writes run there; the
  postconditions are evaluated on that same connection.
* **Atomic rollback.**  Any failure rolls the transaction back and writes a
  labelled failure receipt.
* **Immutable terminal receipt.**  Written write-once; its presence refuses a
  repeat apply of the same plan.
* **Fail closed on unsupported dialects.**  Only PostgreSQL may apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import bindparam, text  # noqa: E402

from scripts.db import tier as tier_module  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s1_policy as policy  # noqa: E402
from scripts.kg import stage2_s1_verify as verify  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg.phoenix_dr_adjudication import assert_development_target  # noqa: E402

__all__ = [
    "RECEIPT_KIND",
    "ApplyRefused",
    "apply_plan",
    "group_targets",
    "load_plan_for_digest",
    "main",
    "CLI_REFUSALS",
    "REFUSED_EXIT_CODE",
    "require_protected_receipt",
    "write_receipt",
]

RECEIPT_KIND = "kg-stage2-s1-receipt"

#: Exit status for an expected refusal, distinct from an unexpected crash (1).
REFUSED_EXIT_CODE = 2


#: The only dialect permitted to apply, absent an explicit mechanics-only call.
APPLY_DIALECTS = ("postgresql",)


class ApplyRefused(RuntimeError):
    """The apply was refused; nothing was written."""
#: Guard refusals a caller should expect.  Both mean "this apply will not run
#: and nothing was written", so both are reported as one line, not a traceback.
CLI_REFUSALS = (ApplyRefused, tier_module.TierError)


def load_plan_for_digest(plan_path: str | Path, supplied_digest: str) -> dict[str, Any]:
    """Load a plan, requiring both its own digest and the supplied digest to match."""
    plan = artifacts.load_verified(plan_path)
    actual = artifacts.compute_digest(plan)
    if actual != supplied_digest:
        raise ApplyRefused(
            f"supplied digest {supplied_digest} does not match plan digest {actual}"
        )
    return plan


def require_protected_receipt(
    receipt_path: str | Path, plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Require an existing, permission-protected, valid backup receipt."""
    path = Path(receipt_path)
    if not path.exists():
        raise ApplyRefused(f"backup receipt not found: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ApplyRefused(f"backup receipt is not protected (mode {mode:o}): {path}")
    receipt = json.loads(path.read_text(encoding="utf-8"))

    baseline_counts = (plan.get("baseline") or {}).get("counts") or {}
    if not baseline_counts:
        raise ApplyRefused("plan carries no baseline counts to check the backup against")
    restored = receipt.get("counts")
    if not isinstance(restored, Mapping):
        raise ApplyRefused("backup receipt carries no restored counts")

    # Every baseline key is mandatory.  Intersecting with whatever the receipt
    # happens to contain would let a receipt that omits whole tables pass purely
    # because it never claimed them — the coverage gap it silences is the one
    # that matters.
    missing = sorted(k for k in baseline_counts if k not in restored)
    if missing:
        raise ApplyRefused(f"backup receipt is missing required count keys: {missing}")
    expected = {k: int(v) for k, v in baseline_counts.items()}

    try:
        result = receipts.require_receipt(receipt, expected_counts=expected)
    except ValueError as exc:
        # The runner exposes exactly one refusal type, so a caller cannot
        # accidentally handle only some of the ways an apply is stopped.
        raise ApplyRefused(f"backup receipt refused: {exc}") from exc

    # The values are checked above key-by-key; the fingerprint binds the whole
    # set under one authoritative algorithm, so a plan and a receipt that agree
    # on the numbers must agree here too.  Absence on either side is refused:
    # an unbound backup is not evidence that this database was captured.
    planned_fp = (plan.get("baseline") or {}).get("counts_fingerprint")
    if not planned_fp:
        raise ApplyRefused(
            "plan carries no baseline counts fingerprint to bind the backup to"
        )
    if "counts_fingerprint" not in result or result["counts_fingerprint"] is None:
        raise ApplyRefused("backup receipt produced no counts fingerprint to compare")
    if result["counts_fingerprint"] != planned_fp:
        raise ApplyRefused(
            f"backup receipt counts fingerprint {result['counts_fingerprint']} "
            f"does not match plan baseline {planned_fp}"
        )
    return {
        "receipt": receipt,
        "validated": result,
        "path": str(path),
        "counts_fingerprint": result["counts_fingerprint"],
    }


def group_targets(plan: Mapping[str, Any]) -> dict[int, list[int]]:
    """Group assigned meeting ids by target body, for exact per-body rowcounts."""
    groups: dict[int, list[int]] = {}
    for entry in plan.get("assignments", ()):
        groups.setdefault(int(entry["target_public_body_id"]), []).append(
            int(entry["meeting_db_id"])
        )
    return {body: sorted(ids) for body, ids in sorted(groups.items())}


def _preimage_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "kg-stage2-s1-preimage",
        "plan_id": plan.get("plan_id"),
        "plan_digest": artifacts.compute_digest(plan),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rollback_preimage": list(plan.get("rollback_preimage") or ()),
        "holds": [int(e["meeting_db_id"]) for e in plan.get("holds", ())],
    }


def write_receipt(out_dir: str | Path, plan_id: str, payload: Mapping[str, Any],
                  *, suffix: str = "") -> tuple[Path, str]:
    """Write a terminal receipt immutably."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = artifacts.receipt_filename(f"{plan_id}{suffix}")
    digest = artifacts.write_immutable(directory / name, payload)
    return directory / name, digest


def apply_plan(
    engine: Any,
    plan: Mapping[str, Any],
    *,
    supplied_digest: str,
    backup_receipt: Mapping[str, Any],
    out_dir: str | Path,
    allow_unsupported_dialect: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one reviewed plan inside a single transaction.

    ``allow_unsupported_dialect`` exists solely so isolated tests can exercise the
    *mechanics* on SQLite.  It skips row locking and never relaxes any identity,
    digest, backup or postcondition check.
    """
    if artifacts.compute_digest(plan) != supplied_digest:
        raise ApplyRefused("plan digest changed after loading")
    problems = verify.verify_plan_shape(plan)
    if problems:
        raise ApplyRefused(f"plan shape refused: {'; '.join(problems[:5])}")

    dialect = engine.dialect.name
    if dialect not in APPLY_DIALECTS and not allow_unsupported_dialect:
        raise ApplyRefused(f"unsupported dialect for apply: {dialect!r}")
    target_info = assert_development_target(engine)

    plan_id = str(plan.get("plan_id"))
    terminal = Path(out_dir) / artifacts.receipt_filename(plan_id)
    if terminal.exists():
        raise ApplyRefused(f"plan {plan_id} already has a terminal receipt: {terminal}")

    backup = require_protected_receipt(backup_receipt, plan)

    # Bind the plan, the live engine and the backup receipt to one target.
    # Development-class membership alone is not enough: the plan must have been
    # written for this engine, and the backup must have come from this database.
    binding = verify.verify_target_binding(plan, engine, backup["receipt"])
    if binding:
        raise ApplyRefused("target binding refused: " + "; ".join(binding[:5]))

    preimage_path, _preimage_digest = write_receipt(
        out_dir, plan_id, _preimage_payload(plan), suffix="-preimage"
    )

    groups = group_targets(plan)
    expected_total = sum(len(v) for v in groups.values())
    if expected_total != int((plan.get("counts") or {}).get("assignments", -1)):
        raise ApplyRefused("assignment grouping does not match the plan's counts")

    receipt: dict[str, Any] = {
        "kind": RECEIPT_KIND,
        "plan_id": plan_id,
        "plan_digest": supplied_digest,
        "applied_at": (now or datetime.now(timezone.utc)).isoformat(),
        "engine_target": target_info,
        "plan_target": plan.get("target"),
        "backup_receipt": {
            "path": backup["path"],
            "counts_fingerprint": backup["validated"].get("counts_fingerprint"),
        },
        "preimage_artifact": str(preimage_path),
        "operations": {"assignments": expected_total, "target_bodies": len(groups)},
    }

    try:
        with engine.begin() as connection:
            if dialect in APPLY_DIALECTS:
                connection.execute(
                    text("SELECT id FROM meetings WHERE public_body_id IS NULL FOR UPDATE")
                )
            pre = verify.verify_preconditions(connection, plan)
            scope = verify.verify_scope(connection, plan)
            if pre or scope:
                raise ApplyRefused(
                    "preconditions failed: " + "; ".join((pre + scope)[:5])
                )

            rowcount = 0
            per_body: dict[str, int] = {}
            for body, ids in groups.items():
                result = connection.execute(
                    text(
                        "UPDATE meetings SET public_body_id = :body "
                        "WHERE id IN :ids AND public_body_id IS NULL"
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"body": body, "ids": ids},
                )
                if result.rowcount != len(ids):
                    raise ApplyRefused(
                        f"body {body}: updated {result.rowcount} rows, expected {len(ids)}"
                    )
                rowcount += result.rowcount
                per_body[str(body)] = int(result.rowcount)
            if rowcount != expected_total:
                raise ApplyRefused(f"updated {rowcount} rows, expected {expected_total}")
            receipt["operations"]["rows_updated"] = rowcount
            receipt["operations"]["per_target_body"] = per_body

            post = verify.verify_postconditions(connection, plan)
            if post:
                raise ApplyRefused(f"postconditions failed: {'; '.join(post[:5])}")
            receipt["postconditions"] = {
                "null_parent_after": int(
                    connection.execute(
                        text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
                    ).scalar()
                    or 0
                ),
                "passed": True,
            }
            receipt["status"] = "success"
    except Exception as exc:  # rollback already happened via engine.begin()
        failure = dict(receipt)
        failure["status"] = "failed"
        failure["error"] = f"{type(exc).__name__}: {exc}"
        try:
            write_receipt(out_dir, plan_id, failure, suffix="-failure")
        except artifacts.ArtifactCollision:
            pass
        raise

    path, digest = write_receipt(out_dir, plan_id, receipt)
    # Report the digest under the same field name the artifact itself records,
    # so stdout and the persisted file cannot disagree about where it lives.
    receipt["receipt_path"] = str(path)
    receipt[artifacts.DIGEST_FIELD] = digest
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    """Apply a reviewed plan.  Requires explicit authorization to run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--backup-receipt", required=True)
    parser.add_argument("--out-dir", default="data/kg-plans")
    args = parser.parse_args(argv)

    try:
        from scripts.db import config
        from db.core import get_engine

        tier_module.validate_tier_target(config.DB_TIER, config.DB_TARGET)
        if config.DB_TIER != tier_module.DEVELOPMENT:
            raise ApplyRefused(f"apply is development-only, got tier {config.DB_TIER!r}")

        plan = load_plan_for_digest(args.plan, args.digest)
        engine = get_engine()
        receipt = apply_plan(
            engine,
            plan,
            supplied_digest=args.digest,
            backup_receipt=args.backup_receipt,
            out_dir=args.out_dir,
        )
    except CLI_REFUSALS as exc:
        # A refusal is an expected outcome, not a crash: an already-applied plan
        # replayed unchanged lands here, as does a tier guard.  Report one line
        # and exit 2 so the caller can tell "refused" from "unexpected failure".
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return REFUSED_EXIT_CODE
    print(json.dumps(receipt, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
