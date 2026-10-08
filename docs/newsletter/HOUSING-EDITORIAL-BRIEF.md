# Housing & Development — Editorial Brief (newsletter quality bar)

**Audience:** YIMBYs — readers who want more housing supply, density, and
entitlement certainty across Maricopa County.
**Purpose:** Turn verified agenda items into a newsletter people actually read.
This brief is the difference between a data dump and a newsletter. Every step
that touches content (classify, summarize, editorial) reads this file.

---

## Non-negotiables

1. **Every item carries two links:** a poliscopic.com deep link AND the source
   agenda link.
   - poliscopic: `https://poliscopic.com/meetings/{body}/{meeting_id}#item-{agenda_item_number}`
     (body slugs like `mesa-cc`, `glendale-pc`; verify against DB `meetings.body`)
   - source: the original `source_url` on the meeting or agenda item
2. **Specifics, never generalities.** Acres, addresses, council districts,
   applicant/owner names, staff and P&Z recommendations. If the source says
   "196.3± acres at Ellsworth Rd & Williams Field Rd," say exactly that.
3. **YIMBY lens.** For every item, answer: what does this do for housing
   supply, density, or entitlement certainty?
   - Supply-positive: rezones to residential/mixed-use, PAD amendments adding
     land uses, annexations enabling development, ADUs, density, PC districts.
   - Supply-negative / watch: moratoriums, parking minimums, height/density
     caps, use-permit friction, lengthy continuances.
   - Judge by what the item *does*, not by its title.
4. **Honesty about data gaps.** If a jurisdiction's agenda shows no extracted
   items (e.g., Phoenix Formal with 0 items), say so explicitly — never imply
   coverage that doesn't exist. Note when to re-check.
5. **Never invent facts.** No vote counts unless the source shows them. No unit
   counts unless stated. If the staff report is missing, say "staff report not
   yet available" rather than guessing.

---

## Newsletter format

**Subject (the week's headline):** a fresh, specific headline drawn from this
week's biggest story (the top-ranked item) + the date range, e.g.
`"<headline> — <date range>"`. Name the actor and the concrete action in
active voice, ~6–12 words. Not a generic label.

*Section 1 — The overview.* 2–4 short paragraphs separated by a blank line
(two newlines, `\n\n`, in the JSON `text` string — the renderer turns each
block into its own `<p>`; never one unbroken wall of text), each 1–3
sentences in plain news language (~5th-grade reading level). Synthesizes ALL
item summaries. Lead with the biggest story — the item a YIMBY would forward
— then the remaining top items in relevance order (high → medium). Every
item you mention is hyperlinked inline on the natural phrase that names it
(markdown `[anchor](poliscopic_deep_link)` — see §1 link rule in the shared
summarize instructions). No machine tokens like "(bos item 44)" — readers
get clean prose with real links. Draw the through-line:
what this week does for housing supply, density, or entitlement certainty.
Thin week (1–2 items): a 2–4 sentence write-up of those items is fine.

*Section 2 — Also on the calendar.* Remaining items, bulleted, 1–2 sentences
each, grouped by body/date. Lead with the meeting name and date in bold.

*Section 3 — Data gaps & notes.* What we can't see yet and when to look again
("Phoenix Formal 8/26 and 9/9 show no extracted items — Phoenix formal agendas
usually carry the zoning action; re-check after Thursday's scrape").

---

## Gold-standard example

The return from 2026-08-25 (chat query "housing-related items in the next two
weeks for YIMBYs") — it had editorial voice, grouped items by meeting with
dates, flagged the data gaps, and offered follow-ups. Style to match:

> _Glendale City Council — Tue 8/25 (tonight)_
> • Project Eagle AN-273 annexation, west of Litchfield Rd & Olive Ave —
>   185 acres-ish annexation (Glendale's been doing a string of these to bring
>   developable land into the city). Item 2.
>
> _Glendale Planning Commission — Thu 8/27_
> • ZON26-03: PAD amendment at 7400 N Zanjero Blvd ("Nirvana at Zanjero,"
>   7.5 ac) to allow additional land uses — this one smells like a residential
>   project looking for more entitlements.
>
> Caveats: Phoenix City Council Formal meeting 8/26 is in the DB but with 0
> extracted items... Tempe Board of Adjustment 8/26 is still pending
> extraction. Most early-September agendas aren't published/extracted yet.

## Sources & window

- Window: next 14 days from run date (`meeting_date BETWEEN today AND today+13`)
- DB: `meetings` + `agenda_items` via `scripts/db/config.py` (`DATABASE_URL`)
- Housing keywords: zoning, rezone, general plan, land use, development
  agreement, PAD, subdivision, plat, ADU, accessory dwelling, density,
  affordable, multifamily, apartment, mixed-use, infill, annexation,
  entitlement, specific plan, townhome, condominium, transit-oriented, floor
  area. Also case-number prefixes: ZON-, ANX-, GP-, PC-.

---

## Wiring (implemented directly by the poliscopic agent)

- Reference this brief from `workflows/housing/01-classify/instructions.md`
  and `workflows/shared/03-summarize/instructions.md`, or add an
  editorial step between summarize and verify that reads this file.
- Keep `workflows/templates/render.py` as the deterministic HTML formatter;
  the brief's Section 1/2/3 structure should map onto the template.
- The send step already deep-links poliscopic.com correctly — preserve that.
