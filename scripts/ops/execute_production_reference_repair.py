#!/usr/bin/env python3
"""Closed transaction executor for the single reviewed OP-REPAIR operation.

This is preparation-only code.  It deliberately has no concrete production
adapter, command-line interface, or interlock route and therefore cannot be
executed against production.  Its sole public operation accepts the exact
immutable context emitted by ``production_reference_g8``.  Until that module
exists, every call refuses before URL lookup, driver loading, or network access.

The small database adapter is intentional: production can bind it to psycopg,
while focused tests can prove ordering and rollback without a database.  It is
not an alternate entry point; adapters receive no authorization decisions and
cannot change the SQL, target, scope, or counts.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
for _candidate in (REPO, REPO / "scripts", Path(__file__).resolve().parent):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

OPERATION = "OP-REPAIR"
EXPECTED = MappingProxyType({"meetings": 1124, "agenda_items": 6849})
EXPECTED_TOTAL = 7973
EXPECTED_QUARANTINE = 3621
TARGET_KEYS = ("database", "configured_host", "configured_port", "server_address",
               "server_port", "cluster_system_identifier")
ADVISORY_LOCK_KEY = 0x504F4C4953434F50  # stable, reviewed, operation-specific
LOCK_TIMEOUT = "15s"
STATEMENT_TIMEOUT = "20min"
BATCH_SIZE = 500


class Refused(RuntimeError):
    """A safe pre-commit refusal."""


class CommitUncertain(RuntimeError):
    """The server's commit acknowledgement was not conclusive."""


class DatabaseAdapter(Protocol):
    """Minimal production-driver seam; methods must not auto-commit."""

    def connect(self, canonical_url: str) -> Any: ...
    def target_identity(self, connection: Any) -> Mapping[str, Any]: ...
    def schema_sha256(self, connection: Any) -> str: ...
    def execute(self, connection: Any, sql: str,
                params: Sequence[Any] = ()) -> int: ...
    def fetch_rows(self, connection: Any, table: str, row_ids: Sequence[int],
                   *, for_update: bool) -> Sequence[Mapping[str, Any]]:
        """Select every physical column; use ``FOR UPDATE`` when requested."""
        ...
    def integrity_vector(self, connection: Any) -> Mapping[str, Any]: ...
    def quarantine_digest(self, connection: Any) -> str: ...
    def commit(self, connection: Any) -> None: ...
    def rollback(self, connection: Any) -> None: ...
    def close(self, connection: Any) -> None: ...


@dataclass(frozen=True)
class FrozenReceipt:
    """Immutable terminal handoff passed back to the G8 append-only state."""

    values: Mapping[str, Any]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalized(value: Any) -> Any:
    """Match the G6 proof's normalization of driver-native scalar values."""
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    return value


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_g8_context(context: Any) -> tuple[Any, Any]:
    """Require the future validator's concrete type, never a duck-typed flag."""
    try:
        validator = importlib.import_module("production_reference_g8")
        context_type = validator.AuthorizedRepairContext
    except (ImportError, AttributeError) as exc:
        raise Refused("closed G8 validator/type is unavailable") from exc
    if type(context) is not context_type:  # subclasses/proxies cannot widen authority
        raise Refused("executor requires the exact G8 AuthorizedRepairContext")
    required = ("operation", "target", "schema_sha256",
                "operations", "preimage_digest", "quarantine_digest",
                "integrity_before", "integrity_after", "bindings", "nonce",
                "attempt_id")
    if any(not hasattr(context, name) for name in required):
        raise Refused("authorized context is incomplete")
    if context.operation != OPERATION:
        raise Refused("authorized context operation differs")
    # This operation must happen before even reading the URL environment.  The
    # validator owns both the already-created claim and terminal state writer;
    # neither can be supplied as a callback by an executor caller.
    try:
        validator.assert_claimed_context(context)
        writer = validator.write_context_terminal
    except (AttributeError, TypeError, ValueError) as exc:
        raise Refused("G8 context does not prove its consumed claim") from exc
    return context, writer


def _canonical_url(context: Any) -> str:
    """Use the sole authoritative resolver, after G8 has consumed its claim."""
    preflight = importlib.import_module("production_preflight")
    configured = preflight.resolve_production_url(REPO / ".env")
    parsed = urlsplit(configured)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise Refused("canonical production URL is not PostgreSQL")
    database = parsed.path.lstrip("/")
    target = context.target
    if (database != target.get("database") or parsed.hostname != target.get("configured_host") or
            parsed.port != target.get("configured_port")):
            raise Refused("resolved production URL differs from authorized target")
    return configured


