"""Tests for the versioned information-model registries (Brief 018 Step 1).

Exit criteria: registry snapshots are deterministic; duplicate slugs, invalid
parents, cycles, missing inverses, invalid domain/range references, and
incomplete compatibility mappings each fail loudly.
"""

from __future__ import annotations

import pytest

from scripts.kg import registries as r
from scripts.kg.registries import entity_taxonomy, events, roles
from scripts.kg.registries.model import CompatibilityMapping, RegistryError
from scripts.kg.registries import validation as v


# --------------------------------------------------------------------------
# Canonical outcome contract: base outcome + optional controlled qualifier
# --------------------------------------------------------------------------


def test_canonicalize_outcome_documented_examples():
    assert events.canonicalize_outcome("approved_with_conditions") == (
        events.CanonicalOutcome("approved", "with_conditions")
    )
    assert events.canonicalize_outcome("approved") == (
        events.CanonicalOutcome("approved", None)
    )


def test_every_historical_compatibility_form_canonicalizes():
    assert events.OUTCOME_COMPATIBILITY, "compatibility map must not be empty"
    for raw, (base, qualifier) in events.OUTCOME_COMPATIBILITY.items():
        canonical = events.canonicalize_outcome(raw)
        assert canonical == events.CanonicalOutcome(base, qualifier), raw
        assert canonical.base == base
        assert canonical.qualifier == qualifier


def test_unqualified_base_outcomes_canonicalize_without_a_qualifier():
    for base in events.BASE_OUTCOMES:
        canonical = events.canonicalize_outcome(base)
        assert canonical.base == base
        assert canonical.qualifier is None
        assert canonical.is_qualified is False


def test_approved_permits_each_of_its_qualifiers():
    permitted = events.OUTCOME_QUALIFIERS["approved"]
    assert permitted
    for qualifier in permitted:
        # The pairing itself is permitted...
        assert events.CanonicalOutcome("approved", qualifier).qualifier == qualifier
        # ...and where a historical raw form exists it canonicalises to it.
        raw = f"approved_{qualifier}"
        if raw in events.OUTCOME_COMPATIBILITY:
            assert events.canonicalize_outcome(raw) == (
                events.CanonicalOutcome("approved", qualifier)
            )


def test_denied_permits_without_prejudice():
    assert events.canonicalize_outcome("denied_without_prejudice") == (
        events.CanonicalOutcome("denied", "without_prejudice")
    )


def test_invalid_cross_pairings_fail_closed():
    # approved does not permit without_prejudice.
    with pytest.raises(events.OutcomeError):
        events.canonicalize_outcome("approved_without_prejudice")
    with pytest.raises(events.OutcomeError):
        events.CanonicalOutcome("approved", "without_prejudice")
    # denied permits only without_prejudice.
    with pytest.raises(events.OutcomeError):
        events.CanonicalOutcome("denied", "with_conditions")
    with pytest.raises(events.OutcomeError):
        events.canonicalize_outcome("denied_with_conditions")


def test_a_qualifier_without_a_base_fails_closed():
    for raw in ("with_conditions", "without_prejudice", "_with_conditions"):
        with pytest.raises(events.OutcomeError):
            events.canonicalize_outcome(raw)
    with pytest.raises(events.OutcomeError):
        events.CanonicalOutcome("", "with_conditions")


def test_unknown_outcome_forms_fail_closed():
    for raw in ("", "   ", "bogus", "approved_with_bells", "APPROVAL"):
        with pytest.raises(events.OutcomeError):
            events.canonicalize_outcome(raw)


def test_qualified_form_is_not_reported_as_the_canonical_outcome():
    canonical = events.canonicalize_outcome("approved_with_conditions")
    assert canonical.base != "approved_with_conditions"
    assert canonical.serialize() == {
        "outcome": "approved",
        "outcome_qualifier": "with_conditions",
    }


def test_accepted_outcome_forms_keeps_qualified_historical_forms_readable():
    forms = events.accepted_outcome_forms("approved")
    assert "approved" in forms
    assert "approved_with_conditions" in forms
    assert "approved_with_stipulations" in forms
    assert "approved_subject_to" in forms
    assert "denied_without_prejudice" not in forms
    # Every accepted form canonicalises back to the queried base.
    for form in forms:
        assert events.canonicalize_outcome(form).base == "approved"


