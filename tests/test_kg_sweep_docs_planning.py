"""Unit tests for the pure sweep_docs classification-plan module.

These tests exercise planning in isolation: no database, no SQLAlchemy, no
validator, no receipt.  Every input is an in-memory snapshot.
"""

from __future__ import annotations

import pathlib

import pytest

from scripts.entities.sweep_docs_planning import (
    ClassificationPlan,
    EntityAssertion,
    ExtractedCandidate,
    MentionAssertion,
    PlanInvariantError,
    build_classification_plan,
)

MODULE_SOURCE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "entities"
    / "sweep_docs_planning.py"
)


def candidate(
    name: str, entity_type: str, role: str, source_id: int
) -> ExtractedCandidate:
    return ExtractedCandidate(
        normalized_name=name,
        entity_type=entity_type,
        role=role,
        source_id=source_id,
    )


def plan_for(*candidates, existing_entities=(), existing_mentions=()):
    return build_classification_plan(
        candidates,
        existing_entities=existing_entities,
        existing_mentions=existing_mentions,
    )


# 1 -- new entity and mention -------------------------------------------------


def test_new_entity_and_mention_are_both_inserts():
    plan = plan_for(candidate("acme llc", "organization", "mentioned", 1))

    assert plan.total_proposed == 2
    assert plan.total_would_insert == 2
    assert plan.total_replay_noop == 0
    assert [a.identity for a in plan.entity_inserts] == [("acme llc", "organization")]
    assert [m.identity for m in plan.mention_inserts] == [
        ("acme llc", "organization", "supporting_document", 1, "mentioned")
    ]
    assert plan.entity_replay_noops == ()
    assert plan.mention_replay_noops == ()


# 2 -- unchanged replay -------------------------------------------------------


def test_unchanged_replay_yields_two_replay_noops():
    entity = EntityAssertion("acme llc", "organization", entity_id=7)
    mention = MentionAssertion(entity=entity, source_id=1, role="mentioned")

    plan = plan_for(
        candidate("acme llc", "organization", "mentioned", 1),
        existing_entities=[entity],
        existing_mentions=[mention],
    )

    assert plan.total_proposed == 2
    assert plan.total_would_insert == 0
    assert plan.total_replay_noop == 2
    assert plan.entity_inserts == ()
    assert plan.mention_inserts == ()
    assert [a.identity for a in plan.entity_replay_noops] == [
        ("acme llc", "organization")
    ]
    # The known canonical id travels with the replay classification.
    assert plan.entity_replay_noops[0].entity_id == 7


# 3 -- existing entity with new mention ---------------------------------------


def test_existing_entity_with_new_mention_is_one_replay_one_insert():
    entity = EntityAssertion("acme llc", "organization", entity_id=7)

    plan = plan_for(
        candidate("acme llc", "organization", "mentioned", 2),
        existing_entities=[entity],
    )

    assert plan.total_proposed == 2
    assert plan.total_would_insert == 1
    assert plan.total_replay_noop == 1
    assert len(plan.entity_replay_noops) == 1
    assert len(plan.mention_inserts) == 1
    assert plan.mention_inserts[0].source_id == 2


# 4 -- duplicate raw candidates -----------------------------------------------


def test_duplicate_raw_candidates_collapse():
    repeated = [
        candidate("acme llc", "organization", "mentioned", 1),
        candidate("acme llc", "organization", "mentioned", 1),
        candidate("acme llc", "organization", "mentioned", 1),
    ]
    plan = plan_for(*repeated)

    assert plan.total_proposed == 2
    assert plan.total_would_insert == 2
    assert len(plan.entity_inserts) == 1
    assert len(plan.mention_inserts) == 1


def test_duplicate_entities_collapse_but_distinct_mentions_survive():
    plan = plan_for(
        candidate("acme llc", "organization", "applicant", 1),
        candidate("acme llc", "organization", "applicant", 1),
        candidate("acme llc", "organization", "owner", 1),
    )

    assert len(plan.entity_inserts) == 1
    assert len(plan.mention_inserts) == 2
    assert plan.total_proposed == 3


# 5 -- same entity in two documents -------------------------------------------


