#!/usr/bin/env python3
"""Adversarial tests for the reservation contract, governed collision control, and
the additive schema apply.

Each safety-critical property gets an attack: a reservation table with the wrong
primary key, the wrong type, a nullable key, a missing or unvalidated foreign key;
a writer with neither a unique index nor a reservation; a plan whose DDL was edited,
whose bound code drifted, or whose target is not development; an apply that would
run twice; a replay that would write.
"""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_reservation as RES  # noqa: E402
from scripts.kg import stage2_reservation_apply as AP  # noqa: E402
from scripts.kg import stage2_reservation_plan as PL  # noqa: E402
from scripts.kg import stage2_s2_collision as COL  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


def plan():
    return copy.deepcopy(artifacts.load_verified(
        _live("kg-stage2-reservation-schema-plan-*.json")))


# ══ the declared DDL is the contract ═══════════════════════════════════

def test_the_ddl_is_one_additive_create_table():
    ddl = RES.reservation_ddl()
    assert len(ddl) == 1
    assert ddl[0].startswith(f"CREATE TABLE {RES.RESERVATION_TABLE} (")
    for forbidden in ("ALTER TABLE", "DROP", "CREATE INDEX"):
        assert forbidden not in ddl[0]


def test_the_declared_contract_names_the_exact_primary_key():
    assert RES.RESERVATION_TABLE == "agenda_item_key_reservation"
    assert "(meeting_db_id, agenda_item_number)" in RES.DDL[0]
    assert RES.PK_NAME in RES.DDL[0]


def test_the_key_column_matches_agenda_items_exactly():
    """A narrower column could refuse a key the item itself would accept."""
    key = next(c for c in RES.COLUMNS if c["name"] == "agenda_item_number")
    assert key["type"] == "character varying(32)"
    assert key["nullable"] is False


def test_the_foreign_key_cascades_from_meetings_only():
    column = next(c for c in RES.COLUMNS if c["name"] == "meeting_db_id")
    assert column["references"]["table"] == "meetings"
    assert column["references"]["on_delete"] == "CASCADE"


def test_the_table_has_no_foreign_key_to_agenda_items():
    """A reservation is made before the item exists, so that FK is unsatisfiable."""
    assert "REFERENCES agenda_items" not in RES.DDL[0]


# ══ a real fixture exercises the proof and its refusals ════════════════

