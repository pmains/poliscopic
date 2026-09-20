"""Regression coverage for public-schema metadata capture with an FDW dev twin."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.kg import stage2_subitem_schema_plan as plan  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class _Result:
    def __init__(self, rows=(), scalar=0):
        self.rows = list(rows)
        self.scalar_value = scalar

    def mappings(self):
        return _Rows(self.rows)

    def scalars(self):
        return _Rows(self.rows)

    def scalar(self):
        return self.scalar_value

    def __iter__(self):
        return iter(self.rows)


class _PublicAndDevFixture:
    """Same table name and tied ordinals, as production's FDW mirror exposes."""

    def __init__(self):
        self.queries = []
        self.public_columns = [
            {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
            {"column_name": "body", "data_type": "text", "is_nullable": "NO", "column_default": None},
        ]
        self.dev_columns = [
            {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
            {"column_name": "legacy_body", "data_type": "text", "is_nullable": "YES", "column_default": None},
        ]

    def execute(self, statement, _params=None):
        sql = str(statement)
        self.queries.append(sql)
        if "information_schema.columns" in sql:
            # The public and dev copies intentionally tie on ordinal positions;
            # a missing schema predicate would return all four rows.
            rows = self.public_columns if "table_schema = current_schema()" in sql else self.public_columns + self.dev_columns
            return _Result(rows)
        if "pg_constraint" in sql or "pg_indexes" in sql:
            return _Result([])
        if "COUNT(*)" in sql:
            return _Result(scalar=0)
        raise AssertionError(sql)


def test_schema_signature_excludes_same_named_dev_table_and_orders_ties():
    connection = _PublicAndDevFixture()
    signature = plan.schema_signature(connection)

    assert [column["column_name"] for column in signature["columns"]] == ["id", "body"]
    columns_sql = next(query for query in connection.queries if "information_schema.columns" in query)
    assert "table_schema = current_schema()" in columns_sql
    assert "ORDER BY ordinal_position, column_name" in columns_sql
