"""Integration coverage for member routes with scoped read sessions."""

from contextlib import contextmanager

from db.models import Person


def _app():
    from routes import create_app

    app = create_app()
    app.config.update(TESTING=True)
    return app


def _track_session_scope(monkeypatch):
    import routes.members as member_routes

    original = member_routes.session_scope
    state = {"entered": 0, "exited": 0}

    @contextmanager
    def tracked_scope():
        state["entered"] += 1
        try:
            with original() as session:
                yield session
        finally:
            state["exited"] += 1

    monkeypatch.setattr(member_routes, "session_scope", tracked_scope)
    return state


def test_unknown_member_vote_api_closes_its_session(fresh_session, monkeypatch):
    state = _track_session_scope(monkeypatch)

    response = _app().test_client().get("/api/members/not-present/votes")

    assert response.status_code == 200
    assert response.get_json() == {
        "page": 1,
        "per_page": 25,
        "rows": [],
        "total": 0,
    }
    assert state == {"entered": 1, "exited": 1}


def test_legacy_member_redirect_closes_session_and_preserves_query(
    fresh_session,
    monkeypatch,
):
    fresh_session.add(Person(name="Jane Doe", normalized_name="jane doe"))
    fresh_session.commit()
    state = _track_session_scope(monkeypatch)

    response = _app().test_client().get(
        "/members/jane-doe?start_year=2025",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/members/maricopa-county/bos/jane-doe?start_year=2025"
    )
    assert state == {"entered": 1, "exited": 1}


def test_inferred_abstentions_renders_after_session_closes(
    fresh_session,
    monkeypatch,
):
    state = _track_session_scope(monkeypatch)

    response = _app().test_client().get("/debug/inferred-abstentions")

    assert response.status_code == 200
    assert state == {"entered": 1, "exited": 1}


def test_qualified_member_profile_renders_projection_after_session_closes(
    fresh_session,
    monkeypatch,
):
    fresh_session.add(Person(name="Jane Profile", normalized_name="jane profile"))
    fresh_session.commit()
    state = _track_session_scope(monkeypatch)

    response = _app().test_client().get(
        "/members/maricopa-county/bos/jane-profile?start_year=2025"
    )
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Jane Profile" in text
    assert 'value="2025" selected' in text
    assert state == {"entered": 1, "exited": 1}


def test_empty_body_analytics_closes_session_on_early_return(
    fresh_session,
    monkeypatch,
):
    state = _track_session_scope(monkeypatch)

    response = _app().test_client().get(
        "/members/maricopa-county/bos/analytics?start_year=2025"
    )

    assert response.status_code == 200
    assert state == {"entered": 1, "exited": 1}
