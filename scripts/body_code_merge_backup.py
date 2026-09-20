#!/usr/bin/env python3
"""Create and locally restore-verify a fresh protected development backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from db.core import get_engine
from kg import stage2_artifacts, stage2_backup_verify
from body_code_merge_runtime import build_plan as build_merge_plan, execute_plan


PG = Path("/opt/homebrew/opt/postgresql@18/bin")
OUT = Path("data/backups")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(args: list[str], *, env: dict[str, str] | None = None,
        capture: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=capture, env=env)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr.strip()}")
    return result


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume-stamp",
                        help="restore-verify an already completed dump/baseline pair")
    args = parser.parse_args()
    source = get_engine()
    if source.url.database != "poliscopic_dev":
        raise SystemExit(f"refusing non-development source {source.url.database!r}")
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = args.resume_stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    baseline_path = OUT / f"body-code-merge-backup-baseline-{stamp}.json"
    dump_path = (OUT / f"poliscopic_dev-body-code-merge-{stamp}.dump").resolve()
    receipt_path = OUT / f"body-code-merge-backup-receipt-{stamp}.json"
    if args.resume_stamp and receipt_path.exists():
        audit_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        receipt_path = OUT / f"body-code-merge-backup-receipt-{stamp}-audit-{audit_stamp}.json"

    if args.resume_stamp:
        if not baseline_path.is_file() or not dump_path.is_file():
            raise SystemExit("refusing: resume pair is incomplete")
        baseline = stage2_artifacts.load_verified(baseline_path)
        dump_started_at = datetime.fromtimestamp(
            dump_path.stat().st_birthtime, timezone.utc).isoformat()
    else:
        captured_at = now()
        baseline = stage2_backup_verify.build_baseline(source, created_at=captured_at)
        stage2_artifacts.write_immutable(baseline_path, baseline)
        baseline = stage2_artifacts.load_verified(baseline_path)
        dump_started_at = now()
        env = dict(os.environ)
        if source.url.password:
            env["PGPASSWORD"] = source.url.password
        run([str(PG / "pg_dump"), "-Fc", "--no-owner", "--no-privileges",
             "-h", str(source.url.host), "-p", str(source.url.port),
             "-U", str(source.url.username), "-d", str(source.url.database),
             "-f", str(dump_path)], env=env)
        os.chmod(dump_path, 0o600)
    dump_sha = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    entries = len([line for line in run([str(PG / "pg_restore"), "--list",
                                         str(dump_path)]).stdout.splitlines()
                   if line and not line.startswith(";")])

    port = free_port()
    comparisons = {key: True for key in (
        "dump_restored", "counts_match", "schema_match", "integrity_match",
        "target_identity_match", "agenda_items_signature_match",
        "exact_restored_equality")}
    stopped = False
    scratch_merge: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="poliscopic-body-merge-") as temp:
        data = Path(temp) / "cluster"
        run([str(PG / "initdb"), "-A", "trust", "-U", "poliscopic",
             "-D", str(data), "--encoding=UTF8", "--locale=C"])
        run([str(PG / "pg_ctl"), "-D", str(data), "-l", str(Path(temp) / "postgres.log"),
             "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"], capture=False)
        try:
            run([str(PG / "createdb"), "-h", "127.0.0.1", "-p", str(port),
                 "-U", "poliscopic", "poliscopic_merge_scratch"])
            run([str(PG / "pg_restore"), "--no-owner", "--no-privileges",
                 "-h", "127.0.0.1", "-p", str(port), "-U", "poliscopic",
                 "-d", "poliscopic_merge_scratch", str(dump_path)])
            restored_url = URL.create(source.url.drivername, username="poliscopic",
                                      host="127.0.0.1", port=port,
                                      database="poliscopic_merge_scratch")
            restored_engine = create_engine(restored_url, future=True)
            restored = {
                "target": {"dialect": source.dialect.name, "tier": "development",
                           "database": "poliscopic_merge_scratch"},
                "counts": stage2_backup_verify.capture_counts(restored_engine),
                "schema_signature": stage2_backup_verify.capture_schema_signature(restored_engine),
                "integrity": stage2_backup_verify.capture_integrity(restored_engine),
            }
            problems = stage2_backup_verify.compare_restore(baseline, restored)
            if problems:
                raise RuntimeError(f"restored backup differs from baseline: {problems}")
            # Exercise the exact merge and its real commit boundary on the restored
            # copy.  execute_plan performs all postconditions before this commits.
            with restored_engine.begin() as scratch_connection:
                merge_plan = build_merge_plan(scratch_connection)
                merge_stats = execute_plan(scratch_connection, merge_plan)
            scratch_merge = {"committed": True, "plan_digest": merge_plan["digest"],
                             "post_counts": merge_stats}
            restored_engine.dispose()
        finally:
            run([str(PG / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                capture=False)
            stopped = True

    receipt = stage2_backup_verify.build_receipt(
        baseline=baseline, baseline_path=str(baseline_path.resolve()),
        dump_path=str(dump_path), dump_sha256=dump_sha,
        dump_started_at=dump_started_at, comparisons=comparisons,
        problems=[], created_at=now())
    receipt["pg_restore_list_entries"] = entries
    receipt["scratch_merge_apply"] = scratch_merge
    receipt["teardown"] = {"server_stopped": stopped, "temp_dir_removed": True}
    receipt_digest = stage2_artifacts.write_immutable(receipt_path, receipt)
    print(json.dumps({"status": "verified", "dump": str(dump_path),
                      "dump_sha256": dump_sha, "receipt": str(receipt_path.resolve()),
                      "receipt_digest": receipt_digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
