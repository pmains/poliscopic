"""Isolated smoke tests for the public entity graph pages."""

import os

import pytest


@pytest.fixture
def graph_page_client(fresh_session):
    """Seed one representative graph slice in the per-test SQLite database."""
    from db.models import (
        AgendaItem,
        Entity,
        EntityMention,
        EntityRelationship,
        Jurisdiction,
        Meeting,
        PublicBody,
    )

    jurisdiction = Jurisdiction(name="Test City", slug="test-city", state="AZ")
    fresh_session.add(jurisdiction)
    fresh_session.flush()
    body = PublicBody(
        jurisdiction_id=jurisdiction.id,
        name="Test City Council",
        slug="test-city-council",
        body_code="test-cc",
    )
    fresh_session.add(body)
    fresh_session.flush()
    meeting = Meeting(
        body="test-cc",
        meeting_id="test-meeting-1",
        meeting_date="2026-01-15",
        meeting_type="Regular Meeting",
        jurisdiction_id=jurisdiction.id,
        public_body_id=body.id,
    )
    fresh_session.add(meeting)
    fresh_session.flush()
    item = AgendaItem(
        body="test-cc",
        meeting_id=meeting.meeting_id,
        meeting_db_id=meeting.id,
        agenda_item_number="3",
        agenda_item_id="test-meeting-1-item-3",
        agenda_item_title="Downtown redevelopment proposal",
        agenda_item_text="The council considered a proposal from Acme Development.",
        jurisdiction_id=jurisdiction.id,
        public_body_id=body.id,
    )
    subject = Entity(
        entity_type="organization",
        name="Acme Development",
        normalized_name="acme development",
        jurisdiction_id=jurisdiction.id,
        mention_count=1,
    )
    representative = Entity(
        entity_type="law_firm",
        name="Example Legal",
        normalized_name="example legal",
        jurisdiction_id=jurisdiction.id,
        mention_count=0,
    )
    fresh_session.add_all([item, subject, representative])
    fresh_session.flush()
    fresh_session.add(
        EntityMention(
            entity_id=subject.id,
            source_type="agenda_item",
            source_id=item.id,
            mention_text="Acme Development",
            context_snippet="The council considered Acme Development's proposal.",
            confidence=95,
            role_in_context="applicant",
        )
    )
    fresh_session.add(
        EntityRelationship(
            from_entity_id=representative.id,
            to_entity_id=subject.id,
            relationship="represents",
        )
    )
    fresh_session.commit()

    os.environ["POLISCOPIC_DISABLE_ADMIN"] = "true"
    from routes import create_app

    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client(), subject.id


def test_entity_search_renders_meaningful_entity_content(graph_page_client):
    client, _ = graph_page_client

    response = client.get("/entities?type=organization")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Acme Development" in body
    assert "1 mention" in body


def test_entity_detail_renders_mention_and_relationship_content(graph_page_client):
    client, entity_id = graph_page_client

    response = client.get(f"/entities/{entity_id}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Acme Development" in body
    assert "Downtown redevelopment proposal" in body
    assert "Example Legal" in body
    assert "Relationships" in body


def test_missing_entity_returns_404(graph_page_client):
    client, _ = graph_page_client

    response = client.get("/entities/999999")

    assert response.status_code == 404


def test_entity_search_is_case_insensitive(graph_page_client):
    """Search matches on uppercase query via normalized_name (LOWER LIKE)."""
    client, subject_id = graph_page_client

    response = client.get("/entities?q=ACME")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # The matched entity's display name is rendered in the result row…
    assert "Acme Development" in body
    # …and the row links to the entity detail page.
    assert f'href="/entities/{subject_id}"' in body
    # No false positive: the unrelated law firm is not in the results.
    assert "Example Legal" not in body


def test_timeline_shows_same_meeting_once_for_multiple_mentions(graph_page_client, fresh_session):
    """Two mentions on different items of one meeting render as one timeline entry."""
    from db.models import AgendaItem, Entity, EntityMention

    client, subject_id = graph_page_client
    subject = fresh_session.query(Entity).filter_by(id=subject_id).one()
    first_item = fresh_session.query(AgendaItem).filter_by(agenda_item_number="3").one()

    second_item = AgendaItem(
        body=first_item.body,
        meeting_id=first_item.meeting_id,
        meeting_db_id=first_item.meeting_db_id,
        agenda_item_number="4",
        agenda_item_id="test-meeting-1-item-4",
        agenda_item_title="Second proposal item",
        agenda_item_text="A second proposal also referenced Acme Development.",
        jurisdiction_id=first_item.jurisdiction_id,
        public_body_id=first_item.public_body_id,
    )
    fresh_session.add(second_item)
    fresh_session.flush()
    fresh_session.add(
        EntityMention(
            entity_id=subject_id,
            source_type="agenda_item",
            source_id=second_item.id,
            mention_text="Acme Development",
            context_snippet="A second proposal from Acme Development.",
            confidence=90,
            role_in_context="applicant",
        )
    )
    subject.mention_count = 2
    fresh_session.commit()

    body = client.get(f"/entities/{subject_id}").get_data(as_text=True)
    assert "Meeting Timeline" in body
    timeline_section = body.split("Meeting Timeline", 1)[1]
    # The timeline renders one meeting row per distinct meeting; each row with
    # multiple mentions exposes exactly one expand toggle targeting that meeting.
    assert timeline_section.count('data-bs-target="#mentions-') == 1
    # Both mentions are present, grouped under the single meeting row…
    assert "Downtown redevelopment proposal" in timeline_section
    assert "Second proposal item" in timeline_section
    # …and the row advertises the extra mention instead of duplicating itself.
    assert "(+1 more mention)" in timeline_section


def test_relationship_direction_renders_representative(graph_page_client, fresh_session):
    """'represents' edge direction: Example Legal → Acme Development."""
    from db.models import Entity

    client, subject_id = graph_page_client
    law_firm = fresh_session.query(Entity).filter_by(normalized_name="example legal").one()

    # The represented entity's page lists its representative under
    # "Represented by" (not the reverse).
    represented_page = client.get(f"/entities/{subject_id}").get_data(as_text=True)
    assert "Represented by" in represented_page
    represented_block = represented_page.split("Represented by", 1)[1].split("Relationships", 1)[0]
    assert "Example Legal" in represented_block

    # The law firm's page lists the client under "Clients".
    law_firm_page = client.get(f"/entities/{law_firm.id}").get_data(as_text=True)
    assert "Clients" in law_firm_page
    clients_block = law_firm_page.split("Clients", 1)[1].split("Relationships", 1)[0]
    assert "Acme Development" in clients_block
