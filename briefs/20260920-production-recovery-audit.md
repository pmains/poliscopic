# Production recovery audit — bounded execution brief

## Objective

Determine whether production lost or materially altered records during the
recent body-code merge, sync, and stabilization work. Produce evidence that is
specific enough to support a later targeted repair. This audit does not repair,
sync, deploy, scrape, or modify production or `poliscopic_dev`.

## Fixed inputs

- Before image: `data/backups/20260920T023148Z-production-op-repair.dump`
- Expected SHA-256: `d2ff2b8d9e8bafbbc2146a2e18838af1f5e0b33eed0d60b617e500d2ad5a7521`
- Windows retained copy:
  `<off-volume-retention-path>/production-repair.dump`
- Baseline: `data/audit/20260920T023148Z-g6-baseline.json`
- G6 receipt: `data/audit/20260920T023148Z-g6-receipt.json`

## Execution sequence

1. Revalidate the local baseline, receipt, dump size/hash, and retained-copy
   metadata. Refuse on any mismatch.
2. Create one uniquely named scratch database on the Windows development host.
   Restore the retained Windows file with the Windows-local PostgreSQL client.
   Do not copy or stream the archive again.
3. Capture current production in a read-only transaction.
4. Compare all public-table counts. Classify increases as expected post-snapshot
   growth, decreases as possible loss, and equal counts as inconclusive until
   identity comparison.
5. Compare primary-key populations and deterministic row hashes for the tables
   implicated by the recent work: reference tables, meetings, agenda records,
   supporting documents, case events, membership/attendance, article sources,
   and other body-coded dependents discovered from the schema. Fetch full rows
   only for snapshot-only or changed identities.
6. Run the established integrity snapshot against both databases.
7. Write immutable JSON evidence and a human-readable Markdown report. Do not
   generate or apply a repair unless the evidence identifies concrete loss.
8. Force-drop only the named scratch database and prove it is absent.

## Stop conditions

- Stop immediately on dump/receipt mismatch, non-read-only production session,
  restore error, schema incompatibility, or inability to prove scratch cleanup.
- Do not expand to older backups unless this snapshot contains a concrete gap
  that requires older history to resolve.
- Do not treat current-only records as damage; they are expected after the
  snapshot and must remain untouched.
- Do not infer lost rows from aggregate counts alone.
- No production or development write is authorized by this brief.

## Deliverables

- `data/audit/<timestamp>-production-recovery-audit.json`
- `data/audit/<timestamp>-production-recovery-audit.md`
- exact scratch name and absence proof
- per-table classifications: current-only, snapshot-only, changed, unchanged
- row-level evidence for every suspected loss, capped in the report but complete
  in JSON where practical
- explicit verdict: no observed loss, suspected loss requiring review, or audit
  incomplete/refused

## Morning-scrape constraint

This run uses one restore and one comparison pass. It does not alter scheduler,
scraper, sync, deployment, or application state. If the bounded pass cannot
complete, it stops with preserved evidence instead of retrying alternative
transport or restore strategies.

## Completed result

Completed at `2026-09-20T04:12Z` with verdict
**`NO_OBSERVED_ROW_LOSS`**.

- Evidence JSON:
  `data/audit/20260920T035800Z-production-recovery-audit.json`
- Human report:
  `data/audit/20260920T035800Z-production-recovery-audit.md`
- Evidence digest:
  `cced7e053ec5c1809d11d7831d683aa6bc42815a2b3b7dea6690934245ef4339`
- All 45 public tables were compared.
- Snapshot-only row identities: **0 in every table**.
- Current-only row identities: **0 in every table**.
- Table counts: identical in every table.
- Established integrity snapshot: identical before/current.
- Scratch database:
  `poliscopic_g6_scratch_20260920_035800`; force-dropped, with absence
  proved both by the runner and an independent administrative query.
- Production access was `SERIALIZABLE READ ONLY DEFERRABLE`.
- No repair, sync, deploy, scrape, restart, scheduler action, production write,
  or `poliscopic_dev` write occurred.

The per-row content hash comparison is retained as diagnostic evidence, but it
is not used to claim content changes: PostgreSQL JSON text rendering can differ
between the production and Windows scratch sessions (notably session-dependent
types). The loss verdict rests on complete primary-key/multiset populations,
exact table counts, and the integrity snapshot. No recovery plan is warranted
from this snapshot because it contains no row identity absent from current
production.
