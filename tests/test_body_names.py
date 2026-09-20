"""Tests for the shared body/jurisdiction name registry (scripts/db/names.py).

Names come from the DB (`public_bodies`), not from hardcoded dicts
(Pete 2026-09-17).  Runs on the temp SQLite tier — no network.

The test DB may already be seeded (init_db() creates a jurisdiction
registry), so everything here is get-or-create with names that cannot
collide with real data.
"""

import os
import sys

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import Jurisdiction, PublicBody  # noqa: E402
from db.names import (  # noqa: E402
    body_jurisdictions,
    body_name,
    body_names,
    clear_cache,
    get_display_name,
    humanize_code,
)

JUR_SLUG = "testland"
BODIES = {
    # registry name carries the jurisdiction as a prefix
    "testland-cc": "Testland City Council",
    "testland-pz": "Testland Planning & Zoning Commission",
    # registry name has NO jurisdiction prefix (like county bodies)
    "testland-adjust": "Board of Adjustment",
    # starts with the jurisdiction but the remainder is not a body name
    "testland-weekly": "Testland Weekly",
}


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


def _seed(session):
    """Get-or-create a jurisdiction + bodies; safe on an already-seeded DB."""
    jur = session.execute(
        select(Jurisdiction).where(Jurisdiction.slug == JUR_SLUG)
    ).scalar_one_or_none()
    if jur is None:
        jur = Jurisdiction(name="Testland", slug=JUR_SLUG)
        session.add(jur)
        session.flush()
    for code, name in BODIES.items():
        exists = session.execute(
            select(PublicBody).where(PublicBody.body_code == code)
        ).scalar_one_or_none()
        if exists is None:
            session.add(PublicBody(jurisdiction_id=jur.id, name=name,
                                   slug=code, body_code=code))
    session.commit()
    return jur


def test_resolves_name_from_registry(fresh_session):
    _seed(fresh_session)
    assert body_name("testland-cc", session=fresh_session) == "Testland City Council"


def test_bulk_lookup(fresh_session):
    _seed(fresh_session)
    got = body_names(list(BODIES), session=fresh_session)
    assert got == BODIES


def test_jurisdiction_for_body(fresh_session):
    _seed(fresh_session)
    got = body_jurisdictions(["testland-cc"], session=fresh_session)
    assert got == {"testland-cc": "Testland"}


def test_unknown_code_never_returns_shouting_slug(fresh_session):
    """The old `.upper()` bug produced 'APACHE-JUNCTION-CC'."""
    got = body_name("apache-junction-cc", session=fresh_session)
    assert got != "APACHE-JUNCTION-CC"
    assert got == "Apache Junction Cc"
    assert "-" not in got


def test_empty_code_is_empty_string(fresh_session):
    assert body_name("", session=fresh_session) == ""
    assert body_names([], session=fresh_session) == {}


def test_humanize_code_replaces_separators():
    assert humanize_code("mc-bos") == "Mc Bos"
    assert humanize_code("mc_audit") == "Mc Audit"
    assert humanize_code("") == ""


def test_cache_serves_without_a_session(fresh_session):
    """Second read comes from the cache, so no session is needed."""
    _seed(fresh_session)
    assert body_name("testland-cc", session=fresh_session) == "Testland City Council"
    # no session passed — must still resolve from the populated cache
    assert body_name("testland-cc") == "Testland City Council"


def test_clear_cache_forces_a_fresh_lookup(fresh_session):
    _seed(fresh_session)
    body_name("testland-cc", session=fresh_session)
    clear_cache()
    assert body_name("testland-cc", session=fresh_session) == "Testland City Council"


def test_missing_registry_row_is_logged(fresh_session, caplog):
    _seed(fresh_session)
    with caplog.at_level("WARNING"):
        body_names(["testland-cc", "not-registered"], session=fresh_session)
    assert any("not-registered" in r.getMessage() for r in caplog.records)


# ── context-aware get_display_name (Pete 2026-09-17) ─────────────────────

def test_canonical_style_returns_registry_name(fresh_session):
    _seed(fresh_session)
    assert get_display_name("canonical", "testland-adjust",
                            session=fresh_session) == "Board of Adjustment"


def test_with_jurisdiction_qualifies(fresh_session):
    _seed(fresh_session)
    assert get_display_name("with-jurisdiction", "testland-adjust",
                            session=fresh_session) == "Testland Board of Adjustment"


def test_with_jurisdiction_does_not_double_up(fresh_session):
    _seed(fresh_session)
    assert get_display_name("with-jurisdiction", "testland-cc",
                            session=fresh_session) == "Testland City Council"


