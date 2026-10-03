"""Small value-normalization helpers shared by persistence modules."""

from datetime import date


def _parse_date(value: object) -> date | None:
    """Parse a value into a date, including SQLite string results."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    return None
