"""Pure-layer tests for normalization candidate construction.

No database, no pipeline, no subprocess.  Everything here is in-memory.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from scripts.entities.event_normalize_models import (
    SOURCE_SYSTEM,
    CandidateError,
    NormalizationCandidate,
    normalize_action_verb,
    normalize_nullable_string,
    normalize_offsets,
)
from scripts.entities.event_normalize_snapshot import ExistingNormalizedEvent
from scripts.entities.event_normalize_storage import (
    analyzed_text_hash,
    fetch_normalization_page,
)

from _kg_event_normalize_sqlite import (
    DEFAULT_TEXT,
    SOURCE_BYTES_HASH,
    build_engine,
    seed,
)


def candidate(**overrides) -> NormalizationCandidate:
    kwargs = dict(
        extraction_id=1,
        supporting_document_id=10,
        meeting_db_id=500,
        meeting_source_id="2024-03-05-CC",
        public_body_id="B-1",
        jurisdiction_id="J-1",
        action_verb="approved",
        content_hash="hash-a",
        extraction_method="pdftotext",
        confidence=0.9,
    )
    kwargs.update(overrides)
    return NormalizationCandidate.create(**kwargs)


def rich_candidate(**overrides) -> NormalizationCandidate:
    """A candidate with a populated span and case number."""
    kwargs = dict(span_start=100, span_end=140, case_number="CV2024-001")
    kwargs.update(overrides)
    return candidate(**kwargs)


# -- meeting reference semantics ---------------------------------------------


def test_canonical_meeting_db_identity_drives_the_context():
    a = candidate(meeting_db_id=500)
    b = candidate(meeting_db_id=501)

    assert a.meeting_identity.kind == "context"
    assert dict(a.meeting_identity.parts)["context_id"] == "500"
    assert a.meeting_identity.digest != b.meeting_identity.digest


def test_meeting_identity_ignores_the_external_reference_string():
    """Only the canonical database id builds the civic meeting identity."""
    base = candidate(meeting_db_id=500, meeting_source_id="2024-03-05-CC")
    renamed = candidate(meeting_db_id=500, meeting_source_id="2024-03-05-CC-RENAMED")

    assert base.meeting_source_id != renamed.meeting_source_id
    assert base.meeting_identity.digest == renamed.meeting_identity.digest


def test_same_meeting_db_id_under_different_bodies_differs_by_context():
    a = candidate(meeting_db_id=500, meeting_source_id="M-1", public_body_id="B-1")
    b = candidate(meeting_db_id=500, meeting_source_id="M-1", public_body_id="B-2")
    assert a.meeting_identity.digest != b.meeting_identity.digest


def test_meeting_reference_construction_fails_closed():
    with pytest.raises(CandidateError):
        candidate(meeting_db_id=None)
    with pytest.raises(CandidateError):
        candidate(meeting_db_id="not-an-integer")
    with pytest.raises(CandidateError):
        candidate(meeting_source_id=None)
    with pytest.raises(CandidateError):
        candidate(meeting_source_id="   ")


# -- civic context chain ------------------------------------------------------


def test_context_chain_is_jurisdiction_to_body_to_meeting():
    c = candidate()

    assert c.jurisdiction_identity.kind == "context"
    assert c.body_identity.kind == "context"
    assert c.meeting_identity.kind == "context"
    assert c.evidence_identity.kind == "evidence"
    assert c.extraction_identity.kind == "extraction"

    assert dict(c.meeting_identity.parts)["parent"] == c.body_identity.digest
    assert dict(c.body_identity.parts)["parent"] == c.jurisdiction_identity.digest
    # The jurisdiction is the chain root: its parent is the explicit sentinel.
    assert dict(c.jurisdiction_identity.parts)["parent"] == "<none>"

    other = candidate(public_body_id="B-2")
    assert other.body_identity.digest != c.body_identity.digest
    assert other.meeting_identity.digest != c.meeting_identity.digest


def test_context_identities_are_scoped_to_the_source_system():
    assert SOURCE_SYSTEM == "poliscopic"
    assert candidate().meeting_identity.digest == candidate().meeting_identity.digest
    assert (
        candidate(meeting_db_id=501).meeting_identity.digest
        != candidate(meeting_db_id=500).meeting_identity.digest
    )


# -- outcomes -----------------------------------------------------------------


def test_unqualified_outcome_is_a_bare_base():
    plain = candidate(action_verb="approved")
    assert plain.event_type == "approval"
    assert plain.outcome.base == "approved"
    assert plain.outcome.qualifier is None
    assert plain.is_qualified_outcome is False


def test_qualified_outcomes_split_into_base_and_qualifier():
    qualified = candidate(action_verb="approved_with_conditions")
    assert qualified.event_type == "approval"
    assert qualified.outcome.base == "approved"
    assert qualified.outcome.qualifier == "with_conditions"
    assert qualified.is_qualified_outcome is True

    denied = candidate(action_verb="denied_without_prejudice")
    assert denied.event_type == "denial"
    assert denied.outcome.base == "denied"
    assert denied.outcome.qualifier == "without_prejudice"


# -- dotted slug to canonical leaf -------------------------------------------


@pytest.mark.parametrize(
    ("verb", "leaf"),
    (
        ("approved", "approval"),
        ("approved_with_conditions", "approval"),
        ("denied", "denial"),
        ("denied_without_prejudice", "denial"),
        ("continued", "continuation"),
        ("tabled", "continuation"),
        ("adopted", "adoption"),
        ("introduced", "introduction"),
        ("amended", "amendment"),
        ("received", "receipt"),
        ("discussed", "discussion"),
        ("called_to_order", "discussion"),
        ("no_action", "discussion"),
        ("no_response", "discussion"),
    ),
)
def test_dotted_slug_normalizes_to_the_canonical_leaf(verb, leaf):
    event_type, _outcome = normalize_action_verb(verb)
    assert event_type == leaf, verb
    assert "." not in event_type


def test_verbs_without_a_mapping_fail_closed():
    with pytest.raises(CandidateError):
        normalize_action_verb("not_a_real_verb")
    with pytest.raises(CandidateError):
        normalize_action_verb("")


# -- evidence identity --------------------------------------------------------


def test_evidence_identity_pins_content_version_and_offsets():
    base = candidate(span_start=100, span_end=140)
    same = candidate(span_start=100, span_end=140)
    assert base.evidence_identity.digest == same.evidence_identity.digest

    changed_content = candidate(span_start=100, span_end=140, content_hash="hash-b")
    assert changed_content.evidence_identity.digest != base.evidence_identity.digest

    changed_span = candidate(span_start=101, span_end=140)
    assert changed_span.evidence_identity.digest != base.evidence_identity.digest


def test_evidence_identity_is_not_satisfied_by_an_extraction_method_alone():
    first = candidate(supporting_document_id=10, content_hash="h1")
    second = candidate(supporting_document_id=11, content_hash="h1")
    assert first.evidence_identity.digest != second.evidence_identity.digest


# -- normalization helpers ----------------------------------------------------


def test_absent_offsets_are_accepted_and_invalid_offsets_rejected():
    assert normalize_offsets(None, None) is None
    assert normalize_offsets(10, 10) == (10, 10)

    c = candidate(span_start=None, span_end=None)
    assert c.span_start is None and c.span_end is None

    with pytest.raises(CandidateError):
        normalize_offsets(10, None)
    with pytest.raises(CandidateError):
        normalize_offsets(None, 20)
    with pytest.raises(CandidateError):
        normalize_offsets(40, 10)


def test_absent_and_empty_case_numbers_normalize_together():
    assert normalize_nullable_string(None) is None
    assert normalize_nullable_string("") is None
    assert normalize_nullable_string("   ") is None
    assert normalize_nullable_string(" CA-1 ") == "CA-1"


# -- missing context ----------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    (
        "extraction_id",
        "supporting_document_id",
        "meeting_db_id",
        "meeting_source_id",
        "public_body_id",
        "jurisdiction_id",
    ),
)
def test_missing_context_member_fails_construction(missing):
    with pytest.raises(CandidateError):
        candidate(**{missing: None})


def test_missing_knowledge_content_version_fails_construction():
    with pytest.raises(CandidateError):
        candidate(content_hash=None)
    with pytest.raises(CandidateError):
        candidate(content_hash="")
    with pytest.raises(CandidateError):
        candidate(content_hash="   ")


@pytest.mark.parametrize(
    "method",
    (None, "", "bogus_method", "pdftotext-failed", "quarantine:oversized",
     "failed", "tesseract"),
)
def test_unsupported_extraction_method_fails_construction(method):
    with pytest.raises(CandidateError):
        candidate(extraction_method=method)


def test_no_fabricated_stored_content_hash_field():
    """No pretend provenance: a stored content hash must not exist."""
    snapshot_fields = {f.name for f in dataclasses.fields(ExistingNormalizedEvent)}
    candidate_fields = {f.name for f in dataclasses.fields(NormalizationCandidate)}

    # The snapshot of stored state carries no content version at all.
    assert not any("hash" in name for name in snapshot_fields), snapshot_fields
    # The candidate has exactly one content hash, and nothing claiming to be the
    # stored or historical one.
    assert {n for n in candidate_fields if "hash" in n} == {"content_hash"}
    assert not any(n.startswith("stored_") for n in candidate_fields)


# -- candidate construction from a stored row --------------------------------


@pytest.fixture()
def engine():
    return build_engine()


def test_complete_jurisdiction_body_meeting_join(engine):
    ids = seed(engine)
    c = fetch_normalization_page(engine).candidates[0]

    assert dict(c.jurisdiction_identity.parts)["context_id"] == str(ids["jid"])
    assert dict(c.body_identity.parts)["context_id"] == str(ids["bid"])
    assert dict(c.meeting_identity.parts)["context_id"] == str(ids["mid"])

    # body -> jurisdiction, meeting -> body, exactly.
    assert dict(c.body_identity.parts)["parent"] == c.jurisdiction_identity.digest
    assert dict(c.meeting_identity.parts)["parent"] == c.body_identity.digest
    assert dict(c.jurisdiction_identity.parts)["parent"] == "<none>"


def test_analyzed_text_hash_is_over_the_text_not_the_source_bytes(engine):
    seed(engine, text_content=DEFAULT_TEXT, doc_content_hash=SOURCE_BYTES_HASH)
    c = fetch_normalization_page(engine).candidates[0]

    assert c.content_hash == hashlib.sha256(DEFAULT_TEXT.encode("utf-8")).hexdigest()
    assert c.content_hash == analyzed_text_hash(DEFAULT_TEXT)
    # The stored source-bytes hash must never stand in for the analysed text.
    assert c.content_hash != SOURCE_BYTES_HASH


def test_analyzed_text_hash_changes_with_the_text(engine):
    seed(engine, text_content="one")
    first = fetch_normalization_page(engine).candidates[0].content_hash

    seed(engine, xid=2, did=2, mid=2, text_content="two")
    page = fetch_normalization_page(engine)
    hashes = {c.extraction_id: c.content_hash for c in page.candidates}
    assert hashes[1] != hashes[2]
    assert hashes[1] == first


def test_extractor_name_and_version_propagate(engine):
    seed(engine, extractor="ocr", extractor_version="v9")
    c = fetch_normalization_page(engine).candidates[0]

    parts = dict(c.extraction_identity.parts)
    assert parts["extractor"] == "ocr"
    assert parts["extractor_version"] == "v9"
