"""Focused tests for the production-target-bound merge path (Brief 031 §C).

These are pure-function tests: no database, no network.  They pin the
fail-closed guards that stand between a reviewed plan and a production write.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import body_code_merge_runtime as rt  # noqa: E402
import body_code_merge_prod as prod  # noqa: E402
from body_code_merge_prod import (  # noqa: E402
    BASELINE_COUNTS,
    RECEIPT_KIND,
    assert_public_dump_toc,
    capture_counts,
    execution_plan,
    plan_artifact_binding,
    public_dump_command,
    require_backup,
)
from sqlalchemy import create_engine, text  # noqa: E402

PROD_HOST = rt.PRODUCTION_TARGET["host"]


def ident(database, host="10.0.0.1", port=5432, version="16.0"):
    return {"database": database, "host": host, "port": port,
            "server_version": version, "dialect": "postgresql",
            "driver": "psycopg", "cluster_identity": "cluster-1"}


# ── tier classification ──────────────────────────────────────────────────

def test_classify_identity_tiers():
    assert rt.classify_identity(ident("poliscopic")) == "production"
    assert rt.classify_identity(ident("poliscopic_dev")) == "development"
    assert rt.classify_identity(ident(rt.SCRATCH_PREFIX + "_prod")) == "development"
    assert rt.classify_identity(ident("some_other_db")) == "unknown"


# ── production guard ─────────────────────────────────────────────────────

def test_production_target_accepts_the_pinned_host():
    rt.assert_target_identity(ident("poliscopic", host=PROD_HOST),
                              rt.PRODUCTION_TARGET)


def test_production_target_refuses_a_development_database():
    with pytest.raises(RuntimeError):
        rt.assert_target_identity(ident("poliscopic_dev", host=PROD_HOST),
                                  rt.PRODUCTION_TARGET)


def test_production_target_refuses_a_same_named_database_on_another_host():
    """A database called `poliscopic` somewhere else must never be mutated."""
    with pytest.raises(RuntimeError):
        rt.assert_target_identity(ident("poliscopic", host="elsewhere.example"),
                                  rt.PRODUCTION_TARGET)


def test_production_target_refuses_scratch_database():
    with pytest.raises(RuntimeError):
        rt.assert_target_identity(ident(rt.SCRATCH_PREFIX + "_prod",
                                        host=PROD_HOST), rt.PRODUCTION_TARGET)


def test_production_engine_uses_the_authoritative_tier_resolver(monkeypatch):
    """URL validation is delegated before engine construction (no connection)."""
    import db.tier as tier

    raw = f"postgresql://role:secret@{PROD_HOST}:25060/poliscopic"
    seen = []
    monkeypatch.setenv("PROD_DATABASE_URL", raw)
    monkeypatch.setattr(tier, "resolve_role_url",
                        lambda role, url, *, label: seen.append((role, url, label)))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(prod, "create_engine", lambda url, **_: Engine())
    monkeypatch.setattr(prod, "assert_target", lambda *_: ident("poliscopic", PROD_HOST))
    prod.production_engine()
    assert seen == [(tier.PRODUCTION, raw, "body-code merge")]


# ── development guard (the other direction) ──────────────────────────────

def test_development_target_refuses_the_production_database():
    with pytest.raises(RuntimeError):
        rt.assert_target_identity(ident("poliscopic", host=PROD_HOST),
                                  rt.DEV_TARGET)


def test_development_target_refuses_the_production_host():
    with pytest.raises(RuntimeError):
        rt.assert_target_identity(ident("poliscopic_dev", host=PROD_HOST),
                                  rt.DEV_TARGET)


def test_development_target_allows_dev_and_scratch():
    rt.assert_target_identity(ident("poliscopic_dev"), rt.DEV_TARGET)
    scratch = {"tier": "development", "database": rt.SCRATCH_PREFIX + "_prod"}
    rt.assert_target_identity(ident(rt.SCRATCH_PREFIX + "_prod",
                                    host="127.0.0.1"), scratch)


def test_scratch_restore_drops_only_disposable_public_schema(monkeypatch):
    """The strict restore precondition is pinned to the named scratch DB."""
    commands = []
    monkeypatch.setattr(prod, "run", lambda args, **_: commands.append(args))
    prod.create_empty_scratch_database(15432)
    assert Path(commands[0][0]).name == "createdb"
    assert commands[0][-1] == prod.SCRATCH_DB
    assert commands[0][commands[0].index("-h") + 1] == "127.0.0.1"
    assert commands[0][commands[0].index("-p") + 1] == "15432"
    assert Path(commands[1][0]).name == "psql"
    assert commands[1][commands[1].index("-h") + 1] == "127.0.0.1"
    assert commands[1][commands[1].index("-p") + 1] == "15432"
    assert commands[1][commands[1].index("-d") + 1] == prod.SCRATCH_DB
    assert commands[1][-1] == "DROP SCHEMA IF EXISTS public CASCADE"


# ── content digest ───────────────────────────────────────────────────────

def _plan(**over):
    base = {"kind": "body-code-merge", "version": 4, "merges": [],
            "tier": "production", "target": "poliscopic",
            "target_identity": ident("poliscopic", host=PROD_HOST),
            "code_hashes": {"a": "b"}, "baseline": {"counts": {"meetings": 1}},
            "digest": "a" * 64}
    base.update(over)
    return base


def test_content_digest_ignores_target_binding():
    production = _plan(tier="production", target="poliscopic",
                       target_identity=ident("poliscopic", host=PROD_HOST),
                       digest="aaa")
    scratch = _plan(tier="development",
                    target=rt.SCRATCH_PREFIX + "_prod",
                    target_identity=ident(rt.SCRATCH_PREFIX + "_prod",
                                          host="127.0.0.1"),
                    digest="bbb")
    assert rt.content_digest(production) == rt.content_digest(scratch)


def test_content_digest_detects_merge_differences():
    assert rt.content_digest(_plan()) != rt.content_digest(
        _plan(merges=[{"old": "x", "new": "y"}]))


def test_content_digest_detects_baseline_differences():
    """The protected-table snapshot is part of the proof, not decoration."""
    assert rt.content_digest(_plan()) != rt.content_digest(
        _plan(baseline={"counts": {"meetings": 2}}))


def test_content_digest_detects_code_hash_differences():
    assert rt.content_digest(_plan()) != rt.content_digest(
        _plan(code_hashes={"a": "c"}))


# ── code binding ─────────────────────────────────────────────────────────

def test_code_hashes_binds_all_mutation_modules():
    hashes = rt.code_hashes()
    assert set(hashes) == set(rt.MUTATION_MODULES)
    assert all(len(v) == 64 for v in hashes.values())


def test_code_hashes_refuses_a_missing_module(monkeypatch):
    monkeypatch.setattr(rt, "MUTATION_MODULES",
                        ("body_code_merge_runtime.py", "does_not_exist.py"))
    with pytest.raises(RuntimeError):
        rt.code_hashes()


# ── backup receipt gating ────────────────────────────────────────────────

def _bound_plan(tmp_path, plan=None):
    """A reviewed immutable plan, dump and runbook proof for local tests."""
    from kg import stage2_artifacts

    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime = dict(plan or _plan())
    artifact_payload = dict(runtime)
    artifact_payload["plan_digest"] = artifact_payload.pop("digest")
    plan_path = tmp_path / "plan.json"
    stage2_artifacts.write_immutable(plan_path, artifact_payload)
    artifact = stage2_artifacts.load_verified(plan_path)
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"payload")
    os.chmod(dump, 0o600)
    dump_sha = hashlib.sha256(b"payload").hexdigest()
    runbook = tmp_path / "restore.md"
    runbook.write_text(
        f"dump {dump}\nsha {dump_sha}\nplan {runtime['digest']}\n")
    os.chmod(runbook, 0o600)
    return artifact, plan_path, dump, {
        "path": str(runbook.resolve()), "sha256": hashlib.sha256(runbook.read_bytes()).hexdigest(),
        "dump_sha256": dump_sha, "plan_digest": runtime["digest"], "present": True,
    }


def _write_receipt(tmp_path, bound, *, mode=0o600, **over):
    """Write a properly signed receipt artifact (Brief 031E item 9).

    Receipts are loaded through the verified immutable-artifact helper, so a
    test fixture that wrote raw JSON would no longer be a valid artifact.
    """
    from kg import stage2_artifacts

    artifact, plan_path, dump, runbook = bound
    plan = execution_plan(artifact)
    payload = {"kind": RECEIPT_KIND, "tier": "production",
               "target": plan["target_identity"],
               "comparisons": {"dump_restored": True},
               "problems": [], "dump_path": str(dump),
               "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
               "plan_content_digest": rt.content_digest(plan),
               "plan_artifact": plan_artifact_binding(plan_path, artifact),
               "restore_runbook": runbook}
    payload.update(over)
    path = tmp_path / "receipt.json"
    stage2_artifacts.write_immutable(path, payload)
    os.chmod(path, mode)
    return path


def test_immutable_plan_envelope_round_trips_the_runtime_plan(tmp_path):
    artifact, _, _, _ = _bound_plan(tmp_path)
    runtime = execution_plan(artifact)
    assert runtime["digest"] == "a" * 64
    assert "plan_digest" not in runtime
    assert artifact["digest"] != runtime["digest"]


def test_require_backup_refuses_a_tampered_receipt(tmp_path):
    """A receipt edited after signing must fail cryptographic verification."""
    bound = _bound_plan(tmp_path)
    artifact, plan_path, _, _ = bound
    path = _write_receipt(tmp_path, bound)
    document = json.loads(path.read_text())
    document["comparisons"] = {"dump_restored": False}  # tamper after signing
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)
    with pytest.raises(SystemExit):
        require_backup(path, artifact, plan_path)


def test_require_backup_refuses_a_non_0600_receipt(tmp_path):
    bound = _bound_plan(tmp_path)
    with pytest.raises(SystemExit):
        require_backup(_write_receipt(tmp_path, bound, mode=0o644), bound[0], bound[1])


def test_require_backup_refuses_recorded_problems(tmp_path):
    bound = _bound_plan(tmp_path)
    with pytest.raises(SystemExit):
        require_backup(_write_receipt(tmp_path, bound, problems=["boom"]), bound[0], bound[1])


def test_require_backup_refuses_a_failed_comparison(tmp_path):
    bound = _bound_plan(tmp_path)
    with pytest.raises(SystemExit):
        require_backup(
            _write_receipt(tmp_path, bound, comparisons={"dump_restored": False}),
            bound[0], bound[1])


def test_require_backup_refuses_empty_comparisons(tmp_path):
    bound = _bound_plan(tmp_path)
    with pytest.raises(SystemExit, match="comparisons"):
        require_backup(_write_receipt(tmp_path, bound, comparisons={}), bound[0], bound[1])


def test_require_backup_refuses_a_development_tier_receipt(tmp_path):
    bound = _bound_plan(tmp_path)
    with pytest.raises(SystemExit):
        require_backup(_write_receipt(tmp_path, bound, tier="development"), bound[0], bound[1])


def test_require_backup_refuses_a_foreign_host(tmp_path):
    bound = _bound_plan(tmp_path)
    with pytest.raises(SystemExit):
        require_backup(
            _write_receipt(tmp_path, bound,
                           target={**bound[0]["target_identity"], "host": "evil"}),
            bound[0], bound[1])


def test_require_backup_refuses_a_missing_dump(tmp_path):
    bound = _bound_plan(tmp_path)
    with pytest.raises(SystemExit):
        require_backup(_write_receipt(tmp_path, bound, dump_path=str(tmp_path / "nope.dump")),
                       bound[0], bound[1])


def test_require_backup_refuses_a_dump_digest_mismatch(tmp_path):
    bound = _bound_plan(tmp_path)
    with pytest.raises(SystemExit):
        require_backup(
            _write_receipt(tmp_path, bound, dump_sha256="1" * 64), bound[0], bound[1])


def test_require_backup_refuses_a_non_0600_dump(tmp_path):
    bound = _bound_plan(tmp_path)
    os.chmod(bound[2], 0o644)
    with pytest.raises(SystemExit, match="dump mode"):
        require_backup(_write_receipt(tmp_path, bound), bound[0], bound[1])


def test_require_backup_refuses_a_different_plan_content(tmp_path):
    bound = _bound_plan(tmp_path)
    good = _write_receipt(tmp_path, bound)
    other = _bound_plan(tmp_path / "other", _plan(merges=[{"old": "a", "new": "b"}]))
    # same receipt, different plan content -> must refuse
    with pytest.raises(SystemExit):
        require_backup(good, other[0], other[1])


def test_require_backup_accepts_a_fully_bound_receipt(tmp_path):
    bound = _bound_plan(tmp_path)
    path = _write_receipt(tmp_path, bound)
    result = require_backup(path, bound[0], bound[1])
    assert result["dump_sha256"] == hashlib.sha256(b"payload").hexdigest()


def test_require_backup_refuses_a_port_only_identity_change(tmp_path):
    bound = _bound_plan(tmp_path)
    changed = {**bound[0]["target_identity"], "port": 9999}
    path = _write_receipt(tmp_path, bound, target=changed)
    with pytest.raises(SystemExit, match="identity"):
        require_backup(path, bound[0], bound[1])


def test_require_backup_refuses_a_tampered_restore_runbook(tmp_path):
    bound = _bound_plan(tmp_path)
    path = _write_receipt(tmp_path, bound)
    Path(bound[3]["path"]).write_text("tampered")
    os.chmod(bound[3]["path"], 0o600)
    with pytest.raises(SystemExit, match="runbook"):
        require_backup(path, bound[0], bound[1])


def test_public_backup_command_excludes_the_dev_fdw_schema_and_toc_credentials(tmp_path):
    class Url:
        host, port, username, database = "db.example", 25060, "role", "poliscopic"

    command = public_dump_command(Url(), tmp_path / "public.dump")
    assert "--schema=public" in command
    assert "--exclude-schema=dev" in command
    assert "--no-owner" in command and "--no-privileges" in command
    assert_public_dump_toc(
        "; 1; 0 1 TABLE public meetings poliscopic\n"
        "; 2; 0 2 TABLE DATA public meetings poliscopic\n"
    )
    with pytest.raises(RuntimeError, match="FDW"):
        assert_public_dump_toc("; 2; 0 2 USER MAPPING - dev_fdw role\n")


# ── schema-conditional handling of the KG-era tables (Brief 031 §C) ─────

ABSENT = {"optional_tables": {"agenda_item_key_reservation": False,
                             "_pattern_cascade_watermark": False},
          "optional_columns": {"agenda_items.parent_item_id": False,
                               "supporting_documents.agenda_item_db_id": False}}
PRESENT = {"optional_tables": {"agenda_item_key_reservation": True,
                              "_pattern_cascade_watermark": True},
           "optional_columns": {"agenda_items.parent_item_id": True,
                                "supporting_documents.agenda_item_db_id": True}}


def test_optional_tables_are_the_kg_era_pair():
    assert rt.OPTIONAL_TABLES == ("agenda_item_key_reservation",
                                  "_pattern_cascade_watermark")
    # It sits inside PROTECTED_TABLES, which is exactly why filtering is needed.
    assert "agenda_item_key_reservation" in rt.PROTECTED_TABLES


def test_optional_columns_are_the_write_path_pair():
    assert rt.OPTIONAL_COLUMNS == (("agenda_items", "parent_item_id"),
                                   ("supporting_documents", "agenda_item_db_id"))
    # Both are reparented by the merge, so both must be capability-gated.
    for pair in rt.OPTIONAL_COLUMNS:
        assert pair in rt.ITEM_REFERENCE_COLUMNS


def test_has_reads_the_optional_flag():
    assert rt._has(PRESENT, "agenda_item_key_reservation") is True
    assert rt._has(ABSENT, "agenda_item_key_reservation") is False
    assert rt._has({}, "agenda_item_key_reservation") is False


def test_has_column_reads_the_optional_flag():
    assert rt._has_column(PRESENT, "agenda_items", "parent_item_id") is True
    assert rt._has_column(ABSENT, "agenda_items", "parent_item_id") is False
    # unknown columns default to present, so always-present columns are untouched
    assert rt._has_column(ABSENT, "agenda_items", "source_url") is True
    assert rt._has_column({}, "agenda_items", "parent_item_id") is True


def test_assert_capabilities_refuses_drift(monkeypatch):
    monkeypatch.setattr(rt, "schema_capabilities", lambda connection: PRESENT)
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_capabilities(None, {"capabilities": ABSENT})
    assert "schema drift" in str(excinfo.value)


def test_assert_capabilities_refuses_column_drift(monkeypatch):
    """A column appearing/vanishing between plan and apply must refuse."""
    drifted = {"optional_tables": PRESENT["optional_tables"],
               "optional_columns": {"agenda_items.parent_item_id": False,
                                    "supporting_documents.agenda_item_db_id": True}}
    monkeypatch.setattr(rt, "schema_capabilities", lambda connection: drifted)
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_capabilities(None, {"capabilities": PRESENT})
    assert "schema drift" in str(excinfo.value)


def test_assert_capabilities_refuses_an_unbound_plan():
    with pytest.raises(RuntimeError) as excinfo:
        rt.assert_capabilities(None, {})
    assert "no schema capabilities" in str(excinfo.value)


def test_assert_capabilities_accepts_a_match(monkeypatch):
    monkeypatch.setattr(rt, "schema_capabilities", lambda connection: ABSENT)
    assert rt.assert_capabilities(None, {"capabilities": ABSENT}) == ABSENT


def test_content_digest_includes_schema_capabilities():
    """Presence/absence of the KG-era schema is part of the proof content."""
    assert rt.content_digest(_plan()) != rt.content_digest(
        _plan(capabilities=ABSENT))


def test_content_digest_includes_column_capabilities():
    only_columns_differ = {"optional_tables": PRESENT["optional_tables"],
                           "optional_columns": {**PRESENT["optional_columns"],
                                                "agenda_items.parent_item_id": False}}
    assert rt.content_digest(_plan(capabilities=PRESENT)) != rt.content_digest(
        _plan(capabilities=only_columns_differ))


def _sqlite(tmp_path, *, with_reservation: bool, with_columns: bool):
    engine = create_engine(f"sqlite:///{tmp_path / 'counts.sqlite'}")
    parent = ", parent_item_id integer" if with_columns else ""
    linked = ", agenda_item_db_id integer" if with_columns else ""
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE meetings (id integer primary key)"))
        connection.execute(text(
            "CREATE TABLE public_bodies (id integer primary key)"))
        connection.execute(text(
            f"CREATE TABLE supporting_documents (id integer primary key{linked})"))
        connection.execute(text(
            f"CREATE TABLE agenda_items (id integer primary key{parent})"))
        connection.execute(text(
            "CREATE TABLE meeting_events (id integer primary key)"))
        if with_reservation:
            connection.execute(text(
                "CREATE TABLE agenda_item_key_reservation "
                "(id integer primary key)"))
    return engine


def _expected_keys(capabilities):
    tables = capabilities["optional_tables"]
    columns = capabilities["optional_columns"]
    out = set()
    for key, table, column, _ in BASELINE_COUNTS:
        if table in rt.OPTIONAL_TABLES and not tables.get(table):
            continue
        if column and not columns.get(f"{table}.{column}", True):
            continue
        out.add(key)
    return out


def test_capture_counts_on_a_production_shaped_schema(tmp_path):
    """No reservation table, no optional columns — the production shape."""
    engine = _sqlite(tmp_path, with_reservation=False, with_columns=False)
    counts = capture_counts(engine, ABSENT)
    assert "agenda_item_key_reservation" not in counts
    assert "agenda_items_with_parent" not in counts
    assert "supporting_documents_linked" not in counts
    assert set(counts) == _expected_keys(ABSENT)


def test_capture_counts_on_a_development_shaped_schema(tmp_path):
    """Everything present — the development shape."""
    engine = _sqlite(tmp_path, with_reservation=True, with_columns=True)
    counts = capture_counts(engine, PRESENT)
    assert counts["agenda_item_key_reservation"] == 0
    assert counts["agenda_items_with_parent"] == 0
    assert counts["supporting_documents_linked"] == 0
    assert set(counts) == _expected_keys(PRESENT)


def test_capture_counts_handles_absent_columns_on_present_tables(tmp_path):
    """The exact production case: tables exist, two columns do not."""
    engine = _sqlite(tmp_path, with_reservation=True, with_columns=False)
    counts = capture_counts(engine, ABSENT)
    assert "agenda_items_with_parent" not in counts
    assert "supporting_documents_linked" not in counts
    assert "meetings" in counts
