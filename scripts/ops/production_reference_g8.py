#!/usr/bin/env python3
"""Offline G8 validation and single-use state for the reviewed OP-REPAIR.

This module deliberately has no command-line authorization issuer, credential,
database, network, environment-variable, or force path.  A caller may validate
literal artifacts and consume an authorization before doing any external work.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

G7_SCHEMA = "production-reference-op-repair-g7/2"
G8_SCHEMA = "production-reference-op-repair-g8/1"
CLAIM_SCHEMA = "production-reference-op-repair-claim/1"
TERMINAL_SCHEMA = "production-reference-op-repair-terminal/1"
OPERATION = "OP-REPAIR"
APPROVAL_PHRASE = "I approve OP-REPAIR"
READY_STATUS = "G7-READY-FOR-HUMAN-AUTHORIZATION"
EXPECTED_COMMIT_LENGTH = 40
EXPECTED_COUNTS = {"total": 7973, "by_table": {"meetings": 1124,
                                                "agenda_items": 6849}}
EXPECTED_SCOPE = {
    "tables": {
        "public.meetings": {"column": "public_body_id", "rows": 1124},
        "public.agenda_items": {"column": "public_body_id", "rows": 6849},
    },
    "quarantine_count": 3621,
    "quarantine_excluded": True,
    "other_changes": False,
}
TERMINALS = frozenset({"SUCCESS", "FAILED", "COMMIT_UNCERTAIN"})


class Refused(RuntimeError):
    """Fail-closed local authorization refusal."""


_CONTEXT_TOKEN = object()


def _freeze(value: Any) -> Any:
    """Recursively detach JSON data and expose no mutable containers."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class AuthorizedRepairContext:
    """Exact post-claim capability passed to the one reviewed executor."""

    g7: Mapping[str, Any]
    g8: Mapping[str, Any]
    attempt_id: str
    operation: str
    operations: tuple[Mapping[str, Any], ...]
    preimage_digest: str
    quarantine_digest: str
    integrity_before: Mapping[str, Any]
    integrity_after: Mapping[str, Any]
    bindings: Mapping[str, Any]
    target: Mapping[str, Any]
    schema_sha256: str
    nonce: str
    operation_counts: Mapping[str, Any]
    scope: Mapping[str, Any]
    _state_dir: Path = field(repr=False)
    _seal: object = field(init=False, repr=False, compare=False)
    _token: InitVar[object] = None

    def __post_init__(self, _token: object) -> None:
        if _token is not _CONTEXT_TOKEN:
            raise Refused("authorization context may only be created by authorize()")
        object.__setattr__(self, "_seal", _CONTEXT_TOKEN)

    def write_terminal(self, receipt: object) -> dict[str, Any]:
        """Bound safe-receipt terminal writer; no caller callback is retained."""
        return write_context_terminal(self, receipt)


