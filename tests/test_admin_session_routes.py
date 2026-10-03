"""Integration coverage for admin list routes with scoped read sessions."""

from contextlib import contextmanager
from io import BytesIO

from sqlalchemy import select

from db.newsroom import (
    AdminUser,
    Article,
    DismissedSuggestion,
    MediaImage,
    Notification,
    SkeetDraft,
    Tag,
)


def _authenticated_client(fresh_session, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "false")
    user = AdminUser(
        username="session-admin",
        display_name="Session Admin",
        password_hash="unused-in-session-test",
        role="admin",
    )
    fresh_session.add(user)
    fresh_session.commit()

    from routes import create_app

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["_user_id"] = str(user.id)
        flask_session["_fresh"] = True
    return client


def _track_session_scope(monkeypatch):
    import routes.admin as admin_routes

    original = admin_routes.session_scope
    state = {"entered": 0, "exited": 0}

    @contextmanager
    def tracked_scope():
        state["entered"] += 1
        try:
            with original() as session:
                yield session
        finally:
            state["exited"] += 1

    monkeypatch.setattr(admin_routes, "session_scope", tracked_scope)
    return state


def _track_transaction_scope(monkeypatch):
    import routes.admin as admin_routes

    original = admin_routes.transaction_scope
    state = {"entered": 0, "exited": 0}

    @contextmanager
    def tracked_scope():
        state["entered"] += 1
        try:
            with original() as session:
                yield session
        finally:
            state["exited"] += 1

    monkeypatch.setattr(admin_routes, "transaction_scope", tracked_scope)
    return state


def test_admin_read_routes_close_owned_sessions(fresh_session, monkeypatch):
    client = _authenticated_client(fresh_session, monkeypatch)
    state = _track_session_scope(monkeypatch)
    paths = (
        "/admin/",
        "/admin/drafts",
        "/admin/subscribers",
        "/admin/published",
        "/admin/archived",
        "/admin/featured",
        "/admin/bluesky",
        "/admin/suggestions",
        "/admin/notifications",
        "/admin/notifications/count",
        "/admin/images",
        "/admin/images/api",
        "/admin/articles/search?q=session",
        "/admin/articles/999999/style-check",
    )

    responses = [client.get(path) for path in paths]

    assert [response.status_code for response in responses] == [
        *([200] * (len(paths) - 1)),
        404,
    ]
    assert state == {"entered": len(paths), "exited": len(paths)}


def test_notification_mutations_use_owned_transactions(
    fresh_session,
    monkeypatch,
):
    notification = Notification(message="Transaction test")
    fresh_session.add(notification)
    fresh_session.commit()
    notification_id = notification.id
    client = _authenticated_client(fresh_session, monkeypatch)
    client.application.config["WTF_CSRF_ENABLED"] = False
    state = _track_transaction_scope(monkeypatch)

    marked = client.post("/admin/notifications/mark-read")
    fresh_session.expire_all()
    assert fresh_session.get(Notification, notification_id).is_read is True
    fresh_session.rollback()

    deleted = client.post(f"/admin/notifications/{notification_id}/delete")
    fresh_session.expire_all()

    assert marked.status_code == 302
    assert deleted.status_code == 302
    assert fresh_session.execute(
        select(Notification).where(Notification.id == notification_id)
    ).scalar_one_or_none() is None
    assert state == {"entered": 2, "exited": 2}


