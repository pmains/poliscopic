#!/usr/bin/env python3
"""Isolated tests for the DISABLED alias apply runner.

No test touches the development database.  The gate tests use engine URLs that are never
connected to; the transactional-core tests run against a throwaway file-backed SQLite fixture.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage3_b2_alias_apply as AP  # noqa: E402
from scripts.kg import stage3_b2_identity as ID  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _live(pattern, **need):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    for p in hits:
        d = A.load_verified(p)
        if all(d.get(k) == v for k, v in need.items()):
            return d
    if not hits:
        pytest.skip(f"no {pattern} artifact")
    return A.load_verified(hits[-1])


def _live_path(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    if not hits:
        pytest.skip(f"no {pattern} head")
    return hits[-1]


def _data_plan_path():
    return _live_path("kg-stage3-meeting-source-alias-plan-*.json")


def _schema_plan_path():
    return _live_path("kg-stage3-meeting-source-alias-schema-plan-*.json")


def _data_plan():
    return _live("kg-stage3-meeting-source-alias-plan-*.json")


def _schema_plan():
    return _live("kg-stage3-meeting-source-alias-schema-plan-*.json")


def _schema_bound_to(data_digest):
    s = _resign(copy.deepcopy(_schema_plan()))
    s["bindings"]["code_hashes"] = ID.code_hashes(
        tuple(s["bindings"]["code_hashes"]))
    s["bindings"]["data_plan_digest"] = data_digest
    return _resign(s)


def _current_plans():
    """Disposable current-code copies; recorded plans remain stale evidence."""
    data = copy.deepcopy(_data_plan())
    data["bindings"]["code_hashes"] = ID.code_hashes(
        tuple(data["bindings"]["code_hashes"]))
    _resign(data)
    schema = copy.deepcopy(_schema_plan())
    schema["bindings"]["code_hashes"] = ID.code_hashes(
        tuple(schema["bindings"]["code_hashes"]))
    schema["bindings"]["data_plan_digest"] = data["digest"]
    _resign(schema)
    return data, schema


def _resign(doc):
    doc.pop("digest", None)
    doc["digest"] = ID.canonical_sha256(doc)
    return doc


def _fake_pg(host="100.0.0.1", database="poliscopic_dev"):
    from sqlalchemy import create_engine
    return create_engine(f"postgresql://u:p@{host}:5432/{database}")


def _fixture_engine():
    """A throwaway file-backed SQLite fixture with the tables the runner counts."""
    from sqlalchemy import create_engine, text
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    handle.close()
    eng = create_engine(f"sqlite:///{handle.name}")
    with eng.begin() as c:
        for t in ("meetings", "agenda_items", "supporting_documents"):
            c.execute(text(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)"))
        c.execute(text("INSERT INTO meetings (id) VALUES (1),(2),(3)"))
    return eng


def _synthetic_plan(rows=2):
    ops = []
    for i in range(rows):
        ops.append({"source_meeting_db_id": 100 + i, "canonical_meeting_db_id": 200 + i,
                    "source_system": "phoenix_aem_publicmeetings_results",
                    "body": "phoenix-cc", "external_id": f"publicmeetings-results-x-{i}r",
                    "rule": ["item_number_set"], "evidence_sha256": "a" * 64,
                    "justification": "one-to-one: single evidenced source alias"})
    plan = {"kind": ID.DATA_KIND, "operations": ops, "operation_count": len(ops)}
    return plan


# --- disabled by design -----------------------------------------------------

def test_the_apply_is_disabled_by_design():
    assert AP.ENABLED is False
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.apply_aliases(_fake_pg(), data_plan=_data_plan(), schema_plan=_schema_plan(),
                         supplied_data_digest=_data_plan()["digest"],
                         supplied_schema_digest=_schema_plan()["digest"],
                         data_plan_path=_data_plan_path(),
                         schema_plan_path=_schema_plan_path(),
                         backup_receipt="nonexistent.json", out_dir="/tmp",
                         approver="Peter Mains", authorization=AP.AUTHORIZATION_TOKEN)
    assert "DISABLED" in str(exc.value)


def test_the_rollback_is_disabled_by_design():
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.rollback_aliases(_fake_pg(), out_dir="/tmp", approver="Peter Mains",
                            reason="test", authorization=AP.AUTHORIZATION_TOKEN)
    assert "DISABLED" in str(exc.value)


def test_replay_is_disabled_by_design():
    with pytest.raises(AP.ApplyRefused):
        AP.replay_aliases(_fake_pg(), data_plan=_data_plan())


# --- target gates -----------------------------------------------------------

def test_production_targets_are_refused_structurally():
    for host in ("db.ondigitalocean.com", "poliscopic.com"):
        with pytest.raises(AP.ApplyRefused) as exc:
            AP.check_target(_fake_pg(host=host), tier="development")
        assert "production" in str(exc.value)


def test_a_non_development_tier_is_refused():
    with pytest.raises(AP.ApplyRefused):
        AP.check_target(_fake_pg(), tier="production")


def test_a_non_development_database_is_refused():
    with pytest.raises(AP.ApplyRefused):
        AP.check_target(_fake_pg(database="poliscopic"), tier="development")


def test_a_non_postgres_dialect_is_refused():
    eng = _fixture_engine()
    with pytest.raises(AP.ApplyRefused):
        AP.check_target(eng, tier="development")


def test_a_development_target_passes_the_gate():
    t = AP.check_target(_fake_pg(), tier="development")
    assert t["database"] == "poliscopic_dev" and t["tier"] == "development"


# --- plan gates -------------------------------------------------------------

def test_tampered_digests_are_refused():
    d, s = _data_plan(), _schema_plan()
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.check_plan_bindings(d, s, supplied_data_digest="0" * 64,
                               supplied_schema_digest=s["digest"])
    assert "digest" in str(exc.value)


def test_writer_registry_drift_is_refused():
    d = _resign(copy.deepcopy(_data_plan()))
    d["producer"]["namespace_registry"] = "kg-stage3-writer-namespaces/0.9"
    _resign(d)
    s = _schema_bound_to(d["digest"])          # keep the binding gate satisfied
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.check_plan_bindings(d, s, supplied_data_digest=d["digest"],
                               supplied_schema_digest=s["digest"])
    assert "registry" in str(exc.value)


def test_producer_drift_is_refused():
    d = _resign(copy.deepcopy(_data_plan()))
    d["producer"]["version"] = "kg-stage3-b2-identity/1.0"
    _resign(d)
    s = _schema_bound_to(d["digest"])          # keep the binding gate satisfied
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.check_plan_bindings(d, s, supplied_data_digest=d["digest"],
                               supplied_schema_digest=s["digest"])
    assert "producer drift" in str(exc.value)


def test_a_schema_plan_that_does_not_bind_the_data_plan_is_refused():
    d = _data_plan()
    s = _resign(copy.deepcopy(_schema_plan()))
    s["bindings"]["data_plan_digest"] = "1" * 64
    _resign(s)
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.check_plan_bindings(d, s, supplied_data_digest=d["digest"],
                               supplied_schema_digest=s["digest"])
    assert "does not bind" in str(exc.value)


def test_a_plan_without_provenance_is_refused():
    d = _resign(copy.deepcopy(_data_plan()))
    d["operations"][0]["rule"] = None
    _resign(d)
    with pytest.raises(AP.ApplyRefused):
        AP.check_plan_bindings(d, _schema_plan(), supplied_data_digest=d["digest"],
                               supplied_schema_digest=_schema_plan()["digest"])


def test_a_mergeable_plan_is_refused():
    d, _ = _current_plans()
    d["no_merge"] = False
    _resign(d)
    s = _schema_bound_to(d["digest"])
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.check_plan_bindings(d, s, supplied_data_digest=d["digest"],
                               supplied_schema_digest=s["digest"])
    assert "merges" in str(exc.value)


def test_current_code_copies_pass_every_plan_gate():
    d, s = _current_plans()
    out = AP.check_plan_bindings(d, s, supplied_data_digest=d["digest"],
                                 supplied_schema_digest=s["digest"])
    assert out["operations"] == 10
    assert out["namespace_registry"] == ID.NAMESPACE_REGISTRY_VERSION


# --- backup receipt ---------------------------------------------------------

def test_a_missing_backup_receipt_is_refused(tmp_path):
    with pytest.raises(AP.ApplyRefused):
        AP.require_backup(tmp_path / "absent.json", target={"database": "poliscopic_dev"})


def test_a_world_readable_backup_receipt_is_refused(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"target": {"database": "poliscopic_dev"}}))
    p.chmod(0o644)
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.require_backup(p, target={"database": "poliscopic_dev"})
    assert "0o600" in str(exc.value)


def test_a_receipt_from_another_database_is_refused(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"target": {"database": "somewhere_else"},
                             "pg_restore": {"exit_code": 0, "evidence": "x"}}))
    p.chmod(0o600)
    with pytest.raises(AP.ApplyRefused):
        AP.require_backup(p, target={"database": "poliscopic_dev"})


# --- the PG DDL invariants the runner depends on ----------------------------

def test_the_pg_ddl_keeps_the_body_key_and_restrict_actions():
    sql = " ".join(_schema_plan()["ddl"])
    assert "UNIQUE (source_system, body, external_id)" in sql
    assert sql.count("ON DELETE RESTRICT") == 2
    assert "ON DELETE CASCADE" not in sql and "ON DELETE SET NULL" not in sql


def test_the_runner_never_issues_a_delete_or_update_against_source_tables():
    src = (_REPO / "scripts" / "kg" / "stage3_b2_alias_apply.py").read_text().upper()
    for forbidden in ("DELETE FROM MEETINGS", "DELETE FROM AGENDA_ITEMS",
                      "DELETE FROM SUPPORTING_DOCUMENTS", "UPDATE MEETINGS SET",
                      "TRUNCATE MEETINGS"):
        assert forbidden not in src
    # the only DROP is the additive alias table
    assert "DROP TABLE" in src
    assert "DROP TABLE MEETING_SOURCE_ALIASES" in src or \
        "DROP TABLE {ALIAS_TABLE}" in src


# --- isolated transactional core -------------------------------------------

def test_the_isolated_core_creates_the_table_inserts_and_holds_postconditions():
    eng = _fixture_engine()
    plan = _synthetic_plan(rows=2)
    with eng.begin() as c:
        before = AP.meetings_counts(c)
        result = AP._apply_in_transaction(c, plan, _schema_plan(), dialect="sqlite")
        assert result["schema_statements"] == 4
        assert result["writes"] == 2
        assert result["postconditions"]["aliases"] == 2
        assert result["postconditions"]["protected_unchanged"] is True
        assert AP.meetings_counts(c) == before


def test_a_second_core_run_fails_closed_because_the_table_exists():
    """Replay is a no-op only through the receipt-aware public path; the raw core must
    refuse rather than re-create or silently re-insert."""
    eng = _fixture_engine()
    plan = _synthetic_plan(rows=2)
    with eng.begin() as c:
        AP._apply_in_transaction(c, plan, _schema_plan(), dialect="sqlite")
    with pytest.raises(Exception) as exc:
        with eng.begin() as c:
            AP._apply_in_transaction(c, plan, _schema_plan(), dialect="sqlite")
    assert "already exists" in str(exc.value).lower() or "duplicate" in str(exc.value).lower()


def test_a_duplicate_identity_is_refused_by_the_unique_constraint():
    eng = _fixture_engine()
    plan = _synthetic_plan(rows=1)
    dup = copy.deepcopy(plan)
    dup["operations"].append(dict(plan["operations"][0], source_meeting_db_id=999))
    with eng.begin() as c:
        AP._apply_in_transaction(c, plan, _schema_plan(), dialect="sqlite")
    with pytest.raises(AP.ApplyRefused) as exc:
        with eng.begin() as c:
            AP.insert_aliases(c, dup, dialect="sqlite")
    assert "refused" in str(exc.value)


def test_the_isolated_rollback_drops_only_the_alias_table_and_preserves_sources():
    eng = _fixture_engine()
    plan = _synthetic_plan(rows=2)
    with eng.begin() as c:
        AP._apply_in_transaction(c, plan, _schema_plan(), dialect="sqlite")
        before = AP.meetings_counts(c)
    with eng.begin() as c:
        c.execute(__import__("sqlalchemy").text("DROP TABLE meeting_source_aliases"))
    with eng.begin() as c:
        from sqlalchemy import text as _t
        remaining = [r[0] for r in c.execute(_t(
            "SELECT name FROM sqlite_master WHERE type='table'"))]
    assert "meeting_source_aliases" not in remaining
    assert set(before) <= set(remaining), "source tables must survive a rollback"


def test_a_rollback_receipt_records_zero_source_rows_touched(tmp_path):
    out = AP.rollback_receipt(data_plan=_data_plan(), approver="Peter Mains",
                              out_dir=tmp_path, reason="test rollback")
    assert out["source_rows_touched"] == 0 and out["source_rows_deleted"] == 0
    assert out["statements"] == ["DROP TABLE meeting_source_aliases"]
    assert pathlib.Path(out["receipt_path"]).exists()
    assert oct(pathlib.Path(out["receipt_path"]).stat().st_mode)[-3:] == "600"


# --- code binding + plan-file gates (this turn) -----------------------------

def test_both_plans_bind_the_complete_safety_critical_code_set():
    d, s = _data_plan(), _schema_plan()
    for plan, label in ((d, "data"), (s, "schema")):
        bound = plan["bindings"]["code_hashes"]
        assert set(bound) == set(ID.CODE_MODULES), f"{label} binds the wrong module set"
        assert all(len(v) == 64 for v in bound.values())
    assert "scripts/kg/stage3_b2_alias_apply.py" in d["bindings"]["code_hashes"]
    assert "scripts/kg/stage3_b2_identity.py" in s["bindings"]["code_hashes"]


def test_stale_code_is_refused_by_both_validators():
    for plan, validator in ((_data_plan(), ID.validate_data_plan),
                            (_schema_plan(), ID.validate_schema_plan)):
        tampered = _resign(copy.deepcopy(plan))
        key = sorted(tampered["bindings"]["code_hashes"])[0]
        tampered["bindings"]["code_hashes"][key] = "0" * 64
        _resign(tampered)
        assert any("stale code" in p for p in validator(tampered))


def test_a_plan_without_code_bindings_is_refused():
    for plan, validator in ((_data_plan(), ID.validate_data_plan),
                            (_schema_plan(), ID.validate_schema_plan)):
        tampered = _resign(copy.deepcopy(plan))
        tampered["bindings"].pop("code_hashes")
        _resign(tampered)
        assert any("binds no implementation code hashes" in p for p in validator(tampered))


def test_stale_code_is_refused_by_the_apply_gate():
    d = _resign(copy.deepcopy(_data_plan()))
    d["bindings"]["code_hashes"][sorted(d["bindings"]["code_hashes"])[0]] = "0" * 64
    _resign(d)
    s = _schema_bound_to(d["digest"])
    with pytest.raises(AP.ApplyRefused) as exc:
        AP.check_plan_bindings(d, s, supplied_data_digest=d["digest"],
                               supplied_schema_digest=s["digest"])
    assert "stale-code" in str(exc.value) or "stale code" in str(exc.value)


def test_the_plan_file_gate_requires_the_exact_supplied_digests(tmp_path):
    d, s = _data_plan(), _schema_plan()
    # the right paths with the right digests pass
    ok = AP.check_plan_files(data_plan_path=_data_plan_path(),
                             schema_plan_path=_schema_plan_path(),
                             supplied_data_digest=d["digest"],
                             supplied_schema_digest=s["digest"])
    assert ok["data"]["digest"] == d["digest"]
    # a wrong digest for the named file is refused
    with pytest.raises(AP.ApplyRefused):
        AP.check_plan_files(data_plan_path=_data_plan_path(),
                            schema_plan_path=_schema_plan_path(),
                            supplied_data_digest="0" * 64,
                            supplied_schema_digest=s["digest"])
    # a missing path is refused
    with pytest.raises(AP.ApplyRefused):
        AP.check_plan_files(data_plan_path=tmp_path / "nope.json",
                            schema_plan_path=_schema_plan_path(),
                            supplied_data_digest=d["digest"],
                            supplied_schema_digest=s["digest"])


def test_a_superseded_plan_file_is_refused(tmp_path):
    """A plan carrying an obsolete marker must never be applied."""
    generic = tmp_path / "kg-stage3-meeting-source-alias-plan-20990101T000000Z.json"
    generic.write_text(_data_plan_path().read_text())
    try:
        (tmp_path / (generic.name + ".obsolete.json")).write_text("{}")
        with pytest.raises(AP.ApplyRefused) as exc:
            AP.check_plan_files(data_plan_path=generic,
                                schema_plan_path=_schema_plan_path(),
                                supplied_data_digest=_data_plan()["digest"],
                                supplied_schema_digest=_schema_plan()["digest"])
        assert "superseded" in str(exc.value)
    finally:
        (tmp_path / (generic.name + ".obsolete.json")).unlink(missing_ok=True)
        generic.unlink(missing_ok=True)


def test_the_runner_requires_a_named_human_approver():
    import inspect
    sig = inspect.signature(AP.apply_aliases)
    assert "approver" in sig.parameters
    assert sig.parameters["approver"].default is inspect.Parameter.empty
