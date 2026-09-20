#!/usr/bin/env python3
"""Adversarial tests for the Stage-2-aware fresh-backup verification path (v2)."""

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

from scripts.kg import stage2_backup_verify as V  # noqa: E402
from scripts.kg import stage1_backup_receipt as S1  # noqa: E402

_BACKUPS = _REPO / "data" / "backups"


def _baseline(*, counts=None, integrity=None, schema=None, created_at="2026-09-14T05:00:00+00:00",
              database="poliscopic_dev"):
    counts = counts if counts is not None else {"agenda_items": 10, "meetings": 2}
    body = {
        "kind": V.BASELINE_KIND, "version": "2.0", "created_at": created_at,
        "target": {"dialect": "postgresql", "host": "h", "port": 5432,
                   "database": database, "tier": "development"},
        "counts": counts, "counts_sha256": V.canonical_sha256(counts),
        "schema_signature": schema if schema is not None else {
            "schema_sha256": "a" * 64, "agenda_items": {"digest": "b" * 64}},
        "integrity": integrity if integrity is not None else {"orphan_extractions": 0},
    }
    return {**body, "digest": V.canonical_sha256(body)}


def _restored(baseline):
    return {"counts": dict(baseline["counts"]),
            "schema_signature": copy.deepcopy(baseline["schema_signature"]),
            "integrity": dict(baseline["integrity"]),
            "target": dict(baseline["target"])}


def _receipt(baseline, **over):
    receipt = V.build_receipt(
        baseline=baseline, baseline_path="data/backups/b.json",
        dump_path="data/backups/d.dump", dump_sha256="c" * 64,
        dump_started_at="2026-09-14T05:00:01+00:00",
        comparisons={"counts_match": True, "schema_match": True, "integrity_match": True,
                     "target_identity_match": True, "exact_restored_equality": True},
        problems=[], created_at="2026-09-14T05:01:00+00:00")
    receipt.update(over)
    return receipt


# --- baseline integrity -----------------------------------------------------

def test_a_well_formed_baseline_validates():
    assert V.validate_baseline(_baseline()) == []


def test_a_tampered_baseline_is_refused():
    b = _baseline()
    b["counts"] = dict(b["counts"], agenda_items=11)   # digest no longer matches
    problems = V.validate_baseline(b)
    assert any("digest" in p or "fingerprint" in p for p in problems)


def test_a_baseline_with_a_broken_counts_fingerprint_is_refused():
    b = _baseline()
    b["counts_sha256"] = "0" * 64
    b["digest"] = V.canonical_sha256({k: v for k, v in b.items() if k != "digest"})
    assert any("fingerprint" in p for p in V.validate_baseline(b))


def test_a_non_development_baseline_target_is_refused():
    assert any("development" in p for p in V.validate_baseline(_baseline(database="prod")))


def test_a_baseline_without_a_schema_signature_is_refused():
    b = _baseline(schema={"schema_sha256": "", "agenda_items": {}})
    problems = V.validate_baseline(b)
    assert any("signature" in p for p in problems)


def test_a_baseline_without_integrity_metrics_is_refused():
    b = _baseline(integrity={})
    assert any("integrity" in p for p in V.validate_baseline(b))


def test_an_unsupported_baseline_version_is_refused():
    b = _baseline()
    b["version"] = "1.0"
    b["digest"] = V.canonical_sha256({k: v for k, v in b.items() if k != "digest"})
    assert any("version" in p for p in V.validate_baseline(b))


# --- exact restored equality ------------------------------------------------

def test_an_identical_restore_matches_exactly():
    b = _baseline()
    assert V.compare_restore(b, _restored(b)) == []


def test_mismatched_restored_counts_are_refused():
    b = _baseline()
    r = _restored(b)
    r["counts"]["agenda_items"] = 999
    assert any("agenda_items" in p for p in V.compare_restore(b, r))


def test_unexpected_restored_counts_are_refused():
    b = _baseline()
    r = _restored(b)
    r["counts"]["surprise_table"] = 5
    assert any("unexpected" in p for p in V.compare_restore(b, r))


def test_mismatched_restored_schema_is_refused():
    b = _baseline()
    r = _restored(b)
    r["schema_signature"]["schema_sha256"] = "9" * 64
    assert any("schema signature" in p for p in V.compare_restore(b, r))


def test_mismatched_restored_agenda_items_signature_is_refused():
    b = _baseline()
    r = _restored(b)
    r["schema_signature"]["agenda_items"]["digest"] = "9" * 64
    assert any("agenda_items" in p for p in V.compare_restore(b, r))


def test_mismatched_restored_integrity_is_refused():
    b = _baseline(integrity={"orphan_extractions": 0})
    r = _restored(b)
    r["integrity"]["orphan_extractions"] = 7
    assert any("orphan_extractions" in p for p in V.compare_restore(b, r))


def test_mismatched_restored_target_is_refused():
    b = _baseline()
    r = _restored(b)
    r["target"]["dialect"] = "sqlite"
    assert any("dialect" in p for p in V.compare_restore(b, r))


# --- receipt construction and validation ------------------------------------