def test_article_mutations_use_owned_transactions(fresh_session, monkeypatch):
    articles = [
        Article(title=f"Transaction Article {index}", slug=f"tx-article-{index}")
        for index in range(3)
    ]
    fresh_session.add_all(articles)
    fresh_session.commit()
    article_ids = [article.id for article in articles]
    client = _authenticated_client(fresh_session, monkeypatch)
    client.application.config["WTF_CSRF_ENABLED"] = False
    state = _track_transaction_scope(monkeypatch)
    synced = []
    monkeypatch.setattr("routes.admin.sync_article_fts", synced.append)

    responses = [
        client.post(
            f"/admin/articles/{article_ids[0]}/priority",
            data={"priority": "42"},
        ),
        client.post(f"/admin/articles/{article_ids[1]}/archive"),
        client.post(f"/admin/articles/{article_ids[2]}/feature"),
        client.post(
            "/admin/articles/reorder",
            json={"order": [article_ids[2], article_ids[0]]},
        ),
        client.post(
            "/admin/suggestions/dismiss",
            data={
                "body": "tempe-cc",
                "meeting_id": "tx-meeting",
                "agenda_item_number": "4B2",
            },
        ),
        client.post(f"/admin/articles/{article_ids[0]}/promote"),
        client.post(f"/admin/articles/{article_ids[1]}/delete"),
    ]
    fresh_session.rollback()

    remaining = {
        article.id: article
        for article in fresh_session.execute(select(Article)).scalars().all()
    }
    dismissal = fresh_session.execute(select(DismissedSuggestion)).scalar_one()

    assert [response.status_code for response in responses] == [
        302,
        302,
        302,
        200,
        302,
        302,
        302,
    ]
    assert article_ids[1] not in remaining
    assert remaining[article_ids[2]].is_featured is True
    assert remaining[article_ids[2]].priority == 2
    assert remaining[article_ids[0]].priority == 1
    assert remaining[article_ids[0]].status == "published"
    assert synced == [article_ids[0]]
    assert dismissal.meeting_id == "tx-meeting"
    assert state == {"entered": 7, "exited": 7}


def test_skeet_and_image_metadata_mutations_use_owned_transactions(
    fresh_session,
    monkeypatch,
):
    article = Article(
        title="Skeet Transaction Article",
        slug="skeet-transaction-article",
        summary="A summary for the generated draft.",
    )
    image = MediaImage(filename="transaction.jpg", alt_text="Old", tags="old")
    fresh_session.add_all([article, image])
    fresh_session.commit()
    article_id = article.id
    image_id = image.id
    client = _authenticated_client(fresh_session, monkeypatch)
    client.application.config["WTF_CSRF_ENABLED"] = False
    state = _track_transaction_scope(monkeypatch)

    created = client.post(f"/admin/skeet-drafts/create/{article_id}")
    fresh_session.rollback()
    draft_id = fresh_session.execute(select(SkeetDraft.id)).scalar_one()
    fresh_session.rollback()
    edited = client.post(
        f"/admin/skeet-drafts/{draft_id}/edit",
        data={"draft_text": "Edited draft", "action": "approve"},
    )
    image_edited = client.post(
        f"/admin/images/{image_id}/edit",
        data={"alt_text": "Updated", "tags": "new,regional"},
    )
    deleted = client.post(f"/admin/skeet-drafts/{draft_id}/delete")
    fresh_session.rollback()

    saved_image = fresh_session.get(MediaImage, image_id)
    assert [created.status_code, edited.status_code, image_edited.status_code,
            deleted.status_code] == [302, 302, 302, 302]
    assert saved_image.alt_text == "Updated"
    assert saved_image.tags == "new,regional"
    assert fresh_session.get(SkeetDraft, draft_id) is None
    assert state == {"entered": 4, "exited": 4}


def test_suggestion_drafts_splits_and_tags_commit_once(
    fresh_session,
    monkeypatch,
):
    client = _authenticated_client(fresh_session, monkeypatch)
    client.application.config["WTF_CSRF_ENABLED"] = False
    state = _track_transaction_scope(monkeypatch)
    synced = []
    monkeypatch.setattr("routes.admin.sync_article_fts", synced.append)

    draft = client.post(
        "/admin/suggestions/draft",
        data={
            "body": "tempe-cc",
            "meeting_id": "draft-meeting",
            "agenda_item_number": "1",
            "source_url": "https://example.test/agenda",
        },
    )
    split = client.post(
        "/admin/suggestions/split",
        data={
            "title": "Split Transaction Story",
            "body": "tempe-cc",
            "meeting_id": "split-meeting",
            "agenda_item_number": "2",
        },
    )
    tag = client.post(
        "/admin/tags",
        data={"action": "add", "name": "Transaction Tag"},
    )
    fresh_session.rollback()

    articles = fresh_session.execute(select(Article)).scalars().all()
    dismissals = fresh_session.execute(select(DismissedSuggestion)).scalars().all()

    assert [draft.status_code, split.status_code, tag.status_code] == [302, 302, 200]
    assert {article.title for article in articles} == {
        "Untitled",
        "Split Transaction Story",
    }
    assert len(synced) == 1
    assert [dismissal.reason for dismissal in dismissals] == ["split"]
    assert fresh_session.execute(
        select(Tag).where(Tag.name == "Transaction Tag")
    ).scalar_one().slug == "transaction-tag"
    assert state == {"entered": 3, "exited": 3}


