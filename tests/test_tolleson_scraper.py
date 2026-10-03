"""Contracts for the registry-owned Tolleson CivicClerk adapter."""

from types import SimpleNamespace


class _Result:
    def scalar_one_or_none(self):
        return None


class _Session:
    def __init__(self):
        self.closed = False

    def execute(self, _statement):
        return _Result()

    def close(self):
        self.closed = True


def test_sync_persists_items_and_supporting_documents(monkeypatch):
    import db
    from scraper.jurisdictions import tolleson

    session = _Session()
    persisted = []
    meeting = {
        "event_id": 1964,
        "meeting_id": "1964",
        "meeting_date": "2026-08-25",
        "body_code": "tolleson-cc",
        "meeting_type": "City Council",
        "meeting_title": "Regular Meeting",
        "source_url": "https://example.test/1964",
    }
    items = [{"agenda_item_number": "4B2"}]
    documents = [{"document_url": "https://example.test/minutes.pdf"}]

    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setattr(db, "get_session", lambda: session)
    monkeypatch.setattr(
        db,
        "replace_meeting_data_safe",
        lambda *args, **kwargs: persisted.append((args, kwargs)),
    )
    monkeypatch.setattr(tolleson, "search_meetings", lambda *_args, **_kwargs: [meeting])
    monkeypatch.setattr(tolleson, "_agenda_id", lambda _event_id: 22853)
    monkeypatch.setattr(
        tolleson,
        "fetch_meeting_items",
        lambda *_args, **_kwargs: (items, documents),
    )

    result = tolleson.sync(
        SimpleNamespace(
            year="2026",
            start_date="2026-01-01",
            end_date="2026-12-31",
            limit=0,
            force=False,
        )
    )

    assert result == 0
    assert session.closed is True
    assert len(persisted) == 1
    args, kwargs = persisted[0]
    assert args[1:5] == ("tolleson-cc", "1964", {
        "meeting_id": "1964",
        "meeting_date": "2026-08-25",
        "meeting_type": "City Council",
        "meeting_title": "Regular Meeting",
        "source_url": "https://example.test/1964",
    }, items)
    assert kwargs == {"supporting_doc_dicts": documents}


def test_agenda_lookup_failure_is_nonfatal(monkeypatch):
    from scraper.jurisdictions import tolleson

    monkeypatch.setattr(tolleson.urllib.request, "urlopen", lambda *_a, **_k: 1 / 0)

    assert tolleson._agenda_id(1964) == 0
