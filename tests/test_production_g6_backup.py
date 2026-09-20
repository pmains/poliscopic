"""Focused contract tests for the candidate-bound G6 backup proof."""

from __future__ import annotations

import importlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
for candidate in (ROOT, ROOT / "scripts", OPS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

G = importlib.import_module("production_g6_backup")
P = importlib.import_module("production_preflight")

TARGET = {
    "database": "poliscopic", "configured_host": "prod.example",
    "configured_port": 25060, "server_address": "192.0.2.4",
    "server_port": 25060, "cluster_system_identifier": "12345",
}


def candidate(path: Path) -> dict:
    body = {
        "schema": G.CANDIDATE_SCHEMA, "operation": "OP-REPAIR",
        "status": "CANDIDATE-NOT-AUTHORIZABLE", "apply_blocked": True,
        "target": dict(TARGET), "proposals": [], "quarantine": [],
        "counts": {"proposals": 0, "quarantine": 0},
    }
    artifact = {**body, "digest": P.digest(body)}
    path.write_bytes(P.canonical_bytes(artifact))
    os.chmod(path, 0o600)
    return artifact


def baseline() -> dict:
    body = {
        "schema": G.BASELINE_SCHEMA, "captured_at": "2026-09-19T16:00:00Z",
        "target": dict(TARGET), "counts": {"meetings": 4},
        "counts_sha256": P.digest({"meetings": 4}),
        "schema_projection": [{"table": "meetings", "column": "id",
                               "ordinal": 1, "udt_name": "int8",
                               "nullable": False}],
        "schema_sha256": "s" * 64, "integrity": {"orphan_mentions": 0},
        "integrity_sha256": P.digest({"orphan_mentions": 0}),
        "source_locale": {"provider": "i", "collate": "en_US.UTF-8",
                          "ctype": "en_US.UTF-8"},
        "proposal_preimages": {"count": 7973,
                               "by_table": {"meetings": 1124,
                                            "agenda_items": 6849},
                               "digest": "p" * 64},
    }
    return {**body, "digest": P.digest(body)}


class Resource:
    def __init__(self, events, name):
        self.events, self.name = events, name

    def commit(self):
        self.events.append("snapshot_commit")

    def rollback(self):
        self.events.append("snapshot_rollback")

    def close(self):
        self.events.append("connection_close")

    def dispose(self):
        self.events.append("engine_dispose")


class Adapter:
    def __init__(self, *, remote_hash=None, restored=None, drop_result=True,
                 fail_restore=False):
        self.events = []
        self.remote_hash = remote_hash
        self.restored = restored
        self.drop_result = drop_result
        self.fail_restore = fail_restore

    def open_snapshot(self, target):
        self.events.append("snapshot_export")
        return (Resource(self.events, "engine"), Resource(self.events, "connection"),
                Resource(self.events, "transaction"), "00000003-0000001B-1")

    def dump(self, engine, snapshot, path):
        self.events.append("dump")
        path.write_bytes(b"custom-format-dump")
        os.chmod(path, 0o600)
        return {"toc_sha256": "a" * 64, "toc_entries": 2}

    def copy_off_volume(self, path, remote_path):
        self.events.append("copy")
        return {"hash": self.remote_hash or G.sha256_file(path),
                "bytes": path.stat().st_size, "machine": "DEVHOST",
                "volume": "volume-1"}

    def create_scratch(self, name):
        assert name.startswith(G.SCRATCH_PREFIX)
        self.events.append("scratch_create")
        return {"encoding": "UTF8", "collate": "C", "ctype": "C"}

    def restore_off_volume(self, name, remote_path, **kwargs):
        self.events.append("scratch_restore")
        if self.fail_restore:
            raise RuntimeError("restore failed")
        return {"client_major": 18, "source": "off-volume-roundtrip",
                "sha256_verified": True, "bytes_verified": True}

    def capture_scratch(self, name, expected, locale, candidate):
        self.events.append("scratch_capture")
        return self.restored or {
            "counts": expected["counts"],
            "schema_projection": expected["schema_projection"],
            "integrity": expected["integrity"],
            "proposal_preimages": expected["proposal_preimages"], **locale,
        }

    def drop_scratch(self, name):
        self.events.append("scratch_drop")
        return self.drop_result


@pytest.fixture(autouse=True)
def allowed(monkeypatch):
    monkeypatch.setattr(G.production_interlock, "check",
                        lambda *a, **k: {"status": "ALLOWED"})
    monkeypatch.setattr(G, "capture_baseline", lambda *a, **k: baseline())
    monkeypatch.setattr(G, "capture_proposal_preimages",
                        lambda *a, **k: baseline()["proposal_preimages"])


def execute(tmp_path, adapter, run_tag="20260919T170000Z"):
    candidate_path = tmp_path / "candidate.json"
    candidate(candidate_path)
    return G.run(candidate_path=candidate_path, output_dir=tmp_path / "audit",
                 backup_dir=tmp_path / "backups", adapter=adapter,
                 run_tag=run_tag)


def test_snapshot_remains_open_through_baseline_and_dump_then_scratch(tmp_path):
    adapter = Adapter()
    receipt = execute(tmp_path, adapter)
    assert receipt["status"] == "VALID"
    assert adapter.events == [
        "snapshot_export", "dump", "snapshot_commit", "copy",
        "scratch_create", "scratch_restore", "scratch_capture", "scratch_drop",
        "connection_close", "engine_dispose",
    ]
    assert receipt["dump"]["snapshot_exported"] is True
    assert receipt["scratch"]["absence_proved"] is True
    assert receipt["scratch"]["restore"]["source"] == "off-volume-roundtrip"
    assert receipt["candidate_binding"]["semantic_digest"]
    assert receipt["candidate_binding"]["raw_sha256"]
    assert receipt["candidate_counts"] == {"proposals": 0, "quarantine": 0}
    assert receipt["problems"] == []
    assert all(receipt["comparisons"].values())
    assert receipt["off_volume"]["retained"] is True
    assert receipt["dump"]["toc_entries"] == 2
    for path in (tmp_path / "audit").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_candidate_raw_and_semantic_tampering_are_refused(tmp_path):
    path = tmp_path / "candidate.json"
    artifact = candidate(path)
    artifact["target"]["database"] = "other"
    path.write_text(json.dumps(artifact))
    with pytest.raises(G.Refused, match="semantic digest mismatch"):
        G.load_candidate(path)


def test_exact_target_mismatch_is_refused():
    actual = dict(TARGET)
    actual["cluster_system_identifier"] = "different"
    with pytest.raises(G.Refused, match="cluster_system_identifier"):
        G.compare_target(TARGET, actual)


def test_off_volume_hash_mismatch_refuses_before_scratch(tmp_path):
    adapter = Adapter(remote_hash="0" * 64)
    with pytest.raises(G.Refused, match="failure receipt"):
        execute(tmp_path, adapter)
    assert "scratch_create" not in adapter.events
    failure = json.loads(next((tmp_path / "audit").glob("*-failure.json")).read_text())
    assert failure["status"] == "FAILED"
    assert failure["scratch_absence_proved"] is False


def test_production_preimage_drift_refuses_before_dump(tmp_path, monkeypatch):
    adapter = Adapter()
    monkeypatch.setattr(G, "capture_proposal_preimages",
                        lambda *a, **k: (_ for _ in ()).throw(
                            G.Refused("meetings proposal preimage drift at id 7")))
    with pytest.raises(G.Refused, match="failure receipt"):
        execute(tmp_path, adapter)
    assert "dump" not in adapter.events
    assert "snapshot_rollback" in adapter.events


def test_restored_preimage_drift_refuses_and_drops_scratch(tmp_path):
    expected = baseline()
    restored = {"counts": expected["counts"],
                "schema_projection": expected["schema_projection"],
                "integrity": expected["integrity"],
                "proposal_preimages": {**expected["proposal_preimages"],
                                       "digest": "changed"},
                "encoding": "UTF8", "collate": "C", "ctype": "C"}
    adapter = Adapter(restored=restored)
    with pytest.raises(G.Refused, match="failure receipt"):
        execute(tmp_path, adapter)
    assert "scratch_drop" in adapter.events
    assert not list((tmp_path / "audit").glob("*-receipt.json"))
    failure = json.loads(next((tmp_path / "audit").glob("*-failure.json")).read_text())
    assert failure["verification_problems"] == [
        "restored proposal_preimages differs from exported snapshot"]


def test_off_volume_copy_refuses_existing_remote_path_before_scp(
        tmp_path, monkeypatch):
    dump = tmp_path / "safe.dump"
    dump.write_bytes(b"dump")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "-EncodedCommand" in args:
            decoded = __import__("base64").b64decode(args[-1]).decode("utf-16le")
        else:
            decoded = ""
        if "Test-Path" in decoded:
            raise G.Refused("powershell failed (rc=41)")
        return type("Result", (), {"stdout": ""})()

    monkeypatch.setattr(G, "_run", fake_run)
    remote = G.REMOTE_DIR + "\\" + dump.name
    with pytest.raises(G.Refused):
        G.LiveAdapter().copy_off_volume(dump, remote)
    assert not any(call[0] == "scp" for call in calls)


def test_powershell_transport_uses_one_utf16le_encoded_command(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return type("Result", (), {"stdout": "ok"})()

    monkeypatch.setattr(G, "_run", fake_run)
    script = "New-Item -Force 'C:\\retention' | Out-Null; $x='safe'"
    result = G.LiveAdapter._powershell(script)
    assert result.stdout == "ok"
    assert len(calls) == 1
    command = calls[0]
    assert command[:2] == ["ssh", "-o"]
    assert "powershell.exe" in command
    assert "-NonInteractive" in command
    assert "-EncodedCommand" in command
    assert "-Command" not in command
    encoded = command[command.index("-EncodedCommand") + 1]
    decoded = __import__("base64").b64decode(encoded).decode("utf-16le")
    assert decoded == script
    assert "New-Item" not in command


@pytest.mark.parametrize("failure_kind", ["restore", "comparison", "drop"])
def test_any_scratch_failure_force_drops_and_never_writes_valid_receipt(
        tmp_path, failure_kind):
    expected = baseline()
    restored = None
    if failure_kind == "comparison":
        restored = {"counts": {"meetings": 3},
                    "schema_projection": expected["schema_projection"],
                    "integrity": expected["integrity"], "encoding": "UTF8"}
    adapter = Adapter(fail_restore=failure_kind == "restore", restored=restored,
                      drop_result=failure_kind != "drop")
    with pytest.raises(G.Refused, match="failure receipt"):
        execute(tmp_path, adapter)
    assert adapter.events.count("scratch_drop") >= 1
    assert not list((tmp_path / "audit").glob("*-receipt.json"))


def test_exclusive_artifacts_cannot_be_overwritten(tmp_path):
    first = Adapter()
    execute(tmp_path, first)
    receipt_path = next((tmp_path / "audit").glob("*-receipt.json"))
    before = receipt_path.read_bytes()
    with pytest.raises(G.Refused):
        execute(tmp_path, Adapter())
    assert receipt_path.read_bytes() == before


def test_source_has_no_apply_path_or_secret_material():
    source = (OPS / "production_g6_backup.py").read_text()
    assert "--apply" not in source
    assert "def apply" not in source
    assert "PROD_DATABASE_URL" not in source
    assert "PGPASSWORD" in source  # environment only; never argv or receipt
    adapter = Adapter()
    assert all("password" not in str(value).lower()
               for value in vars(adapter).values())


def test_scratch_bootstrap_schema_is_removed_and_proved_absent_before_restore():
    source = (OPS / "production_g6_backup.py").read_text()
    drop = source.index('DROP SCHEMA IF EXISTS public CASCADE')
    absence = source.index("FROM pg_namespace WHERE nspname='public'")
    restore = source.index("def restore_off_volume(")
    assert drop < absence < restore
    assert "scratch public schema absence was not proved" in source


def test_restore_roundtrip_requires_pg18_and_keeps_password_out_of_argv(
        tmp_path, monkeypatch):
    dump_name = "20260919T170000Z-production-op-repair.dump"
    expected = b"retained-copy"
    calls = []

    monkeypatch.setattr(G, "_require_pg18", lambda version, label: None)
    monkeypatch.setattr(G, "sha256_file",
                        lambda path: __import__("hashlib").sha256(
                            path.read_bytes()).hexdigest())
    monkeypatch.setattr(G, "dotenv_values", None, raising=False)
    monkeypatch.setattr("dotenv.dotenv_values", lambda path: {
        "DATABASE_URL": "postgresql://poliscopic:secret@dev.example/poliscopic_dev"
    })
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "scp":
            Path(args[-1]).write_bytes(expected)
        if args[:2] == ["ssh", "-G"]:
            return type("Result", (), {"stdout": "hostname dev.example\n"})()
        return type("Result", (), {"stdout": "pg_restore (PostgreSQL) 18.4"})()

    monkeypatch.setattr(G, "_run", fake_run)
    result = G.LiveAdapter().restore_off_volume(
        "poliscopic_g6_scratch_20260919_170000",
        G.REMOTE_DIR + "\\" + dump_name,
        expected_sha256=__import__("hashlib").sha256(expected).hexdigest(),
        expected_bytes=len(expected), staging_dir=tmp_path)
    assert result["source"] == "off-volume-roundtrip"
    assert not list(tmp_path.iterdir())
    restore_args, restore_kwargs = calls[-1]
    assert "secret" not in " ".join(restore_args)
    assert restore_kwargs["env"]["PGPASSWORD"] == "secret"
    assert restore_kwargs["env"]["PGDATABASE"].startswith(G.SCRATCH_PREFIX)
    inbound = next(args for args, _ in calls if args[0] == "scp")
    assert inbound[1] == (
        "development-host:C:/retention-hold/g6/" + dump_name)
    assert "\\" not in inbound[1]


def test_restore_refuses_a_dev_url_not_on_the_approved_windows_host(
        tmp_path, monkeypatch):
    monkeypatch.setattr("dotenv.dotenv_values", lambda path: {
        "DATABASE_URL": "postgresql://poliscopic:secret@other.example/poliscopic_dev"
    })
    monkeypatch.setattr(
        G, "_run",
        lambda args, **kwargs: type("Result", (), {
            "stdout": "hostname approved.example\n"
        })())
    with pytest.raises(G.Refused, match="approved Windows host"):
        G.LiveAdapter().restore_off_volume(
            "poliscopic_g6_scratch_20260919_170000",
            G.REMOTE_DIR + "\\20260919T170000Z-production-op-repair.dump",
            expected_sha256="a" * 64, expected_bytes=1,
            staging_dir=tmp_path)


def test_failure_receipt_records_safe_phase_without_error_detail(tmp_path):
    adapter = Adapter(remote_hash="0" * 64)
    with pytest.raises(G.Refused):
        execute(tmp_path, adapter)
    failure = json.loads(next((tmp_path / "audit").glob("*-failure.json")).read_text())
    assert failure["phase"] == "copy"
    assert "error" not in failure
    assert "message" not in failure


def test_verify_failure_records_only_safe_step_code(tmp_path):
    class VerifyAdapter(Adapter):
        last_verification_step = "count:meetings"

        def capture_scratch(self, *args, **kwargs):
            raise G.Refused("sensitive backend detail")

    with pytest.raises(G.Refused):
        execute(tmp_path, VerifyAdapter())
    failure = json.loads(next((tmp_path / "audit").glob("*-failure.json")).read_text())
    assert failure["verification_step"] == "count:meetings"
    assert "sensitive" not in json.dumps(failure)


def test_windows_preimage_batches_stay_below_encoded_command_ceiling():
    source = (OPS / "production_g6_backup.py").read_text()
    assert "batch_size = 200" in source
    ids = ",".join(str(value) for value in range(100000, 100200))
    sql = ("SELECT row_to_json(q) FROM (SELECT id, meeting_id "
           f"FROM public.agenda_items WHERE id IN ({ids}) ORDER BY id) q")
    script = ("$q=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" +
              __import__("base64").b64encode(sql.encode()).decode() + "')); & $p -c $q")
    encoded = __import__("base64").b64encode(script.encode("utf-16le"))
    assert len(encoded) < 8191
