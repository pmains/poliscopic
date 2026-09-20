#!/usr/bin/env python3
"""Stage 2 Step 2 apply machinery — fail-closed mechanics on isolated SQLite.

Each test here corresponds to a defect an independent review found in the first
draft of this runner.  They are behavioural: they assert the refusal, not the
presence of a line of code.

SQLite cannot express ``ADD CONSTRAINT``, so the foreign key is asserted
structurally and the column/index mechanics are exercised end to end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import pathlib
import sys

import pytest
from sqlalchemy import bindparam, create_engine, inspect, text

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_apply as apply_mod  # noqa: E402
from scripts.kg import stage2_s2_apply_checks as checks  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402

_DDL = (
    """
    CREATE TABLE agenda_items (
        id INTEGER PRIMARY KEY,
        meeting_db_id INTEGER NOT NULL,
        agenda_item_number VARCHAR(32) NOT NULL,
        agenda_item_id VARCHAR(128) NOT NULL
    )
    """,
    """
    CREATE TABLE supporting_documents (
        id INTEGER PRIMARY KEY,
        meeting_db_id INTEGER NOT NULL,
        agenda_item_id VARCHAR(256) NOT NULL,
        agenda_item_number VARCHAR(32) NOT NULL,
        document_url VARCHAR(1024) NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        body VARCHAR(256) NOT NULL DEFAULT ''
    )
    """,
)

_ITEMS = ((101, 1, "7", "A7"), (102, 1, "0", "A0a"),
          (103, 1, "0", "A0b"), (105, 1, "3", "A3"))

# id, key, number
_DOCUMENTS = (
    (1, "0", "7"),                        # links to 101 via the item number
    (2, "0", "0"),                        # ambiguous: 102 and 103
    (3, "A3", "99"),                      # links to 105 via the source key
    (4, "0", "55"),                       # Stage 3 gap
    (5, "result-phoenix-cc-x", "77"),     # meeting-level
)


def _populate(engine) -> None:
    with engine.begin() as connection:
        for statement in _DDL:
            connection.execute(text(statement))
        for item in _ITEMS:
            connection.execute(
                text("INSERT INTO agenda_items (id, meeting_db_id, agenda_item_number, "
                     "agenda_item_id) VALUES (:i, :m, :n, :k)"),
                {"i": item[0], "m": item[1], "n": item[2], "k": item[3]})
        for row in _DOCUMENTS:
            connection.execute(
                text("INSERT INTO supporting_documents (id, meeting_db_id, agenda_item_id, "
                     "agenda_item_number, document_url, updated_at, body) "
                     "VALUES (:i, 1, :k, :n, :u, :s, 'b')"),
                {"i": row[0], "k": row[1], "n": row[2],
                 "u": f"https://example.test/{row[0]}", "s": "2026-09-12 00:00:00"})


@pytest.fixture()
def fixture():
    engine = create_engine("sqlite://")
    _populate(engine)
    return engine


def _plan(engine, plan_id="20260913T000000Z", created_at="2026-09-13T00:00:00+00:00",
          **extra) -> dict:
    plan = documents.build_plan(engine, {"integrity": {}}, plan_id=plan_id,
                                created_at=created_at, **extra)
    return plan


def _receipt(plan, **overrides):
    """A protected receipt that satisfies the plan unless overridden."""
    baseline = plan["baseline"]
    counts = dict(baseline["db_counts"])
    request = {
        "dump_path": "/protected/dev.dump",
        "dump_sha256": "a" * 64,
        # Fresh by default: the receipt contract carries a one-day freshness window, so a
        # baked-in date silently rots.  Staleness tests override this explicitly.
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": dict(plan["target"], tier="development"),
        "scratch": {"host": "localhost", "database": "poliscopic_restore_scratch"},
        "pg_restore": {"exit_code": 0, "evidence": "pg_restore restored all objects"},
        "counts": counts,
        "signatures": {"schema_sha256": "b" * 64,
                       "counts_sha256": receipts.counts_fingerprint(counts)},
    }
    request.update(overrides)
    return request


def _write_receipt(tmp_path, payload, name="backup.json", mode=0o600):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    path.chmod(mode)
    return path


def _apply(engine, plan, tmp_path, receipt, **kw):
    return apply_mod.apply_plan(
        engine, plan, supplied_digest=artifacts.compute_digest(plan),
        backup_receipt=receipt, out_dir=tmp_path,
        allow_unsupported_dialect=True, **kw)


def _columns(engine) -> set:
    return {c["name"] for c in inspect(engine).get_columns("supporting_documents")}


def _null_parents(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT COUNT(*) FROM supporting_documents WHERE agenda_item_db_id IS NULL")
        ).scalar()


# ── the additive schema change ──────────────────────────────────────────


def test_the_change_is_strictly_additive():
    statements = apply_mod.add_column_ddl("postgresql")
    assert statements[0] == \
        "ALTER TABLE supporting_documents ADD COLUMN agenda_item_db_id integer NULL"
    assert any("CREATE INDEX" in s for s in statements)
    assert any(f"CONSTRAINT {apply_mod.FK_NAME}" in s for s in statements)
    assert any("ON DELETE SET NULL" in s for s in statements)
    assert not any("DROP" in s.upper() for s in statements)


def test_sqlite_omits_the_foreign_key_it_cannot_express():
    statements = apply_mod.add_column_ddl("sqlite")
    assert not any("CONSTRAINT" in s for s in statements)
    assert len(statements) == 2


def test_an_existing_column_is_refused(fixture):
    with fixture.begin() as connection:
        connection.execute(
            text("ALTER TABLE supporting_documents ADD COLUMN agenda_item_db_id integer"))
        with pytest.raises(apply_mod.ApplyRefused) as exc:
            apply_mod.require_absent_column(connection, "sqlite")
    assert "already exists" in str(exc.value)


# ── defect 1: a protected backup receipt was not required ───────────────


def test_a_missing_backup_receipt_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    with pytest.raises(checks.CheckRefused) as exc:
        _apply(fixture, plan, tmp_path, tmp_path / "nope.json")
    assert "not found" in str(exc.value)
    assert "agenda_item_db_id" not in _columns(fixture)


def test_an_unprotected_backup_receipt_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt(plan), mode=0o644)
    with pytest.raises(checks.CheckRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "not protected" in str(exc.value)
    assert "agenda_item_db_id" not in _columns(fixture)


def test_a_receipt_missing_a_count_key_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    counts = dict(plan["baseline"]["db_counts"])
    counts.pop("supporting_documents")
    receipt = _write_receipt(tmp_path, _receipt(plan, counts=counts))
    with pytest.raises(checks.CheckRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "missing required count keys" in str(exc.value)
    assert "supporting_documents" in str(exc.value)


def test_a_receipt_with_a_wrong_count_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    counts = dict(plan["baseline"]["db_counts"])
    counts["supporting_documents"] += 1
    receipt = _write_receipt(tmp_path, _receipt(plan, counts=counts))
    with pytest.raises(checks.CheckRefused):
        _apply(fixture, plan, tmp_path, receipt)


def test_a_receipt_whose_fingerprint_disagrees_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    receipt = _write_receipt(
        tmp_path,
        _receipt(plan, signatures={"schema_sha256": "b" * 64, "counts_sha256": "f" * 64}))
    with pytest.raises(checks.CheckRefused):
        _apply(fixture, plan, tmp_path, receipt)


def test_a_receipt_describing_another_database_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    target = dict(plan["target"], tier="development", database="poliscopic_other")
    receipt = _write_receipt(tmp_path, _receipt(plan, target=target))
    with pytest.raises(checks.CheckRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "target refused" in str(exc.value)


def test_the_baseline_binds_db_counts_and_a_fingerprint(fixture):
    plan = _plan(fixture)
    baseline = plan["baseline"]
    assert baseline["db_counts"]["supporting_documents"] == len(_DOCUMENTS)
    assert baseline["db_counts"]["agenda_items"] == len(_ITEMS)
    assert baseline["db_counts_fingerprint"] == receipts.counts_fingerprint(baseline["db_counts"])
    assert "supporting_documents" in baseline["protected_tables"]


# ── defect 2: held rows were verified only up to 2000 ───────────────────


def test_chunks_cover_every_item_in_order():
    items = list(range(5000))
    seen = [i for chunk in checks.chunks(items, 1000) for i in chunk]
    assert seen == items                      # every item, exactly once, in order
    assert len(list(checks.chunks([1, 2, 3], 1000))) == 1
    assert list(checks.chunks([], 10)) == []


def test_a_linked_held_row_is_caught_beyond_the_first_chunk(fixture, tmp_path, monkeypatch):
    """The old check stopped at 2000 rows; a late held row slipped through."""
    monkeypatch.setattr(checks, "CHUNK", 2)
    plan = _plan(fixture)
    held = [h["document_id"] for h in plan["holds"]]
    assert len(held) > 2, "fixture must span more than one chunk"
    with fixture.begin() as connection:
        connection.execute(text("ALTER TABLE supporting_documents ADD COLUMN agenda_item_db_id integer"))
        connection.execute(
            text("UPDATE supporting_documents SET agenda_item_db_id = 101 WHERE id = :i"),
            {"i": held[-1]})                   # only the *last* chunk contains it
    with fixture.connect() as connection:
        problems = checks.verify_held_unlinked(connection, held)
    assert problems == [f"held document {held[-1]} was linked"]


def test_held_rows_are_verified_in_full_not_sampled(fixture, tmp_path, monkeypatch):
    """Every held row is checked, so the failure is caught wherever it sits."""
    monkeypatch.setattr(checks, "CHUNK", 1)
    plan = _plan(fixture)
    held = [h["document_id"] for h in plan["holds"]]
    with fixture.begin() as connection:
        connection.execute(text("ALTER TABLE supporting_documents ADD COLUMN agenda_item_db_id integer"))
        connection.execute(text("UPDATE supporting_documents SET agenda_item_db_id = 101 "
                                "WHERE id IN :ids")
                           .bindparams(bindparam("ids", expanding=True)), {"ids": held})
    with fixture.connect() as connection:
        problems = checks.verify_held_unlinked(connection, held)
    assert len(problems) == len(held)         # all of them, not just one


def test_document_identities_are_read_in_full(fixture, monkeypatch):
    monkeypatch.setattr(checks, "CHUNK", 2)
    plan = _plan(fixture)
    ids = [a["document_id"] for a in plan["attachments"]] + \
          [h["document_id"] for h in plan["holds"]]
    with fixture.connect() as connection:
        observed = checks.read_document_identities(connection, ids)
    assert set(observed) == set(ids)
    assert checks.verify_document_identities(plan, observed) == []


# ── defect 3: identity was read outside the transaction, unlocked ───────


def test_postgres_locks_every_document_it_reads():
    """A spy connection proves FOR UPDATE is issued, per chunk."""
    statements = []

    class _Spy:
        def execute(self, statement, params):
            statements.append(str(statement))
            return []

    checks.lock_scope(_Spy(), "postgresql", [1, 2, 3])
    assert statements and all("FOR UPDATE" in s for s in statements)
    statements.clear()
    checks.lock_scope(_Spy(), "sqlite", [1, 2, 3])
    assert statements and not any("FOR UPDATE" in s for s in statements)


def test_the_lock_precedes_every_read_inside_the_transaction():
    """Lock, then read, all inside one transaction — no TOCTOU window."""
    import inspect as _inspect

    source = _inspect.getsource(apply_mod.apply_plan)
    body = source[source.index("with engine.begin() as connection:"):
                  source.index("except Exception as exc:")]
    assert "FOR UPDATE" not in body           # locking is delegated, not inlined
    lock = body.index("checks.lock_scope(")
    identity = body.index("checks.read_document_identities(")
    keys = body.index("checks.read_source_keys(")
    ddl = body.index("for statement in add_column_ddl(dialect):")
    update = body.index("UPDATE supporting_documents")
    assert lock < identity < keys < ddl < update
    # ...and the source keys are re-read after the write, inside the same txn
    assert body.count("checks.read_source_keys(") == 2
    assert body.rindex("checks.read_source_keys(") > update


def test_a_document_that_drifted_after_locking_is_refused(fixture, tmp_path, monkeypatch):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt(plan))
    drifted = {int(a["document_id"]): {"source_key": "0", "fingerprint": "0" * 64}
               for a in plan["attachments"]}
    drifted.update({int(h["document_id"]): {"source_key": "0", "fingerprint": "0" * 64}
                    for h in plan["holds"]})
    monkeypatch.setattr(checks, "read_document_identities", lambda c, ids: drifted)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "preconditions failed" in str(exc.value)
    assert "drifted" in str(exc.value)
    assert "agenda_item_db_id" not in _columns(fixture)


def test_a_missing_document_is_refused(fixture, tmp_path, monkeypatch):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt(plan))
    monkeypatch.setattr(checks, "read_document_identities", lambda c, ids: {})
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "missing" in str(exc.value)


def test_a_drifted_agenda_item_is_refused(fixture, tmp_path, monkeypatch):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt(plan))
    monkeypatch.setattr(checks, "read_agenda_items", lambda c, ids: {})
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "agenda item" in str(exc.value)


def test_a_changed_source_key_is_refused():
    before = {1: "0", 2: "A3"}
    assert checks.verify_source_keys(before, dict(before)) == []
    problems = checks.verify_source_keys(before, {1: "0", 2: "REWRITTEN"})
    assert problems and "document 2" in problems[0]
    assert checks.verify_source_keys(before, {1: "0"})


def test_a_source_key_changed_mid_transaction_is_refused(fixture, tmp_path, monkeypatch):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt(plan))
    real = checks.read_source_keys
    calls = {"n": 0}

    def _rewriting(connection, ids):
        calls["n"] += 1
        observed = real(connection, ids)
        if calls["n"] > 1:                      # the post-write recheck
            observed = dict(observed)
            observed[int(next(iter(observed)))] = "REWRITTEN"
        return observed

    monkeypatch.setattr(checks, "read_source_keys", _rewriting)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "source key" in str(exc.value)


# ── defect 4: a current plan was refused for its predecessor's status ───


def test_a_plan_marking_itself_obsolete_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    plan["obsolete"] = True
    path = tmp_path / "plan.json"
    artifacts.write_immutable(path, plan)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        apply_mod.require_plan(path, artifacts.compute_digest(plan), tmp_path)
    assert "marks itself obsolete" in str(exc.value)


def test_a_plan_superseded_by_a_newer_plan_is_refused(fixture, tmp_path):
    older = _plan(fixture)
    older_path = tmp_path / "kg-stage2-s2-plan-20260913T000000Z.json"
    artifacts.write_immutable(older_path, older)
    newer = _plan(fixture, plan_id="20260913T010000Z",
                  created_at="2026-09-13T01:00:00+00:00",
                  supersedes={"plan_id": "20260913T000000Z", "obsolete": True})
    artifacts.write_immutable(tmp_path / "kg-stage2-s2-plan-20260913T010000Z.json", newer)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        apply_mod.require_plan(older_path, artifacts.compute_digest(older), tmp_path)
    assert "superseded by" in str(exc.value)


def test_a_current_plan_may_supersede_an_obsolete_prior(fixture, tmp_path):
    """The successor must not inherit its predecessor's obsolescence."""
    plan = _plan(fixture, plan_id="20260913T020000Z",
                 created_at="2026-09-13T02:00:00+00:00",
                 supersedes={"plan_id": "20260913T000000Z", "digest": "d" * 64,
                             "obsolete": True, "unbound_modules": [],
                             "drifted_modules": []})
    path = tmp_path / "kg-stage2-s2-plan-20260913T020000Z.json"
    artifacts.write_immutable(path, plan)
    loaded = apply_mod.require_plan(path, artifacts.compute_digest(plan), tmp_path)
    assert loaded["supersedes"]["obsolete"] is True
    assert apply_mod.superseded_by(tmp_path, "20260913T020000Z") == []


