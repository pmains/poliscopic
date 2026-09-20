"""Isolated tests for the corrected emission boundary, coverage, and taxonomy.

Nothing here touches a database.  These exercise registry classification, the
lifecycle state machine, and the coverage authority.
"""

from __future__ import annotations

import pytest

from scripts.kg import emission as e
from scripts.kg import producer_coverage as coverage
from scripts.kg.registries import entity_taxonomy as taxonomy


def validator(**kwargs):
    return e.EmissionValidator("test_producer", "1.0", **kwargs)


def collecting(**kwargs):
    """A validator left in the collecting state, where validation is legal."""
    return validator(**kwargs)


# ---------------------------------------------------------------------------
# Canonical emissions
# ---------------------------------------------------------------------------


def test_canonical_values_are_accepted_and_observed():
    v = collecting()
    assert v.entity_type("person") == "person"
    assert v.role("applicant", context_class="agenda_item") == "applicant"
    assert v.participation_basis("agenda_listing") == "agenda_listing"
    assert v.relationship("APPLIED_FOR", from_class="person", to_class="case") == "APPLIED_FOR"
    assert v.event_type("decision.approval") == "approval"
    # A qualified outcome is reported as its canonical base at value level; the
    # qualifier is validated and observed separately.
    assert v.outcome("approved_with_conditions") == "approved"
    assert v.evidence_class("source_pdf_text") == "source_pdf_text"
    assert v.assertion_class("source_supported") == "source_supported"
    assert v.model_version("kg-model/1.0") == "kg-model/1.0"
    receipt = v.seal()
    assert receipt.values_attempted == receipt.values_accepted == 9
    assert receipt.values_rejected == 0
    assert receipt.values_reconcile
    assert receipt.observed["entity_type"] == {"person": 1}
    assert receipt.observed["event_type"] == {"approval": 1}


@pytest.mark.parametrize(
    "category,value",
    [
        ("entity_type", "ghost_type"),
        ("role", "definitely_not_a_role"),
        ("participation_basis", "vibes"),
        ("relationship", "HAS_VIBES"),
        ("event_type", "ghost_event"),
        ("outcome", "ghost_outcome"),
        ("evidence_class", "ghost_evidence"),
        ("assertion_class", "ghost_assertion"),
        ("model_version", "kg-model/9.9"),
    ],
)
def test_every_category_rejects_unknown_values(category, value):
    v = collecting()
    assert v.validate(category, value) is None
    receipt = v.seal()
    assert receipt.values_rejected == 1
    assert receipt.values_attempted == receipt.values_accepted + receipt.values_rejected
    assert len(receipt.rejections) == 1
    assert receipt.rejections[0].category == category


def test_rejections_carry_source_identity():
    v = collecting()
    v.validate("entity_type", "ghost", source="entities:row42")
    assert v.seal().rejections[0].source == "entities:row42"


# ---------------------------------------------------------------------------
# Direction, prohibited values, qualifier pairing
# ---------------------------------------------------------------------------


def test_invalid_predicate_direction_fails_before_writes():
    v = collecting()
    assert v.validate(
        "relationship", "APPLIED_FOR", from_class="case", to_class="person"
    ) is None
    assert "does not allow" in v.seal().rejections[0].reason


def test_non_canonical_predicate_is_refused():
    v = collecting()
    assert v.validate("relationship", "HAS_APPLICANT") is None
    assert "not canonical" in v.seal().rejections[0].reason


def test_recommendation_entity_and_predicate_are_prohibited():
    v = collecting()
    assert v.validate("entity_type", "recommendation") is None
    assert v.validate("relationship", "HAS_RECOMMENDATION") is None
    receipt = v.seal()
    assert receipt.values_rejected == 2
    assert "prohibited for new emission" in receipt.rejections[0].reason


def test_prohibited_values_are_still_readable_historically():
    from scripts.kg import registries as r

    assert r.classify_value("entity_type", "recommendation") == "quarantined"
    assert "recommendation" not in r.ENTITY_TYPES


def test_outcome_qualifier_pairing_is_enforced():
    v = collecting()
    assert v.outcome("denied_without_prejudice") == "denied"
    assert v.validate("outcome", "approved_without_prejudice") is None
    with pytest.raises(e.EmissionError):
        v.outcome("approved_with_without_prejudice")
    assert "does not permit" in v.seal().rejections[0].reason


