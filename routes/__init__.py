"""Flask routes package — app factory and blueprint registration."""

import logging
import os
import sys
import time
from functools import wraps
from pathlib import Path
from flask import Flask
from typing import Callable

log = logging.getLogger(__name__)

_here = Path(__file__).resolve().parent.parent  # repo root
_scripts_dir = _here / "scripts"

sys.path.insert(0, str(_scripts_dir))

# Load .env so DATABASE_URL is available
from dotenv import load_dotenv
load_dotenv(_here / ".env")

_database_url = os.environ.get("DATABASE_URL")
if not _database_url:
    _database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _database_url

# Startup diagnostic: redact through the tier module's single authority so a
# raw URL (which carries the password) can never reach stderr/journald.
def _safe_database_target(url: str | None) -> str:
    if not url:
        return "(not set)"
    try:
        from db.tier import redacted_url

        return redacted_url(url)
    except Exception:  # pragma: no cover - import-time safety net
        try:
            from urllib.parse import urlsplit

            parts = urlsplit(str(url))
            location = parts.hostname or "(local)"
            if parts.port is not None:
                location = f"{location}:{parts.port}"
            database = (parts.path or "").lstrip("/") or "(none)"
            return f"{parts.scheme or '?'} {location}/{database}"
        except Exception:
            return "(unparseable target)"


print(f"Database target: {_safe_database_target(_database_url)}", file=sys.stderr)


# ── Cache version — bump to invalidate all cached pages ──────────────────
_CACHE_VERSION = "v10"

# ── Shared template constants ────────────────────────────────────────────
SYNC_STATUS_BADGES = {
    "complete": "success",
    "failed": "danger",
    "partial": "warning",
    "manual_review": "secondary",
    "pending": "info",
}

_cache_instance = None


def get_cache():
    """Return the shared cache instance (set during create_app)."""
    return _cache_instance


def _cache(timeout=60, query_string=False):
    """Apply Flask-Caching if available, otherwise no-op.

    Versions the cache key via _CACHE_VERSION so reclassification or
    data migrations naturally invalidate stale cached pages.
    """
    if _cache_instance:
        original_cached = _cache_instance.cached(timeout=timeout, query_string=query_string)

        def _wrapper(fn):
            @wraps(fn)
            def _versioned(*args, **kwargs):
                from flask import request
                old = dict(request.args) if hasattr(request, 'args') else {}
                try:
                    if hasattr(request, 'args'):
                        request.args = request.args.copy()
                        request.args['_cv'] = _CACHE_VERSION
                    return original_cached(fn)(*args, **kwargs)
                finally:
                    if old and hasattr(request, 'args'):
                        request.args = type(request.args)(old)
            return _versioned
        return _wrapper
    return lambda f: f


