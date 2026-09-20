"""Public newsletter signup / manage routes.

Security controls (OWASP-oriented):
* CSRF: session-bound token on every POST form, compared with
  hmac.compare_digest (Flask-WTF is globally disabled in this app).
* Anti-bot: honeypot field (auto-filled by naive bots → silent fake
  success), time-trap (submit < 2.5s after page render → rejected),
  and DB-backed per-email / per-IP rate limits.
* Double opt-in: signup only creates a *pending* row + sends a confirm
  email; active recipients are never touched until the link is followed.
* Input validation: email normalized + length-capped, topics allowlisted.
* Output encoding: Jinja autoescape everywhere (no |safe on user data).
* Error pages never reveal whether an email is subscribed (no
  enumeration); rate-limit page is generic.
"""

import hmac
import logging
import secrets
import time
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, redirect,
                   url_for, abort, session)

from db.core import get_session
from newsletter_svc import (NEWSLETTER_TOPICS, VALID_TOPICS, normalize_email,
                            create_pending_subscriber, confirm_subscriber_by_email,
                            set_topics_by_email, unsubscribe_by_email,
                            get_by_email, active_topics,
                            rate_limited, log_submit,
                            make_action_url)
from newsletter_mail import send_html, NewsletterMailError

log = logging.getLogger(__name__)

newsletter_bp = Blueprint("newsletter", __name__, url_prefix="/newsletter")

EMAIL_MAX = 320
MIN_FORM_SECONDS = 2.5          # time-trap floor
HONEYPOT_FIELD = "company_website"   # humans never see/fill this


# ── CSRF helpers ─────────────────────────────────────────────────────────

def _csrf_token() -> str:
    if "_newsletter_csrf" not in session:
        session["_newsletter_csrf"] = secrets.token_urlsafe(32)
    return session["_newsletter_csrf"]


def widget_context():
    """Template-global context for the inline subscribe widget.

    Called while rendering ANY page that embeds the widget (front page,
    newsletter article pages).  Stamps the form-render time so the time-trap
    in the subscribe route starts from when the visitor actually saw the
    page, and exposes the session CSRF token + honeypot field name.
    """
    session["_newsletter_form_at"] = time.time()
    return {
        "csrf_token": _csrf_token(),
        "honeypot": HONEYPOT_FIELD,
        "topics": NEWSLETTER_TOPICS,
    }


def _csrf_ok(form_token) -> bool:
    expected = session.get("_newsletter_csrf", "")
    return bool(expected) and isinstance(form_token, str) and \
        hmac.compare_digest(expected, form_token)


def _form_started_recently() -> bool:
    """Time-trap: form must have been rendered >= MIN_FORM_SECONDS ago."""
    started = session.get("_newsletter_form_at", 0.0)
    return (time.time() - started) >= MIN_FORM_SECONDS


# ── HTML email bodies (branded shell, matching digest + site styling) ──

# Brand palette mirrors static/navy-orange.css + workflows/templates/newsletter.html
_BRAND_NAVY = "#05235B"
_BRAND_ORANGE = "#ED6800"
_BRAND_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
_LOGO_URL = "https://poliscopic.com/static/poliscopic-logo.png"


def _branded_email_html(title: str, body_html: str) -> str:
    """Wrap a transactional email body in the Poliscopic branded shell.

    Header + typography + footer mirror the weekly digest template
    (workflows/templates/newsletter.html) so transactional mail looks like
    it came from the same publication.  All styling is inline — email-safe.
    """
    return f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
