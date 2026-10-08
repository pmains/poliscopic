# Verify — Hard Gate with Correction Loop (checklist Step 5)

**You are the last line of defense. Read `docs/newsletter/PRODUCTION-CHECKLIST.md`
Step 5 before verifying.** If an editorial brief exists for your topic
(`docs/newsletter/<TOPIC>-EDITORIAL-BRIEF.md`, e.g. HOUSING for the housing
topic), read it as the quality bar; otherwise this instruction file is the
quality bar.

**How the gate behaves (Pete directive 2026-08-26):** when you catch a
factually incorrect or unsupported claim, **correct the text, then verify the
corrected text, then continue.** A catch is NOT a failed run. You fix what is
fixable, drop what is not, and only reject when something cannot be made
verifiable at all. The run proceeds to send with your corrected copy.

## Inputs (files in your context)

- `summarize-result` (or the previous step's result): the editorial output —
  `subject`, `overview`, `items` (with `summary`), `data_gaps`.
- The topic file (e.g. `housing`): enriched items with full
  `agenda_item_text` and `supporting_documents` — the source material.
- `gaps`: the deterministic data-gap entries.

## Step 1 — Two-link check (per item)

For EVERY item in the editorial output:

1. `poliscopic_deep_link` is present AND exactly matches
   `https://poliscopic.com/meetings/{body}/{meeting_id}#item-{agenda_item_number}`
   where `{body}` equals the item's own `body` value from the DB.
   - Missing, malformed, or mismatched body/meeting_id/item number →
     **correct it** (links are deterministic — regenerate from the item's own
     body/meeting_id/item number). Only if you genuinely cannot produce a
     valid link → blocking.
2. `source_url` is present and starts with `http://` or `https://`.
   - Missing or empty → **correct it** from the enriched item data. Only if
     no source URL exists anywhere → blocking.

## Step 2 — Claim audit + correct (per item)

Break each `summary` (and the `overview.text`) into individual factual
claims. Verify each against `agenda_item_text` and `supporting_documents`:

- **Supported** — source confirms it → keep, score `high`.
- **Implied** — source suggests but doesn't state → soften the wording to
  match the source ("the agreement covers…" not "the agreement means…"),
  score `medium`, note it.
- **Unsupported / contradicted** — source says something different or is
  silent → **REWRITE the claim to match the source exactly**, score the
  corrected claim `high` or `medium`.

Hard rules:
- **No invented vote counts.** Only say "approved" / "voted" / "3-2" if the
  source shows it. Otherwise say "agenda item; decision pending" or similar.
- **No invented unit counts / acreage / dollar amounts.** Only repeat numbers
  the source states.
- **No invented staff or P&Z recommendations.** If the staff report is
  missing, say "staff report not yet available".
- **No source text = drop, don't invent.** If an item has NO
  `agenda_item_text` AND NO `supporting_documents`, you cannot verify it.
  **Remove it from `items` and add it to `data_gaps`** with a subscriber-facing
  note ("Item listed without published details — re-check after the next
  scrape"). This is NOT a run failure — it is honest gap reporting.
- **Overview audit (same rules apply to `overview.text`).** Every claim in
  the overview must be anchored to a specific item — the overview links
  that item inline: markdown `[phrase](https://poliscopic.com/meetings/{body}/{meeting_id}#item-{agenda_item_number})`,
  URL copied verbatim from the item's `poliscopic_deep_link`. Verify each
  overview claim against THAT item's `agenda_item_text` /
  `supporting_documents`, not the corpus as a whole.
  **Link audit (deterministic):** every `http(s)://` URL inside
  `overview.text` must exactly equal the `poliscopic_deep_link` of an item
  in your output `items`. A missing, malformed, or mismatched link →
  **correct it** (links are deterministic — regenerate from the item's own
  body/meeting_id/item number). A claim with no link at all → rewrite so
  the sentence links its item, or drop the sentence from the overview. Do
  not delete the overview — it is required §1 content; if the overview
  becomes empty, fall back to the top item's write-up.
  **Never emit machine tokens like "(bos item 44)" in the overview.** If
  the editorial output used them, rewrite those sentences so each item is
  linked inline on a natural phrase instead.
  **Never leave a street address at the end of an overview paragraph.**
  Gmail auto-linkifies addresses and will stitch an end-of-paragraph
  address to the next paragraph's first word (often a city name) into a
  bogus Google Maps link. If a paragraph ends with a street address
  (e.g. "…at 1338 W Lobo Avenue."), rewrite so the address sits
  mid-sentence with a short clause after it, or move it into the item
  summary bullet.
  **Preserve paragraph structure while correcting:** the overview is written
  as 2–4 short paragraphs separated by blank lines (`\n\n`) — keep those
  separators intact in every rewrite. The renderer splits on blank lines to
  make separate `<p>` tags; collapsing them re-creates the one-wall-of-text
  email bug.
- After rewriting, **re-audit the corrected text against the source**. The
  corrected text must verify. If a correction is impossible, drop the item to
  `data_gaps` as above.

## Step 3 — Data-gap audit (internal only, not rendered)

Compare the editorial `data_gaps` against the deterministic `gaps` input:

- Every entry in `gaps` MUST appear in `data_gaps` (allowing the note to add
  re-check timing). A missing gap → **blocking** ("§3 omitted gap for
  <body> <date>").
- Gap notes must be honest — no implied coverage.
- **Data gaps are INTERNAL.** They are NOT rendered in the subscriber
  newsletter (render.py drops §3; Pete directive 2026-08-26). The daily
  ops digest (Brief 007) is the only subscriber-adjacent surface for gap
  reporting, and it computes its own checks from the DB. Keep the audit
  honest and complete, but no subscriber-facing language rules apply —
  the block is simply not shipped.

## Step 4 — Produce the final (corrected) text

- All claims score `high`/`medium` after correction: write the corrected
  `revised_summary` (same as `summary` when nothing changed).
- If any claim was rewritten or an item was dropped: `revised_summary` is
  your corrected version; dropped items appear only in `data_gaps`.
- If you corrected the `overview.text`, output the corrected text as
  `_summary` AND in `overview.text`. Preserve the blank-line (`\n\n`)
  paragraph separators in both — do not merge paragraphs into one block.

## Step 5 — Output (single JSON object)

```json
{
  "status": "approved",
  "approved": true,
  "subject": "<editorial subject, passthrough>",
  "_summary": "<overview.text — corrected if needed — rendered as §1>",
  "overview": { "<editorial overview object, passthrough, corrected if needed>" },
  "data_gaps": [ "<editorial data_gaps + any dropped items, subscriber-facing>" ],
  "items": [
    {
      "body": "glendale-cc",
      "agenda_item_number": "…",
      "agenda_item_title": "…",
      "summary": "original editorial summary",
      "revised_summary": "corrected version (same as summary when clean)",
      "poliscopic_deep_link": "https://poliscopic.com/meetings/…",
      "source_url": "https://…",
      "claims": [
        { "claim": "…", "score": "high", "source": "agenda_item_text", "notes": "" }
      ]
    }
  ],
  "claim_audit": { "total_claims": 0, "high": 0, "medium": 0, "low": 0 },
  "blocking_issues": []
}
```

Set `status: "approved"`, `approved: true` whenever the corrected content is
verifiable — even if you had to rewrite claims or drop textless items. That
is the normal path: catch, correct, verify, continue.

`status: "rejected"`, `approved: false` ONLY when something cannot be made
verifiable at all AND cannot be dropped (e.g. a broken link with no way to
reconstruct it, or a gap note that cannot be made honest). List those in
`blocking_issues`. Rejection means the run fails — use it sparingly.
