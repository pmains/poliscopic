"""Tests for the sentinel-quarantine semantic invariant.

The invariant replaces a hardcoded "exactly 18 rows" expectation that went stale once
the approved dedup legitimately retired duplicate sentinel rows.  These tests cover
the pre-dedup and post-dedup shapes, and each fail-closed condition.

No database is touched: a throwaway in-memory SQLite engine stands in, and the
population reader is stubbed so the leakage comparison can be exercised directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from scripts.entities import event_normalize_preflight
from scripts.entities.event_normalize_remediation import (
    ADJUDICATED_SENTINEL_IDS,
    SENTINEL_DECISION_ID,
    SENTINEL_DOCUMENT_ID,
    SENTINEL_REASON,
    committed_dedup_evidence,
    sentinel_invariant,
)
from scripts.kg.stage1_adjudication import ADJUDICATION

EARLIER = tuple(i for i in ADJUDICATED_SENTINEL_IDS if i < 40000)   # 31757-31765
LATER = tuple(i for i in ADJUDICATED_SENTINEL_IDS if i >= 40000)     # 51301-51309
ADJUDICATOR = str(ADJUDICATION["adjudicator"])

_SCHEMA = (
    "CREATE TABLE meeting_event_extractions (id INTEGER PRIMARY KEY, "
    "quarantined_at TEXT, quarantine_reason TEXT, quarantined_by TEXT, "
    "decision_id TEXT, model_version TEXT, supporting_doc_id INTEGER)",
)
_QUARANTINE_COLUMNS = ("quarantine_reason", "quarantined_at", "quarantined_by",
                       "decision_id", "model_version")


def row(extraction_id: int, *, quarantined: bool = True, reason: str = SENTINEL_REASON,
        adjudicator: str = ADJUDICATOR, decision: str = SENTINEL_DECISION_ID,
        model: str = "kg-model/1.0", doc: int = SENTINEL_DOCUMENT_ID) -> dict:
    return {
        "id": extraction_id,
        "quarantined_at": "2026-09-11T19:57:43Z" if quarantined else None,
        "quarantine_reason": reason,
        "quarantined_by": adjudicator,
        "decision_id": decision,
        "model_version": model,
        "supporting_doc_id": doc,
    }


def build_engine(rows: list[dict]):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
        for values in rows:
            columns = ", ".join(values)
            marks = ", ".join(f":{k}" for k in values)
            connection.execute(text(
                f"INSERT INTO meeting_event_extractions ({columns}) VALUES ({marks})"),
                values)
    return engine


def stub_population(monkeypatch, examined: int) -> None:
    monkeypatch.setattr(event_normalize_preflight, "collect_population",
                        lambda engine, *, page_size, force=True: {"examined": examined})


def write_evidence(base: Path, retired: tuple[int, ...]) -> None:
    """A committed apply receipt plus the rollback artifact for its own plan."""
    plan = base / "kg-stage1-joint-dedup-plan-20990101T000000Z.json"
    plan.write_text(json.dumps({"plan_digest": "deadbeef"}))
    (base / "kg-stage1-joint-dedup-rollback-20990101T000000Z.json").write_text(
        json.dumps({"extractions": [{"id": i} for i in retired]}))
    (base / "kg-stage1-apply-receipt-20990101T000000Z.json").write_text(json.dumps({
        "kind": "kg-stage1-apply-receipt", "status": "applied",
        "plan_path": str(plan),
    }))


def non_quarantined_count(rows: list[dict]) -> int:
    return sum(1 for r in rows if r["quarantined_at"] is None)


def ordinary(n: int, start: int = 900000) -> list[dict]:
    """Ordinary non-quarantined extractions, so the eligible population is real."""
    return [{"id": start + i, "quarantined_at": None, "quarantine_reason": None,
             "quarantined_by": None, "decision_id": None, "model_version": None,
             "supporting_doc_id": 1} for i in range(n)]


# -- pre-dedup: 18 survivors --------------------------------------------

def test_pre_dedup_eighteen_survivors_satisfy_the_invariant(tmp_path, monkeypatch):
    rows = [row(i) for i in sorted(ADJUDICATED_SENTINEL_IDS)] + ordinary(40)
    engine = build_engine(rows)
    stub_population(monkeypatch, non_quarantined_count(rows))
    result = sentinel_invariant(engine, base=tmp_path)
    assert result["adjudicated_count"] == 18
    assert result["retired_count"] == 0
    assert len(result["expected_survivors"]) == 18
    assert result["observed_survivors"] == 18
    assert result["blockers"] == []
    assert result["dedup_evidence_applied"] is False


# -- post-dedup: 9 survivors + 9 retired --------------------------------

def test_post_dedup_nine_survivors_with_retired_evidence_satisfy_the_invariant(
        tmp_path, monkeypatch):
    rows = [row(i) for i in LATER] + ordinary(40)
    engine = build_engine(rows)
    write_evidence(tmp_path, EARLIER)
    stub_population(monkeypatch, non_quarantined_count(rows))
    result = sentinel_invariant(engine, base=tmp_path)
    assert result["retired_count"] == 9
    assert sorted(result["retired_ids"]) == sorted(EARLIER)
    assert sorted(result["expected_survivors"]) == sorted(LATER)
    assert result["observed_survivors"] == 9
    assert result["blockers"] == []
    assert result["dedup_evidence_applied"] is True
    assert len(result["dedup_evidence_sources"]) == 2


def test_retired_duplicates_are_not_counted_as_missing(tmp_path, monkeypatch):
    """The exact regression: 9 retired must not read as 9 missing."""
    rows = [row(i) for i in LATER] + ordinary(40)
    engine = build_engine(rows)
    write_evidence(tmp_path, EARLIER)
    stub_population(monkeypatch, non_quarantined_count(rows))
    result = sentinel_invariant(engine, base=tmp_path)
    assert not any("absent" in b for b in result["blockers"])


# -- fail-closed conditions ---------------------------------------------

def test_missing_survivor_blocks(tmp_path, monkeypatch):
    rows = [row(i) for i in LATER[:-1]]           # one expected survivor absent
    engine = build_engine(rows)
    write_evidence(tmp_path, EARLIER)
    stub_population(monkeypatch, len(rows))
    result = sentinel_invariant(engine, base=tmp_path)
    assert any("absent" in b for b in result["blockers"])
    assert LATER[-1] in result["expected_survivors"]


def test_unquarantined_survivor_blocks(tmp_path, monkeypatch):
    rows = [row(i) for i in LATER[:-1]] + [row(LATER[-1], quarantined=False)]
    engine = build_engine(rows)
    write_evidence(tmp_path, EARLIER)
    stub_population(monkeypatch, non_quarantined_count(rows))
    result = sentinel_invariant(engine, base=tmp_path)
    assert any("not quarantined" in b for b in result["blockers"])


@pytest.mark.parametrize("field,value,needle", [
    ("reason", "some_other_reason", "wrong reason"),
    ("adjudicator", "Someone Else", "wrong adjudicator"),
    ("decision", "kg-stage1-other", "wrong decision"),
    ("model", "", "lack a model version"),
    ("doc", 999999, "document lineage"),
])
def test_wrong_metadata_blocks(tmp_path, monkeypatch, field, value, needle):
    rows = [row(i) for i in LATER]
    rows[0] = row(LATER[0], **{field: value})
    engine = build_engine(rows)
    write_evidence(tmp_path, EARLIER)
    stub_population(monkeypatch, non_quarantined_count(rows))
    result = sentinel_invariant(engine, base=tmp_path)
    assert any(needle in b for b in result["blockers"]), result["blockers"]


def test_unexpected_extra_sentinel_row_blocks(tmp_path, monkeypatch):
    rows = [row(i) for i in LATER] + [row(999999)]
    engine = build_engine(rows)
    write_evidence(tmp_path, EARLIER)
    stub_population(monkeypatch, non_quarantined_count(rows))
    result = sentinel_invariant(engine, base=tmp_path)
    assert result["unexpected_extras"] == [999999]
    assert any("unexpected extra" in b for b in result["blockers"])


def test_quarantine_leakage_blocks(tmp_path, monkeypatch):
    """Eligible population disagreeing with the non-quarantined set is leakage."""
    rows = [row(i) for i in LATER]
    engine = build_engine(rows)
    write_evidence(tmp_path, EARLIER)
    stub_population(monkeypatch, len(rows) + 5)     # five quarantined rows leaked in
    result = sentinel_invariant(engine, base=tmp_path)
    assert any("leakage" in b for b in result["blockers"])


def test_incomplete_dedup_evidence_blocks(tmp_path, monkeypatch):
    plan = tmp_path / "kg-stage1-joint-dedup-plan-20990101T000000Z.json"
    plan.write_text("{}")
    (tmp_path / "kg-stage1-apply-receipt-20990101T000000Z.json").write_text(json.dumps({
        "status": "applied", "plan_path": str(plan)}))     # rollback artifact missing
    rows = [row(i) for i in ADJUDICATED_SENTINEL_IDS] + ordinary(40)
    engine = build_engine(rows)
    stub_population(monkeypatch, non_quarantined_count(rows))
    result = sentinel_invariant(engine, base=tmp_path)
    assert any("dedup evidence incomplete" in b for b in result["blockers"])


# -- evidence reader -----------------------------------------------------

def test_evidence_ignores_non_applied_receipts(tmp_path):
    (tmp_path / "kg-stage1-apply-receipt-20990101T000000Z.json").write_text(
        json.dumps({"status": "aborted_rolled_back"}))
    evidence = committed_dedup_evidence(tmp_path)
    assert evidence["applied"] is False
    assert evidence["complete"] is True


def test_evidence_without_any_receipt_is_applied_false(tmp_path):
    evidence = committed_dedup_evidence(tmp_path)
    assert evidence["applied"] is False
    assert evidence["retired_extraction_ids"] == frozenset()


def test_invariant_fingerprint_changes_when_facts_change(tmp_path, monkeypatch):
    write_evidence(tmp_path, EARLIER)
    rows_a = [row(i) for i in LATER] + ordinary(40)
    engine_a = build_engine(rows_a)
    stub_population(monkeypatch, non_quarantined_count(rows_a))
    first = sentinel_invariant(engine_a, base=tmp_path)["fingerprint"]
    rows_b = [row(i, model="kg-model/2.0") for i in LATER] + ordinary(40)
    engine_b = build_engine(rows_b)
    second = sentinel_invariant(engine_b, base=tmp_path)["fingerprint"]
    assert first != second
