#!/usr/bin/env python3
"""Auth isolation diagnostic for contact@poliscopic.com (Namecheap Private Email).

Tests the mailbox credential from .env against:
  1. IMAP 993/SSL        -> isolates account-level credential validity
  2. SMTP 465/SSL        -> implicit SSL send path (only if IMAP succeeds)
  3. SMTP 587/STARTTLS   -> current send path (only if IMAP succeeds)

Uses EMAIL_APP_PASSWORD (the documented client credential) with fallback to
EMAIL_PASSWORD (master). Never prints passwords. Prints one line per attempt:
LABEL: OK | FAIL(code). If IMAP fails, SMTP tests are skipped to avoid
escalating a possible lockout.
"""
import imaplib
import os
import smtplib
import ssl
import sys

from dotenv import load_dotenv

load_dotenv()

USER = os.environ.get("EMAIL_ADDRESS", "contact@poliscopic.com")
PWD = os.environ.get("EMAIL_APP_PASSWORD") or os.environ.get("EMAIL_PASSWORD")
HOST = "mail.privateemail.com"

if not PWD:
    print("ERROR: no password found in env")
    sys.exit(1)

ctx = ssl.create_default_context()


def smtp_test(port, use_starttls):
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(HOST, port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(HOST, port, timeout=30)
            server.starttls(context=ctx)
        server.login(USER, PWD)
        print(f"SMTP{port}: OK")
        server.quit()
    except smtplib.SMTPAuthenticationError as e:
        print(f"SMTP{port}: FAIL auth code={e.smtp_code}")
    except Exception as e:
        print(f"SMTP{port}: ERROR {type(e).__name__}: {e}")


def imap_test():
    try:
        m = imaplib.IMAP4_SSL(HOST, 993, ssl_context=ctx, timeout=30)
        m.login(USER, PWD)
        print("IMAP993: OK")
        m.logout()
        return True
    except imaplib.IMAP4.error as e:
        print(f"IMAP993: FAIL {e}")
    except Exception as e:
        print(f"IMAP993: ERROR {type(e).__name__}: {e}")
    return False


if __name__ == "__main__":
    ok = imap_test()
    if not ok:
        print("IMAP failed -> skipping SMTP tests to avoid lockout escalation")
        sys.exit(1)
    smtp_test(465, False)
    smtp_test(587, True)