def test_same_entity_in_two_documents_is_one_entity_two_mentions():
    plan = plan_for(
        candidate("acme llc", "organization", "mentioned", 1),
        candidate("acme llc", "organization", "mentioned", 2),
    )

    assert len(plan.entity_inserts) == 1
    assert len(plan.mention_inserts) == 2
    assert {m.source_id for m in plan.mention_inserts} == {1, 2}
    assert plan.total_proposed == 3


# 6 -- same entity with two roles ---------------------------------------------


def test_same_entity_with_two_roles_is_two_mentions():
    plan = plan_for(
        candidate("jane doe", "person", "applicant", 1),
        candidate("jane doe", "person", "attorney", 1),
    )

    assert len(plan.entity_inserts) == 1
    assert len(plan.mention_inserts) == 2
    assert {m.role for m in plan.mention_inserts} == {"applicant", "attorney"}
    assert plan.total_proposed == 3


# 7 -- input-order independence -----------------------------------------------


def test_row_order_does_not_affect_the_plan():
    forward = [
        candidate("acme llc", "organization", "mentioned", 1),
        candidate("jane doe", "person", "applicant", 2),
        candidate("acme llc", "organization", "owner", 1),
    ]
    backward = list(reversed(forward))

    entity = EntityAssertion("acme llc", "organization", entity_id=7)
    existing = MentionAssertion(entity=entity, source_id=1, role="owner")

    plan_a = plan_for(*forward, existing_entities=[entity], existing_mentions=[existing])
    plan_b = plan_for(*backward, existing_entities=[entity], existing_mentions=[existing])

    assert plan_a == plan_b
    assert plan_a.total_proposed == plan_b.total_proposed == 5
    assert plan_a.total_replay_noop == 2
    assert plan_a.total_would_insert == 3


def test_existing_entity_order_does_not_affect_the_plan():
    first = EntityAssertion("acme llc", "organization", entity_id=7)
    second = EntityAssertion("beta corp", "organization", entity_id=9)
    candidates = [
        candidate("acme llc", "organization", "mentioned", 1),
        candidate("beta corp", "organization", "mentioned", 2),
    ]

    plan_a = plan_for(*candidates, existing_entities=[first, second])
    plan_b = plan_for(*candidates, existing_entities=[second, first])

    assert plan_a == plan_b
    assert plan_a.total_replay_noop == 2
    assert plan_a.total_would_insert == 2


# 8 -- exact identity classification invariants -------------------------------


def test_every_unique_assertion_appears_in_exactly_one_classification():
    entity = EntityAssertion("acme llc", "organization", entity_id=7)
    existing = MentionAssertion(entity=entity, source_id=1, role="mentioned")

    plan = plan_for(
        candidate("acme llc", "organization", "mentioned", 1),
        candidate("acme llc", "organization", "mentioned", 1),
        candidate("acme llc", "organization", "owner", 3),
        candidate("beta corp", "organization", "mentioned", 4),
        existing_entities=[entity],
        existing_mentions=[existing],
    )

    plan.check_invariants()
    assert plan.total_proposed == plan.total_would_insert + plan.total_replay_noop
    # 2 unique entities (acme replay, beta insert) + 3 unique mentions
    # (acme/1/mentioned replay, acme/3/owner insert, beta/4/mentioned insert).
    assert plan.total_proposed == 5
    assert plan.total_would_insert == 3
    assert plan.total_replay_noop == 2

    for assertion in (*plan.entity_inserts, *plan.entity_replay_noops):
        assert plan.classification_of(assertion) in {"insert", "replay_noop"}
    for assertion in (*plan.mention_inserts, *plan.mention_replay_noops):
        assert plan.classification_of(assertion) in {"insert", "replay_noop"}


def test_invariant_violation_is_detected():
    entity = EntityAssertion("acme llc", "organization")
    broken = ClassificationPlan(
        entity_inserts=(entity,),
        entity_replay_noops=(entity,),
    )
    with pytest.raises(PlanInvariantError, match="classified twice"):
        broken.check_invariants()


def test_mention_referencing_entity_absent_from_plan_is_rejected():
    orphan = MentionAssertion(
        entity=EntityAssertion("ghost corp", "organization"),
        source_id=1,
        role="mentioned",
    )
    broken = ClassificationPlan(mention_inserts=(orphan,))
    with pytest.raises(PlanInvariantError, match="absent from the plan"):
        broken.check_invariants()


