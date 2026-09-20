"""Tests for pure identity keys and assertion classes (Brief 018 Step 3).

Fixtures cover the identity cases Brief 018 requires: shared source
occurrences, multiple evidence for one claim, one source for several claims,
repeated contexts, repeated votes, role/context separation, participation
bases, and unknown-time handling.  Nothing here touches a database.
"""

from __future__ import annotations

import pytest

from scripts.kg import identity as ident
from scripts.kg.identity import (
    IdentityError,
    assertion_problems,
    build_assertion,
    claim_identity,
    co_occurrence_assertion,
    entity_candidate_identity,
    evidence_identity,
    event_identity,
    extraction_identity,
    mention_identity,
    participation_identity,
    serialize_assertion,
    vote_identity,
)

URL = "https://example.test/agendas/2026-01-06.pdf"


def occurrence(**overrides):
    """One source occurrence of one document version."""
    kwargs = dict(
        source_type="supporting_document", source_id=101, url=URL,
        content_hash="hash-v1", extraction_method="source_pdf_text",
        span_start=0, span_end=120,
    )
    kwargs.update(overrides)
    return evidence_identity(**kwargs)


def meeting_context(name: str):
    return entity_candidate_identity(entity_type="meeting", surface_form=name)


def agenda_context(item_id: int):
    return evidence_identity(
        source_type="agenda_item", source_id=item_id, url=f"{URL}#item{item_id}",
        content_hash="hash-v1", extraction_method="structured_record",
    )


def applicant():
    return entity_candidate_identity(entity_type="person", surface_form="Dana Reyes")


def case_entity():
    return entity_candidate_identity(entity_type="case", surface_form="C-1")


# ---------------------------------------------------------------------------
# Keys are pure and deterministic
# ---------------------------------------------------------------------------


def test_identical_inputs_produce_identical_keys():
    assert occurrence().digest == occurrence().digest
    assert occurrence().serialize() == occurrence().serialize()


def test_canonical_form_is_key_order_stable():
    key = occurrence()
    assert key.canonical == ident.IdentityKey(kind=key.kind, parts=tuple(reversed(key.parts))).canonical


def test_digest_is_sha256_of_canonical():
    import hashlib

    key = occurrence()
    assert key.digest == hashlib.sha256(key.canonical.encode()).hexdigest()


def test_missing_discriminator_is_rejected():
    with pytest.raises(IdentityError):
        evidence_identity(source_type="supporting_document", source_id=101)


def test_half_span_is_rejected():
    with pytest.raises(IdentityError):
        occurrence(span_start=5, span_end=None)


# ---------------------------------------------------------------------------
# 1. Two extractors, one source occurrence
# ---------------------------------------------------------------------------


def test_two_extractors_share_evidence_but_not_extraction():
    shared = occurrence()
    first = extraction_identity(shared, extractor="pattern_cascade", extractor_version="1")
    second = extraction_identity(shared, extractor="event_extract", extractor_version="1")
    assert extraction_identity(
        shared, extractor="pattern_cascade", extractor_version="1"
    ) == first
    assert first.digest != second.digest
    assert first.kind == "extraction"
    assert ("evidence", shared.digest) in first.parts
    assert ("extractor", "pattern_cascade") in first.parts
    # The same finding is reproducible; a different extractor is a new act.
    assert extraction_identity(shared, extractor="pattern_cascade") != first


# ---------------------------------------------------------------------------
# 2. Two evidence records supporting one claim
# ---------------------------------------------------------------------------


def test_two_evidence_records_support_one_claim_and_stay_distinct():
    first = occurrence()
    second = occurrence(span_start=200, span_end=320)
    claim = claim_identity(applicant(), "APPLIED_FOR", case_entity())
    left = build_assertion(
        subject=applicant(), predicate="APPLIED_FOR", object=case_entity(),
        assertion_class="source_supported", evidence=(first,),
        observed_at="2026-01-07T00:00:00-07:00", valid_time="2026-01-06",
    )
    right = build_assertion(
        subject=applicant(), predicate="APPLIED_FOR", object=case_entity(),
        assertion_class="source_supported", evidence=(second,),
        observed_at="2026-01-07T00:00:00-07:00", valid_time="2026-01-06",
    )
    assert first.digest != second.digest
    assert left.predicate == right.predicate == "APPLIED_FOR"
    assert claim.digest == claim_identity(applicant(), "APPLIED_FOR", case_entity()).digest


