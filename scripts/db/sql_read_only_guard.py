#!/usr/bin/env python3
"""``sql_read_only_guard.py`` — conservative read-only statement detection.

Owned here so both the connection guard and the statement audit share one policy.
Detection is deliberately conservative: an unrecognised statement head is refused
rather than allowed, and ``WITH``/``CALL``/``DO``/``COPY``/multi-statement payloads
and SELECTs invoking known mutating functions are all rejected.
"""

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


READ_ONLY_PREFIXES = ("select", "show", "set", "begin", "rollback", "commit", "with", "pragma")
MUTATING_KEYWORDS = ("insert", "update", "delete", "alter", "drop", "truncate", "create", "grant")

#: Functions that write even when invoked from a SELECT.
MUTATING_FUNCTIONS = (
    "nextval(", "setval(", "pg_advisory_lock", "pg_advisory_xact_lock",
    "lo_import", "lo_unlink", "lo_export", "dblink_exec", "pg_terminate_backend",
)

#: Statement heads that may write even though they are not classic DML.
WRITE_CAPABLE_HEADS = (
    "call", "do", "copy", "merge", "vacuum", "analyze", "refresh", "reindex", "lock",
)


def strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line and ``/* */`` block comments (conservatively)."""
    out, i, n = [], 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            i = sql.find("\n", i)
            if i == -1:
                break
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(sql[i]); i += 1
    return "".join(out)


def split_statements(sql: str) -> list[str]:
    """Split on ``;`` outside single/double quotes, so multi-statements are seen."""
    statements, current, quote = [], [], None
    for char in sql:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char; current.append(char); continue
        if char == ";":
            statements.append("".join(current)); current = []; continue
        current.append(char)
    statements.append("".join(current))
    return statements


def statement_write_problem(sql: str) -> str | None:
    """Return why a statement may write, or ``None`` when it is safely read-only.

    Conservative by construction: an unrecognised head is refused rather than
    allowed, so a new write syntax cannot slip through as "not a known keyword".
    """
    import re as _re

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
                if _re.search(rf"\b{word}\b", lowered):
                    return f"refused a write-bearing WITH statement ({word})"
        if head == "select":
            for marker in MUTATING_FUNCTIONS:
                if marker in lowered:
                    return f"refused a SELECT invoking a mutating function: {marker}"
        if head not in READ_ONLY_PREFIXES:
            return f"refused an unrecognised statement: {head}"
    return None
