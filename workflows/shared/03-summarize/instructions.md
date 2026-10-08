# Summarize & Editorial — Newsletter-Ready Output

**Before anything else, read `docs/newsletter/PRODUCTION-CHECKLIST.md`
(the run contract).** If an editorial brief exists for your topic — e.g.
`docs/newsletter/HOUSING-EDITORIAL-BRIEF.md` for the housing topic — read it
as the quality bar; otherwise this instruction file IS the quality bar.
This step is the **editorial** stage (checklist Step 4): your output must be
newsletter-ready — Subject line, §1 overview, §2 grouped bullets, §3 data
gaps — not a chat reply.

## Inputs (files in your context)

- The topic file (e.g. `housing`): enriched items, each with full
  `agenda_item_text`, `supporting_documents`, `source_url`, and
  `poliscopic_deep_link`.
- `gaps`: meetings in the window with 0 extracted items, each with `body`,
  `meeting_id`, `meeting_date`, and `gap_reason`.
- `summarize-result`/previous output: carry its shape forward if present.

## Rules (editorial brief non-negotiables)

1. **Two links, preserved.** Every item keeps its `poliscopic_deep_link`
   (`https://poliscopic.com/meetings/{body}/{meeting_id}#item-{agenda_item_number}`)
   and its `source_url`. Never drop, rewrite, or invent a link.
2. **Specifics, never generalities.** Acres, addresses, council districts,
   applicant/owner names, staff and P&Z recommendations — exactly as the
   source states. If the source says "196.3± acres at Ellsworth Rd &
   Williams Field Rd," say exactly that.
3. **Topic lens.** For every item, say what it does for the topic at hand
   (for housing: supply, density, entitlement certainty; for public safety:
   response capacity, officer/firefighter resources, emergency
   communications, community safety; adapt to the topic). Judge by what the
   item *does*, not its title.
4. **Never invent facts.** No vote counts, unit counts, dollar amounts, or
   staff recommendations unless the source shows them. If the staff report
   is missing, say "staff report not yet available" rather than guessing.