# ---------------------------------------------------------------------------
# 3. One source supporting several claims
# ---------------------------------------------------------------------------


def test_one_source_supports_several_distinct_claims():
    shared = occurrence()
    applied = claim_identity(applicant(), "APPLIED_FOR", case_entity())
    owns = claim_identity(applicant(), "OWNS", case_entity())
    assert applied.digest != owns.digest
    for claim in (applied, owns):
        assertion = build_assertion(
            subject=applicant(), predicate=claim.parts[1][1], object=case_entity(),
            assertion_class="source_supported", evidence=(shared,),
            observed_at="2026-01-07T00:00:00-07:00", valid_time="2026-01-06",
        )
        assert assertion.evidence[0].digest == shared.digest


# ---------------------------------------------------------------------------
# 4. Repeated appearances of one case across meetings
# ---------------------------------------------------------------------------


def test_one_case_across_meetings_is_one_candidate_with_distinct_contexts():
    assert case_entity().digest == case_entity().digest
    january = meeting_context("2026-01-06 BOS")
    february = meeting_context("2026-02-03 BOS")
    assert january.digest != february.digest
    assert case_entity().parts == case_entity().parts


# ---------------------------------------------------------------------------
# 5. Similar actions at different agenda items
# ---------------------------------------------------------------------------


def test_similar_events_at_different_items_do_not_collapse():
    first = event_identity(
        event_type="decision.approval", context=agenda_context(1), occurrence="1",
    )
    second = event_identity(
        event_type="decision.approval", context=agenda_context(2), occurrence="1",
    )
    assert first.digest != second.digest
    assert first.parts[0] == ("event_type", "approval")  # dotted slug normalized


# ---------------------------------------------------------------------------
# 6. Reconsidered or repeated vote
# ---------------------------------------------------------------------------


def test_reconsidered_vote_is_a_distinct_identity():
    original = vote_identity(
        context=agenda_context(1), actor=applicant(), motion="approve C-1",
        occurrence="1", valid_time="2026-01-06",
    )
    reconsidered = vote_identity(
        context=agenda_context(1), actor=applicant(), motion="approve C-1",
        occurrence="2", valid_time="2026-02-03",
    )
    assert original.digest != reconsidered.digest


def test_vote_requires_an_occurrence():
    with pytest.raises(IdentityError):
        vote_identity(context=agenda_context(1), actor=None, motion="x", occurrence="")


# ---------------------------------------------------------------------------
# 7. One actor, different roles in different contexts
# ---------------------------------------------------------------------------


def test_one_actor_in_different_roles_and_contexts():
    actor = applicant()
    as_applicant = participation_identity(
        actor, agenda_context(1), role="applicant", basis="agenda_listing",
    )
    as_presenter = participation_identity(
        actor, agenda_context(2), role="presenter", basis="scheduled_role",
    )
    ident.assert_distinct([as_applicant, as_presenter], "actor role/context")
    assert as_applicant.parts[0] == as_presenter.parts[0]  # same actor digest


# ---------------------------------------------------------------------------
# 8. Meeting-level co-occurrence is derived
# ---------------------------------------------------------------------------


def test_co_occurrence_is_derived_and_cannot_claim_source_support():
    meeting = meeting_context("2026-01-06 BOS")
    assertion = co_occurrence_assertion(
        applicant(), "PARTICIPATED_IN", meeting,
        context=meeting, inputs=(occurrence(),),
        observed_at="2026-01-07T00:00:00-07:00",
    )
    assert assertion.assertion_class == "derived"
    assert assertion.evidence == ()
    record = serialize_assertion(assertion)
    assert record["source_supported"] is False
    assert record["label"] == "inferred/derived"
    with pytest.raises(IdentityError):
        serialize_assertion(assertion, as_source_supported=True)


