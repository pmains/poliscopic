# Boards & Commissions — Classify

**Before anything else, read `docs/newsletter/PRODUCTION-CHECKLIST.md`
(the run contract).** Your job is Step 2 of the checklist: classify.
Rules that carry forward into the newsletter (all topics):
- Every kept item must carry BOTH `poliscopic_deep_link` and `source_url`.
- Data-gap notes in §3 are **subscriber-facing only** — never include
  internal diagnostics (brief numbers, sync state, extraction internals).
  Ops detail belongs in the daily digest, not subscriber copy.


Find agenda items from recent public meetings that are genuinely about
**Maricopa County boards and commissions** (excluding the Board of Supervisors).

**Scope is hard-limited to the Maricopa County jurisdiction.** The candidate
pool is already filtered to Maricopa County bodies — do not reintroduce items
from other jurisdictions. Exclude city and town councils *and* their boards
and commissions (e.g. Chandler IDA, Fountain Hills BOA), and regional bodies
(MAG, Valley Metro). If an item's `jurisdiction_name` is not "Maricopa County",
drop it.

Include items about:

- Planning & Zoning Commission — zoning cases, general plan amendments
- Board of Adjustment — variance requests, special use permits
- Industrial Development Authority (IDA) — bond issuances, development incentives
- Transportation Advisory Board — road and transit planning recommendations
- Board of Health — public health policy, health department oversight
- HOME/CDAC — affordable housing funding allocations
- Parks & Recreation Board — park planning, bond projects
- Library District Board
- Air Quality Commission
- Any other county board or commission action

Exclude:
- Board of Supervisors items (covered separately in county coverage)
- Anything from another jurisdiction — city/town councils and their boards
  and commissions, and regional bodies such as MAG or Valley Metro
- Routine minutes approval

Return a JSON object with key "boards_commissions" and an array of matching items.
Each item should include: body, meeting_id, meeting_date, meeting_type,
meeting_title, agenda_item_number, agenda_item_title, jurisdiction_name,
relevance (high/medium/low), and a brief reason explaining which board/commission
it concerns.

Do NOT write summaries — the system will enrich items with full text
and supporting documents in the enrichment step, then generate summaries
in a dedicated summarization step. For now, just identify and classify.
