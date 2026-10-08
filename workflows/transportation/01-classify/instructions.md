# Transportation & Infrastructure — Classify

**Before anything else, read `docs/newsletter/PRODUCTION-CHECKLIST.md`
(the run contract).** Your job is Step 2 of the checklist: classify.
Rules that carry forward into the newsletter (all topics):
- Every kept item must carry BOTH `poliscopic_deep_link` and `source_url`.
- Data-gap notes in §3 are **subscriber-facing only** — never include
  internal diagnostics (brief numbers, sync state, extraction internals).
  Ops detail belongs in the daily digest, not subscriber copy.


Find agenda items from recent public meetings that are genuinely about
**transportation and infrastructure**. Include items about:

- Road and highway projects — construction, widening, maintenance
- Transit — bus routes, light rail, Valley Metro funding and planning
- MAG (Maricopa Association of Governments) transportation planning
- Bicycle and pedestrian infrastructure
- Complete streets projects
- Traffic management and signal systems
- Bridge maintenance and replacement
- Freeway projects (ADOT)
- Airport operations and planning
- Transit-oriented development
- Transportation funding and sales tax measures

Exclude:
- Routine street sweeping or landscaping
- Traffic control for special events
- Sidewalk repair requests from individual properties

Return a JSON object with key "transportation" and an array of matching items.
Each item should include: body, meeting_id, meeting_date, meeting_type,
meeting_title, agenda_item_number, agenda_item_title, jurisdiction_name,
relevance (high/medium/low), and a brief reason explaining why it matters
for transportation.

Do NOT write summaries — the system will enrich items with full text
and supporting documents in the enrichment step, then generate summaries
in a dedicated summarization step. For now, just identify and classify.
