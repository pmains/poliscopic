"""Isolated tests for Stage 2 parentage schema readiness (planner and runner).

Every test runs against in-memory SQLite.  PostgreSQL-only DDL is never faked:
where a plan contains PostgreSQL-specific operations the mechanics are exercised
by their refusal and rollback behaviour rather than by pretending SQLite supports
``ALTER TABLE ... ADD CONSTRAINT``.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, event, text

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.db import tier as tier_module  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_schema_plan as schema_plan  # noqa: E402
from scripts.kg import stage2_schema_readiness as sr  # noqa: E402
from scripts.kg import stage2_schema_runner as runner  # noqa: E402

#: The tables a protected backup receipt must reproduce counts for.
GATE_TABLES = ("entities", "entity_mentions", "entity_relationships",
               "event_participants", "meeting_event_extractions", "meeting_events")

GATE_DDL = tuple(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)" for t in GATE_TABLES)

PARENTS = (
    "CREATE TABLE jurisdictions (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, name TEXT)",
)

MISSING = ("CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT)",)

READY = (
    "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
    " public_body_id INTEGER REFERENCES public_bodies(id),"
    " jurisdiction_id INTEGER REFERENCES jurisdictions(id))",
    "CREATE INDEX ix_meetings_public_body_id ON meetings(public_body_id)",
    "CREATE INDEX ix_meetings_jurisdiction_id ON meetings(jurisdiction_id)",
)

NO_FK = (
    "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
    " public_body_id INTEGER, jurisdiction_id INTEGER)",
    "CREATE INDEX ix_meetings_public_body_id ON meetings(public_body_id)",
    "CREATE INDEX ix_meetings_jurisdiction_id ON meetings(jurisdiction_id)",
)


def _make_engine(url: str = "sqlite://", *, transactional_ddl: bool = False):
    """An isolated SQLite engine.

    ``transactional_ddl`` applies SQLAlchemy's documented pysqlite recipe
    (``isolation_level = None`` plus an explicit ``BEGIN``).  Without it the
    driver commits implicitly before DDL, so a rollback test would be measuring
    the driver rather than the runner.
    """
    engine = create_engine(url)
    if transactional_ddl:
        @event.listens_for(engine, "connect")
        def _no_implicit_begin(dbapi_connection, _record):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def _explicit_begin(connection):
            connection.exec_driver_sql("BEGIN")
    return engine


def _engine(*statements: str, transactional_ddl: bool = False):
    engine = _make_engine(transactional_ddl=transactional_ddl)
    with engine.begin() as conn:
        for statement in (*GATE_DDL, *statements):
            conn.execute(text(statement))
    return engine


def _target_for(engine) -> tier_module.Target:
    """The target identity a plan must record to match this engine."""
    parts = urlsplit(str(engine.url))
    return tier_module.Target(
        url_class="development",
        dialect=engine.dialect.name,
        host=parts.hostname,
        port=parts.port,
        database=(parts.path or "").lstrip("/"),
    )


def _ready_engine():
    return _engine(*PARENTS, *READY)


def _plan(engine):
    return schema_plan.build_plan(engine, _target_for(engine))


def _receipt(tmp_path, counts=None, *, protected=True, name="backup.json"):
    counts = dict(counts) if counts is not None else {t: 0 for t in GATE_TABLES}
    payload = {
        "dump_path": "/protected/dev.dump",
        "dump_sha256": "a" * 64,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": {"tier": "development", "host": "192.0.2.10", "database": "poliscopic_dev"},
        "scratch": {"host": "localhost", "database": "poliscopic_restore_scratch"},
        "pg_restore": {"exit_code": 0, "evidence": "restored 4/4"},
        "counts": counts,
        "signatures": {"schema_sha256": "b" * 64,
                       "counts_sha256": receipts.counts_fingerprint(counts)},
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    os.chmod(path, 0o600 if protected else 0o644)
    return path


def _apply(engine, plan, tmp_path, receipt=None, **kw):
    return runner.apply_plan(
        engine, plan,
        supplied_digest=artifacts.compute_digest(plan),
        backup_receipt=receipt or _receipt(tmp_path),
        out_dir=tmp_path, allow_unsupported_dialect=True, **kw,
    )


def _refusal_receipts(tmp_path) -> list[str]:
    return sorted(p.name for p in pathlib.Path(tmp_path).glob("*-refused.json"))


def _failure_receipts(tmp_path) -> list[str]:
    return sorted(p.name for p in pathlib.Path(tmp_path).glob("*-failure.json"))


def _schema_snapshot(engine) -> str:
    return sr.signature_digest(sr.observe(engine))


# ── observation and planning ────────────────────────────────────────────


def test_missing_columns_are_detected_and_planned():
    observed = sr.observe(_engine(*PARENTS, *MISSING))
    assert observed["columns"]["public_body_id"]["present"] is False
    kinds = [op["kind"] for op in sr.operations_for(observed)]
    assert kinds.count("add_column") == 2
    assert sr.is_ready(observed) is False


def test_existing_columns_with_missing_constraints_are_planned():
    observed = sr.observe(_engine(*PARENTS, *NO_FK))
    for column in ("public_body_id", "jurisdiction_id"):
        assert observed["columns"][column]["present"] is True
        assert observed["foreign_keys"][column]["exists"] is False
    kinds = [op["kind"] for op in sr.operations_for(observed)]
    assert kinds.count("add_fk_not_valid") == 2
    assert kinds.count("validate_fk") == 2


def test_safe_constraint_installation_is_not_valid_then_validate():
    """The two-step matches the existing authority, kg_integrity_schema.py."""
    observed = sr.observe(_engine(*PARENTS, *NO_FK))
    ddl = [op["sql"] for op in sr.operations_for(observed)]
    assert any("NOT VALID" in sql for sql in ddl)
    assert any(sql.startswith("ALTER TABLE meetings VALIDATE CONSTRAINT") for sql in ddl)
    added = next(i for i, s in enumerate(ddl) if "NOT VALID" in s)
    validated = next(i for i, s in enumerate(ddl) if "VALIDATE CONSTRAINT" in s)
    assert added < validated


def test_dangling_values_block_validation():
    engine = _engine(*PARENTS, *NO_FK)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO meetings VALUES (1,'x',999,NULL)"))
    observed = sr.observe(engine)
    assert observed["dangling"]["public_body_id"] == 1
    assert any("dangling" in p for p in sr.blocking_problems(observed))
    operations = sr.operations_for(observed)
    validated = [op["target"] for op in operations if op["kind"] == "validate_fk"]
    assert "meetings.public_body_id" not in validated
    assert "meetings.jurisdiction_id" in validated
    assert sr.is_ready(observed) is False


def test_type_drift_blocks_rather_than_retyping():
    engine = _engine(*PARENTS,
                     "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
                     " public_body_id TEXT, jurisdiction_id INTEGER)")
    assert any("public_body_id is TEXT" in p for p in sr.blocking_problems(sr.observe(engine)))


def test_nullability_drift_blocks():
    engine = _engine(*PARENTS,
                     "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
                     " public_body_id INTEGER NOT NULL, jurisdiction_id INTEGER)")
    assert any("nullability" in p for p in sr.blocking_problems(sr.observe(engine)))


def test_index_state_is_detected_and_shape_matched():
    observed = sr.observe(_engine(*PARENTS, *READY[:1]))
    assert observed["indexes"]["public_body_id"]["satisfied"] is False
    assert any(op["kind"] == "create_index" for op in sr.operations_for(observed))


def test_index_name_drift_is_satisfied_by_shape():
    engine = _engine(*PARENTS, *READY[:1],
                     "CREATE INDEX some_other_name ON meetings(public_body_id)",
                     "CREATE INDEX ix_meetings_jurisdiction_id ON meetings(jurisdiction_id)")
    observed = sr.observe(engine)
    assert observed["indexes"]["public_body_id"]["satisfied"] is True
    assert observed["indexes"]["public_body_id"]["name_matches_preferred"] is False


def test_redundant_indexes_are_reported_not_removed():
    engine = _engine(*PARENTS, *READY,
                     "CREATE INDEX ix_meetings_public_body_id_dupe ON meetings(public_body_id)")
    observed = sr.observe(engine)
    assert "public_body_id" in observed["redundant_indexes"]
    assert not any(op["kind"] == "drop_index" for op in sr.operations_for(observed))


def test_plan_for_a_ready_schema_is_idempotent():
    plan = _plan(_ready_engine())
    assert plan["operations"] == []
    assert plan["ready"] is True
    assert plan["idempotent"] is True


def test_repeated_planning_yields_the_same_signature():
    assert _plan(_ready_engine())["signature_digest"] == _plan(_ready_engine())["signature_digest"]


def test_wrong_fk_target_is_not_satisfied_by_shape():
    engine = _engine(
        "CREATE TABLE other_parent (id INTEGER PRIMARY KEY)",
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
        " public_body_id INTEGER REFERENCES other_parent(id), jurisdiction_id INTEGER)",
    )
    assert sr.observe(engine)["foreign_keys"]["public_body_id"]["exists"] is False


def test_plan_binds_the_target_identity_and_baseline_counts():
    plan = _plan(_ready_engine())
    for field in ("dialect", "host", "database"):
        assert field in plan["target"]
    baseline = plan["baseline"]
    assert sorted(baseline["counts"]) == sorted(GATE_TABLES)
    assert baseline["counts_fingerprint"] == receipts.counts_fingerprint(baseline["counts"])


# ── refusal matrix: every preflight check, before any DDL ───────────────


def test_apply_refuses_a_digest_that_does_not_match(tmp_path):
    plan = _plan(_ready_engine())
    with pytest.raises(runner.SchemaRefused):
        runner.apply_plan(_ready_engine(), plan, supplied_digest="f" * 64,
                          backup_receipt=_receipt(tmp_path), out_dir=tmp_path,
                          allow_unsupported_dialect=True)


def test_apply_refuses_an_unsupported_dialect(tmp_path):
    plan = _plan(_ready_engine())
    with pytest.raises(runner.SchemaRefused) as exc:
        runner.apply_plan(_ready_engine(), plan,
                          supplied_digest=artifacts.compute_digest(plan),
                          backup_receipt=_receipt(tmp_path), out_dir=tmp_path)
    assert "unsupported dialect" in str(exc.value)


def test_apply_refuses_a_non_development_target(tmp_path, monkeypatch):
    plan = _plan(_ready_engine())

    def boom(_engine):
        raise runner.SchemaRefused("target is not development")

    monkeypatch.setattr(runner, "assert_development_target", boom)
    with pytest.raises(runner.SchemaRefused):
        _apply(_ready_engine(), plan, tmp_path)


def test_apply_refuses_a_different_development_database_with_identical_schema(tmp_path):
    """A development-class engine is not enough; it must be the *planned* one."""
    planned = _make_engine(f"sqlite:///{tmp_path / 'planned.db'}")
    other = _make_engine(f"sqlite:///{tmp_path / 'other.db'}")
    for engine in (planned, other):
        with engine.begin() as conn:
            for statement in (*GATE_DDL, *PARENTS, *READY):
                conn.execute(text(statement))
    plan = _plan(planned)
    assert sr.observe(other)["columns"] == sr.observe(planned)["columns"]

    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(other, plan, tmp_path)
    assert "engine database" in str(exc.value)


def test_apply_refuses_a_tampered_target_identity(tmp_path):
    plan = _plan(_ready_engine())
    plan["target"]["database"] = "some_other_dev"
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(_ready_engine(), plan, tmp_path)
    assert "engine database" in str(exc.value)


def test_apply_refuses_a_blocking_plan(tmp_path):
    engine = _engine(*PARENTS,
                     "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
                     " public_body_id TEXT, jurisdiction_id INTEGER)")
    plan = _plan(engine)
    assert plan["blocking_problems"]
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(engine, plan, tmp_path)
    assert "blocking drift" in str(exc.value)


def test_apply_refuses_schema_drift_since_planning(tmp_path):
    """A redundant index changes the observed signature, not the derived work."""
    engine = _ready_engine()
    plan = _plan(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX ix_meetings_public_body_id_extra ON meetings(public_body_id)"))
    assert sr.operations_for(sr.observe(engine)) == []
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(engine, plan, tmp_path)
    assert "drifted" in str(exc.value)


@pytest.mark.parametrize("field", ["contract", "readiness_digest", "code_hashes"])
def test_apply_refuses_plan_binding_drift(tmp_path, field):
    plan = _plan(_ready_engine())
    if field == "contract":
        plan["contract"]["version"] = "tampered"
    elif field == "readiness_digest":
        plan["readiness_digest"] = "0" * 64
    else:
        module = sr.BOUND_MODULES[0]
        plan["code_hashes"][module] = "0" * 64
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(_ready_engine(), plan, tmp_path)
    assert "preflight refused" in str(exc.value)


@pytest.mark.parametrize("module", list(sr.BOUND_MODULES))
def test_apply_refuses_each_bound_module_hash_drift(tmp_path, module):
    plan = _plan(_ready_engine())
    plan["code_hashes"][module] = "0" * 64
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(_ready_engine(), plan, tmp_path)
    assert module in str(exc.value)


def test_apply_refuses_arbitrary_extra_ddl(tmp_path):
    engine = _engine(*PARENTS, *MISSING)
    plan = _plan(engine)
    plan["operations"] = list(plan["operations"]) + [
        {"kind": "drop_table", "target": "meetings", "sql": "DROP TABLE meetings",
         "rationale": "smuggled"}]
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(engine, plan, tmp_path)
    assert "preflight refused" in str(exc.value)


def test_apply_refuses_reordered_ddl(tmp_path):
    engine = _engine(*PARENTS, *MISSING)
    plan = _plan(engine)
    ops = list(plan["operations"])
    ops.reverse()
    plan["operations"] = ops
    plan["expected_ddl"] = [op["sql"] for op in ops]
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(engine, plan, tmp_path)
    assert "preflight refused" in str(exc.value)


def test_apply_refuses_missing_ddl(tmp_path):
    engine = _engine(*PARENTS, *MISSING)
    plan = _plan(engine)
    ops = list(plan["operations"])[:-1]
    plan["operations"] = ops
    plan["expected_ddl"] = [op["sql"] for op in ops]
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(engine, plan, tmp_path)
    assert "preflight refused" in str(exc.value)


def test_apply_refuses_substituted_ddl_sql(tmp_path):
    engine = _engine(*PARENTS, *MISSING)
    plan = _plan(engine)
    ops = [dict(op) for op in plan["operations"]]
    ops[0]["sql"] = "DROP TABLE meetings"
    plan["operations"] = ops
    plan["expected_ddl"] = [op["sql"] for op in ops]
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(engine, plan, tmp_path)
    assert "preflight refused" in str(exc.value)


def test_apply_refuses_expected_ddl_that_disagrees_with_operations(tmp_path):
    plan = _plan(_ready_engine())
    plan["expected_ddl"] = ["DROP TABLE meetings"]
    with pytest.raises(runner.SchemaRefused):
        _apply(_ready_engine(), plan, tmp_path)


# ── backup evidence ─────────────────────────────────────────────────────


def test_apply_refuses_a_missing_backup_receipt(tmp_path):
    plan = _plan(_ready_engine())
    with pytest.raises(runner.SchemaRefused):
        _apply(_ready_engine(), plan, tmp_path, receipt=tmp_path / "absent.json")


def test_apply_refuses_an_unprotected_backup_receipt(tmp_path):
    plan = _plan(_ready_engine())
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(_ready_engine(), plan, tmp_path, receipt=_receipt(tmp_path, protected=False))
    assert "not protected" in str(exc.value)


def test_apply_refuses_an_invalid_backup_receipt(tmp_path):
    plan = _plan(_ready_engine())
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"dump_path": "x"}))
    os.chmod(bad, 0o600)
    with pytest.raises(runner.SchemaRefused):
        _apply(_ready_engine(), plan, tmp_path, receipt=bad)


def test_apply_refuses_backup_counts_that_differ_from_the_plan_baseline(tmp_path):
    plan = _plan(_ready_engine())
    counts = {t: 0 for t in GATE_TABLES}
    counts["entities"] = 7                      # structurally valid, wrong database
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(_ready_engine(), plan, tmp_path, receipt=_receipt(tmp_path, counts))
    assert "does not match source" in str(exc.value) or "refused" in str(exc.value)


def test_apply_refuses_a_backup_receipt_missing_a_required_table(tmp_path):
    plan = _plan(_ready_engine())
    counts = {t: 0 for t in GATE_TABLES if t != "meeting_events"}
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(_ready_engine(), plan, tmp_path, receipt=_receipt(tmp_path, counts))
    assert "missing required tables" in str(exc.value)


def test_apply_refuses_a_receipt_with_no_counts(tmp_path):
    plan = _plan(_ready_engine())
    path = tmp_path / "nocounts.json"
    payload = json.loads(_receipt(tmp_path).read_text())
    payload["counts"] = {}
    path.write_text(json.dumps(payload))
    os.chmod(path, 0o600)
    with pytest.raises(runner.SchemaRefused):
        _apply(_ready_engine(), plan, tmp_path, receipt=path)


# ── refusals happen before any DDL, with durable evidence ───────────────


@pytest.mark.parametrize("mutate,marker", [
    (lambda p: p["target"].__setitem__("database", "elsewhere"), "engine database"),
    (lambda p: p.__setitem__("readiness_digest", "0" * 64), "preflight refused"),
    (lambda p: p["operations"].append(
        {"kind": "x", "target": "y", "sql": "DROP TABLE meetings", "rationale": "z"}),
     "preflight refused"),
])
def test_preflight_refusals_happen_before_ddl_and_leave_evidence(tmp_path, mutate, marker):
    engine = _engine(*PARENTS, *MISSING)
    plan = _plan(engine)
    mutate(plan)
    before = _schema_snapshot(engine)

    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(engine, plan, tmp_path)
    assert marker in str(exc.value)

    assert _schema_snapshot(engine) == before, "refusal must not change the schema"
    refused = _refusal_receipts(tmp_path)
    assert len(refused) == 1, refused
    evidence = json.loads((tmp_path / refused[0]).read_text())
    assert evidence["status"] == "refused"
    assert evidence["refused_before_ddl"] is True
    assert evidence["ddl_started"] is False
    assert evidence["operations_executed"] == 0
    assert _failure_receipts(tmp_path) == []


def test_a_refused_plan_writes_no_preimage(tmp_path):
    engine = _engine(*PARENTS, *MISSING)
    plan = _plan(engine)
    plan["readiness_digest"] = "0" * 64
    with pytest.raises(runner.SchemaRefused):
        _apply(engine, plan, tmp_path)
    assert list(pathlib.Path(tmp_path).glob("*-preimage.json")) == []


def test_refusal_evidence_is_not_overwritten(tmp_path):
    """Immutable receipt rules still hold: a second refusal cannot clobber the first."""
    engine = _engine(*PARENTS, *MISSING)
    plan = _plan(engine)
    plan["readiness_digest"] = "0" * 64
    with pytest.raises(runner.SchemaRefused):
        _apply(engine, plan, tmp_path)
    first = (tmp_path / _refusal_receipts(tmp_path)[0]).read_text()

    with pytest.raises(runner.SchemaRefused):
        _apply(engine, plan, tmp_path)
    assert (tmp_path / _refusal_receipts(tmp_path)[0]).read_text() == first
    assert len(_refusal_receipts(tmp_path)) == 1


# ── idempotence, repeat refusal, rollback ───────────────────────────────


def test_apply_on_a_ready_schema_is_a_noop_success(tmp_path):
    engine = _ready_engine()
    plan = _plan(engine)
    result = _apply(engine, plan, tmp_path)
    assert result["status"] == "success"
    assert result["operations_planned"] == 0
    assert result["operations_executed"] == 0
    assert result["idempotent_noop"] is True
    assert (tmp_path / f"kg-stage2-schema-receipt-{plan['signature_digest'][:16]}.json").exists()


def test_preimage_is_written_before_the_transaction(tmp_path):
    engine = _ready_engine()
    plan = _plan(engine)
    _apply(engine, plan, tmp_path)
    preimage = tmp_path / f"kg-stage2-schema-preimage-{plan['signature_digest'][:16]}.json"
    assert preimage.exists()
    artifacts.load_verified(preimage)


def test_repeat_apply_is_refused(tmp_path):
    engine = _ready_engine()
    plan = _plan(engine)
    _apply(engine, plan, tmp_path)
    with pytest.raises(runner.SchemaRefused) as exc:
        _apply(engine, plan, tmp_path)
    assert "already has a terminal receipt" in str(exc.value)


def test_transactional_rollback_leaves_the_schema_unchanged(tmp_path):
    """A mid-transaction failure must not leave a partial schema change.

    SQLite cannot execute ``ALTER TABLE ... ADD CONSTRAINT``, so a plan for a
    schema missing everything runs the column and index operations and then
    fails on the foreign key.  Every earlier operation must be rolled back.
    """
    engine = _engine(*PARENTS, *MISSING, transactional_ddl=True)
    plan = _plan(engine)
    assert any(op["kind"] == "add_fk_not_valid" for op in plan["operations"])
    before = _schema_snapshot(engine)

    with pytest.raises(Exception):
        _apply(engine, plan, tmp_path)

    after = sr.observe(engine)
    assert _schema_snapshot(engine) == before, "schema changed despite a failed transaction"
    assert after["columns"]["public_body_id"]["present"] is False
    assert after["indexes"]["public_body_id"]["satisfied"] is False


def test_failed_apply_writes_a_labelled_failure_receipt(tmp_path):
    engine = _engine(*PARENTS, *MISSING)
    plan = _plan(engine)
    with pytest.raises(Exception):
        _apply(engine, plan, tmp_path)
    failure = json.loads(
        (tmp_path / f"kg-stage2-schema-receipt-{plan['signature_digest'][:16]}-failure.json")
        .read_text()
    )
    assert failure["status"] == "failed"
    assert failure["ddl_started"] is True
    assert failure["error"]
    assert failure["operations_executed"] < len(plan["operations"])


# ── contract, template, sequencing, rollback prose ──────────────────────


def test_readiness_digest_depends_on_the_bound_modules():
    first = sr.readiness_digest()
    assert first == sr.readiness_digest()
    changed = dict(sr.code_hashes())
    changed[sr.BOUND_MODULES[0]] = "0" * 64
    assert sr.readiness_digest(hashes=changed) != first


def test_production_template_is_unexecuted_and_carries_no_signature():
    plan = _plan(_ready_engine())
    template = schema_plan.production_template({**plan, "digest": "d" * 64})
    assert template["executed"] is False
    assert template["target"]["tier"] == "production"
    assert template["target"]["signature"] is None
    assert template["derived_from"]["signature_digest"] == plan["signature_digest"]
    assert template["expected_ddl"] == plan["expected_ddl"]


def test_sequencing_puts_development_before_production_before_sync():
    steps = " ".join(schema_plan.sequencing()["steps"])
    assert steps.index("development") < steps.index("production")
    assert steps.index("production") < steps.index("sync")


def test_rollback_contract_is_accurate_about_transactions():
    rb = schema_plan.rollback_contract()
    assert "transactional" in rb["transaction"]
    assert "rolls back" in rb["transaction"]
    assert "after commit" in rb["after_commit"].lower()
    # no absolutist irreversibility claim
    assert "NOT REVERSIBLE" not in json.dumps(rb)
    assert "one-way" not in rb["limitation"]
    assert "un-validate" in rb["validate_fk"]


def test_plan_rollback_section_matches_the_shared_contract():
    plan = _plan(_ready_engine())
    assert plan["rollback"] == schema_plan.rollback_contract()


def test_code_hashes_cover_every_bound_module():
    hashes = sr.code_hashes()
    assert set(hashes) == set(sr.BOUND_MODULES)
    for digest in hashes.values():
        assert len(digest) == 64


def test_runner_does_not_claim_absolute_irreversibility():
    doc = runner.__doc__ or ""
    assert "transactional" in doc
    assert "NOT REVERSIBLE" not in doc
