"""Conservative read-only SQL statement detection."""

from __future__ import annotations

import re

__all__ = [
    "MUTATING_FUNCTIONS",
    "MUTATING_KEYWORDS",
    "READ_ONLY_PREFIXES",
    "WRITE_CAPABLE_HEADS",
    "split_statements",
    "statement_write_problem",
    "strip_sql_comments",
]

READ_ONLY_PREFIXES = (
    "select",
    "show",
    "set",
    "begin",
    "rollback",
    "commit",
    "with",
    "pragma",
)
MUTATING_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "alter",
    "drop",
    "truncate",
    "create",
    "grant",
)

# Functions that write even when invoked from a SELECT.
MUTATING_FUNCTIONS = (
    "nextval(",
    "setval(",
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
    "lo_import",
    "lo_unlink",
    "lo_export",
    "dblink_exec",
    "pg_terminate_backend",
)

# Statement heads that may write even though they are not classic DML.
WRITE_CAPABLE_HEADS = (
    "call",
    "do",
    "copy",
    "merge",
    "vacuum",
    "analyze",
    "refresh",
    "reindex",
    "lock",
)


def strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line and ``/* */`` block comments conservatively."""
    output: list[str] = []
    index = 0
    while index < len(sql):
        if sql.startswith("--", index):
            index = sql.find("\n", index)
            if index == -1:
                break
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = len(sql) if end == -1 else end + 2
            continue
        output.append(sql[index])
        index += 1
    return "".join(output)


def split_statements(sql: str) -> list[str]:
    """Split on semicolons outside quotes so multi-statements are visible."""
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in sql:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            continue
        if char == ";":
            statements.append("".join(current))
            current = []
            continue
        current.append(char)
    statements.append("".join(current))
    return statements


def statement_write_problem(sql: str) -> str | None:
    """Return why a statement may write, or ``None`` when safely read-only.

    Detection is conservative: an unrecognised statement head is refused, so
    a new write syntax cannot pass merely because it is not a known keyword.
    """
    for raw in split_statements(sql):
        statement = strip_sql_comments(raw).strip()
        if not statement:
            continue
        lowered = " ".join(statement.split()).lower()
        head = lowered.split(None, 1)[0].rstrip("(")
        if head in MUTATING_KEYWORDS:
            return f"refused a mutating statement: {head}"
        if head in WRITE_CAPABLE_HEADS:
            return f"refused a write-capable statement: {head}"
        if head == "with":
            for word in ("insert", "update", "delete", "merge"):
                if re.search(rf"\b{word}\b", lowered):
                    return f"refused a write-bearing WITH statement ({word})"
        if head == "select":
            for marker in MUTATING_FUNCTIONS:
                if marker in lowered:
                    return (
                        "refused a SELECT invoking a mutating function: "
                        f"{marker}"
                    )
        if head not in READ_ONLY_PREFIXES:
            return f"refused an unrecognised statement: {head}"
    return None
