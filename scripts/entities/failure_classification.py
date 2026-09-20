"""Typed failure classification for phase retry and abort decisions.

A phase failure is exactly one of two kinds:

``TRANSIENT``
    An infrastructure fault that may plausibly succeed on a retry: connection
    loss, a dropped socket, a timeout, or lock contention.

``DETERMINISTIC``
    A contract fault that reproduces exactly: a refused ontology value, a
    malformed data shape (a blank entity identity, a bad assertion), or a
    refused receipt.  Retrying spends a whole phase to reproduce the same error
    and hides the defect, so these get **one** attempt.

The default is ``DETERMINISTIC``.  Retry is reserved for narrowly classified
transient infrastructure faults, so an unrecognised error is never retried and
never silently swallowed by a retry loop.

The same classification drives the orchestrator's abort rule, so retry policy
and abort policy cannot disagree: a deterministic failure is both
non-retryable and aborting.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DETERMINISTIC",
    "TRANSIENT",
    "classify_failure",
    "is_retryable",
    "should_abort",
]

TRANSIENT = "transient"
DETERMINISTIC = "deterministic"


def _transient_exception_types() -> tuple[type[BaseException], ...]:
    """Return the exception types that count as transient infrastructure faults.

    Narrow on purpose: anything not listed falls through to ``DETERMINISTIC``.
    """
    types: list[type[BaseException]] = [ConnectionError, TimeoutError]
    try:  # SQLAlchemy is a hard dependency, but classification must not crash.
        from sqlalchemy.exc import (
            DisconnectionError,
            InterfaceError,
            OperationalError,
        )
    except Exception:  # pragma: no cover - defensive
        return tuple(types)
    types.extend([DisconnectionError, InterfaceError, OperationalError])
    return tuple(types)


TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    _transient_exception_types()
)


def classify_failure(
    error: BaseException | None = None, *, receipt_refused: bool = False,
) -> str:
    """Classify one failure as ``TRANSIENT`` or ``DETERMINISTIC``.

    ``receipt_refused`` short-circuits to deterministic: a refused receipt is a
    contract outcome, never an infrastructure fault.
    """
    if receipt_refused:
        return DETERMINISTIC
    if error is None:
        return DETERMINISTIC
    if isinstance(error, TRANSIENT_EXCEPTION_TYPES):
        return TRANSIENT
    return DETERMINISTIC


def is_retryable(kind: str) -> bool:
    """Whether a failure of ``kind`` may be retried."""
    return kind == TRANSIENT


def should_abort(kind: str) -> bool:
    """Whether a failure of ``kind`` aborts the remaining phases."""
    return kind == DETERMINISTIC


def describe(kind: str) -> str:
    """Human-readable one-liner for logs and artifacts."""
    if kind == TRANSIENT:
        return "transient infrastructure failure (retryable)"
    return "deterministic contract failure (single attempt, aborting)"


def classify_result_payload(payload: Any) -> str:
    """Classify a returned (non-raising) failure payload.

    A phase that returns ``success=False`` has already completed its own
    validation, so it is always deterministic.
    """
    return DETERMINISTIC
