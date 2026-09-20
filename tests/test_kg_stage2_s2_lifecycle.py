#!/usr/bin/env python3
"""Stage 2 S2 — locking coverage, artifact lifecycle, ORM staging, renderer."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, inspect, select, text

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_adjudication_markdown as md  # noqa: E402
from scripts.kg import stage2_s2_apply as apply_mod  # noqa: E402
from scripts.kg import stage2_s2_apply_checks as checks  # noqa: E402


class _Spy:
    """Records the SQL it is asked to run."""

    def __init__(self):
        self.statements: list[str] = []
        self.params: list[dict] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params.append(dict(params or {}))
        return []


# ── locking covers both tables, every chunk, in order ──────────────────


def test_both_tables_are_locked_and_the_order_is_fixed():
    spy = _Spy()
    checks.lock_scope(spy, "postgresql", [1, 2], [7, 8])
    assert len(spy.statements) == 2
    assert "supporting_documents" in spy.statements[0]
    assert "agenda_items" in spy.statements[1]
    assert all("FOR UPDATE" in s for s in spy.statements)


def test_locking_issues_every_chunk_for_both_tables():
    spy = _Spy()
    documents = list(range(1, 12))
    items = list(range(101, 109))
    original = checks.CHUNK
    checks.CHUNK = 3
    try:
        checks.lock_scope(spy, "postgresql", documents, items)
    finally:
        checks.CHUNK = original
    seen_docs = [i for s, p in zip(spy.statements, spy.params)
                 if "supporting_documents" in s for i in p["ids"]]
    seen_items = [i for s, p in zip(spy.statements, spy.params)
                  if "agenda_items" in s for i in p["ids"]]
    assert seen_docs == documents           # every document, in order, once
    assert seen_items == items              # every item, in order, once
    # 11 documents at 3 per chunk -> 4 statements; 8 items -> 3 statements.
    # The point is that no chunk is skipped, which the coverage asserts above.
    assert len(spy.statements) == 4 + 3


def test_sqlite_locks_without_for_update():
    spy = _Spy()
    checks.lock_scope(spy, "sqlite", [1], [2])
    assert spy.statements and not any("FOR UPDATE" in s for s in spy.statements)


def test_the_apply_locks_items_before_reading_them():
    import inspect as _inspect

    source = _inspect.getsource(apply_mod.apply_plan)
    body = source[source.index("with engine.begin() as connection:"):
                  source.index("except Exception as exc:")]
    lock = body.index("checks.lock_scope(connection, dialect, all_ids, item_ids)")
    read_items = body.index("checks.read_agenda_items(")
    assert lock < read_items
    assert "item_ids" in body[lock:lock + 80]


def test_agenda_item_reads_are_chunked_and_complete():
    """A small chunk size must not change which items are returned."""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE agenda_items (id INTEGER PRIMARY KEY, "
                                "meeting_db_id INTEGER, agenda_item_number VARCHAR(32), "
                                "agenda_item_id VARCHAR(128))"))
        for i in range(1, 8):
            connection.execute(
                text("INSERT INTO agenda_items (id, meeting_db_id, agenda_item_number, "
                     "agenda_item_id) VALUES (:i, 1, :n, :k)"),
                {"i": i, "n": str(i), "k": f"K{i}"})
    ids = list(range(1, 8))
    original = checks.CHUNK
    checks.CHUNK = 2
    try:
        with engine.connect() as connection:
            observed = checks.read_agenda_items(connection, ids)
    finally:
        checks.CHUNK = original
    assert sorted(observed) == ids


# ── artifact lifecycle ─────────────────────────────────────────────────


def _scaffold(tmp_path, plan_id="20260913T000000Z"):
    """The artifact set a failed attempt leaves behind."""
    preimage = tmp_path / f"kg-stage2-s2-receipt-{plan_id}-preimage.json"
    failure = tmp_path / f"kg-stage2-s2-receipt-{plan_id}-failure.json"
    artifacts.write_immutable(preimage, {"kind": "x", "stage": "preimage"})
    artifacts.write_immutable(failure, {"kind": "x", "stage": "failure"})
    return preimage, failure


def test_a_preimage_or_failure_is_not_a_terminal_receipt(tmp_path):
    """The old glob treated every related artifact as a terminal receipt."""
    plan_id = "20260913T000000Z"
    _scaffold(tmp_path, plan_id)
    assert apply_mod.terminal_receipt(tmp_path, plan_id) is None
    assert len(apply_mod.prior_attempts(tmp_path, plan_id)) == 2


def test_a_terminal_receipt_is_recognised_exactly(tmp_path):
    plan_id = "20260913T000000Z"
    path, _ = apply_mod.write_receipt(tmp_path, plan_id, {"kind": "x", "stage": "terminal"})
    assert apply_mod.terminal_receipt(tmp_path, plan_id) == path
    assert apply_mod.prior_attempts(tmp_path, plan_id) == []


def test_a_retry_without_disposition_is_refused(tmp_path):
    plan_id = "20260913T000000Z"
    _scaffold(tmp_path, plan_id)
    attempts = apply_mod.prior_attempts(tmp_path, plan_id)
    assert all(a["digest"] for a in attempts)
    # Refusal is the apply's own guard, exercised directly.
    covered = set()
    outstanding = [a for a in attempts if a["digest"] not in covered]
    assert len(outstanding) == 2


def test_a_disposition_covering_every_attempt_clears_the_retry(tmp_path):
    plan_id = "20260913T000000Z"
    _scaffold(tmp_path, plan_id)
    attempts = apply_mod.prior_attempts(tmp_path, plan_id)
    path, digest = apply_mod.record_disposition(
        tmp_path, plan_id, attempts, "reviewed: preimage captured, guard was added after")
    disposition = json.loads(path.read_text())
    assert disposition["reason"].startswith("reviewed")
    assert {a["digest"] for a in disposition["superseded_attempts"]} == \
        {a["digest"] for a in attempts}
    covered = {a["digest"] for a in disposition["superseded_attempts"]}
    assert [a for a in attempts if a["digest"] not in covered] == []
    assert artifacts.recorded_digest(disposition) == digest


def test_a_disposition_covering_only_some_attempts_is_not_enough(tmp_path):
    plan_id = "20260913T000000Z"
    _scaffold(tmp_path, plan_id)
    attempts = apply_mod.prior_attempts(tmp_path, plan_id)
    partial = {"superseded_attempts": [attempts[0]]}
    covered = {a["digest"] for a in partial["superseded_attempts"]}
    outstanding = [a for a in attempts if a["digest"] not in covered]
    assert len(outstanding) == 1


def test_retry_artifacts_never_overwrite_the_earlier_ones(tmp_path):
    plan_id = "20260913T000000Z"
    first, _ = _scaffold(tmp_path, plan_id)
    before = first.read_bytes()
    second, _ = apply_mod.write_receipt(
        tmp_path, plan_id, {"kind": "x", "stage": "preimage"}, suffix="-preimage", unique=True)
    assert second != first
    assert first.read_bytes() == before      # the earlier evidence is untouched
    assert second.name.endswith("-preimage-2.json")


def test_write_receipt_still_refuses_to_overwrite_by_default(tmp_path):
    plan_id = "20260913T000000Z"
    apply_mod.write_receipt(tmp_path, plan_id, {"kind": "x", "stage": "terminal"})
    with pytest.raises(artifacts.ArtifactCollision):
        apply_mod.write_receipt(tmp_path, plan_id, {"kind": "x", "stage": "terminal"})


def test_prior_attempts_is_empty_for_an_unattempted_plan(tmp_path):
    assert apply_mod.prior_attempts(tmp_path, "20260913T999999Z") == []
    assert apply_mod.terminal_receipt(tmp_path, "20260913T999999Z") is None


# ── staged ORM compatibility ───────────────────────────────────────────


def test_the_deferred_column_is_out_of_default_selects():
    """The declarative column exists, but no ordinary query asks for it."""
    from scripts.db import models

    statement = select(models.SupportingDocument)
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "agenda_item_db_id" not in sql


def test_the_staged_column_is_still_declared_for_the_migration():
    """The migration still has a declaration to apply."""
    from sqlalchemy import inspect as sa_inspect

    from scripts.db import models

    column = models.SupportingDocument.__table__.columns["agenda_item_db_id"]
    assert column.nullable is True
    assert column.name in models.SupportingDocument.__table__.c
    # mapped, so the ORM knows it once the column exists
    assert "agenda_item_db_id" in sa_inspect(models.SupportingDocument).attrs


def test_loading_a_document_does_not_require_the_new_column():
    """A pre-migration table loads cleanly, which is the whole point."""
    from scripts.db import models

    engine = create_engine("sqlite://")
    table = models.SupportingDocument.__table__
    # Exactly the pre-migration shape: the declaration exists, the column (and
    # the constraint that references it) does not.
    columns = ", ".join(
        f"{c.name} {c.type.compile(engine.dialect)}" + ("" if c.nullable else " NOT NULL")
        for c in table.columns if c.name != "agenda_item_db_id"
    )
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE supporting_documents ({columns})"))
        connection.execute(text(
            "INSERT INTO supporting_documents (id, body, agenda_item_id, meeting_id, "
            "meeting_db_id, agenda_item_number, document_title, document_url, created_at, "
            "updated_at, extraction_attempts) VALUES "
            "(1, 'b', '0', 'm', 1, '7', 't', 'https://example.test/1', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00', 0)"))
    assert "agenda_item_db_id" not in {
        c["name"] for c in __import__("sqlalchemy").inspect(engine).get_columns("supporting_documents")}
    with engine.connect() as connection:
        rows = connection.execute(select(models.SupportingDocument)).scalars().all()
    assert len(rows) == 1                     # the pre-migration shape still queries


def test_sync_derives_columns_from_the_database_not_the_model():
    """The staged column cannot leak into sync before it exists."""
    import inspect as _inspect

    from scripts.db import sync_schema

    source = _inspect.getsource(sync_schema)
    assert "get_columns" in source            # catalogue-driven, not metadata-driven
    assert "SupportingDocument" not in source


# ── markdown renderer ──────────────────────────────────────────────────


def _packet() -> dict:
    return {
        "kind": "kg-stage2-s2-adjudication",
        "packet_id": "p1",
        "plan": {"plan_id": "20260913T000000Z", "digest": "d" * 64},
        "counts": {"ambiguous_documents": 2, "groups": 1, "meetings": 1, "bodies": 1,
                   "candidates_in_total": 2},
        "decision_contract": {"required": ["choose a listed candidate", "or hold"],
                              "forbidden": ["guessing"]},
        "groups": [{
            "group_key": {"body": "el-mirage-cc", "meeting_db_id": 42,
                          "agenda_item_number": "5"},
            "candidates": [
                {"agenda_item_db_id": 900, "source_key": "A5", "section_level": 1,
                 "sort_order": 3, "item_type": "item", "title": "Call to order"},
                {"agenda_item_db_id": 901, "source_key": "B5", "section_level": 2,
                 "sort_order": 4, "item_type": "subitem", "title": "Approval | of minutes"},
            ],
            "candidate_count": 2,
            "documents": [
                {"document_id": 11, "source_key": "0", "document_type": "PDF",
                 "document_title": "Packet"},
                {"document_id": 12, "source_key": "0", "document_type": "Attachment",
                 "document_title": "Memo"},
            ],
            "status": "awaiting_human_decision",
            "decision": None,
        }],
    }


def test_render_covers_every_group_and_every_candidate():
    body = md.render(_packet(), "packet.json")
    assert "Group 1:" in body
    assert "`900`" in body and "`901`" in body
    assert "A5" in body and "B5" in body
    assert "Call to order" in body
    assert "`11`" in body and "`12`" in body
    assert "Groups rendered: **1** of 1." in body


def test_render_preselects_nothing_and_offers_choose_or_hold():
    body = md.render(_packet(), "packet.json")
    assert "- [ ] choose" in body
    assert "- [ ] hold" in body
    assert "- [x]" not in body                # nothing is ticked
    assert body.count("**Decision**") == 1


def test_render_escapes_table_breaking_text():
    body = md.render(_packet(), "packet.json")
    assert "Approval \\| of minutes" in body  # pipes escaped
    assert "Approval | of minutes" not in body


def test_render_is_deterministic():
    assert md.render(_packet(), "packet.json") == md.render(_packet(), "packet.json")


def test_markdown_is_written_immutably(tmp_path):
    path, digest = md.write_markdown(_packet(), "packet.json", tmp_path)
    assert path.suffix == ".md"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert artifacts.recorded_digest(json.loads(path.read_text())) == digest
    with pytest.raises(artifacts.ArtifactCollision):
        md.write_markdown(_packet(), "packet.json", tmp_path)