def create_app() -> Flask:
    """Create and configure the Flask application."""
    from flask import Flask, render_template, request

    app = Flask(__name__,
                 template_folder=str(_here / "templates"),
                 static_folder=str(_here / "static"))

    # ── Cache setup ──────────────────────────────────────────────────────
    global _cache_instance
    try:
        from flask_caching import Cache
        _cache_instance = Cache(app, config={
            "CACHE_TYPE": "FileSystemCache",
            "CACHE_DIR": str(_here / ".cache" / "flask-cache"),
            "CACHE_DEFAULT_TIMEOUT": 60,
            "CACHE_THRESHOLD": 200,
        })
        log.info("Flask-Caching enabled (FileSystemCache, 60s default)")
    except ImportError:
        _cache_instance = None
        log.warning("Flask-Caching not installed — install with: pip install Flask-Caching")

    # ── Seed default data on startup ─────────────────────────────────────
    from db import seed_default_jurisdictions
    seed_default_jurisdictions()

    # ── Request timing ───────────────────────────────────────────────────
    @app.before_request
    def _start_timer():
        request._start_time = time.monotonic()

    @app.after_request
    def _log_timing(response):
        elapsed = time.monotonic() - getattr(request, "_start_time", time.monotonic())
        if elapsed > 1.0:
            log.warning("%s %.1fs", request.path, elapsed)
        return response

    # ── Login manager ────────────────────────────────────────────────────
    _disable_admin = os.environ.get("POLISCOPIC_DISABLE_ADMIN", "").lower() in ("true", "1", "yes")

    from flask_login import LoginManager
    from db.newsroom import AdminUser

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def _load_user(user_id):
        from db.core import get_session
        from sqlalchemy import select
        session = get_session()
        user = session.get(AdminUser, int(user_id))
        session.close()
        return user

    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

    # Explicit session cookie settings for broader browser compatibility
    _secure_cookies = os.environ.get("POLISCOPIC_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
    app.config.update(
        SESSION_COOKIE_NAME="poliscopic_session",  # Avoid conflicts with old cookies
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=_secure_cookies,  # True on prod (https); False on localhost
        PERMANENT_SESSION_LIFETIME=3600 * 24,  # 24 hours
        SESSION_REFRESH_EACH_REQUEST=False,
        WTF_CSRF_ENABLED=False,  # Disable CSRF for dev
    )

    # ── Security headers (OWASP: nosniff / clickjacking / referrer) ────
    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Content-Security-Policy is intentionally NOT set globally: legacy
        # templates use inline styles/scripts.  Newsletter pages inherit only
        # the safe subset above.
        return response

    # ── Markdown filter ──────────────────────────────────────────────────
    import markdown as _md

    @app.template_filter("markdown")
    def _render_markdown(text):
        if not text:
            return ""
        return _md.markdown(
            text,
            extensions=["fenced_code", "tables", "sane_lists"],
        )

    @app.template_filter("split_at_first_meeting")
    def _split_at_first_meeting(md):
        """Split markdown body at the first '## ' heading (a meeting section).

        Returns (lede_md, meetings_md).  When there is no meeting heading,
        meetings_md is "" so callers can fall back to the whole body.
        """
        if not md:
            return "", ""
        idx = md.find("\n## ")
        if idx == -1:
            return md, ""
        return md[:idx], md[idx + 1:]

    import html as _html
    import re as _re
    import unicodedata as _ud

    def _anchor_slug(text: str) -> str:
        s = _ud.normalize("NFKD", text).encode("ascii", "ignore").decode()
        s = _re.sub(r"[^\w\s-]", "", s).strip().lower()
        s = _re.sub(r"[\s_]+", "-", s)
        return _re.sub(r"-{2,}", "-", s) or "section"

    @app.template_filter("newsletter_sections")
    def _newsletter_sections(md):
        """Parse a newsletter body into a lede, a TOC, and anchored sections.

        Returns {"lede_html", "groups", "body_html", "count"}.  Each `## `
        heading becomes an anchored `<h2 id=...>`, and ``groups`` bundles
        sections that share a body name.  A body meeting twice (an upcoming
        meeting plus last week's) therefore yields ONE group holding both
        entries, so both stay visible and jumpable (Pete 2026-09-17).
        """
        if not md:
            return {"lede_html": "", "groups": [], "body_html": "", "count": 0}
        idx = md.find("\n## ")
        if idx == -1:
            return {"lede_html": _render_markdown(md), "groups": [],
                    "body_html": "", "count": 0}

        lede_md, rest = md[:idx], md[idx + 1:]
        raw, cur = [], None
        for ln in rest.split("\n"):
            if ln.startswith("## "):
                if cur is not None:
                    raw.append(cur)
                cur = {"heading": ln[3:].strip(), "lines": []}
            elif cur is not None:
                cur["lines"].append(ln)
        if cur is not None:
            raw.append(cur)

        groups, index, html_parts, used = [], {}, [], set()
        for sec in raw:
            heading = sec["heading"]
            if " — " in heading:
                name, date = (p.strip() for p in heading.split(" — ", 1))
            else:
                name, date = heading, ""
            slug, n = _anchor_slug(heading), 2
            while slug in used:
                slug = f"{_anchor_slug(heading)}-{n}"
                n += 1
            used.add(slug)
            html_parts.append(
                f'<h2 id="{slug}">{_html.escape(heading)}</h2>\n'
                + _render_markdown("\n".join(sec["lines"]).strip())
            )
            entry = {"id": slug, "name": name, "date": date, "label": heading}
            index.setdefault(name, {"name": name, "entries": []})
            if index[name]["entries"] == []:
                groups.append(index[name])
            index[name]["entries"].append(entry)

        return {"lede_html": _render_markdown(lede_md), "groups": groups,
                "body_html": "\n".join(html_parts),
                "count": sum(len(g["entries"]) for g in groups)}

    @app.template_filter("body_display_name")
    def _body_display_name(code):
        """Human-readable name for a body code.

        Single source of truth is scripts/db/names.py (the DB registry) —
        templates must not carry their own name maps (Pete 2026-09-17).
        """
        if not code:
            return ""
        from db.names import body_name
        return body_name(code)

    # ── Arizona timezone filter ──────────────────────────────────────────
    from zoneinfo import ZoneInfo
    _UTC = ZoneInfo("UTC")
    _AZ = ZoneInfo("America/Phoenix")

    @app.template_filter("az_date")
    def _format_az_date(dt, fmt="%B %d, %Y"):
        """Convert a UTC datetime to Arizona time and format it."""
        import datetime as _dt
        if dt is None:
            return ""
        # Handle strings (raw SQL with text() may return date as string)
        if isinstance(dt, str):
            # Just return the date portion of the string
            return dt[:10]
        # Handle plain date objects (no tzinfo attribute)
        if isinstance(dt, _dt.date) and not isinstance(dt, _dt.datetime):
            return dt.strftime(fmt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return dt.astimezone(_AZ).strftime(fmt)

    # ── Relationship label filter ────────────────────────────────────────
    _REL_LABELS = {
        "HAS_APPLICANT": "Applicant",
        "HAS_ATTORNEY": "Attorney",
        "HAS_STAFF": "Staff",
        "HAS_RECOMMENDATION": "Recommendation",
        "HAS_VENDOR": "Vendor",
        "HAS_CONSULTANT": "Consultant",
        "MEMBER_OF": "Member Of",
        "PRESENT_AT": "Present At",
        "VOTE_CAST": "Vote Cast",
        "CONTRACT_AWARDED": "Contract Awarded",
        "MOTION_MADE": "Motion",
        "SECONDED": "Seconded",
        "REPRESENTED": "Represented",
        "EMPLOYED_BY": "Employed By",
        "OWNS": "Owns",
        "CONCERNS": "Concerns",
        "APPLIES_FOR": "Applies For",
        "HEARING_BODY": "Hearing Body",
        "LOCATION": "Location",
    }
    @app.template_filter("rel_label")
    def _format_rel_label(rel: str) -> str:
        """Convert a relationship code (e.g. HAS_APPLICANT) to a human label."""
        return _REL_LABELS.get(rel, rel.replace("_", " ").title())

    # ── Article card image helper (Pete directive 2026-09-08) ──────────
    # Front-page/article cards: use the article's featured_image when set;
    # otherwise fall back to a topic-appropriate photo (newsletter_images
    # card_fallback_image) so cards never show the gray tag chip.
    def _article_card_image(article) -> str:
        if not article:
            return ""
        if getattr(article, "featured_image", None):
            return article.featured_image
        try:
            from newsletter_images import card_fallback_image
            return card_fallback_image(
                getattr(article, "tags", None),
                published_at=getattr(article, "published_at", None),
            )
        except Exception:
            return ""

    app.template_global("card_image")(_article_card_image)

    # ── Newsletter subscribe widget context (front page + article pages) ──
    from routes.newsletter import widget_context as _newsletter_widget_context
    app.template_global("newsletter_widget_ctx")(_newsletter_widget_context)

    # ── Initialize newsroom tables ───────────────────────────────────────
    from db.newsroom import init_newsroom_db, seed_default_tags, seed_default_users, seed_default_topics
    init_newsroom_db()
    seed_default_tags()
    seed_default_users()
    seed_default_topics()

    # ── Register blueprints ──────────────────────────────────────────────
    from routes.meetings import meetings_bp
    from routes.bodies import bodies_bp
    from routes.members import members_bp
    from routes.auth import auth_bp
    from routes.admin import admin_bp
    from routes.articles import articles_bp
    from routes.themes import themes_bp
    from routes.topics import topics_bp
    from routes.entities import entities_bp
    from routes.entity_annotation import annotation_bp
    from routes.entity_viewer import entity_viewer_bp
    from routes.kg_quality_review import kg_quality_review_bp
    from routes.kg_stage3_approval import kg_stage3_approval_bp
    from routes.podcast import podcast_bp
    app.register_blueprint(meetings_bp)
    app.register_blueprint(bodies_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(articles_bp)
    app.register_blueprint(themes_bp)
    app.register_blueprint(topics_bp)
    app.register_blueprint(entities_bp)
    app.register_blueprint(podcast_bp)
    app.register_blueprint(annotation_bp)
    app.register_blueprint(entity_viewer_bp)
    app.register_blueprint(kg_quality_review_bp)
    app.register_blueprint(kg_stage3_approval_bp)

    from routes.newsletter import newsletter_bp
    app.register_blueprint(newsletter_bp)

    # Admin and auth are only registered when admin is enabled
    if not _disable_admin:
        app.register_blueprint(auth_bp)
        app.register_blueprint(admin_bp)

    if _disable_admin:
        @app.route("/admin")
        @app.route("/admin/")
        @app.route("/admin/<path:_path>")
        @app.route("/login")
        def _admin_disabled(_path=None):
            from flask import abort
            abort(404)

    @app.route("/about")
    def about():
        return render_template("about.html")

    @app.route("/terms")
    def terms():
        return render_template("terms.html")

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html")

    return app
