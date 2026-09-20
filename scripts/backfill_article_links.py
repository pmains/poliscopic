#!/usr/bin/env python3
"""Backfill previously published newsletter articles so lede copy has real
inline links to poliscopic items instead of old machine tokens like
"(agenda item 2)", "(bos item 44)", "(item 90)".

Same editorial standard as new newsletters (Pete directive 2026-09-08):
- anchor = a NATURAL phrase that names the item (LLM picks it),
- URL copied verbatim from the article's OWN article_sources deep link,
- every URL in the result must exactly equal one of that article's item
  deep links (hard audit),
- no "(... item ...)" machine tokens may remain,
- paragraph/sentence structure and all factual wording otherwise preserved.

Safety:
- Works on ONE article at a time (--slug) or all watch articles with tokens.
- --dry-run prints the proposed lede diff and writes nothing.
- Always writes a backup JSON (slug -> old body) before updating.
- Updates only the dev DB. Prod push is a separate, explicit editorial_sync.

Usage:
    source .env
    .venv/bin/python -u scripts/backfill_article_links.py --slug 2026-09-07-boards-commissions-watch --dry-run
    .venv/bin/python -u scripts/backfill_article_links.py --slug 2026-09-07-boards-commissions-watch
    .venv/bin/python -u scripts/backfill_article_links.py --all
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sqlalchemy import select  # noqa: E402

from db.core import get_engine, get_session  # noqa: E402
from db.newsroom import Article, ArticleSource  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "data" / "archive"
WATCH_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})-(housing-development|water-environment|public-safety|transportation|"
    r"boards-commissions|weekly-roundup)-watch$"
)

# Machine item token, e.g. (agenda item 2), (bos item 44), (item 90),
# (phoenix-cc items 102 and 103), (items 7 and 8), (item 4-e), (BOS item 29).
TOKEN_RE = re.compile(
    r"\(\s*(?:[A-Za-z][A-Za-z0-9-]*\s+)?item[s]?\s+"
    r"\d[0-9A-Za-z.\-]*(?:\s+and\s+\d[0-9A-Za-z.\-]*)*\s*\)"
)
# Allow "BOS item 29" style with uppercase acronym; TOKEN_RE covers [A-Za-z] prefix.

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
POLI_LINK_RE = re.compile(r"^https://poliscopic\.com/meetings/[^/]+/[^#]+#item-.+$")


def _load_env():
    env_path = ROOT / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _call_llm(prompt: str, retries: int = 2) -> str:
    """Call DeepSeek (same provider/pattern as workflows/step-executor.py)."""
    _load_env()
    try:
        import requests
    except ImportError:
        raise RuntimeError("requests required")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("REPORTS_LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set in .env")
    model = os.environ.get("REPORTS_LLM_MODEL", "deepseek-chat")
    base_url = os.environ.get("REPORTS_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                },
                timeout=600,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content:
                return content
        except Exception as exc:  # transient network/API errors
            last_err = exc
    raise RuntimeError(f"LLM call failed after {retries + 1} attempts: {last_err}")


def _parse_json(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
        if m:
            return json.loads(m.group(1))
        raise


def strip_md_links(text: str) -> str:
    return MD_LINK_RE.sub(r"\1", text)


def build_prompt(lede: str, items: list[dict]) -> str:
    item_lines = []
    for it in items:
        item_lines.append(
            f"- agenda item {it['agenda_item_number']} | body={it['body']} | "
            f"meeting={it['meeting_id']} | title: {it['agenda_item_title'][:220]} | "
            f"deep link: {it['deep_link']}"
        )
    items_block = "\n".join(item_lines)
    return f"""# Task — relink a newsletter lede with inline poliscopic links

You are fixing the §1 overview (lede) of an already-published newsletter
article. It currently contains MACHINE TOKENS like "(agenda item 2)",
"(bos item 44)", "(item 90)", "(phoenix-cc items 102 and 103)". Readers
should instead see natural prose where the phrase that names an item is an
inline markdown link to that item's poliscopic page — exactly like new
newsletters.

## The item universe (ONLY these items exist in the article)

{items_block}

## Rules
1. For every machine token, find the item it refers to and hyperlink the
   NATURAL phrase in the sentence that names/describes that item, markdown
   style: [natural phrase](deep link). Copy the deep link VERBATIM from the
   list above — never invent, modify, or truncate a URL.
2. A token covering several items ("items 7 and 8", "items 24 and 25",
   "phoenix-cc items 102 and 103") must produce ONE link per item, each on
   its own natural phrase. If one natural phrase covers both, still emit
   both links (adjacent).
3. Never write any "(... item ...)" token in the output.
4. Do NOT add, remove, or reword any factual content: keep every number,
   name, address, date, acreage, and recommendation exactly as written.
   Keep the same sentences IN THE SAME ORDER and the same blank-line
   paragraph breaks. Do not merge or split sentences. The ONLY change is:
   machine tokens gone, natural phrases become links.
5. If a token references an item NOT in the list above, remove just that
   parenthetical token (nothing else) and note it in "notes". Never keep a
   token; never link to an item outside the list.
