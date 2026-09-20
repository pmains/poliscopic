#!/usr/bin/env python3
"""Scratch re-parse plan/runner: binding, drift, and fail-closed authorization."""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_scratch_reparse as S  # noqa: E402

_TARGET = {"dialect": "postgresql", "host": "192.0.2.10", "port": 5432,
           "database": "poliscopic_scratch_s2_reparse"}
_BACKUP = {"required": True, "receipt_path": "data/backups/x.receipt.json",
           "verified_before_execution": True}


def _plan(**overrides):
    kwargs = dict(target=dict(_TARGET), protected_backup=dict(_BACKUP),
                  created_at="2026-09-12T20:11:00+00:00")
    kwargs.update(overrides)
    return S.build_plan(**kwargs)


# ── binding ────────────────────────────────────────────────────────────


def test_the_plan_binds_all_six_fixture_cases():
    plan = _plan()
    assert plan["counts"]["cases"] == 6
    names = {c["fixture"] for c in plan["fixtures"]}
    assert names == {"granicus_423_cc.txt", "granicus_424_cc.txt", "granicus_1071_cfd.txt",
                     "granicus_1046_pz.txt", "workshop_cc.txt", "non_pz_arts_culture.txt"}


def test_every_case_binds_its_source_meeting_and_body():
    by_fixture = {c["fixture"]: c for c in _plan()["fixtures"]}
    assert by_fixture["granicus_423_cc.txt"]["db_meeting_id"] == 10558
    assert by_fixture["granicus_424_cc.txt"]["db_meeting_id"] == 10530
    assert by_fixture["granicus_1071_cfd.txt"]["db_meeting_id"] == 10428
    assert by_fixture["granicus_1046_pz.txt"]["db_meeting_id"] == 10461
    assert by_fixture["workshop_cc.txt"]["db_meeting_id"] == 11181
    assert by_fixture["non_pz_arts_culture.txt"]["db_meeting_id"] == 10476
    assert by_fixture["workshop_cc.txt"]["meeting_type"] == "Council Workshop"
    assert by_fixture["non_pz_arts_culture.txt"]["body"] == "buckeye-arts-culture"


def test_every_case_binds_expected_before_and_after_identities():
    for case in _plan()["fixtures"]:
        assert case["expected_items"], case["fixture"]
        assert case["must_not_contain"], case["fixture"]
        assert "held" in case or case["expected_holds"] == [], case["fixture"]
    by_fixture = {c["fixture"]: c for c in _plan()["fixtures"]}
    assert by_fixture["granicus_1071_cfd.txt"]["must_not_contain"] == ["2026"]


def test_the_plan_binds_code_hashes_and_fixture_hashes():
    plan = _plan()
    assert len(plan["code_hashes"]) == 4
    assert all(len(h) == 64 for h in plan["code_hashes"].values())
    assert all(len(c["fixture_sha256"]) == 64 for c in plan["fixtures"])


def test_the_plan_declares_zero_mutation_of_development():
    plan = _plan()
    assert plan["creates_database"] is False
    assert plan["executed"] is False
    assert plan["zero_mutation"]["database"] == "poliscopic_dev"


# ── target and backup ──────────────────────────────────────────────────


def test_a_development_target_is_refused():
    with pytest.raises(S.ScratchReparseRefused) as exc:
        _plan(target={**_TARGET, "database": "poliscopic_dev"})
    assert "must not target poliscopic_dev" in str(exc.value)


def test_a_missing_backup_requirement_is_refused():
    with pytest.raises(S.ScratchReparseRefused):
        _plan(protected_backup={"required": False, "receipt_path": "x",
                                "verified_before_execution": True})


def test_a_backup_without_a_receipt_is_refused():
    with pytest.raises(S.ScratchReparseRefused):
        _plan(protected_backup={"required": True, "release": "x",
                                "verified_before_execution": True})


# ── drift ──────────────────────────────────────────────────────────────


def test_a_drifted_code_hash_is_refused():
    plan = _plan()
    plan["code_hashes"]["scripts/kg/stage2_scratch_reparse.py"] = "0" * 64
    problems = S.validate_plan(plan)
    assert any("has drifted" in p or "missing" in p for p in problems)


def test_a_drifted_fixture_is_refused():
    plan = _plan()
    plan["fixtures"][0]["fixture_sha256"] = "0" * 64
    assert any("drifted" in p for p in S.validate_plan(plan))


def test_a_tampered_target_is_refused():
    plan = _plan()
    problems = S.validate_plan(plan, target={**_TARGET, "database": "other"})
    assert any("does not match the supplied target" in p for p in problems)


# ── authorization: nothing runs by default ─────────────────────────────


def test_execution_without_authorization_is_refused():
    with pytest.raises(S.ScratchReparseRefused) as exc:
        S.execute(_plan(), None)
    assert "explicit authorization" in str(exc.value)


def test_authorization_for_another_plan_is_refused():
    plan = _plan()
    auth = S.Authorization(plan_digest="0" * 64, phrase=S.AUTHORIZATION_PHRASE,
                           authorized_by="Peter Mains", authorized_at="t")
    with pytest.raises(S.ScratchReparseRefused) as exc:
        S.execute(plan, auth)
    assert "different plan digest" in str(exc.value)


def test_a_wrong_phrase_is_refused():
    plan = _plan()
    auth = S.Authorization(plan_digest=S._plan_digest(plan), phrase="yes please",
                           authorized_by="Peter Mains", authorized_at="t")
    with pytest.raises(S.ScratchReparseRefused) as exc:
        S.execute(plan, auth)
    assert "phrase does not match" in str(exc.value)


def test_an_authorization_without_an_authorizer_is_refused():
    plan = _plan()
    auth = S.Authorization(plan_digest=S._plan_digest(plan), phrase=S.AUTHORIZATION_PHRASE,
                           authorized_by="   ", authorized_at="t")
    with pytest.raises(S.ScratchReparseRefused) as exc:
        S.execute(plan, auth)
    assert "no authorizer" in str(exc.value)


def test_a_fully_authorized_plan_still_refuses_without_an_engine():
    """Creating a scratch database is a separate act this runner will not do."""
    plan = _plan()
    auth = S.Authorization(plan_digest=S._plan_digest(plan), phrase=S.AUTHORIZATION_PHRASE,
                           authorized_by="Peter Mains", authorized_at="t")
    with pytest.raises(S.ScratchReparseRefused) as exc:
        S.execute(plan, auth, engine=None)
    assert "no engine supplied" in str(exc.value)


class _FakeEngine:
    def __init__(self, database):
        self.url = type("U", (), {"database": database})()


def test_an_authorized_run_against_development_is_refused():
    plan = _plan()
    auth = S.Authorization(plan_digest=S._plan_digest(plan), phrase=S.AUTHORIZATION_PHRASE,
                           authorized_by="Peter Mains", authorized_at="t")
    with pytest.raises(S.ScratchReparseRefused) as exc:
        S.execute(plan, auth, engine=_FakeEngine("poliscopic_dev"))
    assert "refusing to execute against 'poliscopic_dev'" in str(exc.value)


def test_the_recorded_plan_artifact_is_immutable_and_digest_bound():
    path = _REPO / "data" / "kg-plans" / \
        "kg-stage2-scratch-reparse-plan-scratch-reparse-20260912T201100Z.json"
    assert path.exists()
    problems = S.validate_plan(__import__("json").loads(path.read_text()))
    assert problems == []
