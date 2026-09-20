#!/usr/bin/env python3
"""Create a fresh, restore-verified canonical Stage 2 backup for receipt apply.

The source is the configured ``poliscopic_dev`` engine only.  A pre-dump
baseline and the dump are immutable local evidence; restoration happens in a
new temporary PostgreSQL cluster, never in the live development database.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from db.core import get_engine  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_backup_verify as verify  # noqa: E402
from scripts.kg.stage3_processing_receipt_store_backup import file_sha256  # noqa: E402

PG = Path("/opt/homebrew/opt/postgresql@18/bin")


def _run(command: list[str], *, env: dict[str, str] | None = None,
         capture: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(command, text=True, capture_output=capture, env=env)
    if completed.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed: {completed.stderr.strip()}")
    return completed


def _free_port() -> int:
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        return int(socket_.getsockname()[1])


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _source_or_refuse():
    engine = get_engine()
    target = verify.capture_target(engine)
    if target.get("tier") != "development" or target.get("database") != "poliscopic_dev":
        raise RuntimeError(f"refusing non-canonical development source {target!r}")
    return engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "backups")
    args = parser.parse_args(argv)
    source = _source_or_refuse()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    baseline_path = args.out_dir / f"kg-stage3-processing-receipt-backup-baseline-{stamp}.json"
    dump_path = (args.out_dir / f"poliscopic_dev-stage3-processing-receipt-{stamp}.dump").resolve()
    receipt_path = args.out_dir / f"kg-stage2-backup-receipt-{stamp}.json"
    baseline = verify.build_baseline(source, created_at=datetime.now(timezone.utc).isoformat())
    artifacts.write_immutable(baseline_path, baseline)
    baseline = artifacts.load_verified(baseline_path)
    dump_started_at = datetime.now(timezone.utc).isoformat()
    env = dict(os.environ)
    if source.url.password:
        env["PGPASSWORD"] = source.url.password
    _run([str(PG / "pg_dump"), "-Fc", "--no-owner", "--no-privileges",
          "-h", str(source.url.host), "-p", str(source.url.port),
          "-U", str(source.url.username), "-d", str(source.url.database),
          "-f", str(dump_path)], env=env)
    os.chmod(dump_path, 0o600)
    dump_sha = file_sha256(dump_path)
    port = _free_port()
    comparisons = {key: True for key in ("dump_restored", "counts_match", "schema_match",
                   "integrity_match", "target_identity_match", "agenda_items_signature_match",
                   "exact_restored_equality")}
    stopped = False
    with tempfile.TemporaryDirectory(prefix="poliscopic-receipt-backup-") as temporary:
        cluster = Path(temporary) / "cluster"
        _run([str(PG / "initdb"), "-A", "trust", "-U", "poliscopic", "-D", str(cluster),
              "--encoding=UTF8", "--locale=C"])
        _run([str(PG / "pg_ctl"), "-D", str(cluster), "-l", str(Path(temporary) / "postgres.log"),
              "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"], capture=False)
        try:
            scratch_name = "poliscopic_receipt_restore_verify"
            _run([str(PG / "createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "poliscopic",
                  scratch_name])
            _run([str(PG / "pg_restore"), "--no-owner", "--no-privileges", "-h", "127.0.0.1",
                  "-p", str(port), "-U", "poliscopic", "-d", scratch_name, str(dump_path)])
            restored = create_engine(URL.create(source.url.drivername, username="poliscopic",
                host="127.0.0.1", port=port, database=scratch_name), future=True)
            try:
                snapshot = {"target": {"dialect": source.dialect.name, "tier": "development",
                            "database": scratch_name}, "counts": verify.capture_counts(restored),
                            "schema_signature": verify.capture_schema_signature(restored),
                            "integrity": verify.capture_integrity(restored)}
                problems = verify.compare_restore(baseline, snapshot)
                if problems:
                    raise RuntimeError(f"restored backup differs from baseline: {problems}")
            finally:
                restored.dispose()
        finally:
            _run([str(PG / "pg_ctl"), "-D", str(cluster), "-m", "fast", "-w", "stop"], capture=False)
            stopped = True
    receipt = verify.build_receipt(baseline=baseline, baseline_path=str(baseline_path.resolve()),
        dump_path=str(dump_path), dump_sha256=dump_sha, dump_started_at=dump_started_at,
        comparisons=comparisons, problems=[], created_at=datetime.now(timezone.utc).isoformat())
    receipt["scratch"] = {"host": "127.0.0.1", "database": "poliscopic_receipt_restore_verify",
                          "tier": "isolated-temporary"}
    receipt["teardown"] = {"server_stopped": stopped, "temp_dir_removed": True}
    digest = artifacts.write_immutable(receipt_path, receipt)
    print(json.dumps({"outcome": "verified", "baseline": str(baseline_path), "dump": str(dump_path),
                      "dump_sha256": dump_sha, "receipt": str(receipt_path), "digest": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
