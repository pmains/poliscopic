#!/usr/bin/env python3
"""Stage 2 repair plan — P1 review corrections, regression-tested."""

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

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_evidence_materialize as E  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as B  # noqa: E402
from scripts.kg import stage2_s2_repair_plan as RP  # noqa: E402

def _live(pattern):
    """The one live artifact matching a glob, resolved rather than named."""
    hits = [p for p in sorted((_REPO / "data" / "kg-plans").glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (p.parent / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


_PLAN = _live("kg-stage2-s2-repair-plan-*.json")



_BAR = ("2.C\nCITY OF BUCKEYE\nBOARD ACTION REPORT\nAGENDA ITEM: 2.C. FIN - Resolution\n"
        "No. 04-26 Tartesso West CFD\nDATE PREPARED: 05/14/26\n")
_ATTACH = "NOTICE OF PUBLIC HEARING\nNOTICE IS HEREBY GIVEN"


def _row(sd, meeting, item, text, title="t"):
    return {"id": sd, "meeting_db_id": meeting, "body": "buckeye-cfd",
            "item_number": item, "document_title": title, "file_name": f"f{sd}.pdf",
            "document_url": f"u/{sd}", "text_content": text}


def _decision(document_id=107938):
    return {"document_id": document_id, "decision_id": f"d{document_id}",
            "decision": "approve", "adjudicator": "Peter Mains", "decided_at": "t",
            "digest": "e" * 64, "path": f"dec.json", "document_role": "item report",
            "proposal": {"path": "p.json", "digest": "r" * 64},
            "candidate": {"agenda_item_db_id": 278759}, "human_stated_item": "4.I"}


def _five():
    return [_decision(d) for d in (107915, 107916, 107917, 107938, 107939)]


#: The governed collision control and the accepted reservation receipt every plan
#: must now bind.  A synthetic plan without them is refused, which is the point.
_CONTROL = {"mode": "exact_key_reservation", "table": "agenda_item_key_reservation",
            "primary_key": ["meeting_db_id", "agenda_item_number"],
            "invariant_carrier": "primary key", "advisory_locking": True,
            "scope": "governed writers only", "historical_key_is_unique": False,
            "signature_digest": "0" * 64}
_RECEIPT = {"path": "kg-stage2-reservation-schema-receipt-deadbeefdeadbeef.json",
            "digest": "0" * 64}


def _plan(holds, canonical=(), decisions=None):
    return RP.build_plan(holds, canonical_items=list(canonical),
                         decisions=decisions if decisions is not None else _five(),
                         created_at="t",
                         target={"dialect": "postgresql", "database": "poliscopic_dev"},
                         collision_control=_CONTROL, reservation_receipt=_RECEIPT)


# 1 ── one canonical digest field; replay hash named separately ─────────


def test_the_recorded_digest_is_the_canonical_artifact_digest():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    assert plan["digest"] == artifacts.compute_digest(plan)


def test_a_competing_digest_is_impossible():
    """Tampering with the body breaks the recorded digest."""
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    tampered = copy.deepcopy(plan)
    tampered["rows"][0]["proposed_title"] = "something else"
    assert tampered["digest"] != artifacts.compute_digest(tampered)
    assert any("canonical digest" in p for p in RP.validate_plan(tampered))


def test_replay_digest_is_separate_and_ignores_metadata():
    a = _plan([_row(1, 10, "2.C", _BAR)])
    b = RP.build_plan([_row(1, 10, "2.C", _BAR)], canonical_items=[], decisions=_five(),
                      created_at="another", target={"dialect": "postgresql",
                                                    "database": "poliscopic_dev"},
                   collision_control=_CONTROL, reservation_receipt=_RECEIPT)
    assert a["replay_digest"] == b["replay_digest"]
    assert a["digest"] != b["digest"]


def test_the_recorded_plan_loads_but_refuses_after_bound_code_drift():
    plan = artifacts.load_verified(_PLAN)
    assert any("stage2_s2_classify.py" in problem
               for problem in RP.validate_plan(plan))
    assert plan["digest"] == artifacts.recorded_digest(plan)


# 2 ── lineage, decisions, population, target, code bindings ───────────


def test_the_plan_binds_the_current_lineage_and_target():
    plan = artifacts.load_verified(_PLAN)
    lineage = plan["bindings"]["lineage"]
    assert lineage["plan"]["digest"] and lineage["aggregate"]["digest"]
    assert plan["bindings"]["target"]["database"] == "poliscopic_dev"
    assert lineage["aggregate"]["approved"] == 5
    assert lineage["aggregate"]["promoted"] == 0


def test_the_plan_binds_all_five_decisions_with_identity_and_time():
    plan = artifacts.load_verified(_PLAN)
    decisions = plan["bindings"]["decisions"]
    assert len(decisions) == 5
    assert {d["document_id"] for d in decisions} == {107915, 107916, 107917, 107938, 107939}
    for d in decisions:
        assert d["decision_id"] and d["digest"] and d["adjudicator"] == "Peter Mains"
        assert d["decided_at"] and d["proposal_digest"]
        assert "human approval" in d["relationship"]


def test_the_code_hashes_cover_both_builders_and_the_binding_module():
    plan = artifacts.load_verified(_PLAN)
    hashes = plan["bindings"]["code_hashes"]
    for required in ("scripts/kg/stage2_s2_repair_plan.py",
                     "scripts/kg/stage2_s2_label_correction.py",
                     "scripts/kg/stage2_s2_plan_binding.py"):
        assert required in hashes


def test_a_missing_binding_is_refused():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    del plan["bindings"]["lineage"]
    assert any("lineage" in p for p in RP.validate_plan(plan))


def test_a_short_decision_set_is_refused():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    plan["bindings"]["decisions"] = plan["bindings"]["decisions"][:4]
    assert any("expected 5 bound decisions" in p for p in RP.validate_plan(plan))


def test_a_decision_without_a_proposal_digest_is_refused():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    plan["bindings"]["decisions"][0]["proposal_digest"] = None
    assert any("proposal_digest" in p for p in RP.validate_plan(plan))


# 3 ── witness bindings: row fingerprint, content hash, number/title spans


def test_a_materialise_row_binds_the_full_witness():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    witness = plan["rows"][0]["witness"]
    assert len(witness["document_row_fingerprint"]) == 64
    assert witness["content_sha256"] == E.content_sha256(_BAR)
    assert witness["number_span"]["span"] == "AGENDA ITEM: 2.C."
    assert witness["title_span"]["span"].startswith("FIN - Resolution")
    assert witness["evidence_sha256"] == witness["number_span"]["sha256"]


def test_a_witness_without_a_title_span_is_refused():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    plan["rows"][0]["witness"]["title_span"] = None
    assert any("title span" in p for p in RP.validate_plan(plan))


def test_a_content_hash_change_is_detectable():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    other = _plan([_row(1, 10, "2.C", _BAR + "extra")])
    assert (plan["rows"][0]["witness"]["content_sha256"]
            != other["rows"][0]["witness"]["content_sha256"])


# 4 ── exact set equality, not cardinality ─────────────────────────────


def test_accounting_is_exact_set_equality():
    holds = [_row(1, 10, "2.C", _BAR), _row(2, 10, "2.C", _ATTACH)]
    plan = _plan(holds)
    assert plan["accounting"]["set_equality"] is True
    assert plan["accounting"]["accounted_document_ids"] == [1, 2]
    assert plan["accounting"]["authoritative_population_sha256"] == \
        B.hold_population(holds)["sha256"]


def test_a_disjoint_same_size_population_is_refused():
    """Two documents swapped for two different documents is not the same work."""
    rows_a = [_row(1, 10, "2.C", _BAR), _row(2, 10, "2.C", _ATTACH)]
    rows_b = [_row(3, 10, "2.C", _BAR), _row(4, 10, "2.C", _ATTACH)]
    plan = _plan(rows_a)
    assert len(B.hold_population(rows_a)["document_ids"]) == \
        len(B.hold_population(rows_b)["document_ids"])
    assert B.hold_population(rows_a)["sha256"] != B.hold_population(rows_b)["sha256"]
    plan["accounting"]["accounted_document_ids"] = [3, 4]
    problems = RP.validate_plan(plan, holds=rows_a)
    assert any("do not equal the supplied hold population" in p for p in problems)


def test_a_missing_document_is_refused():
    """Truncating the accounting is caught against the BOUND population."""
    plan = _plan([_row(1, 10, "2.C", _BAR), _row(2, 10, "2.C", _ATTACH)])
    plan["accounting"]["accounted_document_ids"] = [1]
    assert any("do not equal the bound hold population" in p
               for p in RP.validate_plan(plan))


def test_a_tampered_population_digest_is_refused():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    plan["accounting"]["authoritative_population_sha256"] = "0" * 64
    assert any("population digest does not match" in p for p in RP.validate_plan(plan))


def test_a_duplicated_document_is_refused():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    plan["accounting"]["accounted_document_ids"] = [1, 1]
    assert any("more than once" in p or "do not equal" in p
               for p in RP.validate_plan(plan))


# 5 ── replay contract is truthful ─────────────────────────────────────


def test_replay_claims_no_stability():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    assert plan["replay"]["no_stability_claim"] is True
    assert "does not claim" not in plan["replay"]["statement"]
    assert "CHANGES the database" in plan["replay"]["statement"]
    assert "no-op" in plan["replay"]["on_post_apply_run"]


def test_a_stability_claim_is_refused():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    plan["replay"]["no_stability_claim"] = False
    assert any("stays valid after apply" in p for p in RP.validate_plan(plan))



def test_rollback_is_owned_by_the_receipt_and_never_by_the_natural_key():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    rollback = plan["rollback"]
    assert "never delete by (meeting_db_id, agenda_item_number) alone" == rollback["never"]
    assert rollback["ownership"] == "only surrogate ids recorded in the apply receipt"
    assert rollback["restore"] == "exact preimage values, never recomputed ones"


def test_each_proposed_row_has_exact_values_and_an_identity_strategy():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    row = plan["rows"][0]
    assert row["proposed_row"]["agenda_item_number"] == "2.C"
    assert row["row_fingerprint"] == B.canonical_sha256(row["proposed_row"])
    assert row["identity"]["strategy"].startswith("surrogate id")
    assert row["identity"]["rollback_owner"] == "apply receipt inserted ids only"


def test_a_proposed_row_without_a_fingerprint_is_refused():
    plan = _plan([_row(1, 10, "2.C", _BAR)])
    plan["rows"][0]["row_fingerprint"] = None
    assert any("proposed row" in p for p in RP.validate_plan(plan))


def test_collision_locking_is_specified():
    plan = artifacts.load_verified(_PLAN)
    assert "FOR UPDATE" in plan["collision_policy"]["locking"]


# 7 ── decisions distinguished from deterministic repairs ──────────────


def test_the_accountability_block_separates_evidence_from_approval():
    plan = artifacts.load_verified(_PLAN)
    account = plan["accountability"]
    assert account["deterministic_evidence_repairs"]["count"] == 20
    assert account["approved_proposal_documents"]["count"] == 5
    assert account["overlap"] == [107915]
    assert "never merged" in account["statement"]


# 8 ── the recorded artifact is complete ───────────────────────────────


def test_the_recorded_plan_is_dry_and_accounts_for_every_hold():
    plan = artifacts.load_verified(_PLAN)
    assert plan["mode"] == "dry-run" and plan["applied"] is False
    assert plan["accounting"]["set_equality"] is True
    # 203 inherited holds minus the 30 documents the COMMITTED correction linked: a
    # linked document is no longer an unlinked hold.
    assert plan["counts"]["documents_accounted"] == 173
