#!/usr/bin/env python3
"""Bounded read-only production recovery audit against a retained snapshot.

The retained archive is restored locally on the Windows development host into
one disposable database. Production is opened read-only. No repair/apply path
exists in this program.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO = Path(__file__).resolve().parents[2]
for item in (REPO, REPO / "scripts", Path(__file__).resolve().parent):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import production_g6_backup as g6  # noqa: E402
import production_preflight as preflight  # noqa: E402

EXPECTED_DUMP_SHA256 = os.environ.get("POLISCOPIC_RECOVERY_DUMP_SHA256", "")
EXPECTED_DUMP_BYTES = int(os.environ.get("POLISCOPIC_RECOVERY_DUMP_BYTES", "0"))
REMOTE_DUMP = os.environ.get("POLISCOPIC_RECOVERY_REMOTE_DUMP", "")
LOCAL_DUMP = Path(os.environ.get(
    "POLISCOPIC_RECOVERY_LOCAL_DUMP", REPO / "data/backups/recovery.dump"))
BASELINE = Path(os.environ.get(
    "POLISCOPIC_RECOVERY_BASELINE", REPO / "data/audit/recovery-baseline.json"))
RECEIPT = Path(os.environ.get(
    "POLISCOPIC_RECOVERY_RECEIPT", REPO / "data/audit/recovery-receipt.json"))
OUT_DIR = REPO / "data/audit"
SCHEMA = "production-recovery-audit/1"
DETAIL_LIMIT = 250


class Refused(RuntimeError):
    pass


def utc_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Refused(f"{path.name} is not a JSON object")
    return value


def verify_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    if LOCAL_DUMP.stat().st_size != EXPECTED_DUMP_BYTES:
        raise Refused("local retained dump byte count differs")
    if sha256_file(LOCAL_DUMP) != EXPECTED_DUMP_SHA256:
        raise Refused("local retained dump hash differs")
    baseline, receipt = load_json(BASELINE), load_json(RECEIPT)
    if receipt.get("status") != "VALID" or receipt.get("baseline_digest") != baseline.get("digest"):
        raise Refused("G6 receipt does not bind the retained baseline")
    dump = receipt.get("dump") or {}
    remote = receipt.get("off_volume") or {}
    if (dump.get("sha256") != EXPECTED_DUMP_SHA256 or
            int(dump.get("bytes", -1)) != EXPECTED_DUMP_BYTES or
            remote.get("sha256") != EXPECTED_DUMP_SHA256 or
            int(remote.get("bytes", -1)) != EXPECTED_DUMP_BYTES or
            str(remote.get("path", "")).replace("/", "\\") != REMOTE_DUMP):
        raise Refused("G6 receipt does not bind both retained dump copies")
    raw = g6.LiveAdapter._powershell(
        f"$f=Get-Item -LiteralPath '{REMOTE_DUMP}'; "
        "[pscustomobject]@{hash=(Get-FileHash -Algorithm SHA256 $f.FullName).Hash.ToLowerInvariant();"
        "bytes=$f.Length}|ConvertTo-Json -Compress").stdout
    metadata = json.loads(raw)
    if (metadata.get("hash") != EXPECTED_DUMP_SHA256 or
            int(metadata.get("bytes", -1)) != EXPECTED_DUMP_BYTES):
        raise Refused("Windows retained dump no longer matches its receipt")
    return baseline, receipt


def restore_windows_local(adapter: g6.LiveAdapter, scratch: str) -> dict[str, Any]:
    binary = r"C:\Program Files\PostgreSQL\pgsql\bin\pg_restore.exe"
    command = (f"& '{binary}' --exit-on-error --no-owner --no-privileges "
               f"-U poliscopic -d '{scratch}' '{REMOTE_DUMP}'; "
               "$rc=$LASTEXITCODE; if ($rc -ne 0) { exit $rc }")
    result = adapter._powershell(command)
    return {"client": "Windows-local PostgreSQL", "exit_code": result.returncode,
            "transport": "none; retained file read locally"}


PK_SQL = """
SELECT a.attname
FROM pg_index i
JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
WHERE i.indrelid=to_regclass(:qualified) AND i.indisprimary
ORDER BY array_position(i.indkey, a.attnum)
"""


def tables(connection: Any) -> list[str]:
    return [row[0] for row in connection.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"))]


def primary_key(connection: Any, table_name: str) -> list[str]:
    qualified = f"public.{table_name}"
    return [str(row[0]) for row in connection.execute(text(PK_SQL), {"qualified": qualified})]


def count(connection: Any, table_name: str) -> int:
    return int(connection.execute(text(f'SELECT COUNT(*) FROM "public"."{table_name}"')).scalar_one())


def identity_hashes(connection: Any, table_name: str, pk: list[str]) -> dict[str, str] | Counter[str]:
    # row_to_json follows physical column order, which the verified restore and
    # source share. md5 is an identity-change detector, not a security digest.
    if pk:
        key_expr = "json_build_array(" + ",".join(f't."{col}"' for col in pk) + ")::text"
        sql = (f'SELECT {key_expr}, md5(row_to_json(t)::text) '
               f'FROM "public"."{table_name}" t')
        return {str(row[0]): str(row[1]) for row in connection.execution_options(
            stream_results=True).execute(text(sql))}
    sql = f'SELECT md5(row_to_json(t)::text), COUNT(*) FROM "public"."{table_name}" t GROUP BY 1'
    return Counter({str(row[0]): int(row[1]) for row in connection.execute(text(sql))})


def fetch_rows(connection: Any, table_name: str, pk: list[str], keys: list[str]) -> list[dict[str, Any]]:
    if not pk or not keys:
        return []
    key_expr = "json_build_array(" + ",".join(f't."{col}"' for col in pk) + ")::text"
    statement = text(f'SELECT row_to_json(t) AS row FROM "public"."{table_name}" t '
                     f'WHERE {key_expr} IN :keys ORDER BY {key_expr}').bindparams(
                         __import__("sqlalchemy").bindparam("keys", expanding=True))
    rows: list[dict[str, Any]] = []
    for start in range(0, min(len(keys), DETAIL_LIMIT), 100):
        selected = keys[start:start + 100]
        rows.extend(dict(row[0]) for row in connection.execute(statement, {"keys": selected}))
    return rows


def compare_table(current: Any, snapshot: Any, table_name: str) -> dict[str, Any]:
    current_count, snapshot_count = count(current, table_name), count(snapshot, table_name)
    current_pk, snapshot_pk = primary_key(current, table_name), primary_key(snapshot, table_name)
    if current_pk != snapshot_pk:
        raise Refused(f"primary key differs for {table_name}")
    result: dict[str, Any] = {"snapshot_count": snapshot_count,
                              "current_count": current_count,
                              "count_delta": current_count - snapshot_count,
                              "primary_key": current_pk}
    before = identity_hashes(snapshot, table_name, snapshot_pk)
    after = identity_hashes(current, table_name, current_pk)
    if current_pk:
        before_map, after_map = dict(before), dict(after)
        snapshot_only = sorted(set(before_map) - set(after_map))
        current_only = sorted(set(after_map) - set(before_map))
        shared = set(before_map) & set(after_map)
        changed = sorted(key for key in shared if before_map[key] != after_map[key])
        result.update({"snapshot_only_count": len(snapshot_only),
                       "current_only_count": len(current_only),
                       "changed_count": len(changed),
                       "unchanged_count": len(shared) - len(changed),
                       "snapshot_only_keys": snapshot_only[:DETAIL_LIMIT],
                       "current_only_keys": current_only[:DETAIL_LIMIT],
                       "changed_keys": changed[:DETAIL_LIMIT],
                       "details_truncated": any(len(values) > DETAIL_LIMIT for values in
                                                (snapshot_only, current_only, changed)),
                       "snapshot_only_rows": fetch_rows(snapshot, table_name, current_pk,
                                                        snapshot_only),
                       "changed_before_rows": fetch_rows(snapshot, table_name, current_pk, changed),
                       "changed_current_rows": fetch_rows(current, table_name, current_pk, changed)})
    else:
        before_counter, after_counter = Counter(before), Counter(after)
        result.update({"snapshot_only_count": sum((before_counter - after_counter).values()),
                       "current_only_count": sum((after_counter - before_counter).values()),
                       "changed_count": None, "unchanged_count": None,
                       "no_primary_key": True})
    result["classification"] = (
        "SUSPECTED_LOSS" if result["snapshot_only_count"] else
        "CHANGED_NO_LOSS" if (result.get("changed_count") or result["current_only_count"]) else
        "UNCHANGED")
    return result


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Production recovery audit", "", f"Verdict: **{report['verdict']}**", "",
             f"Snapshot: `{report['snapshot']['captured_at']}`", "",
             "| Table | Snapshot | Current | Snapshot-only | Current-only | Changed | Result |",
             "|---|---:|---:|---:|---:|---:|---|"]
    for name, item in report["tables"].items():
        lines.append(f"| `{name}` | {item['snapshot_count']} | {item['current_count']} | "
                     f"{item['snapshot_only_count']} | {item['current_only_count']} | "
                     f"{item.get('changed_count') if item.get('changed_count') is not None else 'n/a'} | "
                     f"{item['classification']} |")
    lines.extend(["", "## Boundaries", "", "- Production was read in a read-only transaction.",
                  "- `poliscopic_dev` was not modified.",
                  "- No repair, sync, deploy, scrape, restart, or scheduler action occurred.",
                  f"- Scratch `{report['scratch']['name']}` was force-dropped and absence was proved.",
                  "", "## Interpretation", ""])
    if report["suspected_loss_tables"]:
        lines.append("Snapshot-only identities require human review before any repair: " +
                     ", ".join(f"`{x}`" for x in report["suspected_loss_tables"]) + ".")
    else:
        lines.append("No row identity present in the snapshot was absent from current production.")
    return "\n".join(lines) + "\n"


def write_exclusive(path: Path, content: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def run(run_tag: str) -> dict[str, Any]:
    baseline, receipt = verify_inputs()
    scratch_name = "poliscopic_g6_scratch_" + run_tag.lower().replace("t", "_").replace("z", "")
    adapter = g6.LiveAdapter()
    scratch_created = False
    scratch_engine = prod_engine = None
    try:
        locale = adapter.create_scratch(scratch_name)
        scratch_created = True
        restore = restore_windows_local(adapter, scratch_name)
        from dotenv import dotenv_values
        dev_url = make_url(str(dotenv_values(REPO / ".env").get("DATABASE_URL")))
        scratch_url = dev_url.set(database=scratch_name)
        prod_url = preflight.resolve_production_url(REPO / ".env")
        scratch_engine = create_engine(scratch_url, future=True, pool_pre_ping=True)
        prod_engine = create_engine(prod_url, future=True, pool_pre_ping=True)
        with scratch_engine.connect() as snapshot, prod_engine.connect().execution_options(
                isolation_level="SERIALIZABLE") as current:
            transaction = current.begin()
            current.execute(text("SET TRANSACTION READ ONLY DEFERRABLE"))
            current.execute(text("SET LOCAL statement_timeout='20min'"))
            snapshot_tables, current_tables = tables(snapshot), tables(current)
            if snapshot_tables != current_tables:
                raise Refused("public table population differs between snapshot and current production")
            comparisons = {name: compare_table(current, snapshot, name) for name in snapshot_tables}
            from scripts.entities.detect_entities import integrity_snapshot
            integrity = {"snapshot": dict(sorted(integrity_snapshot(snapshot).items())),
                         "current": dict(sorted(integrity_snapshot(current).items()))}
            transaction.rollback()
        suspected = [name for name, item in comparisons.items()
                     if item["snapshot_only_count"]]
        verdict = "SUSPECTED_LOSS_REQUIRES_REVIEW" if suspected else "NO_OBSERVED_ROW_LOSS"
        report: dict[str, Any] = {
            "schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "snapshot": {"captured_at": baseline.get("captured_at"),
                         "baseline_digest": baseline.get("digest"),
                         "receipt_digest": receipt.get("digest"),
                         "dump_sha256": EXPECTED_DUMP_SHA256},
            "production_access": "SERIALIZABLE READ ONLY DEFERRABLE",
            "tables": comparisons, "integrity": integrity,
            "suspected_loss_tables": suspected,
            "scratch": {"name": scratch_name, "locale": locale, "restore": restore,
                        "force_dropped": False, "absence_proved": False},
            "limitations": ["Row hashes identify change but do not adjudicate whether a change was correct.",
                            f"Detailed rows are capped at {DETAIL_LIMIT} identities per category.",
                            "The comparison starts at the retained snapshot time, not before it."],
            "repair_applied": False,
        }
        return report
    finally:
        if scratch_engine is not None:
            scratch_engine.dispose()
        if prod_engine is not None:
            prod_engine.dispose()
        if scratch_created:
            if not adapter.drop_scratch(scratch_name):
                raise Refused("scratch database absence was not proved")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-tag", default=utc_tag())
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", args.run_tag):
        raise SystemExit("unsafe run tag")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"{args.run_tag}-production-recovery-audit.json"
    md_path = OUT_DIR / f"{args.run_tag}-production-recovery-audit.md"
    try:
        report = run(args.run_tag)
        report["scratch"]["force_dropped"] = True
        report["scratch"]["absence_proved"] = True
        body = {**report, "digest": preflight.digest(report)}
        write_exclusive(json_path, json.dumps(body, indent=2, sort_keys=True, default=str) + "\n")
        write_exclusive(md_path, render_markdown(body))
        print(json.dumps({"verdict": body["verdict"], "json": str(json_path),
                          "markdown": str(md_path), "digest": body["digest"]}, sort_keys=True))
        return 0 if body["verdict"] == "NO_OBSERVED_ROW_LOSS" else 2
    except Exception as exc:
        failure = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
                   "status": "REFUSED", "error_type": type(exc).__name__, "error": str(exc)}
        failure_path = OUT_DIR / f"{args.run_tag}-production-recovery-audit-failure.json"
        write_exclusive(failure_path, json.dumps(failure, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"status": "REFUSED", "failure": str(failure_path),
                          "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
