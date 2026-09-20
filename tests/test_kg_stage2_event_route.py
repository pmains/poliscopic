#!/usr/bin/env python3
"""Adversarial tests for the deterministic event -> agenda-item attachment route."""

from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.db.core import get_engine  # noqa: E402
from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage2_event_plan as P  # noqa: E402
from scripts.kg import stage2_event_route as R  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _pg():
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("these assertions read the live development tier")
    return engine


def _event(**over):
    e = {"id": 1, "meeting_id": "m1", "supporting_doc_id": 10, "agenda_item_id": None,
         "event_type_id": 5, "text_offset_start": 100, "text_offset_end": 120,
         "case_number": None, "doc_id": 10, "doc_agenda_item_id": None,
         "doc_agenda_item_db_id": None, "doc_agenda_item_number": "0",
         "doc_document_type": "Meeting Result", "doc_content_hash": None,
         "doc_meeting_db_id": 1, "extraction_quarantined_at": None, "extraction_id": 1}
    e.update(over)
    return e


def _routes(**over):
    r = {k: [] for k in R.ROUTES}
    r.update(over)
    return r


# --- exact-evidence promotion ----------------------------------------------

def test_an_exact_source_document_link_promotes():
    v = R.classify_event(_event(), _routes(source_document_canonical_link=["item-7"]),
                         span_supported=False)
    assert v["class"] == "would_link" and v["target"] == "item-7"
    assert v["route"] == "source_document_canonical_link"


def test_an_exact_canonical_source_key_promotes():
    v = R.classify_event(_event(), _routes(source_document_canonical_key=["item-9"]),
                         span_supported=False)
    assert v["class"] == "would_link" and v["route"] == "source_document_canonical_key"


def test_the_same_target_via_two_routes_is_still_unique():
    v = R.classify_event(_event(), _routes(source_document_canonical_link=["item-7"],
                                           source_document_canonical_key=["item-7"]),
                         span_supported=False)
    assert v["class"] == "would_link" and v["target"] == "item-7"


def test_two_distinct_targets_are_ambiguous():
    v = R.classify_event(_event(), _routes(source_document_canonical_link=["item-7"],
                                           source_document_canonical_key=["item-8"]),
                         span_supported=False)
    assert v["class"] == "hold_ambiguous" and sorted(v["candidates"]) == ["item-7", "item-8"]


# --- forbidden evidence -----------------------------------------------------

def test_meeting_co_membership_alone_is_never_evidence():
    """The classifier receives no meeting-matching input, so a co-member stays held."""
    e = _event(doc_meeting_db_id=1, meeting_id="m1")
    assert R.classify_event(e, _routes(), span_supported=False)["class"] == \
        "hold_missing_item_evidence"


def test_no_fuzzy_title_similarity_or_document_order_is_used():
    import ast
    src = (_REPO / "scripts" / "kg" / "stage2_event_route.py").read_text()
    doc = ast.get_docstring(ast.parse(src)) or ""
    lowered = src.replace(doc, "").lower()   # executable code, not prose
    for forbidden in ("similarity", "difflib", "levenshtein", "rapidfuzz", "sequencematcher"):
        assert forbidden not in lowered
    # only documented exact routes exist
    assert R.ROUTES == ("source_document_canonical_link", "source_document_canonical_key",
                        "coordinate_containment")


def test_coordinate_containment_cannot_contribute_without_a_span_source():
    v = R.classify_event(_event(), _routes(coordinate_containment=["item-7"]),
                         span_supported=False)
    assert v["class"] == "hold_missing_item_evidence"


def test_coordinate_containment_promotes_only_when_the_span_source_is_supported():
    v = R.classify_event(_event(), _routes(coordinate_containment=["item-7"]),
                         span_supported=True)
    assert v["class"] == "would_link" and v["route"] == "coordinate_containment"


# --- eligibility and replay -------------------------------------------------

def test_an_event_without_a_source_document_is_ineligible():
    v = R.classify_event(_event(supporting_doc_id=None, doc_id=None), _routes(),
                         span_supported=False)
    assert v["class"] == "ineligible"


def test_an_event_whose_source_document_is_missing_is_ineligible():
    v = R.classify_event(_event(doc_id=None), _routes(), span_supported=False)
    assert v["class"] == "ineligible"


def test_an_event_without_coordinates_is_ineligible():
    v = R.classify_event(_event(text_offset_start=None), _routes(), span_supported=False)
    assert v["class"] == "ineligible"


