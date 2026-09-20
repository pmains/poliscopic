"""Focused tests for the Brief 031D production-merge audit repairs.

Pure/stub level: no production access, no dumps, no scratch databases.
The inventory and advisory-lock integration tests that need a real PostgreSQL
cluster are marked ``postgres`` and skipped by default — they must be run in a
review window, not here.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import body_code_merge_prod as bp  # noqa: E402
import body_code_merge_runtime as rt  # noqa: E402
from kg import stage2_artifacts  # noqa: E402


# ── item 6: the meeting_events source-ID contract ────────────────────────

def test_event_contract_constants():
    assert rt.EVENT_TABLE == "meeting_events"
    assert rt.EVENT_MEETING_COLUMN == "meeting_id"


def test_no_code_path_rewrites_event_ids_to_a_database_pk():
    """The forbidden statement must not exist anywhere in the package."""
    for name in ("body_code_merge_runtime.py", "body_code_merge_prod.py",
                 "body_code_merge.py"):
        source = (rt.Path(__file__).resolve().parent.parent / "scripts" / name
                  ).read_text()
        assert "UPDATE meeting_events e SET meeting_id" not in source, name


def _event_snapshot(**over):
    base = {"present": True, "total": 100, "non_null": 100,
            "external_hits": 100, "pk_hits": 0}
    base.update(over)
    return base


def test_event_contract_accepts_an_unchanged_snapshot():
    rt.assert_event_contract(_event_snapshot(), _event_snapshot())


def test_event_contract_refuses_a_pk_rewrite():
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_event_contract(
            _event_snapshot(),
            _event_snapshot(external_hits=99, pk_hits=1))
    assert "rewritten onto database PKs" in str(excinfo.value)


def test_event_contract_refuses_a_row_loss():
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_event_contract(_event_snapshot(), _event_snapshot(total=99))
    assert "row count changed" in str(excinfo.value)


def test_event_contract_refuses_lost_external_matches():
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_event_contract(_event_snapshot(),
                                 _event_snapshot(external_hits=98))
    assert "external-id matches decreased" in str(excinfo.value)


def test_event_contract_refuses_an_equal_count_id_substitution():
    before = _event_snapshot(mapping_digest="a" * 64)
    after = _event_snapshot(mapping_digest="b" * 64)
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_event_contract(before, after)
    assert "mapping changed" in str(excinfo.value)


def test_event_contract_ignores_an_absent_table():
    rt.assert_event_contract({"present": False}, {"present": False})


# ── item 4: cardinality assertions ───────────────────────────────────────

def _merge(meetings=(), items=()):
    return {"old": "long-code", "new": "short-code",
            "meeting_map": list(meetings), "item_map": list(items)}


def test_cardinality_accepts_one_to_one_maps():
    rt.assert_meeting_item_cardinality([_merge(
        meetings=[{"old_id": 1, "new_id": 2, "meeting_id": "m"}],
        items=[{"old_id": 3, "new_id": 4}])])


def test_cardinality_refuses_duplicate_old_ids():
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_meeting_item_cardinality([_merge(
            items=[{"old_id": 1, "new_id": 1}, {"old_id": 1, "new_id": 2}])])
    assert "duplicate old_id" in str(excinfo.value)


def test_cardinality_refuses_many_to_one():
    """Two duplicate rows collapsing onto one survivor silently deletes data."""
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_meeting_item_cardinality([_merge(
            items=[{"old_id": 1, "new_id": 9}, {"old_id": 2, "new_id": 9}])])
    assert "not one-to-one" in str(excinfo.value)


# ── item 5: polymorphic reference handling ───────────────────────────────

def test_polymorphic_registry_declares_the_real_classes():
    declared = {(s["table"], s["type_column"]) for s in rt.POLYMORPHIC_REFERENCES}
    assert ("entity_mentions", "source_type") in declared
    assert ("entity_relationships", "provenance_type") in declared


def test_known_types_cover_the_observed_development_values():
    """Observed read-only in development; declared so unknown values refuse."""
    mentions = rt.KNOWN_POLYMORPHIC_TYPES[("entity_mentions", "source_type")]
    assert {"supporting_document", "agenda_item", "meeting_member",
            "pz_item_detail", "body_membership"} <= mentions
    rels = rt.KNOWN_POLYMORPHIC_TYPES[("entity_relationships", "provenance_type")]
    assert {"entity_resolution", "meeting_member", "agenda_item",
            "pz_item_detail", "meetings", "public_bodies",
            "body_membership"} <= rels


def test_registry_reparents_both_agenda_item_and_meeting_forms():
    targets = {}
    for spec in rt.POLYMORPHIC_REFERENCES:
        for value, table in spec["targets"].items():
            targets[(spec["table"], value)] = table
    assert targets[("entity_relationships", "agenda_item")] == "agenda_items"
    assert targets[("entity_relationships", "meetings")] == "meetings"
    assert targets[("entity_mentions", "agenda_item")] == "agenda_items"


# ── items 5/9: orphan coverage is registry-driven ────────────────────────

def test_every_declared_polymorphic_form_is_an_orphan_class():
    """The snapshot must be able to count every declared form."""
    import inspect
    source = inspect.getsource(rt.protected_snapshot)
    assert "POLYMORPHIC_REFERENCES" in source, (
        "orphan coverage must be driven by the registry, not hand-listed")


# ── item 1: location-only digest discipline ──────────────────────────────

def test_location_only_keys_exclude_the_baseline():
    assert set(rt.LOCATION_ONLY_KEYS) == {"tier", "target", "target_identity",
                                          "digest"}
    assert "baseline" not in rt.LOCATION_ONLY_KEYS


def test_content_digest_detects_baseline_drift():
    a = {"kind": "x", "merges": [], "baseline": {"counts": {"meetings": 1}}}
    b = {"kind": "x", "merges": [], "baseline": {"counts": {"meetings": 2}}}
    assert rt.content_digest(a) != rt.content_digest(b)


def test_content_digest_ignores_only_location_fields():
    a = {"kind": "x", "merges": [], "baseline": {}, "tier": "production",
         "target": "poliscopic", "target_identity": {"host": "a"}, "digest": "1"}
    b = {"kind": "x", "merges": [], "baseline": {}, "tier": "development",
         "target": "scratch", "target_identity": {"host": "b"}, "digest": "2"}
    assert rt.content_digest(a) == rt.content_digest(b)


# ── item 1: plan binding / freshness / artifact verification ─────────────

def _bound_plan(**over):
    plan = {"digest": "d", "plan_digest": "d" * 64,
            "code_hashes": rt.code_hashes(), "tier": "production",
            "target": "poliscopic", "target_identity": {
                "database": "poliscopic",
                "host": rt.PRODUCTION_TARGET["host"],
                "port": 5432, "server_version": "16.0",
                "dialect": "postgresql", "driver": "psycopg2",
                "cluster_identity": "",
            }}
    plan.update(over)
    return plan


def test_plan_binding_accepts_the_exact_plan():
    bp.assert_plan_binding(_bound_plan(), "d" * 64)


def test_plan_binding_refuses_a_different_digest():
    with pytest.raises(SystemExit):
        bp.assert_plan_binding(_bound_plan(), "other")


def test_plan_binding_refuses_code_drift():
    with pytest.raises(SystemExit) as excinfo:
        bp.assert_plan_binding(_bound_plan(code_hashes={"x": "y"}), "d" * 64)
    assert "different mutation code" in str(excinfo.value)


def test_plan_binding_refuses_a_non_production_plan():
    with pytest.raises(SystemExit):
        bp.assert_plan_binding(
            _bound_plan(tier="development", target="poliscopic_dev"), "d")


def test_freshness_accepts_a_recent_artifact():
    bp.assert_fresh({"created_at": datetime.now(timezone.utc).isoformat()},
                    label="plan")


def test_freshness_refuses_a_stale_artifact():
    old = (datetime.now(timezone.utc)
           - timedelta(hours=bp.MAX_ARTIFACT_AGE_HOURS + 1)).isoformat()
    with pytest.raises(SystemExit) as excinfo:
        bp.assert_fresh({"created_at": old}, label="plan")
    assert "old" in str(excinfo.value)


def test_freshness_refuses_a_missing_timestamp():
    with pytest.raises(SystemExit):
        bp.assert_fresh({}, label="plan")


def test_artifact_verification_refuses_a_missing_file(tmp_path):
    with pytest.raises(SystemExit):
        bp.verify_artifact(tmp_path / "nope.json", label="plan")


def test_artifact_verification_refuses_a_wrong_mode(tmp_path):
    path = tmp_path / "a.json"
    stage2_artifacts.write_immutable(path, {"kind": "test"})
    os.chmod(path, 0o644)
    with pytest.raises(SystemExit) as excinfo:
        bp.verify_artifact(path, label="plan")
    assert "0600" in str(excinfo.value)


def test_artifact_verification_refuses_a_digest_mismatch(tmp_path):
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"kind": "test", "digest": "0" * 64}))
    os.chmod(path, 0o600)
    with pytest.raises(SystemExit):
        bp.verify_artifact(path, label="plan")


def test_artifact_verification_accepts_a_verified_artifact(tmp_path):
    path = tmp_path / "c.json"
    stage2_artifacts.write_immutable(path, {"kind": "test", "created_at": "x"})
    loaded = bp.verify_artifact(path, label="plan")
    assert loaded["kind"] == "test"


# ── item 8: advisory locking ─────────────────────────────────────────────

class _StubResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _StubConnection:
    def __init__(self, acquired):
        self._acquired = acquired

    def execute(self, *args, **kwargs):
        return _StubResult(self._acquired)

    def close(self):
        pass


class _StubEngine:
    def __init__(self, acquired):
        self._acquired = acquired

    def connect(self):
        return _StubConnection(self._acquired)


def test_merge_lock_refuses_when_the_lock_is_already_held():
    with pytest.raises(RuntimeError) as excinfo:
        with rt.merge_lock(_StubEngine(False)):
            pass
    assert "advisory lock" in str(excinfo.value)


def test_merge_lock_uses_the_sync_lock_id():
    import inspect
    source = inspect.getsource(rt.merge_lock)
    assert "LOCK_ID" in source and "sync_declarations" in source
    assert "SERIALIZABLE" in source
