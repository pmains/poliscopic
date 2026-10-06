"""Tests for deterministic provenance-consolidation planning and apply.

The automatic apply path is exercised against isolated in-memory SQLite
engines with the same table shapes the consolidation SQL touches.  The real
``_integrity_snapshot`` query set needs the full production schema, so tests
monkeypatch it on ``provenance_consolidation_apply`` (where ``apply_consolidation``
resolves the name at call time).
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

from sqlalchemy import bindparam, create_engine, text

import scripts.entities.provenance_consolidation as consolidation
import scripts.entities.provenance_consolidation_apply as apply_module
from scripts.entities.provenance_consolidation import (
    apply_consolidation,
    build_human_adjudication_plan,
    _operation_target_ids,
    _plan_operations,
    _rows_fingerprint,
)
from scripts.entities.provenance_consolidation_operations import (
    _rows_as_dicts,
    _plan_fingerprint,
)

INTEGRITY_BASELINE = {
    "graph_builder_repeat_excess": 0,
    "pattern_extraction_repeat_excess": 0,
    "orphan_mentions": 0,
    "orphan_relationships": 0,
    "orphan_extractions": 0,
    "orphan_participants": 0,
    "unresolved_relationship_provenance": 2,
    "unknown_relationship_provenance_type": 0,
}


def _relationship(identifier: int, source_id: int) -> dict[str, object]:
    return {
        "id": identifier,
        "from_entity_id": 10,
        "relationship": "PRESENT_AT",
        "to_entity_id": 20,
        "provenance_type": "meeting_member",
        "provenance_id": source_id,
    }


def _patch_integrity(monkeypatch, baseline=None):
    """Point the apply module's integrity snapshot at a fixed baseline."""
    monkeypatch.setattr(
        apply_module, "_integrity_snapshot",
        lambda engine: dict(baseline or INTEGRITY_BASELINE),
    )


