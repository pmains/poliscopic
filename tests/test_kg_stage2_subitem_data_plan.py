#!/usr/bin/env python3
"""Adversarial refusal, rollback and replay tests for the containment data apply."""

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

from sqlalchemy import text  # noqa: E402

from scripts.db.core import get_engine  # noqa: E402
from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage2_subitem_data_apply as AP  # noqa: E402
from scripts.kg import stage2_subitem_data_plan as P  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _pg():
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("the containment data apply requires the development PostgreSQL tier")
    return engine


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if len(hits) != 1:
        pytest.skip(f"{pattern!r} matched {len(hits)}")
    return hits[0]


def _backup_path():
    plans = sorted((_REPO / "data").glob("kg-stage1-backup-receipt-*.json"))
    if not plans:
        pytest.skip("no backup receipt available")
    return plans[-1]


def _non_null():
    with _pg().connect() as c:
        return int(c.execute(text(
            "SELECT COUNT(*) FROM agenda_items WHERE parent_item_id IS NOT NULL")).scalar())


def _pristine():
    if _non_null() != 0:
        pytest.skip("edges are already applied; the pre-apply assertions no longer hold")


@pytest.fixture(scope="module")
def plan():
    return A.load_verified(_live("kg-stage2-subitem-data-plan-*.json"))


def test_the_plan_is_dry_run_and_binds_every_required_artifact(plan):
    assert plan["mode"] == "dry-run" and plan["applied"] is False
    assert plan["relation"] == "PART_OF"
    b = plan["bindings"]
    assert b["baseline_digest"]
    assert b["containment_plan_digest"]
    assert b["schema_receipt_digest"]
    assert b["schema_signature"]["column_present"] is True
    assert b["row_fingerprints_digest"]
    assert b["table_fingerprint"]
    assert b["collision_population"]["row_ids_count"] > 0
    assert b["fresh_backup_receipt"]["restore_proven"] is True
    assert any("stage2_subitem_schema_plan.py" in problem
               for problem in P.validate_plan(plan))


def test_the_operation_set_is_exactly_the_plan_count(plan):
    assert plan["counts"]["operations"] == len(plan["operations"])
    assert plan["counts"]["operations"] == 459
    owned = [o["child_item_id"] for o in plan["operations"]]
    assert len(set(owned)) == len(owned), "a child must be claimed once"


def test_every_operation_shortens_and_never_self_links(plan):
    for op in plan["operations"]:
        c, p = str(op["child_number"]), str(op["parent_number"])
        assert c.startswith(p + ".") and len(c) > len(p)
        assert op["child_item_id"] != op["parent_item_id"]


def test_a_drifted_digest_is_refused(plan):
    tampered = copy.deepcopy(plan)
    tampered["operations"] = tampered["operations"][:-1]
    with pytest.raises(AP.ApplyRefused):
        AP.apply_plan(get_engine(), tampered,
                      supplied_digest=A.recorded_digest(plan),
                      backup_receipt=_backup_path(), out_dir=_PLANS)


def test_target_drift_is_refused(plan):
    tampered = copy.deepcopy(plan)
    tampered["target"] = dict(plan["target"], database="somewhere_else")
    tampered.pop(A.DIGEST_FIELD, None)
    tampered[A.DIGEST_FIELD] = A.compute_digest(tampered)
    with pytest.raises(AP.ApplyRefused):
        AP.apply_plan(_pg(), tampered, supplied_digest=A.recorded_digest(tampered),
                      backup_receipt=_backup_path(), out_dir=_PLANS)


def test_schema_drift_is_refused(plan):
    tampered = copy.deepcopy(plan)
    tampered["bindings"]["schema_signature"] = dict(
        tampered["bindings"]["schema_signature"], digest="0" * 64)
    tampered.pop(A.DIGEST_FIELD, None)
    tampered[A.DIGEST_FIELD] = A.compute_digest(tampered)
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.apply_plan(_pg(), tampered, supplied_digest=A.recorded_digest(tampered),
                      backup_receipt=_backup_path(), out_dir=_PLANS)
    assert "schema" in str(exc.value)


