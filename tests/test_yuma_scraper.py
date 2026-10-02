from scraper.jurisdictions.yuma import parse_events, parse_items


def test_yuma_events_map_council_and_worksession():
    events = [{"EventId": 42, "EventDate": "2026-09-16T00:00:00",
               "EventBodyName": "City Council Worksession", "EventComment": None,
               "EventInSiteURL": "https://example.test/42", "EventGuid": "g",
               "EventAgendaFile": "https://example.test/a.pdf", "EventMinutesFile": None}]
    meeting = parse_events(events, 2026)[0]
    assert meeting["body_code"] == "yuma-cc"
    assert meeting["meeting_type"] == "Work Session"
    assert meeting["agenda_url"].endswith("a.pdf")


def test_yuma_items_ignore_boilerplate_and_link_documents():
    meeting = {"meeting_id": "42", "source_url": "https://example.test/42"}
    rows = [
        {"EventItemAgendaSequence": 1, "EventItemMatterId": None, "EventItemTitle": "Notice"},
        {"EventItemAgendaSequence": 2, "EventItemMatterId": 99,
         "EventItemMatterFile": "R2026-001", "EventItemTitle": "Water agreement",
         "EventItemActionText": "ADOPTED", "EventItemMatterAttachments": [{
             "MatterAttachmentName": "Staff report",
             "MatterAttachmentHyperlink": "https://example.test/report.pdf"}]},
    ]
    items, docs = parse_items(rows, meeting)
    assert len(items) == 1
    assert items[0]["agenda_item_number"] == "R2026-001"
    assert items[0]["agenda_item_text"] == "ADOPTED"
    assert docs[0]["document_url"].endswith("report.pdf")


def test_yuma_worksession_falls_back_to_non_matter_rows():
    meeting = {"meeting_id": "43", "source_url": "https://example.test/43"}
    rows = [{"EventItemAgendaSequence": 1, "EventItemMatterId": None,
             "EventItemTitle": "Notice is hereby given to the public"},
            {"EventItemAgendaSequence": 3, "EventItemMatterId": None,
             "EventItemTitle": "UTILITIES DEPARTMENT UPDATE"}]
    items, _ = parse_items(rows, meeting)
    assert len(items) == 1
    assert items[0]["agenda_item_number"] == "3"
