"""Pure classification tests for conservative provenance repair."""

from sqlalchemy import create_engine, text

from scripts.entities.provenance_repair import (
    RepairDecision,
    UnresolvedRelationship,
    _replacement_collisions,
    classify_repairs,
)


def _relationship(
    relationship_id: int,
    stale_id: int,
    relationship: str = "PRESENT_AT",
) -> UnresolvedRelationship:
    return UnresolvedRelationship(
        relationship_id=relationship_id,
        provenance_type="meeting_member",
        stale_provenance_id=stale_id,
        from_entity_id=10,
        relationship=relationship,
        to_entity_id=20,
    )


def test_unique_current_source_is_repairable():
    relationship = _relationship(1, 99)
    decisions = classify_repairs(
        [relationship],
        {("meeting_member", relationship.edge_identity): {123}},
    )

    assert decisions == [RepairDecision(
        provenance_type="meeting_member",
        stale_provenance_id=99,
        replacement_provenance_id=123,
        status="repairable",
        relationship_ids=(1,),
        candidate_ids=(123,),
    )]


def test_conflicting_current_sources_are_ambiguous():
    relationship = _relationship(1, 99)
    decision = classify_repairs(
        [relationship],
        {("meeting_member", relationship.edge_identity): {123, 124}},
    )[0]

    assert decision.status == "ambiguous"
    assert decision.replacement_provenance_id is None


def test_missing_current_source_is_not_guessed():
    decision = classify_repairs([_relationship(1, 99)], {})[0]

    assert decision.status == "unmatched"
    assert decision.candidate_ids == ()


def test_multiple_edges_without_one_common_source_are_unmatched():
    first = _relationship(1, 99, "PRESENT_AT")
    second = _relationship(2, 99, "MEMBER_OF")
    decision = classify_repairs(
        [first, second],
        {
            ("meeting_member", first.edge_identity): {123},
            ("meeting_member", second.edge_identity): {124},
        },
    )[0]

    assert decision.status == "unmatched"
    assert decision.candidate_ids == ()


def test_multiple_edges_with_two_common_sources_remain_ambiguous():
    first = _relationship(1, 99, "PRESENT_AT")
    second = _relationship(2, 99, "MEMBER_OF")
    decision = classify_repairs(
        [first, second],
        {
            ("meeting_member", first.edge_identity): {123, 124},
            ("meeting_member", second.edge_identity): {123, 124},
        },
    )[0]

    assert decision.status == "ambiguous"
    assert decision.candidate_ids == (123, 124)


def test_two_surviving_signals_can_adjudicate_past_obsolete_edge():
    applicant = _relationship(1, 99, "HAS_APPLICANT")
    staff = _relationship(2, 99, "HAS_STAFF")
    obsolete = _relationship(3, 99, "OBSOLETE_VARIANT")
    decision = classify_repairs(
        [applicant, staff, obsolete],
        {
            ("meeting_member", applicant.edge_identity): {123, 124},
            ("meeting_member", staff.edge_identity): {123},
        },
    )[0]

    assert decision.status == "repairable"
    assert decision.candidate_ids == (123,)


def test_one_surviving_signal_is_insufficient_for_multi_edge_bundle():
    supported = _relationship(1, 99, "HAS_STAFF")
    obsolete = _relationship(2, 99, "OBSOLETE_VARIANT")
    decision = classify_repairs(
        [supported, obsolete],
        {("meeting_member", supported.edge_identity): {123, 124}},
    )[0]

    assert decision.status == "ambiguous"
    assert decision.candidate_ids == (123, 124)


def test_one_singleton_signal_is_still_insufficient_for_multi_edge_bundle():
    supported = _relationship(1, 99, "HAS_STAFF")
    obsolete = _relationship(2, 99, "OBSOLETE_VARIANT")
    decision = classify_repairs(
        [supported, obsolete],
        {("meeting_member", supported.edge_identity): {123}},
    )[0]

    assert decision.status == "ambiguous"
    assert decision.replacement_provenance_id is None


def test_preflight_detects_collision_between_two_proposed_updates():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE entity_relationships (
                id INTEGER PRIMARY KEY,
                from_entity_id INTEGER,
                relationship TEXT,
                to_entity_id INTEGER,
                provenance_type TEXT,
                provenance_id INTEGER
            )
        """))
        connection.execute(text("""
            INSERT INTO entity_relationships VALUES
              (1, 10, 'PRESENT_AT', 20, 'meeting_member', 91),
              (2, 10, 'PRESENT_AT', 20, 'meeting_member', 92)
        """))
        decisions = [
            RepairDecision("meeting_member", 91, 100, "repairable", (1,), (100,)),
            RepairDecision("meeting_member", 92, 100, "repairable", (2,), (100,)),
        ]

        collisions = _replacement_collisions(connection, decisions)

    assert collisions == {1: [2], 2: [1]}