def _operations(context: Any) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in EXPECTED}
    seen: set[tuple[str, int]] = set()
    raw = context.operations
    if not isinstance(raw, tuple) or len(raw) != EXPECTED_TOTAL:
        raise Refused("authorized operation population is not immutable/exact")
    for operation in raw:
        if not isinstance(operation, Mapping):
            raise Refused("authorized operation is malformed")
        table = operation.get("table")
        primary = operation.get("primary_key")
        change = operation.get("set")
        before = operation.get("before")
        row_id = primary.get("id") if isinstance(primary, Mapping) else None
        if (operation.get("kind") != "UPDATE" or table not in EXPECTED or
                not isinstance(row_id, int) or type(change) not in (dict, MappingProxyType) or
                set(change) != {"public_body_id"} or
                not isinstance(change["public_body_id"], int) or
                not isinstance(before, Mapping) or before.get("id") != row_id):
            raise Refused("operation exceeds the two-table/one-column allowlist")
        key = (table, row_id)
        if key in seen:
            raise Refused("duplicate authorized operation")
        seen.add(key)
        grouped[table].append({"id": row_id, "before": dict(before),
                               "after": change["public_body_id"]})
    if {table: len(rows) for table, rows in grouped.items()} != dict(EXPECTED):
        raise Refused("authorized operation rowcounts differ")
    return {table: tuple(rows) for table, rows in grouped.items()}


def _batch(rows: Sequence[dict[str, Any]]) -> Sequence[Sequence[dict[str, Any]]]:
    return tuple(rows[index:index + BATCH_SIZE]
                 for index in range(0, len(rows), BATCH_SIZE))


def _validate_preimages(adapter: DatabaseAdapter, connection: Any,
                        grouped: Mapping[str, Sequence[dict[str, Any]]]
                        ) -> tuple[str, dict[str, dict[int, dict[str, Any]]]]:
    canonical: list[dict[str, Any]] = []
    full_rows: dict[str, dict[int, dict[str, Any]]] = {
        table: {} for table in EXPECTED}
    for table in sorted(EXPECTED):
        for batch in _batch(grouped[table]):
            expected = {item["id"]: item for item in batch}
            observed = adapter.fetch_rows(connection, table, tuple(expected),
                                          for_update=True)
            if len(observed) != len(expected):
                raise Refused("missing or extra target preimage")
            by_id: dict[int, Mapping[str, Any]] = {}
            for row in observed:
                row_id = row.get("id")
                if not isinstance(row_id, int) or row_id in by_id or row_id not in expected:
                    raise Refused("duplicate or unexpected target preimage")
                by_id[row_id] = row
            for row_id, wanted in expected.items():
                actual = dict(by_id[row_id])
                baseline = wanted["before"]
                if any(key not in actual for key in baseline):
                    raise Refused("physical row is missing an authorized preimage key")
                authorized_actual = _normalized(
                    {key: actual[key] for key in baseline})
                if authorized_actual != _normalized(baseline):
                    raise Refused("authorized preimage drift")
                full_rows[table][row_id] = actual
                canonical.append({"table": table, "id": row_id,
                                  "before": authorized_actual})
    canonical.sort(key=lambda item: (item["table"], item["id"]))
    return _digest(canonical), full_rows


def _validate_postimages(adapter: DatabaseAdapter, connection: Any,
                         grouped: Mapping[str, Sequence[dict[str, Any]]],
                         full_rows: Mapping[str, Mapping[int, Mapping[str, Any]]]
                         ) -> str:
    canonical: list[dict[str, Any]] = []
    for table in sorted(EXPECTED):
        for batch in _batch(grouped[table]):
            expected = {item["id"]: item for item in batch}
            observed = adapter.fetch_rows(connection, table, tuple(expected),
                                          for_update=True)
            if len(observed) != len(expected):
                raise Refused("missing or extra target postimage")
            by_id: dict[int, Mapping[str, Any]] = {}
            for row in observed:
                row_id = row.get("id")
                if not isinstance(row_id, int) or row_id in by_id or row_id not in expected:
                    raise Refused("duplicate or unexpected target postimage")
                by_id[row_id] = row
            for row_id, wanted in expected.items():
                actual = dict(by_id[row_id])
                physical_before = dict(full_rows[table][row_id])
                physical_after = dict(physical_before)
                physical_after["public_body_id"] = wanted["after"]
                if actual != physical_after:
                    raise Refused("postcondition or unrelated physical column drift")
                canonical.append({"table": table, "id": row_id,
                                  "after": _normalized(actual)})
    canonical.sort(key=lambda item: (item["table"], item["id"]))
    return _digest(canonical)


def _update(adapter: DatabaseAdapter, connection: Any, table: str,
            rows: Sequence[dict[str, Any]]) -> int:
    # Identifiers are selected only from the hardcoded branch. Values remain params.
    sql = (f"UPDATE public.{table} AS target SET public_body_id = patch.public_body_id "
           "FROM (SELECT unnest(%s::bigint[]) AS id, "
           "unnest(%s::bigint[]) AS public_body_id) AS patch "
           "WHERE target.id = patch.id")
    ids = tuple(row["id"] for row in rows)
    values = tuple(row["after"] for row in rows)
    return adapter.execute(connection, sql, (ids, values))


