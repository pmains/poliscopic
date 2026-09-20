#!/usr/bin/env python3
"""Remove the overproduced newsletter item-articles (2026-08-27 spree).

Deletes articles created on 2026-08-27 (ids 122-162) plus their
article_tags / article_sources rows, from BOTH dev and prod. Also removes
the tags created that day (Surprise, Avondale) once they are orphaned.

Usage:
    source .env
    .venv/bin/python -u scripts/remove_newsletter_articles.py
"""
import logging
import os
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from sqlalchemy import create_engine, text
from ops.production_interlock_guard import require_production_interlock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("remove_articles")

DELETE_DATE = "2026-08-27"


def cleanup(url, label):
    engine = create_engine(url, pool_size=2, connect_args={"connect_timeout": 10})
    with engine.begin() as c:
        ids = [r[0] for r in c.execute(text(
            "SELECT id FROM articles WHERE created_at::date = :d ORDER BY id"
        ), {"d": DELETE_DATE}).fetchall()]
        if not ids:
            log.info("[%s] no articles created on %s — nothing to do", label, DELETE_DATE)
            engine.dispose()
            return
        log.info("[%s] %d articles to remove (ids %d..%d)", label, len(ids), ids[0], ids[-1])

        # FK-safe order: join tables first, then articles
        c.execute(text(
            "DELETE FROM article_tags WHERE article_id = ANY(:ids)"), {"ids": ids})
        c.execute(text(
            "DELETE FROM article_sources WHERE article_id = ANY(:ids)"), {"ids": ids})
        c.execute(text(
            "DELETE FROM articles WHERE id = ANY(:ids)"), {"ids": ids})
        log.info("[%s] deleted %d articles + join rows", label, len(ids))

        # Orphaned tags created the same day
        tags = [r[0] for r in c.execute(text(
            "SELECT id FROM tags WHERE created_at::date = :d"), {"d": DELETE_DATE}).fetchall()]
        for tid in tags:
            used = c.execute(text(
                "SELECT COUNT(*) FROM article_tags WHERE tag_id = :t"), {"t": tid}).scalar()
            if used == 0:
                c.execute(text("DELETE FROM tags WHERE id = :t"), {"t": tid})
                log.info("[%s] removed orphaned tag id %s", label, tid)
    engine.dispose()


def main():
    require_production_interlock(
        "OP-REPAIR", "scripts/remove_newsletter_articles.py")
    dev_url = os.environ.get("DATABASE_URL", "")
    prod_url = os.environ.get("PROD_DATABASE_URL", "")
    if not dev_url or not prod_url:
        log.error("Set DATABASE_URL and PROD_DATABASE_URL (source .env)")
        return 1
    cleanup(dev_url, "dev")
    cleanup(prod_url, "prod")
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
