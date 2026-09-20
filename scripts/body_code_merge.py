#!/usr/bin/env python3
"""Plan, rehearse, or apply the Chandler/Mesa body-code identity merge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db.core import get_engine  # noqa: E402
from body_code_merge_runtime import build_plan, execute_plan, write_plan  # noqa: E402
from kg import stage2_backup_verify  # noqa: E402


def require_backup(path_value: str, plan: dict) -> dict:
    """Require a protected, restore-verified backup of this exact dev baseline."""
    path = Path(path_value).resolve()
    if not path.is_file():
        raise SystemExit(f"refusing: backup receipt not found: {path}")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit("refusing: backup receipt mode is not 0600")
    receipt = json.loads(path.read_text())
    problems = stage2_backup_verify.validate_stage2_receipt(receipt)
    if problems:
        raise SystemExit(f"refusing: invalid backup receipt: {problems}")
    target = receipt.get("target") or {}
    if target.get("database") != plan["target"] or target.get("tier") != "development":
        raise SystemExit("refusing: backup receipt target differs from merge plan")
    restored = receipt.get("counts") or {}
    for key in ("meetings", "agenda_items", "meeting_events"):
        expected = plan["baseline"]["counts"][key]
        if restored.get(key) != expected:
            raise SystemExit(f"refusing: backup count drift for {key}")
    dump_path = Path(receipt.get("dump_path") or "")
    if not dump_path.is_file():
        raise SystemExit("refusing: receipt dump is absent")
    actual_sha = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    if actual_sha != receipt.get("dump_sha256"):
        raise SystemExit("refusing: backup dump digest mismatch")
    return {"path": str(path), "dump_path": str(dump_path),
            "dump_sha256": actual_sha}


def write_immutable(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--rehearse", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--backup-receipt")
    parser.add_argument("--plan-dir", type=Path,
                        default=Path("data/body-code-merge"))
    args = parser.parse_args()
    if sum((args.analyze, args.rehearse, args.apply)) != 1:
        parser.error("choose exactly one of --analyze, --rehearse, --apply")

    engine = get_engine()
    if args.apply:
        if not args.digest:
            parser.error("--apply requires --digest")
        if not args.backup_receipt:
            parser.error("--apply requires --backup-receipt")
        with engine.begin() as connection:
            plan = build_plan(connection)
            if plan["digest"] != args.digest:
                raise SystemExit("refusing: exact live plan digest does not match")
            backup = require_backup(args.backup_receipt, plan)
            stats = execute_plan(connection, plan)
            receipt = {"status": "success", "plan_digest": plan["digest"],
                       "backup": backup, "post_counts": stats}
        # The transaction has committed successfully before the terminal receipt
        # is allowed to claim success.
        path = args.plan_dir / f"body-code-merge-receipt-{plan['digest'][:16]}.json"
        write_immutable(path, receipt)
        print(json.dumps({**receipt, "receipt": str(path)}, sort_keys=True))
        return 0

    connection = engine.connect()
    transaction = connection.begin()
    try:
        plan = build_plan(connection)
        path = write_plan(plan, args.plan_dir)
        print(json.dumps({"status": "planned", "digest": plan["digest"],
                          "plan": str(path), "merges": [
                              {"old": m["old"], "new": m["new"],
                               "overlap_meetings": len(m["meeting_map"]),
                               "deduplicated_items": len(m["item_map"])}
                              for m in plan["merges"]]}, sort_keys=True))
        if args.rehearse:
            stats = execute_plan(connection, plan)
            print(json.dumps({"status": "rehearsed_rollback",
                              "digest": plan["digest"],
                              "post_counts": stats}, sort_keys=True))
        transaction.rollback()
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