6. Output JSON only: {{"lede": "<relinked overview, blank lines between
   paragraphs>", "notes": "<any removed/unresolved tokens or concerns, or ''>"}}

## Current lede

{lede}
"""


def article_item_universe(session, article) -> list[dict]:
    srcs = session.execute(
        select(ArticleSource).where(ArticleSource.article_id == article.id)
    ).scalars().all()
    items = []
    for s in srcs:
        deep = f"https://poliscopic.com/meetings/{s.body}/{s.meeting_id}#item-{s.agenda_item_number}"
        items.append({
            "body": s.body,
            "meeting_id": str(s.meeting_id),
            "agenda_item_number": str(s.agenda_item_number),
            "agenda_item_title": s.item_title or "",
            "deep_link": deep,
        })
    return items


def split_lede(body: str):
    """Return (lede, rest) — lede is everything before the first bullet section."""
    idx = body.find("\n## ")
    if idx == -1:
        return body, ""
    return body[:idx], body[idx:]


def audit(new_lede: str, items: list[dict]) -> list[str]:
    """Hard audit. Returns list of problems (empty = clean)."""
    problems = []
    allowed = {it["deep_link"] for it in items}
    # 1. every markdown link URL must be a poliscopic deep link of THIS article
    for label, url in MD_LINK_RE.findall(new_lede):
        if url not in allowed:
            problems.append(f"URL not in this article's item set: {url} (label: {label[:60]})")
        elif not POLI_LINK_RE.match(url):
            problems.append(f"Malformed poliscopic URL: {url}")
    # 2. no machine tokens may remain
    leftover = TOKEN_RE.findall(new_lede)
    if leftover:
        problems.append(f"machine tokens remain: {leftover[:5]}")
    # 3. links must not be empty/nested weirdly
    if re.search(r"\[\]\(|\(\)", new_lede):
        problems.append("empty link label or URL present")
    return problems


def relink_article(session, article, dry_run: bool = True, force: bool = False):
    body = article.body or ""
    lede, rest = split_lede(body)
    tokens = TOKEN_RE.findall(lede)
    if not tokens:
        return {"slug": article.slug, "status": "no-tokens", "changed": False}

    items = article_item_universe(session, article)
    if not items:
        return {"slug": article.slug, "status": "no-items", "changed": False}

    prompt = build_prompt(lede, items)
    raw = _call_llm(prompt)
    parsed = _parse_json(raw)
    new_lede = (parsed.get("lede") or "").strip()
    notes = parsed.get("notes") or ""

    if not new_lede:
        return {"slug": article.slug, "status": "empty-llm-output", "changed": False}

    problems = audit(new_lede, items)
    # sanity: after removing markdown link syntax, word count should be close
    old_plain = TOKEN_RE.sub("", lede).strip()
    new_plain = strip_md_links(new_lede).strip()
    ratio = 0.0
    if old_plain:
        import difflib
        ratio = difflib.SequenceMatcher(None, old_plain, new_plain).ratio()

    result = {
        "slug": article.slug,
        "status": "ok" if not problems else "problems",
        "changed": False,
        "problems": problems,
        "notes": notes,
        "similarity": round(ratio, 3),
        "old_lede": lede,
        "new_lede": new_lede,
    }

    if problems:
        return result

    if ratio < 0.80 and not force:
        result["status"] = "low-similarity"
        result["problems"].append(f"plain-text similarity {ratio:.2f} < 0.80 — review before applying")
        return result
    if ratio < 0.80 and force:
        result["notes"] = (notes + " ").strip() + "applied with --force (reviewed; low-sim because of minor rewording)"

    if dry_run:
        result["status"] = "ok-dry"
        return result

    # Apply: replace lede, keep the bullet section untouched.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"article-backfill-{stamp}-{article.slug}.json"
    backup_path.write_text(json.dumps({"slug": article.slug, "old_body": body}, indent=2))

    new_body = new_lede + rest
    article.body = new_body
    # summary column: plain text of the new lede (tokens already gone)
    plain_summary = strip_md_links(new_lede)[:500]
    if plain_summary:
        article.summary = plain_summary
    session.add(article)
    session.commit()
    result["status"] = "applied"
    result["changed"] = True
    result["backup"] = str(backup_path)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="bypass the low-similarity gate (audit still enforced) — only after human review")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    session = get_session()
    articles = []
    if args.slug:
        a = session.execute(select(Article).where(Article.slug == args.slug)).scalar_one_or_none()
        if not a:
            print(f"article not found: {args.slug}")
            return 1
        articles = [a]
    elif args.all:
        articles = session.execute(
            select(Article).where(Article.status == "published").order_by(Article.published_at)
        ).scalars().all()
        articles = [a for a in articles if a.slug and WATCH_RE.search(a.slug)]
    else:
        parser.error("pass --slug or --all")

    print(f"{'DRY-RUN ' if args.dry_run else ''}processing {len(articles)} article(s)")
    results = []
    for a in articles:
        try:
            r = relink_article(session, a, dry_run=args.dry_run, force=args.force)
            results.append(r)
            status = r["status"]
            mark = {"applied": "APPLIED", "ok": "OK", "ok-dry": "OK (dry)", "no-tokens": "no-tokens",
                    "no-items": "no-items", "problems": "PROBLEMS",
                    "low-similarity": "LOW-SIM", "empty-llm-output": "EMPTY-LLM"}.get(status, status)
            print(f"[{mark}] {r['slug']} sim={r.get('similarity')} notes={r.get('notes','')[:80]}")
            for p in r.get("problems", [])[:5]:
                print(f"      ! {p[:200]}")
        except Exception as exc:  # keep going; report per article
            print(f"[ERROR] {a.slug}: {exc}")
            results.append({"slug": a.slug, "status": "error", "error": str(exc)})

    out_name = f"backfill-article-links-{args.slug}.json" if args.slug else "backfill-article-links-report.json"
    out_path = ROOT / "data" / out_name
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"report: {out_path}")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
