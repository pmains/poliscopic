"""Contracts for the Phoenix planning registry adapter."""

from types import SimpleNamespace


def test_adapter_closes_owned_session(monkeypatch):
    import db
    from scraper.jurisdictions import phoenix_planning, phoenix_planning_adapter

    events = []

    class Session:
        def close(self):
            events.append("close")

    monkeypatch.setattr(db, "init_db", lambda: events.append("init"))
    monkeypatch.setattr(db, "get_session", lambda: Session())
    monkeypatch.setattr(
        phoenix_planning,
        "sync_all",
        lambda _session, *, force: events.append(("sync", force))
        or {
            "events": {"synced": 1, "fetched": 2},
            "staff_reports": {"docs_synced": 3, "fetched": 4},
            "pud_cases": {"docs_synced": 5, "fetched": 6},
        },
    )

    assert phoenix_planning_adapter.sync(SimpleNamespace(force=True)) == 0
    assert events == ["init", ("sync", True), "close"]
