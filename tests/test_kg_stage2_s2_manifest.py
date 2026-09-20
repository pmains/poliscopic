#!/usr/bin/env python3
"""Stage 2 S2 — the derived plan index must never change an answer."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import pytest
from sqlalchemy import create_engine

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_apply as apply_mod  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402
from scripts.kg import stage2_s2_manifest as manifest_mod  # noqa: E402


def _write_plan(tmp_path, plan_id, supersedes=None, bump=0):
    """A structurally valid plan artifact, minimal but digest-clean."""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        from sqlalchemy import text
        connection.execute(text("CREATE TABLE agenda_items (id INTEGER PRIMARY KEY, "
                                "meeting_db_id INTEGER, agenda_item_number VARCHAR(32), "
                                "agenda_item_id VARCHAR(128))"))
        connection.execute(text("CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, "
                                "meeting_db_id INTEGER, agenda_item_id VARCHAR(256), "
                                "agenda_item_number VARCHAR(32), document_url VARCHAR(1024), "
                                "updated_at TIMESTAMP, body VARCHAR(256) DEFAULT '')"))
    hour = int(plan_id[9:11])
    plan = documents.build_plan(
        engine, {"integrity": {}}, plan_id=plan_id,
        created_at=f"2026-09-13T{hour:02d}:00:00+00:00", supersedes=supersedes)
    path = tmp_path / f"kg-stage2-s2-plan-{plan_id}.json"
    artifacts.write_immutable(path, plan)
    if bump:
        stamp = time.time() + bump
        os.utime(path, (stamp, stamp))
    return path, plan


def test_the_index_answers_the_same_question_as_a_scan(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    _write_plan(tmp_path, "20260913T010000Z",
                supersedes={"plan_id": "20260913T000000Z", "obsolete": True})
    found = manifest_mod.find_successors(tmp_path, "20260913T000000Z")
    assert found == manifest_mod.scan_successors(tmp_path, "20260913T000000Z")
    assert found == ["20260913T010000Z"]
    assert manifest_mod.find_successors(tmp_path, "20260913T010000Z") == []


def test_a_fresh_index_matches_the_scan_on_every_plan(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    _write_plan(tmp_path, "20260913T010000Z",
                supersedes={"plan_id": "20260913T000000Z", "obsolete": True})
    _write_plan(tmp_path, "20260913T020000Z",
                supersedes={"plan_id": "20260913T010000Z", "obsolete": True})
    assert manifest_mod.save_manifest(tmp_path, manifest_mod.build_manifest(tmp_path))
    for plan_id in ("20260913T000000Z", "20260913T010000Z", "20260913T020000Z",
                    "20260913T999999Z"):
        assert manifest_mod.find_successors(tmp_path, plan_id) == \
            manifest_mod.scan_successors(tmp_path, plan_id), plan_id


def test_a_newer_plan_makes_the_index_stale(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    manifest_mod.save_manifest(tmp_path, manifest_mod.build_manifest(tmp_path))
    assert manifest_mod.manifest_is_fresh(manifest_mod.load_manifest(tmp_path), tmp_path)
    _write_plan(tmp_path, "20260913T010000Z",
                supersedes={"plan_id": "20260913T000000Z", "obsolete": True}, bump=60)
    assert not manifest_mod.manifest_is_fresh(manifest_mod.load_manifest(tmp_path), tmp_path)
    # ...and the stale index still yields the authoritative answer
    assert manifest_mod.find_successors(tmp_path, "20260913T000000Z") == ["20260913T010000Z"]


def test_a_tampered_index_is_ignored(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    _write_plan(tmp_path, "20260913T010000Z",
                supersedes={"plan_id": "20260913T000000Z", "obsolete": True})
    manifest_mod.find_successors(tmp_path, "20260913T000000Z")   # builds the index
    path = tmp_path / manifest_mod.MANIFEST_NAME
    document = json.loads(path.read_text())
    document["plans"]["20260913T010000Z"]["supersedes_plan_id"] = None   # hide it
    path.write_text(json.dumps(document))
    assert not manifest_mod.manifest_is_fresh(document, tmp_path)
    # the scan still finds the successor, so hiding it in the index fails closed
    assert manifest_mod.find_successors(tmp_path, "20260913T000000Z") == ["20260913T010000Z"]


def test_a_corrupt_index_falls_back_to_a_scan(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    (tmp_path / manifest_mod.MANIFEST_NAME).write_text("{not json")
    assert manifest_mod.load_manifest(tmp_path) is None
    assert manifest_mod.manifest_is_fresh(None, tmp_path) is False
    assert manifest_mod.find_successors(tmp_path, "20260913T000000Z") == []


def test_a_missing_index_falls_back_to_a_scan(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    assert manifest_mod.load_manifest(tmp_path) is None
    assert manifest_mod.find_successors(tmp_path, "20260913T000000Z") == []
    assert (tmp_path / manifest_mod.MANIFEST_NAME).exists()   # scan refreshed it


def test_an_empty_directory_yields_nothing(tmp_path):
    assert manifest_mod.find_successors(tmp_path, "20260913T000000Z") == []
    assert manifest_mod.scan_successors(tmp_path, "20260913T000000Z") == []


def test_the_index_is_written_atomically_and_tightly(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    path = manifest_mod.save_manifest(tmp_path, manifest_mod.build_manifest(tmp_path))
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob(".*tmp*"))          # no partial file left behind
    assert manifest_mod.load_manifest(tmp_path)["version"] == manifest_mod.MANIFEST_VERSION


def test_the_index_is_a_cache_not_evidence(tmp_path):
    """Rebuilding replaces it; the plans themselves are never touched."""
    first = _write_plan(tmp_path, "20260913T000000Z")[0]
    before = first.read_bytes()
    manifest_mod.save_manifest(tmp_path, manifest_mod.build_manifest(tmp_path))
    rebuilt = manifest_mod.build_manifest(tmp_path)
    assert set(rebuilt["plans"]) == {"20260913T000000Z"}
    assert first.read_bytes() == before               # the artifact is untouched


def test_the_apply_uses_the_index_for_the_same_answer(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    _write_plan(tmp_path, "20260913T010000Z",
                supersedes={"plan_id": "20260913T000000Z", "obsolete": True})
    manifest_mod.save_manifest(tmp_path, manifest_mod.build_manifest(tmp_path))
    assert apply_mod.superseded_by(tmp_path, "20260913T000000Z") == ["20260913T010000Z"]
    assert apply_mod.superseded_by(tmp_path, "20260913T010000Z") == []


def test_the_tampered_index_cannot_hide_a_successor_from_the_apply(tmp_path):
    _write_plan(tmp_path, "20260913T000000Z")
    _write_plan(tmp_path, "20260913T010000Z",
                supersedes={"plan_id": "20260913T000000Z", "obsolete": True})
    manifest_mod.find_successors(tmp_path, "20260913T000000Z")
    path = tmp_path / manifest_mod.MANIFEST_NAME
    document = json.loads(path.read_text())
    document["plans"] = {}
    path.write_text(json.dumps(document))
    assert apply_mod.superseded_by(tmp_path, "20260913T000000Z") == ["20260913T010000Z"]