def test_an_obsolete_prior_is_only_refused_for_itself(fixture, tmp_path):
    prior = _plan(fixture)
    prior_id = prior["plan_id"]
    successor = _plan(fixture, plan_id="20260913T030000Z",
                      created_at="2026-09-13T03:00:00+00:00",
                      supersedes={"plan_id": prior_id, "obsolete": True})
    artifacts.write_immutable(tmp_path / f"kg-stage2-s2-plan-{prior_id}.json", prior)
    artifacts.write_immutable(tmp_path / "kg-stage2-s2-plan-20260913T030000Z.json", successor)
    assert apply_mod.superseded_by(tmp_path, prior_id) == ["20260913T030000Z"]
    assert apply_mod.superseded_by(tmp_path, "20260913T030000Z") == []
    # the successor itself is current
    apply_mod.require_plan(tmp_path / "kg-stage2-s2-plan-20260913T030000Z.json",
                           artifacts.compute_digest(successor), tmp_path)


# ── defect 5: stale bound hashes ────────────────────────────────────────


def test_the_plan_binds_the_modules_the_column_touches():
    bound = documents.CODE_MODULES
    for module in ("scripts/kg/stage2_s2_apply.py", "scripts/kg/stage2_s2_apply_checks.py",
                   "scripts/kg/stage2_s2_adjudication.py", "scripts/db/models.py",
                   "scripts/db/sync_prod.py", "scripts/entities/schema_parity.py",
                   "scripts/kg/stage2_s2_verify.py"):
        assert module in bound, module


