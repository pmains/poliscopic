#!/usr/bin/env python3
"""``quarantine_schema.py`` — idempotent, reversible quarantine DDL.

The table authority here is the migration/DDL path, **not** the ORM:
``meeting_event_extractions`` has no SQLAlchemy model in ``scripts/db/models.py``,
so there is no ORM declaration to keep in step.  Dev/prod parity therefore rests
on this idempotent DDL plus the existing schema-parity check, and the statements
below are written to be safe to re-run on either tier.

Semantics are owned by :mod:`scripts.kg.quarantine`; this module only materialises
the columns it declares.  Nothing here connects to a database.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import text  # noqa: E402

from scripts.kg.quarantine import QUARANTINE_COLUMNS  # noqa: E402

__all__ = [
    "COLUMN_DDL",
    "TABLE",
    "column_names",
    "downgrade",
    "is_applied",
    "upgrade",
]

TABLE = "meeting_event_extractions"

#: Column type per quarantine column.  ``TIMESTAMPTZ`` is native on PostgreSQL and
#: accepted by SQLite's dynamic typing, so one definition serves both tiers.
COLUMN_DDL: dict[str, str] = {
    "quarantine_reason": "TEXT",
    "quarantined_at": "TIMESTAMPTZ",
    "quarantined_by": "TEXT",
    "decision_id": "TEXT",
    "model_version": "TEXT",
}

#: Reporting index: finding quarantined rows is the common access path.
INDEX_NAME = "ix_mee_quarantined_at"


def column_names(conn: Any, dialect: str, table: str = TABLE) -> set[str]:
    """Existing column names for ``table`` on either dialect."""
    if dialect == "sqlite":
        rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
        return {str(r[1]) for r in rows}
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
        ),
        {"t": table},
    ).all()
    return {str(r[0]) for r in rows}


def is_applied(conn: Any, dialect: str, table: str = TABLE) -> bool:
    """Whether every quarantine column is present."""
    present = column_names(conn, dialect, table)
    return set(COLUMN_DDL) <= present


def upgrade(conn: Any, dialect: str, table: str = TABLE) -> list[str]:
    """Add any missing quarantine columns.  Returns the statements executed.

    Idempotent: columns already present are skipped, so re-running is a no-op and
    no error.  Safe to apply to development and production alike.
    """
    present = column_names(conn, dialect, table)
    executed: list[str] = []
    for column, col_type in COLUMN_DDL.items():
        if column in present:
            continue
        if_not_exists = " IF NOT EXISTS" if dialect == "postgresql" else ""
        statement = (
            f"ALTER TABLE {table} ADD COLUMN{if_not_exists} {column} {col_type}"
        )
        conn.execute(text(statement))
        executed.append(statement)
    if dialect == "postgresql":
        index = f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {table} (quarantined_at)"
        conn.execute(text(index))
        executed.append(index)
    return executed


def downgrade(conn: Any, dialect: str, table: str = TABLE) -> list[str]:
    """Drop the quarantine columns.  Returns the statements executed.

    Idempotent and reversible.  SQLite gained ``DROP COLUMN`` in 3.35; on older
    SQLite the equivalent is a table rebuild, which is deliberately *not* done
    implicitly — the caller is told instead.
    """
    present = column_names(conn, dialect, table)
    executed: list[str] = []
    for column in COLUMN_DDL:
        if column not in present:
            continue
        if dialect == "sqlite":
            version = tuple(
                int(part) for part in str(
                    conn.execute(text("SELECT sqlite_version()")).scalar()
                ).split(".")[:2]
            )
            if version < (3, 35):
                raise RuntimeError(
                    "SQLite {0}.{1} cannot DROP COLUMN; a table rebuild is required".format(
                        *version
                    )
                )
        statement = f"ALTER TABLE {table} DROP COLUMN {column}"
        conn.execute(text(statement))
        executed.append(statement)
    if dialect == "sqlite" and INDEX_NAME in executed:
        pass
    return executed


def statements_for_review(dialect: str, table: str = TABLE) -> dict[str, Sequence[str]]:
    """The exact SQL a reviewer should read before approving either direction."""
    if_not_exists = " IF NOT EXISTS" if dialect == "postgresql" else ""
    up = [
        f"ALTER TABLE {table} ADD COLUMN{if_not_exists} {column} {col_type}"
        for column, col_type in COLUMN_DDL.items()
    ]
    if dialect == "postgresql":
        up.append(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {table} (quarantined_at)"
        )
    down = [f"ALTER TABLE {table} DROP COLUMN {column}" for column in COLUMN_DDL]
    return {"up": up, "down": down, "columns": list(QUARANTINE_COLUMNS)}
