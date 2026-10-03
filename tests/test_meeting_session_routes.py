"""Integration coverage for meeting routes with scoped read sessions."""

from contextlib import contextmanager
from datetime import datetime, timezone

from db.models import Jurisdiction, Meeting, SupportingDocument


def _app():
    from routes import create_app

    app = create_app()
    app.config.update(TESTING=True)
    return app


def _track_session_scope(monkeypatch):
    import routes.meetings as meeting_routes

    original = meeting_routes.session_scope
    state = {"entered": 0, "exited": 0}

    @contextmanager
    def tracked_scope():
        state["entered"] += 1
        try:
            with original() as session:
                yield session
        finally:
            state["exited"] += 1

    monkeypatch.setattr(meeting_routes, "session_scope", tracked_scope)
    return state


def test_body_filter_api_projects_rows_before_session_closes(fresh_session):
    jurisdiction = Jurisdiction(name="Session City", slug="session-city")
    fresh_session.add(jurisdiction)
    fresh_session.flush()
    fresh_session.add(Meeting(
        body="session-cc",
        meeting_id="session-api-meeting",
        meeting_date="2026-10-02",
        meeting_type="Regular Meeting",
        meeting_title="Session City Council",
        jurisdiction_id=jurisdiction.id,
    ))
    fresh_session.commit()

    response = _app().test_client().get("/api/bodies")

    assert response.status_code == 200
    assert response.get_json()["session-city"] == {
        "name": "Session City",
        "bodies": [{"label": "session-cc", "value": "session-cc"}],
    }


def test_document_detail_renders_projection_after_session_closes(fresh_session):
    jurisdiction = Jurisdiction(name="Document City", slug="document-city")
    fresh_session.add(jurisdiction)
    fresh_session.flush()
    meeting = Meeting(
        body="document-cc",
        meeting_id="document-meeting",
        meeting_date="2026-10-02",
        meeting_type="Regular Meeting",
        meeting_title="Document City Council",
        jurisdiction_id=jurisdiction.id,
    )
    fresh_session.add(meeting)
    fresh_session.flush()
    document = SupportingDocument(
        body="document-cc",
        agenda_item_id="document-item",
        meeting_id=meeting.meeting_id,
        meeting_db_id=meeting.id,
        agenda_item_number="4B2",
        document_title="Scoped Supporting Document",
        document_url="https://example.test/document.pdf",
        text_content="Extracted document text.",
        text_extracted_at=datetime.now(timezone.utc),
        text_extraction_method="native",
    )
    fresh_session.add(document)
    fresh_session.commit()

    response = _app().test_client().get(f"/documents/{document.id}")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Scoped Supporting Document" in text
    assert "Extracted document text." in text
    assert "Document City" in text


def test_unknown_document_returns_404(fresh_session):
    response = _app().test_client().get("/documents/999999")

    assert response.status_code == 404


def test_missing_meeting_and_case_close_owned_sessions(fresh_session, monkeypatch):
    state = _track_session_scope(monkeypatch)
    client = _app().test_client()

    meeting = client.get("/meetings/not-present")
    case = client.get("/cases/not-present")

    assert meeting.status_code == 200
    assert case.status_code == 404
    assert state == {"entered": 2, "exited": 2}


def test_meeting_query_helpers_close_owned_sessions(fresh_session, monkeypatch):
    import routes.meetings as meeting_routes

    state = _track_session_scope(monkeypatch)

    assert meeting_routes.get_distinct_meeting_types() == []
    assert meeting_routes.get_filtered_meetings() == ([], 0, 1, 1)
    assert meeting_routes.get_related_case_events("NOT-PRESENT") == []
    assert state == {"entered": 3, "exited": 3}
