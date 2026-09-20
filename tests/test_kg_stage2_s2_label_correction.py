#!/usr/bin/env python3
"""Stage 2 label-correction plan — the same P1 corrections, re-reviewed."""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_apply_runner as AR  # noqa: E402
from scripts.kg import stage2_s2_evidence_materialize as E  # noqa: E402
from scripts.kg import stage2_s2_label_correction as LC  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as B  # noqa: E402

def _live(pattern):
    """The one live artifact matching a glob, resolved rather than named."""
    hits = [p for p in sorted((_REPO / "data" / "kg-plans").glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (p.parent / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


_PLAN = _live("kg-stage2-s2-label-correction-plan-*.json")

_TEMPE = _REPO / "data" / "kg-plans" / "kg-stage2-s2-meeting-level-evidence-9775.json"


def _doc(sd, meeting, recorded, stated, title="t"):
    header = (f"AGENDA ITEM: {stated}. {title}\nDATE PREPARED: 09/01/25\n"
              if stated else "no label\n")
    return {"id": sd, "meeting_db_id": meeting, "body": "buckeye-cc",
            "item_number": recorded, "document_title": title, "file_name": f"f{sd}.pdf",
            "document_url": f"u/{sd}", "text_content": header}


def _decision(document_id):
    return {"document_id": document_id, "decision_id": f"d{document_id}",
            "decision": "approve", "adjudicator": "Peter Mains", "decided_at": "t",
            "digest": "e" * 64, "path": "dec.json", "document_role": "role",
            "proposal": {"path": "p.json", "digest": "r" * 64},
            "candidate": {"agenda_item_db_id": 1}, "human_stated_item": "4.I"}


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


def _plan(rows, canonical=()):
    return LC.build_plan(rows, canonical_items=list(canonical), decisions=_five(),
                         created_at="t", target={"database": "poliscopic_dev"},
                   collision_control=_CONTROL, reservation_receipt=_RECEIPT)


# ── truncation and the two operations ─────────────────────────────────


def test_a_truncation_is_a_prefix_at_a_label_boundary():
    assert LC._is_truncation("4", "4.AA") is True
    assert LC._is_truncation("4", "4.A") is True
    assert LC._is_truncation("4", "4") is False
    assert LC._is_truncation("4", "5.AA") is False
    assert LC._is_truncation("4", "40") is False


def test_a_group_without_the_truncated_row_creates_new_rows():
    plan = _plan([_doc(1, 10, "4", "4.AA")])
    op = plan["operations"][0]
    assert op["action"] == "new_item_row"
    assert op["creates_a_new_row"] is True and op["renumbers_an_existing_row"] is False


def test_a_group_with_the_truncated_row_renumbers():
    plan = _plan([_doc(1, 10, "4", "4.AA")],
                 canonical=[{"meeting_db_id": 10, "agenda_item_number": "4"}])
    op = plan["operations"][0]
    assert op["action"] == "renumber_existing_row"
    assert op["renumber"]["from"] == "4" and op["renumber"]["to"] == "4.AA"


def test_an_existing_corrected_item_is_reported_not_recreated():
    plan = _plan([_doc(1, 10, "4", "4.AA")],
                 canonical=[{"meeting_db_id": 10, "agenda_item_number": "4.AA"}])
    assert plan["operations"][0]["action"] == "already_materialised"


def test_a_multi_label_group_holds_the_unlabelled_attachment():
    rows = [_doc(1, 10, "4", "4.AA"), _doc(2, 10, "4", "4.AB"), _doc(3, 10, "4", None)]
    plan = _plan(rows)
    assert plan["counts"]["operations"] == 2 and plan["counts"]["held_documents"] == 1
    assert "document order" in plan["held_documents"][0]["reason"]


# 1 ── canonical digest ────────────────────────────────────────────────


def test_the_recorded_digest_is_canonical_and_competing_digests_are_impossible():
    plan = _plan([_doc(1, 10, "4", "4.AA")])
    assert plan["digest"] == artifacts.compute_digest(plan)
    tampered = copy.deepcopy(plan)
    tampered["operations"][0]["proposed_title"] = "changed"
    assert any("canonical digest" in p for p in LC.validate_plan(tampered))


def test_the_recorded_plan_loads_but_refuses_after_bound_code_drift():
    plan = artifacts.load_verified(_PLAN)
    assert any("stage2_s2_classify.py" in problem
               for problem in LC.validate_plan(plan))
    assert plan["digest"] == artifacts.recorded_digest(plan)


def test_replay_digest_is_separate():
    a = _plan([_doc(1, 10, "4", "4.AA")])
    b = LC.build_plan([_doc(1, 10, "4", "4.AA")], canonical_items=[],
                      decisions=_five(), created_at="other",
                      target={"database": "poliscopic_dev"},
                   collision_control=_CONTROL, reservation_receipt=_RECEIPT)
    assert a["replay_digest"] == b["replay_digest"] and a["digest"] != b["digest"]


# 2 ── bindings ───────────────────────────────────────────────────────


def test_the_recorded_plan_binds_lineage_decisions_target_and_code():
    plan = artifacts.load_verified(_PLAN)
    bindings = plan["bindings"]
    assert bindings["lineage"]["plan"]["digest"]
    assert bindings["lineage"]["aggregate"]["digest"]
    assert bindings["target"]["database"] == "poliscopic_dev"
    assert len(bindings["decisions"]) == 5
    for required in ("scripts/kg/stage2_s2_repair_plan.py",
                     "scripts/kg/stage2_s2_label_correction.py",
                     "scripts/kg/stage2_s2_plan_binding.py"):
        assert required in bindings["code_hashes"]


def test_a_missing_binding_is_refused():
    plan = _plan([_doc(1, 10, "4", "4.AA")])
    del plan["bindings"]["hold_population"]
    assert any("hold_population" in p for p in LC.validate_plan(plan))


# 3 ── witness bindings ───────────────────────────────────────────────


def test_each_operation_binds_the_full_witness():
    plan = _plan([_doc(1, 10, "4", "4.AA")])
    witness = plan["operations"][0]["witness"]
    assert len(witness["document_row_fingerprint"]) == 64
    assert witness["number_span"]["span"] == "AGENDA ITEM: 4.AA."
    assert witness["title_span"]["span"] == "t"
    assert witness["content_sha256"] == E.content_sha256(
        witness and _doc(1, 10, "4", "4.AA")["text_content"])


def test_a_witness_without_a_number_span_is_refused():
    plan = _plan([_doc(1, 10, "4", "4.AA")])
    plan["operations"][0]["witness"]["number_span"] = None
    assert any("number_span" in p for p in LC.validate_plan(plan))


# 4 ── exact set equality ─────────────────────────────────────────────


def test_accounting_is_exact_set_equality_and_a_disjoint_population_is_refused():
    rows_a = [_doc(1, 10, "4", "4.AA"), _doc(2, 10, "4", None)]
    rows_b = [_doc(3, 10, "4", "4.AA"), _doc(4, 10, "4", None)]
    plan = _plan(rows_a)
    assert plan["accounting"]["set_equality"] is True
    assert B.hold_population(rows_a)["sha256"] != B.hold_population(rows_b)["sha256"]
    plan["accounting"]["accounted_document_ids"] = [3, 4]
    assert any("bound hold population" in p for p in LC.validate_plan(plan))


# 5 ── replay ─────────────────────────────────────────────────────────



def test_rollback_forbids_the_natural_key_and_requires_preimages():
    plan = _plan([_doc(1, 10, "4", "4.AA")])
    rollback = plan["rollback"]
    assert "never delete by (meeting_db_id, agenda_item_number) alone" == rollback["never"]
    assert rollback["restore"] == "exact preimage values, never recomputed ones"
    assert "renumbered_preimages" in rollback


def test_each_operation_has_exact_values_and_identity():
    plan = _plan([_doc(1, 10, "4", "4.AA")])
    op = plan["operations"][0]
    assert op["proposed_row"]["agenda_item_number"] == "4.AA"
    assert op["row_fingerprint"] == B.canonical_sha256(op["proposed_row"])
    assert op["identity"]["rollback_owner"] == "apply receipt recorded ids only"


def test_collision_locking_is_specified():
    plan = artifacts.load_verified(_PLAN)
    assert "FOR UPDATE" in plan["collision_policy"]["locking"]


# 7 ── decisions vs evidence ─────────────────────────────────────────


def test_the_recorded_plan_separates_evidence_from_approval():
    plan = artifacts.load_verified(_PLAN)
    account = plan["accountability"]
    assert account["deterministic_evidence_repairs"]["count"] == 30
    assert account["approved_proposal_documents"]["count"] == 5
    assert account["overlap"] == []


# ── the recorded artifacts ───────────────────────────────────────────


def test_the_recorded_correction_plan_accounts_for_all_four_groups():
    plan = artifacts.load_verified(_PLAN)
    assert plan["counts"] == {"groups": 4, "operations": 30, "new_item_row": 29,
                              "renumber_existing_row": 0, "already_materialised": 1,
                              "held_documents": 70, "documents_accounted": 101}
    assert plan["accounting"]["set_equality"] is True
    assert len(set(plan["accounting"]["accounted_document_ids"])) == 101


def test_the_tempe_artifact_keeps_the_hold_without_inventing_an_item():
    art = artifacts.load_verified(_TEMPE)
    assert art["classification"] == "meeting_level_only"
    assert art["no_item_invented"] is True
    assert [d["document_id"] for d in art["documents"]] == [46970, 46971]
    assert art["accounted_exactly_once"] is True


# ── the runner owns rollback and refuses everything by default ────────