def test_derived_assertion_may_not_cite_evidence_as_source():
    problems = assertion_problems(
        ident.Assertion(
            subject=applicant(), predicate="PARTICIPATED_IN", object=meeting_context("m"),
            assertion_class="derived", evidence=(occurrence(),),
            inputs=(occurrence(),), observed_at="2026-01-07",
        )
    )
    assert any("must not cite evidence" in problem for problem in problems)


def test_derived_assertion_must_declare_inputs():
    problems = assertion_problems(
        ident.Assertion(
            subject=applicant(), predicate="PARTICIPATED_IN", object=meeting_context("m"),
            assertion_class="derived", observed_at="2026-01-07",
        )
    )
    assert any("must declare its inputs" in problem for problem in problems)


# ---------------------------------------------------------------------------
# 9-11. Participation bases and forbidden promotions
# ---------------------------------------------------------------------------


def test_four_bases_for_one_actor_and_context_stay_distinct():
    actor, context = applicant(), agenda_context(1)
    keys = [
        participation_identity(actor, context, role="applicant", basis=basis)
        for basis in (
            "agenda_listing", "scheduled_role", "observed_attendance", "observed_action",
        )
    ]
    ident.assert_distinct(keys, "participation basis")


def test_agenda_listing_cannot_promote_to_attendance():
    assertion = ident.Assertion(
        subject=applicant(), predicate="PRESENT_AT", object=meeting_context("2026-01-06"),
        assertion_class="source_supported", evidence=(occurrence(),),
        basis="agenda_listing", promotion="attendance", observed_at="2026-01-07",
        valid_time="2026-01-06",
    )
    assert any("cannot support promotion attendance" in p for p in assertion_problems(assertion))


def test_staff_contact_is_not_attendance_or_presentation():
    problems = assertion_problems(
        ident.Assertion(
            subject=applicant(), predicate="PRESENT_AT", object=meeting_context("m"),
            assertion_class="source_supported", evidence=(occurrence(),),
            basis="scheduled_role", promotion="attendance", observed_at="2026-01-07",
        )
    )
    assert any("cannot support promotion attendance" in problem for problem in problems)


def test_dca_listing_is_not_a_completed_action():
    problems = assertion_problems(
        ident.Assertion(
            subject=applicant(), predicate="DECIDED", object=case_entity(),
            assertion_class="source_supported", evidence=(occurrence(),),
            basis="agenda_listing", promotion="completed_action",
            observed_at="2026-01-07",
        )
    )
    assert any("cannot support promotion completed_action" in p for p in problems)


def test_observed_action_may_support_completed_action():
    assertion = build_assertion(
        subject=applicant(), predicate="DECIDED", object=case_entity(),
        assertion_class="source_supported", evidence=(occurrence(),),
        basis="observed_action", promotion="completed_action",
        observed_at="2026-01-07", valid_time="2026-01-06",
    )
    assert serialize_assertion(assertion)["source_supported"] is True


# ---------------------------------------------------------------------------
# 12. Changed document at the same URL
# ---------------------------------------------------------------------------


def test_changed_document_at_same_url_is_a_new_identity():
    before = occurrence(content_hash="hash-v1")
    after = occurrence(content_hash="hash-v2")
    assert before.digest != after.digest
    assert before.parts[2][1] == after.parts[2][1]  # same url part


# ---------------------------------------------------------------------------
# 13. Structured and OCR observations of one canonical claim
# ---------------------------------------------------------------------------


