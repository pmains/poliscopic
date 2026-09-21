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
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import create_engine, text
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
RECEIPT_TABLE = "public.processing_receipts"
SCRATCH_DATABASE = "poliscopic_receipt_restore_verify"
RESTORE_SECTIONS = ("pre-data", "data", "post-data")


def _failure_detail(completed: subprocess.CompletedProcess, command: list[str]) -> str:
    """Describe a failed command without dereferencing an uncaptured stream.

    ``capture=False`` leaves ``stdout``/``stderr`` set to ``None``.  Reporting the
    failure must never mask the original command and exit status by touching them
    blindly; the exit status is always reported, output only when it was captured.
    """
    detail = (completed.stderr or completed.stdout or "").strip()
    if detail:
        return f"{Path(command[0]).name} failed ({completed.returncode}): {detail}"
    return f"{Path(command[0]).name} failed with exit status {completed.returncode}"


def _run(command: list[str], *, env: dict[str, str] | None = None,
         capture: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(command, text=True, capture_output=capture, env=env)
    if completed.returncode:
        raise RuntimeError(_failure_detail(completed, command))
    return completed


def _cluster_env() -> dict[str, str]:
    """Environment for temporary-cluster commands.

    PostgreSQL 18 refuses a postmaster that becomes multithreaded during startup,
    which is what macOS locale initialization does when no locale is resolvable.
    Naming the C locale explicitly keeps the start single-threaded and therefore
    independent of whatever environment launched this script.
    """
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}


def _cluster_run(command: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    """Run one temporary-cluster command under that explicit locale."""
    return _run(command, env=_cluster_env(), capture=capture)


def _mentions(text_value: str, name: str) -> bool:
    """True when ``name`` occurs in ``text_value`` as a whole identifier."""
    if not name:
        return False
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text_value) is not None


def _dump_function_signatures(dump_path: Path) -> dict[str, set[str]]:
    """Public function signatures the dump declares: name -> identity arguments.

    Read from the dump's own table of contents, so the required set is never
    derived from the scratch catalog that is being validated.
    """
    completed = _run([str(PG / "pg_restore"), "-l", str(dump_path)])
    declared: dict[str, set[str]] = {}
    for line in str(completed.stdout or "").splitlines():
        if " FUNCTION " not in line:
            continue
        fields = line.split(" FUNCTION ", 1)[1].split()
        if len(fields) < 2 or fields[0] != "public":
            continue
        name, opened, arguments = fields[1].partition("(")
        if not name or not opened or not arguments.endswith(")"):
            continue
        declared.setdefault(name, set()).add(arguments[:-1])
    return declared


def _generated_expression_texts(connection: Any) -> list[str]:
    """Expressions of the generated columns on the receipt table."""
    rows = connection.execute(text(
        "SELECT pg_get_expr(ad.adbin, ad.adrelid) AS expression "
        "FROM pg_attribute a "
        "JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
        "WHERE a.attrelid = to_regclass(:table) AND a.attgenerated <> ''"),
        {"table": RECEIPT_TABLE}).scalars().all()
    return [str(row) for row in rows if row]


