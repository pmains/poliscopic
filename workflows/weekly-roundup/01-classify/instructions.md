# Weekly Roundup — Classify

**Before anything else, read `docs/newsletter/PRODUCTION-CHECKLIST.md`
(the run contract).** Your job is Step 2 of the checklist: classify.
Rules that carry forward into the newsletter (all topics):
- Every kept item must carry BOTH `poliscopic_deep_link` and `source_url`.
- Data-gap notes in §3 are **subscriber-facing only** — never include
  internal diagnostics (brief numbers, sync state, extraction internals).
  Ops detail belongs in the daily digest, not subscriber copy.
- Do NOT write summaries — the system will enrich items with full text
  and supporting documents in the enrichment step, then generate summaries
  in a dedicated summarization step. For now, just identify and classify.


This is the **cross-topic** edition. Unlike the topic newsletters
(boards-commissions, housing, public-safety, water-environment,
transportation), there is **no topic keyword filter** — you select across
ALL jurisdictions and ALL topics in the window.

## Selection: exactly 20 items, two buckets

Select **exactly 20 items total**:

- **10 most interesting from the past week** — `meeting_date` within the
  past 7 days (review).
- **10 most interesting upcoming** — `meeting_date` today through the next
  13 days (look ahead).

"Interesting" means public impact, money involved, policy significance,
controversy, number of residents affected, or precedent-setting. Rank the
items within each bucket (most interesting first). If a bucket has fewer
candidates than 10, keep all of them rather than padding with weak items —
the edition is allowed to be thin; it is never padded.

## What counts

- Major contracts and expenditures
- Land use, zoning, and annexation
- Transportation and infrastructure
- Water and environment
- Housing and development
- Public safety
- Budgets, taxes, and fees
- Ordinances and resolutions
- Council actions with broad public impact
- Anything with clear public controversy or precedent-setting weight

Exclude purely routine items: consent-calendar housekeeping, individual
permit decisions with no broader impact, routine appointments, ceremonial
proclamations, and items whose only significance is procedural.

## Output

Return a JSON object with key `"weekly-roundup"` and an array of items.
Each item should include the standard fields: body, meeting_id,
meeting_date, meeting_type, meeting_title, agenda_item_number,
agenda_item_title, jurisdiction_name, relevance (high/medium/low), and a
brief reason explaining why it matters.

The `meeting_date` field drives the renderer's past/upcoming split — no
custom section field is needed. Keep items ordered within each bucket:
the first 10 entries should be the past-week review in rank order, the
next 10 the upcoming look-ahead in rank order.
