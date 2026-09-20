"""Tests for the one-shot Poliscopic-to-Codex completion bridge."""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts.tools import poliscopic_codex_bridge as bridge


def _response(text: str = "REPORT: done", *, aborted: bool = False) -> str:
    return json.dumps({
        "status": "ok",
        "result": {
            "payloads": [{"text": text, "mediaUrl": None}],
            "meta": {"aborted": aborted},
        },
    })


def test_dispatch_requests_json_and_returns_terminal_report(monkeypatch) -> None:
    observed: list[str] = []

    def fake_run(command, *, stdin=None):
        observed.extend(command)
        return subprocess.CompletedProcess(command, 0, _response(), "")

    monkeypatch.setattr(bridge, "_run", fake_run)

    assert bridge._dispatch("session", "prompt", 60) == "REPORT: done"
    assert "--json" in observed


def test_default_delivery_uses_configuration_not_checked_in_ids() -> None:
    assert bridge.DEFAULT_CODEX_THREAD == ""
    assert bridge.DEFAULT_MANAGER_THREAD == ""


def test_delivery_resumes_inbox_and_requires_delivery_proof(monkeypatch, tmp_path) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, *, stdin=None):
        observed["command"] = tuple(command)
        observed["stdin"] = stdin
        pending.rename(pending.with_suffix(".delivered"))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(bridge, "_run", fake_run)
    pending = tmp_path / "report.pending"
    pending.write_text("REPORT: done\n", encoding="utf-8")
    bridge._deliver("inbox-thread", pending, "manager-thread")

    assert observed["command"] == (
        str(bridge.CODEX), "exec", "resume", "inbox-thread", "-"
    )
    prompt = str(observed["stdin"])
    assert str(pending.resolve()) in prompt
    assert "send_message_to_thread" in prompt
    assert "manager-thread" in prompt
    assert "requests its next bounded task" in prompt
    assert "rename only that exact file" in prompt
    assert "REPORT: enormous worker body" not in prompt


def test_delivery_fails_if_inbox_does_not_confirm_forward(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        bridge,
        "_run",
        lambda command, *, stdin=None: subprocess.CompletedProcess(command, 0, "", ""),
    )
    pending = tmp_path / "report.pending"
    pending.write_text("REPORT: done\n", encoding="utf-8")

    with pytest.raises(bridge.BridgeError, match="without proving manager delivery"):
        bridge._deliver("inbox-thread", pending, "manager-thread")

    assert pending.exists()


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        json.dumps({"status": "error", "result": {}}),
        json.dumps({"status": "ok", "result": {}}),
        json.dumps({"status": "ok", "result": {"payloads": []}}),
        json.dumps({
            "status": "ok",
            "result": {"payloads": [{"text": "REPORT: stopped"}],
                       "meta": {"aborted": True}},
        }),
        _response("ordinary reply"),
    ],
)
def test_terminal_report_parser_fails_closed(output: str) -> None:
    with pytest.raises(bridge.BridgeError):
        bridge._terminal_report_from_json(output)
