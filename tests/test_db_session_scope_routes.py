"""Route pilots for the incremental database-session ownership migration."""

from contextlib import contextmanager


def test_theme_preview_uses_scoped_session(monkeypatch):
    from routes import themes

    values = [["article"], ["featured"], ["tag"]]
    events = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class Session:
        def execute(self, statement):
            events.append("execute")
            return Result(values.pop(0))

    @contextmanager
    def scoped_session():
        events.append("open")
        try:
            yield Session()
        finally:
            events.append("close")

    monkeypatch.setattr(themes, "session_scope", scoped_session)
    monkeypatch.setattr(themes, "render_template", lambda *args, **kwargs: kwargs)

    rendered = themes.theme_preview(1)

    assert rendered["articles"] == ["article"]
    assert rendered["featured"] == ["featured"]
    assert rendered["tags"] == ["tag"]
    assert events == ["open", "execute", "execute", "execute", "close"]


def test_create_notification_uses_transaction_scope(monkeypatch):
    from routes import admin

    added = []

    class Session:
        def add(self, value):
            added.append(value)

    @contextmanager
    def transaction():
        yield Session()

    monkeypatch.setattr(admin, "transaction_scope", transaction)

    admin.create_notification("Scrape complete", "/admin", article_id=42)

    assert len(added) == 1
    assert added[0].message == "Scrape complete"
    assert added[0].url == "/admin"
    assert added[0].article_id == 42