def _installed_function_bodies(connection: Any, name: str) -> list[str]:
    """Definitions that the scratch catalog currently holds for that function."""
    rows = connection.execute(text(
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = :name"), {"name": name}).scalars().all()
    return [str(row) for row in rows if row]


def _required_generated_functions(connection: Any, *,
                                  declared: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """Signatures the generated expressions need, closed over installed bodies.

    Anchored on the dump's declaration so an undeclared function cannot satisfy its
    own requirement, and closed over the bodies the scratch catalog already holds so
    a helper reached only through another function is still required before any data
    is loaded.
    """
    required: dict[str, set[str]] = {}
    frontier = _generated_expression_texts(connection)
    visited: set[str] = set()
    while frontier:
        current = frontier.pop()
        for name, signatures in declared.items():
            if name in visited or not _mentions(current, name):
                continue
            visited.add(name)
            required.setdefault(name, set()).update(signatures)
            frontier.extend(_installed_function_bodies(connection, name))
    return required


def _missing_generated_functions(connection: Any, *,
                                 declared: Mapping[str, set[str]]) -> list[str]:
    """Refuse before the data section when a required function is absent or mismatched."""
    required = _required_generated_functions(connection, declared=declared)
    if not required:
        return [f"no generated-column function requirement could be derived for {RECEIPT_TABLE}"]
    problems: list[str] = []
    for name in sorted(required):
        for arguments in sorted(required[name]):
            signature = f"public.{name}({arguments})"
            present = connection.execute(text("SELECT to_regprocedure(:signature) IS NOT NULL"),
                                         {"signature": signature}).scalar()
            if not present:
                problems.append(
                    f"generated-column function is absent in the scratch catalog: {signature}")
    return problems


def _restore_section(port: int, scratch_name: str, dump_path: Path, section: str) -> None:
    """Restore exactly one dump section into the scratch database."""
    _cluster_run([str(PG / "pg_restore"), "--no-owner", "--no-privileges",
                  f"--section={section}", "-h", "127.0.0.1", "-p", str(port),
                  "-U", "poliscopic", "-d", scratch_name, str(dump_path)])


def _restore_and_compare(source: Any, *, port: int, scratch_name: str, dump_path: Path,
                         baseline: Mapping[str, Any]) -> None:
    """Restore sectionally, prove the receipt functions resolve, then compare exactly.

    The data section loads only after the catalog confirms that every function the
    generated expressions on the receipt table need is present with the signature the
    dump declares.  A dump that cannot reconstruct those functions in its own pre-data
    section is refused rather than silently restored with an incomplete receipt table.
    """
    _restore_section(port, scratch_name, dump_path, "pre-data")
    restored = create_engine(URL.create(source.url.drivername, username="poliscopic",
        host="127.0.0.1", port=port, database=scratch_name), future=True)
    try:
        declared = _dump_function_signatures(dump_path)
        with restored.connect() as connection:
            problems = _missing_generated_functions(connection, declared=declared)
        if problems:
            raise RuntimeError(f"scratch schema cannot restore {RECEIPT_TABLE}: {problems}")
        _restore_section(port, scratch_name, dump_path, "data")
        _restore_section(port, scratch_name, dump_path, "post-data")
        snapshot = {"target": {"dialect": source.dialect.name, "tier": "development",
                    "database": scratch_name}, "counts": verify.capture_counts(restored),
                    "schema_signature": verify.capture_schema_signature(restored),
                    "integrity": verify.capture_integrity(restored)}
        differences = verify.compare_restore(baseline, snapshot)
        if differences:
            raise RuntimeError(f"restored backup differs from baseline: {differences}")
    finally:
        restored.dispose()


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
        _cluster_run([str(PG / "initdb"), "-A", "trust", "-U", "poliscopic", "-D", str(cluster),
                      "--encoding=UTF8", "--locale=C"])
        _cluster_run([str(PG / "pg_ctl"), "-D", str(cluster),
                      "-l", str(Path(temporary) / "postgres.log"),
                      "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"], capture=False)
        try:
            scratch_name = SCRATCH_DATABASE
            _cluster_run([str(PG / "createdb"), "-h", "127.0.0.1", "-p", str(port),
                          "-U", "poliscopic", scratch_name])
            _restore_and_compare(source, port=port, scratch_name=scratch_name,
                                 dump_path=dump_path, baseline=baseline)
        finally:
            _cluster_run([str(PG / "pg_ctl"), "-D", str(cluster), "-m", "fast", "-w", "stop"],
                         capture=False)
            stopped = True
    receipt = verify.build_receipt(baseline=baseline, baseline_path=str(baseline_path.resolve()),
        dump_path=str(dump_path), dump_sha256=dump_sha, dump_started_at=dump_started_at,
        comparisons=comparisons, problems=[], created_at=datetime.now(timezone.utc).isoformat())
    receipt["scratch"] = {"host": "127.0.0.1", "database": SCRATCH_DATABASE,
                          "tier": "isolated-temporary"}
    receipt["teardown"] = {"server_stopped": stopped, "temp_dir_removed": True}
    digest = artifacts.write_immutable(receipt_path, receipt)
    print(json.dumps({"outcome": "verified", "baseline": str(baseline_path), "dump": str(dump_path),
                      "dump_sha256": dump_sha, "receipt": str(receipt_path), "digest": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
