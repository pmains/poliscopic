#!/usr/bin/env python3
"""Explicitly initialize a development or test application database.

The web application intentionally performs no schema or seed writes during
startup. Production schema work must use the separately authorized OP-SCHEMA
workflow; this command refuses the production tier.
"""

from __future__ import annotations

import argparse

from db import init_db
from db.config import DB_TARGET, DB_TIER
from db.newsroom import init_newsroom_db, seed_default_tags, seed_default_topics
from db.tier import PRODUCTION


def bootstrap() -> None:
    """Create/migrate application tables and seed non-credential reference data."""
    if DB_TIER == PRODUCTION:
        raise RuntimeError(
            "bootstrap_app_db refuses production; use the authorized OP-SCHEMA workflow"
        )
    init_db()
    init_newsroom_db()
    seed_default_tags()
    seed_default_topics()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(f"Bootstrapping {DB_TIER} database: {DB_TARGET.redacted()}")
    bootstrap()
    print("Application database bootstrap complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
