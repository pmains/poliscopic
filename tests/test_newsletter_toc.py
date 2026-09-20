"""Tests for the newsletter table-of-contents filter (Pete 2026-09-17).

The filter parses a newsletter article body into a lede + anchor-able
meeting sections and groups sections by body, so a body that meets twice
(an upcoming meeting plus last week's) shows BOTH dates in the TOC.

Pure string work — no database involved.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import create_app  # noqa: E402

BODY = """The council took up water policy this week.

More lede text here.

## Chandler City Council — September 14, 2026

- **Item one:** Something happened. [View item](/meetings/chandler-cc/1#item-1)

## Mesa City Council — September 17, 2026

- **Item two:** Another thing. [View item](/meetings/mesa-cc/2#item-2)

## Chandler City Council — September 17, 2026

- **Item three:** Third thing. [View item](/meetings/chandler-cc/3#item-3)
"""


@pytest.fixture(scope="module")
def toc():
    app = create_app()
    return app.jinja_env.filters["newsletter_sections"]


def test_filter_is_registered(toc):
    assert callable(toc)


def test_lede_split_from_sections(toc):
    out = toc(BODY)
    assert out["count"] == 3
    assert "water policy" in out["lede_html"]
    # the lede must not be repeated inside the sections HTML
    assert "water policy" not in out["body_html"]


def test_headings_get_anchors(toc):
    out = toc(BODY)
    assert '<h2 id="chandler-city-council-september-14-2026">' in out["body_html"]
    assert '<h2 id="mesa-city-council-september-17-2026">' in out["body_html"]


def test_duplicate_body_groups_both_dates(toc):
    """A body with an upcoming AND a past-week section shows both."""
    out = toc(BODY)
    assert [g["name"] for g in out["groups"]] == [
        "Chandler City Council", "Mesa City Council",
    ]
    chandler = out["groups"][0]
    assert len(chandler["entries"]) == 2
    assert [e["date"] for e in chandler["entries"]] == [
        "September 14, 2026", "September 17, 2026",
    ]
    # distinct anchors so both are individually jumpable
    assert chandler["entries"][0]["id"] != chandler["entries"][1]["id"]


def test_single_meeting_body_has_one_entry(toc):
    out = toc(BODY)
    mesa = out["groups"][1]
    assert len(mesa["entries"]) == 1


def test_all_anchor_ids_unique(toc):
    out = toc(BODY)
    ids = [e["id"] for g in out["groups"] for e in g["entries"]]
    assert len(ids) == len(set(ids))


def test_labels_are_human_readable_not_slugs(toc):
    """TOC labels must never surface a raw machine slug."""
    out = toc(BODY)
    for g in out["groups"]:
        for e in g["entries"]:
            assert "-cc" not in e["label"]
            assert e["label"].isupper() is False


def test_body_without_headings_returns_lede_only(toc):
    out = toc("Just prose, no meetings at all.")
    assert out["count"] == 0
    assert out["groups"] == []
    assert "Just prose" in out["lede_html"]


def test_empty_input_is_safe(toc):
    out = toc("")
    assert out["count"] == 0
    assert out["groups"] == []
