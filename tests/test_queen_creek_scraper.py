"""Queen Creek source identity regressions."""

from scraper.jurisdictions import queen_creek


def test_planning_rss_meeting_keeps_its_canonical_body_code(monkeypatch):
    monkeypatch.setattr(
        queen_creek,
        "fetch_rss",
        lambda: """
            <rss><channel><item>
              <title>Planning and Zoning Commission - October 7, 2026</title>
              <link>https://queencreekaz.granicus.com/?event_id=321</link>
            </item></channel></rss>
        """,
    )

    meetings = queen_creek.search_meetings()

    assert len(meetings) == 1
    assert meetings[0]["body_slug"] == "queen-creek-pz"
    assert meetings[0]["body_code"] == "queen-creek-pz"
