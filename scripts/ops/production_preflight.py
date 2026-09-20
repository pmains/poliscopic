#!/usr/bin/env python3
"""G5 read-only production identity and integrity preflight.

The production interlock is evaluated before environment resolution or engine
creation.  A successful run proves that all live evidence was captured on one
connection in one PostgreSQL REPEATABLE READ, READ ONLY transaction.  Existing
integrity debt is recorded factually; it is not itself a refusal condition.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _candidate in (REPO, REPO / "scripts", Path(__file__).resolve().parent):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import production_interlock  # noqa: E402

SCHEMA = "production-g5-preflight/1"
DEFAULT_OUT_DIR = REPO / "data" / "audit"

class Refused(RuntimeError):
    """Fail-closed preflight refusal."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _scalar(connection: Any, statement: str) -> Any:
    from sqlalchemy import text

    return connection.execute(text(statement)).scalar_one()


def capture_snapshot(connection: Any, *, configured_host: str,
                     configured_port: int | None, dialect: str,
                     driver: str, captured_at: str) -> dict[str, Any]:
    """Capture all evidence through the caller's single live connection."""
    from sqlalchemy import inspect
    from scripts.body_code_merge_runtime import PRODUCTION_TARGET
    # The legacy entity module imports db.config, whose startup diagnostic writes
    # to stdout.  G5's CLI stdout is a machine-readable receipt, so contain that
    # unrelated import-time banner.  The function itself receives our already-open
    # read-only connection and performs no target resolution.
    with contextlib.redirect_stdout(io.StringIO()):
        from scripts.entities.detect_entities import integrity_snapshot
    from scripts.ops.propagation_contract import (
        BODY_CODE_COLUMNS, PUBLIC_BODY_DEPENDENTS, PUBLIC_BODY_ID_COLUMN,
    )

    isolation = str(_scalar(connection, "SHOW transaction_isolation")).lower()
    read_only = str(_scalar(connection, "SHOW transaction_read_only")).lower()
    if isolation != "repeatable read" or read_only not in ("on", "true"):
        raise Refused(
            "database did not prove one REPEATABLE READ, READ ONLY transaction")

    identity = {
        "database": str(_scalar(connection, "SELECT current_database()")),
        "configured_host": configured_host,
        "configured_port": configured_port,
        "server_address": str(_scalar(connection, "SELECT inet_server_addr()")),
        "server_port": int(_scalar(connection, "SELECT inet_server_port()")),
        "cluster_system_identifier": str(_scalar(
            connection, "SELECT system_identifier FROM pg_control_system()")),
        "server_version": str(_scalar(
            connection, "SELECT current_setting('server_version')")),
        "dialect": dialect,
        "driver": driver,
    }
    expected = {"database": PRODUCTION_TARGET["database"],
                "host": PRODUCTION_TARGET["host"]}
    if (identity["database"] != expected["database"] or
            identity["configured_host"] != expected["host"]):
        raise Refused("live target identity does not match the pinned production target")

    inspector = inspect(connection)
    tables = sorted(inspector.get_table_names(schema="public"))
    if not tables:
        raise Refused("public schema introspection returned no tables")
    schema_projection: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        schema_projection[table] = [
            {"name": column["name"], "type": str(column["type"]),
             "nullable": bool(column.get("nullable", True))}
            for column in inspector.get_columns(table, schema="public")
        ]

    registry: dict[str, int] = {}
    for table, columns in sorted(PUBLIC_BODY_DEPENDENTS.items()):
        if table not in schema_projection:
            raise Refused(f"required public-body dependent table absent: {table}")
        actual = {column["name"] for column in schema_projection[table]}
        for column in columns:
            if column not in actual:
                raise Refused(f"required public-body reference absent: {table}.{column}")
            if column in BODY_CODE_COLUMNS:
                registry[f"{table}.{column}.sentinel"] = int(_scalar(connection, f'''
                    SELECT COUNT(*) FROM "{table}"
                    WHERE "{column}" IS NULL OR BTRIM("{column}") = ''
                       OR "{column}" = '__skip__'
                ''') or 0)
                registry[f"{table}.{column}.dangling"] = int(_scalar(connection, f'''
                    SELECT COUNT(*) FROM "{table}" d
                    WHERE d."{column}" IS NOT NULL
                      AND BTRIM(d."{column}") <> ''
                      AND d."{column}" <> '__skip__'
                      AND NOT EXISTS (SELECT 1 FROM public_bodies pb
                                      WHERE pb.body_code = d."{column}")
                ''') or 0)
            elif column == PUBLIC_BODY_ID_COLUMN:
                registry[f"{table}.{column}.null"] = int(_scalar(connection, f'''
                    SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NULL
                ''') or 0)
                registry[f"{table}.{column}.dangling"] = int(_scalar(connection, f'''
                    SELECT COUNT(*) FROM "{table}" d
                    WHERE d."{column}" IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM public_bodies pb
                                      WHERE pb.id = d."{column}")
                ''') or 0)

    stage0 = dict(sorted(integrity_snapshot(connection).items()))
    debt = {
        **{f"stage0.{name}": count for name, count in stage0.items() if count != 0},
        **{f"registry.{name}": count for name, count in registry.items()
           if count != 0},
    }
    return {
        "schema": SCHEMA,
        "captured_at": captured_at,
        "operation": "OP-PREFLIGHT",
        "status": "VALID",
        "target": identity,
        "pinned_target": expected,
        "transaction": {
            "connection_count": 1,
            "transaction_count": 1,
            "isolation": isolation,
            "read_only": True,
        },
        "schema_snapshot": {
            "table_count": len(tables),
            "sha256": digest(schema_projection),
            "tables": schema_projection,
        },
        "integrity": {
            "stage0_metrics": stage0,
            "registry_metrics": registry,
            "debt": debt,
            "debt_present": bool(debt),
            "policy": "recorded-not-invalidating",
        },
    }