</head>
<body style="font-family:{_BRAND_FONT}; background:#f4f4f4; margin:0; padding:1em;">
<div style="max-width:600px; margin:0 auto; background:#ffffff; border-radius:8px; overflow:hidden;">
  <!-- HEADER -->
  <div style="background:{_BRAND_NAVY}; padding:1.4em 1.5em; text-align:center;">
    <img src="{_LOGO_URL}" alt="Poliscopic"
         style="max-width:180px; max-height:56px; width:auto; display:inline-block;">
  </div>
  <!-- BODY -->
  <div style="padding:1.6em 1.5em; color:#333;">
    <h1 style="color:{_BRAND_NAVY}; font-size:1.25em; margin:0 0 1em 0;">{title}</h1>
    {body_html}
  </div>
  <!-- FOOTER -->
  <div style="border-top:3px solid {_BRAND_ORANGE}; background:#fafafa; padding:1em 1.5em;
              text-align:center; color:#888; font-size:0.78em;">
    <p style="margin:0;"><strong style="color:#666;">Poliscopic</strong> — Government Intelligence Platform</p>
    <p style="margin:0.4em 0 0;">Public meetings, agendas, and decisions across Maricopa County<br>
       <a href="https://poliscopic.com" style="color:{_BRAND_NAVY};">poliscopic.com</a></p>
  </div>
</div>
</body>
</html>"""


def _confirm_email_html(confirm_url: str, topics: list) -> str:
    topic_lines = "".join(
        f"<li>{NEWSLETTER_TOPICS.get(t, t)}</li>" for t in topics)
    body = f"""\
  <p style="margin:0 0 1em 0;">You asked to receive:</p>
  <ul style="color:#444; margin:0 0 1.2em 1.2em; padding:0;">{topic_lines}</ul>
  <p style="margin:0 0 1.4em 0;">If that was you, click below to confirm. If you didn't
     request this, you can ignore this email — nothing will be sent.</p>
  <p style="margin:0 0 1.6em 0;">
    <a href="{confirm_url}"
       style="background:{_BRAND_NAVY}; color:#ffffff; padding:12px 22px; border-radius:6px;
              text-decoration:none; display:inline-block; font-weight:600;">Confirm subscription</a>
  </p>
  <p style="color:#888; font-size:0.82em; margin:0;">This link expires in 3 days.
     You're receiving this because someone submitted this address on poliscopic.com.</p>"""
    return _branded_email_html("Confirm your subscription", body)


def _manage_email_html(manage_url: str) -> str:
    body = f"""\
  <p style="margin:0 0 1.4em 0;">Use this link to change which digests you receive
     or to unsubscribe:</p>
  <p style="margin:0 0 1.6em 0;">
    <a href="{manage_url}"
       style="background:{_BRAND_NAVY}; color:#ffffff; padding:12px 22px; border-radius:6px;
              text-decoration:none; display:inline-block; font-weight:600;">Manage subscriptions</a>
  </p>
  <p style="color:#888; font-size:0.82em; margin:0;">This link expires in 1 hour. If you
     didn't request this, ignore the email — nothing will change.</p>"""
    return _branded_email_html("Manage your Poliscopic newsletters", body)


def _plain_confirm_text(confirm_url: str) -> str:
    return (f"Confirm your Poliscopic newsletter subscription:\n{confirm_url}\n\n"
            "If you didn't request this, ignore this email.")


def _plain_manage_text(manage_url: str) -> str:
    return (f"Manage your Poliscopic newsletters:\n{manage_url}\n\n"
            "If you didn't request this, ignore this email.")


# ── Client IP ────────────────────────────────────────────────────────────

def _client_ip() -> str:
    # App sits behind nginx on prod; never trust X-Forwarded-For from the
    # client — nginx sets it.  Cap length to keep the log table tidy.
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "")[:64]


# ── Landing page (subscribe + manage-link forms) ─────────────────────────

@newsletter_bp.route("")
@newsletter_bp.route("/")
def landing():
    session["_newsletter_form_at"] = time.time()
    return render_template("newsletter/landing.html",
                           topics=NEWSLETTER_TOPICS,
                           csrf_token=_csrf_token(),
                           honeypot=HONEYPOT_FIELD)


# ── Subscribe ────────────────────────────────────────────────────────────