def _automatic_engine():
    """Seed a small graph with two stale meeting_member provenance refs."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE meeting_members (id INTEGER PRIMARY KEY, name TEXT)",
            "CREATE TABLE pz_item_details (id INTEGER PRIMARY KEY, name TEXT)",
            """CREATE TABLE entity_relationships (
                   id INTEGER PRIMARY KEY,
                   from_entity_id INTEGER NOT NULL,
                   relationship TEXT NOT NULL,
                   to_entity_id INTEGER NOT NULL,
                   provenance_type TEXT NOT NULL,
                   provenance_id INTEGER NOT NULL,
                   updated_at TEXT
               )""",
            """CREATE TABLE entity_mentions (
                   id INTEGER PRIMARY KEY,
                   entity_id INTEGER NOT NULL,
                   source_type TEXT NOT NULL,
                   source_id INTEGER NOT NULL,
                   role_in_context TEXT,
                   extracted_by TEXT NOT NULL,
                   updated_at TEXT
               )""",
        ):
            connection.execute(text(statement))
        connection.execute(
            text("INSERT INTO meeting_members (id, name) VALUES (100, 'Current Body')")
        )
        connection.execute(text("""
            INSERT INTO entity_relationships
                (id, from_entity_id, relationship, to_entity_id,
                 provenance_type, provenance_id, updated_at)
            VALUES
                (1, 10, 'PRESENT_AT', 20, 'meeting_member', 901, NULL),
                (2, 11, 'PRESENT_AT', 20, 'meeting_member', 902, NULL),
                (3, 10, 'PRESENT_AT', 20, 'meeting_member', 100, NULL)
        """))
        connection.execute(text("""
            INSERT INTO entity_mentions
                (id, entity_id, source_type, source_id,
                 role_in_context, extracted_by, updated_at)
            VALUES
                (11, 10, 'meeting_member', 901, 'PRESENT_AT', 'graph_builder', NULL),
                (12, 11, 'meeting_member', 902, 'PRESENT_AT', 'graph_builder', NULL),
                (13, 10, 'meeting_member', 100, 'PRESENT_AT', 'graph_builder', NULL)
        """))
    return engine


def _build_automatic_plan(engine) -> dict[str, object]:
    """Mirror ``build_consolidation_plan`` on the isolated fixture schema."""
    replacement_ids = {
        ("meeting_member", 901): 100,
        ("meeting_member", 902): 100,
    }
    with engine.connect() as connection:
        stale_relationships = _rows_as_dicts(
            connection,
            "SELECT * FROM entity_relationships WHERE id IN :ids ORDER BY id",
            [1, 2],
        )
        stale_mentions = _rows_as_dicts(
            connection,
            "SELECT * FROM entity_mentions WHERE id IN :ids ORDER BY id",
            [11, 12],
        )
        current_relationships = _rows_as_dicts(
            connection,
            """SELECT * FROM entity_relationships
               WHERE provenance_type='meeting_member'
                 AND provenance_id IN :ids ORDER BY id""",
            [100],
        )
        current_mentions = _rows_as_dicts(
            connection,
            """SELECT * FROM entity_mentions
               WHERE source_type='meeting_member'
                 AND source_id IN :ids ORDER BY id""",
            [100],
        )
        replacement_sources = {
            "meeting_members": _rows_as_dicts(
                connection, "SELECT * FROM meeting_members WHERE id IN :ids ORDER BY id",
                [100],
            ),
            "pz_item_details": [],
        }
    relationship_operations = [
        operation.__dict__
        for operation in _plan_operations(
            stale_relationships, current_relationships, replacement_ids,
            row_kind="relationship",
        )
    ]
    mention_operations = [
        operation.__dict__
        for operation in _plan_operations(
            stale_mentions, current_mentions, replacement_ids, row_kind="mention",
        )
    ]
    current_survivor_ids = {
        "entity_relationships": sorted({
            int(operation["survivor_id"])
            for operation in relationship_operations
            if operation["survivor_is_current"] and operation["survivor_id"] is not None
        }),
        "entity_mentions": sorted({
            int(operation["survivor_id"])
            for operation in mention_operations
            if operation["survivor_is_current"] and operation["survivor_id"] is not None
        }),
    }
    current_survivors = {
        "entity_relationships": [
            row for row in current_relationships
            if int(row["id"]) in current_survivor_ids["entity_relationships"]
        ],
        "entity_mentions": [
            row for row in current_mentions
            if int(row["id"]) in current_survivor_ids["entity_mentions"]
        ],
    }
    backup = {
        "entity_relationships": stale_relationships,
        "entity_mentions": stale_mentions,
    }
    preserved_row_ids = {
        "entity_relationships": sorted(
            int(row["id"]) for row in stale_relationships
            if int(row["id"]) not in _operation_target_ids(relationship_operations)
        ),
        "entity_mentions": sorted(
            int(row["id"]) for row in stale_mentions
            if int(row["id"]) not in _operation_target_ids(mention_operations)
        ),
    }
    plan = {
        "plan_id": "test-automatic-plan",
        "generated_at": "2026-09-09T00:00:00-07:00",
        "plan_kind": "automatic_consolidation",
        "pre_operation_integrity": dict(INTEGRITY_BASELINE),
        "unresolved_relationship_ids": [1, 2],
        "classification": {
            "repairable": {
                "count": 2,
                "relationship_ids": [1, 2],
                "source_references": [
                    ["meeting_member", 901], ["meeting_member", 902],
                ],
            },
            "ambiguous": {"count": 0, "relationship_ids": [], "source_references": []},
            "unmatched": {"count": 0, "relationship_ids": [], "source_references": []},
        },
        "backup": backup,
        "backup_sha256": {
            table: _rows_fingerprint(rows) for table, rows in backup.items()
        },
        "current_survivors": current_survivors,
        "current_survivors_sha256": {
            table: _rows_fingerprint(rows)
            for table, rows in current_survivors.items()
        },
        "replacement_sources": replacement_sources,
        "replacement_sources_sha256": {
            table: _rows_fingerprint(rows)
            for table, rows in replacement_sources.items()
        },
        "preserved_row_ids": preserved_row_ids,
        "relationship_operations": relationship_operations,
        "mention_operations": mention_operations,
        "expected_operation_counts": {
            "relationships": {
                "updated": sum(
                    not bool(operation["survivor_is_current"])
                    for operation in relationship_operations
                ),
                "deleted": sum(
                    len(operation["delete_ids"])
                    for operation in relationship_operations
                ),
            },
            "mentions": {
                "updated": sum(
                    not bool(operation["survivor_is_current"])
                    for operation in mention_operations
                ),
                "deleted": sum(
                    len(operation["delete_ids"]) for operation in mention_operations
                ),
            },
        },
        "expected_unresolved_relationship_ids_after": [],
        "summary": {
            "relationship_rows_backed_up": len(stale_relationships),
            "relationship_rows_to_delete": sum(
                len(operation["delete_ids"])
                for operation in relationship_operations
            ),
            "relationship_rows_to_repoint": sum(
                not bool(operation["survivor_is_current"])
                for operation in relationship_operations
            ),
            "mention_rows_backed_up": len(stale_mentions),
            "mention_rows_to_delete": sum(
                len(operation["delete_ids"]) for operation in mention_operations
            ),
            "mention_rows_to_repoint": sum(
                not bool(operation["survivor_is_current"])
                for operation in mention_operations
            ),
        },
    }
    plan["plan_sha256"] = _plan_fingerprint(plan)
    return plan


def _expect_no_backup(path) -> None:
    assert path is None or not os.path.exists(path)


# --------------------------------------------------------------------------
# Planning primitives
# --------------------------------------------------------------------------


def test_existing_current_relationship_survives_and_stale_rows_are_deleted():
    stale = [_relationship(1, 91), _relationship(2, 92)]
    current = [_relationship(3, 100)]
    operations = _plan_operations(
        stale,
        current,
        {("meeting_member", 91): 100, ("meeting_member", 92): 100},
        row_kind="relationship",
    )

    assert len(operations) == 1
    assert operations[0].survivor_id == 3
    assert operations[0].survivor_is_current is True
    assert operations[0].delete_ids == (1, 2)


def test_one_stale_relationship_is_promoted_when_no_current_row_exists():
    stale = [_relationship(4, 91), _relationship(2, 92)]
    operations = _plan_operations(
        stale,
        [],
        {("meeting_member", 91): 100, ("meeting_member", 92): 100},
        row_kind="relationship",
    )

    assert len(operations) == 1
    assert operations[0].survivor_id == 2
    assert operations[0].survivor_is_current is False
    assert operations[0].replacement_source_id == 100
    assert operations[0].delete_ids == (4,)


def test_mentions_use_role_and_extractor_in_canonical_identity():
    stale = [
        {
            "id": 1,
            "entity_id": 10,
            "source_type": "meeting_member",
            "source_id": 91,
            "role_in_context": "PRESENT_AT",
            "extracted_by": "graph_builder",
        },
        {
            "id": 2,
            "entity_id": 10,
            "source_type": "meeting_member",
            "source_id": 92,
            "role_in_context": "MEMBER_OF",
            "extracted_by": "graph_builder",
        },
    ]
    operations = _plan_operations(
        stale,
        [],
        {("meeting_member", 91): 100, ("meeting_member", 92): 100},
        row_kind="mention",
    )

    assert len(operations) == 2
    assert all(operation.delete_ids == () for operation in operations)


def test_backup_fingerprint_changes_when_a_row_changes():
    original = [_relationship(1, 91)]
    changed = [_relationship(1, 92)]

    assert _rows_fingerprint(original) != _rows_fingerprint(changed)


def test_operation_targets_include_promoted_survivors_and_deletions():
    operations = [
        {
            "survivor_id": 2,
            "survivor_is_current": False,
            "delete_ids": [3, 4],
        },
        {
            "survivor_id": 10,
            "survivor_is_current": True,
            "delete_ids": [5],
        },
    ]

    assert _operation_target_ids(operations) == {2, 3, 4, 5}


def test_plan_fingerprint_is_stable_across_volatile_fields():
    plan = _build_automatic_plan(_automatic_engine())
    first = plan["plan_sha256"]
    variant = copy.deepcopy(plan)
    variant["plan_id"] = "another-id"
    variant["generated_at"] = "2026-09-10T00:00:00-07:00"
    assert _plan_fingerprint(variant) == first


def test_plan_fingerprint_changes_when_evidence_changes():
    plan = _build_automatic_plan(_automatic_engine())
    first = plan["plan_sha256"]
    variant = copy.deepcopy(plan)
    variant["unresolved_relationship_ids"] = [1]
    assert _plan_fingerprint(variant) != first


# --------------------------------------------------------------------------
# Automatic apply: digest and path gates
# --------------------------------------------------------------------------


def test_automatic_apply_requires_approved_digest(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="approved plan SHA-256"):
        apply_consolidation(
            engine, plan,
            automatic_backup_path=tmp_path / "backup.json",
        )


def test_automatic_apply_rejects_incorrect_digest(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="does not match"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256="0" * 64,
            automatic_backup_path=tmp_path / "backup.json",
        )
    _expect_no_backup(tmp_path / "backup.json")


def test_automatic_apply_rejects_tampered_plan(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    plan["relationship_operations"][0]["delete_ids"] = [999]
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="fingerprint is invalid"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    _expect_no_backup(tmp_path / "backup.json")


def test_automatic_apply_requires_backup_path(monkeypatch):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="pre-operation backup path"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
        )


def test_automatic_apply_rejects_non_json_backup_path(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match=r"\.json suffix"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.txt",
        )


def test_automatic_apply_rejects_existing_backup_path(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    backup_path = tmp_path / "backup.json"
    backup_path.write_text("{}", encoding="utf-8")
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="new file"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=backup_path,
        )


def test_automatic_apply_rejects_backup_equal_to_saved_plan(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    saved_plan_path = tmp_path / "plan.json"
    saved_plan_path.write_text(json.dumps(plan), encoding="utf-8")
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="must not equal the saved plan path"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=saved_plan_path,
            saved_plan_path=saved_plan_path,
        )


def test_automatic_apply_protects_full_postgresql_dump(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    dump_path = apply_module._PROTECTED_FULL_DUMP
    stat_before = os.stat(dump_path)
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="protected PostgreSQL dump"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=dump_path,
        )
    stat_after = os.stat(dump_path)
    assert (stat_before.st_size, stat_before.st_mtime_ns) == (
        stat_after.st_size, stat_after.st_mtime_ns,
    )


def test_automatic_apply_rejects_changed_integrity_baseline(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    shifted = dict(INTEGRITY_BASELINE)
    shifted["unresolved_relationship_provenance"] = 7
    _patch_integrity(monkeypatch, baseline=shifted)
    with pytest.raises(RuntimeError, match="integrity baseline changed"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    _expect_no_backup(tmp_path / "backup.json")


# --------------------------------------------------------------------------
# Automatic apply: mutable-evidence drift gates
# --------------------------------------------------------------------------


def test_automatic_apply_rejects_stale_row_drift(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE entity_relationships SET to_entity_id=99 WHERE id=1"
        ))
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="changed after the consolidation plan"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    _expect_no_backup(tmp_path / "backup.json")


def test_automatic_apply_rejects_survivor_drift(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE entity_relationships SET to_entity_id=99 WHERE id=3"
        ))
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="survivor changed after"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    _expect_no_backup(tmp_path / "backup.json")


def test_automatic_apply_rejects_replacement_source_drift(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE meeting_members SET name='Changed' WHERE id=100"
        ))
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="replacement meeting_members changed"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    _expect_no_backup(tmp_path / "backup.json")


def test_automatic_apply_rejects_changed_unresolved_id_set(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO entity_relationships
                (id, from_entity_id, relationship, to_entity_id,
                 provenance_type, provenance_id, updated_at)
            VALUES (9, 12, 'PRESENT_AT', 20, 'meeting_member', 903, NULL)
        """))
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="unresolved relationship set differs"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    _expect_no_backup(tmp_path / "backup.json")


