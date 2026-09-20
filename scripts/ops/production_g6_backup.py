#!/usr/bin/env python3
"""G6 fresh backup and off-volume scratch-restore proof for OP-REPAIR.

There is intentionally no apply mode.  Production is held in one exported,
read-only snapshot while its baseline and public-only custom dump are captured.
The dump is copied to the Windows development host, restored into one uniquely
named disposable database, compared exactly, and force-dropped.  A VALID
receipt can exist only after absence of that scratch database is proved.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _path in (REPO, REPO / "scripts", Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import production_interlock  # noqa: E402
import production_preflight as preflight  # noqa: E402

SCHEMA = "production-g6-backup-proof/1"
BASELINE_SCHEMA = "production-g6-baseline/1"
CANDIDATE_SCHEMA = "production-reference-repair-plan/1"
PG = Path("/opt/homebrew/opt/postgresql@18/bin")
REMOTE = os.environ.get("POLISCOPIC_DEV_SSH_HOST", "development-host")
REMOTE_DIR = os.environ.get(
    "POLISCOPIC_OFF_VOLUME_BACKUP_DIR", r"C:\retention-hold\g6")
SCRATCH_PREFIX = "poliscopic_g6_scratch_"
DEFAULT_OUT = REPO / "data" / "audit"
DEFAULT_BACKUPS = REPO / "data" / "backups"


class Refused(RuntimeError):
    """Fail-closed G6 refusal."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Refused(f"{label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise Refused(f"{label} is not an object")
    return value


def load_candidate(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    candidate = _load_object(path, "candidate")
    body = {key: value for key, value in candidate.items() if key != "digest"}
    semantic = preflight.digest(body)
    if candidate.get("digest") != semantic:
        raise Refused("candidate semantic digest mismatch")
    if (candidate.get("schema") != CANDIDATE_SCHEMA or
            candidate.get("operation") != "OP-REPAIR" or
            candidate.get("status") != "CANDIDATE-NOT-AUTHORIZABLE" or
            candidate.get("apply_blocked") is not True):
        raise Refused("candidate is not an apply-blocked OP-REPAIR candidate")
    counts = candidate.get("counts") or {}
    proposals = candidate.get("proposals")
    if (not isinstance(proposals, list) or
            counts.get("proposals") != len(proposals)):
        raise Refused("candidate proposal count does not match its population")
    target = candidate.get("target") or {}
    required = ("database", "configured_host", "configured_port",
                "server_address", "server_port", "cluster_system_identifier")
    if any(target.get(key) in (None, "") for key in required):
        raise Refused("candidate target identity is incomplete")
    age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
    if age < -300 or age > 24 * 3600:
        raise Refused("candidate is not fresh enough for a G6 proof")
    return candidate, {"semantic_digest": semantic,
                       "raw_sha256": sha256_file(path),
                       "path": str(path.resolve())}


def compare_target(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    for key, value in expected.items():
        if actual.get(key) != value:
            raise Refused(f"live target differs from candidate at {key}")


def _normalized(value: Any) -> Any:
    """Normalize driver-native dates/decimals to their reviewed JSON forms."""
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _proposal_groups(candidate: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, int]] = set()
    proposals = candidate.get("proposals")
    if not isinstance(proposals, list):
        raise Refused("candidate proposals are absent")
    for proposal in proposals:
        table = proposal.get("table") if isinstance(proposal, dict) else None
        primary = proposal.get("primary_key") if isinstance(proposal, dict) else None
        before = proposal.get("before") if isinstance(proposal, dict) else None
        row_id = primary.get("id") if isinstance(primary, dict) else None
        if (table not in {"meetings", "agenda_items"} or
                not isinstance(row_id, int) or not isinstance(before, dict) or
                before.get("id") != row_id):
            raise Refused("candidate contains a malformed proposal preimage")
        key = (table, row_id)
        if key in seen:
            raise Refused("candidate contains duplicate proposal identities")
        seen.add(key)
        fields = tuple(sorted(before))
        if any(not re.fullmatch(r"[a-z_][a-z0-9_]*", field) for field in fields):
            raise Refused("candidate preimage contains an unsafe field name")
        group = groups.setdefault(table, {"fields": fields, "rows": {}})
        if group["fields"] != fields:
            raise Refused(f"candidate {table} preimages have inconsistent fields")
        group["rows"][row_id] = _normalized(before)
    return groups


def _preimage_proof(groups: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    canonical: list[dict[str, Any]] = []
    by_table: dict[str, int] = {}
    for table, group in sorted(groups.items()):
        expected_rows = group["rows"]
        actual_rows = observed.get(table) or {}
        if set(actual_rows) != set(expected_rows):
            raise Refused(f"{table} proposal preimage population differs")
        for row_id, expected in sorted(expected_rows.items()):
            actual = _normalized(actual_rows[row_id])
            if actual != expected:
                raise Refused(f"{table} proposal preimage drift at id {row_id}")
            canonical.append({"table": table, "id": row_id, "before": actual})
        by_table[table] = len(expected_rows)
    return {"count": len(canonical), "by_table": by_table,
            "digest": preflight.digest(canonical)}


def capture_proposal_preimages(connection: Any, candidate: Mapping[str, Any],
                               *, batch_size: int = 1000) -> dict[str, Any]:
    """Set-based exact preimage proof; at most ceil(N/1000) queries per table."""
    from sqlalchemy import bindparam, text

    groups = _proposal_groups(candidate)
    observed: dict[str, dict[int, dict[str, Any]]] = {}
    for table, group in sorted(groups.items()):
        fields = group["fields"]
        ids = sorted(group["rows"])
        rows: dict[int, dict[str, Any]] = {}
        columns = ", ".join(f'"{field}"' for field in fields)
        statement = text(
            f'SELECT {columns} FROM "public"."{table}" WHERE id IN :ids ORDER BY id'
        ).bindparams(bindparam("ids", expanding=True))
        for start in range(0, len(ids), batch_size):
            for row in connection.execute(statement, {"ids": ids[start:start + batch_size]}):
                mapping = dict(row._mapping)
                row_id = int(mapping["id"])
                if row_id in rows:
                    raise Refused(f"duplicate {table} preimage row {row_id}")
                rows[row_id] = mapping
        observed[table] = rows
    return _preimage_proof(groups, observed)


SCHEMA_SQL = """SELECT table_name, column_name, ordinal_position, udt_name,
is_nullable FROM information_schema.columns WHERE table_schema='public'
ORDER BY table_name, ordinal_position"""


def _schema_projection(connection: Any) -> list[dict[str, Any]]:
    from sqlalchemy import text

    return [{"table": row[0], "column": row[1], "ordinal": int(row[2]),
             "udt_name": row[3], "nullable": row[4] == "YES"}
            for row in connection.execute(text(SCHEMA_SQL)).fetchall()]


def capture_baseline(connection: Any, *, engine: Any, captured_at: str,
                     candidate_target: Mapping[str, Any]) -> dict[str, Any]:
    """Capture target, counts, schema and integrity on the exported snapshot."""
    from sqlalchemy import text
    from scripts.entities.detect_entities import integrity_snapshot

    scalar = lambda sql: connection.execute(text(sql)).scalar_one()
    actual = {
        "database": str(scalar("SELECT current_database()")),
        "configured_host": str(engine.url.host or ""),
        "configured_port": engine.url.port,
        "server_address": str(scalar("SELECT inet_server_addr()")),
        "server_port": int(scalar("SELECT inet_server_port()")),
        "cluster_system_identifier": str(scalar(
            "SELECT system_identifier FROM pg_control_system()")),
    }
    compare_target(candidate_target, actual)
    schema = _schema_projection(connection)
    if not schema:
        raise Refused("production public schema is empty")
    tables = sorted({column["table"] for column in schema})
    counts = {table: int(scalar(f'SELECT COUNT(*) FROM "public"."{table}"'))
              for table in tables}
    integrity = dict(sorted(integrity_snapshot(connection).items()))
    locale_row = connection.execute(text(
        "SELECT datlocprovider::text, datcollate, datctype FROM pg_database "
        "WHERE datname=current_database()"))
    locale_values = locale_row.one()
    body = {"schema": BASELINE_SCHEMA, "captured_at": captured_at,
            "target": actual, "counts": counts,
            "counts_sha256": preflight.digest(counts),
            "schema_projection": schema,
            "schema_sha256": preflight.digest(schema),
            "integrity": integrity,
            "integrity_sha256": preflight.digest(integrity),
            "source_locale": {"provider": str(locale_values[0]),
                              "collate": str(locale_values[1]),
                              "ctype": str(locale_values[2])}}
    return {**body, "digest": preflight.digest(body)}


def compare_restore(baseline: Mapping[str, Any], restored: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in ("counts", "schema_projection", "integrity", "proposal_preimages"):
        if restored.get(field) != baseline.get(field):
            problems.append(f"restored {field} differs from exported snapshot")
    if restored.get("encoding") != "UTF8":
        problems.append("scratch database encoding is not UTF8")
    return problems


def _run(args: list[str], *, env: Mapping[str, str] | None = None,
         input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, env=env,
                            input=input_text)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise Refused(f"{Path(args[0]).name} failed (rc={result.returncode}): {detail}")
    return result


def _require_pg18(version: str, label: str) -> None:
    if not re.search(r"\b18(?:\.|\b)", version):
        raise Refused(f"{label} is not PostgreSQL 18")


class LiveAdapter:
    """External operations, isolated so the full workflow is unit-testable."""

    last_verification_step: str | None = None

    @staticmethod
    def _powershell(script: str) -> subprocess.CompletedProcess[str]:
        """Cross the cmd.exe SSH boundary without exposing PowerShell syntax."""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                     REMOTE, "powershell.exe", "-NoProfile", "-NonInteractive",
                     "-EncodedCommand", encoded])

    def open_snapshot(self, expected_target: Mapping[str, Any]):
        from sqlalchemy import create_engine, text

        raw = preflight.resolve_production_url(REPO / ".env")
        engine = create_engine(raw, future=True, pool_pre_ping=True)
        connection = engine.connect().execution_options(isolation_level="SERIALIZABLE")
        transaction = connection.begin()
        connection.execute(text("SET TRANSACTION READ ONLY DEFERRABLE"))
        snapshot = str(connection.execute(text("SELECT pg_export_snapshot()")).scalar_one())
        return engine, connection, transaction, snapshot

    def dump(self, engine: Any, snapshot: str, path: Path) -> Mapping[str, Any]:
        if path.exists():
            raise Refused("dump path already exists")
        _require_pg18(_run([str(PG / "pg_dump"), "--version"]).stdout,
                      "local pg_dump")
        _require_pg18(_run([str(PG / "pg_restore"), "--version"]).stdout,
                      "local pg_restore")
        url = engine.url
        env = dict(os.environ)
        env.update({"LC_ALL": "C", "LANG": "C"})
        if url.password:
            env["PGPASSWORD"] = url.password
        command = [str(PG / "pg_dump"), "-Fc", "--no-owner", "--no-privileges",
                   "--schema=public", "--exclude-schema=dev",
                   f"--snapshot={snapshot}", "-h", str(url.host), "-p", str(url.port),
                   "-U", str(url.username), "-d", str(url.database)]
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                result = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE,
                                        env=env)
                handle.flush()
                os.fsync(handle.fileno())
            if result.returncode:
                raise Refused(f"pg_dump failed (rc={result.returncode})")
        except BaseException:
            # Preserve the exclusive partial image for diagnostics; it is never
            # called valid or copied because the failure interrupts the chain.
            raise
        listing = _run([str(PG / "pg_restore"), "--list", str(path)]).stdout
        from scripts.body_code_merge_prod import assert_public_dump_toc
        assert_public_dump_toc(listing)
        os.chmod(path, 0o600)
        return {"toc_sha256": hashlib.sha256(listing.encode()).hexdigest(),
                "toc_entries": len([line for line in listing.splitlines()
                                    if line and not line.startswith(";")])}

    def copy_off_volume(self, dump: Path, remote_path: str) -> Mapping[str, Any]:
        expected = REMOTE_DIR + "\\" + dump.name
        if (remote_path != expected or not re.fullmatch(
                r"[A-Za-z]:\\[A-Za-z0-9_.\\-]+", remote_path)):
            raise Refused("remote retention path is unsafe")
        self._powershell(
            f"New-Item -ItemType Directory -Force '{REMOTE_DIR}' | Out-Null")
        self._powershell(
            f"if (Test-Path -LiteralPath '{remote_path}') {{ exit 41 }}")
        _run(["scp", str(dump), f"{REMOTE}:{remote_path}"])
        command = (f"$f=Get-Item '{remote_path}'; $v=Get-Volume -DriveLetter C; "
                   "[pscustomobject]@{hash=(Get-FileHash -Algorithm SHA256 $f.FullName).Hash.ToLowerInvariant();"
                   "bytes=$f.Length;machine=$env:COMPUTERNAME;volume=$v.UniqueId}|ConvertTo-Json -Compress")
        raw = self._powershell(command).stdout
        try:
            metadata = json.loads(raw)
        except Exception as exc:
            raise Refused("off-volume metadata is not valid JSON") from exc
        if not all(metadata.get(key) not in (None, "")
                   for key in ("hash", "bytes", "machine", "volume")):
            raise Refused("off-volume metadata is incomplete")
        return metadata

    @staticmethod
    def _remote_psql(database: str, sql: str) -> str:
        prefix = r"$p='C:\Program Files\PostgreSQL\pgsql\bin\psql.exe'; "
        encoded = base64.b64encode(sql.encode()).decode()
        command = (prefix + f"$q=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
                   f"& $p -X -v ON_ERROR_STOP=1 -U poliscopic -d '{database}' -At -c $q")
        return LiveAdapter._powershell(command).stdout

    @staticmethod
    def _remote_admin(sql: str) -> str:
        binary = r'C:\Program Files\PostgreSQL\pgsql\bin\psql.exe'
        encoded = base64.b64encode(sql.encode()).decode()
        command = (f"$q=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
                   f"& '{binary}' -X -v ON_ERROR_STOP=1 -U postgres -d postgres -At -c $q")
        return LiveAdapter._powershell(command).stdout

    def create_scratch(self, name: str) -> Mapping[str, str]:
        if not re.fullmatch(r"poliscopic_g6_scratch_[0-9]{8}_[0-9]{6}", name):
            raise Refused("scratch database name is unsafe")
        sql = (f"CREATE DATABASE {name} WITH TEMPLATE template0 ENCODING 'UTF8' "
               "LC_COLLATE 'C' LC_CTYPE 'C' OWNER poliscopic")
        self._remote_admin(sql)
        if self._remote_admin(
                f"SELECT COUNT(*) FROM pg_database WHERE datname='{name}'").strip() != "1":
            raise Refused("scratch creation was not observable")
        meta = self._remote_psql(name,
            "SELECT pg_encoding_to_char(encoding)||E'\\t'||datcollate||E'\\t'||datctype "
            "FROM pg_database WHERE datname=current_database()")
        encoding, collate, ctype = meta.strip().split("\t")
        # A custom public-only dump carries CREATE SCHEMA public.  Remove the
        # disposable database's bootstrap schema first so --exit-on-error is a
        # meaningful strict restore rather than an expected duplicate failure.
        self._remote_psql(name, "DROP SCHEMA IF EXISTS public CASCADE")
        remaining = self._remote_psql(name,
            "SELECT COUNT(*) FROM pg_namespace WHERE nspname='public'").strip()
        if remaining != "0":
            raise Refused("scratch public schema absence was not proved")
        return {"encoding": encoding, "collate": collate, "ctype": ctype}

    def restore_off_volume(self, name: str, remote_path: str, *,
                           expected_sha256: str, expected_bytes: int,
                           staging_dir: Path) -> Mapping[str, Any]:
        """Restore the retained Windows copy with the local PG18 client.

        The Windows host has PostgreSQL 17.2 server/client binaries.  Archives
        created by pg_dump 18 must be read by pg_restore 18, so the retained
        copy is copied back to a unique local staging file, hash/size checked,
        and then restored over the development connection into *only* the
        caller-created scratch database.  Credentials remain in the process
        environment and never enter argv or receipts.
        """
        from dotenv import dotenv_values
        from sqlalchemy.engine import make_url

        if not name.startswith(SCRATCH_PREFIX):
            raise Refused("refusing to restore into a non-G6 database")
        filename = remote_path.rsplit("\\", 1)[-1]
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-production-op-repair\.dump", filename):
            raise Refused("off-volume restore path is unsafe")
        staged = staging_dir / f".{filename}.{name}.off-volume-copy"
        if staged.exists():
            raise Refused("off-volume restore staging path already exists")
        values = dotenv_values(REPO / ".env")
        raw_url = values.get("DATABASE_URL")
        if not raw_url:
            raise Refused("development DATABASE_URL is unavailable")
        url = make_url(str(raw_url))
        if (url.get_backend_name() != "postgresql" or
                url.database != "poliscopic_dev" or not url.host or
                not url.username or not url.password):
            raise Refused("development DATABASE_URL is not the protected dev target")
        ssh_config = _run(["ssh", "-G", REMOTE]).stdout.splitlines()
        approved_hosts = [line.split(None, 1)[1].strip() for line in ssh_config
                          if line.lower().startswith("hostname ")]
        if len(approved_hosts) != 1 or str(url.host) != approved_hosts[0]:
            raise Refused("development DATABASE_URL is not on the approved Windows host")
        env = dict(os.environ)
        env.update({"LC_ALL": "C", "LANG": "C", "PGHOST": str(url.host),
                    "PGPORT": str(url.port or 5432), "PGUSER": str(url.username),
                    "PGPASSWORD": str(url.password), "PGDATABASE": name})
        sslmode = url.query.get("sslmode")
        if sslmode:
            env["PGSSLMODE"] = str(sslmode)
        _require_pg18(_run([str(PG / "pg_restore"), "--version"]).stdout,
                      "local pg_restore")
        try:
            # Windows OpenSSH accepts native backslashes as an scp destination,
            # but its SFTP subsystem requires forward slashes when reading the
            # same absolute path back.
            inbound_path = remote_path.replace("\\", "/")
            _run(["scp", f"{REMOTE}:{inbound_path}", str(staged)])
            os.chmod(staged, 0o600)
            if staged.stat().st_size != expected_bytes:
                raise Refused("off-volume restore copy byte count mismatch")
            if sha256_file(staged) != expected_sha256:
                raise Refused("off-volume restore copy hash mismatch")
            _run([str(PG / "pg_restore"), "--exit-on-error", "--no-owner",
                  "--no-privileges", "-d", name, str(staged)], env=env)
            return {"client_major": 18, "source": "off-volume-roundtrip",
                    "sha256_verified": True, "bytes_verified": True}
        finally:
            if staged.exists():
                staged.unlink()

    def capture_scratch(self, name: str, baseline: Mapping[str, Any],
                        locale: Mapping[str, str], candidate: Mapping[str, Any]) -> dict[str, Any]:
        def query(sql: str) -> str:
            last_error = None
            for _attempt in range(3):
                try:
                    return self._remote_psql(name, sql)
                except Refused as exc:
                    last_error = exc
            raise Refused("scratch verification query failed after bounded retries") from last_error

        count_sql = "SELECT COUNT(*) FROM \"public\".\"{}\""
        counts = {}
        for table in baseline["counts"]:
            self.last_verification_step = f"count:{table}"
            counts[table] = int(query(count_sql.format(table)).strip())
        self.last_verification_step = "schema"
        raw_schema = query(SCHEMA_SQL)
        schema = []
        for line in raw_schema.splitlines():
            table, column, ordinal, udt_name, nullable = line.split("|")
            schema.append({"table": table, "column": column,
                           "ordinal": int(ordinal), "udt_name": udt_name,
                           "nullable": nullable == "YES"})
        from scripts.entities.detect_entities import INTEGRITY_QUERIES
        integrity = {}
        for key, sql in sorted(INTEGRITY_QUERIES.items()):
            self.last_verification_step = f"integrity:{key}"
            integrity[key] = int(query(sql).strip() or 0)
        groups = _proposal_groups(candidate)
        observed: dict[str, dict[int, dict[str, Any]]] = {}
        for table, group in sorted(groups.items()):
            fields = group["fields"]
            ids = sorted(group["rows"])
            table_rows: dict[int, dict[str, Any]] = {}
            columns = ", ".join(f'"{field}"' for field in fields)
            # Keep the nested SQL/Base64/PowerShell EncodedCommand below the
            # Windows command-line ceiling.  A 1,000-id batch exceeds it.
            batch_size = 200
            for start in range(0, len(ids), batch_size):
                self.last_verification_step = f"preimages:{table}:{start}"
                selected = ",".join(str(value) for value in ids[start:start + batch_size])
                sql = ("SELECT row_to_json(q) FROM (SELECT " + columns +
                       f' FROM "public"."{table}" WHERE id IN ({selected}) ORDER BY id) q')
                for line in query(sql).splitlines():
                    row = json.loads(line)
                    row_id = int(row["id"])
                    if row_id in table_rows:
                        raise Refused(f"duplicate scratch {table} preimage row {row_id}")
                    table_rows[row_id] = row
            observed[table] = table_rows
        preimages = _preimage_proof(groups, observed)
        self.last_verification_step = "complete"
        return {"counts": counts, "schema_projection": schema,
                "integrity": integrity, "proposal_preimages": preimages, **locale}

    def drop_scratch(self, name: str) -> bool:
        if not name.startswith(SCRATCH_PREFIX):
            raise Refused("refusing to drop a non-G6 scratch database")
        self._remote_admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        exists = self._remote_admin(
            f"SELECT COUNT(*) FROM pg_database WHERE datname='{name}'").strip()
        return exists == "0"


def _artifact(path: Path, payload: Mapping[str, Any]) -> None:
    preflight.write_exclusive(path, payload)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise Refused(f"artifact mode is not 0600: {path}")


def run(*, candidate_path: Path, output_dir: Path = DEFAULT_OUT,
        backup_dir: Path = DEFAULT_BACKUPS, adapter: Any | None = None,
        run_tag: str | None = None) -> dict[str, Any]:
    verdict = production_interlock.check(
        "OP-PREFLIGHT", entry_point="scripts/ops/production_g6_backup.py")
    if verdict.get("status") != "ALLOWED":
        raise Refused(f"production interlock refused: {verdict.get('code')}")
    candidate, binding = load_candidate(candidate_path)
    adapter = adapter or LiveAdapter()
    run_tag = run_tag or tag()
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", run_tag):
        raise Refused("run tag is not a safe UTC timestamp")
    scratch = SCRATCH_PREFIX + run_tag.lower().replace("t", "_").replace("z", "")
    output_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = output_dir / f"{run_tag}-g6-baseline.json"
    receipt_path = output_dir / f"{run_tag}-g6-receipt.json"
    failure_path = output_dir / f"{run_tag}-g6-failure.json"
    dump_path = backup_dir / f"{run_tag}-production-op-repair.dump"
    remote_path = REMOTE_DIR + "\\" + dump_path.name
    engine = connection = transaction = None
    scratch_created = False
    teardown_proven = False
    verification_problems: list[str] = []
    phase = "output_preflight"
    try:
        for fresh_path in (baseline_path, receipt_path, failure_path, dump_path):
            if fresh_path.exists():
                raise Refused(f"output path already exists: {fresh_path.name}")
        phase = "snapshot"
        engine, connection, transaction, snapshot = adapter.open_snapshot(candidate["target"])
        baseline = capture_baseline(connection, engine=engine, captured_at=now(),
                                    candidate_target=candidate["target"])
        baseline["proposal_preimages"] = capture_proposal_preimages(
            connection, candidate)
        baseline["snapshot"] = {
            "id_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
            "transaction": "SERIALIZABLE READ ONLY DEFERRABLE",
            "exported_before_baseline": True,
            "held_open_through_dump": True,
        }
        baseline["candidate_binding"] = {"path": str(candidate_path), **binding}
        body = {key: value for key, value in baseline.items() if key != "digest"}
        baseline["digest"] = preflight.digest(body)
        _artifact(baseline_path, baseline)
        phase = "dump"
        dump_metadata = adapter.dump(engine, snapshot, dump_path)
        transaction.commit()
        transaction = None
        dump_sha = sha256_file(dump_path)
        if stat.S_IMODE(dump_path.stat().st_mode) != 0o600:
            raise Refused("dump mode is not 0600")
        phase = "copy"
        remote = adapter.copy_off_volume(dump_path, remote_path)
        if remote.get("hash") != dump_sha or int(remote.get("bytes", -1)) != dump_path.stat().st_size:
            raise Refused("off-volume dump hash mismatch")
        phase = "create"
        # From this point on, cleanup owns the exact unique name even if CREATE
        # succeeds but its immediate metadata/schema validation fails.
        scratch_created = True
        locale = adapter.create_scratch(scratch)
        phase = "restore"
        restore_metadata = adapter.restore_off_volume(
            scratch, remote_path, expected_sha256=dump_sha,
            expected_bytes=dump_path.stat().st_size, staging_dir=backup_dir)
        phase = "verify"
        restored = adapter.capture_scratch(scratch, baseline, locale, candidate)
        problems = compare_restore(baseline, restored)
        verification_problems = list(problems)
        if problems:
            raise Refused("; ".join(problems))
        phase = "drop"
        teardown_proven = adapter.drop_scratch(scratch)
        scratch_created = not teardown_proven
        if not teardown_proven:
            raise Refused("scratch database absence was not proved")
        created = datetime.now(timezone.utc)
        phase = "receipt"
        receipt_body = {
            "schema": SCHEMA, "status": "VALID", "operation": "OP-REPAIR",
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (created + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "candidate_binding": {**binding,
                                  "upstream_bindings": candidate.get("bindings")},
            "candidate_path": str(candidate_path),
            "candidate_counts": candidate.get("counts"),
            "target": baseline["target"], "baseline_path": str(baseline_path),
            "baseline_digest": baseline["digest"],
            "proposal_preimages": baseline["proposal_preimages"],
            "dump": {"path": str(dump_path), "sha256": dump_sha,
                     "bytes": dump_path.stat().st_size, **dump_metadata,
                     "mode": "0600", "public_only": True,
                     "snapshot_exported": True, "pg_major": 18},
            "off_volume": {"host": REMOTE, "path": remote_path,
                           "sha256": remote["hash"], "bytes": remote["bytes"],
                           "machine": remote["machine"], "volume": remote["volume"],
                           "retained": True},
            "scratch": {"name": scratch, **locale, "restored_from_off_volume": True,
                        "restore": dict(restore_metadata),
                        "counts_match": True, "schema_match": True,
                        "integrity_match": True, "force_dropped": True,
                        "absence_proved": True,
                        "locale_qualification": {
                            "source": baseline.get("source_locale"),
                            "scratch": locale,
                            "policy": "logical_restore; locale equality not required; UTF8 required",
                        }},
            "comparisons": {"off_volume_bytes": True, "off_volume_sha256": True,
                            "counts": True, "schema": True, "integrity": True,
                            "proposal_preimages": True},
            "problems": [],
        }
        receipt = {**receipt_body, "digest": preflight.digest(receipt_body)}
        _artifact(receipt_path, receipt)
        return receipt
    except Exception as exc:
        cleanup_problem = None
        if scratch_created:
            try:
                teardown_proven = adapter.drop_scratch(scratch)
                scratch_created = False
                if not teardown_proven:
                    cleanup_problem = "ScratchAbsenceNotProved"
            except Exception as cleanup_exc:  # preserve both failures
                cleanup_problem = type(cleanup_exc).__name__
        failure_body = {
            "schema": SCHEMA, "status": "FAILED", "operation": "OP-REPAIR",
            "created_at": now(), "candidate_binding": binding,
            "candidate_path": str(candidate_path), "scratch_name": scratch,
            "scratch_absence_proved": teardown_proven,
            "phase": phase, "error_type": type(exc).__name__,
            "cleanup_error_type": cleanup_problem,
        }
        if verification_problems:
            failure_body["verification_problems"] = verification_problems
        verification_step = getattr(adapter, "last_verification_step", None)
        if phase == "verify" and verification_step:
            failure_body["verification_step"] = verification_step
        failure = {**failure_body, "digest": preflight.digest(failure_body)}
        _artifact(failure_path, failure)
        raise Refused("G6 proof failed; immutable failure receipt written") from exc
    finally:
        if transaction is not None:
            transaction.rollback()
        if connection is not None:
            connection.close()
        if engine is not None:
            engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUPS)
    args = parser.parse_args(argv)
    try:
        receipt = run(candidate_path=args.candidate, output_dir=args.output_dir,
                      backup_dir=args.backup_dir)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"status": "VALID", "digest": receipt["digest"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
