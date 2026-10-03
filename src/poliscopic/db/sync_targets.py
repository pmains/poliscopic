"""Validate dev and production sync URLs before constructing an engine."""

from __future__ import annotations

import logging
import os
import re

from .tier import DEVELOPMENT, PRODUCTION, TierError, resolve_role_url

log = logging.getLogger("sync")


def _mask_url(url: str) -> str:
    """Mask the password portion of a PostgreSQL URL for logging."""
    return re.sub(r"(//[^:]+:).+?(@)", r"\1****\2", url)


def _resolve_dev_url() -> str:
    """Resolve the development role before any engine is constructed."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("Set DATABASE_URL to your dev database")
        raise SystemExit(1)
    try:
        target = resolve_role_url(DEVELOPMENT, url, label="dev")
    except TierError as exc:
        log.error("dev role refused before connecting: %s", exc)
        raise SystemExit(1) from exc
    log.info("Dev:   %s", target.redacted())
    return url


def _resolve_prod_url() -> str:
    """Resolve the production role before any engine is constructed."""
    url = os.environ.get("PROD_DATABASE_URL")
    if not url:
        log.error("Set PROD_DATABASE_URL")
        raise SystemExit(1)
    try:
        target = resolve_role_url(PRODUCTION, url, label="prod")
    except TierError as exc:
        log.error("prod role refused before connecting: %s", exc)
        raise SystemExit(1) from exc
    log.info("Prod:  %s", target.redacted())
    return url
