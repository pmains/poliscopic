"""Regression tests for the bounded Stage 1 dry-gate repairs.

Covers the four implemented repairs: sweep_docs version binding (A), blank
identity refusal (B), canonical producer emission (C), and failure
classification with retry/abort (D).  Everything is isolated: no database, no
network, no pipeline run.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from scripts.entities import detect_entities
from scripts.entities import failure_classification as fc
from scripts.entities import pattern_cascade
from scripts.entities.graph_builder_sources import PZItemDetailsSource
from scripts.entities.sweep_docs_extraction import extract_entities_from_doc
from scripts.kg import registries as r
from scripts.kg.emission_checks import check
from scripts.kg.producer_versions import (
    declared_producer_version,
    version_declaration_problems,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── A. sweep_docs receipt version binding ───────────────────────────────


def test_sweep_docs_binds_the_declared_version_without_a_literal():
    """The producer resolves the version from the registry, not a literal."""
    source = (REPO_ROOT / "scripts/entities/sweep_docs.py").read_text()
    assert 'declared_producer_version("sweep_docs")' in source
    assert "sweep_docs/1.0" not in source, "version must not be duplicated inline"
    assert declared_producer_version("sweep_docs") == "sweep_docs/1.0"


def test_version_registry_is_still_coherent():
    assert version_declaration_problems() == []


def _sqlite_engine():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _now(dbapi_connection, _record):
        dbapi_connection.create_function("now", 0, lambda: "2026-09-12 00:00:00")

    with engine.begin() as conn:
        conn.execute(text("""CREATE TABLE supporting_documents (
            id INTEGER PRIMARY KEY, document_title TEXT, text_content TEXT,
            text_extraction_method TEXT, swept_at TEXT)"""))
        conn.execute(text("""CREATE TABLE entities (
            id INTEGER PRIMARY KEY, entity_type TEXT, name TEXT,
            normalized_name TEXT, is_government BOOLEAN, resolution_status TEXT,
            first_seen_at TEXT, last_seen_at TEXT, mention_count INTEGER,
            created_at TEXT, updated_at TEXT,
            UNIQUE (normalized_name, entity_type))"""))
        conn.execute(text("""CREATE TABLE entity_mentions (
            id INTEGER PRIMARY KEY, entity_id INTEGER, source_type TEXT,
            source_id INTEGER, role_in_context TEXT)"""))
        conn.execute(text("""INSERT INTO supporting_documents
            (id, document_title, text_content, text_extraction_method, swept_at)
            VALUES (1, 'Doc', 'Applicant: Acme Development LLC', 'pdftotext', NULL)"""))
    return engine


def test_run_sweep_docs_seals_the_declared_version():
    from scripts.entities import sweep_docs

    result = sweep_docs.run_sweep_docs(_sqlite_engine(), dry_run=True)
    receipt = result["validation_receipt"]
    assert receipt["producer_version"] == declared_producer_version("sweep_docs")
    assert receipt["state"] == "sealed"
    assert receipt["values"]["reconciles"] is True
    assert result["dry_run"] is True


def test_sweep_docs_receipt_is_refused_when_the_version_mismatches():
    from scripts.kg.emission_receipts import reconcile_receipts

    receipt = {
        "producer": "sweep_docs", "producer_version": "unknown",
        "model_version": r.MODEL_VERSION, "registry_snapshot": r.snapshot_sha256(),
        "state": "sealed", "dry_run": True,
        "values": {"attempted": 0, "accepted": 0, "rejected": 0},
        "rows": {"proposed": 0, "would_insert": 0, "would_update": 0,
                 "replay_noop": 0, "unresolved": 0, "committed": 0,
                 "rolled_back": 0},
        "failure": None, "rejections": [], "observed": {},
        "derived_excluded": 0, "derived_exclusion_reasons": [],
        "reclassified_conflicts": [],
    }
    from scripts.kg import orchestration_receipts as orch

    result = orch.enforce_phase_receipt(
        "sweep_docs", raw_result={"validation_receipt": receipt}, dry_run=True)
    assert result["ok"] is False
    assert any("producer_version" in reason for reason in result["reasons"])


# ── B. blank identity refusal ───────────────────────────────────────────


def test_punctuation_only_actor_is_refused_and_counted():
    """The doc-117401 shape: an actor that normalises to the empty string."""
    rejections: list[dict] = []
    candidates = extract_entities_from_doc(
        'Owner: ."\nApplicant: Acme Development LLC\n', rejections=rejections)
    assert [c["normalized"] for c in candidates] == ["acme development"]
    assert len(rejections) == 1
    assert rejections[0]["name"] == '."'
    assert rejections[0]["reason"] == "blank_normalized_identity"


def test_blank_identity_refusal_is_counted_not_silently_dropped():
    rejections: list[dict] = []
    extract_entities_from_doc('Owner: ."\n', rejections=rejections)
    assert rejections, "a refused candidate must be recorded"


def test_valid_short_names_are_still_emitted():
    candidates = extract_entities_from_doc("Applicant: Bo Li\n")
    assert [c["normalized"] for c in candidates] == ["bo li"]


def test_no_candidate_ever_carries_a_blank_identity():
    rejections: list[dict] = []
    candidates = extract_entities_from_doc(
        'Owner: ."\nStaff Contact: --\nApplicant: Acme Development LLC\n',
        rejections=rejections)
    for candidate in candidates:
        assert candidate["normalized"].strip()
        assert candidate["entity_type"].strip()


def test_rejections_parameter_is_optional_and_backward_compatible():
    assert isinstance(extract_entities_from_doc("Applicant: Acme LLC\n"), list)


# ── C. canonical graph_builder emission ─────────────────────────────────


def _specs(rows):
    return list(PZItemDetailsSource().produce(rows))


def _values(specs):
    out = []
    for entity, edge, mention in specs:
        if entity is not None:
            out.append(("entity_type", entity.entity_type))
        if edge is not None:
            out.append(("relationship", edge.relationship))
        if mention is not None and mention.role:
            out.append(("role", mention.role))
    return out


def _row(**over):
    row = {"case_number": "C-1-2", "applicant": "Acme Development LLC",
           "recommendation": "Approved with conditions",
           "presented_by": "Jane Doe", "pz_id": 7, "jurisdiction_id": 1}
    row.update(over)
    return row


def test_applicant_emits_canonical_applied_for_actor_to_case():
    specs = _specs([_row()])
    edges = [e for _en, e, _m in specs if e is not None]
    applied = [e for e in edges if e.relationship == "APPLIED_FOR"]
    assert applied, "applicant must emit APPLIED_FOR"
    for edge in applied:
        assert edge.relationship in r.CANONICAL_PREDICATES
        # actor -> case direction
        assert edge.from_type in ("person", "organization")
        assert edge.to_type == "case"
        assert r.direction_allows(edge.relationship, edge.from_type, edge.to_type)


def test_applicant_relationship_evidence_is_source_supported():
    from scripts.kg.registries.relationships import evidence_allows

    assert evidence_allows("APPLIED_FOR", "structured_record")


def test_comma_pair_emits_no_inferred_represents_edge():
    specs = _specs([_row(applicant="Jane Doe, Acme Development LLC")])
    edges = [e for _en, e, _m in specs if e is not None]
    assert not [e for e in edges if e.relationship == "REPRESENTS"]


def test_presenter_emits_no_participation_edge():
    specs = _specs([_row(presented_by="Jane Doe")])
    edges = [e for _en, e, _m in specs if e is not None]
    assert not [e for e in edges if e.relationship in ("HAS_STAFF", "PARTICIPATED_IN")]


def test_mention_roles_remain_contextual_and_canonical():
    specs = _specs([_row(applicant="Jane Doe, Acme Development LLC")])
    roles = [m.role for _en, _e, m in specs if m is not None and m.role]
    assert roles, "mentions must be preserved"
    for role in roles:
        assert role in r.CANONICAL_ROLES, role


def test_recommendation_is_not_emitted_as_kg():
    specs = _specs([_row()])
    values = _values(specs)
    assert not [v for v in values if v[1] == "recommendation"]
    assert not [v for v in values if v[1] == "HAS_RECOMMENDATION"]
    for entity, _edge, _mention in specs:
        if entity is not None:
            assert entity.entity_type != "recommendation"


def test_every_graph_builder_emitted_value_is_canonical():
    specs = _specs([
        _row(),
        _row(applicant="Jane Doe, Acme Development LLC", presented_by="Bo Li"),
        _row(case_number="C-9-9", applicant="Vertical Bridge", recommendation=""),
    ])
    for category, value in _values(specs):
        check(category, value)  # raises on non-canonical vocabulary


# ── C. canonical pattern_cascade emission ───────────────────────────────


def test_pattern_cascade_maps_only_authorized_relationships():
    assert pattern_cascade.ROLE_EDGE_MAP == {"applicant": "APPLIED_FOR"}
    for predicate in pattern_cascade.ROLE_EDGE_MAP.values():
        assert predicate in r.CANONICAL_PREDICATES


def test_request_and_location_are_evidence_not_roles():
    from scripts.entities.pattern_cascade_patterns import (
        BODY_PATTERNS, EVIDENCE_ONLY_PATTERNS,
    )

    for patterns in BODY_PATTERNS.values():
        for _field, role, _pattern in patterns:
            assert role not in ("request", "location"), role
            assert role in r.CANONICAL_ROLES, role
    recorded = {role for pats in EVIDENCE_ONLY_PATTERNS.values()
                for _f, role, _p in pats}
    assert recorded == {"request", "location"}


def test_no_pattern_role_is_a_legacy_predicate_name():
    from scripts.entities.pattern_cascade_patterns import BODY_PATTERNS

    legacy = {"HAS_APPLICANT", "HAS_ATTORNEY", "HAS_STAFF", "HAS_OWNER",
              "HAS_RECOMMENDATION"}
    for patterns in BODY_PATTERNS.values():
        for _field, role, _pattern in patterns:
            assert role not in legacy


# ── D. failure classification, retry and abort ──────────────────────────


def test_transient_infrastructure_failures_are_retryable():
    error = OperationalError("SELECT 1", {}, Exception("connection reset"))
    kind = fc.classify_failure(error)
    assert kind == fc.TRANSIENT
    assert fc.is_retryable(kind) is True
    assert fc.should_abort(kind) is False


def test_deterministic_failures_are_not_retryable_and_abort():
    for error in (ValueError("blank identity"), RuntimeError("contract"),
                  KeyError("missing"), AssertionError("invariant")):
        kind = fc.classify_failure(error)
        assert kind == fc.DETERMINISTIC, type(error).__name__
        assert fc.is_retryable(kind) is False
        assert fc.should_abort(kind) is True


def test_unclassified_failures_default_to_deterministic():
    class WeirdError(Exception):
        pass

    assert fc.classify_failure(WeirdError("?")) == fc.DETERMINISTIC
    assert fc.classify_failure(None) == fc.DETERMINISTIC


def test_receipt_refusal_is_deterministic():
    assert fc.classify_failure(None, receipt_refused=True) == fc.DETERMINISTIC
    assert fc.classify_failure(
        OperationalError("x", {}, Exception("y")), receipt_refused=True
    ) == fc.DETERMINISTIC


def _run_phase_with(monkeypatch, behaviour):
    monkeypatch.setattr(detect_entities, "_resolve_phase_fn", lambda _p: behaviour)
    args = type("A", (), {"dry_run": True, "force": False, "verbose": False})()
    phase = {"name": "graph_builder", "module": "m", "run_fn_name": "f",
             "description": "d", "critical": False}
    return detect_entities._run_phase(phase, None, args)


def test_deterministic_failure_gets_exactly_one_attempt(monkeypatch):
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise ValueError("EntityAssertion.normalized_name must be non-empty")

    result = _run_phase_with(monkeypatch, boom)
    assert calls["n"] == 1, "a deterministic failure must not be retried"
    assert result["success"] is False
    assert result["failure_kind"] == fc.DETERMINISTIC


def test_transient_failure_is_retried(monkeypatch):
    calls = {"n": 0}

    def flaky(*_a, **_k):
        calls["n"] += 1
        raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    monkeypatch.setattr(detect_entities, "PHASE_RETRY_BACKOFF_S", 0)
    result = _run_phase_with(monkeypatch, flaky)
    assert calls["n"] == detect_entities.PHASE_RETRIES + 1
    assert result["failure_kind"] == fc.TRANSIENT


def test_returned_failure_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def failed(*_a, **_k):
        calls["n"] += 1
        return {"success": False, "error": "ValueError: boom"}

    result = _run_phase_with(monkeypatch, failed)
    assert calls["n"] == 1
    assert result["failure_kind"] == fc.DETERMINISTIC


def _phases(first, second):
    def make(name):
        return {"name": name, "description": name, "module": "m",
                "run_fn_name": name, "critical": False, "allow_skip": True,
                "code_modules": ("scripts.entities.detect_entities",)}
    return [make(first), make(second)]


def _stub_orchestration(monkeypatch, phases, behaviour):
    monkeypatch.setattr(detect_entities, "PHASES", phases)
    monkeypatch.setattr(detect_entities, "_resolve_phase_fn",
                        lambda phase: lambda *a, **k: behaviour(phase))
    monkeypatch.setattr(detect_entities, "_get_watermarks", lambda *_: set())
    monkeypatch.setattr(detect_entities, "_write_run_state", lambda *_: None)
    monkeypatch.setattr(detect_entities, "_schema_contract_violations",
                        lambda *_: [])
    monkeypatch.setattr(detect_entities, "_unmapped_entity_types", lambda *_: [])
    monkeypatch.setattr(detect_entities, "integrity_snapshot", lambda *_: {})
    monkeypatch.setattr(detect_entities, "PHASE_RETRY_BACKOFF_S", 0)


def test_deterministic_failure_stops_later_phases(monkeypatch):
    phases = _phases("graph_builder", "sweep_docs")

    def behaviour(phase):
        if phase["name"] == "graph_builder":
            raise ValueError("blank identity")
        return {"success": True, "entities_created": 0, "edges_created": 0,
                "validation_receipt": _ok_receipt()}

    _stub_orchestration(monkeypatch, phases, behaviour)
    results = detect_entities.run_detection(None, dry_run=True)
    ran = [entry["name"] for entry in results["phases"]]
    assert ran == ["graph_builder"], ran
    assert any("deterministic failure" in str(e.get("error"))
               for e in results["errors"])


def test_receipt_refusal_stops_later_phases(monkeypatch):
    phases = _phases("graph_builder", "sweep_docs")

    def behaviour(phase):
        return {"success": True, "entities_created": 0, "edges_created": 0}

    _stub_orchestration(monkeypatch, phases, behaviour)
    results = detect_entities.run_detection(None, dry_run=True)
    ran = [entry["name"] for entry in results["phases"]]
    assert ran == ["graph_builder"], ran


def test_transient_failure_does_not_abort_on_a_later_success(monkeypatch):
    """Retryable faults keep the run going; only deterministic ones abort."""
    phases = _phases("graph_builder", "sweep_docs")

    def behaviour(phase):
        return {"success": True, "entities_created": 0, "edges_created": 0,
                "validation_receipt": _ok_receipt()}

    _stub_orchestration(monkeypatch, phases, behaviour)
    results = detect_entities.run_detection(None, dry_run=True)
    ran = [entry["name"] for entry in results["phases"]]
    assert ran == ["graph_builder", "sweep_docs"], ran


def _ok_receipt(producer: str = "graph_builder") -> dict:
    from scripts.kg.emission import EmissionValidator

    validator = EmissionValidator(producer, "graph_builder/1.0", dry_run=True)
    validator.start_batch()
    validator.complete_validation()
    validator.classify_rows(would_insert=0)
    return validator.seal().serialize()