@newsletter_bp.route("/subscribe", methods=["POST"])
def subscribe():
    # Honeypot: bots fill the hidden field — answer success, do nothing.
    if request.form.get(HONEYPOT_FIELD):
        _hs = get_session()
        log_submit(_hs, "honeypot-hit@poliscopic.invalid", _client_ip(),
                   kind="subscribe", note="honeypot-hit")
        _hs.close()
        return _success_page()
    if not _csrf_ok(request.form.get("csrf_token")):
        abort(400)
    if not _form_started_recently():
        # Bot submitted instantly; pretend success without creating rows.
        return _success_page()

    email = normalize_email(request.form.get("email", ""))
    picked = request.form.getlist("topics")
    if "All" in picked:
        # The widget's "(All)" master expands to every digest server-side,
        # so a no-JS submit still subscribes to all six.
        topics = set(VALID_TOPICS)
    else:
        topics = {t for t in picked if t in VALID_TOPICS}

    if not email or len(email) > EMAIL_MAX or "@" not in email or "." not in email.split("@")[-1]:
        return render_template("newsletter/landing.html",
                               topics=NEWSLETTER_TOPICS,
                               csrf_token=_csrf_token(),
                               honeypot=HONEYPOT_FIELD,
                               error="Please enter a valid email address.",
                               prev_email=email[:EMAIL_MAX])
    if not topics:
        return render_template("newsletter/landing.html",
                               topics=NEWSLETTER_TOPICS,
                               csrf_token=_csrf_token(),
                               honeypot=HONEYPOT_FIELD,
                               error="Choose at least one newsletter.",
                               prev_email=email[:EMAIL_MAX])

    s = get_session()
    if rate_limited(s, email, _client_ip(), kind="subscribe"):
        s.close()
        return _success_page()   # silent: don't reveal the limit
    log_submit(s, email, _client_ip(), kind="subscribe", note="ok")

    sub = create_pending_subscriber(s, email, topics)
    already_active = sub.status == "active"
    s.commit()
    s.close()

    if already_active:
        # Email already verified — topics updated live; tell them.
        return render_template("newsletter/success.html",
                               already_active=True, email=email)

    # Double opt-in email
    confirm_url = make_action_url(email, "confirm")
    try:
        send_html(email,
                  "Confirm your Poliscopic newsletter subscription",
                  _confirm_email_html(confirm_url, sorted(topics)))
    except NewsletterMailError as e:
        log.error("confirm email failed for %s: %s", email, e)
        # Row stays pending; they can resubmit (rate window allows) or the
        # admin can resend.  Still show the neutral success page.
    return _success_page(email=email)


# ── Confirm (double opt-in) ──────────────────────────────────────────────

@newsletter_bp.route("/confirm")
def confirm():
    payload = _verify("confirm")
    if not payload:
        return render_template("newsletter/confirm.html", ok=False)
    s = get_session()
    sub = confirm_subscriber_by_email(s, payload["e"])
    topics = active_topics(sub) if sub else []
    s.close()
    return render_template("newsletter/confirm.html", ok=sub is not None,
                           email=payload["e"], topics=topics)


# ── Manage (change topics / full unsubscribe) ────────────────────────────
# Accepts BOTH link classes that resolve to this page:
#   manage     — footer links inside every digest (90-day expiry)
#   manage-now — on-demand "email me a link" reset (24-hour expiry)
_MANAGE_ACTIONS = ("manage", "manage-now")


@newsletter_bp.route("/manage")
def manage():
    token = request.args.get("t", "")
    payload = None
    for action in _MANAGE_ACTIONS:
        payload = _verify(action, token)
        if payload:
            break
    if not payload:
        return render_template("newsletter/manage.html", ok=False)
    s = get_session()
    sub = get_by_email(s, payload["e"])
    if sub is None or sub.status != "active":
        s.close()
        return render_template("newsletter/manage.html", ok=False)
    topics = active_topics(sub)
    s.close()
    return render_template("newsletter/manage.html", ok=True,
                           email=sub.email, topics=topics,
                           all_topics=NEWSLETTER_TOPICS,
                           manage_token=token,
                           csrf_token=_csrf_token())


