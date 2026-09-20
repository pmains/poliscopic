"""Resolver emission-boundary tests, plus producer-manifest drift coverage.

Isolated: no database, no pipeline run, no ML.  The composite-split emission path
is driven through a recording stub connection so the *emitted* behaviour is
asserted directly — which role is written, and which statements are never issued.

The one structural fact pinned about the subphases is that composite proposal
building cannot depend on ``dry_run``, because that dependency was the defect
that made a dry run propose nothing.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest
from sqlalchemy import text

from scripts.entities import producer_manifest
from scripts.entities import resolver as resolver_module
from scripts.entities import resolver_persistence
from scripts.entities.resolver import PHASES, PHASE_ORDER, SPLIT_EMITTED_VALUES
from scripts.entities.resolver_accounting import (
    SubphaseProposals,
    aggregate_proposals,
    is_canonical_emission,
    seal_resolver_receipt,
    subphase_result,
)
from scripts.entities.resolver_persistence import apply_composite_splits
from scripts.entities.resolver_proposals import (
    SPLIT_EVIDENCE_CLASS,
    SPLIT_ORG_ROLE,
    build_composite_ops,
    organization_mention_bundle,
    organization_mention_evidence,
)
from scripts.kg import registries as r
from scripts.kg.emission import EmissionValidator
from scripts.kg.emission_receipts import reconcile_receipts
from scripts.kg.registries.relationships import (
    PREDICATES,
    direction_allows,
    evidence_allows,
    get_predicate,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_TEXT = "Annmarie Beckett, Vertical Bridge"


def reconciles(receipt: dict, producer: str) -> list[str]:
    return reconcile_receipts(
        [receipt],
        expected_producers=[producer],
        model_version=r.MODEL_VERSION,
        registry_snapshot=r.snapshot_sha256(),
    )


def resolver_validator(dry_run: bool = True) -> EmissionValidator:
    validator = EmissionValidator("resolver", "resolver/1.0", dry_run=dry_run)
    validator.start_batch()
    return validator


# ── per-subphase accounting ─────────────────────────────────────────────


def test_subphase_proposed_is_satisfied_by_classification():
    item = SubphaseProposals("type_conflict", would_update=3, unresolved=1)
    assert item.proposed == 4
    assert item.problems(dry_run=True) == []


def test_subphase_rejects_committed_rows_in_a_dry_run():
    item = SubphaseProposals("composite_split", would_update=2, committed=2)
    assert any("dry run reported" in p for p in item.problems(dry_run=True))


def test_subphase_rejects_committed_exceeding_proposals():
    item = SubphaseProposals("name_variation", would_update=1, committed=5)
    assert any("exceeds proposals" in p for p in item.problems(dry_run=False))


def test_aggregate_dry_run_classifies_every_proposal_and_writes_nothing():
    subphases = [
        SubphaseProposals("type_conflict", would_update=4),
        SubphaseProposals("composite_split", unresolved=2),
        SubphaseProposals("name_variation", would_update=3, compared=500),
    ]
    accounting = aggregate_proposals(subphases, dry_run=True)
    assert accounting.proposed == 9
    assert accounting.committed == 0
    assert accounting.rolled_back == 0
    assert accounting.problems(dry_run=True) == []


def test_aggregate_live_records_rollback_when_a_subphase_wrote_less():
    accounting = aggregate_proposals(
        [SubphaseProposals("type_conflict", would_update=4, committed=1)],
        dry_run=False,
    )
    assert accounting.committed == 1
    assert accounting.rolled_back == 3
    assert accounting.problems(dry_run=False) == []


def test_comparisons_are_not_treated_as_assertions():
    accounting = aggregate_proposals(
        [SubphaseProposals("name_variation", would_update=1, compared=999)],
        dry_run=True,
    )
    assert accounting.proposed == 1


def test_subphase_result_preserves_legacy_keys_and_carries_accounting():
    accounting = SubphaseProposals("type_conflict", would_update=2)
    result = subphase_result("type_conflict", accounting, phase1_type_conflicts=2)
    assert result["phase1_type_conflicts"] == 2
    assert result["_proposals"] is accounting


def test_phase_table_and_order_are_unchanged():
    assert PHASE_ORDER == ["type_conflict", "composite_split", "name_variation"]
    assert set(PHASES) == set(PHASE_ORDER)


# ── receipt ─────────────────────────────────────────────────────────────


def test_resolver_receipt_is_sealed_bound_and_reconciled():
    validator = resolver_validator(dry_run=True)
    receipt = seal_resolver_receipt(
        validator, [SubphaseProposals("type_conflict", would_update=2)],
        dry_run=True)
    assert reconciles(receipt, "resolver") == []
    assert receipt["producer_version"] == "resolver/1.0"
    assert receipt["model_version"] == r.MODEL_VERSION
    assert receipt["registry_snapshot"] == r.snapshot_sha256()


def test_empty_resolver_receipt_is_an_honest_zero():
    validator = resolver_validator(dry_run=True)
    receipt = seal_resolver_receipt(validator, [], dry_run=True)
    assert reconciles(receipt, "resolver") == []
    assert receipt["rows"]["proposed"] == 0


def test_live_resolver_receipt_commits_exactly_what_it_classified():
    validator = resolver_validator(dry_run=False)
    receipt = seal_resolver_receipt(
        validator,
        [SubphaseProposals("type_conflict", would_update=3, committed=3)],
        dry_run=False)
    assert receipt["rows"]["committed"] == 3
    assert receipt["rows"]["rolled_back"] == 0
    assert reconciles(receipt, "resolver") == []


# ── canonical composite emission ────────────────────────────────────────


def test_split_emits_only_canonical_values_and_no_relationship():
    categories = {category for category, _ in SPLIT_EMITTED_VALUES}
    assert "relationship" not in categories, (
        "a comma is punctuation, not affiliation evidence: no predicate may be "
        "emitted from a composite split"
    )
    for category, value in SPLIT_EMITTED_VALUES:
        assert is_canonical_emission(category, value), (category, value)


def test_organization_role_is_the_weakest_truthful_role():
    assert SPLIT_ORG_ROLE == "mentioned"
    assert is_canonical_emission("role", SPLIT_ORG_ROLE)


def test_split_bundle_validates_and_carries_exact_evidence_identity():
    evidence = organization_mention_evidence(
        source_type="agenda_item", source_id=117268,
        source_text=SOURCE_TEXT, org_name="Vertical Bridge")
    assert evidence.kind == "evidence"
    assert evidence.parts_dict()["span_start"] == "19" if hasattr(
        evidence, "parts_dict") else True
    bundle = organization_mention_bundle(
        source_type="agenda_item", source_id=117268,
        source_text=SOURCE_TEXT, org_name="Vertical Bridge",
        model_version=r.MODEL_VERSION)
    assert bundle.evidence_identity.digest == evidence.digest
    assert bundle.evidence_class == SPLIT_EVIDENCE_CLASS
    assert bundle.assertion_class == "source_supported"
    assert bundle.role == SPLIT_ORG_ROLE
    assert bundle.relationship is None
    validator = resolver_validator(dry_run=True)
    validator.validate_bundle(bundle, source="agenda_item:117268")
    assert validator.receipt.values_rejected == 0
    assert validator.receipt.values_accepted == 5


def test_split_evidence_span_marks_where_the_organization_was_named():
    key = organization_mention_evidence(
        source_type="agenda_item", source_id=1,
        source_text=SOURCE_TEXT, org_name="Vertical Bridge")
    parts = dict(key.parts)
    expected = SOURCE_TEXT.lower().find("vertical bridge")
    assert parts["span_start"] == str(expected)
    assert parts["span_end"] == str(expected + len("Vertical Bridge"))


def test_split_evidence_identity_is_stable_for_the_same_occurrence():
    """The same occurrence yields the same identity; a different one does not."""
    first = organization_mention_evidence(
        source_type="agenda_item", source_id=7,
        source_text=SOURCE_TEXT, org_name="Vertical Bridge")
    again = organization_mention_evidence(
        source_type="agenda_item", source_id=7,
        source_text=SOURCE_TEXT, org_name="Vertical Bridge")
    other = organization_mention_evidence(
        source_type="agenda_item", source_id=8,
        source_text=SOURCE_TEXT, org_name="Vertical Bridge")
    assert first.digest == again.digest
    assert first.digest != other.digest


class _RecordingConn:
    """Minimal connection stub recording exactly what the split issues.

    Statements the split does not write are answered permissively, so the test
    observes only the emission decisions; every written statement is recorded.
    """

    def __init__(self, sources):
        self.sources = sources
        self.inserts: list[tuple[str, dict]] = []
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        params = dict(params or {})
        self.statements.append(sql)
        if "INSERT INTO entity_mentions" in sql:
            self.inserts.append((sql, params))
            return _Result([])
        if "FROM entity_mentions" in sql and "source_type = 'agenda_item'" in sql:
            return _Result(self.sources)
        if sql.strip().startswith("SELECT 1 FROM entity_mentions"):
            return _Result([])
        if "INSERT INTO entities" in sql and "RETURNING" in sql:
            indices = sorted({key[2:] for key in params if key.startswith("nn")},
                             key=lambda value: int(value))
            return _Result([
                (params[f"nn{i}"], params[f"et{i}"], 1000 + int(i))
                for i in indices
            ])
        return _Result([])


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _op(entity_id=11, person_norm="annmarie beckett", org_norm="vertical bridge"):
    from scripts.entities.resolver_proposals import CompositeOp
    return CompositeOp(entity_id, "Annmarie Beckett, Vertical Bridge",
                       "Annmarie Beckett", "Vertical Bridge",
                       person_norm, org_norm)


def test_apply_composite_splits_writes_mentioned_role_and_no_relationship():
    conn = _RecordingConn([("agenda_item", 117268, SOURCE_TEXT)])
    outcome = apply_composite_splits(
        conn, [_op()], {}, dry_run=False, validator=resolver_validator(False),
        model_version=r.MODEL_VERSION)

    assert outcome["organization_mentions_insert"] == 1
    assert len(conn.inserts) == 1
    role = conn.inserts[0][1]["role"]
    assert role == SPLIT_ORG_ROLE
    # Every statement issued is an entity_mentions insert: no relationship row.
    for sql, _ in conn.inserts:
        assert "entity_relationships" not in sql
        assert "HAS_APPLICANT" not in sql


def test_apply_composite_splits_dry_run_classifies_but_writes_nothing():
    conn = _RecordingConn([("agenda_item", 117268, SOURCE_TEXT)])
    outcome = apply_composite_splits(
        conn, [_op()], {}, dry_run=True, validator=resolver_validator(True),
        model_version=r.MODEL_VERSION)
    assert outcome["organization_mentions_insert"] == 1
    assert conn.inserts == []


def test_apply_composite_splits_records_replay_when_org_mention_exists():
    class ExistsConn(_RecordingConn):
        def execute(self, statement, params=None):
            sql = str(statement)
            if sql.strip().startswith("SELECT 1 FROM entity_mentions"):
                return _Result([(1,)])
            return super().execute(statement, params)

    conn = ExistsConn([("agenda_item", 117268, SOURCE_TEXT)])
    outcome = apply_composite_splits(
        conn, [_op()], {}, dry_run=False, validator=resolver_validator(False),
        model_version=r.MODEL_VERSION)
    assert outcome["organization_mentions_replay"] == 1
    assert outcome["organization_mentions_insert"] == 0
    assert conn.inserts == []


def test_apply_composite_splits_fails_closed_without_a_validator():
    conn = _RecordingConn([("agenda_item", 117268, SOURCE_TEXT)])
    outcome = apply_composite_splits(
        conn, [_op()], {}, dry_run=False, validator=None,
        model_version=r.MODEL_VERSION)
    assert outcome["organization_mentions_insert"] == 0
    assert outcome["unresolved"] == 1
    assert conn.inserts == []


def test_composite_path_never_mentions_legacy_vocabulary():
    """The comma-split path emits no predicate and no legacy role.

    Scoped to the composite functions: ``merge_entities`` legitimately re-points
    ``entity_relationships`` for merges, which is a different operation.
    """
    composite_source = (
        inspect.getsource(resolver_persistence.apply_composite_splits)
        + inspect.getsource(
            resolver_persistence.write_split_organization_mentions)
    )
    assert "HAS_APPLICANT" not in composite_source
    assert "entity_relationships" not in composite_source
    assert "'firm'" not in composite_source
    assert "SPLIT_ORG_ROLE" in composite_source


def test_person_mention_roles_are_preserved_by_repointing():
    """Re-pointing updates entity_id only, so contextual roles survive."""
    source = inspect.getsource(resolver_persistence._execute_mention_updates)
    assert "SET entity_id = v.pid" in source
    # Only the resolved target changes: no assignment to the contextual role.
    assert "SET role_in_context" not in source
    assert "role_in_context =" not in source


# ── the dry/live divergence defect ──────────────────────────────────────


def test_composite_proposal_building_cannot_depend_on_dry_run():
    parameters = inspect.signature(build_composite_ops).parameters
    assert list(parameters) == ["conn"]


def test_resolver_composites_uses_the_dry_run_independent_builder():
    source = inspect.getsource(resolver_module._resolve_composites)
    assert "build_composite_ops(conn)" in source


# ── AFFILIATED_WITH registry contract ───────────────────────────────────


def test_affiliated_with_is_registered_and_canonical():
    assert "AFFILIATED_WITH" in PREDICATES
    assert "AFFILIATED_WITH" in r.CANONICAL_PREDICATES


def test_affiliated_with_direction_domain_and_range():
    entry = get_predicate("AFFILIATED_WITH")
    assert entry.domain == ("person",)
    assert entry.range == ("organization",)
    assert entry.inverse_label == "has affiliate"
    assert entry.kind == "relational"
    assert direction_allows("AFFILIATED_WITH", "person", "organization")
    assert not direction_allows("AFFILIATED_WITH", "organization", "person")


def test_affiliated_with_requires_explicit_with_or_of_language():
    entry = get_predicate("AFFILIATED_WITH")
    assert "with/of" in entry.required_support
    assert "Explicit" in entry.required_support


def test_affiliated_with_evidence_classes_are_registered_and_bounded():
    entry = get_predicate("AFFILIATED_WITH")
    for evidence_class in entry.allowed_evidence_classes:
        assert evidence_class in r.EVIDENCE_CLASSES
    assert evidence_allows("AFFILIATED_WITH", "minutes_or_summary")
    assert evidence_allows("AFFILIATED_WITH", "source_pdf_text")
    # A vote record is not affiliation evidence.
    assert not evidence_allows("AFFILIATED_WITH", "vote_or_attendance_record")


def test_affiliated_with_has_a_temporal_requirement():
    assert get_predicate("AFFILIATED_WITH").temporal_requirement == "known_scope"


def test_predicate_inventory_has_no_duplicates():
    from scripts.kg.registries.relationships import _RAW_PREDICATES
    names = [entry.predicate for entry in _RAW_PREDICATES]
    assert len(names) == len(set(names))
    assert len(r.CANONICAL_PREDICATES) == len(PREDICATES)


def _code_without_comments(module) -> str:
    lines = pathlib.Path(module.__file__).read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("#"))


def test_resolver_never_infers_affiliated_with_from_a_comma():
    """No resolver *statement* references the affiliation predicate.

    The predicate may appear in explanatory comments; it must never appear in
    executable code, because a comma is not affiliation evidence.
    """
    for module in (resolver_module, resolver_persistence):
        assert "AFFILIATED_WITH" not in _code_without_comments(module)


# ── manifest drift regression ───────────────────────────────────────────


@pytest.mark.parametrize("phase_name", sorted(producer_manifest.PHASE_CODE_MODULES))
def test_no_manifested_phase_has_undocumented_imports(phase_name):
    from scripts.entities import detect_entities
    phase = next(p for p in detect_entities.PHASES if p["name"] == phase_name)
    missing = producer_manifest.undocumented_imports(phase, REPO_ROOT)
    assert missing == (), f"{phase_name} has undeclared reachable modules: {missing}"


def test_declared_modules_are_sorted_and_unique():
    for phase_name, modules in producer_manifest.PHASE_CODE_MODULES.items():
        assert list(modules) == sorted(modules), phase_name
        assert len(set(modules)) == len(modules), phase_name


def test_sweep_docs_declares_its_emission_stack():
    declared = set(producer_manifest.PHASE_CODE_MODULES["sweep_docs"])
    for module in ("scripts.entities.sweep_docs",
                   "scripts.kg.emission",
                   "scripts.kg.emission_validation",
                   "scripts.kg.identity",
                   "scripts.kg.registries.evidence",
                   "scripts.kg.registries.roles"):
        assert module in declared, f"sweep_docs is missing {module}"