class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def mappings(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        return self._scalar


class _FakeCatalog:
    """A fake PostgreSQL connection returning canned catalogue answers.

    The reservation readers are PostgreSQL-native by design — they read
    ``information_schema`` and ``pg_constraint`` — so the adversarial cases are
    driven through a fake catalogue rather than pretending SQLite is PostgreSQL.
    """

    class dialect:
        name = "postgresql"

    def __init__(self, *, table_exists=True, columns=None, primary=None,
                 foreign=None, item_collation=None):
        self._exists = table_exists
        self._columns = columns if columns is not None else [
            {"column_name": "meeting_db_id", "data_type": "integer",
             "character_maximum_length": None, "is_nullable": "NO",
             "collation_name": None},
            {"column_name": "agenda_item_number", "data_type": "character varying",
             "character_maximum_length": 32, "is_nullable": "NO",
             "collation_name": None},
            {"column_name": "plan_digest", "data_type": "character varying",
             "character_maximum_length": 64, "is_nullable": "NO",
             "collation_name": None},
            {"column_name": "reserved_at", "data_type": "timestamp with time zone",
             "character_maximum_length": None, "is_nullable": "NO",
             "collation_name": None},
            {"column_name": "reserved_by", "data_type": "text",
             "character_maximum_length": None, "is_nullable": "NO",
             "collation_name": None},
        ]
        self._primary = primary if primary is not None else [
            "meeting_db_id", "agenda_item_number"]
        self._foreign = foreign if foreign is not None else [
            {"referred": "meetings", "col": "id", "del": "c", "upd": "a",
             "validated": True}]
        self._item_collation = item_collation

    def execute(self, statement, params=None):
        sql = str(statement)
        if "information_schema.tables" in sql:
            return _Result(scalar=1 if self._exists else 0)
        if "information_schema.columns" in sql and "agenda_items" in sql:
            return _Result(scalar=self._item_collation)
        if "information_schema.columns" in sql:
            return _Result(rows=self._columns)
        if "contype = 'p'" in sql:
            return _Result(scalar=None, rows=self._primary)
        if "contype = 'f'" in sql:
            return _Result(rows=self._foreign)
        return _Result()


def _conforming():
    return _FakeCatalog()


def test_a_conforming_unique_index_is_accepted_as_governed_control():
    found = COL.verify_collision_control(_conforming())
    assert found["mode"] == "exact_key_reservation"
    assert found["invariant_carrier"] == "primary key"
    assert found["historical_key_is_unique"] is False


def test_a_writer_with_neither_control_is_refused():
    catalog = _FakeCatalog(table_exists=False)
    with pytest.raises(COL.CollisionRefused) as exc:
        COL.verify_collision_control(catalog)
    assert "no governed collision control" in str(exc.value)


def test_a_missing_reservation_table_is_reported_not_assumed():
    assert RES.read_reservation_signature(_FakeCatalog(table_exists=False)) is None


def test_the_governed_contract_declares_its_non_guarantees():
    contract = COL.governed_contract("postgresql")
    assert contract["invariant_carrier"] == "the reservation primary key"
    assert any("not constrained" in g for g in contract["non_guarantees"])
    assert "GOVERNED WRITERS ONLY" in contract["scope"]


def _columns_with(**overrides):
    columns = _conforming()._columns
    out = []
    for column in columns:
        out.append({**column, **overrides.get(column["column_name"], {})})
    return out


@pytest.mark.parametrize("kwargs,expect", [
    ({"primary": ["meeting_db_id"]}, "primary key"),
    ({"columns": _columns_with(agenda_item_number={"is_nullable": "YES"})},
     "nullable"),
    ({"columns": _columns_with(plan_digest={"data_type": "text",
                                            "character_maximum_length": None})},
     "plan_digest"),
    ({"columns": _columns_with(agenda_item_number={
        "data_type": "character varying", "character_maximum_length": 16})},
     "length"),
    ({"foreign": []}, "foreign key"),
    ({"foreign": [{"referred": "agenda_items", "col": "id", "del": "c", "upd": "a",
                   "validated": True}]}, "targets"),
    ({"foreign": [{"referred": "meetings", "col": "id", "del": "n", "upd": "a",
                   "validated": True}]}, "ON DELETE"),
    ({"foreign": [{"referred": "meetings", "col": "id", "del": "c", "upd": "a",
                   "validated": False}]}, "validated"),
])
def test_a_reservation_table_that_is_not_the_contract_is_refused(kwargs, expect):
    problems = RES.verify_reservation_contract(_FakeCatalog(**kwargs))
    assert problems, kwargs
    assert any(expect in p for p in problems), problems


def test_the_key_collation_must_match_agenda_items():
    """The key must compare the way agenda_items compares it, or the gap reopens."""
    matching = _FakeCatalog(
        item_collation="en_US.UTF-8",
        columns=_columns_with(agenda_item_number={"collation_name": "en_US.UTF-8"}))
    assert RES.verify_reservation_contract(matching) == []
    mismatched = _FakeCatalog(
        item_collation="C",
        columns=_columns_with(agenda_item_number={"collation_name": "en_US.UTF-8"}))
    assert any("collation" in p
               for p in RES.verify_reservation_contract(mismatched))


def test_the_reservation_table_cannot_be_repointed():
    with pytest.raises(COL.CollisionRefused):
        COL.verify_collision_control(_conforming(), reservation_table="somewhere_else")


# ══ reserving keys ═════════════════════════════════════════════════════

def _reserved_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            f"CREATE TABLE {RES.RESERVATION_TABLE} ("
            f"meeting_db_id INTEGER NOT NULL, agenda_item_number TEXT NOT NULL, "
            f"plan_digest TEXT NOT NULL, "
            f"reserved_at TEXT NOT NULL DEFAULT (datetime('now')), "
            f"reserved_by TEXT NOT NULL DEFAULT '', "
            f"PRIMARY KEY (meeting_db_id, agenda_item_number))"))
    return engine


def test_reserving_inserts_and_reports_holds():
    engine = _reserved_engine()
    with engine.begin() as connection:
        first = RES.reserve_keys(connection,
                                 [{"meeting_db_id": 1, "agenda_item_number": "5"},
                                  {"meeting_db_id": 2, "agenda_item_number": "0"}],
                                 plan_digest="d" * 64, reserved_by="Peter Mains")
        assert len(first["inserted"]) == 2 and first["held"] == []
        second = RES.reserve_keys(connection,
                                  [{"meeting_db_id": 1, "agenda_item_number": "5"}],
                                  plan_digest="e" * 64, reserved_by="Peter Mains")
    assert second["inserted"] == []
    assert second["held"][0]["reason"] == "already reserved"
    assert second["held"][0]["by_plan"] == "d" * 64


def test_reserving_without_a_plan_digest_is_refused():
    with _reserved_engine().begin() as connection:
        with pytest.raises(ValueError):
            RES.reserve_keys(connection, [], plan_digest="", reserved_by="P")


def test_reserving_without_an_approver_is_refused():
    with _reserved_engine().begin() as connection:
        with pytest.raises(ValueError):
            RES.reserve_keys(connection, [], plan_digest="d" * 64, reserved_by=" ")


def test_a_second_reservation_of_one_key_is_held_not_written():
    engine = _reserved_engine()
    with engine.begin() as connection:
        RES.reserve_keys(connection, [{"meeting_db_id": 1, "agenda_item_number": "5"}],
                         plan_digest="d" * 64, reserved_by="P")
        again = RES.reserve_keys(connection,
                                 [{"meeting_db_id": 1, "agenda_item_number": "5"}],
                                 plan_digest="e" * 64, reserved_by="P")
        assert again["inserted"] == [] and again["held"]
    with engine.connect() as connection:
        assert connection.execute(text(
            f"SELECT COUNT(*) FROM {RES.RESERVATION_TABLE}")).scalar() == 1


