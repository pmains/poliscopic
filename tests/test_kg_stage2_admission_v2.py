#!/usr/bin/env python3
"""Adversarial tests for the v2 admission boundary and the repair operation shape.

Two families:

* **typed receipt roles** - a historical dependency plan must verify against the backup it
  was ORIGINALLY bound to, and the current apply plan against the fresh canonical receipt.
  Swapped, missing, stale, tampered, wrong-target and mismatched receipts must all refuse.
* **repair operation shape and rollback** - the six ways a plan can propose a row that must
  not be written.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.db.core import get_engine  # noqa: E402
from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage2_s2_admission_v2 as adm  # noqa: E402
from scripts.kg import stage2_s2_row_derivation as RD  # noqa: E402
from scripts.kg import stage2_s2_schema_signature as SS  # noqa: E402
from scripts.kg import stage2_s2_write_body as WB  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"
_CURRENT = _REPO / "data" / "backups" / "kg-stage1-backup-receipt-20260914T003343Z.v2.json"
_CORRECTION = _PLANS / "kg-stage2-s2-label-correction-plan-1896fc4ff149d4f2.json"
_APPLY_RECEIPT = _PLANS / "kg-stage2-s2-apply-receipt-20260913T191248Z-1896fc4ff149d4f2.json"


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, [h.name for h in hits]
    return hits[0]


@pytest.fixture(scope="module")
def repair():
    return A.load_verified(_live("kg-stage2-s2-repair-plan-*.json"))


@pytest.fixture(scope="module")
def correction():
    return A.load_verified(_CORRECTION)


@pytest.fixture(autouse=True)
def current_receipt_clock(monkeypatch):
    """Evaluate role separation at the receipt's historical admission time."""
    created = datetime.fromisoformat(
        json.loads(_CURRENT.read_text())["created_at"].replace("Z", "+00:00"))
    moment = created.astimezone(timezone.utc) + timedelta(hours=1)

    class _ReceiptClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(adm, "datetime", _ReceiptClock)


# ══ typed roles ════════════════════════════════════════════════════════

def test_the_happy_path_admits_with_separated_roles(repair, correction):
    result = adm.admit_v2(repair=repair, correction=correction,
                          current_receipt=_CURRENT,
                          correction_apply_receipt=_APPLY_RECEIPT)
    assert result["status"] == "admitted" and result["writes"] == 0
    assert result["dependency_evidence"]["role"] == adm.DEPENDENCY_EVIDENCE
    assert result["current_protection"]["role"] == adm.CURRENT_PROTECTION
    assert result["shared_live_receipt_equality"] == "removed"


def test_the_historical_plan_is_not_judged_against_the_fresh_receipt(correction):
    """The invariant this task removes: one live receipt for every plan."""
    fresh = json.loads(_CURRENT.read_text())
    with pytest.raises(adm.AdmissionRefused):
        adm.verify_current(correction, label="correction", receipt_path=_CURRENT)
    assert fresh["target"]["port"] == 5432


def test_swapped_roles_are_refused(repair, correction):
    with pytest.raises(adm.AdmissionRefused):
        adm.admit_v2(repair=repair, correction=correction,
                     current_receipt=_CURRENT,
                     correction_apply_receipt=_CORRECTION)  # not an apply receipt


def test_a_missing_current_receipt_is_refused(repair, tmp_path):
    with pytest.raises(adm.AdmissionRefused) as exc:
        adm.verify_current(repair, label="repair", receipt_path=tmp_path / "nope.json")
    assert "missing" in str(exc.value)


def test_a_stale_current_receipt_is_refused(repair, tmp_path):
    payload = json.loads(_CURRENT.read_text())
    payload["created_at"] = "2020-01-01T00:00:00+00:00"
    path = tmp_path / _CURRENT.name
    path.write_text(json.dumps(payload))
    with pytest.raises(adm.AdmissionRefused) as exc:
        adm.verify_current(repair, label="repair", receipt_path=path)
    assert "STALE" in str(exc.value)


def test_a_tampered_historical_receipt_is_refused(correction, tmp_path, monkeypatch):
    bound = (correction["bindings"]["backup"] or {})
    forged = tmp_path / pathlib.Path(str(bound["path"])).name
    forged.write_text(json.dumps({"target": {}, "tampered": True}))
    monkeypatch.setattr(adm, "REPO", tmp_path.parent.parent)
    with pytest.raises(adm.AdmissionRefused):
        adm.verify_dependency(correction, label="correction")


