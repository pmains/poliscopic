"""Offline adversarial tests for the one-process Serenity bootstrap."""

from pathlib import Path

import pytest

from scripts.kg import stage3_processing_receipt_serenity_bootstrap as B
from scripts.kg import stage3_processing_receipt_store_backup as Backup
from scripts.kg.stage2_artifacts import load_verified, write_immutable


def _artifact(path: Path, body: dict) -> tuple[Path, dict]:
    write_immutable(path, body)
    return path, load_verified(path)


def _plan(path: Path):
    return _artifact(path, {"digest": "p" * 64, "target": {"tier": "development"}})


def _design(path: Path):
    return _artifact(path, {"digest": "d" * 64})


def test_intent_is_write_once_and_refuses_parameter_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(B.apply, "code_digest", lambda: "c" * 64)
    plan_path, plan = _plan(tmp_path / "plan.json")
    design_path, design = _design(tmp_path / "design.json")
    intent_path = B._phase_path(tmp_path, "run-001", "intent")
    kwargs = dict(run_id="run-001", plan=plan, design=design, approver="Peter Mains",
                  writer_role="poliscopic", backup_dir=tmp_path / "backups",
                  packet_path=tmp_path / "packet.json", report_path=tmp_path / "report.pending",
                  seed_paths=[plan_path])
    value = B._intent(intent_path, **kwargs)
    assert B._intent(intent_path, **kwargs)["digest"] == value["digest"]
    with pytest.raises(B.BootstrapRefused, match="intent differs"):
        B._intent(intent_path, **{**kwargs, "writer_role": "other"})


def test_phase_artifact_must_be_owned_by_its_exact_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(B.apply, "code_digest", lambda: "c" * 64)
    _, plan = _plan(tmp_path / "plan.json")
    _, design = _design(tmp_path / "design.json")
    intent_path = B._phase_path(tmp_path, "run-002", "intent")
    intent = B._intent(intent_path, run_id="run-002", plan=plan, design=design,
                       approver="Peter Mains", writer_role="poliscopic",
                       backup_dir=tmp_path / "backups", packet_path=tmp_path / "packet.json",
                       report_path=tmp_path / "report.pending", seed_paths=[])
    artifact_path, artifact = _artifact(tmp_path / "thing.json", {"kind": "thing"})
    phase = B._phase(intent_path, intent, phase="backup", artifact=B._ref(artifact_path, artifact))
    assert B._read_phase(intent_path, intent, "backup")["digest"] == phase["digest"]
    altered = dict(phase); altered.pop("digest"); altered["run_id"] = "other"
    phase_path = B._phase_path(tmp_path, "run-002", "backup")
    phase_path.unlink(); write_immutable(phase_path, altered)
    with pytest.raises(B.BootstrapRefused, match="not owned"):
        B._read_phase(intent_path, intent, "backup")


def test_seed_requires_exact_contiguous_offset_6100(tmp_path, monkeypatch):
    values = {"first": (5100, 5100), "second": (6100, 1000),
              "gap": (6000, 1000), "short": (5500, 400)}
    monkeypatch.setattr(B.serenity, "_seed", lambda path, plan: (
        values[path.name][0], {"selected": values[path.name][1]}, {}))
    assert B._seed([Path("first"), Path("second")], {"digest": "p"}) == 6100
    with pytest.raises(B.BootstrapRefused, match="not contiguous"):
        B._seed([Path("first"), Path("gap")], {"digest": "p"})
    with pytest.raises(B.BootstrapRefused, match="must end"):
        B._seed([Path("first"), Path("short")], {"digest": "p"})


def test_bootstrap_handoff_uses_verified_owned_prerequisites_without_exec(tmp_path, monkeypatch):
    for directory in (tmp_path / "state", tmp_path / "backups", tmp_path / "plans", tmp_path / "bridge"):
        directory.mkdir()
    plan_path, _plan_doc = _artifact(tmp_path / "plan.json", {"target": {"tier": "development"}})
    design_path, _design_doc = _artifact(tmp_path / "design.json", {"kind": "design"})
    backup_path, backup_doc = _artifact(tmp_path / "verified-backup.json", {"kind": "backup"})
    packet_path, packet_doc = _artifact(tmp_path / "verified-packet.json", {"kind": "packet"})
    monkeypatch.setattr(B, "_seed", lambda paths, plan: 6100)
    monkeypatch.setattr(B, "_backup", lambda *args, **kwargs: (backup_path, backup_doc))
    monkeypatch.setattr(B, "_packet", lambda *args, **kwargs: (packet_path, packet_doc))
    monkeypatch.setattr(B.apply, "code_digest", lambda: "c" * 64)
    result = B.bootstrap(run_id="run-003", plan_path=plan_path, design_path=design_path,
                         approver="Peter Mains", writer_role="poliscopic",
                         state_dir=tmp_path / "state", backup_root=tmp_path / "backups",
                         packet_dir=tmp_path / "plans", report_path=tmp_path / "bridge" / "done.pending",
                         seed_paths=[], exec_runner=False)
    assert result["offset"] == 6100
    assert Path(result["handoff"]).is_file()


def test_bootstrap_source_uses_canonical_backup_and_exec_not_a_second_controller():
    source = Path(B.__file__).read_text()
    assert "stage3_processing_receipt_backup_run.py" in source
    assert "os.execv" in source
    assert "_notify" in source and "REPORT" in source
    assert "stage3_processing_receipt_serenity_runner.py" in source
    assert "CREATE TABLE" not in source and "UPDATE supporting_documents" not in source
    bound = Path(B.apply.__file__).read_text()
    assert "stage3_processing_receipt_serenity_bootstrap.py" in bound
    assert "stage3_processing_receipt_backup_run.py" in bound


def test_backup_digest_is_chunked_not_a_large_read(tmp_path):
    path = tmp_path / "dump.bin"
    path.write_bytes(b"abcdefgh")
    assert Backup.file_sha256(path, chunk_size=3) == __import__("hashlib").sha256(b"abcdefgh").hexdigest()
    assert "read_bytes" not in Backup.file_sha256.__doc__
    source = Path(Backup.__file__).with_name("stage3_processing_receipt_backup_run.py").read_text()
    assert "dump_path.read_bytes()" not in source
