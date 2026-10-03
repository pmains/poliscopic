"""Persistence contracts for the registry-owned El Mirage adapter."""

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


def test_sync_deduplicates_items_and_links_documents(monkeypatch):
    import db
    from scraper.jurisdictions import el_mirage_adapter

    session = _Session()
    persisted = []
    meeting = {
        "meeting_id": "314",
        "meeting_date": "2026-09-08",
        "body_code": "el-mirage-cc",
        "agenda_url": "https://example.test/agenda",
        "meeting_type": "Regular Meeting",
        "body_name": "City Council",
    }
    repeated_item = {
        "agenda_item_number": "4B2",
        "agenda_item_title": "Minutes",
        "agenda_item_text": "Approve minutes",
        "agenda_item_url": "https://example.test/memo",
        "sort_order": 1,
    }

    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setattr(db, "get_session", lambda: session)
    monkeypatch.setattr(
        db,
        "replace_meeting_data_safe",
        lambda *args, **kwargs: persisted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        el_mirage_adapter,
        "search_el_mirage_meetings",
        lambda *_args, **_kwargs: [meeting],
    )
    monkeypatch.setattr(el_mirage_adapter, "fetch_page", lambda _url: "<html>")
    monkeypatch.setattr(
        el_mirage_adapter,
        "parse_agenda_items",
        lambda *_args: [repeated_item, dict(repeated_item)],
    )
    monkeypatch.setattr(
        el_mirage_adapter,
        "fetch_agenda_memo_docs",
        lambda *_args, **_kwargs: [{"document_url": "https://example.test/doc.pdf"}],
    )

    result = el_mirage_adapter.sync(
        SimpleNamespace(bodies=None, month=None, year="2026", limit=0, force=False)
    )

    assert result == 0
    assert session.closed is True
    assert len(persisted) == 1
    args, kwargs = persisted[0]
    assert len(args[4]) == 1
    assert args[4][0]["agenda_item_id"] == "el-mirage-cc-314_4B2"
    assert kwargs["supporting_doc_dicts"] == [
        {
            "document_url": "https://example.test/doc.pdf",
            "agenda_item_id": "0",
            "agenda_item_number": "4B2",
        }
    ]
