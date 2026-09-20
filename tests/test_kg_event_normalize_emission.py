"""Emission-bundle tests for event normalization candidates.

Entirely pure: bundles are projections of a candidate, so they are tested without
a database, a transaction, or the pipeline.
"""

from __future__ import annotations

import dataclasses
import pathlib

from scripts.entities.event_normalize_emission import (
    ASSERTION_CLASS, BUNDLE_KIND, CONTEXT_CLASS,
    build_event_bundle, event_bundle_problems, is_complete_event_bundle,
)
from scripts.entities.event_normalize_models import NormalizationCandidate
from scripts.kg.registries import MODEL_VERSION

ENTITIES_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "entities"


def candidate(**overrides):
    kwargs = dict(
        extraction_id=1, supporting_document_id=10, meeting_db_id=500,
        meeting_source_id="2024-03-05-CC", public_body_id="B-1",
        jurisdiction_id="J-1", action_verb="approved", content_hash="hash-a",
        extraction_method="pdftotext", confidence=0.9,
    )
    kwargs.update(overrides)
    return NormalizationCandidate.create(**kwargs)


def test_bundle_is_complete_for_a_plain_candidate():
    c = candidate()

    assert is_complete_event_bundle(c) is True
    assert event_bundle_problems(c) == ()

    bundle = build_event_bundle(c)
    assert bundle.kind == BUNDLE_KIND == "event"
    assert bundle.assertion_class == ASSERTION_CLASS == "source_supported"
    assert bundle.model_version == MODEL_VERSION
    assert bundle.event_type == c.event_type == "approval"
    assert bundle.outcome == "approved"
    assert bundle.outcome_qualifier is None
    assert bundle.evidence_class == c.evidence_class
    # The bundle cites the candidate's own typed evidence, not a copy.
    assert bundle.evidence_identity is c.evidence_identity
    assert bundle.context_class == CONTEXT_CLASS
    assert bundle.context_identity is c.meeting_identity


def test_qualified_outcome_is_carried_as_a_base_and_a_qualifier():
    c = candidate(action_verb="denied_without_prejudice")
    bundle = build_event_bundle(c)

    assert bundle.outcome == "denied"
    assert bundle.outcome_qualifier == "without_prejudice"
    # Never recombined into the raw historical single-string form.
    assert bundle.outcome != "denied_without_prejudice"
    assert event_bundle_problems(c) == ()


def test_the_producer_cannot_emit_a_dotted_event_type():
    """A dotted DB slug is a lookup path; the emitter cannot produce one.

    ``event_type`` is never supplied by a caller -- it is derived from the action
    verb through the registry's compatibility contract -- so a dotted slug has no
    route into an emitted bundle.  The emission layer additionally canonicalizes
    any dotted value it is handed, so such a value cannot become a *distinct*
    event type either.
    """
    import inspect

    from scripts.kg.registries import normalize_event_slug

    params = inspect.signature(NormalizationCandidate.create).parameters
    assert "event_type" not in params  # derived, never supplied
    assert "action_verb" in params

    for verb in ("approved", "denied", "continued", "adopted"):
        bundle = build_event_bundle(candidate(action_verb=verb))
        assert "." not in bundle.event_type, verb

    # A dotted value resolves to the same leaf, so it is not a distinct type.
    assert normalize_event_slug("decision.approval") == "approval"
    dotted = dataclasses.replace(candidate(), event_type="decision.approval")
    assert event_bundle_problems(dotted) == ()


def test_event_type_is_the_canonical_leaf_for_every_supported_verb():
    for verb, leaf in (
        ("approved", "approval"),
        ("continued", "continuation"),
        ("adopted", "adoption"),
        ("introduced", "introduction"),
        ("received", "receipt"),
    ):
        c = candidate(action_verb=verb)
        bundle = build_event_bundle(c)
        assert bundle.event_type == leaf, verb
        assert "." not in bundle.event_type, verb
        assert event_bundle_problems(c) == (), verb


def test_evidence_class_is_taken_from_the_registry_inventory():
    text_layer = candidate(extraction_method="pdftotext")
    ocr = candidate(extraction_method="ocr_local")

    assert text_layer.evidence_class == "source_pdf_text"
    assert ocr.evidence_class == "source_ocr"
    assert build_event_bundle(text_layer).evidence_class == "source_pdf_text"
    assert build_event_bundle(ocr).evidence_class == "source_ocr"
    assert event_bundle_problems(text_layer) == ()
    assert event_bundle_problems(ocr) == ()


def test_bundle_kind_matches_the_completeness_contract():
    """The event kind requires an event type and a context identity."""
    from scripts.kg.emission import BUNDLE_REQUIRED_IDENTITIES, required_bundle_categories

    assert required_bundle_categories("event", "source_supported")[0] == "event_type"
    assert "context_identity" in BUNDLE_REQUIRED_IDENTITIES["event"]

    bundle = build_event_bundle(candidate())
    assert bundle.event_type is not None
    assert bundle.context_identity is not None


def test_source_scan_proves_no_local_vocabulary_table():
    """No outcome, evidence-method, or event-type mapping is reproduced here."""
    source = (ENTITIES_DIR / "event_normalize_emission.py").read_text(encoding="utf-8")

    for forbidden in (
        "decision.", "legislation.", "administration.", "procedure.",
        "with_conditions", "without_prejudice", "as_amended",
        "pdftotext", "ocr_local", "ocr_windows",
        "VERB_MAP", "DB_SLUG_ALIASES", "= {", "= dict(",
    ):
        assert forbidden not in source, forbidden


def test_source_scan_proves_values_are_projected_from_the_registries():
    source = (ENTITIES_DIR / "event_normalize_emission.py").read_text(encoding="utf-8")

    assert "from scripts.kg.registries import MODEL_VERSION" in source
    assert "from scripts.kg.emission import EmissionBundle, bundle_problems" in source
    # The bundle is built from the candidate's already-canonical fields.
    for field in (
        "candidate.event_type", "candidate.outcome.base",
        "candidate.outcome.qualifier", "candidate.evidence_class",
        "candidate.evidence_identity", "candidate.meeting_identity",
    ):
        assert field in source, field