def test_article_create_and_edit_run_fts_after_commit(
    fresh_session,
    monkeypatch,
):
    client = _authenticated_client(fresh_session, monkeypatch)
    client.application.config["WTF_CSRF_ENABLED"] = False
    state = _track_transaction_scope(monkeypatch)
    synced = []
    monkeypatch.setattr("routes.admin.sync_article_fts", synced.append)

    created = client.post(
        "/admin/articles/new",
        data={"title": "Owned Article Form", "body": "Initial body"},
    )
    fresh_session.rollback()
    article = fresh_session.execute(
        select(Article).where(Article.title == "Owned Article Form")
    ).scalar_one()
    article_id = article.id
    fresh_session.rollback()

    edited = client.post(
        f"/admin/articles/{article_id}/edit",
        data={
            "title": "Owned Article Form Updated",
            "summary": "Updated summary",
            "body": "Updated body",
            "status": "draft",
        },
    )
    fresh_session.rollback()
    saved = fresh_session.get(Article, article_id)

    assert created.status_code == 302
    assert edited.status_code == 302
    assert saved.title == "Owned Article Form Updated"
    assert saved.summary == "Updated summary"
    assert synced == [article_id, article_id]
    assert state == {"entered": 2, "exited": 2}


def test_image_upload_and_delete_coordinate_files_with_transactions(
    fresh_session,
    monkeypatch,
    tmp_path,
):
    client = _authenticated_client(fresh_session, monkeypatch)
    client.application.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr("routes.admin._UPLOAD_DIR", str(tmp_path))
    state = _track_transaction_scope(monkeypatch)

    uploaded = client.post(
        "/admin/images/upload",
        data={
            "files": (BytesIO(b"not-an-image"), "transaction.jpg"),
            "tags": "transaction",
        },
        content_type="multipart/form-data",
    )
    fresh_session.rollback()
    image = fresh_session.execute(select(MediaImage)).scalar_one()
    image_id = image.id
    image_path = tmp_path / image.filename
    fresh_session.rollback()

    deleted = client.post(f"/admin/images/{image_id}/delete")
    fresh_session.rollback()

    assert uploaded.status_code == 302
    assert deleted.status_code == 302
    assert not image_path.exists()
    assert fresh_session.get(MediaImage, image_id) is None
    assert state == {"entered": 2, "exited": 2}


def test_bluesky_post_uses_durable_claim_and_final_transaction(
    fresh_session,
    monkeypatch,
):
    article = Article(
        title="Bluesky Transaction",
        slug="bluesky-transaction",
        summary="Summary",
    )
    fresh_session.add(article)
    fresh_session.flush()
    draft = SkeetDraft(
        article_id=article.id,
        draft_text="Post text",
        status="approved",
    )
    fresh_session.add(draft)
    fresh_session.commit()
    draft_id = draft.id
    client = _authenticated_client(fresh_session, monkeypatch)
    client.application.config["WTF_CSRF_ENABLED"] = False
    state = _track_transaction_scope(monkeypatch)
    monkeypatch.setattr(
        "social.post_to_bluesky",
        lambda **kwargs: "at://did:plc:test/app.bsky.feed.post/123",
    )

    response = client.post(f"/admin/skeet-drafts/{draft_id}/post")
    fresh_session.rollback()
    saved = fresh_session.get(SkeetDraft, draft_id)

    assert response.status_code == 302
    assert saved.status == "posted"
    assert saved.bluesky_post_uri.endswith("/123")
    assert state == {"entered": 2, "exited": 2}
