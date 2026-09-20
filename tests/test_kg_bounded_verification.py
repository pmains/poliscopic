"""Isolated contract tests for the Stage 0 bounded verification gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

import pytest
from sqlalchemy import create_engine, text

from scripts.entities import bounded_verification
from scripts.entities import sweep_docs
from scripts.entities.bounded_verification import BoundedPhaseAdapter


class _FakeAdapter(BoundedPhaseAdapter):
    """Inject a deterministic producer without importing a real phase."""

    def __init__(
        self,
        name: str,
        producer: Callable[..., dict[str, Any]],
        count_paths: Sequence[tuple[str, ...]] = (),
    ) -> None:
        super().__init__(name, "unused", "unused", tuple(count_paths))
        object.__setattr__(self, "_producer", producer)

    def load(self) -> Callable[..., dict[str, Any]]:
        return self._producer


@pytest.fixture
def stable_diagnostics(monkeypatch):
    counts = {table: 10 for table in bounded_verification.GATE_COUNT_TABLES}
    integrity = {"orphan_mentions": 0, "unresolved_relationship_provenance": 7}
    monkeypatch.setattr(bounded_verification, "_graph_counts", lambda engine: counts.copy())
    monkeypatch.setattr(
        bounded_verification, "_integrity_snapshot", lambda engine: integrity.copy()
    )
    monkeypatch.setattr(bounded_verification, "_unmapped_entity_types", lambda engine: [])
    monkeypatch.setattr(
        bounded_verification, "_schema_contract_violations", lambda engine: []
    )
    return counts, integrity


def test_bounded_gate_passes_limits_and_preserves_existing_debt(
    tmp_path: Path, stable_diagnostics
):
    calls = []

    def producer(engine, **kwargs):
        calls.append(kwargs)
        return {"success": True, "total_scanned": kwargs["limit"]}

    artifact = bounded_verification.run_bounded_verification(
        object(),
        limit=23,
        force=True,
        output_path=tmp_path / "result.json",
        phase_adapters={"sample": _FakeAdapter("sample", producer)},
    )

    assert artifact["passed"] is True
    assert calls == [{"dry_run": True, "force": True, "verbose": False, "limit": 23}]
    assert artifact["before"]["integrity"]["unresolved_relationship_provenance"] == 7
    assert artifact["deltas"]["integrity"]["unresolved_relationship_provenance"] == 0
    assert (tmp_path / "result.json").exists()


def test_bounded_gate_fails_when_dry_run_changes_graph_counts(
    monkeypatch, tmp_path: Path, stable_diagnostics
):
    before_counts, _ = stable_diagnostics
    snapshots = [before_counts.copy(), {**before_counts, "entities": 11}]
    monkeypatch.setattr(bounded_verification, "_graph_counts", lambda engine: snapshots.pop(0))

    artifact = bounded_verification.run_bounded_verification(
        object(),
        output_path=tmp_path / "result.json",
        phase_adapters={
            "sample": _FakeAdapter("sample", lambda engine, **kwargs: {"success": True})
        },
    )

    assert artifact["passed"] is False
    assert artifact["deltas"]["counts"]["entities"] == 1
    count_check = next(c for c in artifact["checks"] if c["check"] == "graph_counts_unchanged")
    assert count_check["ok"] is False


def test_bounded_gate_records_phase_failure(
    tmp_path: Path, stable_diagnostics
):
    artifact = bounded_verification.run_bounded_verification(
        object(),
        output_path=tmp_path / "result.json",
        phase_adapters={
            "sample": _FakeAdapter(
                "sample", lambda engine, **kwargs: {"success": False, "error": "bad input"}
            )
        },
    )

    assert artifact["passed"] is False
    assert artifact["phases"][0]["error"] == "bad input"


def test_bounded_gate_rejects_reported_count_above_limit(
    tmp_path: Path, stable_diagnostics
):
    artifact = bounded_verification.run_bounded_verification(
        object(),
        limit=10,
        output_path=tmp_path / "result.json",
        phase_adapters={
            "sample": _FakeAdapter(
                "sample",
                lambda engine, **kwargs: {"success": True, "rows_scanned": 11},
                (("rows_scanned",),),
            )
        },
    )

    assert artifact["passed"] is False
    assert artifact["phases"][0]["bound_honored"] is False
    bound_check = next(
        check for check in artifact["checks"]
        if check["check"] == "all_phase_bounds_honored"
    )
    assert bound_check["ok"] is False


def test_bounded_gate_rejects_nonpositive_limit(stable_diagnostics):
    with pytest.raises(ValueError, match="at least 1"):
        bounded_verification.run_bounded_verification(object(), limit=0)


def test_bounded_gate_rejects_phase_without_safe_adapter(stable_diagnostics):
    with pytest.raises(ValueError, match="lack a safe bounded adapter"):
        bounded_verification.run_bounded_verification(
            object(), phase_names=["resolver"], phase_adapters={}
        )


def test_sweep_docs_honors_limit_smaller_than_batch(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE entities (id INTEGER, normalized_name TEXT, entity_type TEXT)"
        ))
        connection.execute(text(
            "CREATE TABLE supporting_documents ("
            "id INTEGER, swept_at TEXT, text_content TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO supporting_documents (id, text_content) "
            "VALUES (1, 'one'), (2, 'two'), (3, 'three')"
        ))

    observed_batch_sizes = []

    def process_batch(connection, watermark, entity_cache, **kwargs):
        observed_batch_sizes.append(sweep_docs.BATCH_SIZE)
        return {
            "processed": sweep_docs.BATCH_SIZE,
            "matches": 0,
            "entities": 0,
            "mentions": 0,
            "max_id": watermark + sweep_docs.BATCH_SIZE,
            "done": False,
        }

    monkeypatch.setattr(sweep_docs, "process_batch", process_batch)
    result = sweep_docs.run_sweep_docs(
        engine, dry_run=True, limit=2, batch_size=200
    )

    assert observed_batch_sizes == [2]
    assert result["docs_processed"] == 2