def test_no_jurisdiction_strips_the_prefix(fresh_session):
    _seed(fresh_session)
    assert get_display_name("no-jurisdiction", "testland-cc",
                            session=fresh_session) == "City Council"


def test_no_jurisdiction_leaves_unprefixed_names_alone(fresh_session):
    _seed(fresh_session)
    assert get_display_name("no-jurisdiction", "testland-adjust",
                            session=fresh_session) == "Board of Adjustment"


def test_no_jurisdiction_refuses_a_useless_strip(fresh_session):
    """'Testland Weekly' -> 'Weekly' would identify nothing."""
    _seed(fresh_session)
    assert get_display_name("no-jurisdiction", "testland-weekly",
                            session=fresh_session) == "Testland Weekly"


def test_jurisdiction_style_returns_only_the_jurisdiction(fresh_session):
    _seed(fresh_session)
    assert get_display_name("jurisdiction", "testland-cc",
                            session=fresh_session) == "Testland"


def test_unknown_style_falls_back_to_canonical(fresh_session, caplog):
    _seed(fresh_session)
    with caplog.at_level("WARNING"):
        got = get_display_name("no-such-style", "testland-cc",
                               session=fresh_session)
    assert got == "Testland City Council"
    assert any("unknown name style" in r.getMessage() for r in caplog.records)


def test_display_name_empty_code_is_safe(fresh_session):
    assert get_display_name("no-jurisdiction", "", session=fresh_session) == ""


# ── jurisdiction wrappers must not double-prefix (Pete 2026-09-17) ───────

def _seed_city_of(session):
    """A jurisdiction named 'City of X' whose body name already carries X."""
    jur = session.execute(
        select(Jurisdiction).where(Jurisdiction.slug == "city-of-testville")
    ).scalar_one_or_none()
    if jur is None:
        jur = Jurisdiction(name="City of Testville", slug="city-of-testville")
        session.add(jur)
        session.flush()
    code = "testville-pz"
    exists = session.execute(
        select(PublicBody).where(PublicBody.body_code == code)
    ).scalar_one_or_none()
    if exists is None:
        session.add(PublicBody(jurisdiction_id=jur.id,
                               name="Testville Planning & Zoning Commission",
                               slug=code, body_code=code))
        session.commit()
    return code


def test_with_jurisdiction_does_not_double_prefix(fresh_session):
    """'City of Testville' + 'Testville Planning & Zoning Commission' must NOT
    become 'City of Testville Testville Planning & Zoning Commission'."""
    code = _seed_city_of(fresh_session)
    clear_cache()
    got = get_display_name("with-jurisdiction", code, session=fresh_session)
    assert got == "Testville Planning & Zoning Commission"
    assert "Testville Testville" not in got


def test_no_jurisdiction_handles_city_of_wrapper(fresh_session):
    code = _seed_city_of(fresh_session)
    clear_cache()
    got = get_display_name("no-jurisdiction", code, session=fresh_session)
    assert got == "Planning & Zoning Commission"


def test_jurisdiction_tokens_expand_wrappers():
    from db.names import _jurisdiction_tokens
    assert _jurisdiction_tokens("City of Chandler") == ["City of Chandler", "Chandler"]
    assert _jurisdiction_tokens("Maricopa County") == ["Maricopa County", "Maricopa"]
    assert _jurisdiction_tokens("Mesa") == ["Mesa"]
    assert _jurisdiction_tokens("") == []


def test_duplicate_registry_rows_are_surfaced(fresh_session, caplog):
    """Duplicate body_code rows must warn, not silently pick one.

    Real cases found 2026-09-17: chandler-cf, chandler-pdc, chandler-pha.
    """
    jur = _seed(fresh_session)
    code = "dupcode-cc"
    existing = fresh_session.execute(
        select(PublicBody).where(PublicBody.body_code == code)
    ).scalars().all()
    if not existing:
        fresh_session.add(PublicBody(jurisdiction_id=jur.id,
                                     name="Aaa Duplicate Body",
                                     slug="dup-a", body_code=code))
        fresh_session.add(PublicBody(jurisdiction_id=jur.id,
                                     name="Zzz Duplicate Body",
                                     slug="dup-b", body_code=code))
        fresh_session.commit()
    clear_cache()
    with caplog.at_level("WARNING"):
        got = body_name(code, session=fresh_session)
    assert any("duplicate body_code" in r.getMessage() for r in caplog.records)
    # deterministic: first row by name
    assert got == "Aaa Duplicate Body"
