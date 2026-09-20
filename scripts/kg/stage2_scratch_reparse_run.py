#!/usr/bin/env python3
"""``stage2_scratch_reparse_run.py`` — the authorized scratch verification run.

Lifecycle, with teardown on every terminal path:

1. re-validate the plan and its digest;
2. capture the protected preimage from ``poliscopic_dev`` (read-only);
3. create a uniquely named scratch database on the development host;
4. verify the dump's sha256 against the protected receipt;
5. restore the dump into the scratch database;
6. validate the restored counts against the receipt signature;
7. run the scratch re-parse and compare all six bound cases;
8. write immutable preimage / result / receipt artifacts;
9. **always** drop the exact named scratch database and verify it is gone;
10. capture the protected postimage and prove no delta.

``poliscopic_dev`` is only ever read.  Production is never contacted.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import create_engine, text  # noqa: E402

from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target,
)
from scripts.db.core import get_engine  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_scratch_reparse as scratch  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402

PG_BIN = Path("/opt/homebrew/opt/postgresql@18/bin")

#: The established administrative path: SSH to the development host and use the
#: host-local postgres superuser through local trust.  Used ONLY to create and
#: drop the uniquely named scratch database.  The application role is used for
#: the restore and every scratch application step.
SSH_HOST = os.environ.get("POLISCOPIC_DEV_SSH_HOST", "development-host")
HOST_PSQL = r'"C:\Program Files\PostgreSQL\pgsql\bin\psql.exe"'
SCRATCH_OWNER = "poliscopic"
PLAN_DIGEST = "a88faea1a258a214810344465155c6bcc3e4a22aef67cc695f8f18e7a6e611d6"
PLAN_PATH = REPO / "data" / "kg-plans" / \
    "kg-stage2-scratch-reparse-plan-scratch-reparse-20260912T201100Z.json"
ARTIFACT_DIR = REPO / "data" / "kg-plans"

COUNTS_SQL = {
    "entities": "SELECT COUNT(*) FROM entities",
    "entity_mentions": "SELECT COUNT(*) FROM entity_mentions",
    "entity_relationships": "SELECT COUNT(*) FROM entity_relationships",
    "event_participants": "SELECT COUNT(*) FROM event_participants",
    "meeting_event_extractions": "SELECT COUNT(*) FROM meeting_event_extractions",
    "meeting_events": "SELECT COUNT(*) FROM meeting_events",
}

SCHEMA_SQL = """
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""
TABLE_SQL = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name
"""


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def _psql(env: dict[str, str], database: str, sql: str) -> str:
    return subprocess.run(
        [str(PG_BIN / "psql"), "-X", "-q", "-t", "-A", "-d", database, "-c", sql],
        capture_output=True, text=True, env=env, check=True).stdout


def _admin(sql: str) -> str:
    """Run one statement as the host-local postgres superuser over SSH.

    This path is used for exactly two statements - CREATE and DROP of the
    uniquely named scratch database - and for nothing else.  The SQL contains no
    double quotes: the scratch name is lower-case with underscores only.
    """
    command = f'{HOST_PSQL} -X -q -t -A -U postgres -d postgres -c "{sql}"'
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH_HOST, command],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"admin statement failed ({result.returncode}): {result.stderr.strip()[:400]}")
    return result.stdout.strip()


def _create_scratch(name: str) -> None:
    """Create the scratch database UTF8 on template0, owned by the app role.

    template0 is explicitly required: the cluster's template1 and postgres are
    WIN1252, and inheriting that produced the prior encoding defect.  The
    collation matches the development database so restored text compares equal.
    """
    out = _admin(
        f"CREATE DATABASE {name} WITH TEMPLATE template0 ENCODING 'UTF8' "
        f"LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8' OWNER {SCRATCH_OWNER}")
    # A CREATE that reports success but leaves no database is the failure that
    # cost two runs: assert existence before anything depends on it.
    for _attempt in range(5):
        present = _admin(
            f"SELECT COUNT(*) FROM pg_database WHERE datname = '{name}'").strip()
        if present == "1":
            return
        time.sleep(1.0)
    raise RuntimeError(
        f"scratch database {name} was not created (admin output {out!r}, "
        f"last existence probe {present!r})")


def _drop_scratch(name: str) -> None:
    _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _scratch_present(prefix: str = "poliscopic_scratch") -> list[str]:
    out = _admin(
        "SELECT COALESCE(string_agg(datname, ','), '') FROM pg_database "
        f"WHERE datname LIKE '{prefix}%'")
    return [r for r in out.split(",") if r.strip()]


def _counts(engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {name: int(connection.execute(text(sql)).scalar())
                for name, sql in COUNTS_SQL.items()}


def _schema(engine) -> dict[str, Any]:
    with engine.connect() as connection:
        columns = [tuple(str(x) for x in row)
                   for row in connection.execute(text(SCHEMA_SQL))]
        tables = [str(r[0]) for r in connection.execute(text(TABLE_SQL))]
    return {"tables": tables, "columns": columns,
            "sha256": hashlib.sha256(
                json.dumps({"tables": tables, "columns": columns},
                           sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def main() -> int:
    env = dict(os.environ)
    env.setdefault("PGHOST", os.environ.get("POLISCOPIC_DEV_DB_HOST", "192.0.2.10"))
    env.setdefault("PGPORT", "5432")
    env.setdefault("PGUSER", "poliscopic")
    env.setdefault("PGDATABASE", "poliscopic_dev")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # PostgreSQL folds an unquoted identifier to lower case, so a name carrying
    # the stamp's upper-case T and Z is created lower-cased while `pg_restore -d`
    # passes the same string verbatim and cannot find it.  The scratch name is
    # therefore lower-cased once, here, and used unchanged everywhere after.
    scratch_db = f"poliscopic_scratch_s2_reparse_{stamp.lower()}"

    dev_engine = get_engine()
    target = assert_read_only_target(dev_engine)
    log(f"protected development target: {target['database']} ({target['tier']})")

    plan = artifacts.load_verified(PLAN_PATH)
    digest = artifacts.recorded_digest(plan)
    if digest != PLAN_DIGEST:
        raise SystemExit(f"plan digest {digest} is not the authorized {PLAN_DIGEST}")
    log(f"plan revalidated: {PLAN_PATH.name} digest {digest}")

    backup = plan["protected_backup"]
    receipt = json.loads((REPO / backup["receipt_path"]).read_text())
    receipt_check = receipts.validate_receipt(
        receipt, expected_counts=None, now=datetime.now(timezone.utc))
    log(f"receipt validation: {receipt_check['problems'] or 'clean'}")
    if not receipt_check["valid"]:
        raise SystemExit(f"protected receipt is not valid: {receipt_check['problems']}")
    dump = Path(str(receipt["dump_path"]))
    dump_sha = hashlib.sha256(dump.read_bytes()).hexdigest()
    if dump_sha != receipt["dump_sha256"]:
        raise SystemExit(f"dump sha {dump_sha} != receipt {receipt['dump_sha256']}")
    log(f"dump verified: {dump.name} sha {dump_sha}")

    preimage = {"captured_at": stamp, "database": target["database"],
                "counts": _counts(dev_engine), "schema_sha256": _schema(dev_engine)["sha256"]}
    pre_path = ARTIFACT_DIR / f"kg-stage2-scratch-reparse-preimage-{stamp}.json"
    pre_digest = artifacts.write_immutable(pre_path, preimage)
    log(f"preimage written: {pre_path.name} digest {pre_digest}")

    url = (f"postgresql+psycopg2://{env.get('PGUSER')}:{env.get('PGPASSWORD','')}"
           f"@{env['PGHOST']}:{env['PGPORT']}/{scratch_db}")
    result: dict[str, Any] = {"scratch_database": scratch_db, "created": False,
                              "restored": False, "dropped": False}
    evidence: dict[str, Any] = {}
    created = False
    try:
        log(f"creating scratch database {scratch_db} "
            f"(template0, UTF8, en_US.UTF-8, owner {SCRATCH_OWNER})")
        _create_scratch(scratch_db)
        result["created"] = True
        # Mark ownership before anything else can fail, so the teardown drops the
        # exact database this run made even if the creation check confuses.
        created = True

        log("restoring dump into scratch")
        restore = subprocess.run(
            [str(PG_BIN / "pg_restore"), "--no-owner", "--no-privileges",
             "-d", scratch_db, str(dump)],
            capture_output=True, text=True, env=env)
        evidence["restore_exit_code"] = restore.returncode
        evidence["restore_stderr_tail"] = restore.stderr[-4000:]
        evidence["restore_stdout_tail"] = restore.stdout[-1000:]
        if restore.returncode != 0:
            log("pg_restore stderr follows:")
            for line in restore.stderr.strip().split("\n")[:25]:
                log(f"  pg_restore: {line}")
            log("pg_restore stdout follows:")
            for line in restore.stdout.strip().split("\n")[:10]:
                log(f"  pg_restore-out: {line}")
            raise SystemExit(f"pg_restore failed ({restore.returncode})")
        result["restored"] = True
        log(f"restore complete (exit {restore.returncode})")

        scratch_engine = create_engine(url)
        if str(scratch_engine.url.database) == target["database"]:
            raise SystemExit("scratch resolved to the development database; aborting")

        restored_counts = _counts(scratch_engine)
        evidence["restored_counts"] = restored_counts
        evidence["receipt_counts"] = dict(receipt["counts"])
        evidence["counts_match"] = restored_counts == dict(receipt["counts"])
        evidence["counts_fingerprint"] = receipts.counts_fingerprint(restored_counts)
        evidence["receipt_counts_sha256"] = receipt["signatures"]["counts_sha256"]
        evidence["counts_signature_match"] = (
            evidence["counts_fingerprint"] == receipt["signatures"]["counts_sha256"])
        if not evidence["counts_match"]:
            raise SystemExit(f"restored counts differ: {restored_counts}")
        log(f"restored counts match the receipt: {restored_counts}")

        scratch_schema = _schema(scratch_engine)
        evidence["scratch_schema_sha256"] = scratch_schema["sha256"]
        evidence["scratch_tables"] = len(scratch_schema["tables"])
        dev_schema = _schema(dev_engine)
        evidence["dev_schema_sha256"] = dev_schema["sha256"]
        evidence["schema_matches_dev"] = scratch_schema["sha256"] == dev_schema["sha256"]
        log(f"restored schema tables={len(scratch_schema['tables'])} "
            f"matches dev: {evidence['schema_matches_dev']}")

        authorization = scratch.Authorization(
            plan_digest=digest, phrase=scratch.AUTHORIZATION_PHRASE,
            authorized_by="Peter Mains", authorized_at=stamp)
        run = scratch.execute(plan, authorization, engine=scratch_engine)
        evidence["run"] = run
        log(f"re-parse complete: {run['counts']}")
    finally:
        try:
            scratch_engine.dispose()
        except Exception:
            pass
        if created:
            log(f"dropping scratch database {scratch_db}")
            try:
                _drop_scratch(scratch_db)
                result["dropped"] = True
            except Exception as exc:  # noqa: BLE001 - teardown must not mask the cause
                log(f"DROP failed: {exc}")
        try:
            result["scratch_remaining"] = _scratch_present()
        except Exception as exc:  # noqa: BLE001
            result["scratch_remaining"] = [f"UNKNOWN: {exc}"]
        log(f"scratch databases remaining: {result['scratch_remaining']}")

    postimage = {"captured_at": datetime.now(timezone.utc).isoformat(),
                 "database": target["database"], "counts": _counts(dev_engine),
                 "schema_sha256": _schema(dev_engine)["sha256"]}
    delta_matrix = {k: postimage["counts"][k] - preimage["counts"][k]
                    for k in preimage["counts"]}
    post_path = ARTIFACT_DIR / f"kg-stage2-scratch-reparse-postimage-{stamp}.json"
    post_digest = artifacts.write_immutable(post_path, postimage)

    outcome = {
        "kind": "kg-stage2-scratch-reparse-result",
        "version": "kg-stage2-scratch-reparse-result/1.0",
        "created_at": stamp,
        "plan_path": PLAN_PATH.name,
        "plan_digest": digest,
        "scratch": result,
        "evidence": evidence,
        "preimage": {"path": pre_path.name, "digest": pre_digest},
        "postimage": {"path": post_path.name, "digest": post_digest},
        "protected_delta": {"counts": delta_matrix,
                            "schema_changed": preimage["schema_sha256"] !=
                                              postimage["schema_sha256"]},
        "no_scratch_remaining": not result["scratch_remaining"],
        "production_contacted": False,
    }
    res_path = ARTIFACT_DIR / f"kg-stage2-scratch-reparse-result-{stamp}.json"
    res_digest = artifacts.write_immutable(res_path, outcome)
    log(f"result written: {res_path.name} digest {res_digest}")
    log(f"protected delta: {delta_matrix}")
    log(f"scratch remaining: {result['scratch_remaining']}")

    ok = (evidence.get("run", {}).get("counts", {}).get("failed") == 0
          and evidence.get("counts_signature_match")
          and evidence.get("schema_matches_dev")
          and result["dropped"] and not result["scratch_remaining"]
          and not any(delta_matrix.values()))
    log(f"GATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - operator path
    raise SystemExit(main())
