"""Packet-level invariants for the Stage 1 execution packet (v2).

Pure and offline: no database, no network.
"""

from __future__ import annotations

import pytest

from scripts.entities.event_normalize_artifacts import ArtifactCollision
from scripts.kg import stage1_apply_runner as runner
from scripts.kg import stage1_execution_packet as packet_module
from scripts.kg import stage1_packet_components as components
from scripts.kg import stage1_runner_checks as checks


# -- packet-level invariants (ported from the superseded v1 suite) -------------

def _renderable_packet():
    return {
        "packet_version": packet_module.PACKET_VERSION,
        "generated_at": "20260911T000000Z",
        "applied": False,
        "mutations_performed": 0,
        "packet_digest": "deadbeef",
        "counts": {"meeting_updates": 55, "public_body_inserts": 3,
                   "quarantine_updates": 18},
        "populations": {"repair_count": 374, "quarantine_count": 18, "disjoint": True,
                        "complete": True},
        "state_derivation": {"derivation": [
            {"count": "public_bodies_total", "baseline": 0, "delta": 3, "expected": 3}]},
        "quarantine": {"values": packet_module.quarantine_values()},
        "verification": {"ready_for_human_adjudication": True, "ready_to_apply": False},
    }


def test_component_order_is_dependency_ordered():
    verified = components.verify_components()
    assert verified[0]["id"] == "schema.quarantine_columns"
    assert verified[-1]["id"] == "data.quarantine_skip_18"
    assert verified[-1]["depends_on"] == ("schema.quarantine_columns",)


def test_schema_component_binds_the_authoritative_columns():
    from scripts.db import quarantine_schema

    assert set(components.QUARANTINE_ATTRIBUTES) == set(quarantine_schema.COLUMN_DDL)
    assert len(quarantine_schema.COLUMN_DDL) == 5
    assert components.HUMAN_REQUIRED_FIELDS == ("quarantined_by", "decision_id",
                                                "quarantined_at")


def test_every_baseline_query_is_a_select():
    for name, query in packet_module._BASELINE_COUNTS.items():
        assert query.lstrip().upper().startswith("SELECT"), name


def test_packet_executes_only_select_statements():
    """The assembler must never write; only SELECT literals may be executed."""
    import ast
    import pathlib

    source = pathlib.Path(packet_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = [
        argument.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "text"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    assert literals, "expected the packet module to issue literal SQL"
    assert all(literal.lstrip().upper().startswith("SELECT") for literal in literals)
    assert ".begin()" not in source


def test_row_fingerprint_is_deterministic_and_sensitive():
    row = {"id": 31757, "meeting_event_id": 11650, "supporting_doc_id": 112947,
           "extractor": "pattern", "extractor_version": "2026-07-27.1",
           "action_verb": "Approved", "confidence": 0.9, "text_offset_start": 751,
           "text_offset_end": 759, "case_number": None, "created_at": "x",
           "raw_text": "Approved"}
    baseline = components.canonical_row_fingerprint(row)
    assert baseline == components.canonical_row_fingerprint(dict(row))
    assert baseline != components.canonical_row_fingerprint({**row, "action_verb": "Denied"})
    assert baseline != components.canonical_row_fingerprint({**row, "raw_text": "Denied"})


def test_postgresql_requires_one_transaction_and_other_dialects_fail_closed():
    decision = packet_module._atomicity("postgresql")
    assert decision["transactional_ddl"] is True
    assert decision["single_transaction_required"] is True
    assert decision["staged_rollback"]
    assert checks.atomicity_refusal("postgresql") == []
    assert checks.atomicity_refusal("sqlite")


def test_packet_artifacts_are_never_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr(components, "DATA_DIR", tmp_path)
    fake = _renderable_packet()
    packet_module.write_packet(fake)
    with pytest.raises(ArtifactCollision):
        packet_module.write_packet(fake)


def test_markdown_labels_the_packet_unapplied():
    rendered = packet_module.render_markdown(_renderable_packet())
    assert "UNAPPLIED" in rendered
    assert "deadbeef" in rendered
    assert "scraper_sentinel_non_meeting" in rendered


def test_packet_version_is_pinned():
    assert packet_module.PACKET_VERSION == "kg-stage1-execution-packet/2.0"
    assert runner.PACKET_VERSION == packet_module.PACKET_VERSION