def test_structured_and_ocr_observations_both_support_one_claim():
    structured = occurrence(
        source_type="agenda_item", source_id=7, extraction_method="structured_record",
        url=URL, content_hash="hash-v1", span_start=0, span_end=40,
    )
    ocr = occurrence(
        source_type="supporting_document", source_id=101,
        extraction_method="source_ocr", content_hash="hash-v1",
        url=URL, span_start=0, span_end=40,
    )
    assert structured.digest != ocr.digest
    claim = claim_identity(applicant(), "APPLIED_FOR", case_entity())
    for evidence in (structured, ocr):
        assertion = build_assertion(
            subject=applicant(), predicate="APPLIED_FOR", object=case_entity(),
            assertion_class="source_supported", evidence=(evidence,),
            observed_at="2026-01-07T00:00:00-07:00", valid_time="2026-01-06",
        )
        assert assertion.cites_source is True
    assert claim.kind == "claim"


# ---------------------------------------------------------------------------
# 14. Unknown valid time versus known observation time
# ---------------------------------------------------------------------------


def test_unknown_valid_time_requires_an_observation_time():
    problems = assertion_problems(
        ident.Assertion(
            subject=applicant(), predicate="APPLIED_FOR", object=case_entity(),
            assertion_class="source_supported", evidence=(occurrence(),),
            valid_time=None, observed_at=None,
        )
    )
    assert any("requires observed_at" in problem for problem in problems)


def test_unknown_valid_time_is_allowed_when_observation_time_is_known():
    assertion = build_assertion(
        subject=applicant(), predicate="APPLIED_FOR", object=case_entity(),
        assertion_class="source_supported", evidence=(occurrence(),),
        valid_time=None, observed_at="2026-01-07T00:00:00-07:00",
    )
    record = serialize_assertion(assertion)
    assert record["valid_time"] is None
    assert record["observed_at"] == "2026-01-07T00:00:00-07:00"
    assert record["source_supported"] is True


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_mention_identity_separates_surface_forms():
    shared = occurrence()
    first = mention_identity(shared, extractor="pattern_cascade", mention_text="Reyes",
                             span_start=0, span_end=5)
    second = mention_identity(shared, extractor="pattern_cascade", mention_text="Dana Reyes",
                              span_start=0, span_end=10)
    assert first.digest != second.digest


def test_unregistered_vocabulary_is_rejected():
    with pytest.raises(IdentityError):
        entity_candidate_identity(entity_type="recommendation", surface_form="x")
    with pytest.raises(IdentityError):
        claim_identity(applicant(), "HAS_APPLICANT", case_entity())
    with pytest.raises(IdentityError):
        participation_identity(applicant(), agenda_context(1), role="known_org",
                               basis="agenda_listing")


def test_source_supported_assertion_must_cite_evidence():
    with pytest.raises(IdentityError):
        build_assertion(
            subject=applicant(), predicate="APPLIED_FOR", object=case_entity(),
            assertion_class="source_supported", valid_time="2026-01-06",
        )


def test_context_distinguishes_otherwise_identical_claims():
    meeting_a, meeting_b = meeting_context("a"), meeting_context("b")
    assert claim_identity(applicant(), "PART_OF", meeting_a).digest != claim_identity(
        applicant(), "PART_OF", meeting_b
    ).digest


def test_serialization_round_trips_all_eight_identity_families():
    keys = [
        occurrence(),
        extraction_identity(occurrence(), extractor="event_extract", extractor_version="1"),
        mention_identity(occurrence(), extractor="event_extract", mention_text="Reyes",
                         span_start=0, span_end=5),
        claim_identity(applicant(), "APPLIED_FOR", case_entity()),
        case_entity(),
        event_identity(event_type="approval", context=agenda_context(1), occurrence="1"),
        participation_identity(applicant(), agenda_context(1), role="applicant",
                               basis="agenda_listing"),
        vote_identity(context=agenda_context(1), actor=applicant(), motion="m",
                      occurrence="1"),
    ]
    kinds = {key.kind for key in keys}
    assert kinds == {
        "evidence", "extraction", "mention", "claim", "entity_candidate",
        "event", "participation", "vote",
    }
    for key in keys:
        payload = key.serialize()
        assert payload["digest"] == key.digest
        assert payload["canonical"] == key.canonical
    ident.assert_distinct(keys, "identity families")
