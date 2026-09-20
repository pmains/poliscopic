#!/usr/bin/env python3
"""URL resolution for the dev→prod sync, validated before any engine exists.

Extracted from ``scripts/db/sync_prod.py`` as a behavior-preserving split; the
code below is unchanged.  ``scripts/db/sync_prod.py`` remains the CLI facade.
"""

from __future__ import annotations

import logging
import os
import sys

# Make the shared modules importable however this module is invoked.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


import os
import re
import sys
from db.tier import DEVELOPMENT, PRODUCTION, TierError, resolve_role_url
log = logging.getLogger("sync")




# ── URL resolution ──


def _mask_url(url: str) -> str:
    """Mask the password portion of a PostgreSQL URL for logging."""
    return re.sub(r'(//[^:]+:).+?(@)', r'\1****\2', url)




def _resolve_dev_url() -> str:
    """The development role, validated before any engine is constructed."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("Set DATABASE_URL to your dev database")
        sys.exit(1)
    try:
        target = resolve_role_url(DEVELOPMENT, url, label="dev")
    except TierError as exc:
        log.error("dev role refused before connecting: %s", exc)
        sys.exit(1)
    log.info("Dev:   %s", target.redacted())
    return url




def _resolve_prod_url() -> str:
    """The production role, validated before any engine is constructed."""
    url = os.environ.get("PROD_DATABASE_URL")
    if not url:
        log.error("Set PROD_DATABASE_URL")
        sys.exit(1)
    try:
        target = resolve_role_url(PRODUCTION, url, label="prod")
    except TierError as exc:
        log.error("prod role refused before connecting: %s", exc)
        sys.exit(1)
    log.info("Prod:  %s", target.redacted())
    return url
