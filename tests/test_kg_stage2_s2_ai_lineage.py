#!/usr/bin/env python3
"""Stage 2 S2 — artifact lineage: head selection, membership, refusals.

Modification time is not evidence, so these tests deliberately touch files and
reorder mtimes to prove the lineage is read from recorded supersession.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_ai_lineage as lineage  # noqa: E402

_TARGET = {"dialect": "postgresql", "host": "192.0.2.10", "port": 5432,
           "database": "poliscopic_dev"}


def _plan(folder, plan_id, supersedes=None):
    path = folder / f"kg-stage2-s2-plan-{plan_id}.json"
    payload = {"kind": "kg-stage2-s2-plan", "plan_id": plan_id, "target": dict(_TARGET),
               "supersedes": supersedes}
    digest = artifacts.write_immutable(path, payload)
    return path, digest


def _aggregate(folder, stamp, plan_digest, per_unit):
    path = folder / f"kg-stage2-s2-ai-proposals-{stamp}.json"
    payload = {"kind": "kg-stage2-s2-ai-proposals-aggregate", "version": "test",
               "manifest": {"plan_digest": plan_digest}, "per_unit": per_unit}
    digest = artifacts.write_immutable(path, payload)
    return path, digest


def _proposal(folder, name, plan_digest):
    path = folder / name
    payload = {"kind": "kg-stage2-s2-ai-proposal", "document_id": 1,
               "input_fingerprints": {"plan": plan_digest}}
    digest = artifacts.write_immutable(path, payload)
    return path, digest


def _touch(path, seconds=120):
    stamp = time.time() + seconds
    os.utime(path, (stamp, stamp))


# ── head selection by supersession, not mtime ──────────────────────────


def test_the_head_is_the_plan_nothing_supersedes(tmp_path):
    _old, old_digest = _plan(tmp_path, "20260101T000000Z")
    head, head_digest = _plan(tmp_path, "20260102T000000Z",
                              supersedes={"path": "kg-stage2-s2-plan-20260101T000000Z.json",
                                          "digest": old_digest})
    path, document, digest = lineage.current_plan(tmp_path)
    assert path == head and digest == head_digest


def test_a_touched_old_plan_does_not_become_the_head(tmp_path):
    """The whole point: mtime cannot promote a superseded plan."""
    old, old_digest = _plan(tmp_path, "20260101T000000Z")
    head, head_digest = _plan(tmp_path, "20260102T000000Z",
                              supersedes={"path": old.name, "digest": old_digest})
    _touch(old, seconds=600)                      # old is now the newest file on disk
    assert old.stat().st_mtime > head.stat().st_mtime
    path, _document, digest = lineage.current_plan(tmp_path)
    assert path == head and digest == head_digest


def test_no_head_is_refused(tmp_path):
    only, _digest = _plan(tmp_path, "20260101T000000Z")
    artifacts.record_obsolete(tmp_path, only, "superseded historically")
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.current_plan(tmp_path)
    assert "no current plan head" in str(exc.value)


def test_multiple_heads_is_refused(tmp_path):
    _plan(tmp_path, "20260101T000000Z")
    _plan(tmp_path, "20260102T000000Z")           # neither supersedes the other
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.current_plan(tmp_path)
    assert "ambiguous plan lineage" in str(exc.value)


def test_an_empty_directory_has_no_head(tmp_path):
    with pytest.raises(lineage.LineageRefused):
        lineage.current_plan(tmp_path)


# ── aggregate selection ────────────────────────────────────────────────


def test_the_aggregate_must_bind_the_current_plan(tmp_path):
    _p, plan_digest = _plan(tmp_path, "20260101T000000Z")
    _aggregate(tmp_path, "20260101T010000Z", "0" * 64, {})      # binds another plan
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.current_aggregate(tmp_path, plan_digest)
    assert "no current aggregate binds" in str(exc.value)


def test_two_aggregates_binding_the_head_is_refused(tmp_path):
    _p, plan_digest = _plan(tmp_path, "20260101T000000Z")
    _aggregate(tmp_path, "20260101T010000Z", plan_digest, {})
    _aggregate(tmp_path, "20260101T020000Z", plan_digest, {})
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.current_aggregate(tmp_path, plan_digest)
    assert "ambiguous aggregate lineage" in str(exc.value)


# ── proposal membership ────────────────────────────────────────────────


def test_a_forged_membership_is_refused(tmp_path):
    """A proposal that names itself in the aggregate but is not listed."""
    _p, plan_digest = _plan(tmp_path, "20260101T000000Z")
    member, member_digest = _proposal(tmp_path, "proposal-member.json", plan_digest)
    _aggregate(tmp_path, "20260101T010000Z", plan_digest,
               {"u1": [{"path": member.name, "digest": member_digest}]})
    forger, _d = _proposal(tmp_path, "proposal-forged.json", plan_digest)
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.verify_lineage(forger, artifacts.load_verified(forger), tmp_path)
    assert "not a member" in str(exc.value)


def test_a_digest_that_is_not_a_member_is_refused(tmp_path):
    _p, plan_digest = _plan(tmp_path, "20260101T000000Z")
    member, _digest = _proposal(tmp_path, "proposal-member.json", plan_digest)
    _aggregate(tmp_path, "20260101T010000Z", plan_digest,
               {"u1": [{"path": member.name, "digest": "0" * 64}]})
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.verify_lineage(member, artifacts.load_verified(member), tmp_path)
    assert "digest" in str(exc.value)


def test_membership_can_be_checked_directly(tmp_path):
    aggregate = {"per_unit": {"u1": [{"path": "p.json", "digest": "abc"}]}}
    ok, problems = lineage.proposal_membership(aggregate, "p.json", "abc")
    assert ok and not problems
    ok, problems = lineage.proposal_membership(aggregate, "other.json", "abc")
    assert not ok and "not a member" in problems[0]
    ok, problems = lineage.proposal_membership({}, "p.json", "abc")
    assert not ok and "lists no per-unit" in problems[0]


# ── the full chain ─────────────────────────────────────────────────────


def _chain(tmp_path):
    old, old_digest = _plan(tmp_path, "20260101T000000Z")
    head, head_digest = _plan(tmp_path, "20260102T000000Z",
                              supersedes={"path": old.name, "digest": old_digest})
    proposal, proposal_digest = _proposal(tmp_path, "proposal-u1-doc1.json", head_digest)
    aggregate, aggregate_digest = _aggregate(
        tmp_path, "20260102T010000Z", head_digest,
        {"u1": [{"path": proposal.name, "digest": proposal_digest}]})
    return head, proposal, aggregate


def test_a_current_lineage_verifies(tmp_path):
    head, proposal, aggregate = _chain(tmp_path)
    record = lineage.verify_lineage(proposal, artifacts.load_verified(proposal), tmp_path)
    assert record["plan"]["path"] == head.name
    assert record["aggregate"]["path"] == aggregate.name
    assert record["proposal"]["path"] == proposal.name
    assert record["plan_heads"] == [head.name]


def test_an_obsolete_proposal_is_refused(tmp_path):
    _head, proposal, _aggregate = _chain(tmp_path)
    artifacts.record_obsolete(tmp_path, proposal, "superseded generation")
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.verify_lineage(proposal, artifacts.load_verified(proposal), tmp_path)
    assert "obsolete" in str(exc.value)


def test_a_proposal_from_an_old_aggregate_is_refused(tmp_path):
    """Bound to the old plan, so its fingerprint no longer matches the head."""
    old, old_digest = _plan(tmp_path, "20260101T000000Z")
    stale, stale_digest = _proposal(tmp_path, "proposal-old.json", old_digest)
    _aggregate(tmp_path, "20260101T010000Z", old_digest,
               {"u1": [{"path": stale.name, "digest": stale_digest}]})
    head, head_digest = _plan(tmp_path, "20260102T000000Z",
                              supersedes={"path": old.name, "digest": old_digest})
    member, member_digest = _proposal(tmp_path, "proposal-new.json", head_digest)
    _aggregate(tmp_path, "20260102T010000Z", head_digest,
               {"u1": [{"path": member.name, "digest": member_digest}]})
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.verify_lineage(stale, artifacts.load_verified(stale), tmp_path)
    assert "does not equal the current plan digest" in str(exc.value)
    # ...and the new one is fine
    assert lineage.verify_lineage(member, artifacts.load_verified(member), tmp_path)


def test_an_aggregate_bound_to_another_plan_is_refused(tmp_path):
    head, proposal, real_aggregate = _chain(tmp_path)
    _aggregate(tmp_path, "20260103T010000Z", "0" * 64, {})
    # the forged aggregate does not bind the head, so it is not current; the real
    # one still is and the proposal still verifies
    record = lineage.verify_lineage(proposal, artifacts.load_verified(proposal), tmp_path)
    assert record["aggregate"]["path"].startswith("kg-stage2-s2-ai-proposals-20260102")


def test_the_loader_reports_the_lineage(tmp_path):
    """The review entry point surfaces the plan and aggregate it bound."""
    import inspect
    from scripts.kg import stage2_s2_ai_adjudication_loader as loader
    source = inspect.getsource(loader.load_for_adjudication)
    assert "lineage.verify_lineage(" in source
    assert '"lineage": lineage_record' in source


# ── membership is a per-entry pairing, not two independent sets ────────


def _agg(entries):
    return {"per_unit": {"u1": list(entries)}}


def test_a_cross_pair_between_two_entries_is_refused(tmp_path):
    """The attack: borrow another proposal's digest while keeping your own name.

    Both the name and the digest exist somewhere in the aggregate, so an
    independent-set test accepts this. Membership is a property of one entry.
    """
    aggregate = _agg([{"path": "proposal-a.json", "digest": "d-a"},
                      {"path": "proposal-b.json", "digest": "d-b"}])
    ok, problems = lineage.proposal_membership(aggregate, "proposal-a.json", "d-b")
    assert not ok
    assert any("digest mismatch" in p for p in problems), problems
    # ...and the honest pairings still pass
    assert lineage.proposal_membership(aggregate, "proposal-a.json", "d-a") == (True, [])
    assert lineage.proposal_membership(aggregate, "proposal-b.json", "d-b") == (True, [])


def test_a_cross_pair_is_refused_end_to_end(tmp_path):
    """The same attack through verify_lineage, with real artifacts."""
    old, old_digest = _plan(tmp_path, "20260101T000000Z")
    head, head_digest = _plan(tmp_path, "20260102T000000Z",
                              supersedes={"path": old.name, "digest": old_digest})
    mine, _mine_digest = _proposal(tmp_path, "proposal-mine.json", head_digest)
    theirs, theirs_digest = _proposal(tmp_path, "proposal-theirs.json", head_digest)
    _aggregate(tmp_path, "20260102T010000Z", head_digest,
               {"u1": [{"path": mine.name, "digest": "0" * 64},
                       {"path": theirs.name, "digest": theirs_digest}]})
    with pytest.raises(lineage.LineageRefused) as exc:
        lineage.verify_lineage(mine, artifacts.load_verified(mine), tmp_path)
    assert "digest mismatch" in str(exc.value)


def test_duplicate_path_entries_are_refused():
    aggregate = _agg([{"path": "proposal-a.json", "digest": "d-a"},
                      {"path": "proposal-a.json", "digest": "d-a"}])
    ok, problems = lineage.proposal_membership(aggregate, "proposal-a.json", "d-a")
    assert not ok
    assert any("ambiguous" in p for p in problems), problems


def test_duplicate_paths_with_different_digests_are_refused():
    aggregate = _agg([{"path": "proposal-a.json", "digest": "d-a"},
                      {"path": "proposal-a.json", "digest": "d-b"}])
    ok, problems = lineage.proposal_membership(aggregate, "proposal-a.json", "d-a")
    assert not ok and any("ambiguous" in p for p in problems)


def test_an_absolute_entry_path_is_refused():
    aggregate = _agg([{"path": "/tmp/proposal-a.json", "digest": "d-a"}])
    ok, problems = lineage.proposal_membership(aggregate, "proposal-a.json", "d-a")
    assert not ok and any("absolute" in p for p in problems), problems


def test_a_traversal_entry_path_is_refused():
    aggregate = _agg([{"path": "sub/../proposal-a.json", "digest": "d-a"}])
    ok, problems = lineage.proposal_membership(aggregate, "proposal-a.json", "d-a")
    assert not ok and any("traversal" in p for p in problems), problems


def test_the_requested_path_may_be_absolute_but_not_traversing():
    """A caller may pass an absolute path; it resolves by basename.

    Traversal is still refused: it lets the requested path climb out of the
    store.  The ambiguity that matters is in the AGGREGATE's entry paths.
    """
    aggregate = _agg([{"path": "proposal-a.json", "digest": "d-a"}])
    assert lineage.proposal_membership(aggregate, "/tmp/proposal-a.json", "d-a") == (True, [])
    ok, problems = lineage.proposal_membership(aggregate, "sub/../proposal-a.json", "d-a")
    assert not ok and any("traversal" in p for p in problems), problems


def test_a_missing_entry_is_refused():
    aggregate = _agg([{"path": "proposal-a.json", "digest": "d-a"}])
    ok, problems = lineage.proposal_membership(aggregate, "proposal-z.json", "d-a")
    assert not ok and "not a member" in problems[0]


def test_a_single_match_by_full_path_still_works():
    aggregate = _agg([{"path": "nested/proposal-a.json", "digest": "d-a"}])
    assert lineage.proposal_membership(aggregate, "nested/proposal-a.json", "d-a") == (True, [])
    # matching by basename alone is still accepted
    assert lineage.proposal_membership(aggregate, "proposal-a.json", "d-a") == (True, [])
