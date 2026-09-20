"""Isolated integration tests for the Stage 1 quarantine mechanism.

No database access beyond throwaway in-memory SQLite; no network.
"""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import create_engine, text

from scripts.db import quarantine_schema as qs
from scripts.entities.event_normalize_gate import evaluate_result, launch_decision
from scripts.entities.event_normalize_preflight import GateTablesMissing, quarantine_counts
from scripts.entities.event_normalize_query import build_page_query
from scripts.kg.quarantine import (
    QUARANTINE_COLUMNS,
    QUARANTINE_SELECTION_CLAUSE,
    QuarantineError,
    QuarantineState,
    is_quarantined_row,
    quarantine_reason_slugs,
    validate_quarantine_reason,
)

BASE = "id INTEGER PRIMARY KEY, action_verb TEXT"
FULL = ("id INTEGER PRIMARY KEY, quarantined_at TEXT, quarantine_reason TEXT, "
        "quarantined_by TEXT, decision_id TEXT, model_version TEXT")
FP = {"code_evidence_complete": True, "modules": {"a": "b"}, "code_sha256": "s"}


def engine(ddl=BASE):
    e = create_engine("sqlite://")
    with e.begin() as c:
        c.execute(text(f"CREATE TABLE meeting_event_extractions ({ddl})"))
    return e


# -- schema: upgrade / downgrade / idempotency --------------------------------


def test_upgrade_adds_columns_and_is_idempotent():
    e = engine()
    with e.begin() as c:
        first = qs.upgrade(c, "sqlite")
        assert qs.is_applied(c, "sqlite") is True
        assert qs.upgrade(c, "sqlite") == []
    assert len(first) == len(QUARANTINE_COLUMNS)


def test_downgrade_removes_columns_and_is_idempotent():
    e = engine()
    with e.begin() as c:
        qs.upgrade(c, "sqlite")
        removed = qs.downgrade(c, "sqlite")
        assert qs.is_applied(c, "sqlite") is False
        assert qs.downgrade(c, "sqlite") == []
    assert len(removed) == len(QUARANTINE_COLUMNS)


def test_review_statements_cover_every_column():
    for dialect in ("postgresql", "sqlite"):
        stmts = qs.statements_for_review(dialect)
        assert len(stmts["up"]) >= len(QUARANTINE_COLUMNS)
        assert len(stmts["down"]) == len(QUARANTINE_COLUMNS)
        assert "model_version" in stmts["columns"]


# -- controlled reason registry ----------------------------------------------


@pytest.mark.parametrize("reason", list(quarantine_reason_slugs()))
def test_registry_reasons_are_accepted(reason):
    assert validate_quarantine_reason(reason) == reason


@pytest.mark.parametrize("reason", ["", "   ", None, "made_up", "legacy_prohibited"])
def test_arbitrary_reasons_are_refused(reason):
    with pytest.raises(QuarantineError):
        validate_quarantine_reason(reason)


# -- quarantine state semantics ----------------------------------------------


def test_model_version_is_required():
    with pytest.raises(QuarantineError):
        QuarantineState(reason="unregistered_value")


def test_human_provenance_must_be_paired():
    with pytest.raises(QuarantineError):
        QuarantineState(reason="unregistered_value", model_version="kg-model/1.0", by="pete")


def test_human_quarantine_records_full_provenance():
    state = QuarantineState(reason="unregistered_value", model_version="kg-model/1.0",
                            by="pete", decision_id="d-1")
    values = state.values()
    assert state.is_human_decision is True
    assert values["model_version"] == "kg-model/1.0"
    assert values["decision_id"] == "d-1"
    assert values["quarantined_at"] is not None


def test_mechanical_quarantine_needs_no_human_provenance():
    state = QuarantineState(reason="unregistered_value", model_version="kg-model/1.0")
    assert state.is_human_decision is False
    assert state.values()["quarantined_by"] is None
    assert state.assertion_class == "quarantined"


# -- selection: quarantine excluded in BOTH modes -----------------------------


def test_normal_mode_excludes_quarantined():
    sql = build_page_query(force=False, has_cursor=False)
    assert QUARANTINE_SELECTION_CLAUSE in sql
    assert "e.meeting_event_id IS NULL" in sql


def test_force_mode_also_excludes_quarantined():
    sql = build_page_query(force=True, has_cursor=False)
    assert QUARANTINE_SELECTION_CLAUSE in sql
    assert "meeting_event_id IS NULL" not in sql


def test_cursor_combines_with_quarantine_clause():
    sql = build_page_query(force=True, has_cursor=True)
    assert QUARANTINE_SELECTION_CLAUSE in sql
    assert "e.id > :after_extraction_id" in sql


# -- explicit counting --------------------------------------------------------


