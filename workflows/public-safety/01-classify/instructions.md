# Public Safety & Justice — Classify

**Before anything else, read `docs/newsletter/PRODUCTION-CHECKLIST.md`
(the run contract).** Your job is Step 2 of the checklist: classify.
Rules that carry forward into the newsletter (all topics):
- Every kept item must carry BOTH `poliscopic_deep_link` and `source_url`.
- Data-gap notes in §3 are **subscriber-facing only** — never include
  internal diagnostics (brief numbers, sync state, extraction internals).
  Ops detail belongs in the daily digest, not subscriber copy.


Find agenda items from recent public meetings that are genuinely about
**public safety and justice**. Include items about:

- Sheriff's office operations, budgets, policies
- Police departments — contracts, equipment, policies, reform
- Fire departments — stations, equipment, emergency response
- Corrections — jails, detention centers, inmate services
- Courts — administration, funding, programs
- Emergency management and 911 dispatch systems
- Law enforcement policies and oversight
- First responder programs
- Public safety technology (body cameras, records systems, dispatch)

Exclude:
- Routine traffic signals or street lighting (those are transportation)
- Fire code inspections for building permits
- Items where "safety" is used generically ("for the safety of our community")

Return a JSON object with key "public_safety" and an array of matching items.
Each item should include: body, meeting_id, meeting_date, meeting_type,
meeting_title, agenda_item_number, agenda_item_title, jurisdiction_name,
relevance (high/medium/low), and a brief reason explaining why it matters
for public safety.

Do NOT write summaries — the system will enrich items with full text
and supporting documents in the enrichment step, then generate summaries
in a dedicated summarization step. For now, just identify and classify.
