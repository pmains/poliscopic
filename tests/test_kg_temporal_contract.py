"""Three-clock temporal contract tests (Stage 1 exit criterion 3).

Pure and offline.  The contract is exercised through its own public API, so the
tests never restate a second clock or field list — a duplicated list is exactly
what this contract exists to prevent.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.kg.registries import temporal
from scripts.kg.registries.model import RegistryError


# -- the three clocks exist ----------------------------------------------------

def test_all_three_clocks_exist():
    slugs = {clock.slug for clock in temporal.CLOCKS}
    assert slugs == {"valid_time", "source_observation_time", "ingestion_system_time"}
    assert set(temporal.CLOCK_BY_SLUG) == slugs


def test_exactly_one_clock_bounds_validity():
    bounding = [clock for clock in temporal.CLOCKS if clock.defines_validity_bounds]
    assert len(bounding) == 1
    assert bounding[0].slug == "valid_time"


def test_ingestion_clock_is_immutable_or_versioned():
    clock = temporal.CLOCK_BY_SLUG["ingestion_system_time"]
    assert clock.immutable_or_versioned is True
    assert clock.planned_note, "the unclaimed column must be explained, not implied"


def test_shipped_contract_validates_clean():
    assert temporal.validate_temporal_contract() == []


# -- fields are distinct -------------------------------------------------------

def test_clock_fields_are_distinct_across_and_within_clocks():
    seen: dict[tuple[str, str], str] = {}
    for slug, field in temporal.iter_declared_fields():
        key = field.as_key()
        assert key not in seen, f"{key} declared by both {seen.get(key)} and {slug}"
        seen[key] = slug


def test_duplicate_field_across_clocks_is_rejected():
    first, second, third = temporal.CLOCKS
    collided = replace(second, fields=first.fields)
    problems = temporal.validate_temporal_contract(clocks=(first, collided, third))
    assert any("declared by both" in problem for problem in problems)


# -- observation is not validity ----------------------------------------------

def test_validity_is_bounded_only_by_the_valid_pair():
    columns = [field.column for field in temporal.validity_bound_fields()]
    assert columns == ["valid_from", "valid_to"]
    assert temporal.is_validity_bound("valid_from")
    assert temporal.is_validity_bound("valid_to")


@pytest.mark.parametrize("column", ["observed_at", "first_observed_at", "last_observed_at"])
def test_observation_timestamps_cannot_be_treated_as_validity_bounds(column):
    assert not temporal.is_validity_bound(column)
    with pytest.raises(RegistryError) as error:
        temporal.require_validity_bound(column)
    assert "observation timestamp" in str(error.value)


def test_validity_bounds_are_not_observation_fields():
    with pytest.raises(RegistryError):
        temporal.require_observation_field("valid_from")


def test_summary_timestamps_do_not_imply_validity():
    observation = temporal.CLOCK_BY_SLUG["source_observation_time"]
    summary_columns = {field.column for field in observation.summary_fields}
    assert summary_columns == {"first_observed_at", "last_observed_at"}
    assert observation.defines_validity_bounds is False
    assert not summary_columns & {f.column for f in temporal.validity_bound_fields()}
    for field in observation.summary_fields:
        assert "not a validity bound" in field.note


def test_validity_clock_may_not_carry_summary_fields():
    valid = temporal.CLOCK_BY_SLUG["valid_time"]
    broken = replace(valid, summary_fields=temporal.observation_fields())
    problems = temporal.validate_temporal_contract(clocks=(broken,) + temporal.CLOCKS[1:])
    assert any("must not also carry summary fields" in problem for problem in problems)


def test_a_non_validity_clock_may_not_declare_valid_fields():
    valid, observation, ingestion = temporal.CLOCKS
    broken = replace(observation, defines_validity_bounds=False, fields=valid.fields)
    problems = temporal.validate_temporal_contract(clocks=(valid, broken, ingestion))
    assert any("valid_*" in problem for problem in problems)


# -- presence is asked, never asserted ----------------------------------------

def test_presence_is_derived_from_the_schema_contract_not_restated():
    from scripts.entities.schema_parity import REQUIRED_COLUMNS

    mapping = temporal.schema_mapping()
    derived = {
        f"{field.table}.{field.column}"
        for _, field in temporal.iter_declared_fields()
        if field.column in REQUIRED_COLUMNS.get(field.table, frozenset())
    }
    assert set(mapping["present"]) == derived
    assert set(mapping["present"]) | set(mapping["planned"]) == {
        f"{field.table}.{field.column}" for _, field in temporal.iter_declared_fields()
    }
    assert not set(mapping["present"]) & set(mapping["planned"])


def test_every_current_field_is_actually_present():
    mapping = temporal.schema_mapping()
    for clock in temporal.CLOCKS:
        for entry in mapping["clocks"][clock.slug]["fields"]:
            assert entry["status"] == "present", entry


def test_planned_fields_are_not_claimed_as_existing():
    mapping = temporal.schema_mapping()
    for clock in temporal.CLOCKS:
        section = mapping["clocks"][clock.slug]
        for key in ("summary_fields", "planned_fields"):
            for entry in section[key]:
                assert entry["status"] == "planned", entry


def test_a_planned_field_declared_as_current_is_rejected():
    """The guard that stops a planned column being claimed as existing."""
    valid, observation, ingestion = temporal.CLOCKS
    promoted = replace(observation, fields=observation.summary_fields, summary_fields=())
    problems = temporal.validate_temporal_contract(clocks=(valid, promoted, ingestion))
    assert any(
        "as a current field, but the schema contract does not require it" in problem
        for problem in problems
    )


def test_a_present_field_hidden_under_planned_is_rejected():
    valid, observation, ingestion = temporal.CLOCKS
    demoted = replace(observation, fields=(), summary_fields=observation.fields)
    problems = temporal.validate_temporal_contract(clocks=(valid, demoted, ingestion))
    assert any("the schema contract already requires it" in problem for problem in problems)


# -- single source of truth ----------------------------------------------------

def test_requirement_tokens_are_derived_from_the_predicates():
    """The token vocabulary belongs to the predicates; the contract only classifies."""
    from scripts.kg.registries.relationships import PREDICATES

    declared = {entry.temporal_requirement for entry in PREDICATES.values()}
    assert set(temporal.declared_temporal_requirements()) == declared
    assert set(temporal.temporal_requirement_clocks()) == declared


def test_unclassified_predicate_token_is_rejected():
    problems = temporal.validate_temporal_contract(
        declared_tokens=tuple(temporal.declared_temporal_requirements()) + ("brand_new",))
    assert any("brand_new" in problem and "not classified" in problem
               for problem in problems)


def test_classification_of_a_token_no_predicate_declares_is_rejected():
    requirements = dict(temporal.temporal_requirement_clocks())
    requirements["never_used"] = None
    problems = temporal.validate_temporal_contract(requirements=requirements)
    assert any("never_used" in problem and "no predicate declares" in problem
               for problem in problems)


def test_unknown_clock_in_a_classification_is_rejected():
    requirements = dict(temporal.temporal_requirement_clocks())
    requirements["known_scope"] = "not_a_clock"
    problems = temporal.validate_temporal_contract(requirements=requirements)
    assert any("unknown clock" in problem for problem in problems)


# -- clock fields are metadata, not ontology -----------------------------------

def test_clock_fields_are_not_ontology_vocabulary():
    assert temporal.ontology_conflicts() == []


def test_an_ontology_colliding_field_name_is_rejected():
    from scripts.kg.registries.entity_taxonomy import ENTITY_TYPES

    valid, observation, ingestion = temporal.CLOCKS
    collision = sorted(ENTITY_TYPES)[0]
    collided = replace(valid, fields=(
        replace(valid.fields[0], column=collision), valid.fields[1]))
    problems = temporal.validate_temporal_contract(clocks=(collided, observation, ingestion))
    assert any("ontology vocabulary" in problem for problem in problems)


# -- registry wiring -----------------------------------------------------------

def test_registry_bundle_exposes_the_contract():
    from scripts.kg import registries

    bundle = registries.registry_bundle()
    assert "temporal_contract" in bundle
    assert bundle["temporal_contract"]["clocks"]
    assert registries.validate_temporal_contract() == []


def test_registry_validation_asserts_the_temporal_contract_at_import():
    from scripts.kg import registries

    registries.assert_temporal_contract()  # does not raise
    assert registries.CLOCK_BY_SLUG["valid_time"].defines_validity_bounds is True


def test_contract_serialization_is_deterministic():
    first, second = temporal.temporal_contract(), temporal.temporal_contract()
    assert first == second
    assert first["schema_mapping"] == temporal.schema_mapping()
    assert first["note"]
