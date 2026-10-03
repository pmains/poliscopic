"""Lifecycle tests for the incremental database-session ownership boundary."""

from __future__ import annotations

import pytest

from poliscopic.db import core


class RecordingSession:
    def __init__(self, *, commit_error: BaseException | None = None):
        self.commit_error = commit_error
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


def install_session(monkeypatch, **kwargs) -> RecordingSession:
    session = RecordingSession(**kwargs)
    monkeypatch.setattr(core, "get_session", lambda: session)
    return session


def test_session_scope_closes_after_success(monkeypatch):
    session = install_session(monkeypatch)

    with core.session_scope() as yielded:
        assert yielded is session
        yielded.events.append("work")

    assert session.events == ["work", "close"]


def test_session_scope_rolls_back_and_closes_after_error(monkeypatch):
    session = install_session(monkeypatch)

    with pytest.raises(ValueError, match="failed read"):
        with core.session_scope():
            raise ValueError("failed read")

    assert session.events == ["rollback", "close"]


def test_transaction_scope_commits_then_closes(monkeypatch):
    session = install_session(monkeypatch)

    with core.transaction_scope() as yielded:
        assert yielded is session
        yielded.events.append("write")

    assert session.events == ["write", "commit", "close"]


def test_transaction_scope_rolls_back_when_work_fails(monkeypatch):
    session = install_session(monkeypatch)

    with pytest.raises(RuntimeError, match="failed write"):
        with core.transaction_scope():
            raise RuntimeError("failed write")

    assert session.events == ["rollback", "close"]


def test_transaction_scope_rolls_back_when_commit_fails(monkeypatch):
    session = install_session(monkeypatch, commit_error=RuntimeError("failed commit"))

    with pytest.raises(RuntimeError, match="failed commit"):
        with core.transaction_scope():
            pass

    assert session.events == ["commit", "rollback", "close"]