# --------------------------------------------------------------------------
# Automatic apply: backup artifact ordering and content
# --------------------------------------------------------------------------


def test_automatic_apply_writes_backup_artifact_before_mutation(
    monkeypatch, tmp_path,
):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    backup_path = tmp_path / "preapply.json"
    _patch_integrity(monkeypatch)
    original_apply = apply_module._apply_operations

    def _spy(connection, operations, *, table, source_id_column):
        assert backup_path.exists(), "backup artifact missing before mutation"
        return original_apply(connection, operations, table=table,
                              source_id_column=source_id_column)

    monkeypatch.setattr(apply_module, "_apply_operations", _spy)
    result = apply_consolidation(
        engine, plan,
        expected_plan_sha256=plan["plan_sha256"],
        automatic_backup_path=backup_path,
    )
    assert result["relationships_deleted"] == 1
    assert result["relationships_updated"] == 1
    artifact = json.loads(backup_path.read_text(encoding="utf-8"))
    assert artifact["artifact_kind"] == "automatic_consolidation_preapply_backup"
    assert artifact["plan_sha256"] == plan["plan_sha256"]
    assert artifact["backup_sha256"] == plan["backup_sha256"]
    assert artifact["restore_instructions"][0]["table"] == "entity_relationships"
    assert artifact["restore_instructions"][0]["reinsert_original_rows"] == [1]
    assert artifact["restore_instructions"][0]["reset_promoted_survivors"] == [
        {"id": 2, "original_source_value": 902}
    ]


