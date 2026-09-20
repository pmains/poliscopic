"""Transactional newsletter emails (confirm, manage, rejoin) from the web app.

Uses the same Namecheap Private Email SMTP config as the digest send step
(scripts/send_plain_email.py, workflows/workflow-runner.py): env supplies
EMAIL_APP_PASSWORD (or EMAIL_PASSWORD).  Never blocks the request hard:
exceptions are logged and re-raised as NewsletterMailError so routes can
decide how to degrade (signup row already saved → resend later works).
"""

import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger(__name__)

EMAIL_FROM = "contact@poliscopic.com"
EMAIL_FROM_NAME = "Poliscopic"
SMTP_HOST = "mail.privateemail.com"
SMTP_PORT = 587


class NewsletterMailError(Exception):
    """Raised when a transactional newsletter email cannot be sent."""


def _password() -> str:
    pw = os.environ.get("EMAIL_APP_PASSWORD") or os.environ.get("EMAIL_PASSWORD")
    if not pw:
        raise NewsletterMailError("EMAIL_APP_PASSWORD not set")
    return pw


def _enabled() -> bool:
    """Skip real SMTP when explicitly disabled (tests / local dev)."""
    return os.environ.get("NEWSLETTER_MAIL_DISABLED", "").lower() not in (
        "1", "true", "yes")


def send_html(to_email: str, subject: str, html_body: str) -> None:
    """Send one transactional HTML email from contact@poliscopic.com."""
    if not _enabled():
        log.info("[mail-disabled] would send '%s' to %s", subject, to_email)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((EMAIL_FROM_NAME, EMAIL_FROM))
    msg["To"] = to_email
    msg.attach(MIMEText("View this email in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls(context=ctx)
            server.login(EMAIL_FROM, _password())
            server.sendmail(EMAIL_FROM, [to_email], msg.as_string())
    except NewsletterMailError:
        raise
    except Exception as e:  # pragma: no cover - network/SMTP failures
        log.error("newsletter mail send failed: %s", e)
        raise NewsletterMailError(f"SMTP send failed: {e}") from e
    log.info("newsletter mail sent: '%s' -> %s", subject, to_email)
