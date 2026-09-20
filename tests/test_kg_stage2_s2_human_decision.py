#!/usr/bin/env python3
"""Stage 2 human-decision artifacts: identity, evidence, refusal, rebind.

Pure tests — no database, no engine. The live decisions are revalidated
separately by the operator path documented in STAGE-2.md.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO, _REPO / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import stage2_s2_human_decision as H  # noqa: E402

_DECIDED_AT = "2026-09-12T20:03:00-07:00"


def _loaded(document_id=107938, candidate=278759, number="4.I", meeting=10428):
    """A loader-shaped payload, as load_for_adjudication would return it."""
    return {
        "proposal": {
            "decision_unit_id": "u10428-01-real_number",
            "model_recommendation": "link",
            "confidence": 0.78,
            "candidate_links": [{
                "agenda_item_db_id": candidate,
                "agenda_item_id": f"buckeye-cfd-1071_{number}",
                "agenda_item_number": number,
                "agenda_item_fingerprint": "c" * 64,
            }],
        },
        "document": {
            "document_id": document_id,
            "meeting_db_id": meeting,
            "document_fingerprint": "d" * 64,
            "unlinked_state_fingerprint": "u" * 64,
            "source_supported": False,
        },
        "target": {"agenda_item_db_id": candidate, "meeting_db_id": meeting,
                   "agenda_item_fingerprint": "c" * 64},
        "lineage": {
            "plan": {"path": "kg-stage2-s2-plan-A.json", "digest": "p" * 64},
            "aggregate": {"path": "kg-stage2-s2-ai-proposals-A.json", "digest": "a" * 64},
            "proposal": {"path": "proposal-doc107938.json", "digest": "r" * 64},
        },
        "target_identity": {"database": "poliscopic_dev"},
        "snapshot": {"isolation": "REPEATABLE READ + READ ONLY"},
    }


def _decision(**overrides):
    kwargs = dict(loaded=_loaded(), role="item report",
                  description="the item report for the approved agenda item",
                  human_stated_item="4.I", adjudicator="Peter Mains",
                  decision_id=H.decision_id_for(_DECIDED_AT, 107938),
                  decided_at=_DECIDED_AT)
    kwargs.update(overrides)
    return H.build_decision(**kwargs)


# ── decision identity ──────────────────────────────────────────────────


def test_a_decision_id_is_derived_from_the_timestamp_and_document():
    assert H.decision_id_for(_DECIDED_AT, 107938) == "kg-s2-dec-20260913T030300Z-doc107938"


def test_a_decision_id_accepts_a_datetime_too():
    moment = datetime(2026, 9, 12, 20, 3, tzinfo=timezone.utc)
    assert H.decision_id_for(moment, 107915) == H.decision_id_for(
        moment.isoformat(), 107915)


def test_a_naive_string_is_taken_as_utc():
    assert H.decision_id_for("2026-09-13T03:03:00", 1).startswith("kg-s2-dec-20260913T030300Z")


def test_a_bad_timestamp_is_refused():
    with pytest.raises(H.DecisionRefused):
        H.decision_id_for("not-a-time", 1)


def test_the_identity_key_uses_the_canonical_adjudication_identity():
    record = _decision()
    assert record["identity_key"]
    assert record["adjudicator"] == "Peter Mains"


# ── evidence: the human's own words survive ────────────────────────────


def test_the_source_document_role_and_description_are_preserved_verbatim():
    record = _decision(role="Public Hearing notice",
                       description="the notice of public hearing")
    assert record["document_role"] == "Public Hearing notice"
    assert record["document_description"] == "the notice of public hearing"


def test_the_document_and_candidate_evidence_is_copied_from_the_loader():
    record = _decision()
    assert record["document_fingerprint"] == "d" * 64
    assert record["unlinked_state_fingerprint"] == "u" * 64
    assert record["candidate"]["agenda_item_db_id"] == 278759
    assert record["candidate"]["agenda_item_fingerprint"] == "c" * 64
    assert record["proposal"]["digest"] == "r" * 64
    assert record["lineage"]["plan"]["digest"] == "p" * 64


def test_an_item_number_mismatch_is_recorded_not_reconciled():
    """The human said 2.C; the candidate is stored as 2026. Both survive."""
    record = _decision(loaded=_loaded(document_id=107915, candidate=278749,
                                      number="2026", meeting=10428),
                       human_stated_item="2.C", role="Item Report",
                       decision_id=H.decision_id_for(_DECIDED_AT, 107915))
    assert record["candidate"]["agenda_item_number"] == "2026"
    assert record["human_stated_item"] == "2.C"
    assert record["item_number_mismatch"] is True
    assert "2.C" in record["item_number_note"] and "2026" in record["item_number_note"]


def test_an_agreeing_item_number_is_not_flagged():
    record = _decision()
    assert record["item_number_mismatch"] is False
    assert record["item_number_note"] == ""


# ── refusal ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["adjudicator", "decision_id", "decided_at",
                                   "role", "description", "human_stated_item"])
def test_a_decision_missing_a_required_part_is_refused(field):
    kwargs = {"role": "r", "description": "d", "human_stated_item": "4.I",
              "adjudicator": "Peter Mains", "decision_id": "x", "decided_at": "t"}
    kwargs[field] = ""
    with pytest.raises(H.DecisionRefused):
        H.build_decision(loaded=_loaded(), **kwargs)


def test_an_unknown_decision_kind_is_refused():
    with pytest.raises(H.DecisionRefused):
        _decision(decision="maybe")


def test_approving_without_a_bound_candidate_is_refused():
    loaded = _loaded()
    loaded["target"] = None
    with pytest.raises(H.DecisionRefused):
        H.build_decision(loaded=loaded, role="r", description="d",
                         human_stated_item="4.I", adjudicator="Peter Mains",
                         decision_id="x", decided_at="t")


# ── current-state validation ───────────────────────────────────────────


def _validate(record, **current):
    base = dict(current_document_fingerprint="d" * 64,
                current_unlinked_state_fingerprint="u" * 64,
                current_candidate_fingerprint="c" * 64,
                current_plan_digest="p" * 64,
                current_aggregate_digest="a" * 64,
                source_supported=False)
    base.update(current)
    return H.validate_decision(record, **base)


def test_a_matching_decision_validates():
    assert _validate(_decision()) == []


def test_document_drift_is_refused():
    problems = _validate(_decision(), current_document_fingerprint="x" * 64)
    assert any("document fingerprint has drifted" in p for p in problems)


def test_unlinked_state_drift_is_refused():
    problems = _validate(_decision(), current_unlinked_state_fingerprint="x" * 64)
    assert any("unlinked-state fingerprint has drifted" in p for p in problems)


def test_candidate_drift_is_refused():
    problems = _validate(_decision(), current_candidate_fingerprint="x" * 64)
    assert any("candidate fingerprint has drifted" in p for p in problems)


def test_plan_drift_is_refused():
    problems = _validate(_decision(), current_plan_digest="x" * 64)
    assert any("current plan digest" in p for p in problems)


def test_a_document_that_became_source_supported_is_refused():
    problems = _validate(_decision(), source_supported=True)
    assert any("source-supported" in p for p in problems)


def test_a_decision_may_not_be_promoted_or_applied():
    record = _decision()
    record["promoted"] = True
    assert any("promoted=false" in p for p in _validate(record))
    record = _decision()
    record["applied"] = True
    assert any("applied=false" in p for p in _validate(record))


def test_the_recorded_aggregate_may_be_an_ancestor_of_current():
    """The aggregate is a derived index; the decision binds the plan."""
    record = _decision()
    # recorded aggregate a* is current -> fine
    assert _validate(record, current_aggregate_digest="a" * 64) == []
    # recorded aggregate a* is an ancestor of the current one -> fine
    assert _validate(record, current_aggregate_digest="b" * 64,
                     aggregate_chain=["b" * 64, "a" * 64]) == []


def test_an_unrelated_aggregate_is_refused():
    problems = _validate(_decision(), current_aggregate_digest="b" * 64,
                         aggregate_chain=["b" * 64])
    assert any("neither current nor an ancestor" in p for p in problems)


# ── rebinding the aggregate index ──────────────────────────────────────


def _aggregate(documents=(107938, 107939, 107915, 107916, 107917)):
    return {
        "kind": "kg-stage2-s2-ai-proposals-aggregate",
        "counts": {"proposals": len(documents), "decided": 0, "promoted": 0},
        "manifest": {"plan_digest": "p" * 64},
        "per_unit": {"u1": [{"document_id": d, "path": f"proposal-{d}.json",
                             "digest": "x" * 64} for d in documents]},
    }


def _five():
    pairs = [(107938, 278759, "4.I", "item report"),
             (107939, 278759, "4.I", "budget"),
             (107915, 278749, "2.C", "Item Report"),
             (107916, 278749, "2.C", "Public Hearing notice"),
             (107917, 278749, "2.C", "resolution")]
    out = []
    for document_id, candidate, item, role in pairs:
        number = item if candidate == 278759 else "2026"
        record = H.build_decision(
            loaded=_loaded(document_id=document_id, candidate=candidate, number=number),
            role=role, description=f"the {role}", human_stated_item=item,
            adjudicator="Peter Mains",
            decision_id=H.decision_id_for(_DECIDED_AT, document_id),
            decided_at=_DECIDED_AT)
        record["digest"] = "e" * 64
        record["path"] = f"kg-stage2-s2-decision-{record['decision_id']}.json"
        out.append(record)
    return out


def test_rebinding_counts_five_approved_and_zero_promoted():
    rebound = H.bind_aggregate(_aggregate(), _five(), created_at=_DECIDED_AT)
    assert rebound["counts"]["approved"] == 5
    assert rebound["counts"]["decided"] == 5
    assert rebound["counts"]["promoted"] == 0
    assert rebound["counts"]["applied"] == 0
    assert sorted(rebound["decisions"]) == ["107915", "107916", "107917",
                                            "107938", "107939"]


def test_rebinding_does_not_move_the_proposals():
    before = _aggregate()
    rebound = H.bind_aggregate(before, _five(), created_at=_DECIDED_AT)
    assert rebound["per_unit"] == before["per_unit"]
    assert rebound["manifest"] == before["manifest"]


def test_rebinding_records_the_superseded_aggregate():
    rebound = H.bind_aggregate(_aggregate(), _five(), created_at=_DECIDED_AT,
                               supersedes={"path": "old.json", "digest": "o" * 64},
                               supersedes_reason="rebound to index decisions")
    assert rebound["supersedes"] == {"path": "old.json", "digest": "o" * 64}
    assert "rebound" in rebound["supersedes_reason"]


def test_a_decision_for_an_unbound_document_is_refused():
    stray = _five()[0]
    stray = dict(stray, document_id=999)
    with pytest.raises(H.DecisionRefused):
        H.bind_aggregate(_aggregate(), [stray], created_at=_DECIDED_AT)


def test_two_decisions_for_one_document_are_refused():
    five = _five()
    with pytest.raises(H.DecisionRefused):
        H.bind_aggregate(_aggregate(), five + [five[0]], created_at=_DECIDED_AT)


def test_the_role_travels_into_the_index():
    rebound = H.bind_aggregate(_aggregate(), _five(), created_at=_DECIDED_AT)
    assert rebound["decisions"]["107916"]["document_role"] == "Public Hearing notice"
    assert rebound["decisions"]["107915"]["document_role"] == "Item Report"
    assert rebound["decisions"]["107939"]["document_role"] == "budget"


# ── the recorded decision request is the human's own words ─────────────


def test_the_recorded_request_names_five_documents_and_peter_mains():
    path = _REPO / "data" / "kg-plans" / \
        "kg-stage2-s2-decision-request-20260913T031000Z.json"
    request = json.loads(path.read_text())
    assert request["adjudicator"] == "Peter Mains"
    assert len(request["decisions"]) == 5
    by_doc = {d["document_id"]: d for d in request["decisions"]}
    assert by_doc[107938]["role"] == "item report"
    assert by_doc[107939]["role"] == "budget"
    assert by_doc[107915]["role"] == "Item Report"
    assert by_doc[107916]["role"] == "Public Hearing notice"
    assert by_doc[107917]["role"] == "resolution"
    assert {by_doc[d]["candidate"] for d in (107938, 107939)} == {278759}
    assert {by_doc[d]["candidate"] for d in (107915, 107916, 107917)} == {278749}
