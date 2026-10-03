"""Integration coverage for public article routes with scoped sessions."""

from datetime import date, datetime, timezone

from db.models import Meeting
from db.newsroom import Article, Tag


def _app():
    from routes import create_app

    app = create_app()
    app.config.update(TESTING=True)
    return app


def test_article_detail_and_archive_render_after_session_closes(
    fresh_session,
    monkeypatch,
):
    monkeypatch.setattr("analytics_db.track_page_view", lambda article_id: None)
    tag = Tag(name="Session Test", slug="session-test")
    article = Article(
        title="Scoped Sessions Work",
        slug="scoped-sessions-work",
        summary="A route ownership test.",
        body="The article remains renderable after its query session closes.",
        status="archived",
        tags=[tag],
    )
    fresh_session.add(article)
    fresh_session.commit()

    client = _app().test_client()
    detail = client.get("/articles/scoped-sessions-work")
    archive = client.get("/articles/archive")

    assert detail.status_code == 200
    assert "Scoped Sessions Work" in detail.get_data(as_text=True)
    assert archive.status_code == 200
    assert "Scoped Sessions Work" in archive.get_data(as_text=True)


def test_article_tag_and_empty_search_render_after_session_closes(fresh_session):
    tag = Tag(name="Water Test", slug="water-test")
    article = Article(
        title="Water Route Test",
        slug="water-route-test",
        summary="Testing the tag route.",
        body="Water policy.",
        status="published",
        tags=[tag],
    )
    fresh_session.add(article)
    fresh_session.commit()

    client = _app().test_client()
    tagged = client.get("/articles/tag/water-test")
    search = client.get("/search")

    assert tagged.status_code == 200
    assert "Water Route Test" in tagged.get_data(as_text=True)
    assert search.status_code == 200


def test_unknown_article_and_tag_return_404(fresh_session):
    client = _app().test_client()

    assert client.get("/articles/not-present").status_code == 404
    assert client.get("/articles/tag/not-present").status_code == 404


def test_front_page_renders_feed_and_meetings_after_session_closes(
    fresh_session,
    monkeypatch,
):
    monkeypatch.setattr("analytics_db.get_trending", lambda limit: [])
    for index in range(4):
        fresh_session.add(Article(
            title=f"Front Page Article {index}",
            slug=f"front-page-article-{index}",
            summary="Front-page session test.",
            body="Published body.",
            status="published",
            published_at=datetime(2026, 10, index + 1, tzinfo=timezone.utc),
        ))
    fresh_session.add(Meeting(
        body="tempe-cc",
        meeting_id="front-page-session-test",
        meeting_date=date.today().isoformat(),
        meeting_type="Regular Meeting",
        meeting_title="Tempe City Council",
        sync_status="complete",
    ))
    fresh_session.commit()

    response = _app().test_client().get("/")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Front Page Article 3" in text
    assert "Front Page Article 0" in text
    assert "Tempe City Council" in text