@newsletter_bp.route("/manage", methods=["POST"])
def manage_post():
    if not _csrf_ok(request.form.get("csrf_token")):
        abort(400)
    # The POST must be authorized by the SAME signed capability that
    # authenticated the page — never by a bare email in a hidden field.
    token = request.form.get("manage_token", "")
    payload = None
    for action in _MANAGE_ACTIONS:
        payload = _verify(action, token)
        if payload:
            break
    if not payload:
        abort(400)
    email = normalize_email(payload["e"])
    action = request.form.get("action", "")

    if action == "unsubscribe_all":
        s = get_session()
        sub = unsubscribe_by_email(s, email)
        s.close()
        if sub is None:
            return render_template("newsletter/manage.html", ok=False)
        return render_template("newsletter/unsubscribed.html", email=email,
                               all_topics=NEWSLETTER_TOPICS)

    topics = {t for t in request.form.getlist("topics") if t in VALID_TOPICS}
    if not topics:
        return render_template("newsletter/manage.html", ok=True, email=email,
                               topics=[], all_topics=NEWSLETTER_TOPICS,
                               manage_token=token,
                               csrf_token=_csrf_token(),
                               error="Choose at least one newsletter.")
    s = get_session()
    sub = set_topics_by_email(s, email, topics)
    s.close()
    if sub is None:
        return render_template("newsletter/manage.html", ok=False)
    return render_template("newsletter/manage.html", ok=True, email=email,
                           topics=sorted(topics), all_topics=NEWSLETTER_TOPICS,
                           manage_token=token,
                           csrf_token=_csrf_token(), saved=True)


# ── One-click unsubscribe (from digest footer) ───────────────────────────

@newsletter_bp.route("/unsubscribe")
def unsubscribe():
    payload = _verify("unsubscribe")
    if not payload:
        return render_template("newsletter/unsubscribed.html", ok=False)
    email = payload["e"]
    topic = payload.get("t")
    s = get_session()
    sub = unsubscribe_by_email(s, email, topic=topic)
    if sub is None:
        s.close()
        return render_template("newsletter/unsubscribed.html", ok=False)
    remaining = active_topics(sub)   # before the session closes
    s.close()
    return render_template("newsletter/unsubscribed.html", ok=True,
                           email=email, topic=topic,
                           remaining=remaining,
                           all_topics=NEWSLETTER_TOPICS)


# ── Send a manage link (from the landing page) ───────────────────────────

@newsletter_bp.route("/send-manage-link", methods=["POST"])
def send_manage_link():
    if not _csrf_ok(request.form.get("csrf_token")):
        abort(400)
    if not _form_started_recently():
        return _success_page()
    email = normalize_email(request.form.get("email", ""))
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return render_template("newsletter/landing.html",
                               topics=NEWSLETTER_TOPICS,
                               csrf_token=_csrf_token(),
                               honeypot=HONEYPOT_FIELD,
                               manage_error="Enter a valid email address.")
    s = get_session()
    if rate_limited(s, email, _client_ip(), kind="manage-link",
                    email_per_hour=2, ip_per_hour=6):
        s.close()
        return _success_page()
    log_submit(s, email, _client_ip(), kind="manage-link", note="ok")
    sub = get_by_email(s, email)
    s.close()
    # Always show the same neutral result whether or not the address is
    # subscribed — prevents address enumeration.
    if sub is not None and sub.status == "active":
        # manage-now = short-lived (24h) reset link, distinct from the
        # 90-day footer manage links inside digests.
        manage_url = make_action_url(email, "manage-now")
        try:
            send_html(email, "Manage your Poliscopic newsletters",
                      _manage_email_html(manage_url))
        except NewsletterMailError as e:
            log.error("manage email failed for %s: %s", email, e)
    return _success_page(email=email)


def _verify(action: str, raw_token: str = ""):
    """Verify a stateless action token; returns payload or None.

    Expiry is enforced per action inside verify_action_token() (ACTION_MAX_AGE)
    — callers never pass max_age, so stale links fail closed.
    """
    from newsletter_svc import verify_action_token
    if not raw_token:
        raw_token = request.args.get("t", "")
    return verify_action_token(action, raw_token)


def _success_page(email: str = ""):
    return render_template("newsletter/success.html", email=email)