# --------------------------------------------------------------------------
# Automatic apply: successful atomic application
# --------------------------------------------------------------------------


def test_automatic_apply_success_commits_exact_mutations(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    _patch_integrity(monkeypatch)
    backup_path = tmp_path / "preapply.json"
    result = apply_consolidation(
        engine, plan,
        expected_plan_sha256=plan["plan_sha256"],
        automatic_backup_path=backup_path,
    )

    assert result["relationships_deleted"] == 1
    assert result["relationships_updated"] == 1
    assert result["mentions_deleted"] == 1
    assert result["mentions_updated"] == 1
    assert result["before_integrity"] == INTEGRITY_BASELINE
    with engine.connect() as connection:
        remaining = connection.execute(text(
            "SELECT id, provenance_id FROM entity_relationships "
            "WHERE id IN (1, 2) ORDER BY id"
        )).fetchall()
        assert [(row[0], row[1]) for row in remaining] == [(2, 100)]
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=3"
        )).scalar_one() == 100
        mention_sources = connection.execute(text(
            "SELECT id, source_id FROM entity_mentions "
            "WHERE id IN (11, 12) ORDER BY id"
        )).fetchall()
        assert [(row[0], row[1]) for row in mention_sources] == [(12, 100)]
        unresolved_after = connection.execute(text("""
            SELECT COUNT(*) FROM entity_relationships r
            WHERE (r.provenance_type = 'meeting_member' AND NOT EXISTS (
                    SELECT 1 FROM meeting_members s WHERE s.id = r.provenance_id))
               OR (r.provenance_type = 'pz_item_detail' AND NOT EXISTS (
                    SELECT 1 FROM pz_item_details s WHERE s.id = r.provenance_id))
        """)).scalar_one()
    assert unresolved_after == 0
    assert backup_path.exists()