def require_authorized_context(value: object) -> AuthorizedRepairContext:
    """Defense in depth for the executor boundary; subclasses are refused."""
    if (type(value) is not AuthorizedRepairContext or
            getattr(value, "_seal", None) is not _CONTEXT_TOKEN):
        raise Refused("exact AuthorizedRepairContext is required")
    return value


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise Refused(f"{label} is absent")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refused(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise Refused(f"{label} lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _hex(value: Any, length: int, label: str) -> str:
    if (not isinstance(value, str) or len(value) != length or
            any(character not in "0123456789abcdef" for character in value)):
        raise Refused(f"{label} is not {length}-character lowercase hexadecimal")
    return value


def _load_immutable(path: Path, label: str, schema: str) -> dict[str, Any]:
    """Read one owner-only, single-link regular file without following symlinks."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise Refused(f"{label} is unavailable") from exc
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or
            stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
        raise Refused(f"{label} must be a single-link mode-0600 regular non-symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            raw = b""
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                raw += block
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise Refused(f"{label} cannot be safely opened") from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
        raise Refused(f"{label} changed while it was read")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(f"{label} is not readable JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise Refused(f"{label} schema is not exact {schema}")
    body = {key: item for key, item in value.items() if key != "digest"}
    if value.get("digest") != digest(body) or raw != canonical_bytes(value):
        raise Refused(f"{label} is not canonical or its digest differs")
    return value


def _validate_g7(g7: Mapping[str, Any], current: datetime) -> None:
    if (g7.get("operation") != OPERATION or g7.get("status") != READY_STATUS or
            g7.get("authorization") != "none - this plan authorizes nothing" or
            g7.get("applies_anything") is not False):
        raise Refused("G7 is not the non-authorizing ready v2 plan")
    if g7.get("operation_counts") != EXPECTED_COUNTS or g7.get("scope") != EXPECTED_SCOPE:
        raise Refused("G7 scope or counts differ from the reviewed operation")
    operations = g7.get("operations")
    if not isinstance(operations, list) or len(operations) != EXPECTED_COUNTS["total"]:
        raise Refused("G7 full operation plan is absent")
    by_table: dict[str, int] = {}
    identities: set[tuple[str, int]] = set()
    for operation in operations:
        table = operation.get("table") if isinstance(operation, dict) else None
        primary = operation.get("primary_key") if isinstance(operation, dict) else None
        row_id = primary.get("id") if isinstance(primary, dict) else None
        change = operation.get("set") if isinstance(operation, dict) else None
        if (operation.get("kind") != "UPDATE" or table not in {"meetings", "agenda_items"} or
                not isinstance(row_id, int) or not isinstance(change, dict) or
                set(change) != {"public_body_id"} or
                not isinstance(change["public_body_id"], int)):
            raise Refused("G7 contains an operation outside the exact update-only scope")
        _hex(operation.get("preimage_digest"), 64, "G7 operation preimage digest")
        identity = (table, row_id)
        if identity in identities:
            raise Refused("G7 contains duplicate operation identities")
        identities.add(identity)
        by_table[table] = by_table.get(table, 0) + 1
    if by_table != EXPECTED_COUNTS["by_table"]:
        raise Refused("G7 operation population differs from exact table counts")
    bindings = g7.get("bindings")
    if not isinstance(bindings, dict):
        raise Refused("G7 bindings are absent")
    _hex(bindings.get("exact_commit"), EXPECTED_COMMIT_LENGTH, "G7 exact commit")
    preimage_digest = _hex(g7.get("preimage_digest"), 64, "G7 preimage_digest")
    if bindings.get("g6_proposal_preimages_digest") != preimage_digest:
        raise Refused("G7 preimage digest does not equal its bound G6 proof")
    _hex(g7.get("quarantine_digest"), 64, "G7 quarantine_digest")
    for name in ("integrity_before", "integrity_after"):
        vector = g7.get(name)
        if not isinstance(vector, dict) or not vector:
            raise Refused(f"G7 {name} must be a non-empty integrity mapping")
    _hex(g7.get("nonce"), 64, "G7 nonce")
    created = _parse_time(g7.get("created_at"), "G7 created_at")
    expiry = _parse_time(g7.get("expires_at"), "G7 expires_at")
    if not created <= current < expiry or expiry - created > timedelta(hours=4):
        raise Refused("G7 is outside its authorization window")


def validate(g7_path: Path, g8_path: Path,
             now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate exact local artifacts without consuming or doing external work."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    g7 = _load_immutable(g7_path, "G7", G7_SCHEMA)
    g8 = _load_immutable(g8_path, "G8", G8_SCHEMA)
    _validate_g7(g7, current)
    approval = g8.get("approval")
    author = g8.get("author")
    if (g8.get("operation") != OPERATION or g8.get("g7_digest") != g7["digest"] or
            g8.get("nonce") != g7["nonce"] or
            g8.get("exact_commit") != g7["bindings"]["exact_commit"] or
            g8.get("scope") != EXPECTED_SCOPE or
            g8.get("operation_counts") != EXPECTED_COUNTS):
        raise Refused("G8 does not bind the exact G7 operation")
    if (not isinstance(author, dict) or author.get("kind") != "human" or
            not isinstance(author.get("name"), str) or
            len(author["name"].strip().split()) < 2):
        raise Refused("G8 author must be a named human")
    if not isinstance(approval, dict) or set(approval) != {"verbatim", "sha256"}:
        raise Refused("G8 verbatim approval is absent")
    text = approval.get("verbatim")
    if (not isinstance(text, str) or text.strip() != text or not text or
            approval.get("sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()):
        raise Refused("G8 approval text or text digest differs")
    # These literal tokens are mandatory; accepting a summary field would permit
    # an agent-authored paraphrase to stand in for the human's actual words.
    if APPROVAL_PHRASE not in text or g7["digest"] not in text:
        raise Refused("verbatim approval must explicitly approve OP-REPAIR and contain the full G7 digest")
    approved = _parse_time(g8.get("approved_at"), "G8 approved_at")
    expiry = _parse_time(g8.get("expires_at"), "G8 expires_at")
    g7_created = _parse_time(g7.get("created_at"), "G7 created_at")
    g7_expiry = _parse_time(g7.get("expires_at"), "G7 expires_at")
    if not g7_created <= approved <= current < expiry <= g7_expiry:
        raise Refused("G8 is outside the exact G7 approval window")
    return g7, g8


def _state_stem(g7: Mapping[str, Any]) -> str:
    return f"{g7['digest']}.{g7['nonce']}"


def _exclusive_write(path: Path, payload: Mapping[str, Any]) -> None:
    data = canonical_bytes(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _terminal_paths(state_dir: Path, stem: str) -> list[Path]:
    return [state_dir / f"{stem}.{terminal}.json" for terminal in sorted(TERMINALS)]


def _validate_state_dir(state_dir: Path) -> None:
    try:
        metadata = state_dir.lstat()
    except OSError as exc:
        raise Refused("state directory is unavailable") from exc
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or
            stat.S_IMODE(metadata.st_mode) != 0o700 or
            metadata.st_uid != os.getuid()):
        raise Refused("state directory must be a current-user-owned mode-0700 real directory")


def inspect_state(state_dir: Path, g7: Mapping[str, Any]) -> str | None:
    """Return CLAIMED/a terminal/None; refuse malformed or ambiguous state."""
    _validate_state_dir(state_dir)
    stem = _state_stem(g7)
    claim = state_dir / f"{stem}.CLAIMED.json"
    terminals = [path for path in _terminal_paths(state_dir, stem) if path.exists()]
    if len(terminals) > 1:
        raise Refused("authorization has multiple ambiguous terminal states")
    if terminals and not claim.exists():
        raise Refused("terminal state exists without its claim")
    if terminals:
        return terminals[0].name.rsplit(".", 2)[1]
    return "CLAIMED" if claim.exists() else None


def claim(g7_path: Path, g8_path: Path, state_dir: Path,
          now: datetime | None = None) -> dict[str, Any]:
    """Validate, then atomically and durably consume this authorization once."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    g7, g8 = validate(g7_path, g8_path, current)
    _validate_state_dir(state_dir)
    if inspect_state(state_dir, g7) is not None:
        raise Refused("authorization was already consumed")
    body = {"schema": CLAIM_SCHEMA, "state": "CLAIMED", "operation": OPERATION,
            "g7_digest": g7["digest"], "g8_digest": g8["digest"],
            "nonce": g7["nonce"],
            "claimed_at": current.strftime("%Y-%m-%dT%H:%M:%SZ")}
    artifact = {**body, "digest": digest(body)}
    try:
        _exclusive_write(state_dir / f"{_state_stem(g7)}.CLAIMED.json", artifact)
    except FileExistsError as exc:
        raise Refused("authorization was claimed concurrently") from exc
    return artifact


def authorize(g7_path: Path, g8_path: Path, state_dir: Path,
              now: datetime | None = None) -> AuthorizedRepairContext:
    """Validate and durably consume G8, then return the sole executor capability."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    g7, g8 = validate(g7_path, g8_path, current)
    claimed = claim(g7_path, g8_path, state_dir, current)
    frozen_g7 = _freeze(g7)
    frozen_g8 = _freeze(g8)
    operations = frozen_g7["operations"]
    return AuthorizedRepairContext(
        g7=frozen_g7, g8=frozen_g8, attempt_id=claimed["digest"],
        operation=OPERATION, operations=operations,
        preimage_digest=frozen_g7["preimage_digest"],
        quarantine_digest=frozen_g7["quarantine_digest"],
        integrity_before=frozen_g7["integrity_before"],
        integrity_after=frozen_g7["integrity_after"],
        bindings=frozen_g7["bindings"],
        target=frozen_g7["target"], schema_sha256=frozen_g7["schema_sha256"],
        nonce=frozen_g7["nonce"], operation_counts=frozen_g7["operation_counts"],
        scope=frozen_g7["scope"], _state_dir=state_dir, _token=_CONTEXT_TOKEN)


def assert_claimed_context(context: object) -> AuthorizedRepairContext:
    """Require this module's exact capability and its still-open durable claim."""
    authorized = require_authorized_context(context)
    if inspect_state(authorized._state_dir, authorized.g7) != "CLAIMED":
        raise Refused("authorized context claim is absent, terminal, or ambiguous")
    return authorized


def _safe_receipt_value(value: Any) -> Any:
    """Thaw only immutable JSON values; reject handles, callbacks, and objects."""
    if isinstance(value, MappingProxyType):
        if any(not isinstance(key, str) for key in value):
            raise Refused("terminal receipt keys must be strings")
        return {key: _safe_receipt_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_safe_receipt_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise Refused("terminal receipt contains a mutable or unsafe value")


def write_context_terminal(context: object, receipt: object) -> dict[str, Any]:
    """Append the executor's frozen safe receipt as this context's one terminal."""
    authorized = assert_claimed_context(context)
    values = getattr(receipt, "values", None)
    if not isinstance(values, MappingProxyType):
        raise Refused("terminal receipt values must be frozen")
    safe = _safe_receipt_value(values)
    outcome = safe.get("terminal")
    if (outcome not in TERMINALS or safe.get("operation") != OPERATION or
            safe.get("attempt_id") != authorized.attempt_id or
            safe.get("nonce") != authorized.nonce or
            safe.get("target") != dict(authorized.target) or
            safe.get("bindings") != dict(authorized.bindings)):
        raise Refused("terminal receipt does not bind the authorized attempt")
    supplied_digest = safe.pop("digest", None)
    # Executor receipts use canonical JSON without the artifact newline.
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    if supplied_digest != hashlib.sha256(encoded).hexdigest():
        raise Refused("terminal receipt digest differs")
    return write_terminal(authorized._state_dir, authorized.g7,
                          authorized.g8["digest"], outcome,
                          {**safe, "receipt_digest": supplied_digest})


def write_terminal(state_dir: Path, g7: Mapping[str, Any], g8_digest: str,
                   outcome: str, details: Mapping[str, Any],
                   now: datetime | None = None) -> dict[str, Any]:
    """Append the one immutable terminal for a claimed attempt."""
    if outcome not in TERMINALS:
        raise Refused("terminal outcome is not closed")
    if not isinstance(details, Mapping):
        raise Refused("terminal details must be a mapping")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if inspect_state(state_dir, g7) != "CLAIMED":
        raise Refused("claim is absent, terminal, or ambiguous")
    body = {"schema": TERMINAL_SCHEMA, "state": outcome, "operation": OPERATION,
            "g7_digest": g7["digest"], "g8_digest": g8_digest,
            "nonce": g7["nonce"],
            "completed_at": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "details": dict(details)}
    artifact = {**body, "digest": digest(body)}
    try:
        _exclusive_write(state_dir / f"{_state_stem(g7)}.{outcome}.json", artifact)
    except FileExistsError as exc:
        raise Refused("terminal already exists") from exc
    # Detect a competing terminal created between inspection and our append.
    if inspect_state(state_dir, g7) != outcome:
        raise Refused("competing terminal made state ambiguous")
    return artifact


def success(state_dir: Path, g7: Mapping[str, Any], g8_digest: str,
            details: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    return write_terminal(state_dir, g7, g8_digest, "SUCCESS", details, now)


def failed(state_dir: Path, g7: Mapping[str, Any], g8_digest: str,
           details: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    return write_terminal(state_dir, g7, g8_digest, "FAILED", details, now)


def commit_uncertain(state_dir: Path, g7: Mapping[str, Any], g8_digest: str,
                     details: Mapping[str, Any],
                     now: datetime | None = None) -> dict[str, Any]:
    return write_terminal(state_dir, g7, g8_digest, "COMMIT_UNCERTAIN", details, now)
