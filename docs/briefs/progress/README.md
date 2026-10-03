# Brief Implementation Progress

This folder is the **living status board** for brief implementation. The
brief itself (`docs/briefs/NNN-<slug>.md`) is the durable contract — problem,
root cause, fix, verification. The progress file here is the running record
of how implementation is going: what's done, what's blocked, what's next.

## Convention

- One file per brief in active work: `docs/briefs/progress/NNN.md`
  (number matches the brief).
- Update the file whenever brief status changes or a meaningful step lands
  (code landed, backfill ran, verification passed, blocker hit).
- Keep entries short and factual — a dated log of state changes, not prose.
- The brief's `Status:` line stays the source of truth for lifecycle
  (`Draft` → `🚀 Queued` → `✅ Implemented` → `Verified`); the progress file
  explains *how* we got there and what remains.

## Required sections

Each progress file carries these sections (omit a section only if it has
nothing to say):

- **Status** — current lifecycle state + date
- **Done** — what's landed, with evidence pointers (logs, DB state, diffs)
- **Blocked** — anything stalled and why
- **Next step** — the immediate action, so any fresh session can pick up
- **Follow-ups** — smaller issues found along the way that don't block this
  brief

## Current queue

| Brief | Title | Status |
|---|---|---|
| 014 | P&Z / BOA supporting-doc varchar fix | ✅ Implemented |
| 015 | Entity taxonomy + PART_OF layer | ✅ Step 1 + Step 2 done; Step 3 deferred |
| 016 | Entity pipeline refactor | ✅ Steps 1–2 verified; Steps 3–4 pending |
| 017 | Knowledge Graph Stage 0 integrity | Draft — awaiting approval |
| 041 | Codebase security and organization remediation | 🚧 Phases 1–4 verified; adapter/module decomposition continues |