def test_mention_for_entity_created_in_same_plan_is_an_insert():
    """Even when a *different* mention of that name already exists."""
    existing_entity = EntityAssertion("acme llc", "organization", entity_id=7)
    stale_mention = MentionAssertion(
        entity=EntityAssertion("acme llc", "disambiguation_placeholder"),
        source_id=1,
        role="mentioned",
    )
    plan = plan_for(
        candidate("acme llc", "organization", "mentioned", 1),
        existing_entities=[existing_entity],
        existing_mentions=[stale_mention],
    )
    # The mention for the organization assertion is not the stale one.
    assert len(plan.mention_inserts) == 1
    assert len(plan.mention_replay_noops) == 0


def test_reclassify_moves_exactly_one_named_assertion():
    plan = plan_for(
        candidate("acme llc", "organization", "mentioned", 1),
        existing_entities=[EntityAssertion("beta corp", "organization", entity_id=9)],
    )

    target = EntityAssertion("acme llc", "organization")
    assert plan.classification_of(target) == "insert"

    revised = plan.reclassify_as_replay(target)

    assert revised.classification_of(target) == "replay_noop"
    assert revised.total_proposed == plan.total_proposed
    assert revised.total_replay_noop == plan.total_replay_noop + 1
    assert revised.total_would_insert == plan.total_would_insert - 1
    revised.check_invariants()
    # The original plan is untouched.
    assert plan.classification_of(target) == "insert"


def test_reclassify_requires_an_exact_pending_insert():
    plan = plan_for(candidate("acme llc", "organization", "mentioned", 1))
    with pytest.raises(PlanInvariantError, match="not a pending insert"):
        plan.reclassify_as_replay(EntityAssertion("beta corp", "organization"))


def test_reclassify_mention_by_exact_identity():
    plan = plan_for(candidate("acme llc", "organization", "mentioned", 1))
    target = plan.mention_inserts[0]
    revised = plan.reclassify_as_replay(target)

    assert revised.mention_inserts == ()
    assert len(revised.mention_replay_noops) == 1
    revised.check_invariants()


def test_unknown_assertion_lookup_is_rejected():
    plan = plan_for(candidate("acme llc", "organization", "mentioned", 1))
    with pytest.raises(PlanInvariantError, match="not present in this plan"):
        plan.classification_of(EntityAssertion("nobody", "person"))


# -- candidate adapters and purity -------------------------------------------


def test_from_mapping_reads_extractor_candidate_shape():
    mapped = ExtractedCandidate.from_mapping(
        {
            "normalized": "acme llc",
            "entity_type": "organization",
            "role": "mentioned",
            "_source_id": 42,
        }
    )
    assert mapped.normalized_name == "acme llc"
    assert mapped.source_id == 42
    assert mapped.source_type == "supporting_document"

    plan = build_classification_plan([mapped])
    assert plan.total_would_insert == 2


def test_assertions_require_canonical_nonempty_values():
    with pytest.raises(ValueError):
        EntityAssertion("", "organization")
    with pytest.raises(ValueError):
        EntityAssertion("acme", "")
    with pytest.raises(ValueError):
        MentionAssertion(
            entity=EntityAssertion("acme", "organization"), source_id=1, role=""
        )
    with pytest.raises(ValueError):
        MentionAssertion(
            entity=EntityAssertion("acme", "organization"), source_id="1", role="x"
        )


def test_identity_ignores_known_canonical_id():
    with_id = EntityAssertion("acme llc", "organization", entity_id=7)
    without_id = EntityAssertion("acme llc", "organization")
    assert with_id == without_id
    assert hash(with_id) == hash(without_id)
    assert with_id.identity == without_id.identity


def test_empty_input_produces_an_empty_consistent_plan():
    plan = build_classification_plan([])
    assert plan.total_proposed == 0
    assert plan.total_would_insert == 0
    assert plan.total_replay_noop == 0
    plan.check_invariants()


def test_module_is_pure_no_database_or_validator_dependencies():
    source = MODULE_SOURCE.read_text(encoding="utf-8")
    for forbidden in (
        "sqlalchemy", "create_engine", "sqlite", "psycopg",
        "EmissionValidator", "ValidationReceipt", "classify_rows",
        "cursor", "execute(",
    ):
        assert forbidden not in source, f"pure module must not reference {forbidden}"
