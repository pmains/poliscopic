"""Newsletter subscription service: signup, confirm, manage, unsubscribe.

Shared by the public web routes (routes/newsletter.py) and the newsletter
send step (workflows/workflow-runner.py).  Subscriber state lives ONLY in
the site database (PostgreSQL); rows are created by the web app, and the
send step reads active recipients straight from that same database.

Security model
--------------
* Stateless action URLs (confirm / manage / unsubscribe) signed with
  itsdangerous using NEWSLETTER_TOKEN_SECRET — nothing secret is stored
  in the DB, and the same env secret must be set on the web host and any
  host that renders digest footers (the send step).  Falls back to
  FLASK_SECRET_KEY for local/dev convenience.
* Double opt-in: signup creates a `pending` subscriber; nothing is emailed
  to the digest list until the confirmation link is followed.
* Rate limiting backed by newsletter_submit_log rows (per-email and
  per-IP windows); log rows pruned after 48h.
* Honeypot + time-trap live in the routes layer (they need the request).

Topic registry mirrors workflows/*.yaml names so the signup form, the
send step, and the DB stay consistent.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from sqlalchemy import select, delete, func
from sqlalchemy.orm import Session

from db.core import get_engine, get_session
from db.newsroom import (NewsletterSubscriber, NewsletterSubscription,
                         NewsletterSubmitLog)

log = logging.getLogger(__name__)

# ── Topic registry (keep in sync with workflows/*.yaml `name`) ──────────
NEWSLETTER_TOPICS = {
    "housing": "Housing & Development",
    "water-environment": "Water & Environment",
    "transportation": "Transportation & Infrastructure",
    "public-safety": "Public Safety & Justice",
    "boards-commissions": "Boards & Commissions",
    "weekly-roundup": "Weekly Roundup (all topics)",
}

VALID_TOPICS = frozenset(NEWSLETTER_TOPICS)

# Newsletter article slug stems (scripts/publish_newsletter_article.py
# TOPIC_META) map to the topic key used by the subscription service.
SLUG_STEM_TO_TOPIC = {
    "housing-development-watch": "housing",
    "water-environment-watch": "water-environment",
    "public-safety-watch": "public-safety",
    "transportation-watch": "transportation",
    "boards-commissions-watch": "boards-commissions",
    "weekly-roundup-watch": "weekly-roundup",
}


def topic_for_article_slug(slug: str) -> Optional[str]:
    """Return the newsletter topic key for an article slug, or None.

    Newsletter article slugs look like ``2026-09-08-housing-development-watch``.
    """
    if not slug:
        return None
    for stem, topic in SLUG_STEM_TO_TOPIC.items():
        if slug.endswith(stem):
            return topic
    return None

CONFIRM_MAX_AGE_DAYS = 3          # confirm links expire after 3 days
MANAGE_MAX_AGE_DAYS = 90         # footer manage links (re-sent weekly)
UNSUBSCRIBE_MAX_AGE_DAYS = 90    # footer one-click unsubscribe
MANAGE_NOW_MAX_AGE_HOURS = 1     # on-demand "email me a link" reset — 1 hour (Pete 2026-09-08)

# Per-action expiry.  The signer salt (newsletter-{action}) binds a token
# to one action; its max age comes from this map at VERIFY time, so an
# expiry can never be forgotten or widened by the minting site.
ACTION_MAX_AGE = {
    "confirm": timedelta(days=CONFIRM_MAX_AGE_DAYS),
    "manage": timedelta(days=MANAGE_MAX_AGE_DAYS),
    "manage-now": timedelta(hours=MANAGE_NOW_MAX_AGE_HOURS),
    "unsubscribe": timedelta(days=UNSUBSCRIBE_MAX_AGE_DAYS),
}

# Action → URL path.  manage-now and manage share the /newsletter/manage
# page; only the salt + expiry differ.
ACTION_PATH = {
    "confirm": "confirm",
    "manage": "manage",
    "manage-now": "manage",
    "unsubscribe": "unsubscribe",
}

LOG_RETENTION = timedelta(hours=48)


def owner_emails() -> List[str]:
    """Recipients always kept on the list even with no subscriber row."""
    raw = os.environ.get("NEWSLETTER_OWNER_EMAILS", "")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def normalize_email(email: str) -> str:
    """Lowercase + strip + collapse inner whitespace."""
    return " ".join(email.strip().lower().split())


def _secret() -> str:
    return (os.environ.get("NEWSLETTER_TOKEN_SECRET")
            or os.environ.get("FLASK_SECRET_KEY")
            or "dev-newsletter-secret-change-me")


def make_action_url(email: str, action: str, topic: Optional[str] = None) -> str:
    """Stateless signed URL for confirm/manage/unsubscribe actions.

    Signed with itsdangerous; payload carries the email (and topic for
    per-topic unsubscribe).  The same env secret must be present wherever
    links are minted AND verified.  Expiry is NOT chosen here — it is
    enforced per action in verify_action_token() via ACTION_MAX_AGE.
    """
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(_secret(), salt=f"newsletter-{action}")
    payload = {"e": normalize_email(email)}
    if topic:
        payload["t"] = topic
    token = s.dumps(payload)
    base = os.environ.get("PUBLIC_BASE_URL", "https://poliscopic.com").rstrip("/")
    path = ACTION_PATH.get(action, action)
    return f"{base}/newsletter/{path}?t={token}"


def verify_action_token(action: str, raw_token: str,
                        max_age: Optional[timedelta] = None) -> Optional[dict]:
    """Verify a stateless action token; return payload dict or None.

    max_age defaults to ACTION_MAX_AGE[action] — the per-action expiry is
    enforced here, at verification time, so stale links (including ones
    minted before a policy change) fail closed.
    """
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
    s = URLSafeTimedSerializer(_secret(), salt=f"newsletter-{action}")
    max_age = max_age or ACTION_MAX_AGE.get(action, timedelta(days=365))
    try:
        return s.loads(raw_token, max_age=int(max_age.total_seconds()))
    except (BadSignature, SignatureExpired):
        return None


# ── Status transitions ───────────────────────────────────────────────────

def create_pending_subscriber(session: Session, email: str,
                              topics: Iterable[str]) -> NewsletterSubscriber:
    """Create/reset a subscriber row for an unconfirmed signup.

    * New email           → pending row + pending topic rows.
    * Active subscriber   → requested topics activated in place (already
                            verified), subscriber stays active.
    * Pending/unsubscribed → row reset to pending with requested topics so
                            a fresh confirmation link works.
    Returns the subscriber row.
    """
    email = normalize_email(email)
    wanted = sorted({t for t in topics if t in VALID_TOPICS})
    if not wanted:
        raise ValueError("at least one valid topic required")
    now = datetime.now(timezone.utc)
    sub = get_by_email(session, email)
    if sub is None:
        sub = NewsletterSubscriber(email=email, status="pending",
                                   created_at=now, updated_at=now)
        session.add(sub)
        session.flush()
        for topic in wanted:
            session.add(NewsletterSubscription(
                subscriber_id=sub.id, topic=topic, status="pending",
                created_at=now, updated_at=now))
    elif sub.status == "active":
        _set_topic_rows(session, sub, wanted, activate=True)
        sub.updated_at = now
    else:
        # pending (resend) or unsubscribed (rejoin)
        sub.status = "pending"
        sub.confirmed_at = None
        sub.unsubscribed_at = None
        sub.updated_at = now
        _set_topic_rows(session, sub, wanted, activate=False,
                        replace=True)
    session.flush()
    return sub


def _set_topic_rows(session: Session, sub: NewsletterSubscriber,
                    wanted: List[str], activate: bool,
                    replace: bool = False) -> None:
    """Align subscription rows to `wanted`.

    activate=True  → rows become/are added as 'active' (verified email).
    activate=False → rows stay 'pending' until confirm; when replace=True
                     any existing rows outside `wanted` are deleted.
    """
    existing = {r.topic: r for r in sub.subscriptions}
    now = datetime.now(timezone.utc)
    for topic in wanted:
        row = existing.get(topic)
        if row is None:
            session.add(NewsletterSubscription(
                subscriber_id=sub.id, topic=topic,
                status="active" if activate else "pending",
                created_at=now, updated_at=now))
        elif activate and row.status != "active":
            row.status = "active"
            row.updated_at = now
        elif not activate and row.status != "pending":
            row.status = "pending"
            row.updated_at = now
    if replace:
        for topic, row in existing.items():
            if topic not in wanted:
                session.delete(row)


def confirm_subscriber_by_email(session: Session, email: str) -> Optional[NewsletterSubscriber]:
    """Activate a pending subscriber + pending topics.  None if not pending."""
    sub = get_by_email(session, email)
    if sub is None or sub.status != "pending":
        return None
    now = datetime.now(timezone.utc)
    sub.status = "active"
    sub.confirmed_at = now
    sub.unsubscribed_at = None
    sub.updated_at = now
    for row in sub.subscriptions:
        if row.status == "pending":
            row.status = "active"
            row.updated_at = now
    session.commit()
    return sub


def set_topics_by_email(session: Session, email: str,
                        topics: Iterable[str]) -> Optional[NewsletterSubscriber]:
    """Manage page: replace the active-topic set for an active subscriber."""
    sub = get_by_email(session, email)
    if sub is None or sub.status != "active":
        return None
    wanted = sorted({t for t in topics if t in VALID_TOPICS})
    existing = {r.topic: r for r in sub.subscriptions}
    now = datetime.now(timezone.utc)
    for topic in wanted:
        row = existing.get(topic)
        if row is None:
            session.add(NewsletterSubscription(
                subscriber_id=sub.id, topic=topic, status="active",
                created_at=now, updated_at=now))
        elif row.status != "active":
            row.status = "active"
            row.updated_at = now
    for topic, row in existing.items():
        if topic not in wanted and row.status == "active":
            row.status = "unsubscribed"
            row.updated_at = now
    sub.updated_at = now
    session.commit()
    return sub


def unsubscribe_by_email(session: Session, email: str,
                         topic: Optional[str] = None) -> Optional[NewsletterSubscriber]:
    """Unsubscribe one topic (or all when topic is None).  Idempotent."""
    sub = get_by_email(session, email)
    if sub is None:
        return None
    now = datetime.now(timezone.utc)
    if topic and topic in VALID_TOPICS:
        for row in sub.subscriptions:
            if row.topic == topic and row.status == "active":
                row.status = "unsubscribed"
                row.updated_at = now
    else:
        sub.status = "unsubscribed"
        sub.unsubscribed_at = now
        for row in sub.subscriptions:
            if row.status == "active":
                row.status = "unsubscribed"
                row.updated_at = now
    sub.updated_at = now
    session.commit()
    return sub


def active_topics(sub: NewsletterSubscriber) -> List[str]:
    return sorted(r.topic for r in sub.subscriptions if r.status == "active")


def get_by_email(session: Session, email: str) -> Optional[NewsletterSubscriber]:
    return session.execute(
        select(NewsletterSubscriber).where(
            NewsletterSubscriber.email == normalize_email(email))
    ).scalars().first()


# ── Send-step recipient lookup (explicit engine, no app context) ─────────

def active_recipients(topic: str, engine_url: Optional[str] = None) -> List[dict]:
    """Active subscriber emails for a topic — used by the send step.

    engine_url: explicit DB URL (e.g. PROD_DATABASE_URL).  When None, uses
    the default engine.  Owner emails are always appended (token-less rows
    may exist or not).  Never raises: on DB failure returns owners only so
    the digest still goes out to the operator.
    """
    if topic not in VALID_TOPICS:
        return []
    engine = get_engine() if engine_url is None else _engine_for(engine_url)
    out: List[dict] = []
    seen = set()
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(NewsletterSubscriber)
                .join(NewsletterSubscription,
                      NewsletterSubscription.subscriber_id == NewsletterSubscriber.id)
                .where(NewsletterSubscription.topic == topic,
                       NewsletterSubscription.status == "active",
                       NewsletterSubscriber.status == "active")
            ).scalars().all()
            for sub in rows:
                seen.add(sub.email)
                out.append({"email": sub.email, "topic": topic})
    except Exception as e:  # pragma: no cover - defensive
        log.error("active_recipients: DB unavailable (%s)", e)
    for owner in owner_emails():
        if owner not in seen and owner not in {r["email"] for r in out}:
            out.append({"email": owner, "topic": topic})
    return out


def _engine_for(url: str):
    from sqlalchemy import create_engine
    return create_engine(url, connect_args={"connect_timeout": 10}, future=True)


# ── Rate limiting (abuse audit log) ──────────────────────────────────────

def _prune_log(session: Session) -> None:
    cutoff = datetime.now(timezone.utc) - LOG_RETENTION
    session.execute(delete(NewsletterSubmitLog).where(
        NewsletterSubmitLog.created_at < cutoff))


def log_submit(session: Session, email: str, ip: Optional[str],
               kind: str = "subscribe", note: str = "") -> None:
    _prune_log(session)
    session.add(NewsletterSubmitLog(
        email=normalize_email(email), ip=ip, kind=kind, note=note,
        created_at=datetime.now(timezone.utc)))
    session.commit()


def rate_limited(session: Session, email: str, ip: Optional[str],
                 kind: str = "subscribe",
                 email_per_hour: int = 3, ip_per_hour: int = 8) -> bool:
    """True when the submit should be rejected (limits exceeded)."""
    _prune_log(session)
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    e = normalize_email(email)
    email_count = session.execute(
        select(func.count()).select_from(NewsletterSubmitLog).where(
            NewsletterSubmitLog.email == e,
            NewsletterSubmitLog.kind == kind,
            NewsletterSubmitLog.created_at >= hour_ago)
    ).scalar() or 0
    if email_count >= email_per_hour:
        return True
    if ip:
        ip_count = session.execute(
            select(func.count()).select_from(NewsletterSubmitLog).where(
                NewsletterSubmitLog.ip == ip,
                NewsletterSubmitLog.kind == kind,
                NewsletterSubmitLog.created_at >= hour_ago)
        ).scalar() or 0
        if ip_count >= ip_per_hour:
            return True
    return False
