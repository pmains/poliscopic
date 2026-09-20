"""Isolated behavioral tests for the shared phoenix-aem sentinel guard.

No database and no network.  ``phoenix_aem`` imports ``pypdf`` at module scope,
which is absent in this environment, so absent third-party dependencies are
stubbed before import; the guard code under test is the real module code.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


def _import_phoenix_aem():
    """Import the real module, stubbing any absent third-party dependency."""
    for _ in range(8):
        try:
            return importlib.import_module("scraper.jurisdictions.phoenix_aem")
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if not missing or missing.startswith(("scraper", "db")):
                raise
            stub = types.ModuleType(missing)
            stub.__getattr__ = lambda name: type(name, (), {})
            sys.modules[missing] = stub
    raise RuntimeError("could not import phoenix_aem")


aem = _import_phoenix_aem()
is_non_meeting = aem.is_non_meeting
is_sentinel_body = aem.is_sentinel_body
resolve_body = aem.resolve_body
resolve_persistable_body = aem.resolve_persistable_body

NON_MEETING_TITLES = [
    "Fire Station 5 Grand Opening",
    "Ribbon Cutting Ceremony",
    "Groundbreaking",
    "Open House",
    "Enhanced Municipal Services District (EMSD) Advisory Board - Annual Notice",
]
VALID_TITLE = "City Council Formal Meeting"


@pytest.mark.parametrize(
    "value", ["", "   ", "__skip__", "SKIP", "skip", "none", "NULL", "nan", None]
)
def test_sentinel_values_are_detected(value):
    assert is_sentinel_body(value) is True


@pytest.mark.parametrize("value", ["phoenix-cc", "phoenix-dr", "bos", "phoenix-dab"])
def test_real_body_values_are_not_sentinels(value):
    assert is_sentinel_body(value) is False


@pytest.mark.parametrize("title", NON_MEETING_TITLES)
def test_non_meeting_titles_are_never_persistable(title):
    assert is_non_meeting(title) is True
    assert resolve_persistable_body(title) is None


def test_sentinel_resolution_is_rejected():
    assert resolve_body("Fire Station 5 Grand Opening") == ("__skip__", "__skip__")
    assert resolve_persistable_body("Fire Station 5 Grand Opening") is None


def test_empty_title_is_rejected():
    assert resolve_persistable_body("") is None


def test_valid_title_still_resolves_to_a_real_body():
    resolved = resolve_persistable_body(VALID_TITLE)
    assert resolved is not None
    slug, code = resolved
    assert not is_sentinel_body(slug)
    assert not is_sentinel_body(code)
    assert code != "__skip__"


def test_persistence_is_never_called_for_sentinels():
    """Mirrors the incremental --sync-results decision order exactly."""
    converted, persisted = [], []
    raws = [{"title": t} for t in NON_MEETING_TITLES] + [{"title": VALID_TITLE}]

    for raw in raws:
        resolved = resolve_persistable_body(raw.get("title", "") or "")
        if resolved is None:
            continue
        converted.append(resolved)
        persisted.append(resolved)

    assert len(converted) == 1
    assert len(persisted) == 1
    assert persisted[0][1] != "__skip__"


def test_valid_results_still_persist():
    raws = [{"title": VALID_TITLE}]
    persisted = [r for raw in raws
                 if (r := resolve_persistable_body(raw["title"])) is not None]
    assert persisted == [resolve_persistable_body(VALID_TITLE)]


def test_incremental_path_uses_the_shared_guard():
    main_src = open("scripts/scraper/main.py", encoding="utf-8").read()
    aem_src = open("scripts/scraper/jurisdictions/phoenix_aem.py", encoding="utf-8").read()
    assert "resolve_persistable_body" in main_src
    assert aem_src.count("resolve_persistable_body") >= 2


def test_ordinary_path_no_longer_hardcodes_the_sentinel():
    aem_src = open("scripts/scraper/jurisdictions/phoenix_aem.py", encoding="utf-8").read()
    assert 'if slug == "__skip__":' not in aem_src
