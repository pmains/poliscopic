#!/usr/bin/env python3
"""``stage2_s2_admission_tx.py`` — the one transaction every apply unit lives in.

Admission and the (future) write are **one unit of work**, not two.  If the checks
run in one transaction and the write in another, the database can move between
them and every guarantee the checks established is void by the time the write
happens.

So the transaction is *owned* here, and the caller supplies a callable rather than
a connection:

* the connection is acquired and the transaction **begun before any check runs**;
* the same transaction is **retained through the write, its postconditions and the
  commit** — nothing is committed early and nothing is re-read outside it;
* on **any** failure the transaction is rolled back and the exception propagates;
* a **serialization failure** (SQLSTATE ``40001`` / ``40P01``, or a locked SQLite
  writer) retries the **entire unit** — checks included — up to
  ``MAX_SERIALIZATION_ATTEMPTS``, because a retry that reused the earlier checks
  would be validating a snapshot that no longer exists;
* a caller cannot hand in an **already-open transaction**.  The whole point is
  that this module owns the begin, so accepting one would let a caller decide when
  the snapshot starts while this module claimed otherwise.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

__all__ = [
    "MAX_SERIALIZATION_ATTEMPTS",
    "SERIALIZABLE",
    "SerializationExhausted",
    "TransactionRefused",
    "is_serialization_failure",
    "run_unit",
]

#: The isolation the admission is proved under, and the write must keep.
SERIALIZABLE = "SERIALIZABLE"

#: Serialization failures retry this many times *in total*, then refuse.
MAX_SERIALIZATION_ATTEMPTS = 3

#: SQLSTATEs that mean "the snapshot raced; redo the whole unit".
_SERIALIZATION_SQLSTATES = frozenset({"40001", "40P01"})

T = TypeVar("T")


class TransactionRefused(RuntimeError):
    """The unit was not run; nothing was committed."""


class SerializationExhausted(TransactionRefused):
    """The unit kept losing a serialization race; it was not committed."""


def _sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE a driver attached to an exception, if any.

    Checked on the exception and on its ``orig`` cause, because SQLAlchemy wraps
    the driver error and the code lives on the wrapped object.
    """
    for candidate in (exc, getattr(exc, "orig", None)):
        if candidate is None:
            continue
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(candidate, attribute, None)
            if value:
                return str(value)
        args = getattr(candidate, "args", ())
        for argument in args:
            text = str(argument)
            for code in _SERIALIZATION_SQLSTATES:
                if code in text:
                    return code
    return None


def is_serialization_failure(exc: BaseException) -> bool:
    """Is this the kind of failure that means "the whole unit must be redone"?"""
    if _sqlstate(exc) in _SERIALIZATION_SQLSTATES:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "could not serialize access",
        "deadlock detected",
        "database is locked",
        "database table is locked",
        "restart transaction",
    )
    return any(marker in text for marker in markers)


def _acquire(engine: Any) -> Any:
    """The owner acquires its **own** connection from an Engine.

    A caller-supplied ``Connection`` is refused outright.  The whole point is that
    this module decides when the snapshot begins and when it ends; accepting a
    connection would let a caller hold it open, share it, or hand in one that is
    already inside someone else's transaction.
    """
    if not hasattr(engine, "connect"):
        raise TransactionRefused(
            "the transaction owner acquires its own connection: pass the Engine, "
            "not a Connection (a caller-supplied connection is refused)")
    return engine.connect()


def _begin(connection: Any) -> Any:
    """Begin the one transaction, at SERIALIZABLE, before any check runs."""
    isolation = getattr(connection, "get_isolation_level", None)
    try:
        connection = connection.execution_options(isolation_level=SERIALIZABLE)
    except Exception as exc:  # noqa: BLE001 - unsupported level must refuse, not pass
        raise TransactionRefused(
            f"the connection will not take {SERIALIZABLE} isolation: {exc}") from exc
    if isolation is not None:
        try:
            observed = connection.get_isolation_level()
        except Exception:  # noqa: BLE001 - SQLite reports through the dialect
            observed = None
        if observed is not None and str(observed).upper() != SERIALIZABLE:
            raise TransactionRefused(
                f"isolation is {observed!r}, not {SERIALIZABLE!r}")
    return connection.begin()


def _abandon(connection: Any, transaction: Any) -> None:
    """Roll back and close **before** any retry.  Both steps are best-effort.

    A failure may have come from the commit itself, in which case the transaction
    is already deactivated and ``rollback`` raises; that must not mask the original
    failure.  The connection is closed either way, so no retry inherits state from
    the attempt that lost the race.
    """
    if transaction is not None:
        try:
            transaction.rollback()
        except Exception:  # noqa: BLE001 - the original failure wins
            pass
    if connection is not None:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing must not mask a refusal
            pass


def run_unit(engine: Any, unit: Callable[[Any], T], *,
             max_attempts: int = MAX_SERIALIZATION_ATTEMPTS,
             on_attempt: Callable[[int], None] | None = None) -> T:
    """Run ``unit`` inside one owned, SERIALIZABLE transaction.

    ``unit`` receives the connection and must do **everything** — checks, write,
    postconditions — because the transaction is committed only after it returns.
    A serialization failure (including one raised by the **commit** itself) rolls
    back, closes, and reruns ``unit`` from the top with a fresh snapshot.
    """
    attempts = max(1, int(max_attempts))
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        connection = None
        transaction = None
        try:
            connection = _acquire(engine)
            transaction = _begin(connection)
            result = unit(connection)
            transaction.commit()
            return result
        except BaseException as exc:  # noqa: BLE001 - rollback and close first
            _abandon(connection, transaction)
            if not is_serialization_failure(exc) or attempt >= attempts:
                if is_serialization_failure(exc):
                    raise SerializationExhausted(
                        f"serialization failed {attempt} times; the unit was not "
                        f"committed: {exc}") from exc
                raise
            last = exc
    raise SerializationExhausted(  # pragma: no cover - loop always returns or raises
        f"serialization failed after {attempts} attempts: {last}")
