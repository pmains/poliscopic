#!/usr/bin/env python3
"""Local-only launcher for the standing daily-sync approval review.

The review surface is deliberately **not** reachable from the main application.
This launcher builds a minimal app containing only the approval blueprint, opts
that process into the route, and binds loopback with debug and the reloader
explicitly off.

Why a separate launcher rather than a route in the main app: the main app runs
with ``debug=True`` on ``0.0.0.0``, and the Werkzeug debugger permits arbitrary
code execution.  A surface that records a standing production authorization must
never be exposed that way, and must not be reachable merely because a blueprint
exists.

Usage:
    .venv/bin/python scripts/ops/standing_sync_approval_serve.py

Then open http://127.0.0.1:5003/ops/standing-sync-approval/
(override the port with STANDING_SYNC_APPROVAL_PORT).

This records authorization only: it executes no sync and opens no database.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

APPROVAL_PATH = "/ops/standing-sync-approval/"
HOST = "127.0.0.1"
DEFAULT_PORT = 5003
DEBUG = False
USE_RELOADER = False


def create_local_app():
    """A minimal app holding only the approval blueprint."""
    from flask import Flask

    from routes.standing_sync_approval import standing_sync_approval_bp

    app = Flask(__name__, template_folder=str(REPO / "templates"))
    app.jinja_env.globals["current_user"] = type(
        "Anonymous", (), {"is_authenticated": False})()
    # A per-process key: a restart invalidates outstanding sessions, which is the
    # right default for a one-off local review and avoids a checked-in secret.
    app.secret_key = secrets.token_hex(32)
    app.register_blueprint(standing_sync_approval_bp)
    return app


def run_kwargs(argv: list[str] | None = None) -> dict:
    """The exact bind parameters: loopback only, debug off, reloader off."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("STANDING_SYNC_APPROVAL_PORT",
                                                  DEFAULT_PORT)))
    args, _unknown = parser.parse_known_args(argv)
    if HOST not in ("127.0.0.1", "::1"):
        raise RuntimeError("the approval surface may only bind loopback")
    if DEBUG or USE_RELOADER:
        raise RuntimeError("the approval surface must never run in debug mode "
                           "or with a reloader")
    return {"host": HOST, "port": args.port, "debug": DEBUG,
            "use_reloader": USE_RELOADER}


def main(argv: list[str] | None = None) -> int:
    from routes.standing_sync_approval import ENABLE_FLAG, ENABLED_VALUE

    os.environ[ENABLE_FLAG] = ENABLED_VALUE  # opt in this process only
    kwargs = run_kwargs(argv)
    app = create_local_app()
    print(f"Standing sync approval review: "
          f"http://{kwargs['host']}:{kwargs['port']}{APPROVAL_PATH}")
    print("Loopback only. Debug disabled, reloader disabled.")
    print("This records authorization only: it executes no sync and reaches no database.")
    app.run(**kwargs)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