def test_quarantine_counts_are_explicit():
    e = engine(FULL)
    with e.begin() as c:
        c.execute(text("INSERT INTO meeting_event_extractions (id, quarantined_at) VALUES (1,'2026-01-01')"))
        c.execute(text("INSERT INTO meeting_event_extractions (id, quarantined_at) VALUES (2,NULL)"))
        c.execute(text("INSERT INTO meeting_event_extractions (id, quarantined_at) VALUES (3,NULL)"))
    assert quarantine_counts(e) == {"extractions_total": 3, "quarantined_excluded": 1}


def test_missing_quarantine_column_is_fatal():
    with pytest.raises(GateTablesMissing):
        quarantine_counts(engine())


# -- exact accounting in the gate evaluator ----------------------------------


def envelope(n):
    stats = {
        "extractions_examined": n, "normalizable": n, "events_planned": 0,
        "extraction_links_planned": 0, "events_replay_noop": n,
        "extraction_links_replay_noop": n, "assertions_inconsistent": 0,
        "events_inserted": 0, "extraction_links_updated": 0, "rows_committed": 0,
        "read_failures": 0, "errors": 0, "rows_rolled_back": 0, "skipped": 0,
        "skipped_unmapped_type": 0, "accounting_mode": "force",
        "classification_reconciles": True,
        "validation_receipt": {"state": "sealed", "failure": None, "dry_run": True,
                               "values": {"reconciles": True},
                               "rows": {"reconciles": True,
                                        "classification_reconciles": True, "committed": 0}},
    }
    return {"step": "normalize", "success": True, "stats": stats}


def preflight(**over):
    record = {"target": {"tier": "development", "redacted": "dev"},
              "eligible_work_items": 5, "gate_tables": {"meetings": 1},
              "integrity": {"orphans": 0}, "extractions_total": 5,
              "quarantined_excluded": 0, "fingerprint": dict(FP)}
    record.update(over)
    return record


def postflight(record):
    return {"target": copy.deepcopy(record["target"]),
            "gate_tables": copy.deepcopy(record["gate_tables"]),
            "integrity": copy.deepcopy(record["integrity"]),
            "extractions_total": record["extractions_total"],
            "quarantined_excluded": record["quarantined_excluded"]}


def test_reconciled_quarantine_accounting_passes():
    pre = preflight(eligible_work_items=4, extractions_total=5, quarantined_excluded=1)
    names = evaluate_result(envelope(4), pre, postflight(pre), fingerprint_after=FP).by_name()
    assert names["quarantine_counts_present"].ok is True
    assert names["quarantine_reconciles"].ok is True
    assert names["quarantine_counts_unchanged"].ok is True


def test_unreconciled_quarantine_accounting_fails():
    pre = preflight(eligible_work_items=4, extractions_total=5, quarantined_excluded=0)
    names = evaluate_result(envelope(4), pre, postflight(pre), fingerprint_after=FP).by_name()
    assert names["quarantine_reconciles"].ok is False


def test_missing_quarantine_counts_fail():
    pre = preflight()
    post = postflight(pre)
    pre.pop("quarantined_excluded")
    post.pop("quarantined_excluded")
    names = evaluate_result(envelope(5), pre, post, fingerprint_after=FP).by_name()
    assert names["quarantine_counts_present"].ok is False


def test_quarantine_drift_between_snapshots_fails():
    pre = preflight()
    post = postflight(pre)
    post["quarantined_excluded"] = 1
    names = evaluate_result(envelope(5), pre, post, fingerprint_after=FP).by_name()
    assert names["quarantine_counts_unchanged"].ok is False


def test_launch_requires_reconciled_quarantine():
    ok, reasons = launch_decision({
        "target_is_development": True, "failures_by_reason": {}, "coverage_complete": True,
        "fingerprint": {"code_evidence_complete": True, "modules": {"a": "b"}},
        "quarantine_reconciles": False,
    })
    assert ok is False
    assert any("quarantine" in r for r in reasons)


# -- ordinary rows unaffected -------------------------------------------------


def test_ordinary_rows_are_unaffected():
    assert is_quarantined_row({"id": 1, "quarantined_at": None, "quarantine_reason": None}) is False
    assert is_quarantined_row({"id": 2}) is False
    assert is_quarantined_row({"id": 3, "quarantined_at": "2026-01-01"}) is True
    assert is_quarantined_row({"id": 4, "quarantine_reason": "unregistered_value"}) is True


def test_the_behaviour_carrying_module_is_fingerprinted():
    """The manifest is scoped to scripts/entities/*; the module whose hash
    changes with the quarantine wiring is event_normalize_query."""
    from scripts.entities import detect_entities as detector
    phase = next(p for p in detector.PHASES if p["name"] == "event_pipeline")
    assert "scripts.entities.event_normalize_query" in phase["code_modules"]