def write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    """Create canonical JSON exactly once at mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def resolve_production_url(dotenv_path: Path) -> str:
    """Resolve one unambiguous production URL without ever returning it in errors."""
    from dotenv import dotenv_values, load_dotenv
    from db.tier import (PRODUCTION, TierError, detect_conflicting_definitions,
                         require_role_url, resolve_role_url)
    from scripts.body_code_merge_runtime import PRODUCTION_TARGET

    conflicts = detect_conflicting_definitions(dotenv_path, keys=("PROD_DATABASE_URL",))
    if conflicts:
        raise Refused("conflicting PROD_DATABASE_URL definitions in .env")
    file_url = dotenv_values(dotenv_path).get("PROD_DATABASE_URL")
    inherited_url = os.environ.get("PROD_DATABASE_URL")
    if inherited_url and file_url and inherited_url != file_url:
        raise Refused("PROD_DATABASE_URL conflicts between environment and .env")
    load_dotenv(dotenv_path, override=False)
    raw = os.environ.get("PROD_DATABASE_URL")
    if not raw:
        raise Refused("PROD_DATABASE_URL is not set")
    try:
        target = resolve_role_url(PRODUCTION, raw, label="G5 preflight")
        if (target.host != PRODUCTION_TARGET["host"] or
                target.database != PRODUCTION_TARGET["database"]):
            raise Refused("configured target does not match pinned production target")
        return require_role_url(PRODUCTION, raw, label="G5 preflight")
    except Refused:
        raise
    except TierError as exc:
        raise Refused(f"tier resolver rejected production URL: {exc}") from exc


def run(*, output: Path, captured_at: str | None = None) -> dict[str, Any]:
    """Run the guarded preflight; no credential or engine work precedes interlock."""
    verdict = production_interlock.check(
        "OP-PREFLIGHT", entry_point="scripts/ops/production_preflight.py")
    if verdict.get("status") != "ALLOWED":
        raise Refused(f"production interlock refused: {verdict.get('code')}")

    # Deliberately local: both actions occur only after the interlock decision.
    from sqlalchemy import create_engine, text

    try:
        validated_url = resolve_production_url(REPO / ".env")
        engine = create_engine(validated_url, future=True, pool_pre_ping=True)
    except Refused:
        raise
    except Exception as exc:
        raise Refused("production engine creation failed") from exc

    timestamp = captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with engine.connect().execution_options(
                isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                body = capture_snapshot(
                    connection,
                    configured_host=str(engine.url.host or ""),
                    configured_port=engine.url.port,
                    dialect=str(engine.dialect.name or ""),
                    driver=str(engine.dialect.driver or ""),
                    captured_at=timestamp,
                )
    except Refused:
        raise
    except Exception as exc:
        raise Refused("production query or introspection failed") from exc
    finally:
        engine.dispose()

    artifact = {**body, "digest": digest(body)}
    try:
        write_exclusive(output, artifact)
    except Exception as exc:
        raise Refused(f"evidence artifact creation failed: {exc}") from exc
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    output = args.output or DEFAULT_OUT_DIR / (
        now.strftime("%Y%m%dT%H%M%SZ") + "-g5-preflight.json")
    try:
        result = run(output=output, captured_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"status": "VALID", "path": str(output),
                      "digest": result["digest"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
