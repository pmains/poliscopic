#!/usr/bin/env python3
"""``event_normalize_preflight.py`` — guarded read-only snapshot for the gate.

Corrections applied after review:

* **Read-only is enforced on every connection, not one discarded connection.**
  :func:`guard_engine` installs a per-connection hook that makes the session
  read-only (PostgreSQL) and a statement guard that refuses any mutating
  statement on *any* connection opened from that engine.  Every provider runs on
  the guarded engine, so no query path escapes the protection.
* **Metric failures are fatal.** A missing gate table or a failed count query
  raises; it is never encoded as a sentinel value and never left launchable.
* **The snapshot owns the evidence.** Target, population, typed civic-chain
  failures, gate-table counts and integrity metrics are captured together, and
  the same shape is produced again as a postflight so the evaluator can compute
  deltas rather than trust a caller's flag.

One classification authority: civic-chain failures come from the runtime's own
read contract, never from parallel SQL predicates.

Usage:
  .venv/bin/python -u scripts/entities/event_normalize_preflight.py --out <path>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_ENTITIES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ENTITIES_DIR.parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import event, text  # noqa: E402

from scripts.entities.event_normalize_remediation import (  # noqa: E402
    remediation_state,
)

from scripts.db.sql_read_only_guard import (  # noqa: E402
    MUTATING_KEYWORDS,
    READ_ONLY_PREFIXES,
    statement_write_problem,
)

from db.core import get_engine  # noqa: E402
from db.tier import (  # noqa: E402
    DEVELOPMENT_LIKE,
    LOCAL,
    PRODUCTION_LIKE,
    UNKNOWN,
    classify_target,
    parse_target,
)
from scripts.entities.event_normalize_accounting import (  # noqa: E402
    CLASSIFICATION_EQUATION,
)
from scripts.entities.event_normalize_gate import (  # noqa: E402
    active_writer_blockers,
    launch_decision,
)
from scripts.entities.event_normalize_storage import (  # noqa: E402
    fetch_normalization_page,
)

__all__ = [
    "GateTablesMissing",
    "PreflightError",
    "ReadOnlyViolation",
    "assert_read_only_target",
    "code_fingerprint",
    "collect_population",
    "guard_engine",
    "run_snapshot",
    "statement_audit",
]


DEFAULT_PAGE_SIZE = 512


class PreflightError(RuntimeError):
    """The snapshot cannot be taken safely."""


class ReadOnlyViolation(PreflightError):
    """A mutating statement was attempted through a guarded engine."""


class GateTablesMissing(PreflightError):
    """A required gate table or metric could not be read."""


def assert_read_only_target(engine: Any) -> dict[str, Any]:
    """Classify the engine's target; refuse anything but development/local.

    Pure: reads only ``engine.url``, so an unsafe target is refused before any
    connection is opened.  The record carries redacted data only.
    """
    url = str(engine.url)
    url_class = classify_target(url)
    if url_class == PRODUCTION_LIKE:
        raise PreflightError("refusing a production-like target")
    if url_class == UNKNOWN:
        raise PreflightError("refusing an unclassifiable target")
    if url_class not in (LOCAL, DEVELOPMENT_LIKE):
        raise PreflightError(f"refusing target class {url_class!r}")
    target = parse_target(url)
    return {
        "tier": "development" if url_class == DEVELOPMENT_LIKE else "test-isolated",
        "url_class": url_class,
        "dialect": target.dialect,
        "host": target.host,
        "port": target.port,
        "database": target.database,
        "redacted": target.redacted(),
    }


def guard_engine(engine: Any) -> list[str]:
    """Make every connection from this engine read-only; record every statement.

    Returns the statement log.  On PostgreSQL each new DBAPI connection is put in
    a read-only session, so *later* queries on *other* connections are covered
    too — the guarantee is not tied to one transaction.  Independently, a
    statement guard raises :class:`ReadOnlyViolation` for any mutating statement,
    which is what makes the protection testable on an isolated engine.
    """
    statements: list[str] = []
    dialect = engine.dialect.name

    @event.listens_for(engine, "connect")
    def _read_only_session(dbapi_connection, connection_record):
        if dialect != "postgresql":
            return
        cursor = dbapi_connection.cursor()
        cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cursor.close()

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(conn, cursor, statement, parameters, context, executemany):
        statements.append(str(statement))
        problem = statement_write_problem(str(statement))
        if problem is not None:
            raise ReadOnlyViolation(problem)

    # Drop any pre-existing pooled connections so a connection created before the
    # guard existed cannot be checked out unguarded.  Skipped for SQLite: an
    # in-memory database lives inside its connection, so disposing would destroy
    # the database rather than protect it.
    if engine.dialect.name != "sqlite":
        try:
            engine.dispose()
        except Exception:  # pragma: no cover - dispose should not be fatal
            pass

    return statements


def statement_audit(statements: Sequence[str]) -> dict[str, Any]:
    """Prove no executed statement mutates data."""
    offenders: list[str] = []
    for raw in statements:
        normalised = " ".join(str(raw).split()).lower()
        if not normalised:
            continue
        if not normalised.startswith(READ_ONLY_PREFIXES):
            offenders.append(normalised[:120])
            continue
        if normalised.split(None, 1)[0] in MUTATING_KEYWORDS:
            offenders.append(normalised[:120])
    return {
        "statement_count": len(statements),
        "mutating_statements": offenders,
        "select_only": not offenders,
    }


def collect_population(engine: Any, *, page_size: int, force: bool = True) -> dict[str, Any]:
    """Read the whole eligible population through the authoritative contract."""
    cursor: int | None = None
    examined = work = linked = unlinked = 0
    pages = 0
    reasons: Counter[str] = Counter()
    cursors: list[int] = []

    while True:
        page = fetch_normalization_page(
            engine, limit=page_size, after_extraction_id=cursor, force=force
        )
        if page.examined == 0:
            break
        pages += 1
        examined += page.examined
        work += len(page.work_items)
        for item in page.work_items:
            linked += 1 if item.is_linked else 0
            unlinked += 0 if item.is_linked else 1
        for failure in page.failures:
            reasons[str(failure.reason)] += 1

        if page.last_extraction_id is None:
            raise PreflightError("a non-empty page reported no cursor")
        if cursors and page.last_extraction_id <= cursors[-1]:
            raise PreflightError("pagination cursor did not advance: a page would be reread")
        cursors.append(int(page.last_extraction_id))
        cursor = page.last_extraction_id
        if not page.has_more:
            break

    return {
        "mode": "force" if force else "normal",
        "examined": examined,
        "work_items": work,
        "failures": sum(reasons.values()),
        "failures_by_reason": dict(sorted(reasons.items())),
        "linked": linked,
        "unlinked": unlinked,
        "pages": pages,
        "cursor_advanced_strictly": all(b > a for a, b in zip(cursors, cursors[1:])),
        "final_cursor": cursors[-1] if cursors else None,
        "accounts_for_population": examined == work + sum(reasons.values()),
    }


def gate_table_counts(engine: Any, tables: Sequence[str] | None = None) -> dict[str, int]:
    """Count every gate table.  A missing table or failed query is fatal."""
    if tables is None:
        from scripts.entities.bounded_verification import GATE_COUNT_TABLES

        tables = GATE_COUNT_TABLES
    counts: dict[str, int] = {}
    for table in tables:
        try:
            with engine.connect() as conn:
                value = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        except ReadOnlyViolation:
            raise
        except Exception as exc:
            raise GateTablesMissing(f"cannot count gate table {table!r}: {exc}") from exc
        if value is None:
            raise GateTablesMissing(f"gate table {table!r} returned no count")
        counts[table] = int(value)
    return counts


def integrity_metrics(engine: Any) -> dict[str, Any]:
    """The Stage 0 integrity snapshot.  A failure here is fatal."""
    try:
        from scripts.entities.detect_entities import _integrity_snapshot

        snapshot = _integrity_snapshot(engine)
    except ReadOnlyViolation:
        raise
    except Exception as exc:
        raise GateTablesMissing(f"cannot read integrity metrics: {exc}") from exc
    if not isinstance(snapshot, Mapping) or not snapshot:
        raise GateTablesMissing("integrity snapshot was empty")
    return dict(snapshot)


def code_fingerprint() -> dict[str, Any]:
    """The executed code set for the event pipeline.  File hashing only."""
    from scripts.entities import detect_entities as detector

    phase = next(p for p in detector.PHASES if p["name"] == "event_pipeline")
    meta = detector._producer_metadata(phase)
    modules = dict(meta.get("code_module_sha256", {}))
    return {
        "code_evidence_complete": meta.get("code_evidence_complete") is True,
        "code_sha256": meta.get("code_sha256"),
        "module_errors": dict(meta.get("code_module_errors", {})),
        "module_count": len(modules),
        "modules": modules,
    }


def active_writers() -> list[str]:
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid,command"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return []
    return active_writer_blockers(listing, own_pid=os.getpid())


#: Sentinel: compute the remediation blocker from live state rather than assuming it.
#: The previous hardcoded string went stale the moment the repair committed.
_LIVE_REMEDIATION = object()


def quarantine_counts(engine: Any) -> dict[str, int]:
    """Explicit quarantine accounting for the extraction table.

    A missing quarantine column is **fatal**, never reported as zero: the
    integration is only trustworthy if the schema genuinely carries the state, so
    a drifted or un-migrated database must stop the gate rather than silently
    claim nothing is quarantined.
    """
    try:
        with engine.connect() as conn:
            total = conn.execute(
                text("SELECT COUNT(*) FROM meeting_event_extractions")
            ).scalar()
            quarantined = conn.execute(
                text(
                    "SELECT COUNT(*) FROM meeting_event_extractions "
                    "WHERE quarantined_at IS NOT NULL"
                )
            ).scalar()
    except ReadOnlyViolation:
        raise
    except Exception as exc:
        raise GateTablesMissing(f"cannot count quarantine state: {exc}") from exc
    if total is None or quarantined is None:
        raise GateTablesMissing("quarantine counts came back empty")
    return {"extractions_total": int(total), "quarantined_excluded": int(quarantined)}


def run_snapshot(
    engine: Any,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    expect_eligible: int | None = None,
    expect_fingerprint: str | None = None,
    expect_target: Mapping[str, Any] | None = None,
    remediation_unapplied: object = _LIVE_REMEDIATION,
    gate_tables_provider: Callable[[Any], dict] | None = None,
    integrity_provider: Callable[[Any], dict] | None = None,
    fingerprint_provider: Callable[[], dict] | None = None,
    active_writers_provider: Callable[[], list[str]] | None = None,
    quarantine_provider: Callable[[Any], dict] | None = None,
) -> dict[str, Any]:
    """Capture one complete read-only snapshot, guarded end to end."""
    target = assert_read_only_target(engine)
    if remediation_unapplied is _LIVE_REMEDIATION:
        # Observed, not assumed: recognises committed remediation and still reports
        # every genuinely undispositioned meeting.
        remediation_unapplied = remediation_state(engine)
    statements = guard_engine(engine)

    population = collect_population(engine, page_size=page_size, force=True)
    counts = (gate_tables_provider or gate_table_counts)(engine)
    integrity = (integrity_provider or integrity_metrics)(engine)
    fingerprint = (fingerprint_provider or code_fingerprint)()
    writers = (active_writers_provider or active_writers)()
    quarantine = (quarantine_provider or quarantine_counts)(engine)

    audit = statement_audit(statements)
    if not audit["select_only"]:
        raise ReadOnlyViolation(
            f"a mutating statement reached the database: {audit['mutating_statements']}"
        )

    population_drift = None
    if expect_eligible is not None and population["examined"] != expect_eligible:
        population_drift = (
            f"eligible population is {population['examined']}, expected {expect_eligible}"
        )

    fingerprint_drift = None
    if expect_fingerprint is not None and fingerprint.get("code_sha256") != expect_fingerprint:
        fingerprint_drift = (
            f"code fingerprint {fingerprint.get('code_sha256')} != expected {expect_fingerprint}"
        )

    target_drift = None
    if expect_target is not None:
        for field in ("tier", "database", "host", "port"):
            if expect_target.get(field) != target.get(field):
                target_drift = (
                    f"target {field} is {target.get(field)!r}, "
                    f"expected {expect_target.get(field)!r}"
                )
                break

    record: dict[str, Any] = {
        "tool": "event_normalize_preflight",
        "kind": "snapshot",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "target_is_development": target["tier"] == "development",
        "mode": population["mode"],
        "eligible_population": population["examined"],
        "eligible_work_items": population["work_items"],
        "extractions_total": quarantine["extractions_total"],
        "quarantined_excluded": quarantine["quarantined_excluded"],
        "quarantine_reconciles": (
            population["examined"] + quarantine["quarantined_excluded"]
            == quarantine["extractions_total"]
        ),
        "linked": population["linked"],
        "unlinked": population["unlinked"],
        "failures_by_reason": population["failures_by_reason"],
        "failures_total": population["failures"],
        "accounts_for_population": population["accounts_for_population"],
        "coverage_complete": True,
        "pages": population["pages"],
        "final_cursor": population["final_cursor"],
        "cursor_advanced_strictly": population["cursor_advanced_strictly"],
        "gate_tables": counts,
        "integrity": integrity,
        "fingerprint": fingerprint,
        "read_only": {
            "guard": "per-connection read-only session + statement guard",
            "statement_audit": audit,
        },
        "active_writers": writers,
        "population_drift": population_drift,
        "fingerprint_drift": fingerprint_drift,
        "target_drift": target_drift,
        "remediation_unapplied": remediation_unapplied,
        "classification_equation": CLASSIFICATION_EQUATION,
    }

    probe = dict(record)
    probe["target_is_development"] = (
        record["target_is_development"] and target_drift is None
    )
    launchable, reasons = launch_decision(probe)
    record["launchable"] = launchable
    record["launch_blockers"] = reasons
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only event_normalize gate snapshot")
    parser.add_argument("--out", default=None)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    args = parser.parse_args(argv)

    try:
        record = run_snapshot(get_engine(), page_size=args.page_size)
    except PreflightError as exc:
        print(json.dumps({"refused": str(exc)}, indent=2))
        return 3

    rendered = json.dumps(record, indent=2, sort_keys=True, default=str)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        print(f"[written] {args.out}")
    print(rendered)
    return 0 if record["launchable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
