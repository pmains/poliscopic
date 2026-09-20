"""Isolated tests for Stage 2 S1 plan/apply/verify (Brief 021 Step 1).

Everything here runs on an isolated in-memory SQLite fixture.  The fixture is
built to the *exact* authorised arithmetic — 978 direct, 126 alias, 108
collision-resolved, 217 phoenix-gp holds and 1 sentinel — so the plan invariants
are exercised at their real counts rather than a toy subset.

No test touches a real database and no test applies anything anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

import pytest
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.db import tier as tier_module  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_parentage_contract as parentage  # noqa: E402
from scripts.kg import stage2_s1_apply as apply_mod  # noqa: E402
from scripts.kg import stage2_s1_plan as plan_mod  # noqa: E402
from scripts.kg import stage2_s1_policy as policy  # noqa: E402
from scripts.kg import stage2_s1_verify as verify  # noqa: E402

DIRECT_BODIES = 978
ALIAS_COUNTS = {
    "chandler-pz": 114,
    "mesa-pz": 9,
    "peoria-planning-zoning": 3,
}
COLLISION_COUNTS = {"chandler-cf": 103, "chandler-pdc": 5}
HOLD_COUNTS = {"phoenix-gp": 217, "__skip__": 1}
#: The in-memory SQLite identity every fixture plan and receipt must match.
TEST_TARGET = tier_module.Target(
    url_class="development", dialect="sqlite", host=None, port=None, database=""
)
#: The receipt-visible form of that identity (the receipt contract carries host
#: and database; dialect/port are optional enrichment).
TEST_RECEIPT_TARGET = {"tier": "development", "dialect": "sqlite",
                       "host": None, "port": None, "database": None}


def _target_for(engine) -> tier_module.Target:
    """The exact target a plan must record to bind to this engine."""
    parts = urlsplit(str(engine.url))
    return tier_module.Target(
        url_class="development", dialect=engine.dialect.name,
        host=parts.hostname, port=parts.port,
        database=(parts.path or "").lstrip("/"),
    )


def _schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE jurisdictions (id INTEGER PRIMARY KEY, name TEXT, slug TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, jurisdiction_id INTEGER,"
            " name TEXT, slug TEXT, body_code TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT, meeting_id TEXT,"
            " meeting_date TEXT, jurisdiction_id INTEGER REFERENCES jurisdictions(id),"
            " public_body_id INTEGER REFERENCES public_bodies(id))"
        ))
        # Parentage schema readiness requires the support indexes too.
        conn.execute(text(
            "CREATE INDEX ix_meetings_public_body_id ON meetings(public_body_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_meetings_jurisdiction_id ON meetings(jurisdiction_id)"
        ))


def _registry_rows() -> list[tuple]:
    rows = [
        (17, 3, "Chandler Planning & Zoning Commission",
         "chandler-planning-zoning-commission", "chandler-planning-zoning-commission"),
        (37, 5, "Mesa Planning & Zoning Board", "mesa-planning-zoning", "mesa-planning-zoning"),
        (87, 10, "Peoria Planning & Zoning Commission",
         "peoria-planning-zoning", "peoria-planning-zoning"),
        (308, 3, "Chandler Community Facilities District", "chandler-cf", "chandler-cfd"),
        (334, 3, "Chandler Cultural Foundation",
         "chandler-cultural-foundation", "chandler-cf"),
        (316, 3, "Chandler Parks Development Commission", "chandler-pdc", "chandler-parks-dev"),
        (333, 3, "Chandler Mayor's Committee for People with Disabilities",
         "chandler-peoples-disabilities-committee", "chandler-pdc"),
    ]
    for i in range(DIRECT_BODIES):
        code = f"direct-{i:04d}"
        rows.append((1000 + i, 1, f"Direct Body {i}", code, code))
    return rows


def _meeting_rows() -> list[tuple]:
    rows: list[tuple] = []
    mid = 1
    for i in range(DIRECT_BODIES):
        rows.append((mid, f"direct-{i:04d}", str(mid), "2024-01-01", 1, None))
        mid += 1
    for body, n in ALIAS_COUNTS.items():
        for _ in range(n):
            rows.append((mid, body, str(mid), "2024-01-01", 3, None))
            mid += 1
    for body, n in COLLISION_COUNTS.items():
        for _ in range(n):
            rows.append((mid, body, str(mid), "2024-01-01", 3, None))
            mid += 1
    for body, n in HOLD_COUNTS.items():
        for _ in range(n):
            rows.append((mid, body, str(mid), "2024-01-01", None, None))
            mid += 1
    return rows


def _populate(engine) -> None:
    """Create and fill the fixture schema on any engine."""
    _schema(engine)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO jurisdictions VALUES (:id,:n,:s)"),
            [{"id": 1, "n": "Maricopa County", "s": "maricopa-county"},
             {"id": 3, "n": "City of Chandler", "s": "chandler"},
             {"id": 5, "n": "City of Mesa", "s": "mesa"},
             {"id": 10, "n": "City of Peoria", "s": "peoria"}],
        )
        conn.execute(
            text("INSERT INTO public_bodies VALUES (:id,:jur,:name,:slug,:code)"),
            [{"id": i, "jur": j, "name": n, "slug": s, "code": c}
             for i, j, n, s, c in _registry_rows()],
        )
        conn.execute(
            text("INSERT INTO meetings VALUES (:id,:body,:mid,:date,:jur,:pb)"),
            [{"id": i, "body": b, "mid": m, "date": d, "jur": j, "pb": p}
             for i, b, m, d, j, p in _meeting_rows()],
        )


def _file_engine(path) -> "object":
    """A file-backed SQLite engine, so each one has a distinct database identity."""
    engine = create_engine(f"sqlite:///{path}")
    _populate(engine)
    return engine


@pytest.fixture()
def fixture():
    """An isolated SQLite database shaped to the authorised arithmetic."""
    engine = create_engine("sqlite://")
    _populate(engine)
    return engine


def _baseline() -> dict:
    counts = {"entities": 1, "entity_mentions": 2, "entity_relationships": 3,
              "event_participants": 4, "meeting_event_extractions": 5,
              "meeting_events": 6}
    return {"counts": counts, "integrity": {"orphan_mentions": 0}}


def _receipt(plan_digest: str, counts: dict, *, bad_counts: bool = False,
             target: dict | None = None, drop_keys: tuple = ()) -> dict:
    payload_counts = dict(counts)
    if bad_counts:
        payload_counts = {k: int(v) + 1 for k, v in counts.items()}
    for key in drop_keys:
        payload_counts.pop(key, None)
    return {
        "dump_path": "/protected/dev.dump",
        "dump_sha256": "a" * 64,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": dict(target) if target is not None else dict(TEST_RECEIPT_TARGET),
        "scratch": {"host": "localhost", "database": "poliscopic_restore_scratch"},
        "pg_restore": {"exit_code": 0, "evidence": "pg_restore restored 4/4 objects"},
        "counts": payload_counts,
        "signatures": {"schema_sha256": "b" * 64,
                       "counts_sha256": receipts.counts_fingerprint(payload_counts)},
    }


def _write_receipt(tmp_path, payload: dict) -> pathlib.Path:
    path = tmp_path / "backup-receipt.json"
    path.write_text(json.dumps(payload))
    os.chmod(path, 0o600)
    return path


def _plan(engine) -> dict:
    return plan_mod.build_plan(engine, _target_for(engine), baseline=_baseline(),
                               plan_id="testplan", created_at="20260912T000000Z")


def _apply(engine, plan, tmp_path, receipt_path, *, digest=None, **kw):
    return apply_mod.apply_plan(
        engine, plan,
        supplied_digest=digest or artifacts.compute_digest(plan),
        backup_receipt=receipt_path,
        out_dir=tmp_path, allow_unsupported_dialect=True, **kw,
    )


# ── plan invariants ─────────────────────────────────────────────────────


def test_plan_counts_are_exactly_the_authorised_arithmetic(fixture):
    plan = _plan(fixture)
    counts = plan["counts"]
    assert counts["direct"] == 978
    assert counts["alias"] == 126
    assert counts["collision"] == 108
    assert counts["assignments"] == 1212
    assert counts["hold_phoenix_gp"] == 217
    assert counts["hold_sentinel"] == 1
    assert counts["holds"] == 218
    assert counts["null_parent_total"] == 1430
    assert verify.verify_plan_shape(plan) == []


def test_hold_set_is_exactly_phoenix_gp_and_the_sentinel(fixture):
    plan = _plan(fixture)
    bodies = sorted({e["body"] for e in plan["holds"]})
    assert bodies == ["__skip__", "phoenix-gp"]
    assert len(plan["holds"]) == 218


def test_alias_and_collision_targets_are_bound(fixture):
    plan = _plan(fixture)
    targets = {e["body"]: e["target_public_body_id"] for e in plan["assignments"]}
    assert targets["chandler-pz"] == 17
    assert targets["mesa-pz"] == 37
    assert targets["peoria-planning-zoning"] == 87
    assert targets["chandler-cf"] == 334
    assert targets["chandler-pdc"] == 333


# ── policy refusals ─────────────────────────────────────────────────────


def test_unhandled_body_is_refused():
    with pytest.raises(policy.PolicyError):
        policy.decide("mystery-body", [])


def test_ambiguous_unlisted_body_is_refused():
    with pytest.raises(policy.PolicyError):
        policy.decide("mystery-body", [1, 2])


def test_alias_ambiguity_is_refused():
    with pytest.raises(policy.PolicyError):
        policy.decide("chandler-pz", [17, 99])


def test_collision_requires_real_ambiguity():
    with pytest.raises(policy.PolicyError):
        policy.decide("chandler-cf", [334])


def test_collision_target_must_be_a_candidate():
    with pytest.raises(policy.PolicyError):
        policy.decide("chandler-cf", [308, 999])


def test_counts_problem_detects_tampering():
    good = dict(policy.EXPECTED_COUNTS)
    assert policy.counts_problem(good) is None
    bad = dict(good, direct=979)
    assert policy.counts_problem(bad) is not None


# ── immutable artifacts ─────────────────────────────────────────────────


def test_artifact_write_is_immutable(tmp_path):
    path = tmp_path / "a.json"
    artifacts.write_immutable(path, {"kind": "x"})
    with pytest.raises(artifacts.ArtifactCollision):
        artifacts.write_immutable(path, {"kind": "y"})


def test_tampered_artifact_is_refused(tmp_path):
    path = tmp_path / "a.json"
    artifacts.write_immutable(path, {"kind": "x", "n": 1})
    document = json.loads(path.read_text())
    document["n"] = 2
    path.write_text(json.dumps(document))
    with pytest.raises(artifacts.ArtifactDigestMismatch):
        artifacts.load_verified(path)


def test_digest_is_recomputed_not_trusted(tmp_path):
    path = tmp_path / "a.json"
    forged = {"kind": "x", "digest": "0" * 64}
    digest = artifacts.write_immutable(path, forged)
    assert digest == artifacts.compute_digest(json.loads(path.read_text()))


# ── apply contract: digest and target ───────────────────────────────────


def test_supplied_digest_mismatch_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, _write_receipt(tmp_path, _receipt("", _baseline()["counts"])),
               digest="f" * 64)


def test_unsupported_dialect_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        apply_mod.apply_plan(fixture, plan,
                             supplied_digest=artifacts.compute_digest(plan),
                             backup_receipt=receipt, out_dir=tmp_path)
    assert "unsupported dialect" in str(exc.value)


def test_target_drift_is_refused(fixture, tmp_path, monkeypatch):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))

    def boom(_engine):
        raise apply_mod.ApplyRefused("target is not development")

    monkeypatch.setattr(apply_mod, "assert_development_target", boom)
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, receipt)


def test_plan_shape_drift_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    plan["counts"]["direct"] = 1
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, _write_receipt(tmp_path, _receipt("", _baseline()["counts"])))


# ── apply contract: backup receipt ──────────────────────────────────────


def test_missing_backup_receipt_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, tmp_path / "absent.json")


def test_unprotected_backup_receipt_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    path = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    os.chmod(path, 0o644)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, path)
    assert "not protected" in str(exc.value)


def test_wrong_backup_receipt_counts_are_refused(fixture, tmp_path):
    plan = _plan(fixture)
    bad = _receipt("", _baseline()["counts"], bad_counts=True)
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, _write_receipt(tmp_path, bad))


# ── apply contract: drift and rollback ──────────────────────────────────


def test_meeting_drift_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    with fixture.begin() as conn:
        conn.execute(text("UPDATE meetings SET meeting_date='2025-05-05' WHERE id=1"))
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "fingerprint drifted" in str(exc.value)


def test_body_drift_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    with fixture.begin() as conn:
        conn.execute(text("UPDATE public_bodies SET name='Renamed' WHERE id=17"))
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "drifted" in str(exc.value)


def test_hold_drift_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    held = int(plan["holds"][0]["meeting_db_id"])
    with fixture.begin() as conn:
        conn.execute(text("UPDATE meetings SET public_body_id=999 WHERE id=:i"), {"i": held})
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, receipt)


def test_scope_drift_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    with fixture.begin() as conn:
        conn.execute(
            text("INSERT INTO meetings VALUES (99999,'direct-0000','99999','2024-01-01',1,NULL)")
        )
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "not in the plan" in str(exc.value)


def test_mid_transaction_failure_rolls_back_atomically(fixture, tmp_path, monkeypatch):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))

    calls = {"n": 0}
    original = verify.verify_postconditions

    def exploding(connection, plan_arg):
        calls["n"] += 1
        raise RuntimeError("postcondition evaluation exploded")

    monkeypatch.setattr(apply_mod.verify, "verify_postconditions", exploding)
    with pytest.raises(RuntimeError):
        _apply(fixture, plan, tmp_path, receipt)
    monkeypatch.setattr(apply_mod.verify, "verify_postconditions", original)

    with fixture.connect() as conn:
        still_null = conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar()
    assert still_null == 1430, "a failed transaction must leave every row unchanged"
    assert (tmp_path / "kg-stage2-s1-receipt-testplan-failure.json").exists()


def test_postcondition_failure_is_refused_and_leaves_no_writes(fixture, tmp_path, monkeypatch):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    monkeypatch.setattr(apply_mod.verify, "verify_postconditions",
                        lambda connection, plan_arg: ["synthetic postcondition failure"])
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, receipt)
    with fixture.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar() == 1430


# ── exact success and repeat refusal ────────────────────────────────────


def test_exact_fixture_success(fixture, tmp_path):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    result = _apply(fixture, plan, tmp_path, receipt)

    assert result["status"] == "success"
    assert result["operations"]["rows_updated"] == 1212
    assert result["operations"]["assignments"] == 1212
    assert result["postconditions"]["null_parent_after"] == 218

    with fixture.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar() == 218
        for body, target in (("chandler-pz", 17), ("chandler-cf", 334), ("chandler-pdc", 333)):
            assert conn.execute(
                text("SELECT COUNT(*) FROM meetings WHERE body=:b AND public_body_id=:t"),
                {"b": body, "t": target},
            ).scalar() == len([
                1 for r in _meeting_rows() if r[1] == body
            ])
    assert (tmp_path / "kg-stage2-s1-receipt-testplan-preimage.json").exists()
    terminal = tmp_path / "kg-stage2-s1-receipt-testplan.json"
    assert terminal.exists()
    artifacts.load_verified(terminal)


def test_repeat_apply_is_refused(fixture, tmp_path):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    _apply(fixture, plan, tmp_path, receipt)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "already has a terminal receipt" in str(exc.value)


def test_hold_set_survives_a_successful_apply(fixture, tmp_path):
    plan = _plan(fixture)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    _apply(fixture, plan, tmp_path, receipt)
    with fixture.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT body FROM meetings WHERE public_body_id IS NULL")
        ).fetchall()
    assert sorted(r[0] for r in rows) == ["__skip__", "phoenix-gp"]


# ── sync/parity readiness binding ───────────────────────────────────────


def test_plan_binds_the_readiness_contract_and_code_hashes(fixture):
    plan = _plan(fixture)
    readiness = plan["sync_parity_readiness"]
    assert readiness["contract"] == parentage.contract_snapshot()
    assert readiness["readiness_digest"] == parentage.readiness_digest()
    assert set(readiness["code_hashes"]) == set(parentage.BOUND_MODULES)
    assert verify.verify_readiness_binding(plan) == []


def test_apply_refuses_when_the_readiness_digest_drifts(fixture, tmp_path):
    plan = _plan(fixture)
    plan["sync_parity_readiness"]["readiness_digest"] = "0" * 64
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "readiness" in str(exc.value)
    with fixture.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar() == 1430


def test_apply_refuses_when_a_bound_module_hash_drifts(fixture, tmp_path):
    plan = _plan(fixture)
    module = parentage.BOUND_MODULES[0]
    plan["sync_parity_readiness"]["code_hashes"][module] = "0" * 64
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "drifted" in str(exc.value)


def test_apply_refuses_when_the_contract_declaration_drifts(fixture, tmp_path):
    plan = _plan(fixture)
    plan["sync_parity_readiness"]["contract"]["parentage_table"] = "somewhere_else"
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, receipt)


def test_apply_refuses_a_plan_with_no_readiness_binding(fixture, tmp_path):
    plan = _plan(fixture)
    plan.pop("sync_parity_readiness")
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(fixture, plan, tmp_path, receipt)


# ── schema readiness gating ─────────────────────────────────────────────


def test_plan_binds_schema_readiness(fixture):
    plan = _plan(fixture)
    bound = plan["schema_readiness"]
    assert bound["ready"] is True
    assert verify.verify_schema_readiness_binding(plan) == []


def test_apply_refuses_without_a_schema_readiness_binding(fixture, tmp_path):
    plan = _plan(fixture)
    plan.pop("schema_readiness")
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "schema readiness" in str(exc.value)


def test_apply_refuses_when_the_target_schema_is_not_ready(fixture, tmp_path):
    """A data apply must not run against a schema missing its parentage support."""
    plan = _plan(fixture)
    with fixture.begin() as conn:
        conn.execute(text("DROP INDEX ix_meetings_public_body_id"))
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "not ready" in str(exc.value)
    with fixture.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar() == 1430


def test_apply_refuses_when_the_schema_readiness_binding_drifts(fixture, tmp_path):
    plan = _plan(fixture)
    plan["schema_readiness"]["readiness_digest"] = "0" * 64
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(fixture, plan, tmp_path, receipt)
    assert "schema readiness" in str(exc.value)


# ── exact target binding and mandatory count coverage ───────────────────


def test_apply_accepts_an_exact_target_binding(fixture, tmp_path):
    """The plan target, live engine and receipt must agree — and here they do."""
    engine = fixture
    plan = _plan(engine)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    import json as _json
    payload = _json.loads(receipt.read_text())
    assert verify.verify_target_binding(plan, engine, payload) == []
    result = _apply(engine, plan, tmp_path, receipt)
    assert result["status"] == "success"
    assert result["operations"]["rows_updated"] == 1212


def test_apply_refuses_receipt_missing_a_baseline_count_key(fixture, tmp_path):
    """A receipt that simply omits a table must not pass by never claiming it."""
    engine = fixture
    plan = _plan(engine)
    receipt = _write_receipt(
        tmp_path, _receipt("", _baseline()["counts"], drop_keys=("meeting_events",)))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, receipt)
    assert "missing required count keys" in str(exc.value)
    assert "meeting_events" in str(exc.value)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar() == 1430


def test_apply_refuses_receipt_with_a_mismatched_count(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    counts = dict(_baseline()["counts"])
    counts["entities"] = counts["entities"] + 1
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"])).parent
    path = _write_receipt(tmp_path, _receipt("", counts))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, path)
    assert "does not match source" in str(exc.value) or "refused" in str(exc.value)


def test_apply_refuses_a_receipt_without_target_identity(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    receipt = _write_receipt(
        tmp_path, _receipt("", _baseline()["counts"], target={"tier": "development"}))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, receipt)
    assert "no target identity" in str(exc.value)


def test_apply_refuses_backup_receipt_target_drift(fixture, tmp_path):
    """A receipt taken from another database must not authorise this one."""
    engine = fixture
    plan = _plan(engine)
    receipt = _write_receipt(tmp_path, _receipt(
        "", _baseline()["counts"],
        target={"tier": "development", "host": "192.0.2.10",
                "database": "poliscopic_dev"}))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, receipt)
    assert "backup receipt" in str(exc.value)


def test_apply_refuses_an_alternate_development_database(tmp_path):
    """A different development database with an identical schema is refused."""
    planned = _file_engine(tmp_path / "planned.db")
    other = _file_engine(tmp_path / "other.db")
    plan = _plan(planned)
    assert verify.engine_identity(planned)["database"] != verify.engine_identity(other)["database"]

    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(other, plan, tmp_path,
           _write_receipt(tmp_path, _receipt("", _baseline()["counts"])))
    assert "live engine database" in str(exc.value)
    with other.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar() == 1430


def test_apply_refuses_plan_target_host_drift(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    plan["target"]["host"] = "elsewhere.internal"
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path,
           _write_receipt(tmp_path, _receipt("", _baseline()["counts"])))
    assert "live engine host" in str(exc.value)


def test_apply_refuses_plan_target_port_drift(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    plan["target"]["port"] = 5433
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path,
           _write_receipt(tmp_path, _receipt("", _baseline()["counts"])))
    assert "live engine port" in str(exc.value)


def test_apply_refuses_plan_target_database_drift(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    plan["target"]["database"] = "some_other_dev"
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path,
           _write_receipt(tmp_path, _receipt("", _baseline()["counts"])))
    assert "live engine database" in str(exc.value)


def test_apply_refuses_plan_target_dialect_drift(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    plan["target"]["dialect"] = "mysql"
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path,
           _write_receipt(tmp_path, _receipt("", _baseline()["counts"])))
    assert "live engine dialect" in str(exc.value)


def test_target_normalization_is_case_insensitive_but_not_confusing(fixture):
    engine = fixture
    plan = _plan(engine)
    plan["target"]["database"] = (plan["target"]["database"] or "").upper() or None
    # case differences normalize away, so this still binds
    assert verify.verify_target_binding(
        plan, engine, {"target": dict(TEST_RECEIPT_TARGET)}) == []


def test_target_normalization_never_makes_different_targets_match(fixture):
    engine = fixture
    plan = _plan(engine)
    plan["target"]["database"] = "poliscopic_dev_v2"
    problems = verify.verify_target_binding(
        plan, engine, {"target": dict(TEST_RECEIPT_TARGET)})
    assert problems and "database" in problems[0]


# ── all four receipt identity fields are mandatory ──────────────────────


class _StubDialect:
    def __init__(self, name):
        self.name = name


class _StubEngine:
    """A target identity with no database connection, for binding unit tests."""

    def __init__(self, url, dialect="postgresql"):
        self.url = url
        self.dialect = _StubDialect(dialect)


PG_URL = "postgresql://poliscopic:***@192.0.2.10:5432/poliscopic_dev"
PG_ENGINE = _StubEngine(PG_URL)
PG_PLAN = {"target": {"dialect": "postgresql", "host": "192.0.2.10",
                      "port": 5432, "database": "poliscopic_dev"}}
PG_RECEIPT_TARGET = {"tier": "development", "dialect": "postgresql",
                     "host": "192.0.2.10", "port": 5432, "database": "poliscopic_dev"}


def _binding(engine=PG_ENGINE, plan=None, target=None):
    return verify.verify_target_binding(
        plan if plan is not None else PG_PLAN, engine,
        {"target": dict(PG_RECEIPT_TARGET if target is None else target)})


def test_all_four_identity_fields_bind_exactly():
    """The happy path: dialect, host, port and database all present and equal."""
    assert _binding() == []


def test_receipt_missing_dialect_is_refused():
    target = {k: v for k, v in PG_RECEIPT_TARGET.items() if k != "dialect"}
    problems = _binding(target=target)
    assert problems and "does not name the live dialect" in problems[0]


def test_receipt_missing_port_is_refused():
    """Port was previously treated as optional enrichment; it no longer is."""
    target = {k: v for k, v in PG_RECEIPT_TARGET.items() if k != "port"}
    problems = _binding(target=target)
    assert problems and "does not name the live port" in problems[0]


def test_receipt_missing_host_and_database_are_refused():
    for field in ("host", "database"):
        target = {k: v for k, v in PG_RECEIPT_TARGET.items() if k != field}
        problems = _binding(target=target)
        assert problems, field
        assert any(field in p for p in problems), (field, problems)


def test_receipt_dialect_mismatch_is_refused():
    target = dict(PG_RECEIPT_TARGET, dialect="mysql")
    problems = _binding(target=target)
    assert problems and "backup receipt dialect" in problems[0]


def test_receipt_port_mismatch_is_refused():
    target = dict(PG_RECEIPT_TARGET, port=5433)
    problems = _binding(target=target)
    assert problems and "backup receipt port" in problems[0]


def test_receipt_host_and_database_mismatch_are_refused():
    for field, bad in (("host", "other.internal"), ("database", "poliscopic_other")):
        problems = _binding(target=dict(PG_RECEIPT_TARGET, **{field: bad}))
        assert problems and f"backup receipt {field}" in problems[0], field


def test_plan_drift_on_every_identity_field_is_refused():
    for field, bad in (("dialect", "mysql"), ("host", "elsewhere.internal"),
                       ("port", 5433), ("database", "poliscopic_other")):
        plan = {"target": dict(PG_PLAN["target"], **{field: bad})}
        problems = verify.verify_target_binding(
            plan, PG_ENGINE, {"target": dict(PG_RECEIPT_TARGET)})
        assert problems and f"live engine {field}" in problems[0], field


def test_receipt_with_no_target_identity_is_refused():
    problems = verify.verify_target_binding(
        PG_PLAN, PG_ENGINE, {"target": {"tier": "development"}})
    assert problems == ["backup receipt carries no target identity to bind"]


def test_apply_refuses_a_receipt_missing_its_dialect(tmp_path):
    """Integration: the apply refuses a receipt that omits only its dialect.

    A file-backed engine is used so ``database`` is genuinely populated and the
    omission is isolated to ``dialect`` rather than emptying the identity.
    """
    engine = _file_engine(tmp_path / "binding.db")
    plan = _plan(engine)
    live = verify.engine_identity(engine)
    target = {"tier": "development", "host": live["host"],
              "port": live["port"], "database": live["database"]}   # dialect omitted
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"], target=target))

    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, receipt)
    assert "does not name the live dialect" in str(exc.value)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar() == 1430


# ── one authoritative counts fingerprint, enforced before any write ─────


def _null_parents(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar()


def test_planner_uses_the_authoritative_fingerprint_algorithm(fixture):
    """The plan must use the same algorithm every backup receipt uses."""
    plan = _plan(fixture)
    counts = _baseline()["counts"]
    assert plan["baseline"]["counts_fingerprint"] == receipts.counts_fingerprint(counts)
    # ...and not the canonical-JSON scheme that used to be here.
    assert plan["baseline"]["counts_fingerprint"] != hashlib.sha256(
        artifacts.canonical_json(counts).encode("utf-8")).hexdigest()


def test_fingerprint_is_stable_and_order_independent():
    counts = _baseline()["counts"]
    shuffled = dict(reversed(list(counts.items())))
    assert receipts.counts_fingerprint(counts) == receipts.counts_fingerprint(shuffled)


def test_apply_refuses_a_plan_without_a_baseline_fingerprint(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    plan["baseline"].pop("counts_fingerprint")
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, receipt)
    assert "no baseline counts fingerprint" in str(exc.value)
    assert _null_parents(engine) == 1430


def test_apply_refuses_a_plan_fingerprint_mismatching_the_receipt(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    plan["baseline"]["counts_fingerprint"] = "0" * 64
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, receipt)
    assert "does not match plan baseline" in str(exc.value)
    assert _null_parents(engine) == 1430


def test_apply_refuses_a_tampered_receipt_fingerprint(fixture, tmp_path):
    """A receipt whose recorded signature contradicts its counts is refused."""
    engine = fixture
    plan = _plan(engine)
    payload = _receipt("", _baseline()["counts"])
    payload["signatures"]["counts_sha256"] = "f" * 64
    receipt = _write_receipt(tmp_path, payload)
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, receipt)
    assert "refused" in str(exc.value)
    assert _null_parents(engine) == 1430


def test_apply_refuses_tampered_receipt_counts(fixture, tmp_path):
    """Counts that disagree with the plan are refused on value, not just hash."""
    engine = fixture
    plan = _plan(engine)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"], bad_counts=True))
    with pytest.raises(apply_mod.ApplyRefused) as exc:
        _apply(engine, plan, tmp_path, receipt)
    assert _null_parents(engine) == 1430


def test_apply_proceeds_when_the_fingerprints_match_exactly(fixture, tmp_path):
    engine = fixture
    plan = _plan(engine)
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    result = _apply(engine, plan, tmp_path, receipt)
    assert result["status"] == "success"
    assert result["operations"]["rows_updated"] == 1212
    assert result["backup_receipt"]["counts_fingerprint"] == \
        plan["baseline"]["counts_fingerprint"]
    assert _null_parents(engine) == 218


def test_no_preimage_is_written_when_the_fingerprint_check_fails(fixture, tmp_path):
    """The refusal must land before the preimage artifact or the transaction."""
    engine = fixture
    plan = _plan(engine)
    plan["baseline"]["counts_fingerprint"] = "0" * 64
    receipt = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    with pytest.raises(apply_mod.ApplyRefused):
        _apply(engine, plan, tmp_path, receipt)
    assert list(tmp_path.glob("*preimage*")) == []
    assert list(tmp_path.glob("*s1-receipt*")) == []


# ── CLI refusal and one digest field name ──────────────────────────────


def test_recorded_digest_prefers_the_canonical_field():
    """Reading is tolerant of the historical alias; writing never emits it."""
    assert artifacts.recorded_digest({"digest": "a", "receipt_digest": "b"}) == "a"
    assert artifacts.recorded_digest({"receipt_digest": "b"}) == "b"
    assert artifacts.recorded_digest({}) is None
    assert artifacts.LEGACY_DIGEST_FIELDS == ("receipt_digest",)


def test_written_receipt_carries_exactly_one_digest_field(tmp_path):
    path, digest = apply_mod.write_receipt(tmp_path, "digestplan", {"kind": "x"})
    document = json.loads(path.read_text())
    assert artifacts.DIGEST_FIELD in document
    assert all(alias not in document for alias in artifacts.LEGACY_DIGEST_FIELDS)
    assert artifacts.recorded_digest(document) == digest
    assert artifacts.compute_digest(document) == digest


def test_apply_plan_reports_the_digest_under_the_artifact_field_name(fixture, tmp_path):
    """The returned receipt and the persisted receipt name the digest alike."""
    engine = fixture
    plan = _plan(engine)
    receipt_path = _write_receipt(tmp_path, _receipt("", _baseline()["counts"]))
    result = _apply(engine, plan, tmp_path, receipt_path)

    on_disk = json.loads(pathlib.Path(result["receipt_path"]).read_text())
    assert artifacts.DIGEST_FIELD in result and artifacts.DIGEST_FIELD in on_disk
    assert "receipt_digest" not in result
    assert all(a not in on_disk for a in artifacts.LEGACY_DIGEST_FIELDS)
    assert artifacts.recorded_digest(result) == artifacts.recorded_digest(on_disk)
    assert artifacts.compute_digest(on_disk) == artifacts.recorded_digest(on_disk)


def test_main_returns_two_and_prints_one_line_on_refusal(monkeypatch, capsys):
    """A refusal is an expected outcome: exit 2, one stderr line, no traceback."""
    def _refuse(*args, **kwargs):
        raise apply_mod.ApplyRefused("plan already has a terminal receipt")

    import scripts.db.config as db_config
    monkeypatch.setattr(db_config, "DB_TIER", "development")
    monkeypatch.setattr(apply_mod.tier_module, "validate_tier_target", lambda *a, **k: None)
    monkeypatch.setattr(apply_mod, "load_plan_for_digest", _refuse)

    code = apply_mod.main(["--plan", "p", "--digest", "d", "--backup-receipt", "b"])
    captured = capsys.readouterr()
    assert code == apply_mod.REFUSED_EXIT_CODE == 2
    # No receipt JSON on stdout; the config banner is harness noise.
    assert "{" not in captured.out and "receipt_path" not in captured.out
    assert captured.err.count("\n") == 1
    assert captured.err == "ApplyRefused: plan already has a terminal receipt\n"
    assert "Traceback" not in captured.err


def test_main_never_leaks_a_traceback_for_a_tier_refusal(monkeypatch, capsys):
    """A tier guard refusal is reported as one line, not a crash."""
    import scripts.db.config as db_config
    monkeypatch.setattr(db_config, "DB_TIER", "production")
    code = apply_mod.main(["--plan", "p", "--digest", "d", "--backup-receipt", "b"])
    captured = capsys.readouterr()
    assert code == apply_mod.REFUSED_EXIT_CODE
    assert "{" not in captured.out and "receipt_path" not in captured.out
    assert captured.err.count("\n") == 1
    assert captured.err.startswith("TierError: ")
    assert "Traceback" not in captured.err


def test_every_cli_refusal_is_an_expected_guard_type():
    """The CLI catches guard refusals only, never blanket exceptions."""
    assert apply_mod.CLI_REFUSALS == (apply_mod.ApplyRefused, apply_mod.tier_module.TierError)
    assert not any(issubclass(t, Exception) and t in (KeyboardInterrupt, SystemExit)
                   for t in apply_mod.CLI_REFUSALS)