def test_canonicalize_outcome_is_the_single_authority():
    from scripts.kg.registries import events

    assert events.canonicalize_outcome("approved_with_conditions") == (
        events.CanonicalOutcome("approved", "with_conditions")
    )
    assert events.canonicalize_outcome("approved") == (
        events.CanonicalOutcome("approved", None)
    )
    # The value-level check returns the canonical base, never the raw form.
    assert collecting().outcome("approved_with_conditions") == "approved"
    assert collecting().outcome_qualifier("with_conditions") == "with_conditions"


def test_unknown_outcome_forms_fail_closed_at_value_level():
    v = collecting()
    with pytest.raises(e.EmissionError):
        v.outcome("ghost_outcome")
    with pytest.raises(e.EmissionError):
        v.outcome("approved_with_bells")
    with pytest.raises(e.EmissionError):
        v.outcome_qualifier("ghost_qualifier")
    with pytest.raises(e.EmissionError):
        v.outcome_qualifier("")


def test_a_qualifier_without_a_base_fails_closed_at_value_level():
    v = collecting()
    with pytest.raises(e.EmissionError):
        v.outcome("with_conditions")
    with pytest.raises(e.EmissionError):
        v.outcome("approved_with_without_prejudice")


def test_role_context_constraint_is_enforced():
    v = collecting()
    assert v.validate("role", "applicant", context_class="body") is None
    assert "not permitted in context" in v.seal().rejections[0].reason


# ---------------------------------------------------------------------------
# Assertion classes
# ---------------------------------------------------------------------------


def test_derived_assertion_cannot_be_emitted_as_source_supported():
    v = collecting()
    assert v.validate("assertion_class", "derived", source_supported=True) is None
    assert "must not be emitted as source-supported" in v.seal().rejections[0].reason


def test_basis_promotion_rules_come_from_the_registry():
    from scripts.kg.registries.roles import basis_forbids

    assert basis_forbids("agenda_listing", "attendance") is True
    assert basis_forbids("scheduled_role", "completed_action") is True
    assert basis_forbids("observed_attendance", "completed_action") is True
    assert basis_forbids("observed_action", "completed_action") is False


def test_identity_layer_refuses_derived_serialized_as_source():
    from scripts.kg import identity as ident

    meeting = ident.entity_candidate_identity(entity_type="meeting", surface_form="m")
    assertion = ident.co_occurrence_assertion(
        ident.entity_candidate_identity(entity_type="person", surface_form="p"),
        "PARTICIPATED_IN", meeting, context=meeting,
        inputs=(meeting,), observed_at="2026-01-07T00:00:00-07:00",
    )
    assert ident.serialize_assertion(assertion)["source_supported"] is False
    with pytest.raises(ident.IdentityError):
        ident.serialize_assertion(assertion, as_source_supported=True)


# ---------------------------------------------------------------------------
# Reconciliation (corrected receipt shape)
# ---------------------------------------------------------------------------


def _receipt(**overrides):
    base = {
        "producer": "p", "producer_version": "1",
        "model_version": "kg-model/1.0", "registry_snapshot": "snap",
        "state": "sealed", "dry_run": False, "failure": None,
        "values": {"attempted": 1, "accepted": 1, "rejected": 0},
        "rows": {"proposed": 1, "would_insert": 1, "would_update": 0,
                 "replay_noop": 0, "unresolved": 0,
                 "committed": 1, "rolled_back": 0},
        "observed": {}, "rejections": [],
        "derived_excluded": 0, "derived_exclusion_reasons": [],
    }
    base.update(overrides)
    return base


def reconcile(**overrides):
    return e.reconcile_receipts(
        [_receipt(**overrides)], expected_producers=["p"],
        model_version="kg-model/1.0", registry_snapshot="snap",
    )


def test_clean_receipt_reconciles():
    assert reconcile() == []


def test_missing_receipt_fails():
    problems = e.reconcile_receipts(
        [], expected_producers=["p"],
        model_version="kg-model/1.0", registry_snapshot="snap",
    )
    assert any("missing validation receipt" in p for p in problems)


def test_model_version_mismatch_fails():
    assert any("model version" in p for p in reconcile(model_version="kg-model/0.9"))


def test_registry_snapshot_mismatch_fails():
    assert any("registry snapshot" in p for p in reconcile(registry_snapshot="other"))


def test_value_accounting_must_reconcile():
    problems = reconcile(
        values={"attempted": 5, "accepted": 1, "rejected": 1},
    )
    assert any("value accounting does not reconcile" in p for p in problems)


