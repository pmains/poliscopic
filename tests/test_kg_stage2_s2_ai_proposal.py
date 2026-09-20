#!/usr/bin/env python3
"""Stage 2 S2 — a model-inferred proposal (`derived`) must never pass as
source-supported, and its assertion class comes from the registry."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_ai_proposal as ai  # noqa: E402

_DOCUMENTS = (
    {"document_id": 11, "document_fingerprint": "f" * 64},
    {"document_id": 12, "document_fingerprint": "e" * 64},
)

_GOOD = {
    "document_id": 11,
    "unlinked_state_fingerprint": "u" * 64,
    "decision_unit_id": "u1",
    "provider": "deepseek",
    "prompt_version": "kg-stage2-s2-ai-adjudication/2.0",
    "model_recommendation": "link",
    "assertion_class": "derived",
    "model": "deepseek-v4-pro",
    "model_version": "2026-09-01",
    "input_fingerprints": {"document": "f" * 64, "agenda_item": "a" * 64},
    "confidence": 0.82,
    "rationale": "the item number appears verbatim in the document title",
    "evidence_spans": [{"text": "Item 4 — CUP", "start": 0, "end": 12}],
    "candidate_links": [{"agenda_item_db_id": 900, "confidence": 0.82}],
}


def _packet(**overrides):
    proposal = dict(_GOOD, **overrides)
    return ai.build_packet(_DOCUMENTS, [proposal], packet_id="p1", created_at="t",
                           plan_digest="p" * 64, packet_digest="q" * 64)


def test_a_clean_packet_validates():
    assert ai.validate_packet(_packet()) == []


def test_the_proposal_class_is_the_canonical_registered_slug():
    """One source of truth: the registry, not a producer-local list."""
    from scripts.kg.registries.evidence import ASSERTION_CLASSES

    assert ai.PROPOSAL_ASSERTION_CLASS == "derived"
    assert ai.ASSERTION_CLASSES is ASSERTION_CLASSES
    assert ai.PROPOSAL_ASSERTION_CLASS in ASSERTION_CLASSES


def test_an_unregistered_assertion_class_fails_closed():
    problems = ai.validate_packet(_packet(assertion_class="inferred"))
    assert any("registry does not define" in p for p in problems)


def test_a_registered_but_wrong_class_is_refused():
    for other in ("source_supported", "human_validated", "quarantined"):
        problems = ai.validate_packet(_packet(assertion_class=other))
        assert any("may only be 'derived'" in p for p in problems), other


def test_a_source_supported_class_can_never_be_claimed():
    problems = ai.validate_packet(_packet(assertion_class="source_supported"))
    assert problems


def test_the_default_policy_cannot_promote_or_overwrite():
    policy = ai.default_review_policy()
    assert policy["promotes_to_canonical"] is False
    assert policy["may_overwrite_source_supported_link"] is False
    assert policy["requires_reviewer_decision"] is True


def test_a_policy_that_could_promote_is_refused():
    packet = _packet()
    packet["review_policy"] = dict(packet["review_policy"], promotes_to_canonical=True)
    assert ai.validate_packet(packet)
    packet["review_policy"] = dict(packet["review_policy"],
                                   promotes_to_canonical=False,
                                   may_overwrite_source_supported_link=True)
    assert ai.validate_packet(packet)
    packet["review_policy"] = dict(packet["review_policy"],
                                   may_overwrite_source_supported_link=False,
                                   requires_reviewer_decision=False)
    assert ai.validate_packet(packet)


def test_a_proposal_cannot_target_a_source_supported_document():
    problems = ai.validate_packet(_packet(), source_supported={11: 900})
    assert any("source-supported link" in p for p in problems)


def test_a_proposal_must_record_model_version_and_inputs():
    assert any("model and version" in p for p in ai.validate_packet(_packet(model=None)))
    assert any("model and version" in p
               for p in ai.validate_packet(_packet(model_version=None)))
    assert any("input fingerprints" in p
               for p in ai.validate_packet(_packet(input_fingerprints={})))


def test_a_proposal_must_carry_confidence_and_rationale():
    assert any("confidence" in p for p in ai.validate_packet(_packet(confidence=None)))
    assert any("confidence" in p for p in ai.validate_packet(_packet(confidence=1.4)))
    assert any("rationale" in p for p in ai.validate_packet(_packet(rationale="")))


def test_evidence_spans_must_be_well_formed():
    assert any("no text" in p
               for p in ai.validate_packet(_packet(evidence_spans=[{"start": 0, "end": 3}])))
    assert any("ends before it starts" in p for p in ai.validate_packet(
        _packet(evidence_spans=[{"text": "x", "start": 9, "end": 1}])))


def test_a_proposal_must_propose_a_candidate_link():
    assert any("candidate link" in p for p in ai.validate_packet(_packet(candidate_links=[])))


def test_proposals_never_arrive_promoted_or_decided():
    packet = _packet()
    packet["proposals"][0]["promoted"] = True
    assert any("already promoted" in p for p in ai.validate_packet(packet))
    packet = _packet()
    packet["proposals"][0]["decision"] = "approve"
    assert any("arrives with a decision" in p for p in ai.validate_packet(packet))


def test_the_packet_offers_all_four_choices_and_decides_none():
    packet = _packet()
    assert packet["proposals"][0]["decision"] is None
    assert packet["proposals"][0]["allowed_choices"] == ["approve", "reject",
                                                         "alternate", "meeting_only"]
    assert packet["decisions"] == []
    assert packet["counts"] == {"candidates": 2, "proposals": 1, "undecided": 1}


def test_a_proposal_for_an_unknown_document_is_dropped():
    packet = ai.build_packet(_DOCUMENTS, [dict(_GOOD, document_id=999)], packet_id="p",
                             created_at="t", plan_digest="p" * 64, packet_digest="q" * 64)
    assert packet["proposals"] == []
    assert packet["counts"]["proposals"] == 0


# ── obsolescence markers ───────────────────────────────────────────────


def test_marking_an_artifact_obsolete_leaves_it_intact(tmp_path):
    path = tmp_path / "worksheet.md"
    digest = artifacts.write_immutable(path, {"markdown": "decision sheet"})
    before = path.read_bytes()
    marker, marker_digest = artifacts.record_obsolete(
        tmp_path, path, "superseded by the corrected attachment model")
    assert path.read_bytes() == before            # the artifact is not deleted
    record = json.loads(marker.read_text())
    assert record["usable_for_decision"] is False
    assert record["target"] == "worksheet.md"
    assert record["target_digest"] == digest
    assert record["reason"].startswith("superseded")
    assert artifacts.recorded_digest(record) == marker_digest


def test_an_unmarked_artifact_is_not_obsolete(tmp_path):
    path = tmp_path / "worksheet.md"
    artifacts.write_immutable(path, {"markdown": "decision sheet"})
    assert artifacts.is_obsolete(path) is None


def test_the_marker_is_write_once(tmp_path):
    path = tmp_path / "worksheet.md"
    artifacts.write_immutable(path, {"markdown": "x"})
    artifacts.record_obsolete(tmp_path, path, "first")
    with pytest.raises(artifacts.ArtifactCollision):
        artifacts.record_obsolete(tmp_path, path, "second")
    assert artifacts.is_obsolete(path)["reason"] == "first"


# ── one canonical proposal per document (contract 2.0) ──────────────────


def _doc(document_id, fingerprint="f" * 64):
    return {"document_id": document_id, "fingerprint": fingerprint}


def _result(recommendation="abstain", candidate=None, confidence=0.8, rationale="because"):
    return {"model_recommendation": recommendation, "agenda_item_db_id": candidate,
            "confidence": confidence, "rationale": rationale}


_CANDIDATES = (
    {"agenda_item_db_id": 900, "agenda_item_id": "b-1_4.I", "agenda_item_number": "4.I"},
    {"agenda_item_db_id": 901, "agenda_item_id": "b-1_4.II", "agenda_item_number": "4.II"},
)


def _expand(result, documents, **kw):
    return ai.expand_group(
        result, documents, unit_id="u1", provider="deepseek", model="deepseek-v4-flash",
        model_version="deepseek-v4-flash", prompt_version="p/1",
        input_fingerprints={"packet": "a" * 64}, candidates=_CANDIDATES, **kw)


def test_a_grouped_result_expands_to_one_proposal_per_document():
    props = _expand(_result(), [_doc(1), _doc(2), _doc(3)])
    assert [p["document_id"] for p in props] == [1, 2, 3]
    assert all(p["decision_unit_id"] == "u1" for p in props)
    assert all(p["version"] == ai.PROPOSAL_VERSION for p in props)


def test_every_expanded_proposal_is_one_document_with_its_own_identity():
    props = _expand(_result(), [_doc(1, "a" * 64), _doc(2, "b" * 64)])
    assert all(isinstance(p["document_id"], int) for p in props)
    assert all("document_ids" not in p for p in props)
    assert [p["document_fingerprint"] for p in props] == ["a" * 64, "b" * 64]
    assert len({p["unlinked_state_fingerprint"] for p in props}) == 2


def test_expansion_fails_closed_on_omission():
    """A group that cannot cover every bound document is refused outright."""
    with pytest.raises(ai.ProposalRefused):
        _expand(_result(), [])


def test_expansion_fails_closed_on_duplicate_documents():
    with pytest.raises(ai.ProposalRefused) as exc:
        _expand(_result(), [_doc(1), _doc(1)])
    assert "more than once" in str(exc.value)


def test_a_cofiled_group_shares_one_decision_unit_id():
    props = _expand(_result(), [_doc(7), _doc(8), _doc(9)])
    assert {p["decision_unit_id"] for p in props} == {"u1"}
    assert {p["document_id"] for p in props} == {7, 8, 9}


def test_a_link_recommendation_carries_candidate_identity_and_fingerprint():
    props = _expand(_result("link", 900), [_doc(1)])
    link = props[0]["candidate_links"][0]
    assert link["agenda_item_db_id"] == 900
    assert link["agenda_item_id"] == "b-1_4.I"
    assert link["agenda_item_fingerprint"] == ai.candidate_fingerprint(_CANDIDATES[0])


def test_a_link_recommendation_outside_the_candidate_set_is_refused():
    with pytest.raises(ai.ProposalRefused):
        _expand(_result("link", 999), [_doc(1)])


def test_a_link_without_a_candidate_is_refused():
    with pytest.raises(ai.ProposalRefused):
        _expand(_result("link", None), [_doc(1)])


def test_an_unknown_recommendation_is_refused():
    with pytest.raises(ai.ProposalRefused):
        _expand(_result("probably"), [_doc(1)])


def test_a_non_link_recommendation_carries_no_candidate():
    for rec in ("abstain", "meeting_only"):
        props = _expand(_result(rec), [_doc(1)])
        assert props[0]["candidate_links"] == []


# ── model recommendation is not a human decision ────────────────────────


def test_every_expanded_proposal_arrives_undecided_and_unpromoted():
    for rec, cand in (("abstain", None), ("link", 900), ("meeting_only", None)):
        prop = _expand(_result(rec, cand), [_doc(1)])[0]
        assert prop["decision"] is None
        assert prop["decided_by"] is None
        assert prop["decided_at"] is None
        assert prop["promoted"] is False
        assert prop["model_recommendation"] == rec


def test_a_model_recommendation_is_not_a_decision():
    """The v2 defect: a model choice sitting in the human decision field."""
    prop = _expand(_result("link", 900), [_doc(1)])[0]
    prop["decision"] = "alternate"                 # the old, wrong shape
    problems = ai.validate_proposal(prop)
    assert any("decision must be null" in p for p in problems)


def test_a_promoted_proposal_is_refused():
    prop = _expand(_result(), [_doc(1)])[0]
    prop["promoted"] = True
    assert any("must not arrive promoted" in p for p in ai.validate_proposal(prop))


def test_a_decided_by_without_a_decision_is_refused():
    prop = _expand(_result(), [_doc(1)])[0]
    prop["decided_by"] = "pete"
    assert any("decided_by must be null" in p for p in ai.validate_proposal(prop))


def test_an_abstention_without_confidence_is_allowed():
    prop = _expand(_result("abstain", None, confidence=None), [_doc(1)])[0]
    assert ai.validate_proposal(prop) == []


# ── drift and conflict ─────────────────────────────────────────────────


def test_document_fingerprint_drift_is_detected():
    prop = _expand(_result(), [_doc(1, "a" * 64)])[0]
    assert ai.validate_proposal(prop, current_document_fingerprint="a" * 64) == []
    problems = ai.validate_proposal(prop, current_document_fingerprint="b" * 64)
    assert any("document fingerprint has drifted" in p for p in problems)


def test_candidate_fingerprint_drift_is_detected():
    prop = _expand(_result("link", 900), [_doc(1)])[0]
    good = ai.candidate_fingerprint(_CANDIDATES[0])
    assert ai.validate_proposal(prop, current_candidate_fingerprint=good) == []
    problems = ai.validate_proposal(prop, current_candidate_fingerprint="0" * 64)
    assert any("candidate fingerprint has drifted" in p for p in problems)


def test_a_source_supported_conflict_is_refused():
    prop = _expand(_result("link", 900), [_doc(1)])[0]
    problems = ai.validate_proposal(prop, source_supported=True)
    assert any("source-supported link" in p for p in problems)


def test_a_link_outside_the_candidate_set_is_refused_by_validation():
    prop = _expand(_result("link", 900), [_doc(1)])[0]
    assert any("not in this meeting's candidate set" in p
               for p in ai.validate_proposal(prop, candidate_ids=[901]))


# ── the one-document rule the old shape violated ────────────────────────


def test_a_document_ids_array_is_refused():
    prop = _expand(_result(), [_doc(1)])[0]
    prop["document_ids"] = [1, 2]
    assert any("document_ids array" in p for p in ai.validate_proposal(prop))


def test_a_null_document_id_is_refused():
    prop = _expand(_result(), [_doc(1)])[0]
    prop["document_id"] = None
    assert any("single integer" in p for p in ai.validate_proposal(prop))


def test_a_missing_document_fingerprint_is_refused():
    prop = _expand(_result(), [_doc(1)])[0]
    prop["document_fingerprint"] = None
    assert any("no document fingerprint" in p for p in ai.validate_proposal(prop))


def test_a_missing_unlinked_state_fingerprint_is_refused():
    prop = _expand(_result(), [_doc(1)])[0]
    prop["unlinked_state_fingerprint"] = None
    assert any("unlinked-state fingerprint" in p for p in ai.validate_proposal(prop))


def test_a_missing_decision_unit_id_is_refused():
    prop = _expand(_result(), [_doc(1)])[0]
    prop["decision_unit_id"] = None
    assert any("decision_unit_id" in p for p in ai.validate_proposal(prop))


# ── immutable supersession ─────────────────────────────────────────────


def test_supersession_is_recorded_by_marker_not_by_overwrite(tmp_path):
    old = tmp_path / "proposal-group.json"
    artifacts.write_immutable(old, {"kind": "group", "document_ids": [1, 2]})
    before = old.read_bytes()
    marker, _ = artifacts.record_obsolete(tmp_path, old, "group-level shape is invalid")
    assert old.read_bytes() == before                      # never overwritten
    record = json.loads(marker.read_text())
    assert record["usable_for_decision"] is False
    assert record["target"] == "proposal-group.json"
    assert artifacts.is_obsolete(old) is not None


def test_the_replacement_proposals_live_beside_the_superseded_one(tmp_path):
    old = tmp_path / "proposal-group.json"
    artifacts.write_immutable(old, {"kind": "group"})
    artifacts.record_obsolete(tmp_path, old, "group-level shape is invalid")
    for document_id in (1, 2):
        replacement = tmp_path / f"proposal-group-doc{document_id}.json"
        artifacts.write_immutable(replacement, {"kind": "proposal", "document_id": document_id})
        assert not (tmp_path / f"{replacement.name}.obsolete.json").exists()
    assert (tmp_path / "proposal-group.json").exists()


# ── P1: the unlinked-state fingerprint must be VALIDATED, not just present ──


def _current(prop, fingerprint="f" * 64, candidates=None, supported=False, cand_fp=None):
    """The full current-state context an adjudication entry point must supply."""
    return {
        "current_document_fingerprint": fingerprint,
        "current_unlinked_state_fingerprint": ai.unlinked_state_fingerprint(
            prop["document_id"], fingerprint),
        "candidate_ids": candidates if candidates is not None else [900, 901],
        "source_supported": supported,
        "current_candidate_fingerprint": cand_fp,
    }


def test_a_nonempty_but_random_unlinked_fingerprint_is_refused():
    """The old defect: presence was checked, correctness was not."""
    prop = _expand(_result(), [_doc(1)])[0]
    prop["unlinked_state_fingerprint"] = "0" * 64
    problems = ai.validate_current(prop, **_current(prop))
    assert any("not the current unlinked state" in p for p in problems)


def test_a_stale_unlinked_fingerprint_from_a_different_state_is_refused():
    prop = _expand(_result(), [_doc(1, "a" * 64)])[0]
    stale = ai.unlinked_state_fingerprint(1, "e" * 64)      # some earlier state
    prop["unlinked_state_fingerprint"] = stale
    problems = ai.validate_current(prop, **_current(prop, fingerprint="a" * 64))
    assert any("not the current unlinked state" in p for p in problems)


def test_the_exact_current_unlinked_fingerprint_is_accepted():
    prop = _expand(_result(), [_doc(1)])[0]
    assert ai.validate_current(prop, **_current(prop)) == []


def test_validate_current_refuses_when_no_current_state_is_supplied():
    prop = _expand(_result(), [_doc(1)])[0]
    problems = ai.validate_current(prop)
    assert len(problems) == len(ai.REQUIRED_CURRENT_STATE)
    assert all("was not supplied" in p for p in problems)


def test_validate_current_refuses_when_the_unlinked_input_is_omitted():
    prop = _expand(_result(), [_doc(1)])[0]
    context = _current(prop)
    context.pop("current_unlinked_state_fingerprint")
    problems = ai.validate_current(prop, **context)
    assert any("current_unlinked_state_fingerprint" in p for p in problems)


def test_validate_current_refuses_when_the_document_fingerprint_is_omitted():
    prop = _expand(_result(), [_doc(1)])[0]
    context = _current(prop)
    context.pop("current_document_fingerprint")
    assert any("current_document_fingerprint" in p for p in ai.validate_current(prop, **context))


def test_validate_current_refuses_when_candidate_membership_is_omitted():
    prop = _expand(_result(), [_doc(1)])[0]
    context = _current(prop)
    context.pop("candidate_ids")
    assert any("candidate_ids" in p for p in ai.validate_current(prop, **context))


def test_validate_current_refuses_when_source_status_is_omitted():
    prop = _expand(_result(), [_doc(1)])[0]
    context = _current(prop)
    context.pop("source_supported")
    assert any("source_supported" in p for p in ai.validate_current(prop, **context))


def test_a_link_needs_the_current_candidate_fingerprint():
    prop = _expand(_result("link", 900), [_doc(1)])[0]
    context = _current(prop)
    context.pop("current_candidate_fingerprint")
    assert any("current_candidate_fingerprint" in p
               for p in ai.validate_current(prop, **context))
    good = ai.candidate_fingerprint(_CANDIDATES[0])
    assert ai.validate_current(prop, **_current(prop, cand_fp=good)) == []


def test_validate_current_refuses_a_stale_candidate_fingerprint():
    prop = _expand(_result("link", 900), [_doc(1)])[0]
    problems = ai.validate_current(prop, **_current(prop, cand_fp="9" * 64))
    assert any("candidate fingerprint has drifted" in p for p in problems)


def test_validate_current_refuses_a_document_that_became_source_supported():
    prop = _expand(_result("link", 900), [_doc(1)])[0]
    good = ai.candidate_fingerprint(_CANDIDATES[0])
    problems = ai.validate_current(prop, **_current(prop, supported=True, cand_fp=good))
    assert any("source-supported link" in p for p in problems)


# ── the review loader fails closed ──────────────────────────────────────


def test_the_loader_returns_a_current_proposal(tmp_path):
    prop = _expand(_result("link", 900), [_doc(1)])[0]
    path = tmp_path / "proposal.json"
    artifacts.write_immutable(path, prop)
    good = ai.candidate_fingerprint(_CANDIDATES[0])
    loaded = ai.load_proposal_for_adjudication(path, **_current(prop, cand_fp=good))
    assert loaded["document_id"] == prop["document_id"]


def test_the_loader_refuses_when_current_state_is_missing(tmp_path):
    prop = _expand(_result(), [_doc(1)])[0]
    path = tmp_path / "proposal.json"
    artifacts.write_immutable(path, prop)
    with pytest.raises(ai.ProposalRefused) as exc:
        ai.load_proposal_for_adjudication(path)
    assert "was not supplied" in str(exc.value)


def test_the_loader_refuses_a_random_unlinked_fingerprint(tmp_path):
    prop = _expand(_result(), [_doc(1)])[0]
    prop["unlinked_state_fingerprint"] = "0" * 64
    path = tmp_path / "proposal.json"
    artifacts.write_immutable(path, prop)
    with pytest.raises(ai.ProposalRefused) as exc:
        ai.load_proposal_for_adjudication(path, **_current(prop))
    assert "unlinked" in str(exc.value)


def test_the_loader_refuses_on_document_drift(tmp_path):
    prop = _expand(_result(), [_doc(1, "a" * 64)])[0]
    path = tmp_path / "proposal.json"
    artifacts.write_immutable(path, prop)
    with pytest.raises(ai.ProposalRefused):
        ai.load_proposal_for_adjudication(path, **_current(prop, fingerprint="b" * 64))


def test_the_loader_refuses_a_tampered_artifact(tmp_path):
    prop = _expand(_result(), [_doc(1)])[0]
    path = tmp_path / "proposal.json"
    artifacts.write_immutable(path, prop)
    document = json.loads(path.read_text())
    document["confidence"] = 0.99
    path.write_text(json.dumps(document))
    with pytest.raises(artifacts.ArtifactDigestMismatch):
        ai.load_proposal_for_adjudication(path, **_current(prop))


def test_the_packet_api_is_reachable_from_the_one_authority():
    """stage2_s2_ai_proposal re-exports the packet surface; one import for callers."""
    for name in ("build_packet", "validate_packet", "default_review_policy",
                 "PROPOSAL_ASSERTION_CLASS", "CHOICES", "validate_current",
                 "load_proposal_for_adjudication"):
        assert hasattr(ai, name), name
