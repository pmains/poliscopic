"""Adversarial offline tests for the durable Serenity receipt controller."""

from pathlib import Path

import pytest

from scripts.kg import stage3_processing_receipt_continue as C
from scripts.kg import stage3_processing_receipt_serenity_runner as S
from scripts.kg.stage2_artifacts import load_verified, write_immutable


def _plan(records=500):
    return {"digest": "p" * 64, "records": [{"source_id": number + 1} for number in range(records)]}


def _packet():
    return {"digest": "a" * 64, "batch_size": 500}


def _preflight(path: Path):
    body = {"kind": "kg-stage3-processing-receipt-preflight", "version": "1.0"}
    write_immutable(path, body)
    return load_verified(path)


def _terminal(path: Path, *, offset=0, selected=500, preflight="d" * 64, failed=0):
    body = {"kind": "kg-stage3-processing-receipt-apply-terminal", "version": "1.0",
            "authorized_packet_digest": "a" * 64, "plan_digest": "p" * 64,
            "preflight_digest": preflight, "offset": offset, "selected": selected,
            "success": selected - failed, "failed": failed, "held": 0, "replay": 0,
            "swept_at_updates": 0, "outcome": "applied"}
    body["digest"] = C.apply.receipt.canonical_sha256(body)
    write_immutable(path, body)
    return load_verified(path)


def _continuation(path: Path, *, start=0, selected=500):
    window = {"offset": start, "selected": selected, "success": selected, "failed": 0,
              "held": 0, "replay": 0}
    body = {"kind": C.KIND, "version": C.VERSION, "plan_digest": "p" * 64,
            "start_offset": start, "outcome": "complete_window", "windows": [window],
            "totals": {"selected": selected, "success": selected, "failed": 0,
                       "held": 0, "replay": 0, "swept_at_updates": 0}}
    write_immutable(path, body)
    return path


def test_seed_requires_contiguous_complete_zero_failure_evidence(tmp_path):
    first, second = _continuation(tmp_path / "first.json"), _continuation(tmp_path / "second.json", start=500)
    assert S._seed(first, _plan())[0] == 500
    assert S._seed(second, _plan())[0] == 1000
    bad = load_verified(first)
    bad["outcome"] = "stopped"
    bad_path = tmp_path / "bad.json"
    write_immutable(bad_path, bad)
    with pytest.raises(S.SerenityRefused, match="does not bind"):
        S._seed(bad_path, _plan())


def test_recovered_terminal_needs_the_exact_historical_preflight(tmp_path):
    terminals, preflights = tmp_path / "terminals", tmp_path / "preflights"
    terminals.mkdir(); preflights.mkdir()
    pref = _preflight(preflights / "kg-stage3-processing-receipt-preflight-ok.json")
    term = _terminal(terminals / "kg-stage3-processing-receipt-apply-ok.json", preflight=pref["digest"])
    recovered = S._recovered_terminal(terminals, preflights, packet=_packet(), plan=_plan(), offset=0, selected=500)
    assert recovered and recovered[1]["digest"] == term["digest"]
    (preflights / "kg-stage3-processing-receipt-preflight-ok.json").unlink()
    with pytest.raises(S.SerenityRefused, match="no immutable matching preflight"):
        S._recovered_terminal(terminals, preflights, packet=_packet(), plan=_plan(), offset=0, selected=500)


def test_checkpoint_chain_refuses_failed_or_forked_heads(tmp_path):
    checkpoints, backup = tmp_path / "checkpoints", tmp_path / "backup.json"
    checkpoints.mkdir(); backup.write_text("backup")
    plan, packet = _plan(), _packet()
    boot_path, boot = S._bootstrap(checkpoints, seed_paths=[], plan=plan, packet=packet, backup=backup)
    assert boot["offset"] == 0
    S._verify_chain(boot_path, boot, plan=plan, packet=packet, backup=backup)
    second = dict(boot)
    second.pop("digest")
    write_immutable(checkpoints / "kg-stage3-processing-receipt-serenity-fork.json", second)
    with pytest.raises(S.SerenityRefused, match="more than one"):
        S._heads(checkpoints, plan=plan, packet=packet, backup=backup)


def test_report_is_atomic_pending_and_refuses_overwrite(tmp_path):
    report = tmp_path / "done.pending"
    S._report(report, outcome="complete", checkpoint=None, error=None, notified="not configured")
    assert report.read_text().startswith("REPORT:")
    with pytest.raises(S.SerenityRefused, match="new .pending"):
        S._report(report, outcome="complete", checkpoint=None, error=None, notified="not configured")


def test_run_checkpoints_then_resumes_without_reinvoking_completed_batch(tmp_path, monkeypatch):
    terminals, preflights, checkpoints, bridge = (tmp_path / name for name in ("terminals", "preflights", "checkpoints", "bridge"))
    for directory in (terminals, preflights, checkpoints, bridge):
        directory.mkdir()
    backup = tmp_path / "backup.json"; backup.write_text("backup")
    plan, packet = _plan(), _packet()
    pref_path = preflights / "kg-stage3-processing-receipt-preflight-test.json"
    pref = _preflight(pref_path)
    monkeypatch.setattr(S, "_fresh_preflight", lambda *_args, **_kwargs: (pref_path, pref))
    calls = []

    def fake_apply(_engine, **kwargs):
        calls.append(kwargs["offset"])
        path = terminals / "kg-stage3-processing-receipt-apply-test.json"
        value = _terminal(path, offset=kwargs["offset"], preflight=pref["digest"])
        return {**value, "terminal_receipt_path": str(path)}

    monkeypatch.setattr(S.apply, "apply_batch", fake_apply)
    result = S.run(object(), plan=plan, design={"digest": "d" * 64}, packet=packet,
                   backup=backup, token="token", terminal_dir=terminals,
                   preflight_dir=preflights, checkpoint_dir=checkpoints, seed_paths=[],
                   renewal_seconds=60, report_out=bridge / "first.pending")
    assert result["offset"] == 500 and calls == [0]
    monkeypatch.setattr(S.apply, "apply_batch", lambda *_args, **_kwargs: pytest.fail("completed batch replayed"))
    resumed = S.run(object(), plan=plan, design={"digest": "d" * 64}, packet=packet,
                    backup=backup, token="token", terminal_dir=terminals,
                    preflight_dir=preflights, checkpoint_dir=checkpoints, seed_paths=[],
                    renewal_seconds=60, report_out=bridge / "second.pending")
    assert resumed["offset"] == 500


def test_runner_source_forbids_unbounded_streaming_and_requires_recovery():
    source = Path(S.__file__).read_text()
    assert "apply.apply_batch" in source
    assert "recover_terminal" in source
    assert "preflight renewal interval" in source
    assert "notify_codex_thread.py" in source
    assert "while checkpoint[\"offset\"] < len(records)" in source
    assert "UPDATE supporting_documents SET swept_at" not in source
    assert "subprocess.run(command" in source