def _receipt(context: Any, started: str, terminal: str, details: Mapping[str, Any],
             error_code: str | None = None) -> FrozenReceipt:
    body = {"schema": "production-reference-repair-terminal/1",
            "operation": OPERATION, "attempt_id": context.attempt_id,
            "nonce": context.nonce, "started_at": started,
            "finished_at": _utcnow(), "terminal": terminal,
            "target": dict(context.target), "bindings": dict(context.bindings),
            "isolation": "SERIALIZABLE",
            "locks": {"advisory": ADVISORY_LOCK_KEY,
                      "tables": ["public.meetings", "public.agenda_items"],
                      "mode": "SHARE ROW EXCLUSIVE"},
            **dict(details)}
    if error_code is not None:
        body["error"] = {"class": "safe-executor-refusal", "code": error_code}
    value = {**body, "digest": _digest(body)}
    return FrozenReceipt(_freeze(value))


def execute_authorized(context: Any, adapter: DatabaseAdapter) -> FrozenReceipt:
    """Consume one G8 context and perform exactly one all-or-nothing transaction."""
    context, terminal_writer = _require_g8_context(context)
    url = _canonical_url(context)
    grouped = _operations(context)
    started = _utcnow()
    connection = None
    details: dict[str, Any] = {"operation_counts": {"total": EXPECTED_TOTAL,
                                                    "by_table": dict(EXPECTED)}}
    try:
        connection = adapter.connect(url)
        adapter.execute(connection, "BEGIN ISOLATION LEVEL SERIALIZABLE")
        adapter.execute(connection, "SET LOCAL lock_timeout = %s", (LOCK_TIMEOUT,))
        adapter.execute(connection, "SET LOCAL statement_timeout = %s", (STATEMENT_TIMEOUT,))
        observed_target = dict(adapter.target_identity(connection))
        if any(observed_target.get(key) != context.target.get(key) for key in TARGET_KEYS):
            raise Refused("connected production target differs")
        if adapter.schema_sha256(connection) != context.schema_sha256:
            raise Refused("production schema differs")
        adapter.execute(connection, "SELECT pg_advisory_xact_lock(%s)",
                        (ADVISORY_LOCK_KEY,))
        adapter.execute(connection,
                        "LOCK TABLE public.meetings, public.agenda_items "
                        "IN SHARE ROW EXCLUSIVE MODE")
        before_integrity = dict(adapter.integrity_vector(connection))
        if before_integrity != dict(context.integrity_before):
            raise Refused("pre-update integrity vector differs")
        before_quarantine = adapter.quarantine_digest(connection)
        if before_quarantine != context.quarantine_digest:
            raise Refused("quarantine population differs")
        details["preimage_digest"], full_preimages = _validate_preimages(
            adapter, connection, grouped)
        if details["preimage_digest"] != context.preimage_digest:
            raise Refused("complete preimage digest differs")
        rowcounts = {table: _update(adapter, connection, table, grouped[table])
                     for table in ("meetings", "agenda_items")}
        if rowcounts != dict(EXPECTED):
            raise Refused("update rowcount differs")
        details["update_rowcounts"] = rowcounts
        details["postimage_digest"] = _validate_postimages(
            adapter, connection, grouped, full_preimages)
        after_integrity = dict(adapter.integrity_vector(connection))
        if after_integrity != dict(context.integrity_after):
            raise Refused("post-update integrity vector differs")
        if adapter.quarantine_digest(connection) != before_quarantine:
            raise Refused("quarantine changed")
        details.update({"observed_target": observed_target,
                        "schema_sha256": context.schema_sha256,
                        "integrity": {"before": before_integrity,
                                      "after": after_integrity},
                        "quarantine": {"count": EXPECTED_QUARANTINE,
                                       "digest": before_quarantine,
                                       "unchanged": True}})
        try:
            adapter.commit(connection)
        except Exception as exc:
            details["commit_outcome"] = "UNCERTAIN"
            receipt = _receipt(context, started, "COMMIT_UNCERTAIN", details,
                               "commit-acknowledgement-uncertain")
            terminal_writer(context, receipt)
            raise CommitUncertain("commit acknowledgement is uncertain") from exc
        details["commit_outcome"] = "ACKNOWLEDGED"
        receipt = _receipt(context, started, "SUCCESS", details)
        terminal_writer(context, receipt)
        return receipt
    except CommitUncertain:
        raise
    except Exception as exc:
        if connection is not None:
            try:
                adapter.rollback(connection)
            except Exception:
                pass
        receipt = _receipt(context, started, "FAILED", details,
                           "precommit-refusal")
        terminal_writer(context, receipt)
        if isinstance(exc, Refused):
            raise
        raise Refused("repair failed before acknowledged commit") from exc
    finally:
        if connection is not None:
            adapter.close(connection)