def test_automatic_apply_rejects_operation_count_mismatch(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    plan["expected_operation_counts"]["relationships"]["deleted"] = 5
    # Keep the digest honest so validation reaches the in-transaction count gate.
    plan["plan_sha256"] = _plan_fingerprint(plan)
    _patch_integrity(monkeypatch)
    with pytest.raises(RuntimeError, match="operation counts differ"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=1"
        )).scalar_one() == 901


def test_automatic_apply_rejects_failed_delete_postcondition(monkeypatch, tmp_path):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    _patch_integrity(monkeypatch)

    def _noop_delete(connection, operations, *, table, source_id_column):
        # Pretend the work happened so the count gate passes; because nothing
        # was actually deleted the delete postcondition must fail loudly.
        return (1, 1)

    monkeypatch.setattr(apply_module, "_apply_operations", _noop_delete)
    with pytest.raises(RuntimeError, match="delete postcondition failed"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=1"
        )).scalar_one() == 901


def test_automatic_apply_rejects_failed_repoint_postcondition(
    monkeypatch, tmp_path,
):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    _patch_integrity(monkeypatch)
    def _partial_apply(connection, operations, *, table, source_id_column):
        # Delete stale rows but never perform the survivor repoint.
        delete_ids = [
            int(value) for operation in operations
            for value in operation["delete_ids"]
        ]
        if delete_ids:
            statement = text(f"DELETE FROM {table} WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            )
            connection.execute(statement, {"ids": delete_ids})
        return (1, 1)

    monkeypatch.setattr(apply_module, "_apply_operations", _partial_apply)
    with pytest.raises(RuntimeError, match="repoint postcondition failed"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=1"
        )).scalar_one() == 901


def test_automatic_apply_rolls_back_on_injected_mid_transaction_failure(
    monkeypatch, tmp_path,
):
    engine = _automatic_engine()
    plan = _build_automatic_plan(engine)
    _patch_integrity(monkeypatch)

    def _boom(connection, operations, *, table, source_id_column):
        connection.execute(text("DELETE FROM entity_relationships WHERE id=1"))
        raise RuntimeError("injected mid-transaction failure")

    monkeypatch.setattr(apply_module, "_apply_operations", _boom)
    with pytest.raises(RuntimeError, match="injected mid-transaction failure"):
        apply_consolidation(
            engine, plan,
            expected_plan_sha256=plan["plan_sha256"],
            automatic_backup_path=tmp_path / "backup.json",
        )
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=1"
        )).scalar_one() == 901
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=2"
        )).scalar_one() == 902
        assert connection.execute(text(
            "SELECT source_id FROM entity_mentions WHERE id=12"
        )).scalar_one() == 902


