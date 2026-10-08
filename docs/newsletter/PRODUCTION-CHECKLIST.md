# Newsletter Production Checklist

**Every newsletter run starts here.** Read this file first, follow the steps in
order, and do not mark a step done until its exit criteria are met. This file is
the contract between Pete and the pipeline: output below this bar is a failed run.

Companion docs:
- Quality bar: `docs/newsletter/HOUSING-EDITORIAL-BRIEF.md`
- Runner architecture: `docs/newsletter/NEWSLETTER-WORKFLOW.md`
- Briefs: `docs/briefs/005-newsletter-production.md`, `docs/briefs/006-phoenix-stale-complete.md`

---

## Step 0 — Preflight

- [ ] Working directory is `/Users/pmains/Code/openclaw/poliscopic`
- [ ] DB reachable (`scripts/db/config.py` loads `.env` → `DATABASE_URL`)
- [ ] Run directory created: `data/runs/<topic>/<run-id>/` with `staged/`,
      `enriched/`, `summarized/`, `verified/`, `send-output/` subfolders
- [ ] No previous run for this topic is still marked `sending` (idempotency check)

## Step 1 — Prepare (query the DB)

- [ ] Window: `meeting_date BETWEEN today-7 AND today+13` — the default
      window covers BOTH template sections: **This Past Week** (today-7..today)
      and **Coming Up** (today..today+13). Override only for retrospective
      editions via `--window-start/--window-end/--days-back` on the runner.
- [ ] Pull from `meetings` + `agenda_items` (join via `meeting_db_id` /
      `body`+`meeting_id`, never bare `meeting_id`)
- [ ] Output: `staged/` — one JSON entry per agenda item with body, meeting_id,
      meeting_date, meeting_type, agenda_item_number, title, text, source_url
- [ ] **Data-gap capture:** for each body with a meeting in the window but 0
      extracted items (e.g., Phoenix Formal 8/26 & 9/9, Brief 006), write a
      gap entry with body, date, meeting_id, and `gap_reason` — do not silently
      drop them

## Step 2 — Classify (housing relevance, wide net)

- [ ] Read `workflows/housing/01-classify/instructions.md` + editorial brief
- [ ] Include: zoning/rezoning, GP amendments, PAD, subdivisions/plats, ADUs,
      density, affordable/multifamily/mixed-use, annexations, entitlements,
      use permits & variances on residential, housing funding (HOME/CDBG/trust
      fund), appointments to housing/planning commissions
- [ ] Exclude: routine permits, passing mentions, purely procedural items
- [ ] Output: `staged/` — classified subset with a one-line `relevance_note`

## Step 3 — Enrich (links + specifics)

- [ ] **Every item carries TWO links:**
  - poliscopic: `https://poliscopic.com/meetings/{body}/{meeting_id}#item-{agenda_item_number}`
    (verify `body` slug against DB `meetings.body`)
  - source: the original `source_url` from the meeting/agenda item
- [ ] Specifics, never generalities: acres, addresses, council districts,
      applicant/owner names, staff & P&Z recommendations (exactly as stated)
- [ ] YIMBY lens note per item: supply-positive, supply-negative/watch, or neutral
- [ ] Output: `enriched/`

## Step 4 — Editorial (newsletter format)

- [ ] Subject: fresh headline from the week's top story + date range
      (`"<headline> — <date range>"` — actor + action, active voice,
      ~6–12 words, ≤ ~80 chars)
- [ ] §1 The overview: 2–4 short paragraphs separated by blank lines
      (two newlines, \n\n, in the JSON text string), each 1–3 sentences of
      plain news prose (~5th-grade reading level) — never one unbroken wall
      of text; synthesizing ALL item summaries — lead with the top-ranked
      item (relevance high → medium); every item mentioned is hyperlinked
      inline on the natural phrase that names it (markdown
      `[anchor](poliscopic_deep_link)`, URL copied verbatim from the item —
      no "(bos item 44)" machine tokens in reader copy)
- [ ] §1 link hygiene: every URL in the overview exactly equals an item's
      poliscopic_deep_link (verify audits this; renderer converts links
      deterministically and allowlists to this run's items)
- [ ] §2 Also on the calendar: remaining items, 1–2 sentences each, grouped by
      body/date, meeting name + date in bold
- [ ] §3 data gaps & notes: **NOT included in the subscriber newsletter.**
      Data gaps are admin content and go ONLY to the daily ops digest
      (`scripts/sync/sync_digest.py`, Brief 007). Never render a "Data gaps"
      section in newsletter copy. The pipeline still tracks gaps internally
      (deterministic capture, verify audit) but they are not subscriber-facing.
- [ ] No invented facts: no vote counts / unit counts unless the source shows them
- [ ] Plain language (~5th-grade reading level) across subject + overview +
      item summaries: short sentences, everyday words, active voice,
      acronyms/jargon expanded on first use
- [ ] Output: `summarized/`

## Step 5 — Verify (hard gate, correct-and-continue)

- [ ] Every item has a working poliscopic deep link AND source link
- [ ] Every claim traces to the source text (Berry audit on the claims)
- [ ] No vote counts, unit counts, or recommendations beyond the source
- [ ] Data gaps are honestly stated — no implied coverage
- [ ] **Catch → correct → verify → continue.** When a claim is unsupported,
      the verify step REWRITES it to match the source, re-verifies the
      corrected text, and the run continues to send the corrected copy.
      Textless items are moved to §3 gaps, not written up. The run only
      fails when something genuinely cannot be made verifiable (blocking).
      Internal diagnostics in §3 are blocking — subscriber-facing only.
- [ ] Output: `verified/`

## Step 6 — Render

- [ ] Use `workflows/templates/render.py` (the ONLY formatter)
- [ ] HTML output maps §1/§2/§3 onto the template structure
- [ ] Output: `send-output/newsletter.html`

## Step 7 — Send

- [ ] SMTP: `mail.privateemail.com:587`, STARTTLS, from `contact@poliscopic.com`
      using `EMAIL_APP_PASSWORD` from `.env`
- [ ] Idempotency: check `data/runs/<topic>/sent.idempotency` (per-topic, keyed by run-id) — never send twice for the same run-id
- [ ] On success, write the idempotency marker + run status = sent

## Step 8 — Publish article (1 newsletter = 1 article)

- [ ] Deterministic step (no LLM): `workflows/shared/06-publish/instructions.md`
- [ ] ONE article per newsletter run — never one article per item
      (Pete directive 2026-08-27)
- [ ] Article: subject as title, §1 overview as the lede, §2 items grouped
      by meeting, topic + jurisdiction tags, per-item ArticleSource rows
- [ ] Created in dev via `scripts/publish_newsletter_article.py` (idempotent
      by slug), then editorial tables pushed to prod via
      `scripts/editorial_sync.py`
- [ ] If the run has no approved items → publish nothing (no padding)
- [ ] Output: `publish-output/publish-result.json`

---

## Definition of done

- All boxes above checked
- `verified/` passes the hard gate
- One email sent, idempotency recorded
- Run log available at `data/runs/<topic>/<run-id>/launcher.log`

Anything less is a failed run — report it, don't ship it.
