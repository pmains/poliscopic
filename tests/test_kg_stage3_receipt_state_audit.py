"""Focused tests for the read-only Stage 3 receipt-state audit.

The audit is what tells an operator what the live receipt state actually is, so the
tests pin the accounting it reports, its refusal to audit a missing table, and the
single-pass outcome tally that a quadratic implementation would make unusable on the
authoritative plan's ~65k selected rows.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from scripts.kg import stage3_receipt_state_audit as audit
from scripts.kg.stage2_artifacts import write_immutable


def _plan(path, rows):
    body = {"kind": "kg-stage3-processing-dry-plan",
            "version": "kg-stage3-processing-dry-plan/1.0",
            "selected": rows}
    write_immutable(path, body)
    return path


def _rows(counts):
    rows = []
    source_id = 1
    for outcome, number in counts.items():
        for _ in range(number):
            rows.append({"processing_identity": ["supporting_document", str(source_id),
                                                 "a" * 64, "pymupdf", "sweep_docs",
                                                 "sweep_docs/1.0"],
                         "outcome": outcome})
            source_id += 1
    return rows


def test_plan_identity_accounting_and_eligible_set(tmp_path):
    path = _plan(tmp_path / "plan.json", _rows({"planned": 3, "held": 2, "replay": 1}))
    result = audit._plan_identities(path)
    assert result["selected"] == 6
    assert result["planned"] == 3
    assert result["outcomes"] == {"held": 2, "planned": 3, "replay": 1}
    assert len(result["eligible_identities"]) == 3
    assert result["plan_digest"]


def test_plan_identity_accounting_is_single_pass_not_quadratic(tmp_path):
    """A quadratic tally over ~65k rows is unusable; keep this comfortably linear."""
    rows = _rows({"planned": 15_000, "held": 5_000})
    path = _plan(tmp_path / "large-plan.json", rows)
    started = time.perf_counter()
    result = audit._plan_identities(path)
    elapsed = time.perf_counter() - started
    assert result["outcomes"] == {"held": 5_000, "planned": 15_000}
    assert result["selected"] == 20_000
    assert elapsed < 3.0, f"outcome accounting looks quadratic: {elapsed:.2f}s for 20k rows"


def test_plan_without_selected_identities_refuses(tmp_path):
    path = _plan(tmp_path / "empty-plan.json", [])
    with pytest.raises(audit.AuditRefused, match="no selected identities"):
        audit._plan_identities(path)


class _Result:
    def __init__(self, value, mapping=False):
        self._value = value
        self._mapping = mapping

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def all(self):
        return [] if self._mapping else [self._value]


class _Connection:
    """Answers the first catalog question: is the receipt table there?"""

    def __init__(self):
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(" ".join(str(statement).split()).lower())
        return _Result(False)

    def rollback(self):
        pass


class _Engine:
    def __init__(self, connection):
        self._connection = connection

    def connect(self):
        return contextlib.nullcontext(self._connection)


def test_audit_refuses_when_the_receipt_table_is_absent(monkeypatch):
    monkeypatch.setattr(audit, "get_engine", lambda: _Engine(_Connection()))
    with pytest.raises(audit.AuditRefused, match="does not exist"):
        audit.audit(audit.DEFAULT_PLAN)


def test_audit_opens_a_read_only_transaction_before_any_catalog_read(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(audit, "get_engine", lambda: _Engine(connection))
    with pytest.raises(audit.AuditRefused):
        audit.audit(audit.DEFAULT_PLAN)
    assert connection.statements[0] == "set transaction read only"