def test_legacy_apply_rejects_stale_database_rows_before_mutation(monkeypatch):
    """Reject a plan whose backed-up relationship row changed after planning."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE entity_relationships (
                id INTEGER PRIMARY KEY,
                from_entity_id INTEGER NOT NULL,
                relationship TEXT NOT NULL,
                to_entity_id INTEGER NOT NULL,
                provenance_type TEXT NOT NULL,
                provenance_id INTEGER NOT NULL,
                updated_at TEXT
            )
        """))
        connection.execute(text("""
            INSERT INTO entity_relationships
                (id, from_entity_id, relationship, to_entity_id,
                 provenance_type, provenance_id, updated_at)
            VALUES (1, 10, 'PRESENT_AT', 20, 'meeting_member', 92, NULL)
        """))

    planned_row = _relationship(1, 91)
    plan = {
        "backup": {
            "entity_relationships": [planned_row],
            "entity_mentions": [],
        },
        "backup_sha256": {
            "entity_relationships": _rows_fingerprint([planned_row]),
            "entity_mentions": _rows_fingerprint([]),
        },
        "relationship_operations": [{
            "survivor_id": 1,
            "survivor_is_current": False,
            "replacement_source_id": 100,
            "delete_ids": [],
        }],
        "mention_operations": [],
        "summary": {"relationship_rows_backed_up": 1},
    }
    _patch_integrity(monkeypatch, baseline={
        "unresolved_relationship_provenance": 1,
    })

    try:
        apply_consolidation(engine, plan)
    except RuntimeError as error:
        assert str(error) == "entity_relationships changed after the consolidation plan was created"
    else:
        raise AssertionError("stale consolidation plan was unexpectedly applied")

    with engine.connect() as connection:
        provenance_id = connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=1"
        )).scalar_one()
    assert provenance_id == 92


def test_legacy_apply_rejects_tampered_human_plan_fingerprint(monkeypatch):
    engine = _human_adjudication_engine()
    plan = build_human_adjudication_plan(engine, _human_adjudication())
    plan["relationship_operations"] = [
        {
            "survivor_id": 10,
            "survivor_is_current": True,
            "replacement_source_id": 201,
            "delete_ids": [1, 999],
        }
    ]
    _patch_integrity(monkeypatch, baseline={
        "unresolved_relationship_provenance": 4,
    })
    with pytest.raises(RuntimeError, match="fingerprint is invalid"):
        apply_consolidation(engine, plan)


# --------------------------------------------------------------------------
# Human adjudication behavior (regression)
# --------------------------------------------------------------------------


def _human_adjudication_engine():
    """Build a small isolated graph exercising partial source adjudication."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE pz_item_details (id INTEGER PRIMARY KEY)
        """))
        connection.execute(text("""
            CREATE TABLE meeting_members (id INTEGER PRIMARY KEY)
        """))
        connection.execute(text("""
            CREATE TABLE entity_relationships (
                id INTEGER PRIMARY KEY,
                from_entity_id INTEGER NOT NULL,
                relationship TEXT NOT NULL,
                to_entity_id INTEGER NOT NULL,
                provenance_type TEXT NOT NULL,
                provenance_id INTEGER NOT NULL,
                updated_at TEXT
            )
        """))
        connection.execute(text("""
            CREATE TABLE entity_mentions (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                role_in_context TEXT,
                extracted_by TEXT NOT NULL,
                updated_at TEXT
            )
        """))
        connection.execute(text(
            "INSERT INTO pz_item_details (id) VALUES "
            "(201), (202)"
        ))
        connection.execute(text("INSERT INTO meeting_members (id) VALUES (301)"))
        connection.execute(text("""
            INSERT INTO entity_relationships
                (id, from_entity_id, relationship, to_entity_id,
                 provenance_type, provenance_id, updated_at)
            VALUES
                (1, 10, 'PRESENT_AT', 20, 'pz_item_detail', 101, NULL),
                (2, 11, 'MEMBER_OF', 21, 'pz_item_detail', 101, NULL),
                (3, 10, 'PRESENT_AT', 30, 'pz_item_detail', 102, NULL),
                (4, 12, 'PRESENT_AT', 40, 'pz_item_detail', 103, NULL),
                (10, 10, 'PRESENT_AT', 20, 'pz_item_detail', 201, NULL)
        """))
        connection.execute(text("""
            INSERT INTO entity_mentions
                (id, entity_id, source_type, source_id,
                 role_in_context, extracted_by, updated_at)
            VALUES
                (20, 10, 'pz_item_detail', 201, 'PRESENT_AT', 'graph_builder', NULL),
                (21, 10, 'pz_item_detail', 202, 'PRESENT_AT', 'graph_builder', NULL),
                (30, 10, 'pz_item_detail', 101, 'PRESENT_AT', 'graph_builder', NULL),
                (31, 11, 'pz_item_detail', 101, 'MEMBER_OF', 'graph_builder', NULL),
                (32, 10, 'pz_item_detail', 102, 'PRESENT_AT', 'graph_builder', NULL),
                (33, 12, 'pz_item_detail', 103, 'PRESENT_AT', 'graph_builder', NULL)
        """))
    return engine


