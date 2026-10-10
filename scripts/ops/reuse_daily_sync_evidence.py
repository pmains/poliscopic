#!/usr/bin/env python3
"""Find fresh, fully validated daily-sync evidence that can be reused safely."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.ops import daily_sync_backup as backup
from scripts.ops import daily_sync_gate as gate


def find_reusable(*, run_date: str, attempt_id: str, authorization_id: str,
                  backup_dir: Path = backup.DEFAULT_BACKUP_DIR,
                  sync_dir: Path | None = None) -> tuple[Path, Path] | None:
    receipts = sorted(
        backup_dir.glob(f"{backup.PREFIX}*.receipt.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for receipt_path in receipts:
        try:
            receipt = json.loads(receipt_path.read_text())
            if (receipt.get("run_date") != run_date or
                    receipt.get("authorization_id") != authorization_id):
                continue
            preflight_path = Path(receipt["preflight_path"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        verdict = gate.validate_pre_sync(
            run_date=run_date,
            attempt_id=attempt_id,
            authorization_id=authorization_id,
            preflight_path=preflight_path,
            backup_receipt_path=receipt_path,
            sync_dir=sync_dir,
        )
        if verdict.get("status") == "ALLOWED":
            return preflight_path.resolve(), receipt_path.resolve()
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--backup-dir", type=Path,
                        default=backup.DEFAULT_BACKUP_DIR)
    parser.add_argument("--sync-dir", type=Path)
    arguments = parser.parse_args(argv)
    match = find_reusable(
        run_date=arguments.run_date,
        attempt_id=arguments.attempt_id,
        authorization_id=arguments.authorization_id,
        backup_dir=arguments.backup_dir,
        sync_dir=arguments.sync_dir,
    )
    if match is None:
        return 1
    print(f"{match[0]}\t{match[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
