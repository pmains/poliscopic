#!/usr/bin/env python3
"""
Verify production code after deploy — import check + HTTP health.

Run on the production server after deploying code (not locally).
Skips if run locally (detects by hostname).

Usage:
    ssh deploy-root@example.invalid "cd /opt/poliscopic && .venv/bin/python scripts/verify_deploy.py"
"""

import logging
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("verify")


def _is_production() -> bool:
    """Heuristic: if hostname doesn't look like a DO droplet, warn."""
    import socket
    host = socket.gethostname()
    if "local" in host or "Mac" in host or "mains" in host.lower():
        log.warning("This looks like a dev machine — skipping HTTP health check")
        return False
    return True


def check_imports():
    """Verify the new code's critical imports work."""
    log.info("── Checking imports ──")
    modules = [
        ("db.models.MeetingMember", "from db.models import MeetingMember"),
        ("db.models.IngestFailure", "from db.models import IngestFailure"),
        ("db.models.MeetingSupervisor", "from db.models import MeetingSupervisor"),
    ]
    all_ok = True
    for label, stmt in modules:
        result = subprocess.run(
            [sys.executable, "-c", stmt],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log.info("  ✅ %s", label)
        else:
            log.error("  ❌ %s: %s", label, result.stderr.strip())
            all_ok = False
    return all_ok


def check_http():
    """Verify the site serves HTTP 200 on key endpoints."""
    if not _is_production():
        return True

    import urllib.request

    log.info("── Checking HTTP endpoints ──")
    endpoints = [
        "https://poliscopic.com/",
        "https://poliscopic.com/meetings",
        "https://poliscopic.com/members",
    ]
    all_ok = True
    for url in endpoints:
        try:
            resp = urllib.request.urlopen(url, timeout=15)
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
            # Confirm it returned actual content, not an error page
            has_content = len(body) > 200
            if status == 200 and has_content:
                log.info("  ✅ %s  (200, %d bytes)", url, len(body))
            else:
                log.warning("  ⚠ %s  (%d, %d bytes)", url, status, len(body))
        except Exception as e:
            log.error("  ❌ %s: %s", url, e)
            all_ok = False
    return all_ok


def check_db_connect():
    """Verify the new code can query the production database."""
    log.info("── Checking DB queries ──")
    try:
        import os
        from sqlalchemy import text, create_engine
        engine = create_engine(
            os.environ["PROD_DATABASE_URL"],
            pool_size=1, connect_args={"connect_timeout": 10}
        )
        with engine.connect() as c:
            # meeting_members exists (the new table)
            mm = c.execute(
                text("SELECT COUNT(*) FROM meeting_members")
            ).scalar()
            log.info("  meeting_members: %d rows", mm)

            # meeting_supervisors may or may not exist (cleanup may have run)
            inspector = __import__("sqlalchemy").inspect(engine)
            if "meeting_supervisors" in inspector.get_table_names():
                ms = c.execute(
                    text("SELECT COUNT(*) FROM meeting_supervisors")
                ).scalar()
                log.info("  meeting_supervisors: %d rows (old table still present)", ms)

        engine.dispose()
        return True
    except Exception as e:
        log.error("  ❌ DB check failed: %s", e)
        return False


def main():
    log.info("Starting post-deploy verification")

    imports_ok = check_imports()
    if not imports_ok:
        log.error("Import check FAILED — do NOT proceed with cleanup")
        log.error("Fix the code, re-deploy, re-verify")
        sys.exit(1)

    http_ok = check_http()
    if not http_ok:
        log.warning("Some HTTP endpoints have issues — investigate before cleanup")

    db_ok = check_db_connect()

    all_ok = imports_ok and db_ok

    print()
    if all_ok:
        log.info("✅ All checks passed — safe to proceed with cleanup")
    else:
        log.warning("⚠ Some checks had issues — review above")
        sys.exit(1)


if __name__ == "__main__":
    main()
