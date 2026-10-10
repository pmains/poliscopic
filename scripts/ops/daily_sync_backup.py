#!/usr/bin/env python3
"""Create a rolling, restore-verified production backup for the daily sync.

The backup is read-only against production.  It captures one exported snapshot,
dumps the public schema from that snapshot, restores the dump into an isolated
temporary PostgreSQL cluster, and compares every public-table count, the schema
projection, and the entity-integrity snapshot.  Only then is a VALID immutable
receipt written.

Retention is deliberately small: after a new receipt verifies, the five newest
verified generations are retained and older verified generations are removed.
An unsuccessful attempt never prunes a known-good generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL

REPO = Path(__file__).resolve().parents[2]
for _candidate in (REPO, REPO / "scripts", Path(__file__).resolve().parent):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import production_interlock  # noqa: E402
import production_preflight  # noqa: E402
from db.tier import PRODUCTION, TierError, require_role_url  # noqa: E402
from scripts.entities.detect_entities import integrity_snapshot  # noqa: E402

SCHEMA = "daily-production-backup/1"
BASELINE_SCHEMA = "daily-production-backup-baseline/1"
RETENTION_GENERATIONS = 5
PG = Path("/opt/homebrew/opt/postgresql@18/bin")
DEFAULT_BACKUP_DIR = REPO / "data" / "backups" / "daily-production"
PREFIX = "daily-production-"


class Refused(RuntimeError):
    """A fail-closed backup refusal."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       default=str) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def run_command(arguments: list[str], *, env: Mapping[str, str] | None = None,
                stdout: Any = subprocess.PIPE) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, stdout=stdout, stderr=subprocess.PIPE,
                            env=env, text=stdout is subprocess.PIPE)
    if result.returncode:
        detail = result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
        raise Refused(f"{Path(arguments[0]).name} failed (rc={result.returncode}): "
                      f"{(detail or '').strip()}")
    return result


def require_pg18() -> None:
    for binary in ("pg_dump", "pg_restore", "initdb", "pg_ctl", "createdb", "psql"):
        path = PG / binary
        if not path.is_file():
            raise Refused(f"required PostgreSQL 18 binary is absent: {path}")
        if binary in {"pg_dump", "pg_restore"}:
            output = run_command([str(path), "--version"]).stdout
            if " 18" not in output:
                raise Refused(f"{binary} is not PostgreSQL 18")


def production_url() -> str:
    from dotenv import dotenv_values

    raw = os.environ.get("PROD_DATABASE_URL") or dotenv_values(REPO / ".env").get(
        "PROD_DATABASE_URL")
    if not raw:
        raise Refused("PROD_DATABASE_URL is not configured")
    try:
        return require_role_url(PRODUCTION, raw, label="daily production backup")
    except TierError as exc:
        raise Refused(f"production URL rejected: {exc}") from exc


def schema_projection(connection: Any) -> dict[str, list[dict[str, Any]]]:
    inspector = inspect(connection)
    projection: dict[str, list[dict[str, Any]]] = {}
    for table in sorted(inspector.get_table_names(schema="public")):
        projection[table] = [
            {"name": column["name"], "type": str(column["type"]),
             "nullable": bool(column.get("nullable", True))}
            for column in inspector.get_columns(table, schema="public")
        ]
    if not projection:
        raise Refused("public schema contains no tables")
    return projection


def snapshot(connection: Any, *, target: Mapping[str, Any], captured_at: str) -> dict:
    schema = schema_projection(connection)
    counts = {
        table: int(connection.execute(
            text(f'SELECT COUNT(*) FROM "public"."{table}"')).scalar_one())
        for table in schema
    }
    integrity = dict(sorted(integrity_snapshot(connection).items()))
    body = {
        "schema": BASELINE_SCHEMA,
        "captured_at": captured_at,
        "target": dict(target),
        "counts": counts,
        "counts_digest": digest(counts),
        "schema_projection": schema,
        "schema_digest": digest(schema),
        "integrity": integrity,
        "integrity_digest": digest(integrity),
    }
    return {**body, "digest": digest(body)}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def capture_restored(url: URL, source: Mapping[str, Any]) -> dict[str, Any]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            return snapshot(connection, target=source["target"],
                            captured_at=datetime.now(timezone.utc).isoformat())
    finally:
        engine.dispose()


