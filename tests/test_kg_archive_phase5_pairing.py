"""Regression protection for the historical positional-linking defect.

The archived ``phase5_normalizer`` paired each extraction with a generated
``meeting_events`` id by position (``zip(rows, event_ids)``).  A candidate that
was skipped shrank the event list, so every later pair shifted — which is how
extraction ids 24195-24606 were mislinked in ``poliscopic_dev``.

These tests prove the archive can no longer mislink, and that it refuses to run
without an explicit override.  They touch no database: the pairing helper is pure,
and the refuse-to-run gate fires before any engine is used.
"""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path

import pytest

_ARCHIVE_PATH = (Path(__file__).resolve().parents[1]
                 / "scripts" / "entities" / "archive" / "phase5_normalizer.py")


@lru_cache(maxsize=1)
def load_archive():
    """Import the archived normalizer by path without collecting it as a test."""
    spec = importlib.util.spec_from_file_location("phase5_normalizer_under_test",
                                                  _ARCHIVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- the pairing helper -------------------------------------------------

def test_pairing_pairs_each_extraction_with_its_own_event():
    p5 = load_archive()
    assert p5.pair_events_with_extractions([10, 11, 12], [500, 501, 502]) == [
        (10, 500), (11, 501), (12, 502),
    ]


def test_shortened_insert_result_cannot_mislink():
    """One candidate lost an event row: refuse rather than shift every pair."""
    p5 = load_archive()
    with pytest.raises(p5.PairingCardinalityError):
        p5.pair_events_with_extractions([10, 11, 12], [500, 501])


def test_skipped_candidate_cannot_mislink():
    """More candidates than events is the exact skip that caused the shift."""
    p5 = load_archive()
    with pytest.raises(p5.PairingCardinalityError):
        p5.pair_events_with_extractions([10, 11, 12, 13], [500, 501, 502])


def test_reordered_rows_cannot_mislink():
    """Ids out of submission order would silently reverse the pairing."""
    p5 = load_archive()
    with pytest.raises(p5.PairingOrderError):
        p5.pair_events_with_extractions([10, 11, 12], [502, 500, 501])


def test_duplicate_event_ids_are_refused():
    p5 = load_archive()
    with pytest.raises(p5.PairingCardinalityError):
        p5.pair_events_with_extractions([10, 11], [500, 500])


def test_duplicate_extraction_ids_are_refused():
    p5 = load_archive()
    with pytest.raises(p5.PairingCardinalityError):
        p5.pair_events_with_extractions([10, 10], [500, 501])


def test_empty_batch_pairs_nothing():
    p5 = load_archive()
    assert p5.pair_events_with_extractions([], []) == []


def test_archive_source_has_no_positional_zip_pairing():
    """The defect site must not reappear as executable code.

    Parsed rather than string-matched, so the docstring that *describes* the old
    ``zip(rows, event_ids)`` bug cannot make this pass or fail.
    """
    import ast

    source = _ARCHIVE_PATH.read_text()
    tree = ast.parse(source)
    zip_calls = [
        ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "zip"
    ]
    # No zip may pair the candidate row list with the returned event ids.
    assert not [call for call in zip_calls if "rows" in call and "event_ids" in call]
    # And the guarded helper is what the write path actually calls.
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "pair_events_with_extractions"
        for node in ast.walk(tree)
    )


# -- the refuse-to-run gate --------------------------------------------

def test_override_gate_refuses_without_env(monkeypatch):
    p5 = load_archive()
    monkeypatch.delenv(p5.SUPERSEDED_ENV, raising=False)
    with pytest.raises(RuntimeError, match="superseded"):
        p5.assert_superseded_execution_allowed()


def test_override_gate_allows_explicit_opt_in(monkeypatch):
    p5 = load_archive()
    monkeypatch.setenv(p5.SUPERSEDED_ENV, "1")
    p5.assert_superseded_execution_allowed()


def test_normalize_refuses_a_live_run_before_using_the_engine(monkeypatch):
    """The gate fires first, so a sentinel engine is never touched."""
    p5 = load_archive()
    monkeypatch.delenv(p5.SUPERSEDED_ENV, raising=False)
    with pytest.raises(RuntimeError, match="superseded"):
        p5.normalize(object(), dry_run=False)


def test_main_refuses_a_live_run_without_override(monkeypatch):
    p5 = load_archive()
    monkeypatch.delenv(p5.SUPERSEDED_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["phase5_normalizer.py"])
    with pytest.raises(RuntimeError, match="superseded"):
        p5.main()
