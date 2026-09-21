#!/usr/bin/env python3
"""Prove the receipt-function correction against an existing dump in scratch only."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.kg import stage2_artifacts as artifacts
from scripts.kg import stage3_processing_receipt_backup_run as backup
from scripts.kg.stage3_processing_receipt_store_backup import file_sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    dump = args.dump.resolve()
    baseline_path = args.baseline.resolve()
    if not dump.is_file() or not baseline_path.is_file():
        raise RuntimeError("dump and baseline must both exist")
    baseline = artifacts.load_verified(baseline_path)
    source = backup._source_or_refuse()
    port = backup._free_port()
    stopped = False
    try:
        with tempfile.TemporaryDirectory(prefix="poliscopic-receipt-correction-proof-") as temporary:
            cluster = Path(temporary) / "cluster"
            backup._cluster_run([str(backup.PG / "initdb"), "-A", "trust", "-U", "poliscopic",
                "-D", str(cluster), "--encoding=UTF8", "--locale=C"])
            backup._cluster_run([str(backup.PG / "pg_ctl"), "-D", str(cluster), "-l",
                str(Path(temporary) / "postgres.log"), "-o", f"-h 127.0.0.1 -p {port}",
                "-w", "start"], capture=False)
            try:
                backup._cluster_run([str(backup.PG / "createdb"), "-h", "127.0.0.1", "-p",
                    str(port), "-U", "poliscopic", backup.SCRATCH_DATABASE])
                backup._restore_and_compare(source, port=port,
                    scratch_name=backup.SCRATCH_DATABASE, dump_path=dump, baseline=baseline,
                    prove_schema_correction=True)
            finally:
                backup._cluster_run([str(backup.PG / "pg_ctl"), "-D", str(cluster), "-m",
                    "fast", "-w", "stop"], capture=False)
                stopped = True
        statements = backup._schema_correction_statements()
        body = {
            "kind": "kg-stage3-processing-receipt-schema-correction-proof",
            "version": "1.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": "disposable scratch database only",
            "dump_path": str(dump),
            "dump_sha256": file_sha256(dump),
            "baseline_path": str(baseline_path),
            "correction_sha256": hashlib.sha256(
                "\n".join(statements).encode("utf-8")).hexdigest(),
            "comparisons": {"dump_restored": True, "counts_match": True,
                "schema_match": True, "integrity_match": True,
                "exact_restored_equality": True},
            "teardown": {"server_stopped": stopped, "temp_dir_removed": True},
            "development_mutated": False,
            "production_contacted": False,
        }
        digest = artifacts.write_immutable(args.output, body)
        print(json.dumps({"outcome": "proved", "artifact": str(args.output),
                          "digest": digest}, sort_keys=True))
        return 0
    finally:
        source.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