def compare(source: Mapping[str, Any], restored: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in ("counts", "schema_projection", "integrity"):
        if restored.get(field) != source.get(field):
            problems.append(f"restored {field} differs from exported snapshot")
    return problems


def verified_generation(receipt_path: Path, *, backup_dir: Path) -> dict[str, Path] | None:
    """Return safe paths for one valid generation, otherwise leave it untouched."""
    try:
        receipt = json.loads(receipt_path.read_text())
        body = {key: value for key, value in receipt.items() if key != "digest"}
        if (receipt.get("schema") != SCHEMA or receipt.get("status") != "VALID" or
                receipt.get("digest") != digest(body)):
            return None
        paths = {name: Path(receipt[name]).resolve()
                 for name in ("baseline_path", "dump_path")}
        paths["receipt_path"] = receipt_path.resolve()
        root = backup_dir.resolve()
        if any(path.parent != root or not path.name.startswith(PREFIX)
               for path in paths.values()):
            return None
        if not all(path.is_file() for path in paths.values()):
            return None
        if sha256_file(paths["dump_path"]) != receipt.get("dump_sha256"):
            return None
        return paths
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def prune_verified(*, backup_dir: Path = DEFAULT_BACKUP_DIR,
                   keep: int = RETENTION_GENERATIONS) -> list[str]:
    """Prune only verified generations older than the newest ``keep`` receipts."""
    if keep < 1:
        raise ValueError("at least one verified generation must be retained")
    verified: list[tuple[Path, dict[str, Path]]] = []
    for receipt in sorted(backup_dir.glob(f"{PREFIX}*.receipt.json")):
        paths = verified_generation(receipt, backup_dir=backup_dir)
        if paths is not None:
            verified.append((receipt, paths))
    removed: list[str] = []
    for _receipt, paths in verified[:-keep]:
        for path in paths.values():
            if path.exists():
                path.unlink()
                removed.append(str(path))
    return removed


def prune_unverified_attempts(*, backup_dir: Path = DEFAULT_BACKUP_DIR,
                              older_than_seconds: int = 3600,
                              now: float | None = None) -> list[str]:
    """Remove stale partial generations that never produced a valid receipt.

    An in-progress dump is protected by the age threshold.  A generation with a
    valid receipt is always preserved and remains governed by ``prune_verified``.
    """
    now = time.time() if now is None else now
    removed: list[str] = []
    stems: set[str] = set()
    for path in backup_dir.glob(f"{PREFIX}*.baseline.json"):
        stems.add(path.name.removesuffix(".baseline.json"))
    for path in backup_dir.glob(f"{PREFIX}*.dump"):
        stems.add(path.name.removesuffix(".dump"))

    for stem in sorted(stems):
        baseline = backup_dir / f"{stem}.baseline.json"
        dump = backup_dir / f"{stem}.dump"
        receipt = backup_dir / f"{stem}.receipt.json"
        if receipt.exists() and verified_generation(receipt, backup_dir=backup_dir):
            continue
        existing = [path for path in (baseline, dump, receipt) if path.exists()]
        if not existing or any(now - path.stat().st_mtime < older_than_seconds
                               for path in existing):
            continue
        for path in existing:
            path.unlink()
            removed.append(str(path))
    return removed


def create_backup(*, run_date: str, preflight_path: Path,
                  authorization_id: str,
                  backup_dir: Path = DEFAULT_BACKUP_DIR) -> dict[str, Any]:
    verdict = production_interlock.check(
        "OP-PREFLIGHT", entry_point="scripts/ops/daily_sync_backup.py")
    if verdict.get("status") != "ALLOWED":
        raise Refused(f"read-only interlock refused: {verdict.get('code')}")
    try:
        preflight = json.loads(preflight_path.read_text())
    except Exception as exc:
        raise Refused("preflight artifact is unreadable") from exc
    preflight_body = {key: value for key, value in preflight.items() if key != "digest"}
    if (preflight.get("schema") != production_preflight.SCHEMA or
            preflight.get("status") != "VALID" or
            preflight.get("digest") != production_preflight.digest(preflight_body)):
        raise Refused("preflight artifact is invalid")

    require_pg18()
    backup_dir.mkdir(parents=True, exist_ok=True)
    prune_unverified_attempts(backup_dir=backup_dir)
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = PREFIX + tag
    baseline_path = (backup_dir / f"{stem}.baseline.json").resolve()
    dump_path = (backup_dir / f"{stem}.dump").resolve()
    receipt_path = (backup_dir / f"{stem}.receipt.json").resolve()
    if any(path.exists() for path in (baseline_path, dump_path, receipt_path)):
        raise Refused("backup output generation already exists")

    engine = create_engine(production_url(), future=True, pool_pre_ping=True)
    transaction = connection = None
    try:
        connection = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        transaction = connection.begin()
        connection.execute(text("SET TRANSACTION READ ONLY"))
        exported = str(connection.execute(text("SELECT pg_export_snapshot()")).scalar_one())
        live_target = production_preflight.capture_snapshot(
            connection,
            configured_host=str(engine.url.host or ""),
            configured_port=engine.url.port,
            dialect=str(engine.dialect.name or ""),
            driver=str(engine.dialect.driver or ""),
            captured_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )["target"]
        if live_target != preflight.get("target"):
            raise Refused("live production target differs from the bound preflight")
        baseline = snapshot(connection, target=live_target,
                            captured_at=datetime.now(timezone.utc).isoformat())
        write_exclusive(baseline_path, baseline)

        environment = dict(os.environ)
        environment.update({"LC_ALL": "C", "LANG": "C"})
        if engine.url.password:
            environment["PGPASSWORD"] = engine.url.password
        command = [str(PG / "pg_dump"), "-Fc", "--no-owner", "--no-privileges",
                   "--schema=public", "--exclude-schema=dev",
                   f"--snapshot={exported}", "-h", str(engine.url.host),
                   "-p", str(engine.url.port), "-U", str(engine.url.username),
                   "-d", str(engine.url.database)]
        fd = os.open(dump_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            run_command(command, env=environment, stdout=output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(dump_path, 0o600)
        transaction.commit()
        transaction = None
    finally:
        if transaction is not None:
            transaction.rollback()
        if connection is not None:
            connection.close()
        engine.dispose()

    port = free_port()
    restored: dict[str, Any]
    stopped = False
    environment = dict(os.environ, LC_ALL="C", LANG="C")
    with tempfile.TemporaryDirectory(prefix="poliscopic-daily-restore-") as temp:
        cluster = Path(temp) / "cluster"
        run_command([str(PG / "initdb"), "-A", "trust", "-U", "poliscopic",
                     "-D", str(cluster), "--encoding=UTF8", "--locale=C"],
                    env=environment)
        run_command([str(PG / "pg_ctl"), "-D", str(cluster), "-l",
                     str(Path(temp) / "postgres.log"), "-o",
                     f"-h 127.0.0.1 -p {port}", "-w", "start"], env=environment)
        try:
            database = "poliscopic_daily_restore"
            run_command([str(PG / "createdb"), "-h", "127.0.0.1", "-p", str(port),
                         "-U", "poliscopic", database], env=environment)
            run_command([str(PG / "psql"), "-v", "ON_ERROR_STOP=1", "-h",
                         "127.0.0.1", "-p", str(port), "-U", "poliscopic",
                         "-d", database, "-c", "DROP SCHEMA IF EXISTS public CASCADE"],
                        env=environment)
            run_command([str(PG / "pg_restore"), "--no-owner", "--no-privileges",
                         "-h", "127.0.0.1", "-p", str(port), "-U", "poliscopic",
                         "-d", database, str(dump_path)], env=environment)
            restored_url = URL.create("postgresql+psycopg2", username="poliscopic",
                                      host="127.0.0.1", port=port, database=database)
            restored = capture_restored(restored_url, baseline)
        finally:
            run_command([str(PG / "pg_ctl"), "-D", str(cluster), "-m", "fast",
                         "-w", "stop"], env=environment)
            stopped = True

    problems = compare(baseline, restored)
    if problems:
        raise Refused("; ".join(problems))
    receipt_body = {
        "schema": SCHEMA,
        "status": "VALID",
        "run_date": run_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "authorization_id": authorization_id,
        "preflight_path": str(preflight_path.resolve()),
        "preflight_digest": preflight["digest"],
        "baseline_path": str(baseline_path),
        "baseline_digest": baseline["digest"],
        "dump_path": str(dump_path),
        "dump_sha256": sha256_file(dump_path),
        "dump_bytes": dump_path.stat().st_size,
        "target": baseline["target"],
        "comparisons": {"all_public_table_counts": True,
                        "schema_projection": True, "integrity": True},
        "restore": {"isolated_temporary_cluster": True,
                    "server_stopped": stopped, "temporary_data_removed": True},
        "retention_generations": RETENTION_GENERATIONS,
    }
    receipt = {**receipt_body, "digest": digest(receipt_body)}
    write_exclusive(receipt_path, receipt)
    removed = prune_verified(backup_dir=backup_dir)
    return {**receipt, "receipt_path": str(receipt_path), "pruned": removed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    arguments = parser.parse_args(argv)
    try:
        result = create_backup(run_date=arguments.run_date,
                               preflight_path=arguments.preflight,
                               authorization_id=arguments.authorization_id,
                               backup_dir=arguments.backup_dir)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"status": result["status"],
                      "receipt": result["receipt_path"],
                      "digest": result["digest"],
                      "dump_bytes": result["dump_bytes"],
                      "pruned": result["pruned"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
