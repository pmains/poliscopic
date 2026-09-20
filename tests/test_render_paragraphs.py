"""Regression tests for the newsletter HTML renderer's paragraph handling.

Brief 012 follow-up (2026-08-30): the weekly roundup's multi-paragraph
overview rendered as one wall of text because render.py stuffed the whole
summary into a single <p> tag (HTML collapses newlines). The fix splits
on blank lines and emits one <p> per paragraph. These tests lock that in
for every newsletter that uses the shared renderer.
"""

import re

import pytest

from workflows.templates.render import render_newsletter

SUMMARY_DIV = re.compile(r'<div style="background: #f5f5f5.*?</div>', re.S)


def _summary_div(html: str) -> str:
    match = SUMMARY_DIV.search(html)
    assert match, "expected a summary div in the rendered HTML"
    return match.group(0)


def test_single_paragraph_summary_renders_one_p():
    """Topic newsletters' 3–6 sentence overviews stay a single <p> (no regression)."""
    html = render_newsletter(
        "Housing", "August 30, 2026", [],
        summary="One paragraph only. A few sentences, like a topic overview.",
    )
    assert _summary_div(html).count("<p ") == 1


def test_multi_paragraph_summary_renders_one_p_per_paragraph():
    """Weekly-roundup overviews (up to 5 paragraphs) split on blank lines."""
    html = render_newsletter(
        "Weekly Roundup", "August 30, 2026", [],
        summary="First paragraph of the week in review.\n\nSecond paragraph.\n\nThird paragraph.",
    )
    assert _summary_div(html).count("<p ") == 3


def test_summary_paragraphs_preserve_text_order():
    """Paragraph text must appear in order across the <p> tags."""
    html = render_newsletter(
        "Weekly Roundup", "August 30, 2026", [],
        summary="Alpha paragraph.\n\nBeta paragraph.",
    )
    div = _summary_div(html)
    first = div.index("Alpha paragraph.")
    second = div.index("Beta paragraph.")
    assert first < second


def test_empty_summary_renders_no_div():
    html = render_newsletter("Housing", "August 30, 2026", [], summary="")
    assert SUMMARY_DIV.search(html) is None


@pytest.mark.parametrize("summary,expected", [
    ("A.\n\nB.", 2),
    ("A.\n\nB.\n\nC.\n\nD.\n\nE.", 5),
    ("A.", 1),
])
def test_paragraph_counts(summary, expected):
    html = render_newsletter("Weekly Roundup", "August 30, 2026", [], summary=summary)
    assert _summary_div(html).count("<p ") == expected
