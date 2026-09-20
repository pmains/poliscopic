"""
Send a plain-text email from contact@poliscopic.com.

Usage:
    python scripts/send_plain_email.py <recipient> <subject> <body>

Uses the same SMTP config as email_article.py (Namecheap Private Email).
Reads EMAIL_APP_PASSWORD (app password) from .env; falls back to EMAIL_PASSWORD
(master) if the app password is not set. Per Namecheap docs, app passwords are
the only valid client credential when they differ from the master password.
"""
import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText
from email.utils import formataddr

from dotenv import load_dotenv
load_dotenv()

EMAIL_FROM = "contact@poliscopic.com"
EMAIL_FROM_NAME = "Poliscopic"
EMAIL_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD") or os.environ.get("EMAIL_PASSWORD")
if not EMAIL_PASSWORD:
    print("Error: EMAIL_APP_PASSWORD not set in .env")
    sys.exit(1)

SMTP_HOST = "mail.privateemail.com"
SMTP_PORT = 587


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    recipient = sys.argv[1]
    subject = sys.argv[2]
    body = sys.argv[3]

    msg = MIMEText(body, 'plain')
    msg['Subject'] = subject
    msg['From'] = formataddr((EMAIL_FROM_NAME, EMAIL_FROM))
    msg['To'] = recipient

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=ctx)
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, [recipient], msg.as_string())

    print(f"Email sent: '{subject}' to {recipient}")


if __name__ == '__main__':
    main()
