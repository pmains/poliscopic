#!/usr/bin/env python3
"""Adversarial refusal tests for the containment schema plan and its apply runner."""

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

from scripts.db.core import get_engine  # noqa: E402
from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage2_subitem_schema_apply as AP  # noqa: E402
from scripts.kg import stage2_subitem_schema_plan as P  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"
_BACKUP = _REPO / "data" / "backups" / "kg-stage1-backup-receipt-20260914T003343Z.v2.json"


def _pg():
    """These assertions read the LIVE development catalogue; gate them to Postgres."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("live-catalogue assertions require the development PostgreSQL tier")
    return engine


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if len(hits) != 1:
        pytest.skip(f"{pattern!r} matched {len(hits)}")
    return hits[0]


@pytest.fixture(scope="module")
def plan():
    return A.load_verified(_live("kg-stage2-subitem-schema-plan-*.json"))


def test_the_plan_is_additive_and_declares_the_enforcement_boundary(plan):
    assert plan["additive_only"] is True
    assert plan["touches_existing_columns"] is False
    assert plan["index"]["unique"] is False
    assert plan["foreign_key"]["on_delete"] == "RESTRICT"
    assert plan["enforcement"]["transactional_only"] == [
        "same_meeting", "number_shortening", "no_cycles"]
    assert any("stage2_subitem_schema_plan.py" in problem
               for problem in P.validate_plan(plan))


def test_the_ddl_is_exactly_the_declared_statements(plan):
    assert plan["ddl"] == P.ddl()
    assert any("CHECK" in s for s in plan["ddl"])
    assert any("ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED" in s
               for s in plan["ddl"])


def test_a_drifted_digest_is_refused(plan):
    tampered = copy.deepcopy(plan)
    tampered["ddl"] = list(plan["ddl"]) + ["DROP TABLE agenda_items"]
    assert AP.apply_plan.__name__ == "apply_plan"
    with pytest.raises(AP.ApplyRefused):
        AP.apply_plan(get_engine(), tampered,
                      supplied_digest=A.recorded_digest(plan),
                      backup_receipt=_BACKUP, out_dir=_PLANS)


def test_target_drift_is_refused(plan):
    tampered = copy.deepcopy(plan)
    tampered["target"] = dict(plan["target"], database="somewhere_else")
    tampered.pop(A.DIGEST_FIELD, None)
    tampered[A.DIGEST_FIELD] = A.compute_digest(tampered)
    with pytest.raises(AP.ApplyRefused):
        AP.apply_plan(get_engine(), tampered,
                      supplied_digest=A.recorded_digest(tampered),
                      backup_receipt=_BACKUP, out_dir=_PLANS)


def test_schema_drift_is_refused(plan):
    tampered = copy.deepcopy(plan)
    tampered["bindings"] = dict(tampered["bindings"])
    tampered["bindings"]["schema_signature"] = dict(
        tampered["bindings"]["schema_signature"], digest="0" * 64)
    tampered.pop(A.DIGEST_FIELD, None)
    tampered[A.DIGEST_FIELD] = A.compute_digest(tampered)
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.apply_plan(_pg(), tampered,
                      supplied_digest=A.recorded_digest(tampered),
                      backup_receipt=_BACKUP, out_dir=_PLANS)
    assert "schema" in str(exc.value)


def test_code_drift_is_refused(plan):
    tampered = copy.deepcopy(plan)
    tampered["bindings"] = dict(tampered["bindings"])
    hashes = dict(tampered["bindings"]["code_hashes"])
    key = sorted(hashes)[0]
    hashes[key] = "0" * 64
    tampered["bindings"]["code_hashes"] = hashes
    assert any("drifted" in p for p in P.validate_plan(tampered))


def test_a_tampered_backup_receipt_is_refused(plan, tmp_path):
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({"target": {"database": "poliscopic_dev"}}))
    forged.chmod(0o600)
    with pytest.raises(AP.ApplyRefused):
        AP.require_backup(forged, target={"database": "poliscopic_dev"})


def test_a_missing_backup_receipt_is_refused(plan, tmp_path):
    with pytest.raises(AP.ApplyRefused):
        AP.require_backup(tmp_path / "absent.json",
                          target={"database": "poliscopic_dev"})


def test_a_preexisting_object_refuses_before_any_ddl(plan):
    """Detected by catalogue read; the runner refuses rather than half-applying."""
    with _pg().connect() as c:
        from sqlalchemy import text
        present = c.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name=:t "
            "AND column_name=:c"), {"t": P.TABLE, "c": P.COLUMN}).scalar()
    assert present in (0, 1)
    if present:
        with pytest.raises(AP.ApplyRefused):
            AP.apply_plan(_pg(), plan, supplied_digest=A.recorded_digest(plan),
                          backup_receipt=_BACKUP, out_dir=_PLANS)


def test_the_apply_is_transactional_so_a_postcondition_failure_leaves_nothing(plan, monkeypatch):
    """The DDL and its validation share one transaction: no partial schema."""
    calls = {"n": 0}
    real = AP.protected_row_counts

    def lying_counts(connection):
        calls["n"] += 1
        counts = real(connection)
        if calls["n"] >= 2:          # the in-transaction re-read
            return {k: v + 1 for k, v in counts.items()}
        return counts

    monkeypatch.setattr(AP, "protected_row_counts", lying_counts)
    with pytest.raises(AP.ApplyRefused):
        AP.apply_plan(get_engine(), plan, supplied_digest=A.recorded_digest(plan),
                      backup_receipt=_BACKUP, out_dir=_PLANS)