4b. **No source text = no write-up.** If an item has NO `agenda_item_text`
   AND NO `supporting_documents`, do NOT include it in `items` — you cannot
   write a verifiable summary from a title alone. Move it to `data_gaps`
   instead, with a subscriber-facing note ("Item listed without published
   details — re-check after the next scrape"). Writing a claim from just a
   title will fail the verify gate (this is the chandler-hrc class of bug).
5. **Data gaps are internal, not subscriber copy.** The pipeline captures
   gaps deterministically and verify audits them, but §3 is NOT rendered in
   the newsletter — data gaps go only to the admin digest (Brief 007). Do
   not treat §3 as required newsletter content.

6. **Plain language — write like a news item, ~5th-grade reading level.**
   Subscribers are general readers, not planners. Short sentences, everyday
   words, active voice. Say what the action means for residents instead of
   echoing ordinance language. Define unavoidable jargon in plain terms on
   first use (a "general plan amendment" is the city's long-term growth
   plan update) and expand acronyms (CAP → Central Arizona Project; P&Z →
   Planning & Zoning) before using them. Specifics (rule 2) stay — acres,
   names, addresses — but in plain words. Applies to the overview, every
   item `summary`, and the subject line.

## Output schema — single JSON object

```json
{
  "subject": "<headline drawn from this week's top story> — <date range>",
  "overview": {
    "text": "2–4 short paragraphs separated by blank lines (\\n\\n in the JSON string), plain news language (~5th-grade reading level): synthesizes ALL item summaries, leads with the top-ranked item, then the remaining top items in relevance order (high → medium). Every item referenced in the prose is hyperlinked inline to its poliscopic deep link (markdown [anchor](url), see §1 link rule). No machine tokens like \"(bos item 44)\".",
    "top_item": {
      "body": "glendale-cc",
      "meeting_id": "…",
      "agenda_item_number": "…",
      "title": "…"
    }
  },
  "items": [
    {
      "body": "glendale-cc",
      "meeting_id": "…",
      "agenda_item_number": "…",
      "agenda_item_title": "…",
      "poliscopic_deep_link": "https://poliscopic.com/meetings/…",
      "source_url": "https://…",
      "meeting_date": "…",
      "meeting_title": "…",
      "relevance": "high",
      "lens": "supply-positive",
      "summary": "1–2 sentence bullet…"
    }
  ],
  "data_gaps": [
    {
      "body": "phoenix-cc",
      "meeting_id": "…",
      "meeting_date": "…",
      "note": "Agenda not yet published — re-check after the next scrape"
    }
  ]
}
```

## Section requirements (checklist Step 4)

- **Subject (the week's headline):** a fresh, specific headline drawn from
  this week's biggest story — the item in `overview.top_item` — followed by
  the date range: `"<headline> — <date range>"`. The headline must name the
  actor and the concrete action (jurisdiction + what they're doing) in
  active editorial voice, roughly 6–12 words, ≤ ~80 chars total so it
  survives as an email subject. It must be anchored to the overview (verify
  maps it to the top item's evidence). No generic labels ("This Week in
  Water"), no clever-for-its-own-sake puns — specific beats cute. Examples
  (illustrative, from real top items): `Phoenix launches multi-agency Gila
  River optimization study — Aug 24–31`; `Scottsdale eyes broader water
  conservation rebates — Aug 24–31`. Fallback: a thin week gets the top
  item's own title tightened, never an invented theme.
- **§1 The overview:** synthesize ALL items in `items` — never just one.
  Write **2–4 short paragraphs separated by a blank line** (two newlines,
  `\n\n`, inside the JSON `text` string). The renderer turns each
  blank-line-separated block into its own `<p>` in the email — never emit
  the whole overview as one long unbroken paragraph. Each paragraph is
  1–3 sentences in plain, approachable news language (~5th-grade reading
  level — rule 6). Lead paragraph: the single biggest story — the item a
  reader of this topic would most want forwarded; record its identity in
  `overview.top_item`. Then the remaining top items in workflow rank order:
  `relevance` high first, then medium. Close with the through-line that
  connects them (for housing: what the week does for supply, density,
  entitlement certainty). **Never end a paragraph with a street address.**
  Gmail auto-linkifies street addresses: an address at the end of a
  paragraph gets stitched to the next paragraph's first word (often a city
  name) into a bogus Google Maps link (observed 2026-09-08: "…1338 W Lobo
  Avenue." at paragraph end followed by a paragraph starting "Phoenix …"
  → Gmail linked "Phoenix" and a stray "." to a Maps search). Keep street
  addresses mid-sentence, followed by at least a short clause (e.g. "…at
  1338 W Lobo Avenue, about 850 feet from another community residence."),
  or move the address into the §2 bullet. The same rule applies inside item
  `summary` text. **Link every item you mention.** Each item
  referenced in a sentence is hyperlinked inline, markdown style, on the
  natural phrase that names it — e.g. "Maricopa County supervisors would
  [expand a down-payment assistance contract](https://poliscopic.com/meetings/bos/4699#item-44)
  to help about 148 buyers." When one sentence covers several items, give
  each its own link on its own phrase (e.g. items 102 and 103 → two links:
  one on the General Plan change phrase, one on the PUD phrase). Copy the
  URL exactly from the item's `poliscopic_deep_link` — never invent,
  truncate, or reformat it. **Never write machine tokens like "(bos item
  44)" or "(item 90)"** — the reader gets clean prose with real links, and
  the verify gate maps claims to items by the link URL. No invented facts —
  same rules as items. Thin week (1–2 items): a single 2–4 sentence
  paragraph is fine (≈ the old single-headline behavior).
- **§2 Also on the calendar:** every remaining item becomes a `summary`
  bullet, 1–2 sentences each, with the specifics from rule 2. Keep them
  grouped by body/date in the output ordering (the renderer groups them and
  bolds meeting name + date).
- **§3 Data gaps & notes:** every entry from the `gaps` input becomes a
  `data_gaps` entry, with an honest `note` and a concrete "re-check after…"
  timing.

## Quality bar

Match the gold-standard style in the editorial brief (or this file):
editorial voice, specifics, grouped by meeting with dates, honest caveats.
If the source material is thin, write what you can from title + context —
but never pad with invented facts.

Process ALL items in the topic file. Preserve all existing fields on every
item.