def test_registry_exports_expose_the_canonical_outcome_api():
    assert r.canonicalize_outcome is events.canonicalize_outcome
    assert r.CanonicalOutcome is events.CanonicalOutcome
    assert r.OutcomeError is events.OutcomeError


# --------------------------------------------------------------------------
# Shipped bundle
# --------------------------------------------------------------------------


def test_shipped_bundle_validates_and_is_versioned():
    r.validate_registries()
    assert r.MODEL_VERSION == "kg-model/1.0"


def test_snapshot_serialization_is_deterministic():
    first, second = r.snapshot_json(), r.snapshot_json()
    assert first == second
    assert r.snapshot_sha256() == r.snapshot_sha256()
    assert r.snapshot_sha256() == __import__("hashlib").sha256(
        first.encode("utf-8")
    ).hexdigest()


def test_bundle_sections_present():
    bundle = r.registry_bundle()
    for section in (
        "entity_taxonomy", "event_taxonomy", "outcomes", "roles",
        "participation", "relationships", "evidence", "compatibility",
    ):
        assert section in bundle


# --------------------------------------------------------------------------
# Required failure modes
# --------------------------------------------------------------------------


def test_duplicate_slugs_fail():
    problems = v.check_unique_slugs(["person", "person"], what="entity type")
    assert problems and "duplicate entity type" in problems[0]


def test_invalid_parent_fails():
    problems = v.check_parents({"firm": "organization"}, what="entity type")
    assert any("unknown parent" in problem for problem in problems)


def test_parent_cycle_fails():
    problems = v.check_parents({"a": "b", "b": "a"}, what="entity type")
    assert any("cycle" in problem for problem in problems)


def test_missing_inverse_fails():
    problems = v.check_inverses({"PART_OF": ""}, what="relationship predicate")
    assert any("missing an inverse label" in problem for problem in problems)


def test_duplicate_inverse_label_is_diagnostic_only():
    labels = {"OCCURRED_IN": "had event", "ABOUT": "had event"}
    assert v.check_inverses(labels, what="relationship predicate") == []
    assert v.duplicate_inverse_labels(labels) == ["had event: ABOUT, OCCURRED_IN"]


def test_invalid_domain_range_reference_fails():
    problems = v.check_domain_range(
        {"PART_OF": (("spaceship",), ("jurisdiction",))},
        r.NODE_CLASSES,
        what="relationship predicate",
    )
    assert any("unknown class spaceship" in problem for problem in problems)


def test_incomplete_compatibility_mapping_fails():
    problems = v.check_compatibility_completeness(
        [],
        required_values={"relationship": ("HAS_APPLICANT",)},
        categories={"relationship": ("APPLIED_FOR",)},
    )
    assert any("neither mapped nor quarantined" in problem for problem in problems)


def test_dangling_compatibility_target_fails():
    mapping = CompatibilityMapping(
        category="relationship", historical_value="HAS_OWNER",
        canonical_value="TOTALLY_MADE_UP", handling="rename", reason="test",
    )
    problems = v.check_compatibility_completeness(
        [mapping],
        required_values={"relationship": ("HAS_OWNER",)},
        categories={"relationship": ("OWNS",)},
    )
    assert any("unregistered value TOTALLY_MADE_UP" in problem for problem in problems)


def test_duplicate_compatibility_mapping_fails():
    mapping = CompatibilityMapping(
        category="role", historical_value="Applicant", canonical_value="applicant",
        handling="lowercase", reason="test",
    )
    problems = v.check_compatibility_completeness(
        [mapping, mapping],
        required_values={"role": ("Applicant",)},
        categories={"role": ("applicant",)},
    )
    assert any("duplicate compatibility mapping" in problem for problem in problems)


