#!/usr/bin/env python3
"""Adversarial tests for the Stage 2 execution binding, adjudication ancestry and
the internal typed write body.

Three defects are closed here, and each gets a refusal test:

1. the plans must bind the **complete execution module set**;
2. each decision's **aggregate anchor** must contain the exact proposal
   path+digest+unit, and the current aggregate must **prove descent** from it;
3. **exact** proposal path+digest and full candidate/lineage/target equality for
   both plans.

Plus the internal typed write body, which must stay inert.
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
from scripts.kg import stage2_s2_admission_binding as AB  # noqa: E402
from scripts.kg import stage2_s2_apply_runner as AR  # noqa: E402
from scripts.kg import stage2_s2_label_correction as LC  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as B  # noqa: E402
from scripts.kg import stage2_s2_repair_plan as RP  # noqa: E402
from scripts.kg import stage2_s2_write_body as WB  # noqa: E402
from tests.kg_stage2_test_fixtures import code_current_copy  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"
_DEV_TARGET = {"dialect": "postgresql", "host": "192.0.2.10", "port": 5432,
               "database": "poliscopic_dev", "tier": "development"}


def _live(pattern):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


def repair():
    return code_current_copy(
        artifacts.load_verified(_live("kg-stage2-s2-repair-plan-*.json")))


def correction():
    return code_current_copy(
        artifacts.load_verified(_live("kg-stage2-s2-label-correction-plan-*.json")))


def recorded_correction():
    """The immutable on-disk artifact used by canonical-loader tests."""
    return artifacts.load_verified(
        _live("kg-stage2-s2-label-correction-plan-*.json"))


def heads():
    return AB._verify_heads(_PLANS)


# ══ P1-1: the complete execution module set is bound ═══════════════════

def test_the_manifest_binds_every_safety_critical_execution_module():
    for required in ("scripts/kg/stage2_s2_apply_runner.py",
                     "scripts/kg/stage2_s2_admission_binding.py",
                     "scripts/kg/stage2_s2_admission_tx.py",
                     "scripts/kg/stage2_s2_apply_target.py",
                     "scripts/kg/stage2_s2_current_state.py",
                     "scripts/kg/stage2_s2_collision.py",
                     "scripts/kg/stage2_s2_receipt.py",
                     "scripts/kg/stage2_s2_receipt_rollback.py",
                     "scripts/kg/stage2_s2_write_body.py"):
        assert required in B.CODE_MODULES, required
        assert required in B.EXECUTION_MODULES, required


def test_the_execution_set_is_a_subset_of_the_bound_set():
    assert set(B.EXECUTION_MODULES) <= set(B.CODE_MODULES)
    assert len(B.CODE_MODULES) == len(set(B.CODE_MODULES))


def test_no_declared_module_is_absent_from_disk():
    assert B.missing_code_modules() == []


def test_a_missing_module_is_reported_rather_than_skipped():
    assert B.missing_code_modules(("scripts/kg/not_a_module.py",)) == \
        ["scripts/kg/not_a_module.py"]


@pytest.mark.parametrize("plan_factory", [repair, correction])
def test_both_plans_bind_the_whole_execution_set(plan_factory):
    plan = plan_factory()
    recorded = set(plan["bindings"]["code_hashes"])
    # v2 binds a superset of the settled set: it adds the schema-signature modules.
    assert set(B.CODE_MODULES) <= recorded
    assert set(B.EXECUTION_MODULES) <= recorded


def test_a_plan_omitting_an_execution_module_is_refused_by_the_plan_validator():
    """Dropping the write body from the binding must fail validation."""
    for plan, validator in ((repair(), RP.validate_plan),
                            (correction(), LC.validate_plan)):
        trimmed = copy.deepcopy(plan)
        trimmed["bindings"]["code_hashes"].pop("scripts/kg/stage2_s2_write_body.py")
        problems = validator(trimmed)
        assert any("write_body" in p for p in problems), problems[:3]


def test_a_plan_omitting_an_execution_module_is_refused_by_admission():
    plan = repair()
    plan["bindings"]["code_hashes"].pop("scripts/kg/stage2_s2_receipt.py")
    with pytest.raises(AR.ApplyRefused) as exc:
        AB._verify_code_hashes(plan)
    assert "complete execution set" in str(exc.value) or "execution modules" in str(exc.value)


def test_a_stale_execution_hash_is_refused():
    plan = repair()
    plan["bindings"]["code_hashes"]["scripts/kg/stage2_s2_write_body.py"] = "0" * 64
    with pytest.raises(AR.ApplyRefused) as exc:
        AB._verify_code_hashes(plan)
    assert "code has drifted" in str(exc.value)


def test_a_plan_with_no_code_hashes_is_refused():
    plan = repair()
    plan["bindings"]["code_hashes"] = {}
    with pytest.raises(AR.ApplyRefused):
        AB._verify_code_hashes(plan)


def test_the_repair_plan_binds_a_superset_of_the_correction_code_set():
    """V2 deliberately binds MORE than v1 did - equality would mean the applied
    correction artifact had been re-bound, which is forbidden."""
    applied = correction()["bindings"]["code_hashes"]
    repaired = repair()["bindings"]["code_hashes"]
    assert set(applied) <= set(repaired)
    assert set(repaired) - set(applied), "v2 must bind at least one extra module"
    for name, digest in applied.items():
        assert repaired[name] == digest


@pytest.mark.parametrize("plan_factory", [repair, correction])
def test_both_plans_pass_their_own_validator(plan_factory):
    plan = plan_factory()
    validator = RP.validate_plan if plan["kind"] == RP.PLAN_KIND else LC.validate_plan
    assert validator(plan) == []


# ══ P1-2: the anchor membership and supersession ancestry ══════════════

def test_the_anchor_contains_the_exact_proposal_path_digest_and_unit():
    rep = repair()
    anchor = artifacts.load_verified(
        _PLANS / rep["bindings"]["decisions"][0]["lineage"]["aggregate"]["path"])
    for entry in rep["bindings"]["decisions"]:
        document_id = int(entry["document_id"])
        members = [{**m, "unit": u} for u, ms in (anchor.get("per_unit") or {}).items()
                   for m in ms if int(m.get("document_id", -1)) == document_id]
        assert len(members) == 1, entry["document_id"]
        assert members[0]["path"] == entry["proposal_path"]
        assert members[0]["digest"] == entry["proposal_digest"]
        assert members[0]["unit"] == entry["proposal_unit"]


def test_the_plan_binds_the_adjudication_unit():
    for entry in repair()["bindings"]["decisions"]:
        assert entry["proposal_unit"], entry["document_id"]


def test_the_current_aggregate_proves_descent_from_the_anchor():
    head = heads()
    rep = repair()
    anchor_digest = rep["bindings"]["decisions"][0]["lineage"]["aggregate"]["digest"]
    ancestry = AB.verify_supersession_ancestry(
        _PLANS, _PLANS / head["aggregate"]["path"], head["aggregate"]["digest"],
        anchor_digest)
    assert ancestry["reached"] is True
    assert ancestry["chain"][-1]["digest"] == anchor_digest


def test_an_unreachable_anchor_is_refused():
    head = heads()
    ancestry = AB.verify_supersession_ancestry(
        _PLANS, _PLANS / head["aggregate"]["path"], head["aggregate"]["digest"],
        "0" * 64)
    assert ancestry["reached"] is False
    assert "chain" in ancestry["reason"]


def test_an_unresolvable_link_ends_the_walk_with_a_refusal():
    """A chain that names an artifact which cannot be loaded must not walk on."""
    head = heads()
    ancestry = AB.verify_supersession_ancestry(
        _PLANS, _PLANS / head["aggregate"]["path"], head["aggregate"]["digest"],
        "0" * 64)
    assert ancestry["reached"] is False
    assert ancestry["reason"]


def test_a_decision_without_a_unit_binding_is_refused():
    plan = repair()
    plan["bindings"]["decisions"][0]["proposal_unit"] = None
    head = heads()
    with pytest.raises(AR.ApplyRefused) as exc:
        AB._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=head["aggregate"]["document"], heads=head)
    assert "unit" in str(exc.value)


def test_a_decision_unit_mismatch_is_refused():
    plan = repair()
    plan["bindings"]["decisions"][0]["proposal_unit"] = "u-not-the-anchor-unit"
    head = heads()
    with pytest.raises(AR.ApplyRefused) as exc:
        AB._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=head["aggregate"]["document"], heads=head)
    assert "unit" in str(exc.value) or "lineage" in str(exc.value)


def test_a_decision_whose_anchor_path_is_missing_is_refused():
    plan = repair()
    plan["bindings"]["decisions"][0]["lineage"]["aggregate"] = {}
    head = heads()
    with pytest.raises(AR.ApplyRefused) as exc:
        AB._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=head["aggregate"]["document"], heads=head)
    assert "lineage" in str(exc.value) or "anchor" in str(exc.value)


def test_a_decision_whose_anchor_digest_is_tampered_is_refused():
    plan = repair()
    plan["bindings"]["decisions"][0]["lineage"]["aggregate"]["digest"] = "0" * 64
    head = heads()
    with pytest.raises(AR.ApplyRefused) as exc:
        AB._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=head["aggregate"]["document"], heads=head)
    assert "lineage" in str(exc.value)


def test_the_anchor_need_not_be_the_current_head():
    """It is a historical record, reached through the supersession chain."""
    head = heads()
    rep = repair()
    anchor_name = rep["bindings"]["decisions"][0]["lineage"]["aggregate"]["path"]
    assert anchor_name != head["aggregate"]["path"]
    assert (_PLANS / anchor_name).exists()


# ══ P1-3: exact proposal and full candidate/lineage/target equality ════

@pytest.mark.parametrize("field,value", [
    ("proposal_path", "proposal-not-mine.json"),
    ("proposal_digest", "0" * 64),
])
def test_a_substituted_proposal_is_refused(field, value):
    plan = repair()
    plan["bindings"]["decisions"][0][field] = value
    head = heads()
    with pytest.raises(AR.ApplyRefused):
        AB._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=head["aggregate"]["document"], heads=head)


@pytest.mark.parametrize("field", list(B.CANDIDATE_FIELDS))
def test_every_candidate_field_is_compared(field):
    plan = repair()
    candidate = copy.deepcopy(plan["bindings"]["decisions"][0]["candidate"])
    candidate[field] = 999999 if field.endswith("_id") else "TAMPERED"
    plan["bindings"]["decisions"][0]["candidate"] = candidate
    head = heads()
    with pytest.raises(AR.ApplyRefused) as exc:
        AB._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=head["aggregate"]["document"], heads=head)
    assert "candidate" in str(exc.value)


def test_a_candidate_missing_a_field_is_refused():
    plan = repair()
    plan["bindings"]["decisions"][0]["candidate"] = {"agenda_item_db_id": 1}
    head = heads()
    with pytest.raises(AR.ApplyRefused):
        AB._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=head["aggregate"]["document"], heads=head)


@pytest.mark.parametrize("key", ["plan", "aggregate", "proposal"])
def test_a_tampered_lineage_entry_is_refused(key):
    plan = repair()
    plan["bindings"]["decisions"][0]["lineage"][key]["digest"] = "0" * 64
    head = heads()
    with pytest.raises(AR.ApplyRefused):
        AB._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=head["aggregate"]["document"], heads=head)


def test_a_decision_missing_from_the_aggregate_is_refused():
    plan = repair()
    head = heads()
    aggregate = copy.deepcopy(head["aggregate"]["document"])
    aggregate["decisions"] = {}
    with pytest.raises(AR.ApplyRefused):
        AB._verify_decisions(plan, plan_dir=_PLANS, aggregate=aggregate, heads=head)


def test_both_plans_pass_the_exhaustive_decision_check():
    head = heads()
    for plan in (repair(), correction()):
        verified = AB._verify_decisions(
            plan, plan_dir=_PLANS, aggregate=head["aggregate"]["document"], heads=head)
        assert len(verified) == B.EXPECTED_APPROVED_DECISIONS


def test_target_equality_holds_across_both_plans_and_the_live_target():
    live_target = {**_DEV_TARGET, "host": repair()["bindings"]["target"]["host"]}
    assert AB.verify_target_equality(repair(), correction(), live_target) == []


def test_a_target_mismatch_between_plans_is_refused():
    rep, cor = repair(), correction()
    cor["bindings"]["target"]["database"] = "somewhere_else"
    assert AB.verify_target_equality(rep, cor, _DEV_TARGET)


def test_a_target_mismatch_against_the_live_target_is_refused():
    assert AB.verify_target_equality(repair(), correction(),
                                     {**_DEV_TARGET, "host": "other-host"})


def test_a_plan_without_a_target_is_refused():
    rep, cor = repair(), correction()
    cor["bindings"]["target"] = {}
    assert AB.verify_target_equality(rep, cor, _DEV_TARGET)


# ══ the typed write body: development-capable, not caller-enablable ════

_AGENDA_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_db_id INTEGER, "
    "agenda_item_number TEXT, agenda_item_title TEXT, agenda_item_text TEXT, "
    "body TEXT, meeting_id TEXT, agenda_item_id TEXT, agenda_item_url TEXT, "
    "vote_or_action TEXT, source_body TEXT, source_url TEXT, c_number TEXT, "
    "c_number_base TEXT, case_number TEXT, agenda_category TEXT, item_type TEXT, "
    "section_level INTEGER, sort_order INTEGER, created_at TIMESTAMP")
AGENDA_DDL = "CREATE TABLE agenda_items (" + _AGENDA_COLUMNS + ", " \
             "UNIQUE (meeting_db_id, agenda_item_number))"


def _write_fixture():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE agenda_items (" + _AGENDA_COLUMNS + ", "
                       "UNIQUE (meeting_db_id, agenda_item_number))"))
        c.execute(text("CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, "
                       "agenda_item_db_id INTEGER, agenda_item_id TEXT, "
                       "agenda_item_number TEXT)"))
        c.execute(text("CREATE TABLE meetings (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE agenda_item_key_reservation (meeting_db_id "
                       "INTEGER NOT NULL, agenda_item_number TEXT NOT NULL, "
                       "plan_digest TEXT NOT NULL, reserved_at TEXT NOT NULL, "
                       "reserved_by TEXT NOT NULL, PRIMARY KEY (meeting_db_id, "
                       "agenda_item_number))"))
    return engine


def test_the_write_body_has_no_caller_enablable_switch():
    """Enablement is a property of the target, never of an argument."""
    assert not hasattr(WB, "WRITE_BODY_ENABLED")
    assert not hasattr(WB, "write_body_enabled")
    assert WB.ALLOWED_DIALECTS == ("sqlite", "postgresql")


def test_the_write_body_has_no_callback_sql_or_table_parameter():
    import inspect
    # The DERIVATION surface must not accept a connection, a callback, SQL or a table.
    # ``assert_writable_target`` and ``preimage_for`` are excluded because they are
    # read-only inspectors that must see the connection to classify a target or read a
    # preimage; ``test_the_read_only_helpers_never_write`` holds them to reading only.
    surface = [WB.operations_for, WB.validate_operations,
               WB.operations_for_authorized_plan, WB.postcondition_expectation]
    banned = ("callback", "hook", "sql", "table", "connection", "callable")
    for func in surface:
        for name, parameter in inspect.signature(func).parameters.items():
            assert "Callable" not in str(parameter.annotation), (func, name)
            assert not any(b in name.lower() for b in banned), (func, name)


def test_the_public_write_body_entry_names_only_plan_and_role():
    assert set(WB.PUBLIC_PARAMETERS) == {"plan_path", "plan_digest", "role", "plan_dir"}
    import inspect
    named = set(inspect.signature(WB.operations_for_authorized_plan).parameters)
    assert named <= {"plan_path", "plan_digest", "role", "plan_dir"}, named


def test_the_read_only_helpers_never_write():
    """The target gate and the preimage reader classify and read; they never write."""
    import inspect
    for func in (WB.assert_writable_target, WB.preimage_for):
        source = inspect.getsource(func)
        for writer in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert writer not in source, (func.__name__, writer)


def test_the_mutation_function_is_private():
    import inspect
    assert "_execute_operations" in dir(WB)
    assert not inspect.signature(WB._execute_operations).parameters.get("callback")


def test_a_production_host_is_refused():
    class _C:
        class dialect:
            name = "postgresql"

        class engine:
            class url:
                host = "db.ondigitalocean.com"
                database = "poliscopic_dev"

    with pytest.raises(WB.WriteRefused) as exc:
        WB.assert_writable_target(_C())
    assert "production host" in str(exc.value)


def test_a_production_database_is_refused():
    class _C:
        class dialect:
            name = "postgresql"

        class engine:
            class url:
                host = "127.0.0.1"
                database = "poliscopic"

    with pytest.raises(WB.WriteRefused) as exc:
        WB.assert_writable_target(_C())
    assert "production database" in str(exc.value)


def test_an_unregistered_dialect_is_refused():
    class _C:
        class dialect:
            name = "mysql"

    with pytest.raises(WB.WriteRefused) as exc:
        WB.assert_writable_target(_C())
    assert "only" in str(exc.value)


def test_a_development_postgres_target_is_allowed():
    class _C:
        class dialect:
            name = "postgresql"

        class engine:
            class url:
                host = "127.0.0.1"
                database = "poliscopic_scratch"

    assert WB.assert_writable_target(_C())["dialect"] == "postgresql"


def test_operations_are_derived_only_from_the_plan():
    rep, cor = repair(), correction()
    repair_ops = WB.operations_for(rep, role="repair")
    correction_ops = WB.operations_for(cor, role="correction")
    assert len(repair_ops) == rep["counts"]["materialise"]
    assert len(correction_ops) == cor["counts"]["new_item_row"]
    for operation in repair_ops + correction_ops:
        assert operation.kind in WB.OPERATION_KINDS
        assert operation.plan_digest == artifacts.recorded_digest(rep) or \
            operation.plan_digest == artifacts.recorded_digest(cor)


def test_an_unknown_operation_kind_is_refused():
    with pytest.raises(WB.WriteRefused) as exc:
        WB.TypedOperation(kind="drop_table", plan_digest="d", meeting_db_id=1,
                          agenda_item_number="1")
    assert "not one of" in str(exc.value)


def test_an_operation_without_a_plan_digest_is_refused():
    with pytest.raises(WB.WriteRefused):
        WB.TypedOperation(kind="insert_item", plan_digest="", meeting_db_id=1,
                          agenda_item_number="1")


def test_a_renumber_without_a_row_id_is_refused():
    with pytest.raises(WB.WriteRefused):
        WB.TypedOperation(kind="renumber_item", plan_digest="d", meeting_db_id=1,
                          agenda_item_number="1")


def test_substituted_operations_are_refused():
    rep = repair()
    operations = list(WB.operations_for(rep, role="repair"))
    assert WB.validate_operations(rep, operations, role="repair") == []
    assert WB.validate_operations(rep, operations[:-1], role="repair")


def test_the_authorized_loader_requires_the_exact_digest():
    cor = recorded_correction()
    digest = artifacts.recorded_digest(cor)
    path = _live("kg-stage2-s2-label-correction-plan-*.json").name
    plan, operations = WB.operations_for_authorized_plan(path, digest,
                                                         role="correction")
    assert artifacts.recorded_digest(plan) == digest
    assert len(operations) == cor["counts"]["new_item_row"]
    with pytest.raises(WB.WriteRefused) as exc:
        WB.operations_for_authorized_plan(path, "0" * 64, role="correction")
    assert "is not the" in str(exc.value)


def test_the_authorized_loader_refuses_a_path_that_is_not_a_plain_name():
    with pytest.raises(WB.WriteRefused):
        WB.operations_for_authorized_plan("sub/dir/plan.json", "d", role="repair")


def test_the_write_body_module_is_bound_by_both_plans():
    for plan in (repair(), correction()):
        assert "scripts/kg/stage2_s2_write_body.py" in plan["bindings"]["code_hashes"]


# ══ the public apply path stays disabled ═══════════════════════════════

def test_the_public_apply_path_is_still_disabled():
    import inspect
    assert AR.APPLY_PARAMETERS == ()
    named = [n for n, p in inspect.signature(AR.apply).parameters.items()
             if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    assert named == [], named
    with pytest.raises(AR.ApplyRefused):
        AR.apply(write=lambda c, r: None, engine=object())


def test_the_public_apply_refuses_before_any_connection():
    class _Spy:
        class dialect:
            name = "postgresql"

        connect_calls = 0

        def connect(self):  # pragma: no cover - only reached on a regression
            type(self).connect_calls += 1
            raise AssertionError("no connection may be opened")

    engine = _Spy()
    with pytest.raises(AR.ApplyRefused):
        AR.apply(write=lambda c, r: None, engine=engine)
    assert _Spy.connect_calls == 0


def test_admit_still_takes_no_write_or_connection_parameter():
    import inspect
    parameters = inspect.signature(AR.admit).parameters
    for forbidden in ("write", "callback", "hook", "connection"):
        assert forbidden not in parameters, forbidden


def test_the_write_body_module_is_bound_by_both_plans():
    for plan in (repair(), correction()):
        assert "scripts/kg/stage2_s2_write_body.py" in plan["bindings"]["code_hashes"]
