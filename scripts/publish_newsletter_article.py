#!/usr/bin/env python3
"""Publish ONE article per newsletter run (1 newsletter = 1 article).

Reads a run's verified output (verify-result.json + enriched topic file)
and creates a single published Article: the overview as the lede, item
summaries grouped by meeting, topic + jurisdiction tags, and per-item
ArticleSource rows.

Idempotent by slug — re-running the same run is a no-op. If the run has
no approved items, nothing is published (no padding).

Usage:
    source .env
    .venv/bin/python -u scripts/publish_newsletter_article.py <topic> [--run-id <id>]
    # topic: housing | water-environment | public-safety | transportation | boards-commissions
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from slugify import slugify  # noqa: E402
from sqlalchemy import select  # noqa: E402

from db.core import get_engine  # noqa: E402
from db.models import Base  # noqa: E402
from db.names import body_jurisdictions, body_names, humanize_code  # noqa: E402
from db.newsroom import Article, ArticleSource, Tag  # noqa: E402

from newsletter_images import pick_featured_image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "data" / "runs"

# topic -> (report filename, slug stem, default topic tags)
TOPIC_META = {
    "housing": ("housing", "housing-development-watch", ["Housing", "Development", "Zoning"]),
    "water-environment": ("water", "water-environment-watch", ["Water", "Environment", "Infrastructure"]),
    "public-safety": ("public-safety", "public-safety-watch", ["Public Safety"]),
    "transportation": ("transportation", "transportation-watch", ["Transportation"]),
    "boards-commissions": ("boards-commissions", "boards-commissions-watch", ["Government"]),
    "weekly-roundup": ("weekly-roundup", "weekly-roundup-watch", ["Government", "Weekly Roundup"]),
}

# Body and jurisdiction display names come from the DB registry via
# scripts/db/names.py — this module deliberately keeps no name map of its
# own (Pete 2026-09-17: one source of truth, DRY).

DL_RE = re.compile(r"/meetings/([^/]+)/([^#]+)#item-(.+)")
# Machine item tokens ((bos item 44)) + markdown link syntax stripped from
# plain-text fields (article summary column) — never reader-facing.
_TOKEN_RE = re.compile(
    r"\(\s*(?:[A-Za-z][A-Za-z0-9-]*\s+)?item[s]?\s+"
    r"\d[0-9A-Za-z.\-]*(?:\s+and\s+\d[0-9A-Za-z.\-]*)*\s*\)"
)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def to_plain_text(text: str) -> str:
    """Strip markdown links ([label](url) → label) and stray machine item
    tokens so plain-text columns (Article.summary) stay clean."""
    if not text:
        return text
    out = _MD_LINK_RE.sub(r"\1", text)
    out = _TOKEN_RE.sub("", out)
    out = re.sub(r" {2,}", " ", out)
    out = re.sub(r"\s+([.,;:])", r"\1", out)
    return out


_SUMMARY_LIMIT = 600


def sentence_summary(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    """Plain-text summary that ends at a sentence boundary.

    A hard character cut left ledes stopping mid-sentence (Pete 2026-09-17:
    "it should end at a period").  Prefer the last complete sentence inside
    the limit; if there is none, fall back to the first sentence, then to a
    word-boundary cut.
    """
    t = to_plain_text(text or "").strip()
    if not t or len(t) <= limit:
        return t
    window = t[:limit]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut != -1:
        return window[: cut + 1].strip()
    m = re.search(r"[.!?](?:\s|$)", t)
    if m:
        return t[: m.end()].strip()
    return window.rstrip()


def find_run_dir(topic, run_id=None):
    topic_dir = RUNS_DIR / topic
    if run_id:
        d = topic_dir / run_id
        return d if d.exists() else None
    candidates = sorted(
        (p for p in topic_dir.glob("2026-*--*") if p.is_dir()),
        key=lambda p: p.name, reverse=True,
    )
    for d in candidates:
        if (d / "verified" / "verify-result.json").exists():
            return d
    return None


def meeting_context(enriched_items):
    ctx = {}
    for it in enriched_items:
        ctx[(it.get("body"), str(it.get("meeting_id")), str(it.get("agenda_item_number")))] = {
            "meeting_date": it.get("meeting_date", ""),
            "meeting_title": it.get("meeting_title", ""),
        }
    return ctx


def load_run(topic):
    run_dir = find_run_dir(topic)
    if not run_dir:
        print(f"No verified run found for {topic}")
        return None
    verified = run_dir / "verified"
    v = json.load(open(verified / "verify-result.json"))
    # Discover the enriched topic report file (name varies per topic:
    # housing.json, water.json, public_safety.json, ...)
    report_path = None
    skip = {"verify-result.json", "manifest.json", "gaps.json",
            "summarize-result.json", "verify-llm-response.json",
            "summarize-llm-response.json", "send-result.json"}
    for cand in sorted(verified.glob("*.json")):
        if cand.name in skip:
            continue
        # Only a real topic report (object with a list of items) counts.
        # Stray files an LLM may have produced (e.g. type.json) must not be
        # mistaken for the report.
        try:
            cand_data = json.load(open(cand))
        except (json.JSONDecodeError, IOError):
            continue
        if isinstance(cand_data, dict) and isinstance(cand_data.get("items"), list):
            report_path = cand
            break
    if report_path is None:
        print(f"No topic report file found in {verified}")
        return None
    enriched = json.load(open(report_path))["items"]
    ctx = meeting_context(enriched)

    items = []
    for it in v.get("items", []):
        dl = it.get("poliscopic_deep_link", "")
        m = DL_RE.search(dl)
        if not m:
            continue
        body, meeting_id, item_num = m.group(1), m.group(2), m.group(3)
        c = ctx.get((body, str(meeting_id), str(item_num)), {})
        items.append({
            "body": body,
            "meeting_id": meeting_id,
            "agenda_item_number": it.get("agenda_item_number"),
            "agenda_item_title": it.get("agenda_item_title", ""),
            "summary": (it.get("revised_summary") or it.get("summary") or "").strip(),
            "source_url": it.get("source_url", ""),
            "meeting_date": c.get("meeting_date", ""),
            "meeting_title": c.get("meeting_title", ""),
        })
    return {"run_dir": run_dir, "verified": v, "items": items}


def fmt_date(iso):
    if not iso:
        return ""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        return ""


def build_body(overview, items):
    """Overview lede + items grouped by body/meeting."""
    parts = [overview.strip(), ""]
    # Display names come from the DB registry (scripts/db/names.py).  The
    # old `BODY_NAMES.get(body, body.upper())` printed raw slugs such as
    # "APACHE-JUNCTION-CC" for anything outside its 13 hand-kept entries
    # (Pete 2026-09-17).  One bulk lookup, no hardcoded map.
    display = body_names([it["body"] for it in items])
    # Group by (body, meeting_id) preserving first-seen order
    groups = []
    seen = set()
    for it in items:
        key = (it["body"], it["meeting_id"])
        if key not in seen:
            seen.add(key)
            groups.append((key, []))
        for gkey, lst in groups:
            if gkey == key:
                lst.append(it)
                break

    for (body, meeting_id), lst in groups:
        name = display.get(body) or humanize_code(body)
        date_str = fmt_date(lst[0]["meeting_date"])
        heading = f"{name} — {date_str}" if date_str else name
        parts.append(f"## {heading}")
        parts.append("")
        for it in lst:
            link = f"/meetings/{body}/{meeting_id}#item-{it['agenda_item_number']}"
            title = it["agenda_item_title"].strip() or f"Item {it['agenda_item_number']}"
            bullet = f"- **{title}:** {it['summary']} [View item]({link})"
            parts.append(bullet)
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def tags_for(topic, items):
    _, _, topic_tags = TOPIC_META[topic]
    tags = list(topic_tags)
    jurs = body_jurisdictions([it["body"] for it in items])
    for it in items:
        jur = jurs.get(it["body"])
        if jur and jur not in tags:
            tags.append(jur)
    return tags


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("topic", choices=TOPIC_META.keys())
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    data = load_run(args.topic)
    if not data:
        return 1
    v, items = data["verified"], data["items"]

    if not v.get("approved", v.get("status") == "approved"):
        print(f"{args.topic}: run not approved — nothing published")
        return 0
    if not items:
        print(f"{args.topic}: no approved items — nothing published (no padding)")
        return 0

    subject = v.get("subject", "")
    overview = v.get("_summary") or (v.get("overview") or {}).get("text", "") or ""

    # Top story text for featured-image selection (newsletter_images.py).
    # Prefer the verify overview's top_item; fall back to the first item.
    top_text = ""
    top_item = (v.get("overview") or {}).get("top_item") or {}
    if top_item.get("body") and top_item.get("agenda_item_number"):
        for it in items:
            if (it["body"] == top_item.get("body")
                    and str(it["agenda_item_number"]) == str(top_item.get("agenda_item_number"))):
                top_text = f"{it['agenda_item_title']} {it['summary']}"
                break
    if not top_text and items:
        top_text = f"{items[0]['agenda_item_title']} {items[0]['summary']}"
    _, slug_stem, _ = TOPIC_META[args.topic]
    run_date = data["run_dir"].name.split("--")[0]  # YYYY-MM-DD
    # City-first image selection (Pete 2026-09-16): when the top story's
    # jurisdiction is knowable, prefer a photo from that city over the bare
    # topic pool.
    top_city = ""
    for cand in (top_item, items[0] if items else {}):
        body_id = (cand or {}).get("body", "")
        if body_id:
            # Brief 032 regression fix: JURISDICTION was removed by the
            # db.names DRY refactor; use the canonical accessor instead.
            top_city = body_jurisdictions([body_id]).get(body_id, "")
            if top_city:
                break
    featured_image = pick_featured_image(args.topic, top_text, run_date=run_date,
                                        city=top_city or None)
    slug = f"{run_date}-{slug_stem}"

    engine = get_engine()
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session
    session = Session(engine)

    existing = session.execute(select(Article).where(Article.slug == slug)).scalar_one_or_none()
    if existing:
        print(f"{args.topic}: article already exists (slug {slug}) — no-op")
        session.close()
        return 0

    tag_cache = {}

    def get_tag(name):
        if name in tag_cache:
            return tag_cache[name]
        tag = session.execute(select(Tag).where(Tag.name == name)).scalar_one_or_none()
        if tag is None:
            tag = Tag(name=name, slug=slugify(name), description="")
            session.add(tag)
            session.flush()
        tag_cache[name] = tag
        return tag

    body = build_body(overview, items)
    print(f"{args.topic}: featured image -> {featured_image or '(none)'}")
    article = Article(
        title=subject or f"{args.topic.replace('-', ' ').title()} Watch",
        slug=slug,
        summary=sentence_summary(overview or subject or ""),
        body=body,
        status="published",
        featured_image=featured_image,
        image_credit=None,
        is_featured=False,
        priority=0,
        published_at=datetime.now(timezone.utc),
    )
    for tag_name in tags_for(args.topic, items):
        article.tags.append(get_tag(tag_name))
    for it in items:
        if it["source_url"]:
            article.sources.append(ArticleSource(
                body=it["body"],
                meeting_id=it["meeting_id"],
                agenda_item_number=str(it["agenda_item_number"]),
                source_url=it["source_url"],
                source_type="agenda",
                item_title=it["agenda_item_title"][:500],
            ))
    session.add(article)
    session.commit()
    print(f"{args.topic}: published {slug} — {len(items)} items, "
          f"{len(article.tags)} tags, {len(article.sources)} sources")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