def test_validator_raises_on_broken_bundle():
    with pytest.raises(RegistryError):
        v.validate_registry_data(
            entity_types={"person": entity_taxonomy.ENTITY_TYPES["person"]},
            event_types={"decision": events.EVENT_TYPES["decision"]},
            roles={},
            predicates={},
            mappings=(),
            required_values={"entity_type": ("ghost_type",)},
            categories={"entity_type": ("person",)},
            node_classes=r.NODE_CLASSES,
            outcome_compatibility={},
            participation_bases=("agenda_listing",),
            assertion_classes=("derived", "source_supported"),
            non_source_classes=("derived",),
            actor_classes=r.ACTOR_CLASSES,
            context_classes=r.CONTEXT_CLASSES,
        )


# --------------------------------------------------------------------------
# Semantics
# --------------------------------------------------------------------------


def test_entity_leaf_policy_and_fallback():
    assert entity_taxonomy.is_leaf_compliant("organization") is True  # fallback
    assert entity_taxonomy.is_leaf_compliant("firm") is False  # has children
    assert entity_taxonomy.is_leaf_compliant("law_firm") is True
    assert entity_taxonomy.is_leaf_compliant("person") is True


def test_legacy_recommendation_is_quarantined_not_canonical():
    assert r.classify_value("entity_type", "recommendation") == "quarantined"
    assert "recommendation" not in r.ENTITY_TYPES


def test_historical_seed_slug_aliases_resolve():
    assert r.get_entity_type("organization.firm").slug == "firm"
    assert r.get_entity_type("organization.firm.law_firm").slug == "law_firm"


def test_base_outcome_query_is_inclusive_of_qualifiers():
    forms = r.accepted_outcome_forms("approved")
    assert "approved" in forms
    assert "approved_with_conditions" in forms
    assert "approved_with_stipulations" in forms
    assert r.split_outcome("approved_with_conditions") == ("approved", "with_conditions")
    assert r.split_outcome("approved") == ("approved", None)


def test_role_context_and_basis_constraints():
    assert roles.role_allows_context("applicant", "agenda_item") is True
    assert roles.role_allows_context("applicant", "body") is False
    assert roles.basis_forbids("agenda_listing", "attendance") is True
    assert roles.basis_forbids("agenda_listing", "completed_action") is True
    assert roles.basis_forbids("observed_action", "attendance") is False


def test_predicate_direction_and_evidence_contracts():
    assert r.PREDICATES["APPLIED_FOR"].domain == ("person", "organization")
    assert r.PREDICATES["APPLIED_FOR"].range == ("case",)
    assert r.PREDICATES["MEMBER_OF"].domain == ("person",)
    assert r.PREDICATES["MEMBER_OF"].range == ("body",)
    assert "structured_record" in r.PREDICATES["MEMBER_OF"].allowed_evidence_classes


def test_assertion_class_is_independent_of_edge_kind():
    assert set(r.EDGE_KINDS) == {"relational", "attributional"}
    assert "derived" not in r.EDGE_KINDS
    assert set(r.NON_SOURCE_CLASSES).issubset(set(r.ASSERTION_CLASSES))
    assert "relational" not in r.ASSERTION_CLASSES


def test_every_declared_historical_value_is_classified():
    for category, values in r.REQUIRED_COMPATIBILITY_VALUES.items():
        assert r.missing_mappings(category, values) == [], category


def test_documents_attach_rather_than_contain():
    """Documents are attachments, not containment children (review item 1)."""
    assert "document" not in r.PREDICATES["PART_OF"].range
    assert "document" not in r.PREDICATES["PART_OF"].domain
    assert r.PREDICATES["ATTACHED_TO"].domain == ("document",)
    assert "agenda_item" in r.PREDICATES["ATTACHED_TO"].range
    assert "meeting" in r.PREDICATES["ATTACHED_TO"].range


def test_agenda_subitems_are_modeled_as_containment_children():
    """Agenda subitems are part-of their parent item (review item 1)."""
    assert "agenda_subitem" in r.NODE_CLASSES
    assert "agenda_subitem" in r.PREDICATES["PART_OF"].domain
    assert "agenda_item" in r.PREDICATES["PART_OF"].range


