"""
Database configuration — explicit, fail-closed tier selection.

TIERS
-----

  TIER          TARGET                          PURPOSE
  ────────────  ──────────────────────────────  ─────────────────────────────────
  development   PostgreSQL (poliscopic_dev)      Daily work: scraping + Flask app
  test          tempfile SQLite                  Unit/integration tests
  production    PostgreSQL (poliscopic)          Public-facing site (declared explicitly)

HOW TIER SELECTION WORKS
------------------------

The authoritative mechanism is :mod:`db.tier`.  This module is a thin, legacy
front door onto it and adds no rules of its own.

  1. ``POLISCOPIC_DB_TIER`` is read and must be a known tier.
  2. With no declared tier, an explicit ``DATABASE_URL`` derives the development
     tier; the target is then validated, not trusted.
  3. The resolved target is classified from its own host and database name and
     must agree with the tier.

Fail-closed behaviour:

  * a production-like target under the development tier is refused;
  * a production declaration without a production-like target is refused;
  * an unclassifiable target is refused;
  * conflicting duplicate definitions in ``.env`` are refused, so first-wins or
    last-wins ordering can never decide which database is used;
  * the test tier accepts only a local target, otherwise it mints a throwaway
    SQLite file and can never reach shared data.

Credentials are never printed.  Diagnostics show tier, host, port and database.

HOW TO USE
----------

Development (default — .env supplies DATABASE_URL):
    python scripts/scrape_agendas.py peoria --sync --year=2026

Test (pytest sets POLISCOPIC_DB_TIER=test automatically):
    pytest tests/

Production (the public service declares POLISCOPIC_DB_TIER=production; deployed
with sync.sh):
    POLISCOPIC_DB_TIER=production DATABASE_URL=postgresql://...@.../poliscopic

To override the database for a one-off command:
    DATABASE_URL=postgresql://user:...@localhost:5432/poliscopic_dev python ...

SQLite (data/maricopa.sqlite) is retained as a historical archive only.
All ongoing work uses PostgreSQL.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from db.tier import (
    DEVELOPMENT,
    TierError,
    resolve_database_url,
)

load_dotenv()  # Load .env — supplies DATABASE_URL

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Resolution ────────────────────────────────────────────────────────
# All rules live in db.tier.resolve_database_url.  This module only adapts the
# result to the historical module-level names the rest of the codebase imports.
try:
    DATABASE_URL, DB_TIER, DB_TARGET = resolve_database_url(
        dotenv_path=_PROJECT_ROOT / ".env"
    )
    DATABASE_TIER = DB_TIER
except TierError as exc:
    raise RuntimeError(f"database tier selection refused to continue: {exc}") from exc

# ── Redacted diagnostic ───────────────────────────────────────────────
# Never prints credentials: the message is built from parsed parts only.
if DB_TARGET.dialect == "sqlite":
    print(f"  [config] Using SQLite: {DB_TARGET.database} (tier={DB_TIER})")
else:
    location = DB_TARGET.host or "(local)"
    if DB_TARGET.port is not None:
        location = f"{location}:{DB_TARGET.port}"
    print(
        f"  [config] Using PostgreSQL: {location}/{DB_TARGET.database} "
        f"(tier={DB_TIER})"
    )