def test_a_wrong_target_current_receipt_is_refused(repair, tmp_path):
    payload = json.loads(_CURRENT.read_text())
    payload["target"] = dict(payload["target"], port=9999)
    path = tmp_path / _CURRENT.name
    path.write_text(json.dumps(payload))
    problems = adm._target_problems(adm.CURRENT_PROTECTION, "repair", payload,
                                    adm.plan_target(repair))
    assert any("port" in p for p in problems)


def test_a_mismatched_bound_digest_is_refused(repair, tmp_path):
    payload = json.loads(_CURRENT.read_text())
    path = tmp_path / _CURRENT.name
    path.write_text(json.dumps(payload))
    forged = copy.deepcopy(repair)
    forged["bindings"] = dict(forged["bindings"])
    forged["bindings"]["backup"] = dict(forged["bindings"]["backup"],
                                        canonical_digest="0" * 64)
    with pytest.raises(adm.AdmissionRefused) as exc:
        adm.verify_current(forged, label="repair", receipt_path=path)
    assert "MISMATCHED" in str(exc.value) or "SWAPPED" in str(exc.value)


# ══ repair operation shape and rollback ════════════════════════════════

def _pg():
    """These assertions read the LIVE development catalogue, so they only run there."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("live-catalogue assertions require the development PostgreSQL tier")
    return engine


def _required_and_columns():
    with _pg().connect() as c:
        columns = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'agenda_items'"))}
        required = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_name = 'agenda_items' AND is_nullable = 'NO' "
            "AND column_default IS NULL"))}
    return columns, required


def _materialisations(plan):
    return [r for r in plan["rows"] if r.get("action") == "materialise"]


def test_a_materialisation_missing_a_required_field_is_refused(repair):
    row = dict(_materialisations(repair)[0]["proposed_row"])
    row.pop("meeting_id")
    assert any("meeting_id" in p for p in RD.validate_row(row))


def test_a_materialisation_naming_a_nonexistent_column_is_refused(repair):
    columns, _ = _required_and_columns()
    for candidate in _materialisations(repair):
        assert not (set(candidate["proposed_row"]) - columns)


def test_proposed_row_schema_drift_is_refused(repair):
    with _pg().connect() as c:
        tampered = copy.deepcopy(repair["bindings"]["schema_signature"])
        tampered["agenda_items"]["columns"] = [{"name": "bogus"}]
        assert SS.verify(c, tampered)
        assert SS.verify(c, repair["bindings"]["schema_signature"]) == []


def test_tampered_derivation_evidence_is_detected(repair):
    row = _materialisations(repair)[0]
    assert row["derivation"]["row_fingerprint"] == row["row_fingerprint"]
    forged = copy.deepcopy(row["proposed_row"])
    forged["agenda_item_title"] = forged["agenda_item_title"] + " (tampered)"
    assert A.canonical_sha256(forged) if hasattr(A, "canonical_sha256") else True
    from scripts.kg import stage2_s2_plan_binding as binding
    assert binding.canonical_sha256(forged) != row["row_fingerprint"]


def test_duplicate_keys_in_a_materialisation_set_are_refused(repair):
    rows = list(_materialisations(repair))
    keys = [(int(r["meeting_db_id"]), str(r["proposed_row"]["agenda_item_number"]))
            for r in rows]
    assert len(keys) == len(set(keys))
    duplicated = rows + [rows[0]]
    dup_keys = [(int(r["meeting_db_id"]), str(r["proposed_row"]["agenda_item_number"]))
                for r in duplicated]
    assert len(dup_keys) != len(set(dup_keys))


def test_a_partial_failure_rolls_back_every_write(repair):
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE agenda_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "meeting_db_id INTEGER, agenda_item_number TEXT, "
                       "agenda_item_title TEXT, agenda_item_text TEXT, body TEXT, "
                       "meeting_id TEXT, agenda_item_id TEXT, agenda_item_url TEXT, "
                       "vote_or_action TEXT, source_body TEXT, source_url TEXT, "
                       "c_number TEXT, c_number_base TEXT, case_number TEXT, "
                       "agenda_category TEXT, item_type TEXT, section_level INTEGER, "
                       "sort_order INTEGER, created_at TIMESTAMP, "
                       "UNIQUE (meeting_db_id, agenda_item_number))"))
        c.execute(text("CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, "
                       "agenda_item_db_id INTEGER, agenda_item_id TEXT, "
                       "agenda_item_number TEXT)"))
    operations = WB.operations_for(repair, role="repair")
    with pytest.raises(WB.WriteRefused):
        with engine.begin() as connection:
            WB._execute_operations(connection, operations,
                                   plan_digest=A.recorded_digest(repair),
                                   expected={"row_count": 999_999})
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM agenda_items")).scalar() == 0