def test_a_quarantined_extraction_is_ineligible():
    v = R.classify_event(_event(extraction_quarantined_at="2026-01-01T00:00:00Z"), _routes(),
                         span_supported=False)
    assert v["class"] == "ineligible"


def test_an_already_linked_event_replays():
    v = R.classify_event(_event(agenda_item_id="item-3"), _routes(), span_supported=False)
    assert v["class"] == "replay" and v["target"] == "item-3"


def test_ineligibility_takes_precedence_over_replay():
    v = R.classify_event(_event(agenda_item_id="item-3", text_offset_start=None), _routes(),
                         span_supported=False)
    assert v["class"] == "ineligible"


# --- identity / fingerprints ------------------------------------------------

def test_event_fingerprints_are_deterministic_and_identity_bound():
    a = R.fingerprint_event(_event())
    assert a == R.fingerprint_event(_event())
    assert a != R.fingerprint_event(_event(id=2))
    assert a != R.fingerprint_event(_event(text_offset_start=999))


def test_the_classification_reconciles_exactly_once_over_the_population():
    with _pg().connect() as c:
        result = R.classify_all(c)
    assert result["reconciles"] is True
    assert sum(result["counts"].values()) == result["total"] == 19588
    ids = [e["event_id"] for e in result["events"]]
    assert len(set(ids)) == len(ids) == result["total"]
    assert set(result["counts"]) == set(R.CLASSES)


# --- ledger invariants ------------------------------------------------------

def test_the_live_ledger_is_a_hold_with_no_write_path():
    hits = sorted(_PLANS.glob("kg-stage2-event-hold-ledger-*.json"))
    if not hits:
        pytest.skip("no hold ledger yet")
    doc = A.load_verified(hits[-1])
    assert any("stage2_subitem_schema_plan.py" in problem
               for problem in P.validate_plan(doc))
    assert doc["mode"] == "hold"
    assert doc["deterministic_population"] == 0
    assert doc["operations"] == []
    assert doc["write_path"] == "absent by design"
    assert doc["hold"]["no_mutation"] is True
    assert doc["applied"] is False
    assert len(doc["ledger"]) == 19588
    assert doc["upstream_backlog"]["items"]


def test_a_hold_with_a_write_path_is_refused():
    hits = sorted(_PLANS.glob("kg-stage2-event-hold-ledger-*.json"))
    if not hits:
        pytest.skip("no hold ledger yet")
    doc = copy.deepcopy(A.load_verified(hits[-1]))
    doc["write_path"] = "governed apply"
    doc.pop(A.DIGEST_FIELD, None)
    doc[A.DIGEST_FIELD] = A.compute_digest(doc)
    assert any("write path" in p for p in P.validate_plan(doc))


def test_a_hold_claiming_mutation_is_refused():
    hits = sorted(_PLANS.glob("kg-stage2-event-hold-ledger-*.json"))
    if not hits:
        pytest.skip("no hold ledger yet")
    doc = copy.deepcopy(A.load_verified(hits[-1]))
    doc["hold"]["no_mutation"] = False
    doc.pop(A.DIGEST_FIELD, None)
    doc[A.DIGEST_FIELD] = A.compute_digest(doc)
    assert any("no mutation" in p for p in P.validate_plan(doc))


def test_a_ledger_missing_an_event_is_refused():
    hits = sorted(_PLANS.glob("kg-stage2-event-hold-ledger-*.json"))
    if not hits:
        pytest.skip("no hold ledger yet")
    doc = copy.deepcopy(A.load_verified(hits[-1]))
    doc["ledger"] = doc["ledger"][:-1]
    doc.pop(A.DIGEST_FIELD, None)
    doc[A.DIGEST_FIELD] = A.compute_digest(doc)
    assert any("cover every event" in p for p in P.validate_plan(doc))


def test_an_operation_without_exact_evidence_is_refused():
    hits = sorted(_PLANS.glob("kg-stage2-event-hold-ledger-*.json"))
    if not hits:
        pytest.skip("no hold ledger yet")
    doc = copy.deepcopy(A.load_verified(hits[-1]))
    doc["deterministic_population"] = 1
    doc["operations"] = [{"event_id": 1, "target": None, "route": None,
                          "class": "would_link"}]
    doc["mode"] = "dry-run"
    doc["write_path"] = "governed apply"
    doc.pop(A.DIGEST_FIELD, None)
    doc[A.DIGEST_FIELD] = A.compute_digest(doc)
    assert any("exact evidence" in p for p in P.validate_plan(doc))
