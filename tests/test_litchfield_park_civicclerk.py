from argparse import Namespace

from scraper import cli
from scraper.jurisdictions import litchfield_park as lp
from scraper.platforms.civicclerk import parse_events_to_meetings, resolve_body


def test_all_public_civicclerk_categories_have_stable_body_codes():
    expected = {
        "City Council": "litchfield-park-cc",
        "Planning and Zoning Commission": "litchfield-park-pz",
        "Board of Adjustment": "litchfield-park-boa",
        "Design Review Board": "litchfield-park-drb",
        "Community Facilities District": "litchfield-park-cfd",
        "Recreation and Public Grounds Commission": "litchfield-park-rpgc",
        "General": "litchfield-park-general",
    }
    assert {name: resolve_body(lp.CONFIG, name)[1] for name in expected} == expected


def test_event_parsing_keeps_official_agenda_and_minutes_urls():
    event = {
        "id": 4212,
        "eventDate": "2026-10-01T18:00:00Z",
        "eventName": "Design Review Board Meeting",
        "categoryName": "Design Review Board",
        "publishedFiles": [
            {"type": "Agenda", "fileId": 7697, "name": "Agenda"},
            {"type": "Minutes", "fileId": 7700, "name": "Minutes"},
        ],
    }
    meeting = parse_events_to_meetings(lp.CONFIG, [event])[0]
    assert meeting["body_code"] == "litchfield-park-drb"
    assert "fileId=7697" in meeting["agenda_url"]
    assert "fileId=7700" in meeting["minutes_url"]
    assert meeting["source_url"].endswith("/event/4212/overview")


def test_cli_recognizes_litchfield_park():
    args = cli.parse_args(["litchfield-park", "--sync", "--limit=1"])
    assert isinstance(args, Namespace)
    assert args.source == "litchfield-park"
    assert args.sync is True
    assert args.limit == 1


def test_litchfield_child_outlines_are_qualified_by_parent(monkeypatch):
    from scraper.platforms import civicclerk

    payload = {
        "publishedFiles": [
            {"type": "Agenda", "fileId": 7697, "name": "Agenda",
             "url": "https://example.invalid/metadata"},
        ],
        "items": [
            {"agendaObjectItemOutlineNumber": "I.", "agendaObjectItemName": "Opening",
             "childItems": [{"agendaObjectItemOutlineNumber": "A.",
                              "agendaObjectItemName": "Notice"}]},
            {"agendaObjectItemOutlineNumber": "IV.", "agendaObjectItemName": "Business",
             "childItems": [{"agendaObjectItemOutlineNumber": "A.",
                              "agendaObjectItemName": "Minutes",
                              "attachmentsList": [{
                                  "fileName": "Prior minutes",
                                  "mediaFullPath": "stream/file.docx",
                                  "pdfVersionFullPath": "https://blob.example/minutes.pdf?sig=x",
                              }]}]},
        ],
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            import json
            return json.dumps(payload).encode()

    monkeypatch.setattr(civicclerk.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: Response())
    items, documents = civicclerk.fetch_meeting_items(
        lp.CONFIG, 4212, 5215, "litchfield-park-drb", "2026-10-01"
    )
    numbers = [item["agenda_item_number"] for item in items]
    assert "I.A" in numbers
    assert "IV.A" in numbers
    assert len({item["agenda_item_id"] for item in items}) == len(items)
    assert any("GetMeetingFileStream(fileId=7697" in doc["document_url"]
               for doc in documents)
    assert any(doc["document_url"] == "https://blob.example/minutes.pdf?sig=x"
               for doc in documents)