def test_a_plan_whose_bound_code_drifted_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    plan["code_hashes"] = dict(plan["code_hashes"],
                               **{"scripts/kg/stage2_s2_apply.py": "0" * 64})
    path = tmp_path / "plan.json"
    artifacts.write_immutable(path, plan)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        apply_mod.require_plan(path, artifacts.compute_digest(plan), tmp_path)
    assert "bound code refused" in str(exc.value)


def test_an_incoherent_plan_identity_is_refused(fixture, tmp_path):
    plan = dict(_plan(fixture), plan_id="20260913T090000Z")   # not the id of created_at
    path = tmp_path / "plan.json"
    artifacts.write_immutable(path, plan)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        apply_mod.require_plan(path, artifacts.compute_digest(plan), tmp_path)
    assert "identity refused" in str(exc.value)


# ── the apply itself ───────────────────────────────────────────────────


def test_apply_links_only_the_deterministic_rows(fixture, tmp_path):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt(plan))
    result = _apply(fixture, plan, tmp_path, receipt)
    assert result["postconditions"]["passed"] is True
    assert result["postconditions"]["linked"] == len(plan["attachments"])
    assert result["postconditions"]["unlinked"] == len(plan["holds"])
    with fixture.connect() as connection:
        rows = connection.execute(text(
            "SELECT id, agenda_item_db_id FROM supporting_documents "
            "WHERE agenda_item_db_id IS NOT NULL ORDER BY id")).fetchall()
    assert [int(r[0]) for r in rows] == [a["document_id"] for a in plan["attachments"]]