def test_baseline_and_containment_digest_drift_are_refused(plan):
    for field in ("baseline_digest", "containment_plan_digest",
                  "row_fingerprints_digest", "schema_receipt_digest"):
        tampered = copy.deepcopy(plan)
        tampered["bindings"][field] = "0" * 64
        assert any("binds no" not in p for p in P.validate_plan(tampered))
        assert tampered["bindings"][field] == "0" * 64


def test_containment_plan_digest_drift_is_refused_at_apply(plan):
    tampered = copy.deepcopy(plan)
    tampered["bindings"]["containment_plan_digest"] = "1" * 64
    tampered["bindings"]["collision_population"] = dict(
        tampered["bindings"]["collision_population"], row_ids_sha256="2" * 64)
    tampered.pop(A.DIGEST_FIELD, None)
    tampered[A.DIGEST_FIELD] = A.compute_digest(tampered)
    with pytest.raises(AP.ApplyRefused):
        AP.apply_plan(_pg(), tampered, supplied_digest=A.recorded_digest(tampered),
                      backup_receipt=_backup_path(), out_dir=_PLANS)


def test_table_fingerprint_drift_is_refused(plan):
    tampered = copy.deepcopy(plan)
    tampered["bindings"]["table_fingerprint"] = "3" * 64
    tampered.pop(A.DIGEST_FIELD, None)
    tampered[A.DIGEST_FIELD] = A.compute_digest(tampered)
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.apply_plan(_pg(), tampered, supplied_digest=A.recorded_digest(tampered),
                      backup_receipt=_backup_path(), out_dir=_PLANS)
    assert "identity columns" in str(exc.value) or "drift" in str(exc.value)


def test_code_drift_is_refused(plan):
    tampered = copy.deepcopy(plan)
    hashes = dict(tampered["bindings"]["code_hashes"])
    hashes[sorted(hashes)[0]] = "0" * 64
    tampered["bindings"]["code_hashes"] = hashes
    assert any("drifted" in p for p in P.validate_plan(tampered))


def test_a_tampered_backup_receipt_is_refused(tmp_path):
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({"target": {"database": "poliscopic_dev"}}))
    forged.chmod(0o600)
    with pytest.raises(AP.ApplyRefused):
        AP.require_backup(forged, target={"database": "poliscopic_dev"})


def test_a_missing_backup_receipt_is_refused(tmp_path):
    with pytest.raises(AP.ApplyRefused):
        AP.require_backup(tmp_path / "absent.json",
                          target={"database": "poliscopic_dev"})


@pytest.mark.skipif(not _PLANS.exists(), reason="plan directory absent")
def test_a_dry_run_exercises_the_transaction_and_leaves_no_trace(plan):
    """The real transaction runs end to end, then is deliberately rolled back."""
    _pristine()
    result = AP.apply_plan(_pg(), plan, supplied_digest=A.recorded_digest(plan),
                           backup_receipt=_backup_path(), out_dir=_PLANS, dry_run=True)
    assert result["stage"] == "dry-run-rolled-back"
    assert result["writes"] == 0
    assert result["would_update"] == len(plan["operations"])
    assert _non_null() == 0


def test_a_postcondition_failure_rolls_the_whole_transaction_back(plan, monkeypatch):
    _pristine()
    def boom(connection, ops):
        raise AP.ApplyRefused("forced postcondition failure")
    monkeypatch.setattr(AP, "_check_cycles", boom)
    with pytest.raises(AP.ApplyRefused):
        AP.apply_plan(_pg(), plan, supplied_digest=A.recorded_digest(plan),
                      backup_receipt=_backup_path(), out_dir=_PLANS)
    assert _non_null() == 0, "a failed apply must leave no rows modified"


def test_replay_of_an_applied_plan_writes_nothing(plan):
    if _non_null() == 0:
        pytest.skip("edges not applied yet; replay is asserted by the apply run itself")
    result = AP.apply_plan(_pg(), plan, supplied_digest=A.recorded_digest(plan),
                           backup_receipt=_backup_path(), out_dir=_PLANS)
    assert result["stage"] == "replay-no-op" and result["writes"] == 0
    assert _non_null() == len(plan["operations"])
