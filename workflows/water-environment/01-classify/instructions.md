# Water & Environment — Classify

**Before anything else, read `docs/newsletter/PRODUCTION-CHECKLIST.md`
(the run contract).** Your job is Step 2 of the checklist: classify.
Rules that carry forward into the newsletter (all topics):
- Every kept item must carry BOTH `poliscopic_deep_link` and `source_url`.
- Data-gap notes in §3 are **subscriber-facing only** — never include
  internal diagnostics (brief numbers, sync state, extraction internals).
  Ops detail belongs in the daily digest, not subscriber copy.


Find agenda items from recent public meetings that are genuinely about
**water policy and the environment**. Include items about:

- Water supply and drought management
- Groundwater management and replenishment
- Central Arizona Project (CAP) allocations and agreements
- Wastewater treatment and infrastructure
- Stormwater management and flood control
- Water rights and transfers
- Reclaimed water and effluent use
- Water conservation programs
- Watershed management
- Environmental policy and regulation
- Air quality — monitoring, regulation, permits, pollution control, PM10/PM2.5, dust control
- Flood control district actions
- Climate adaptation and resilience planning

Exclude:
- Items that mention water only incidentally ("water fountains in the park")
- Routine utility bill payments or rate changes for residential service
- General environmental proclamations without policy substance

Return a JSON object with key "water" and an array of matching items.
Each item should include: body, meeting_id, meeting_date, meeting_type,
meeting_title, agenda_item_number, agenda_item_title, jurisdiction_name,
relevance (high/medium/low), and a brief reason explaining why it matters
for water or environmental policy.

Do NOT write summaries — the system will enrich items with full text
and supporting documents in the enrichment step, then generate summaries
in a dedicated summarization step. For now, just identify and classify.
