# Summarize & Editorial — Weekly Roundup (Week in Review + Look Ahead)

**Before anything else, read `docs/newsletter/PRODUCTION-CHECKLIST.md`
(the run contract).** This step is the **editorial** stage (checklist
Step 4): your output must be newsletter-ready — Subject line, §1 overview,
§2 grouped bullets, §3 data gaps — not a chat reply.

## Inputs (files in your context)

- The topic file (`weekly-roundup`): enriched items, each with full
  `agenda_item_text`, `supporting_documents`, `source_url`, and
  `poliscopic_deep_link`. Items split by `meeting_date` into the past-week
  review (10) and the upcoming look-ahead (10).
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
3. **Cross-topic lens.** This edition spans all topics, so judge each item
   by its public impact: money involved, policy significance, residents
   affected, controversy, precedent-setting weight. Judge by what the item
   *does*, not its title.
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
   first use and expand acronyms before using them. Specifics (rule 2)
   stay — acres, names, addresses — but in plain words. Applies to the
   overview, every item `summary`, and the subject line.

## Output schema — single JSON object

```json
{
  "subject": "Week in Review — <date range>",
  "overview": {
    "text": "Up to 5 paragraphs separated by blank lines (\\n\\n in the JSON string), plain news language (~5th-grade reading level — rule 6): synthesizing the week — what dominated, which bodies were active, the through-line across topics. Every item referenced is hyperlinked inline to its poliscopic deep link (markdown [anchor](url), see §1 link rule). No machine tokens like \"(bos item 44)\".",
    "top_item": {
      "body": "phoenix-cc",
      "meeting_id": "…",
      "agenda_item_number": "…",
      "title": "…"
    }
  },
  "items": [
    {
      "body": "phoenix-cc",
      "meeting_id": "…",
      "agenda_item_number": "…",
      "agenda_item_title": "…",
      "poliscopic_deep_link": "https://poliscopic.com/meetings/…",
      "source_url": "https://…",
      "meeting_date": "…",
      "meeting_title": "…",
      "relevance": "high",
      "lens": "public-impact",
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

- **Subject (the week's headline):** `"Week in Review — <date range>"`
  style. The date range covers the review window (e.g. `Aug 24–30`).
  Optionally add a specific headline drawn from the week's biggest story
  before the range; if you do, it must name the actor and the concrete
  action in active editorial voice, roughly 6–12 words, ≤ ~80 chars total
  so it survives as an email subject. It must be anchored to
  `overview.top_item`. No generic labels, no clever-for-its-own-sake puns —
  specific beats cute. Fallback: a thin week gets the top item's own title
  tightened, never an invented theme.
- **§1 The overview — the "appropriate summary" (up to 5 paragraphs).**
  This edition's overview is a true week-in-review synthesis: frame the
  week, name what dominated and which bodies were active, and draw the
  through-line across topics. Longer than the standard topic overview
  (2–4 short paragraphs; this edition explicitly allows up to 5).
  Separate paragraphs with a blank line (`\n\n` in the JSON `text` string)
  — the renderer turns each blank-line-separated block into its own `<p>`;
  never emit the overview as one unbroken wall of text. Write in plain
  news language (~5th-grade reading level — rule 6). Lead with the single
  biggest story of the week — the item a reader would most want forwarded;
  record its identity in `overview.top_item`. Then the remaining top items
  in workflow rank order: `relevance` high first, then medium. **Never end
  a paragraph with a street address** — Gmail auto-linkifies addresses and
  stitches an end-of-paragraph address to the next paragraph's first word
  (often a city) into a bogus Google Maps link. Keep street addresses
  mid-sentence with a short clause after them, or move them into §2. The
  same rule applies inside item `summary` text. **Link every
  item you mention.** Each item referenced in a sentence is hyperlinked
  inline, markdown style, on the natural phrase that names it — e.g.
  "County supervisors would [expand a down-payment assistance contract](https://poliscopic.com/meetings/bos/4699#item-44)
  to help about 148 buyers." When one sentence covers several items, give
  each its own link on its own phrase. Copy the URL exactly from the item's
  `poliscopic_deep_link` — never invent, truncate, or reformat it. **Never
  write machine tokens like "(bos item 44)" or "(item 90)"** — the reader
  gets clean prose with real links, and the verify gate maps claims to
  items by the link URL. No invented facts —
  same rules as items. Thin week: collapse proportionally, never pad.
- **§2 The week in review / coming up:** every remaining item becomes a
  `summary` bullet, 1–2 sentences each, with the specifics from rule 2.
  Keep them grouped by body/date in the output ordering (the renderer
  groups them, bolds meeting name + date, and splits past vs. upcoming by
  `meeting_date`).
- **§3 Data gaps & notes:** every entry from the `gaps` input becomes a
  `data_gaps` entry, with an honest `note` and a concrete "re-check after…"
  timing.

## Quality bar

Editorial voice, specifics, grouped by meeting with dates, honest caveats.
Synthesize across topics — the reader should finish the overview knowing
what kind of week it was in Maricopa County governance. If the source
material is thin, write what you can from title + context — but never pad
with invented facts.

Process ALL items in the topic file. Preserve all existing fields on every
item.
