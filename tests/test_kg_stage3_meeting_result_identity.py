#!/usr/bin/env python3
"""B1 regression + adversarial tests for canonical Meeting Result item identity."""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage3_meeting_result_identity as B1  # noqa: E402

_FIXTURES = _REPO / "tests" / "fixtures" / "meeting_results"


def _fixture(prefix: str) -> str:
    hits = sorted(_FIXTURES.glob(f"{prefix}-doc*.txt"))
    if not hits:
        pytest.skip(f"no {prefix} fixture retained")
    return hits[0].read_text()


# --- deterministic normalization -------------------------------------------

def test_normalization_is_deterministic():
    assert B1.normalize_item_number("1.") == "1"
    assert B1.normalize_item_number("01") == "1"
    assert B1.normalize_item_number("2.A") == "2.A"
    assert B1.normalize_item_number("2.a") == "2.A"
    assert B1.normalize_item_number("10)") == "10"


def test_placeholder_and_unparseable_tokens_are_refused():
    for bad in (None, "", "0", "  ", "sec-3", "22C", "A", "iv"):
        assert B1.normalize_item_number(bad) is None


# --- real retained fixtures -------------------------------------------------

def test_a_real_document_yields_its_item_spans():
    text = _fixture("hold_missing_target")          # a real results PDF with numbered items
    spans = B1.extract_item_spans(text)
    assert spans, "the retained fixture proves numbered items"
    numbers = [B1.normalize_item_number(s["token"]) for s in spans]
    assert all(n for n in numbers)
    assert numbers[0] == "1"
    starts = [s["start"] for s in spans]
    assert starts == sorted(starts)


def test_a_real_document_without_item_lines_holds_with_missing_identity():
    verdict = B1.classify_document(text_content=_fixture("hold_missing_identity"),
                                   quarantined=False, existing_identity="result-a-b",
                                   canonical_numbers={"1", "2", "3"})
    assert verdict["disposition"] == "missing_identity"
    assert verdict["class"] == "hold_missing_identity"
    assert verdict["items"] == []


def test_a_real_document_that_partially_resolves_holds_fail_closed():
    """This fixture was classified ``malformed`` until the leading-whitespace cap was lifted;
    the widened matcher now extracts its items, so it holds as a partial-target case instead.
    The durable property is that it HOLDS and never promotes."""
    verdict = B1.classify_document(text_content=_fixture("hold_partial_target"),
                                   quarantined=False, existing_identity="result-a-b",
                                   canonical_numbers={"1", "2", "3"})
    assert verdict["class"] in ("hold_missing_target", "hold_malformed_identity",
                                "hold_missing_identity", "hold_ambiguous_identity")
    assert verdict["class"] != "resolved"
    assert verdict["disposition"] in ("missing_target", "malformed_identity",
                                      "missing_identity", "ambiguous_identity")


def test_a_genuinely_malformed_document_still_holds_fail_closed():
    """The malformed branch stays covered by synthetic input, independent of fixture drift."""
    verdict = B1.classify_document(text_content="sec-3. something\n22C. another\n",
                                   quarantined=False, existing_identity="result-a-b",
                                   canonical_numbers={"1", "2", "3"})
    assert verdict["class"] in ("hold_malformed_identity", "hold_missing_identity",
                                "hold_ambiguous_identity")
    assert verdict["class"] != "resolved"


def test_a_real_ambiguous_document_holds_fail_closed():
    text = _fixture("hold_ambiguous_identity")
    verdict = B1.classify_document(text_content=text, quarantined=False,
                                   existing_identity="result-a-b",
                                   canonical_numbers=set())
    assert verdict["class"] in ("hold_ambiguous_identity", "hold_missing_identity",
                                "hold_malformed_identity")


# --- synthetic adversarial cases -------------------------------------------

def _synthetic(*numbers: str) -> str:
    return "\n".join(f"{n}. Item {n} — motion carried." for n in numbers)


def test_canonical_identity_is_emitted_when_the_text_proves_it():
    verdict = B1.classify_document(text_content=_synthetic("1", "2", "3"),
                                   quarantined=False, existing_identity="result-a-b",
                                   canonical_numbers={"1", "2", "3"})
    assert verdict["disposition"] == "resolved"
    assert [i["target"] for i in verdict["items"]] == ["1", "2", "3"]
    assert all(i["resolved"] for i in verdict["items"])


