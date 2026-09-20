"""Pure semantic-role inference for deterministic document-layout tables.

Geometry proves that text occupies a cell; it does not prove what that cell
means.  This module assigns roles only from explicit table headers and leaves
unrecognised columns unknown.  In particular, result-looking words in an
unlabelled table never become result-bearing evidence.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


TABLE_ROLE_VERSION = "document-table-roles/1.0"

RESULT_RE = re.compile(
    r"\b(?:approved|denied|continued|tabled|adopted|received|discussed|"
    r"withdrawn|introduced|amended|sustained|vacated|extended|deferred|"
    r"heard|no\s+action)\b",
    re.IGNORECASE,
)

HEADER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("result", re.compile(r"\b(?:result|action|disposition|outcome|decision)\b", re.I)),
    ("item_number", re.compile(r"^(?:item|agenda\s*item|#|no\.?|number)$", re.I)),
    ("application", re.compile(r"\b(?:application|case|project)\b", re.I)),
    ("title", re.compile(r"\b(?:title|subject|matter|description)\b", re.I)),
    ("detail", re.compile(r"\b(?:detail|notes?|comment|conditions?)\b", re.I)),
)


def _cell_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def header_role(value: Any) -> str:
    """Return an explicit semantic role or ``unknown`` for one header cell."""
    text = _cell_text(value)
    if not text:
        return "unknown"
    for role, pattern in HEADER_RULES:
        if pattern.search(text):
            return role
    return "unknown"


def infer_table_roles(table: Mapping[str, Any]) -> dict[str, Any]:
    """Return role annotations without altering the detector's cell matrix.

    The first non-empty row is treated as a header only when at least one cell
    has an explicit known role.  Result evidence is emitted solely from a
    column explicitly headed as a result-bearing role.
    """
    rows = table.get("cells")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {"version": TABLE_ROLE_VERSION, "header_row": None,
                "column_roles": [], "result_cells": []}

    header_index = None
    roles: list[str] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        candidate = [header_role(cell) for cell in row]
        if any(role != "unknown" for role in candidate):
            header_index = row_index
            roles = candidate
            break

    result_cells: list[dict[str, Any]] = []
    if header_index is not None:
        for row_index, row in enumerate(rows[header_index + 1 :], header_index + 1):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            for column_index, role in enumerate(roles):
                if role != "result" or column_index >= len(row):
                    continue
                text = _cell_text(row[column_index])
                if text and RESULT_RE.search(text):
                    result_cells.append({
                        "row": row_index,
                        "column": column_index,
                        "role": "result",
                        "text": text,
                    })

    return {
        "version": TABLE_ROLE_VERSION,
        "header_row": header_index,
        "column_roles": roles,
        "result_cells": result_cells,
    }


def annotate_tables(tables: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy detector tables and attach their fail-closed semantic roles."""
    annotated = []
    for table in tables:
        copy = dict(table)
        copy["semantic_roles"] = infer_table_roles(table)
        annotated.append(copy)
    return annotated