def _human_adjudication() -> dict[str, object]:
    """Cover every relationship exactly once, including a partial source group."""
    return {
        "version": 1,
        "rule": "earliest qualifying source occurrence wins",
        "approved_by": "test-adjudicator",
        "source_assignments": [
            {
                "stale_source_ids": [101],
                "replacement_source_id": 201,
                "relationship_ids": [1],
                "reason": "Earliest source supporting this edge",
                "evidence_urls": ["https://example.test/source/201"],
            },
            {
                "stale_source_ids": [102],
                "replacement_source_id": 202,
                "relationship_ids": [3],
                "reason": "Earliest source supporting this edge",
                "evidence_urls": ["https://example.test/source/202"],
            },
        ],
        "rejected_relationships": [
            {"relationship_ids": [2], "reason": "Unsupported extraction"},
            {"relationship_ids": [4], "reason": "Unsupported extraction"},
        ],
        "rejected_mentions": [
            {"mention_ids": [33], "reason": "Unsupported source-only mention"},
        ],
    }


def test_human_plan_requires_exact_relationship_coverage_without_duplicates_or_extras():
    engine = _human_adjudication_engine()
    valid = _human_adjudication()
    plan = build_human_adjudication_plan(engine, valid)

    assert {int(row["id"]) for row in plan["backup"]["entity_relationships"]} == {1, 2, 3, 4}

    duplicate = copy.deepcopy(valid)
    duplicate["source_assignments"][0]["relationship_ids"].append(1)
    with pytest.raises(ValueError, match="coverage|duplicate|more than one|exact"):
        build_human_adjudication_plan(engine, duplicate)

    extra = copy.deepcopy(valid)
    extra["rejected_relationships"].append(
        {"relationship_ids": [999], "reason": "Not in the reviewed set"}
    )
    with pytest.raises(ValueError, match="coverage|unknown|exact"):
        build_human_adjudication_plan(engine, extra)


def test_human_plan_accepts_partial_stale_source_group_and_preserves_shared_mentions():
    plan = build_human_adjudication_plan(_human_adjudication_engine(), _human_adjudication())

    # Source 101 has two relationships, but only relationship 1 is assigned;
    # relationship 2 is explicitly rejected.  The source must not be migrated
    # wholesale, and the mention belonging to the rejected edge is preserved.
    assert plan["summary"]["relationship_rows_backed_up"] == 4
    assert plan["summary"]["relationship_rows_to_repoint"] == 1
    assert plan["summary"]["relationship_rows_rejected"] == 2
    assert plan["summary"]["mention_rows_backed_up"] == 4
    mention_operations = plan["mention_operations"]
    assert any(30 in operation["delete_ids"] for operation in mention_operations)
    shared = next(operation for operation in mention_operations if operation["survivor_id"] == 31)
    assert shared["delete_ids"] == ()