def test_a_number_with_no_canonical_target_holds():
    verdict = B1.classify_document(text_content=_synthetic("1", "2"), quarantined=False,
                                   existing_identity="result-a-b", canonical_numbers=set())
    assert verdict["class"] == "hold_missing_target"
    assert all(not i["resolved"] for i in verdict["items"])


def test_a_partial_resolution_still_holds_the_document():
    verdict = B1.classify_document(text_content=_synthetic("1", "2"), quarantined=False,
                                   existing_identity="result-a-b", canonical_numbers={"1"})
    assert verdict["class"] == "hold_missing_target"
    assert verdict.get("partial") is True


def test_duplicate_numbers_in_one_document_are_ambiguous_not_guessed():
    verdict = B1.classify_document(text_content=_synthetic("1", "1"), quarantined=False,
                                   existing_identity="result-a-b", canonical_numbers={"1"})
    assert verdict["class"] == "hold_ambiguous_identity"
    assert verdict["items"] == []


def test_a_quarantined_or_empty_document_is_ineligible():
    for text, quar in (("", False), ("   ", False), ("text", True)):
        v = B1.classify_document(text_content=text, quarantined=quar,
                                 existing_identity="result-a-b", canonical_numbers={"1"})
        assert v["class"] == "ineligible_quarantined"


def test_an_already_canonical_document_replays():
    v = B1.classify_document(text_content=_synthetic("1"), quarantined=False,
                             existing_identity="phoenix-cc-123_1",
                             canonical_numbers={"1"})
    assert v["class"] == "replay"


def test_backward_compatibility_a_synthetic_result_key_is_meeting_level_not_an_item():
    """The legacy result-* key must not be mistaken for a canonical item identity."""
    v = B1.classify_document(text_content=_synthetic("1"), quarantined=False,
                             existing_identity="result-phoenix-cc-260722001R",
                             canonical_numbers={"1"})
    assert v["class"] == "resolved", "a meeting-level key is not an existing item identity"


# --- attribute_event: containment only -------------------------------------

def test_event_attribution_uses_coordinate_containment_only():
    spans = B1.extract_item_spans(_synthetic("1", "2", "3"))
    first = B1.attribute_event(spans, spans[0]["start"] + 1)
    assert first is spans[0]
    assert B1.attribute_event(spans, spans[1]["start"] + 1) is spans[1]
    assert B1.attribute_event(spans, 10 ** 9) is None
    assert B1.attribute_event(spans, None) is None


def test_resolve_number_is_exact_equality():
    assert B1.resolve_number("2", {"1", "2"}) == "2"
    assert B1.resolve_number("02", {"1", "2"}) is None
    assert B1.resolve_number("2.A", {"1", "2"}) is None


# --- forbidden techniques ---------------------------------------------------

def test_no_fuzzy_ordering_or_ai_is_used_for_identity():
    src = (_REPO / "scripts" / "kg" / "stage3_meeting_result_identity.py").read_text()
    tree = ast.parse(src)
    # Inspect the AST, not raw text: a docstring is prose, not a technique.
    imported = set()
    identifiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.lower() for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").lower())
        elif isinstance(node, ast.Name):
            identifiers.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr.lower())
    # Compare whole tokens, not substrings: "llm" occurs inside the real
    # identifier "fullmatch", and a substring hit would be a false positive.
    tokens = set()
    for name in imported | identifiers:
        for part in str(name).replace(".", "_").split("_"):
            if part:
                tokens.add(part)
    for forbidden in ("difflib", "levenshtein", "rapidfuzz", "sequencematcher",
                      "fuzzy", "openai", "anthropic", "llm"):
        assert forbidden not in tokens
    # meeting membership must never be an input to classify_document
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "classify_document":
            names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            assert "meeting_db_id" not in names
            assert "meeting_id" not in names
            assert "document_order" not in names


# --- live reconciliation ----------------------------------------------------

def test_the_live_projection_reconciles_exactly(pytestconfig=None):
    from scripts.db.core import get_engine
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("the projection reads the live development tier")
    with engine.connect() as c:
        b = B1.build_baseline(c)
    assert b["documents"]["total"] == sum(b["documents"]["by_class"].values())
    ev = b["events"]
    assert (ev["would_resolve"] + ev["hold_no_span"] + ev["hold_item_unresolved"]
            + ev["ineligible_document"]) == ev["total_events"] == 19588
    assert b["documents"]["total"] == 4196
