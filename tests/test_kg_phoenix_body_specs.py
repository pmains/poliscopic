"""Tests for the approved Phoenix body specs and spec binding.

Isolated: the collision check runs against a throwaway in-memory SQLite database.
No development or production database is contacted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from scripts.kg import phoenix_dr_adjudication as adjudication
from scripts.kg import phoenix_dr_plan_body as plan_body
from scripts.kg.phoenix_body_specs import (
    APPROVED_BODIES,
    DEFAULT_BODY_CODE,
    approved_spec,
    assert_no_body_collision,
    body_specs,
    bound_spec,
)

EXPECTED = {
    "phoenix-dr": (13, 126),
    "phoenix-dab": (37, 226),
    "phoenix-ds": (5, 22),
}


def engine_with_bodies(rows=()):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, "
            "body_code TEXT, body_type TEXT, jurisdiction_id INTEGER)"
        ))
        for row in rows:
            conn.execute(text(
                "INSERT INTO public_bodies VALUES (:i,:n,:s,:c,'Board',4)"
            ), {"i": row[0], "n": row[1], "s": row[2], "c": row[3]})
    return engine


# -- allowlist -----------------------------------------------------------------


def test_only_three_bodies_are_approved():
    assert sorted(APPROVED_BODIES) == sorted(EXPECTED)


def test_expected_populations_match_the_adjudication():
    for code, (meetings, extractions) in EXPECTED.items():
        spec = approved_spec(code)
        assert spec.expected_meetings == meetings
        assert spec.expected_extractions == extractions


def test_default_body_is_phoenix_dr():
    assert DEFAULT_BODY_CODE == "phoenix-dr"


def test_unknown_body_is_refused():
    with pytest.raises(KeyError) as exc:
        approved_spec("__skip__")
    assert "not an approved repair target" in str(exc.value)


def test_sentinel_codes_are_not_approved():
    for code in ("__skip__", "", "skip"):
        with pytest.raises(KeyError):
            approved_spec(code)


def test_body_specs_can_be_restricted():
    assert [s.body_code for s in body_specs(["phoenix-ds"])] == ["phoenix-ds"]
    assert len(body_specs()) == 3


# -- canonical values ----------------------------------------------------------


def test_canonical_values_come_from_registry_evidence():
    dab = approved_spec("phoenix-dab")
    assert dab.body_code == "phoenix-dab"
    assert dab.name == "Phoenix Development Advisory Board"
    assert dab.slug == "phoenix-development-advisory-board"
    assert any("phoenix_planning.py" in e for e in dab.registry_evidence)


def test_identity_dict_is_exactly_the_row_values():
    assert approved_spec("phoenix-ds").identity() == {
        "body_code": "phoenix-ds",
        "name": "Phoenix Design Standards Committee",
        "slug": "phoenix-design-standards-committee",
        "body_type": "Committee",
    }


@pytest.mark.parametrize("code", ["phoenix-dr", "phoenix-dab", "phoenix-ds"])
def test_convention_derived_fields_are_flagged(code):
    spec = approved_spec(code)
    assert set(spec.convention_derived_fields) == {"name", "slug", "body_type"}
    assert spec.confirmation_note


def test_phoenix_dr_name_follows_the_database_convention():
    spec = approved_spec("phoenix-dr")
    assert spec.name == "Phoenix Design Review Committee"
    assert spec.slug == "phoenix-design-review-committee"
    assert "convention" in spec.confirmation_note


def test_no_slug_collides_with_the_pending_obsolete_value():
    for spec in body_specs():
        assert spec.slug != spec.body_code


# -- binding -------------------------------------------------------------------


def test_binding_selects_the_body_and_restores_it():
    before = (adjudication.BODY_CODE, adjudication.PROPOSED_BODY_SLUG, plan_body.BODY_CODE)

    with bound_spec(approved_spec("phoenix-dab")):
        assert adjudication.BODY_CODE == "phoenix-dab"
        assert adjudication.PROPOSED_BODY_SLUG == "phoenix-development-advisory-board"
        assert plan_body.BODY_CODE == "phoenix-dab"
        assert adjudication.TITLE_PATTERN.search("Development Advisory Board")
        assert not adjudication.TITLE_PATTERN.search("Design Review Committee")

    assert (adjudication.BODY_CODE, adjudication.PROPOSED_BODY_SLUG, plan_body.BODY_CODE) == before
    assert adjudication.TITLE_PATTERN.search("Design Review Committee")


def test_binding_is_restored_even_when_the_body_raises():
    before = plan_body.BODY_CODE
    with pytest.raises(RuntimeError):
        with bound_spec(approved_spec("phoenix-ds")):
            assert plan_body.BODY_CODE == "phoenix-ds"
            raise RuntimeError("boom")
    assert plan_body.BODY_CODE == before


def test_binding_does_not_leak_between_bodies():
    with bound_spec(approved_spec("phoenix-dab")):
        pass
    with bound_spec(approved_spec("phoenix-ds")):
        assert plan_body.BODY_CODE == "phoenix-ds"
        assert adjudication.PROPOSED_BODY_NAME == "Phoenix Design Standards Committee"


# -- collision checks ----------------------------------------------------------


def test_no_collision_when_the_tables_are_clean():
    engine = engine_with_bodies([])
    with engine.connect() as conn:
        assert_no_body_collision(conn, approved_spec("phoenix-dab"))


@pytest.mark.parametrize(
    "row",
    [
        (1, "Other", "other", "phoenix-dab"),                                  # body code
        (2, "Other", "phoenix-development-advisory-board", "other"),           # slug
        (3, "Phoenix Development Advisory Board", "other", "other"),           # name
    ],
)
def test_collision_on_any_identity_field_is_refused(row):
    engine = engine_with_bodies([row])
    with engine.connect() as conn:
        with pytest.raises(ValueError) as exc:
            assert_no_body_collision(conn, approved_spec("phoenix-dab"))
    assert "collision" in str(exc.value)


def test_collision_check_covers_every_approved_body():
    for spec in body_specs():
        engine = engine_with_bodies([(9, "Taken", "taken", "taken")])
        with engine.connect() as conn:
            assert_no_body_collision(conn, spec)
