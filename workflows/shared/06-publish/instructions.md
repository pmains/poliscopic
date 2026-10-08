# Publish — ONE Article per Newsletter Run (deterministic, no LLM)

This step is handled deterministically by the workflow runner — no LLM
call. It creates exactly ONE published article from the verified run
output and syncs it to production.

## Rule (Pete directive 2026-08-27)

**1 newsletter = 1 article.** Never create multiple articles per
newsletter/topic. If the run has nothing substantive to publish, publish
nothing rather than padding.

## What the runner does (workflow-runner.py `_handle_publish_step`)

1. Reads `verify-result.json` from the verified input dir.
2. If the run is not approved, or has zero items → skips (writes
   `publish-result.json` with `status: skipped`). No article is created.
3. Otherwise runs:
   ```
   .venv/bin/python -u scripts/publish_newsletter_article.py <topic> --run-id <run_id>
   ```
   which builds ONE article (subject as title, overview as the lede,
   item summaries grouped by meeting, topic + jurisdiction tags, per-item
   ArticleSource rows) and inserts it into the dev DB — idempotent by slug.
4. Runs `scripts/editorial_sync.py` to push editorial tables
   (tags → articles → article_sources → article_tags) dev → prod.
5. Writes `publish-result.json` to the output dir.

## Featured image (Pete directive 2026-09-08)

The published article gets a topic-appropriate `featured_image`, chosen
DETERMINISTICALLY (no LLM) by `scripts/newsletter_images.py`
(`pick_featured_image(topic, top_story_text, run_date)`):

- housing → `tempe-apartment-building.jpg` ↔ `tempe-row-houses.jpg`
- public-safety → `phoenix-police-suv.jpg` ↔ `phoenix-fire-truck.jpg`
- transportation → `rail-bridge.jpg` ↔ `asu-transit-station.jpg` ↔
  `tempe-rail-station.jpg` ↔ `static/images/mill-ave-light-rail.jpg`
- water-environment → `colorado-river-flickr.jpg` (preferred) ↔
  `scottsdale-water-faucet.jpg`
- boards-commissions / weekly-roundup → classify the run's TOP STORY into
  one of the four pools above and use that pool's image; no match → no
  image ("").

**Mixing rule (Pete, same day): redundant images ROTATE by ISO week of the
run date, so the site does not show the same photo every week.** Specific
stories stay pinned to the right image (fire/EMS → fire truck; police →
police SUV; river/Colorado → colorado-river; faucet/rebate → faucet);
generic stories and ties rotate through the pool. Front-page cards use the
same rotation via `card_fallback_image(tags, published_at)`.

Photos staged by Pete in `featured-photos/`; web-sized copies live in
`static/uploads/`. Paths are root-relative (/static/...) to match how
Article.featured_image is stored elsewhere.

## Output

`publish-result.json`:
```json
{
  "status": "succeeded" | "skipped" | "failed",
  "reason": "…"
}
```

## Guardrails

- One article per run, keyed by slug (`{run-date}-{topic}-watch`).
- No approved items → publish nothing (no padding).
- If the article already exists (re-run), it's a no-op.
