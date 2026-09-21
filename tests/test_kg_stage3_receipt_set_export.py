"""Focused tests for the canonical processing-receipt set export.

The exporter is the one step that turns stored proof into a bindable artifact, so the
tests pin the properties that make it trustworthy: the bodies come from the store and
are never reconstructed, the read order is total and deterministic, and an invalid,
duplicated, or empty population refuses rather than exporting something weaker than
proof.
"""

from __future__ import annotations

import contextlib

import pytest

from scripts.kg import stage3_processing_plan_inputs as plan_inputs
from scripts.kg import stage3_processing_receipt_set_export as export
from scripts.kg.stage2_artifacts import write_immutable
from tests._kg_stage3_processing_fixtures import TARGET, receipt_for, row


class _Result:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _Connection:
    def __init__(self, values, seen):
        self._values = values
        self._seen = seen

    def execute(self, statement):
        self._seen.append(str(statement))
        return _Result(self._values)


class _Engine:
    """Minimal engine stand-in: the exporter only needs connect() and a scalar stream."""

    def __init__(self, values):
        self._values = values
        self.seen: list[str] = []

    def connect(self):
        return contextlib.nullcontext(_Connection(self._values, self.seen))


def _body(source_id: int):
    return receipt_for(row(id=source_id, text_content=f"retained body {source_id}"))


def test_stored_bodies_reads_the_receipt_body_column_in_a_total_order():
    bodies = [_body(1), _body(2)]
    engine = _Engine(bodies)
    assert export.stored_bodies(engine) == bodies
    statement = engine.seen[0].lower()
    assert "select receipt_body from public.processing_receipts" in statement
    for column in export.ORDER_BY:
        assert column in statement, f"read order does not cover {column}"


def test_exported_bodies_are_the_stored_ones_not_reconstructed_substitutes():
    bodies = [_body(7)]
    artifact = export.build_receipt_set(bodies, target=TARGET, created_at="2026-09-21T00:00:00Z")
    assert artifact["receipts"] == bodies
    assert artifact["receipts"][0]["digest"] == bodies[0]["digest"]


def test_the_artifact_loads_as_a_canonical_receipt_set(tmp_path):
    artifact = export.build_receipt_set([_body(1), _body(2)], target=TARGET,
                                        created_at="2026-09-21T00:00:00Z")
    path = tmp_path / "receipt-set.json"
    digest = write_immutable(path, artifact)
    receipts, binding = plan_inputs.load_receipt_set(path)
    assert len(receipts) == 2
    assert binding["digest"] == digest
    assert binding["count"] == 2
    assert artifact["kind"] == plan_inputs.RECEIPT_SET_KIND


def test_count_matches_the_receipts_it_ships():
    artifact = export.build_receipt_set([_body(1), _body(2), _body(3)], target=TARGET,
                                        created_at="2026-09-21T00:00:00Z")
    assert artifact["count"] == len(artifact["receipts"]) == 3


def test_duplicate_identity_refuses():
    body = _body(4)
    with pytest.raises(export.ExportRefused, match="duplicate receipt identity"):
        export.build_receipt_set([body, dict(body)], target=TARGET,
                                 created_at="2026-09-21T00:00:00Z")


def test_invalid_body_refuses():
    broken = _body(5)
    broken["status"] = "not-a-registered-status"
    with pytest.raises(export.ExportRefused, match="is invalid"):
        export.build_receipt_set([broken], target=TARGET, created_at="2026-09-21T00:00:00Z")


def test_empty_population_refuses():
    with pytest.raises(export.ExportRefused, match="refusing an empty export"):
        export.build_receipt_set([], target=TARGET, created_at="2026-09-21T00:00:00Z")


def test_identity_fingerprint_is_stable_for_the_same_stored_state():
    bodies = [_body(1), _body(2), _body(3)]
    first = export.build_receipt_set(bodies, target=TARGET, created_at="2026-09-21T00:00:00Z")
    second = export.build_receipt_set(bodies, target=TARGET, created_at="2026-09-21T09:00:00Z")
    assert first["identity_sha256"] == second["identity_sha256"]
    assert len(first["identity_sha256"]) == 64


def test_artifact_declares_itself_read_only_and_unapplied():
    artifact = export.build_receipt_set([_body(1)], target=TARGET,
                                        created_at="2026-09-21T00:00:00Z")
    assert artifact["mode"] == "read-only"
    assert artifact["applied"] is False
    assert artifact["write_path"] == "absent by design"
    assert artifact["target"] == TARGET