def test_human_plan_can_explicitly_reject_meeting_member_relationship_and_mention():
    engine = _human_adjudication_engine()
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO entity_relationships
                (id, from_entity_id, relationship, to_entity_id,
                 provenance_type, provenance_id, updated_at)
            VALUES (5, 14, 'PRESENT_AT', 50, 'meeting_member', 999, NULL)
        """))
        connection.execute(text("""
            INSERT INTO entity_mentions
                (id, entity_id, source_type, source_id,
                 role_in_context, extracted_by, updated_at)
            VALUES (34, 14, 'meeting_member', 999,
                    'PRESENT_AT', 'graph_builder', NULL)
        """))
    adjudication = _human_adjudication()
    adjudication["rejected_relationships"].append({
        "relationship_ids": [5],
        "reason": "Unsupported cross-jurisdiction attendance assertion",
    })
    adjudication["rejected_mentions"].append({
        "mention_ids": [34],
        "reason": "Mention solely supports the rejected attendance assertion",
    })

    plan = build_human_adjudication_plan(engine, adjudication)

    assert any(5 in operation["delete_ids"] for operation in plan["relationship_operations"])
    assert any(34 in operation["delete_ids"] for operation in plan["mention_operations"])
    assert {row["id"] for row in plan["backup"]["entity_mentions"]} == {30, 31, 32, 33, 34}


def test_human_plan_requires_rejection_reason_and_explicit_allowlist():
    engine = _human_adjudication_engine()
    missing_reason = _human_adjudication()
    del missing_reason["rejected_relationships"][0]["reason"]
    with pytest.raises(ValueError, match="reason"):
        build_human_adjudication_plan(engine, missing_reason)

    implicit_rejection = _human_adjudication()
    implicit_rejection["rejected_relationships"] = implicit_rejection[
        "rejected_relationships"
    ][:1]
    with pytest.raises(ValueError, match="cover|coverage|explicit|allowlist"):
        build_human_adjudication_plan(engine, implicit_rejection)


def test_human_plan_validates_replacement_source_exists_and_has_expected_type():
    engine = _human_adjudication_engine()
    missing = _human_adjudication()
    missing["source_assignments"][0]["replacement_source_id"] = 999
    with pytest.raises(ValueError, match="source|target|exist"):
        build_human_adjudication_plan(engine, missing)

    wrong_type = _human_adjudication()
    wrong_type["source_assignments"][0]["replacement_source_id"] = 301
    with pytest.raises(ValueError, match="type|pz_item_detail|source"):
        build_human_adjudication_plan(engine, wrong_type)


def test_human_plan_binds_declared_stale_sources_to_relationship_rows():
    engine = _human_adjudication_engine()
    mismatched = _human_adjudication()
    mismatched["source_assignments"][0]["stale_source_ids"] = [999]

    with pytest.raises(ValueError, match="stale_source_ids|match"):
        build_human_adjudication_plan(engine, mismatched)


def test_human_plan_is_deterministic_and_accounts_for_relationships_and_mentions():
    engine = _human_adjudication_engine()
    first = build_human_adjudication_plan(engine, _human_adjudication())
    second = build_human_adjudication_plan(engine, _human_adjudication())

    for key in (
        "backup",
        "backup_sha256",
        "relationship_operations",
        "mention_operations",
        "summary",
    ):
        assert first[key] == second[key]
    assert first["summary"] == {
        "relationship_rows_backed_up": 4,
        "relationship_rows_to_delete": 3,
        "relationship_rows_to_repoint": 1,
        "relationship_rows_rejected": 2,
        "mention_rows_backed_up": 4,
        "mention_rows_rejected": 1,
        "mention_rows_to_delete": 3,
        "mention_rows_to_repoint": 1,
    }


def test_human_plan_apply_rejects_stale_backup_fingerprint(monkeypatch):
    engine = _human_adjudication_engine()
    plan = build_human_adjudication_plan(engine, _human_adjudication())
    _patch_integrity(monkeypatch, baseline={
        "unresolved_relationship_provenance": 4,
    })
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE entity_relationships SET to_entity_id=999 WHERE id=1"
        ))

    with pytest.raises(RuntimeError, match="changed after the consolidation plan"):
        apply_consolidation(engine, plan)


def test_human_plan_apply_rejects_changed_replacement_source(monkeypatch):
    engine = _human_adjudication_engine()
    plan = build_human_adjudication_plan(engine, _human_adjudication())
    _patch_integrity(monkeypatch, baseline={
        "unresolved_relationship_provenance": 4,
    })
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM pz_item_details WHERE id=201"))

    with pytest.raises(RuntimeError, match="replacement P&Z source changed"):
        apply_consolidation(engine, plan)

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=1"
        )).scalar_one() == 101


def test_human_plan_apply_success_commits_exact_mutations(monkeypatch):
    engine = _human_adjudication_engine()
    plan = build_human_adjudication_plan(engine, _human_adjudication())
    _patch_integrity(monkeypatch, baseline={
        "unresolved_relationship_provenance": 4,
    })
    result = apply_consolidation(engine, plan)

    assert result["relationships_deleted"] == 3
    assert result["relationships_updated"] == 1
    assert result["mentions_deleted"] == 3
    assert result["mentions_updated"] == 1
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=3"
        )).scalar_one() == 202
        assert connection.execute(text(
            "SELECT provenance_id FROM entity_relationships WHERE id=10"
        )).scalar_one() == 201
        leftover = connection.execute(text(
            "SELECT COUNT(*) FROM entity_relationships WHERE id IN (1, 2, 4)"
        )).scalar_one()
        assert leftover == 0


# --------------------------------------------------------------------------
# CLI regression: automatic --apply shortcut is rejected
# --------------------------------------------------------------------------


def test_main_rejects_automatic_apply_shortcut(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["provenance_consolidation.py", "--apply"])
    with pytest.raises(ValueError, match="--apply is disabled"):
        consolidation.main()
