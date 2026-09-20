"""Shared validation primitives for knowledge-graph accounting results.

Accounting counters cross process and module boundaries, so permissive Python
coercion is unsafe: ``True``, ``1.5``, and ``"1"`` must not be accepted as the
integer count ``1``. These helpers define that contract in one place.
"""

from collections.abc import Iterable, Mapping
from typing import TypeGuard


def is_nonnegative_integer(value: object) -> TypeGuard[int]:
    """Return whether *value* is an integer count, explicitly excluding bool."""
    return type(value) is int and value >= 0


def invalid_counter_fields(
    counters: Mapping[str, object], required_fields: Iterable[str]
) -> list[str]:
    """Return sorted fields whose values violate the counter contract.

    Missing fields are invalid. Sorting makes diagnostics and durable run
    evidence deterministic regardless of the input iterable's ordering.
    """
    return sorted(
        field
        for field in required_fields
        if not is_nonnegative_integer(counters.get(field))
    )
