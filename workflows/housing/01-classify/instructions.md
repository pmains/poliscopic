# Housing & Development — Classify

**Before anything else, read `docs/newsletter/HOUSING-EDITORIAL-BRIEF.md`
(the quality bar) and `docs/newsletter/PRODUCTION-CHECKLIST.md` (the run
contract).** The brief defines the YIMBY lens and the non-negotiables; the
checklist defines the pipeline contract. Your job is Step 2 of the checklist:
cast a wide net for housing-relevant items.

Find agenda items from public meetings in the window that relate to **housing,
residential development, zoning, or land use**. Cast a wide net — it's
better to include a borderline item than miss a real one.

Include items about:

**Housing policy and funding:**
- Affordable housing policy and funding (HOME, CDBG, housing trust fund, Section 8)
- Housing authority actions
- Homeless services and housing stability programs
- Community development block grants and related spending
- Residential development moratoriums or incentives
- Aging in place, senior housing, supportive housing

**Zoning and land use:**
- Rezoning and zoning map amendments (including General Plan amendments)
- Zoning code amendments affecting housing
- Planned area developments and master plans involving residential
- Land use changes affecting residential density
- Annexations enabling residential development

**Development and subdivisions:**
- Subdivision maps and lot splits
- Development agreements for residential projects
- Multifamily and apartment developments
- ADUs (accessory dwelling units) and missing-middle housing
- Entitlements and specific plans with a residential component

**Residential property decisions:**
- Use permits affecting residential properties (parking setbacks, garage
  conversions, home-based businesses on residential lots)
- Variance requests involving residential setback or land use requirements
- Abatement appeals related to housing conditions or code enforcement

**Infrastructure enabling housing:**
- Water/sewer improvements specifically tied to new housing development
- Drainage improvements protecting existing or planned residential areas

**Committee appointments:**
- Appointments to housing, community development, or planning commissions

Exclude:
- Routine building permits with no policy significance
- Items that only mention "housing" in passing (e.g., "the facility includes housing for staff")
- Purely procedural items with zero housing content (minutes approval, adjournment, scheduling-only)
- Law enforcement, criminal justice, or non-housing social services
- Road easements and right-of-way abandonments with no housing nexus

## Two-link rule (editorial brief, non-negotiable #1)

Every classified item MUST carry both links, from the very first step:

- `poliscopic_deep_link`:
  `https://poliscopic.com/meetings/{body}/{meeting_id}#item-{agenda_item_number}`
  — construct it exactly from the item's own `body`, `meeting_id`, and
  `agenda_item_number` (never guess the body slug; use the `body` value from
  the DB context, e.g. `mesa-cc`, `glendale-pc`).
- `source_url`: the meeting's `source_url` from the context.

If you cannot construct both links for an item, still classify it but set
`poliscopic_deep_link` to `""` — the verify step is the hard gate and will
flag it; do not invent a link.

## YIMBY lens (editorial brief, non-negotiable #3)

For every item, answer: what does this do for housing supply, density, or
entitlement certainty? Judge by what the item *does*, not by its title. Label
each item with one of:

- `supply-positive` — rezones to residential/mixed-use, PAD amendments adding
  land uses, annexations enabling development, ADUs, density, PC districts.
- `supply-negative` / `watch` — moratoriums, parking minimums, height/density
  caps, use-permit friction, lengthy continuances.
- `neutral` — appointments, funding allocations without a clear supply signal.

## Data gaps (awareness only)

Your context contains a `gaps` list: meetings inside the window with 0
extracted agenda items. Use it for awareness only. **Do NOT return a `gaps`
key in your output** — the pipeline writes `gaps.json` deterministically
(checklist Step 1). Never imply coverage that doesn't exist.

## Output

Return a JSON object with key `"housing"` and an array of matching items.
Each item should include: `body`, `meeting_id`, `meeting_date`,
`meeting_type`, `meeting_title`, `agenda_item_number`, `agenda_item_title`,
`jurisdiction_name`, `relevance` (high/medium/low), `lens`
(supply-positive / supply-negative / watch / neutral), `poliscopic_deep_link`,
`source_url`, and a brief `reason` explaining why it matters for housing
policy through the YIMBY lens.

Do NOT write summaries — the system will enrich items with full text
and supporting documents in the enrichment step, then generate summaries
in a dedicated editorial step. For now, just identify and classify.
