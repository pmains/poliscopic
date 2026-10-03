"""Regression tests for public web trust boundaries."""

import re

from markupsafe import Markup


def test_search_highlight_escapes_source_html_and_preserves_mark_tags():
    from routes import _safe_search_highlight

    rendered = _safe_search_highlight(
        '<img src=x onerror="alert(1)"> <mark>water</mark>'
    )

    assert isinstance(rendered, Markup)
    assert "<img" not in rendered
    assert "&lt;img" in rendered
    assert "<mark>water</mark>" in rendered


def test_mention_highlight_escapes_both_context_and_mention():
    from routes import _highlight_mention

    rendered = _highlight_mention(
        '<script>alert(1)</script> Acme & Sons',
        "Acme & Sons",
    )

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<mark>Acme &amp; Sons</mark>" in rendered


def test_login_next_url_accepts_only_local_absolute_paths():
    from routes.auth import _is_safe_next_url

    assert _is_safe_next_url("/admin/articles?status=draft")
    assert not _is_safe_next_url("https://attacker.example/phish")
    assert not _is_safe_next_url("//attacker.example/phish")
    assert not _is_safe_next_url("admin/articles")
    assert not _is_safe_next_url(None)


def test_internal_tools_are_disabled_by_default(fresh_session, monkeypatch):
    monkeypatch.delenv("POLISCOPIC_ENABLE_INTERNAL_TOOLS", raising=False)
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "true")

    from routes import create_app

    app = create_app()
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert app.config["POLISCOPIC_INTERNAL_TOOLS_ENABLED"] is False
    assert "/annotation/" not in rules
    assert "/entity-viewer" not in rules
    assert "/kg-quality-review/" not in rules


def test_internal_tools_cannot_override_disabled_admin(fresh_session, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_ENABLE_INTERNAL_TOOLS", "true")
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "true")

    from routes import create_app

    app = create_app()
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert app.config["POLISCOPIC_INTERNAL_TOOLS_ENABLED"] is False
    assert "/annotation/" not in rules


def test_enabled_internal_tools_require_login(fresh_session, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_ENABLE_INTERNAL_TOOLS", "true")
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "false")

    from routes import create_app

    app = create_app()
    app.config.update(TESTING=True)
    response = app.test_client().get("/entity-viewer")

    assert app.config["POLISCOPIC_INTERNAL_TOOLS_ENABLED"] is True
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?next=")


def test_browser_mutations_require_csrf(fresh_session, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "false")
    monkeypatch.delenv("POLISCOPIC_ENABLE_INTERNAL_TOOLS", raising=False)

    from routes import create_app

    app = create_app()
    app.config.update(TESTING=True)
    response = app.test_client().post(
        "/login",
        data={"username": "nobody", "password": "wrong"},
    )

    assert app.config["WTF_CSRF_ENABLED"] is True
    assert response.status_code == 400


def test_login_accepts_session_csrf_and_rejects_external_next(
    fresh_session, monkeypatch
):
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "false")
    monkeypatch.delenv("POLISCOPIC_ENABLE_INTERNAL_TOOLS", raising=False)

    from routes import create_app

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    login_page = client.get("/login").get_data(as_text=True)
    token_match = re.search(r'var csrfToken = "([^"]+)";', login_page)
    assert token_match
    token = token_match.group(1)

    response = client.post(
        "/login?next=https://attacker.example/phish",
        data={
            "username": "nobody",
            "password": "wrong",
            "csrf_token": token,
        },
    )

    # The request crossed the CSRF boundary and reached normal credential
    # validation; invalid credentials render the form instead of redirecting.
    assert response.status_code == 200
    assert "Invalid username or password" in response.get_data(as_text=True)


def test_logout_is_post_only(fresh_session, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "false")

    from routes import create_app

    app = create_app()
    logout_rule = next(rule for rule in app.url_map.iter_rules() if rule.rule == "/logout")

    assert logout_rule.methods >= {"POST"}
    assert "GET" not in logout_rule.methods


def test_newsletter_keeps_its_scoped_csrf_protocol(fresh_session, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "true")

    from routes import _csrf, create_app
    from routes.newsletter import newsletter_bp

    create_app()

    assert newsletter_bp in _csrf._exempt_blueprints


def test_production_session_security_fails_closed():
    from routes import _session_security_settings

    try:
        _session_security_settings({"POLISCOPIC_DB_TIER": "production"})
    except RuntimeError as error:
        assert "FLASK_SECRET_KEY" in str(error)
    else:
        raise AssertionError("production accepted the development secret")

    try:
        _session_security_settings(
            {
                "POLISCOPIC_DB_TIER": "production",
                "FLASK_SECRET_KEY": "a-production-only-secret",
                "POLISCOPIC_COOKIE_SECURE": "false",
            }
        )
    except RuntimeError as error:
        assert "POLISCOPIC_COOKIE_SECURE" in str(error)
    else:
        raise AssertionError("production accepted insecure cookies")


def test_production_session_security_defaults_to_secure_cookies():
    from routes import _session_security_settings

    secret, secure = _session_security_settings(
        {
            "POLISCOPIC_DB_TIER": "production",
            "FLASK_SECRET_KEY": "a-production-only-secret",
        }
    )

    assert secret == "a-production-only-secret"
    assert secure is True


def test_create_app_does_not_seed_or_initialize_database(
    fresh_session, monkeypatch
):
    monkeypatch.setenv("POLISCOPIC_DISABLE_ADMIN", "true")
    import db
    import db.newsroom

    def unexpected_write(*args, **kwargs):
        raise AssertionError("create_app attempted database bootstrap")

    monkeypatch.setattr(db, "seed_default_jurisdictions", unexpected_write)
    monkeypatch.setattr(db.newsroom, "init_newsroom_db", unexpected_write)
    monkeypatch.setattr(db.newsroom, "seed_default_tags", unexpected_write)
    monkeypatch.setattr(db.newsroom, "seed_default_users", unexpected_write)
    monkeypatch.setattr(db.newsroom, "seed_default_topics", unexpected_write)

    from routes import create_app

    app = create_app()

    assert app.url_map is not None


def test_explicit_bootstrap_refuses_production(monkeypatch):
    from scripts import bootstrap_app_db

    monkeypatch.setattr(bootstrap_app_db, "DB_TIER", "production")

    try:
        bootstrap_app_db.bootstrap()
    except RuntimeError as error:
        assert "OP-SCHEMA" in str(error)
    else:
        raise AssertionError("application bootstrap accepted production")


def test_explicit_bootstrap_orders_noncredential_seed_steps(monkeypatch):
    from scripts import bootstrap_app_db

    calls = []
    monkeypatch.setattr(bootstrap_app_db, "DB_TIER", "test")
    monkeypatch.setattr(bootstrap_app_db, "init_db", lambda: calls.append("schema"))
    monkeypatch.setattr(
        bootstrap_app_db,
        "init_newsroom_db",
        lambda: calls.append("newsroom"),
    )
    monkeypatch.setattr(
        bootstrap_app_db,
        "seed_default_tags",
        lambda: calls.append("tags"),
    )
    monkeypatch.setattr(
        bootstrap_app_db,
        "seed_default_topics",
        lambda: calls.append("topics"),
    )

    bootstrap_app_db.bootstrap()

    assert calls == ["schema", "newsroom", "tags", "topics"]
    assert "users" not in calls
