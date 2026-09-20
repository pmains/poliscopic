#!/usr/bin/env python3
"""Unauthenticated SMTP probe: does contact@poliscopic.com exist on the server?

Uses VRFY (if enabled) and MAIL FROM/RCPT TO without authenticating.
No credentials involved, nothing is delivered. If the server requires auth
for RCPT, that is itself informative (code 530).
"""
import smtplib
import ssl

HOST = "mail.privateemail.com"
ADDR = "contact@poliscopic.com"

ctx = ssl.create_default_context()
with smtplib.SMTP(HOST, 587, timeout=30) as s:
    s.starttls(context=ctx)
    s.ehlo()
    code_v, resp_v = s.verify(ADDR)
    print(f"VRFY {ADDR}: {code_v} {resp_v.decode(errors='replace')[:200]}")
    code_m, resp_m = s.mail("probe@example.com")
    print(f"MAIL FROM probe@example.com: {code_m} {resp_m.decode(errors='replace')[:200]}")
    code_r, resp_r = s.rcpt(ADDR)
    print(f"RCPT TO {ADDR}: {code_r} {resp_r.decode(errors='replace')[:200]}")