def test_the_primary_key_decides_a_true_race():
    """The pre-check cannot help writers that never see each other; the PK does."""
    engine = _reserved_engine()
    insert = (f"INSERT INTO {RES.RESERVATION_TABLE} (meeting_db_id, agenda_item_number, "
              f"plan_digest, reserved_by) VALUES (1, '5', :d, 'P')")
    with engine.begin() as connection:
        connection.execute(text(insert), {"d": "d" * 64})
    with pytest.raises(Exception):
        with engine.begin() as connection:
            connection.execute(text(insert), {"d": "e" * 64})
    with engine.connect() as connection:
        assert connection.execute(text(
            f"SELECT COUNT(*) FROM {RES.RESERVATION_TABLE}")).scalar() == 1


def test_the_advisory_lock_is_a_no_op_off_postgresql():
    with _reserved_engine().begin() as connection:
        assert RES.lock_key(connection, 1, "5") is None




# ══ the plan is the reviewed object ════════════════════════════════════

def test_the_recorded_plan_validates():
    assert PL.validate_plan(plan()) == []


def test_the_plan_binds_the_target_code_schema_and_justifying_artifacts():
    bindings = plan()["bindings"]
    assert bindings["schema_signature"]["digest"]
    assert bindings["duplicate_investigation"]["digest"]
    assert bindings["collision_contract"]["digest"]
    assert len(bindings["code_hashes"]) == len(PL.CODE_MODULES)


def test_the_plan_binds_every_safety_critical_module():
    for required in ("scripts/kg/stage2_reservation.py",
                     "scripts/kg/stage2_reservation_apply.py",
                     "scripts/kg/stage2_s2_collision.py",
                     "scripts/kg/stage2_s2_admission_tx.py"):
        assert required in PL.CODE_MODULES, required


def test_an_edited_ddl_is_refused():
    data = plan()
    data["ddl"] = [data["ddl"][0].replace("ON DELETE CASCADE", "ON DELETE SET NULL")]
    assert any("DDL" in p for p in PL.validate_plan(data))


def test_dropped_code_hashes_are_refused():
    data = plan()
    data["bindings"]["code_hashes"].pop("scripts/kg/stage2_reservation.py")
    assert any("code hashes do not cover" in p for p in PL.validate_plan(data))


def test_a_non_development_target_is_refused():
    data = plan()
    data["target"]["tier"] = "production"
    assert any("development" in p for p in PL.validate_plan(data))


def test_a_plan_claiming_it_touches_existing_tables_is_refused():
    data = plan()
    data["touches_existing_tables"] = True
    assert PL.validate_plan(data)


def test_a_plan_with_a_write_path_is_refused():
    data = plan()
    data["write_path"] = "present"
    assert any("write path" in p for p in PL.validate_plan(data))


def test_a_wrong_primary_key_declaration_is_refused():
    data = plan()
    data["primary_key"] = ["meeting_db_id"]
    assert any("primary key" in p for p in PL.validate_plan(data))


# ══ the apply: refusals before mutation ════════════════════════════════

def test_a_plan_digest_that_changed_after_loading_is_refused():
    data = plan()
    with pytest.raises(AP.ReservationApplyRefused) as exc:
        AP.apply_plan(object(), data, supplied_digest="0" * 64,
                      backup_receipt="nope.json", out_dir=_PLANS)
    assert "digest changed" in str(exc.value)


def test_a_missing_backup_receipt_is_refused(tmp_path):
    with pytest.raises(AP.ReservationApplyRefused):
        AP.require_backup(tmp_path / "absent.json", target={"database": "poliscopic_dev"})


def test_a_backup_for_another_database_is_refused(tmp_path):
    path = tmp_path / "other.receipt.json"
    path.write_text('{"target": {"database": "somewhere_else"}}')
    path.chmod(0o600)
    with pytest.raises(AP.ReservationApplyRefused) as exc:
        AP.require_backup(path, target={"database": "poliscopic_dev"})
    assert "different database" in str(exc.value)


def test_a_world_readable_backup_receipt_is_refused(tmp_path):
    path = tmp_path / "loose.receipt.json"
    path.write_text(_BACKUP_TEXT)
    path.chmod(0o644)
    with pytest.raises(AP.ReservationApplyRefused) as exc:
        AP.require_backup(path, target={"database": "poliscopic_dev"})
    assert "0o600" in str(exc.value)


_BACKUP_TEXT = (_REPO / "data"
                / "backups" / "kg-stage1-backup-receipt-20260914T003343Z.v2.json").read_text()


def test_protected_row_counts_are_the_three_tables():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for table in AP.PROTECTED_TABLES:
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER)"))
    with engine.connect() as connection:
        counts = AP.protected_row_counts(connection)
    assert set(counts) == {"agenda_items", "supporting_documents", "meetings"}
    assert all(v == 0 for v in counts.values())