def test_without_prejudice_qualifies_denial_only():
    """``without_prejudice`` belongs to denial, never approval (review item 2)."""
    assert "without_prejudice" in r.OUTCOME_QUALIFIERS["denied"]
    assert "without_prejudice" not in r.OUTCOME_QUALIFIERS["approved"]
    assert "denied_without_prejudice" in r.accepted_outcome_forms("denied")
    assert "approved_without_prejudice" not in r.accepted_outcome_forms("approved")
    assert r.split_outcome("denied_without_prejudice") == (
        "denied", "without_prejudice",
    )


def test_validator_rejects_qualifier_on_wrong_base():
    """A misplaced qualifier is caught structurally, not just by convention."""
    problems = v.check_outcome_compatibility(
        {"approved_without_prejudice": ("approved", "without_prejudice")},
        categories={"outcome": ("approved", "denied")},
        registered_qualifiers=events.QUALIFIERS,
        outcome_qualifier_map=events.OUTCOME_QUALIFIERS,
    )
    assert any("does not permit it" in problem for problem in problems)


def test_validator_accepts_correct_qualifier_base_pairing():
    problems = v.check_outcome_compatibility(
        {"denied_without_prejudice": ("denied", "without_prejudice")},
        categories={"outcome": ("approved", "denied")},
        registered_qualifiers=events.QUALIFIERS,
        outcome_qualifier_map=events.OUTCOME_QUALIFIERS,
    )
    assert problems == []


def test_validator_rejects_unregistered_qualifier_in_map():
    problems = v.check_outcome_compatibility(
        {}, categories={"outcome": ("denied",)},
        registered_qualifiers=events.QUALIFIERS,
        outcome_qualifier_map={"denied": ("without_prejudice", "made_up_qualifier")},
    )
    assert any("unregistered qualifier made_up_qualifier" in p for p in problems)


def test_producer_declared_vocabulary_is_covered():
    """Coverage is asserted against live producer declarations, not a copy.

    This is the drift guard for review item 3: if a producer adds vocabulary
    that the registries do not classify, this fails.
    """
    from scripts.kg import producer_vocabulary as producers

    declared = producers.declared_vocabulary()
    assert declared, "no producer vocabulary was discovered"
    for category in ("role", "relationship", "outcome", "entity_type", "event_type"):
        for declaration in declared.get(category, ()):
            value = declaration.value
            if category == "event_type":
                value = r.normalize_event_slug(value)
            assert r.classify_value(category, value) != "unmapped", (
                f"{declaration.source} declares {category} '{declaration.value}' "
                "which the registries do not classify"
            )


def test_producer_introspection_is_live_not_a_copy(monkeypatch):
    """Introspection reads producer attributes live rather than copying a list.

    ``HAS_OWNER`` is no longer declared by any producer (the pattern cascade now
    emits only canonical vocabulary), so liveness is proved directly instead:
    changing a producer attribute must change the introspected value set.
    """
    from scripts.kg import producer_vocabulary as producers
    from scripts.entities import pattern_cascade

    assert "HAS_OWNER" not in r.PREDICATES  # never canonical

    live = producers.declared_values("relationship")
    assert live["APPLIED_FOR"] == ["pattern_cascade.ROLE_EDGE_MAP"]

    monkeypatch.setattr(pattern_cascade, "ROLE_EDGE_MAP",
                        {"applicant": "CONCERNS"})
    changed = producers.declared_values("relationship")
    assert "CONCERNS" in changed, "introspection did not follow the live attribute"
    assert "APPLIED_FOR" not in changed, "introspection served a stale copy"


def test_introspection_limits_are_declared_with_reasons():
    """An introspection limit is an implementation note, not a coverage claim."""
    from scripts.kg import producer_vocabulary as producers

    assert set(producers.INTROSPECTION_LIMITS) == {
        "role_classifier", "event_link", "graph_builder_sources",
        "graph_builder_materialization",
    }
    assert all(reason for reason in producers.INTROSPECTION_LIMITS.values())


def test_introspection_limit_is_not_an_emission_exemption():
    """Producers limited by introspection still require receipts."""
    from scripts.kg import producer_coverage as coverage
    from scripts.kg import producer_vocabulary as producers

    limited = set(producers.INTROSPECTION_LIMITS)
    exempt = set(coverage.exempt_producers())
    assert limited & exempt == set(), (
        "an introspection limit must never exempt a producer from receipts"
    )