def test_apply_leaves_held_rows_unlinked(fixture, tmp_path):
    plan = _plan(fixture)
    _apply(fixture, plan, tmp_path, _write_receipt(tmp_path, _receipt(plan)))
    assert _null_parents(fixture) == len(plan["holds"])


def test_apply_preserves_every_source_key_byte_for_byte(fixture, tmp_path):
    plan = _plan(fixture)
    with fixture.connect() as connection:
        before = dict(connection.execute(
            text("SELECT id, agenda_item_id FROM supporting_documents")).fetchall())
    _apply(fixture, plan, tmp_path, _write_receipt(tmp_path, _receipt(plan)))
    with fixture.connect() as connection:
        after = dict(connection.execute(
            text("SELECT id, agenda_item_id FROM supporting_documents")).fetchall())
    assert before == after


def test_apply_writes_immutable_preimage_and_receipt(fixture, tmp_path):
    plan = _plan(fixture)
    result = _apply(fixture, plan, tmp_path, _write_receipt(tmp_path, _receipt(plan)))
    for key in ("preimage_artifact", "receipt_path"):
        path = pathlib.Path(result[key])
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600
        assert artifacts.recorded_digest(json.loads(path.read_text()))
    on_disk = json.loads(pathlib.Path(result["receipt_path"]).read_text())
    assert on_disk["operations"] == result["operations"]
    assert on_disk["backup_receipt"]["counts_fingerprint"]