def test_a_receipt_is_built_only_after_every_comparison_passes():
    b = _baseline()
    with pytest.raises(ValueError):
        V.build_receipt(baseline=b, baseline_path="p", dump_path="d", dump_sha256="c" * 64,
                        dump_started_at="2026-09-14T05:00:01+00:00",
                        comparisons={"counts_match": False}, problems=["counts differ"],
                        created_at="2026-09-14T05:01:00+00:00")


def test_a_receipt_from_an_invalid_baseline_is_refused():
    b = _baseline()
    b["integrity"] = {}
    with pytest.raises(ValueError):
        V.build_receipt(baseline=b, baseline_path="p", dump_path="d", dump_sha256="c" * 64,
                        dump_started_at="2026-09-14T05:00:01+00:00",
                        comparisons={"counts_match": True}, problems=[],
                        created_at="2026-09-14T05:01:00+00:00")


def test_a_receipt_without_a_dump_digest_is_refused():
    with pytest.raises(ValueError):
        V.build_receipt(baseline=_baseline(), baseline_path="p", dump_path="d",
                        dump_sha256="", dump_started_at="2026-09-14T05:00:01+00:00",
                        comparisons={"counts_match": True}, problems=[],
                        created_at="2026-09-14T05:01:00+00:00")


def test_a_well_formed_receipt_validates_and_proves_restore():
    receipt = _receipt(_baseline())
    assert V.validate_stage2_receipt(receipt) == []
    assert S1.restore_verified(receipt) is True


def test_a_receipt_missing_the_binding_is_refused():
    receipt = _receipt(_baseline())
    receipt.pop("stage2_verification")
    assert any("stage2_verification" in p for p in V.validate_stage2_receipt(receipt))


def test_a_receipt_missing_the_baseline_digest_is_refused():
    receipt = _receipt(_baseline())
    receipt["stage2_verification"] = dict(receipt["stage2_verification"], baseline_digest="")
    assert any("baseline digest" in p for p in V.validate_stage2_receipt(receipt))


def test_a_receipt_asserting_a_failed_comparison_is_refused():
    receipt = _receipt(_baseline())
    receipt["stage2_verification"] = dict(
        receipt["stage2_verification"],
        comparisons=dict(receipt["stage2_verification"]["comparisons"], schema_match=False))
    assert any("failed comparisons" in p for p in V.validate_stage2_receipt(receipt))


def test_a_receipt_without_the_all_passed_assertion_is_refused():
    receipt = _receipt(_baseline())
    receipt["stage2_verification"] = dict(
        receipt["stage2_verification"], all_comparisons_passed=False)
    assert any("comparison" in p.lower() for p in V.validate_stage2_receipt(receipt))


def test_a_stale_baseline_captured_after_the_dump_is_refused():
    receipt = _receipt(_baseline())
    receipt["stage2_verification"] = dict(
        receipt["stage2_verification"],
        baseline_captured_at="2026-09-14T05:00:02+00:00",
        dump_started_at="2026-09-14T05:00:01+00:00")
    assert any("stale" in p for p in V.validate_stage2_receipt(receipt))


def test_a_receipt_without_a_clean_restore_is_refused():
    receipt = _receipt(_baseline())
    receipt["pg_restore"] = {"exit_code": 1, "evidence": ""}
    problems = V.validate_stage2_receipt(receipt)
    assert any("restore" in p for p in problems)
    assert S1.restore_verified(receipt) is False


# --- historical Stage-1 verifier behaviour is preserved ---------------------

def test_the_stage1_verifier_still_accepts_a_proven_receipt_and_rejects_an_unproven_one():
    proven = {"pg_restore": {"exit_code": 0, "evidence": "restored and compared"}}
    assert S1.restore_verified(proven) is True
    assert S1.restore_verified({"pg_restore": {"exit_code": 0, "evidence": ""}}) is False
    assert S1.restore_verified({"pg_restore": {"exit_code": 3, "evidence": "x"}}) is False
    assert S1.restore_verified({}) is False


def test_the_v2_module_never_reads_a_stage1_plan_baseline():
    """Coupling is checked against executable code, not the module docstring."""
    import ast
    source = (_REPO / "scripts" / "kg" / "stage2_backup_verify.py").read_text()
    tree = ast.parse(source)
    doc = ast.get_docstring(tree) or ""
    code = source.replace(doc, "")
    assert 'plan["baseline"]' not in code
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("stage1_fresh_backup" in m for m in imported)


# --- the real artifact, when present ---------------------------------------

def test_the_live_v2_receipt_validates_when_present():
    hits = sorted(_BACKUPS.glob("kg-stage2-backup-receipt-*.json"))
    if not hits:
        pytest.skip("no v2 backup receipt yet")
    receipt = json.loads(hits[-1].read_text())
    assert V.validate_stage2_receipt(receipt) == []
    assert S1.restore_verified(receipt) is True
    bpath = _BACKUPS / pathlib.Path(
        receipt["stage2_verification"]["baseline_path"]).name
    if not bpath.exists():
        pytest.skip("the bound baseline artifact is not alongside the receipt")
    baseline = json.loads(bpath.read_text())
    assert V.validate_baseline(baseline) == []
    assert baseline["digest"] == receipt["stage2_verification"]["baseline_digest"]
