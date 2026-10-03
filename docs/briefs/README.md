# Briefs — Process & Conventions

This directory is the workspace's **task queue**. A brief is a durable,
self-contained work order: it outlives any session, so a fix doesn't depend on
a model's ephemeral context. This file documents how briefs work.

## What a brief is

A numbered markdown file in `docs/briefs/NNN-<slug>.md` that specifies:

- **Problem** — what's broken or wanted, with evidence (log excerpts, DB
  output, dates).
- **Root cause** — why it happens (not just symptoms).
- **Fix / implementation** — the concrete change, with enough specificity that
  anyone (or any fresh session) can execute it.
- **Verification** — how to prove it worked.
- **Out of scope / follow-ups** — what was deliberately not covered.

The brief is the contract. The queue lives in the file statuses, not in a model.

## Lifecycle

```
Draft        → problem captured, not yet accepted for work
Queued 🚀    → accepted, assigned (to the poliscopic agent or a sub-agent)
Implemented ✅ → code/config landed; may still need verification
Verified     → checked and confirmed working
```

Status is tracked in the `**Status:**` line at the top of each brief.

## Progress reports

Implementation progress is tracked in **`docs/briefs/progress/`** — one file per
brief in active work (`NNN.md`, plus a `README.md` explaining the convention).

- The brief is the **contract** (problem, root cause, fix, verification).
- The progress file is the **living status board**: what's done, what's
  blocked, what's next, with dates and evidence pointers.
- Update the progress file whenever brief status changes or a meaningful
  step lands (code landed, backfill ran, verification passed, blocker hit).
- A brief is not "done" until its progress file records the verification
  evidence and the brief's `Status:` line says `✅ Implemented` / `Verified`.

## Numbering

Sequential, zero-padded to three digits (`003`, `004`, …). Two briefs may
share a number only if created in the same batch; the slug disambiguates
(e.g., `003-entity-viewer-v2.md` vs `003-model-inventory.md`).

## Ownership

- **The poliscopic agent owns the queue.** It writes briefs, marks status,
  and implements the work directly. There is no queen and no specialist
  layer (framework removed 2026-08-31).

## How a brief gets resolved

1. Write the brief (`docs/briefs/NNN-<slug>.md`), status `Draft`.
2. Pete approves / asks to queue → status `🚀 Queued` with assignee.
3. Implement directly.
4. Verify the diffs yourself — compile checks, CLI checks, DB counts, a real
   test where possible. Evidence goes in the brief's Verification section.
5. Update status to `✅ Implemented` / `Verified`; leave the evidence trail.

## Related

- `docs/briefs/progress/` — implementation progress reports (living status board per brief).
- `docs/newsletter/PRODUCTION-CHECKLIST.md` — the run-time checklist the
  newsletter pipeline follows (different thing: operational checklist, not a
  task queue).
- `docs/WORKFLOWS.md` — repeatable processes index.
- `docs/briefs/007-daily-data-quality-digest.md` — the daily ops email that
  surfaces data-quality issues; new issues found there become briefs.
