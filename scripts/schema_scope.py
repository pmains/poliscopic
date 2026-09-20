"""Shared, deterministic scope for application-schema metadata reads."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

PUBLIC_SCHEMA = "public"


def application_schema(bind: Any) -> str | None:
    """Return the explicit application schema for PostgreSQL, else default scope.

    SQLite fixtures do not have PostgreSQL's ``public`` namespace, so callers
    retain their normal default-schema behavior there.
    """
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    return PUBLIC_SCHEMA if dialect == "postgresql" else None


def current_schema_columns(connection: Any, table: str) -> list[dict[str, Any]]:
    """Read one table's columns from the active PostgreSQL schema canonically.

    ``current_schema()`` excludes same-named FDW tables in the production
    ``dev`` schema.  The secondary name key makes the result deterministic even
    for intentionally adversarial metadata fixtures with tied ordinals.
    """
    rows = connection.execute(text("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = :table
        ORDER BY ordinal_position, column_name
    """), {"table": table}).mappings().all()
    return [dict(row) for row in rows]