def test_row_classification_must_reconcile_exactly():
    problems = reconcile(
        rows={"proposed": 10, "would_insert": 1, "would_update": 0,
              "replay_noop": 0, "unresolved": 0,
              "committed": 1, "rolled_back": 0},
    )
    assert any("classification does not reconcile exactly" in p for p in problems)


def test_row_mutation_must_reconcile_exactly():
    problems = reconcile(
        rows={"proposed": 1, "would_insert": 1, "would_update": 0,
              "replay_noop": 0, "unresolved": 0,
              "committed": 2, "rolled_back": 0},
    )
    assert any("mutation does not reconcile exactly" in p for p in problems)


def test_silently_discarded_rejects_fail():
    problems = reconcile(
        values={"attempted": 2, "accepted": 1, "rejected": 1}, rejections=[],
    )
    assert any("silently discarded" in p for p in problems)


def test_unsealed_receipt_fails():
    assert any("not sealed" in p for p in reconcile(state="writing"))


def test_dry_run_writes_fail():
    problems = reconcile(dry_run=True, rows={"proposed": 1, "would_insert": 1,
                                             "would_update": 0, "replay_noop": 0,
                                             "unresolved": 0, "committed": 1,
                                             "rolled_back": 0})
    assert any("dry run mutated rows" in p for p in problems)


def test_failure_is_surfaced_first():
    problems = reconcile(failure="OperationalError: boom")
    assert "run failed" in problems[0]


def test_prohibited_values_in_observed_fail_reconciliation():
    problems = reconcile(observed={"entity_type": {"recommendation": 1}})
    assert any("prohibited entity type recommendation" in p for p in problems)


def test_quarantined_observed_value_fails_reconciliation():
    problems = reconcile(observed={"role": {"known_org": 1}})
    assert any("is quarantined" in p for p in problems)


def test_duplicate_receipt_fails():
    problems = e.reconcile_receipts(
        [_receipt(), _receipt()], expected_producers=["p"],
        model_version="kg-model/1.0", registry_snapshot="snap",
    )
    assert any("duplicate receipt" in p for p in problems)


# ---------------------------------------------------------------------------
# Coverage authority
# ---------------------------------------------------------------------------


def test_all_six_phases_are_covered_and_require_receipts():
    from scripts.entities.detect_entities import PHASES

    phases = [phase["name"] for phase in PHASES]
    assert coverage.coverage_problems(phases) == []
    assert set(coverage.producers_requiring_receipts()) == set(phases)
    assert coverage.exempt_producers() == ()


def test_coverage_flags_an_uncovered_phase():
    problems = coverage.coverage_problems(["graph_builder", "brand_new_phase"])
    assert any("brand_new_phase has no producer coverage entry" in p for p in problems)


def test_preflight_detects_stale_coverage_declarations():
    declared = {name: True for name in coverage.PRODUCER_COVERAGE}
    assert coverage.preflight_declarations(declared) == []
    declared["graph_builder"] = False
    problems = coverage.preflight_declarations(declared)
    assert any("graph_builder declares emits_ontology=False" in p for p in problems)


def test_preflight_requires_every_producer_to_declare():
    problems = coverage.preflight_declarations({})
    assert any("declares no emission capability" in p for p in problems)


# ---------------------------------------------------------------------------
# Taxonomy traversal (inert until Brief 016 adopts it)
# ---------------------------------------------------------------------------


def test_organization_fallback_remains_valid():
    assert taxonomy.FALLBACK_SLUG == "organization"
    assert taxonomy.is_leaf_compliant("organization") is True


def test_traversal_finds_every_organizational_subtype():
    expected = {
        "advocacy_group", "agency", "department", "developer", "firm",
        "law_firm", "planning_firm", "utility", "vendor",
    }
    assert set(taxonomy.descendants("organization")) == expected
    assert set(taxonomy.ORGANIZATION_TYPES) == expected | {"organization"}


def test_traversal_does_not_cross_the_tree():
    assert taxonomy.is_a("law_firm", "organization") is True
    assert taxonomy.is_a("law_firm", "firm") is True
    assert taxonomy.is_a("person", "organization") is False
    assert taxonomy.is_a("case", "organization") is False


def test_traversal_accepts_historical_dotted_slugs():
    assert taxonomy.is_a("organization.firm.law_firm", "organization") is True


def test_hardcoded_union_would_have_missed_subtypes():
    old_union = {"organization", "developer", "planning_firm", "law_firm"}
    assert set(taxonomy.ORGANIZATION_TYPES) - old_union == {
        "advocacy_group", "agency", "department", "firm", "utility", "vendor",
    }
