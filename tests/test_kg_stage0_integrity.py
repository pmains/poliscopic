import json
import types

import pytest
from sqlalchemy import create_engine, text

import scripts.entities.detect_entities as detector
import scripts.entities.producer_manifest as producer_manifest

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
import scripts.entities.event_extractor as event_orchestrator
from scripts.entities.event_normalize import normalize
import scripts.sync.sync_digest as sync_digest
from scripts.entities.detect_entities import _integrity_snapshot
from scripts.entities.detect_entities import (
    _phase_accounting_checks,
    _phase_expected_output_check,
    _producer_metadata,
    _replay_zero_delta_checks,
    _replay_zero_report_checks,
)
from scripts.entities.event_extract import extract_events_from_text
from scripts.entities.schema_parity import contract_violations, schema_signature


def _engine():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE entities(id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE entity_mentions(id INTEGER PRIMARY KEY, entity_id INTEGER, source_type TEXT, source_id INTEGER, role_in_context TEXT, extracted_by TEXT)"))
        c.execute(text("CREATE TABLE entity_relationships(from_entity_id INTEGER, to_entity_id INTEGER, provenance_type TEXT, provenance_id INTEGER)"))
        c.execute(text("CREATE TABLE meeting_events(id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE meeting_event_extractions(meeting_event_id INTEGER, extractor TEXT, supporting_doc_id INTEGER, text_offset_start INTEGER, text_offset_end INTEGER, action_verb TEXT, extractor_version TEXT)"))
        c.execute(text("CREATE TABLE event_participants(meeting_event_id INTEGER, entity_id INTEGER)"))
        c.execute(text("CREATE TABLE agenda_items(id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE public_bodies(id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE meetings(id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE body_memberships(id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE meeting_members(id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE pz_item_details(id INTEGER PRIMARY KEY)"))
    return engine


def test_integrity_snapshot_detects_replay_excess_and_orphans():
    engine = _engine()
    with engine.begin() as c:
        c.execute(text("INSERT INTO entities VALUES (1)"))
        c.execute(text("INSERT INTO entity_mentions VALUES (1,1,'doc',7,'staff','graph_builder'),(2,1,'doc',7,'staff','graph_builder'),(3,99,'doc',8,'staff','regex')"))
        c.execute(text("INSERT INTO meeting_events VALUES (10)"))
        c.execute(text("INSERT INTO meeting_event_extractions VALUES (10,'pattern',5,1,9,'Approved','v1'),(10,'pattern',5,1,9,'Approved','v1')"))
        c.execute(text("INSERT INTO event_participants VALUES (999,1)"))
    snap = _integrity_snapshot(engine)
    assert snap["graph_builder_repeat_excess"] == 1
    assert snap["pattern_extraction_repeat_excess"] == 1
    assert snap["orphan_mentions"] == 1
    assert snap["orphan_participants"] == 1


def test_integrity_snapshot_detects_unresolved_and_unknown_provenance():
    engine = _engine()
    with engine.begin() as c:
        c.execute(text("INSERT INTO entities VALUES (1),(2)"))
        c.execute(text("INSERT INTO entity_relationships VALUES (1,2,'agenda_item',99),(1,2,'mystery',1)"))
    snap = _integrity_snapshot(engine)
    assert snap["unresolved_relationship_provenance"] == 1
    assert snap["unknown_relationship_provenance_type"] == 1


def _run_integrity_gate(monkeypatch, pre_integrity, post_integrity):
    """Run only the live integrity gate against supplied snapshots."""
    engine = _engine()
    snapshots = iter((pre_integrity, post_integrity))
    monkeypatch.setattr(detector, "PHASES", [])
    monkeypatch.setattr(detector, "_get_watermarks", lambda *_: set())
    monkeypatch.setattr(detector, "_integrity_snapshot", lambda _: next(snapshots))
    monkeypatch.setattr(detector, "_schema_contract_violations", lambda _: [])
    monkeypatch.setattr(detector, "_unmapped_entity_types", lambda _: [])
    monkeypatch.setattr(detector, "_write_run_state", lambda _: None)
    return detector.run_detection(engine)["gate"]


def test_unresolved_relationship_provenance_gate_passes_at_zero(monkeypatch):
    gate = _run_integrity_gate(
        monkeypatch,
        {"unresolved_relationship_provenance": 0,
         "graph_builder_repeat_excess": 2},
        {"unresolved_relationship_provenance": 0,
         "graph_builder_repeat_excess": 2},
    )

    assert gate["failed"] is False
    checks = {check["check"]: check for check in gate["checks"]}
    assert checks["unresolved_relationship_provenance"]["ok"] is True
    assert checks["graph_builder_repeat_excess"]["ok"] is True


def test_unresolved_relationship_provenance_gate_fails_when_stably_nonzero(
        monkeypatch):
    gate = _run_integrity_gate(
        monkeypatch,
        {"unresolved_relationship_provenance": 2},
        {"unresolved_relationship_provenance": 2},
    )

    check = next(check for check in gate["checks"]
                 if check["check"] == "unresolved_relationship_provenance")
    assert gate["failed"] is True
    assert check["ok"] is False
    assert "requires zero" in check["detail"]


def test_unresolved_relationship_provenance_gate_fails_when_increased(
        monkeypatch):
    gate = _run_integrity_gate(
        monkeypatch,
        {"unresolved_relationship_provenance": 1},
        {"unresolved_relationship_provenance": 2},
    )

    check = next(check for check in gate["checks"]
                 if check["check"] == "unresolved_relationship_provenance")
    assert gate["failed"] is True
    assert check["ok"] is False
    assert "delta +1" in check["detail"]


def test_event_extraction_keeps_separate_source_locations():
    events = extract_events_from_text(1, "APPROVED item one\nAPPROVED item two")
    assert len(events) == 2
    assert events[0]["text_offset_start"] != events[1]["text_offset_start"]


def test_schema_contract_reports_missing_graph_columns():
    engine = _engine()
    problems = contract_violations(schema_signature(engine))
    assert "missing column: entity_relationships.edge_kind" in problems
    assert "missing table: entity_types" in problems


def test_failed_phase_does_not_advance_watermark(monkeypatch):
    engine = _engine()
    with engine.begin() as c:
        c.execute(text("CREATE TABLE entity_types(id INTEGER PRIMARY KEY, entity_type TEXT)"))
        c.execute(text("CREATE TABLE _detect_entities_watermark(phase TEXT PRIMARY KEY)"))

    phase = {"name": "failure_probe", "description": "failure probe",
             "critical": True, "allow_skip": True}
    monkeypatch.setattr(detector, "PHASES", [phase])
    monkeypatch.setattr(detector, "_get_watermarks", lambda *_: set())
    monkeypatch.setattr(detector, "_run_phase", lambda *_: {
        "success": False, "duration_s": 0.0, "entities_created": 0,
        "edges_created": 0, "attempts": 2, "error": "injected failure",
    })
    monkeypatch.setattr(detector, "_integrity_snapshot", lambda _: {})
    monkeypatch.setattr(detector, "_schema_contract_violations", lambda _: [])
    monkeypatch.setattr(detector, "_unmapped_entity_types", lambda _: [])
    monkeypatch.setattr(detector, "_write_run_state", lambda _: None)

    result = detector.run_detection(engine)
    with engine.connect() as c:
        marked = c.execute(text(
            "SELECT COUNT(*) FROM _detect_entities_watermark WHERE phase='failure_probe'"
        )).scalar()
    assert marked == 0
    assert result["phases"][0]["status"] == "failed"


def test_phase_accounting_reconciles_reported_and_committed_counts():
    checks = _phase_accounting_checks(
        "sweep_docs",
        {"entities_created": 2, "mentions_created": 3},
        {"entities": 2, "entity_mentions": 2},
    )
    assert checks[0]["ok"] is True
    assert checks[1]["ok"] is False


def test_replay_zero_checks_reject_planned_or_reported_writes():
    checks = _replay_zero_report_checks("event_pipeline", {
        "accounting": {
            "extract": {"events_inserted": 0},
            "normalize": {
                "events_planned": 0,
                "events_inserted": 0,
                "extraction_links_updated": 0,
            },
            "link": {
                "participants_planned_insert": 2,
                "participants_planned_update": 0,
                "participants_inserted": 2,
                "participants_updated": 0,
                "participants_written": 2,
                "participants_mutated": 2,
            },
        },
    })

    by_name = {check["check"]: check for check in checks}
    assert by_name[
        "replay_zero:event_pipeline:reported:accounting.link.participants_planned_insert"
    ]["ok"] is False
    assert by_name[
        "replay_zero:event_pipeline:reported:accounting.link.participants_mutated"
    ]["ok"] is False
    assert by_name[
        "replay_zero:event_pipeline:reported:accounting.normalize.events_planned"
    ]["ok"] is True


def test_replay_zero_checks_fail_closed_when_a_counter_is_missing():
    checks = _replay_zero_report_checks("role_classifier", {})

    assert checks == [{
        "check": "replay_zero:role_classifier:reported:total_updated",
        "ok": False,
        "detail": "missing or invalid nonnegative integer count",
    }]


def test_replay_zero_delta_checks_reject_count_neutrality_violations():
    checks = _replay_zero_delta_checks(
        "pattern_cascade",
        {"entities": 0, "entity_mentions": 196, "entity_relationships": 10},
    )

    by_name = {check["check"]: check for check in checks}
    assert by_name["replay_zero:pattern_cascade:delta:entities"]["ok"] is True
    assert by_name[
        "replay_zero:pattern_cascade:delta:entity_mentions"
    ]["ok"] is False
    assert by_name[
        "replay_zero:pattern_cascade:delta:entity_relationships"
    ]["ok"] is False


@pytest.mark.parametrize("bad_count", [-1, 1.5, "2", True, None])
def test_phase_accounting_rejects_invalid_counts_without_raising(bad_count):
    checks = _phase_accounting_checks(
        "sweep_docs",
        {"entities_created": bad_count, "mentions_created": 0},
        {"entities": 0, "entity_mentions": 0},
    )
    assert checks[0]["ok"] is False
    assert "invalid nonnegative integer" in checks[0]["detail"]
    assert checks[1]["ok"] is True


def test_graph_builder_accounting_reconciles_all_insert_classes():
    checks = _phase_accounting_checks(
        "graph_builder",
        {"entities_attempted": 4, "entities_planned": 2,
         "entities_inserted": 2,
         "edges_attempted": 5, "edges_inserted": 3,
         "edge_replay_collisions": 1, "edges_unresolved_endpoint": 1,
         "mentions_attempted": 6, "mentions_inserted": 4,
         "mention_replay_collisions": 2, "mentions_unresolved_entity": 0},
        {"entities": 2, "entity_relationships": 3, "entity_mentions": 5},
    )
    by_name = {check["check"]: check for check in checks}
    assert by_name["accounting:graph_builder:edges_balance"]["ok"] is True
    assert by_name["accounting:graph_builder:mentions_balance"]["ok"] is True
    assert by_name["accounting:graph_builder:entities_balance"]["ok"] is True
    assert by_name["accounting:graph_builder:edges_resolved"]["ok"] is False
    assert by_name["accounting:graph_builder:mentions_resolved"]["ok"] is True
    assert by_name["accounting:graph_builder:entities_inserted"]["ok"] is True
    assert by_name["accounting:graph_builder:edges_inserted"]["ok"] is True
    assert by_name["accounting:graph_builder:mentions_inserted"]["ok"] is False


def test_graph_builder_accounting_requires_new_insert_counters():
    checks = _phase_accounting_checks(
        "graph_builder",
        {"entities_created": 2, "edges_created": 3},
        {"entities": 2, "entity_relationships": 3, "entity_mentions": 0},
    )
    assert all(check["ok"] is False for check in checks)
    missing_insert_checks = [check for check in checks
                             if check["check"].endswith("_inserted")]
    assert len(missing_insert_checks) == 3
    assert all("did not report required count" in check["detail"]
               for check in missing_insert_checks)


def test_graph_builder_accounting_rejects_unbalanced_or_invalid_outcomes():
    raw = {
        "entities_attempted": 0, "entities_planned": 0,
        "entities_inserted": 0,
        "edges_attempted": 3, "edges_inserted": 1,
        "edge_replay_collisions": 1, "edges_unresolved_endpoint": 0,
        "mentions_attempted": 1, "mentions_inserted": 0,
        "mention_replay_collisions": -1, "mentions_unresolved_entity": 2,
    }
    checks = _phase_accounting_checks(
        "graph_builder", raw,
        {"entities": 0, "entity_relationships": 1, "entity_mentions": 0},
    )
    by_name = {check["check"]: check for check in checks}
    assert by_name["accounting:graph_builder:edges_balance"]["ok"] is False
    assert by_name["accounting:graph_builder:mentions_balance"]["ok"] is False
    assert "mention_replay_collisions" in by_name[
        "accounting:graph_builder:mentions_balance"
    ]["detail"]


def test_graph_builder_accounting_rejects_impossible_entity_totals():
    raw = {
        "entities_attempted": 1, "entities_planned": 2,
        "entities_inserted": 2,
        "edges_attempted": 0, "edges_inserted": 0,
        "edge_replay_collisions": 0, "edges_unresolved_endpoint": 0,
        "mentions_attempted": 0, "mentions_inserted": 0,
        "mention_replay_collisions": 0, "mentions_unresolved_entity": 0,
    }
    checks = _phase_accounting_checks(
        "graph_builder", raw,
        {"entities": 2, "entity_relationships": 0, "entity_mentions": 0},
    )
    by_name = {check["check"]: check for check in checks}
    assert by_name["accounting:graph_builder:entities_balance"]["ok"] is False


def test_event_pipeline_accounting_reconciles_each_step_delta():
    checks = _phase_accounting_checks(
        "event_pipeline",
        {"accounting": {
            "extract": {"events_inserted": 4},
            "normalize": {"events_inserted": 3},
            "link": {
                "participant_attempts": 7,
                "participants_planned_insert": 7,
                "participants_planned_update": 0,
                "participants_inserted": 7,
                "participants_updated": 0,
                "participants_written": 7,
                "participants_mutated": 7,
                "participant_replay_collisions": 0,
            },
        }},
        {"meeting_event_extractions": 4, "meeting_events": 3,
         "event_participants": 6},
    )
    assert [check["ok"] for check in checks] == [
        True, True, False, True, True, True,
    ]
    assert checks[2]["check"] == (
        "accounting:event_pipeline:link:participants_inserted"
    )


def test_event_pipeline_accounting_accepts_update_producer_evidence():
    checks = _phase_accounting_checks(
        "event_pipeline",
        {"dry_run": False, "accounting": {
            "extract": {"events_inserted": 0},
            "normalize": {"events_inserted": 0},
            "link": {
                "participant_attempts": 2,
                "participants_planned_insert": 0,
                "participants_planned_update": 2,
                "participants_inserted": 0,
                "participants_updated": 2,
                "participants_written": 2,
                "participants_mutated": 2,
                "participant_replay_collisions": 0,
            },
        }},
        {"meeting_event_extractions": 0, "meeting_events": 0,
         "event_participants": 0},
    )

    by_name = {check["check"]: check for check in checks}
    assert by_name["accounting:event_pipeline:link:participants_inserted"]["ok"] is True
    assert by_name["accounting:event_pipeline:link:participants_updated"]["ok"] is True
    assert "row-count delta does not measure updates" in by_name[
        "accounting:event_pipeline:link:participants_updated"
    ]["detail"]


def test_event_pipeline_accounting_rejects_inconsistent_link_updates():
    checks = _phase_accounting_checks(
        "event_pipeline",
        {"dry_run": False, "accounting": {
            "extract": {"events_inserted": 0},
            "normalize": {"events_inserted": 0},
            "link": {
                "participant_attempts": 1,
                "participants_planned_insert": 0,
                "participants_planned_update": 1,
                "participants_inserted": 0,
                "participants_updated": 0,
                "participants_written": 0,
                "participants_mutated": 0,
                "participant_replay_collisions": 0,
            },
        }},
        {"meeting_event_extractions": 0, "meeting_events": 0,
         "event_participants": 0},
    )

    by_name = {check["check"]: check for check in checks}
    assert by_name["accounting:event_pipeline:link:participants_updated"]["ok"] is False


def test_expected_output_distinguishes_replay_from_unexplained_zero():
    deltas = {"entities": 0, "entity_mentions": 0}
    unexplained = _phase_expected_output_check(
        "sweep_docs", {"docs_processed": 4, "matches": 2}, deltas, force=False
    )
    replay = _phase_expected_output_check(
        "sweep_docs", {"docs_processed": 4, "matches": 2}, deltas, force=True
    )
    assert unexplained["ok"] is False
    assert replay["ok"] is True


def test_pattern_cascade_accepts_fully_accounted_incremental_replays():
    check = _phase_expected_output_check(
        "pattern_cascade",
        {
            "items_processed": 12541,
            "matches": 27,
            "mentions_planned": 27,
            "mentions_created": 0,
            "mention_replay_collisions": 27,
            "mentions_unresolved_entity": 0,
            "edges_planned": 27,
            "edges_created": 0,
            "edge_replay_collisions": 27,
            "edges_unresolved_endpoint": 0,
        },
        {"entities": 0, "entity_mentions": 0, "entity_relationships": 0},
        force=False,
    )
    assert check["ok"] is True
    assert check["detail"] == "zero writes with 54 replay collisions"


def test_event_pending_normalization_cannot_silently_produce_zero():
    check = _phase_expected_output_check(
        "event_pipeline", {"pending": {"normalize": 7}},
        {"meeting_events": 0}, force=False,
    )
    assert check["ok"] is False


@pytest.mark.parametrize("bad_value", [-1, 1.5, "4", True, None])
def test_expected_output_rejects_malformed_inputs_without_raising(bad_value):
    check = _phase_expected_output_check(
        "sweep_docs", {"docs_processed": bad_value, "matches": 0},
        {"entities": 0}, force=False,
    )
    assert check["ok"] is False
    assert "invalid expected-output counters" in check["detail"]


def test_expected_output_rejects_malformed_committed_delta():
    check = _phase_expected_output_check(
        "sweep_docs", {"docs_processed": 0, "matches": 0},
        {"entities": "0"}, force=False,
    )
    assert check["ok"] is False
    assert "invalid committed delta counters" in check["detail"]


def test_event_expected_output_rejects_boolean_pending_count():
    check = _phase_expected_output_check(
        "event_pipeline", {"pending": {"normalize": True}},
        {"meeting_events": 0}, force=False,
    )
    assert check["ok"] is False
    assert check["detail"] == "invalid pending event counts: normalize"


def test_event_expected_output_accepts_reported_link_updates_without_row_delta():
    check = _phase_expected_output_check(
        "event_pipeline",
        {"accounting": {"link": {
            "participant_attempts": 2,
            "participants_planned_insert": 0,
            "participants_planned_update": 2,
            "participants_inserted": 0,
            "participants_updated": 2,
            "participant_replay_collisions": 0,
        }}},
        {"event_participants": 0, "meeting_events": 0,
         "meeting_event_extractions": 0},
        force=True,
    )
    assert check["ok"] is True
    assert "reported 2 participant mutations" in check["detail"]


def test_event_expected_output_rejects_pending_link_work_even_when_forced():
    check = _phase_expected_output_check(
        "event_pipeline",
        {"pending": {"link": 3}},
        {"event_participants": 0, "meeting_events": 0,
         "meeting_event_extractions": 0},
        force=True,
    )
    assert check["ok"] is False
    assert "pending event work" in check["detail"]


def test_accounting_disagreement_fails_run_gate(monkeypatch):
    engine = _engine()
    phase = {"name": "sweep_docs", "description": "sweep probe",
             "critical": False, "allow_skip": True}

    def run_phase(*_):
        with engine.begin() as c:
            c.execute(text("INSERT INTO entities VALUES (1)"))
            c.execute(text(
                "INSERT INTO entity_mentions VALUES "
                "(1,1,'supporting_document',7,'staff','sweep_docs')"
            ))
        return {
            "success": True, "duration_s": 0.0, "entities_created": 1,
            "edges_created": 0, "attempts": 1, "error": None,
            "raw_result": {"docs_processed": 1, "matches": 2,
                           "entities_created": 1, "mentions_created": 2},
        }

    monkeypatch.setattr(detector, "PHASES", [phase])
    monkeypatch.setattr(detector, "_get_watermarks", lambda *_: set())
    monkeypatch.setattr(detector, "_run_phase", run_phase)
    monkeypatch.setattr(detector, "_mark_watermark", lambda *_: None)
    monkeypatch.setattr(detector, "_integrity_snapshot", lambda _: {})
    monkeypatch.setattr(detector, "_schema_contract_violations", lambda _: [])
    monkeypatch.setattr(detector, "_unmapped_entity_types", lambda _: [])
    monkeypatch.setattr(detector, "_write_run_state", lambda _: None)

    result = detector.run_detection(engine)
    assert result["gate"]["failed"] is True
    failed = [c for c in result["gate"]["checks"] if not c["ok"]]
    assert any(c["check"] == "accounting:sweep_docs:mentions_created"
               for c in failed)


def test_event_orchestrator_forwards_dry_run_and_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(event_orchestrator, "count_pending", lambda _: {
        "extract": 0, "normalize": 0, "link": 0,
    })
    monkeypatch.setattr(event_orchestrator, "run_step", lambda name, extra_args: (
        calls.append((name, list(extra_args))) or
        {"step": name, "ok": True, "elapsed": 0, "returncode": 0}
    ))

    result = event_orchestrator.run_event_pipeline(
        object(), steps=["extract"], dry_run=True, limit=5
    )
    assert result["success"] is True
    assert calls == [("extract", ["--dry-run", "--limit", "5"])]


def test_failed_phase_and_gate_surface_in_ops_digest(monkeypatch, tmp_path):
    state = tmp_path / "entity-run-2026-09-07.json"
    state.write_text("""{
      "phases": [{"name": "sweep_docs", "status": "failed",
                  "error": "injected failure", "attempts": 2}],
      "gate": {"failed": true, "checks": [
        {"check": "accounting:sweep_docs:mentions_created", "ok": false,
         "detail": "reported 2, committed entity_mentions delta +1"}
      ]}}
    """)
    monkeypatch.setattr(sync_digest, "DATA_SYNC", tmp_path)
    lines = sync_digest.entity_run_health()
    assert any("sweep_docs FAILED" in line for line in lines)
    assert any("accounting:sweep_docs:mentions_created" in line for line in lines)


def test_run_evidence_is_immutable_with_daily_latest_pointer(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_REPO_ROOT", str(tmp_path))
    state = {
        "run_id": "a" * 32,
        "state_file": "entity-run-2026-09-07-120000-aaaaaaaa.json",
        "latest_file": "entity-run-2026-09-07.json",
        "dry_run": False, "phases": [], "gate": {"failed": False},
    }
    detector._write_run_state(state)
    immutable = tmp_path / "data" / "sync" / state["state_file"]
    latest = tmp_path / "data" / "sync" / state["latest_file"]
    assert immutable.exists() and latest.exists()
    assert json.loads(immutable.read_text())["run_id"] == "a" * 32
    assert json.loads(latest.read_text())["state_file"] == state["state_file"]


def test_dry_run_evidence_does_not_replace_daily_latest(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_REPO_ROOT", str(tmp_path))
    state = {
        "run_id": "b" * 32,
        "state_file": "entity-run-2026-09-07-130000-bbbbbbbb.json",
        "latest_file": "entity-run-2026-09-07.json",
        "dry_run": True, "phases": [], "gate": {"failed": False},
    }
    detector._write_run_state(state)
    run_dir = tmp_path / "data" / "sync"
    assert (run_dir / state["state_file"]).exists()
    assert not (run_dir / state["latest_file"]).exists()


def test_producer_metadata_fingerprints_source_code():
    metadata = _producer_metadata({
        "module": "scripts.entities.event_extract",
        "run_fn_name": "process_docs",
    })
    assert metadata["function"] == "process_docs"
    assert len(metadata["code_sha256"]) == 64
    assert metadata["code_modules"] == ["scripts.entities.event_extract"]
    assert metadata["code_module_sha256"] == {
        "scripts.entities.event_extract": metadata["code_sha256"],
    }
    assert metadata["code_module_errors"] == {}
    assert metadata["code_evidence_complete"] is True


def test_graph_builder_declares_all_producer_code_modules():
    phase = next(phase for phase in detector.PHASES
                 if phase["name"] == "graph_builder")
    modules = tuple(phase["code_modules"])

    assert list(modules) == sorted(modules)
    assert len(set(modules)) == len(modules)
    # Derived, not copied: graph_builder_runtime is reachable from the declaration
    # and was previously missing from it.
    runtime = "scripts.entities.graph_builder_runtime"
    assert runtime in producer_manifest.import_closure(modules, REPO_ROOT)
    assert runtime in modules
    assert producer_manifest.undocumented_imports(phase, REPO_ROOT) == ()


def test_event_pipeline_declares_complete_producer_manifest_and_hashes():
    phase = next(phase for phase in detector.PHASES
                 if phase["name"] == "event_pipeline")
    modules = tuple(phase["code_modules"])

    assert list(modules) == sorted(modules)
    assert len(set(modules)) == len(modules)
    # The quarantine module bears on normalisation behaviour while living outside
    # scripts/entities; the manifest must name it.
    assert "scripts.kg.quarantine" in modules
    assert producer_manifest.undocumented_imports(phase, REPO_ROOT) == ()

    metadata = _producer_metadata(phase)
    assert metadata["code_modules"] == list(modules)
    assert metadata["code_evidence_complete"] is True
    assert metadata["code_module_errors"] == {}
    assert set(metadata["code_module_sha256"]) == set(modules)
    assert all(len(module_hash) == 64
               for module_hash in metadata["code_module_sha256"].values())
    assert len(metadata["code_sha256"]) == 64


def test_multi_module_producer_hash_changes_when_any_component_changes(
        monkeypatch, tmp_path):
    module_names = ("test.producer.one", "test.producer.two")
    source_files = [tmp_path / "one.py", tmp_path / "two.py"]
    for index, source_file in enumerate(source_files):
        source_file.write_text(f"VALUE = {index}\\n")
    modules = {
        name: types.SimpleNamespace(__file__=str(source))
        for name, source in zip(module_names, source_files)
    }
    monkeypatch.setattr(detector.importlib, "import_module", modules.__getitem__)
    phase = {"module": module_names[0], "code_modules": module_names,
             "run_fn_name": "run"}

    original = _producer_metadata(phase)
    for changed_index, source_file in enumerate(source_files):
        source_file.write_text(f"VALUE = {changed_index + 10}\\n")
        changed = _producer_metadata(phase)
        assert original["code_sha256"] != changed["code_sha256"]
        assert original["code_module_sha256"][module_names[changed_index]] != (
            changed["code_module_sha256"][module_names[changed_index]]
        )


@pytest.mark.parametrize("module, module_object", [
    ("test.producer.missing_import", None),
    ("test.producer.missing_file", types.SimpleNamespace(
        __file__="/definitely/not/a/producer.py")),
])
def test_producer_metadata_surfaces_unavailable_declared_evidence(
        monkeypatch, module, module_object):
    def import_module(name):
        if module_object is None:
            raise ModuleNotFoundError(f"No module named {name!r}")
        return module_object

    monkeypatch.setattr(detector.importlib, "import_module", import_module)
    metadata = _producer_metadata({"module": module, "code_modules": (module,)})

    assert metadata["code_sha256"] is None
    assert metadata["code_module_sha256"] == {module: None}
    assert module in metadata["code_module_errors"]
    assert metadata["code_evidence_complete"] is False


def test_ops_digest_skips_newer_dry_run_state(monkeypatch, tmp_path):
    live = tmp_path / "entity-run-2026-09-07-120000-live.json"
    dry = tmp_path / "entity-run-2026-09-07-130000-dry.json"
    live.write_text(json.dumps({
        "dry_run": False,
        "phases": [{"name": "resolver", "status": "failed"}],
        "gate": {"failed": False},
    }))
    dry.write_text(json.dumps({
        "dry_run": True, "phases": [], "gate": {"failed": False},
    }))
    monkeypatch.setattr(sync_digest, "DATA_SYNC", tmp_path)
    lines = sync_digest.entity_run_health()
    assert any("resolver FAILED" in line for line in lines)


# The event-normalization SQLite contract is shared, not re-declared here: the
# accepted read chain (jurisdictions -> public_bodies -> meetings ->
# supporting_documents with text_content/meeting_db_id/extraction method, plus
# extraction extractor/version fields) lives in one place.
from _kg_event_normalize_sqlite import add_extraction, build_engine, seed


def test_event_normalizer_reports_explicit_dry_run_accounting():
    engine = build_engine()
    seed(engine, xid=1, action_verb="approved")
    add_extraction(engine, xid=2, action_verb="denied")

    stats = normalize(engine, dry_run=True)

    assert stats["extractions_examined"] == 2
    assert stats["normalizable"] == 2
    assert stats["events_planned"] == 2
    assert stats["events_inserted"] == 0
    assert stats["extraction_links_updated"] == 0
    assert stats["skipped_unmapped_type"] == 0
    assert stats["read_failures"] == 0
    assert stats["rows_rolled_back"] == 0
    assert stats["failure_reason"] is None

    receipt = stats["validation_receipt"]
    assert receipt["dry_run"] is True
    assert receipt["state"] == "sealed"
    assert receipt["failure"] is None
    # Two candidates = two event assertions + two link assertions.
    assert receipt["rows"]["proposed"] == 4
    assert receipt["rows"]["would_insert"] == 2
    assert receipt["rows"]["would_update"] == 2
    assert receipt["rows"]["committed"] == 0


def test_an_unmapped_verb_fails_closed_rather_than_being_skipped():
    """The accepted read contract rejects an uninterpretable verb."""
    from scripts.entities.event_normalize_runtime import NormalizationRunError

    engine = build_engine()
    seed(engine, xid=1, action_verb="unmapped action")

    with pytest.raises(NormalizationRunError) as exc:
        normalize(engine, dry_run=True)

    error = exc.value
    assert error.stats["read_failures"] == 1
    assert error.stats["skipped_unmapped_type"] == 0
    assert error.stats["events_inserted"] == 0
    assert error.receipt["state"] == "sealed"
    assert error.receipt["failure"]
    # A row that could not be interpreted is not an assertion row.
    assert error.receipt["rows"]["proposed"] == 0


def test_event_normalizer_honors_exact_extraction_limit():
    engine = build_engine()
    seed(engine, xid=1, action_verb="approved")
    add_extraction(engine, xid=2, action_verb="denied")
    add_extraction(engine, xid=3, action_verb="continued")

    stats = normalize(engine, limit=1, dry_run=True)
    assert stats["extractions_examined"] == 1
    assert stats["normalizable"] == 1
    assert stats["events_planned"] == 1
    assert stats["read_failures"] == 0
    assert stats["validation_receipt"]["rows"]["proposed"] == 2