def test_a_second_apply_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt(plan))
    _apply(fixture, plan, tmp_path, receipt)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "already has a terminal receipt" in str(exc.value)


def test_a_wrong_digest_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        apply_mod.apply_plan(
            fixture, plan, supplied_digest="0" * 64,
            backup_receipt=_write_receipt(tmp_path, _receipt(plan)),
            out_dir=tmp_path, allow_unsupported_dialect=True)
    assert "digest changed" in str(exc.value)
    assert "agenda_item_db_id" not in _columns(fixture)


def test_a_plan_that_links_nothing_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    plan["attachments"] = []
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, _write_receipt(tmp_path, _receipt(plan)))
    assert "links nothing" in str(exc.value)


def test_a_failed_apply_rolls_back_every_row_write(fixture, tmp_path, monkeypatch):
    """Data rollback is demonstrable; transactional DDL is PostgreSQL-only."""
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt(plan))
    monkeypatch.setattr(checks, "read_agenda_items", lambda c, ids: {})
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, receipt)
    with fixture.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM supporting_documents")).scalar() == len(_DOCUMENTS)
    names = sorted(p.name for p in pathlib.Path(tmp_path).glob("*.json"))
    assert any(n.endswith("-failure.json") for n in names), names
    terminal = f"kg-stage2-s2-receipt-{plan['plan_id']}.json"
    assert terminal not in names, names


def test_protected_state_drift_is_refused():
    before = {"supporting_documents": 5}
    assert checks.verify_protected_state(before, dict(before), {"orphan_mentions": 0},
                                         {"orphan_mentions": 0}) == []
    problems = checks.verify_protected_state(before, {"supporting_documents": 6},
                                             {"orphan_mentions": 0}, {"orphan_mentions": 1})
    assert len(problems) == 2


def test_refusal_exit_code_is_two():
    assert apply_mod.REFUSED_EXIT_CODE == 2
    assert apply_mod.ApplyRefused in apply_mod.CLI_REFUSALS
    assert checks.CheckRefused in apply_mod.CLI_REFUSALS
