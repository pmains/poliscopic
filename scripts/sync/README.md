# Daily scraping and production synchronization

This directory contains the two operational lanes that move civic data:

1. the development scrape/entity pipeline; and
2. the separately guarded development-to-production upsert.

They are intentionally independent. A successful scrape does not authorize a
production write, and a scheduler process exit is not success unless the
required terminal artifacts validate.

## Development scrape lane

`runner.py` builds daily and weekly plans from
`scripts/scraper/source_registry.py`. The registry owns scheduled identities,
jurisdictions, execution groups, invocation commands, date-window support, and
body selections.

```bash
# Inspect the complete registered plan without network or database writes
PYTHONPATH=scripts uv run python scripts/sync/runner.py --dry-run
PYTHONPATH=scripts uv run python scripts/sync/runner.py --tier weekly --dry-run

# Run the ordinary daily wrapper
bash scripts/sync/sync_log.sh
```

The wrapper records dated logs, summaries, metrics, and post-scrape entity-gate
state under ignored `data/sync/` storage. `sync_completion_check.sh` validates
both levels for one date:

```bash
bash scripts/sync/sync_completion_check.sh
bash scripts/sync/sync_completion_check.sh 2026-10-07
```

Success requires a valid scrape summary and log, numeric pre/post metrics, no
surviving pipeline process, and a passing entity gate from the same dated
lineage.

The repository includes a launchd template for the scrape lane. Its checked-in
paths and schedule are examples for this workstation deployment, not portable
installation defaults; inspect them before loading on another host.

## Production upsert lane

`daily_prod_upsert.sh` is the deterministic production-data entry point. It is
designed for a scheduler rather than an interactive or model-driven turn. It
returns:

- `0` when the date was already complete or completed successfully;
- `75` when same-day scrape/entity lineage is not ready and should be retried;
- another nonzero code for a production incident.

Before writing, it requires the completion check, a fresh production preflight,
a snapshot-consistent backup that passes scratch restore, and a valid standing
authorization. The database operation is upsert-only. It never propagates
deletions or performs reconciliation, schema changes, repairs, or code
deployment.

After writing, it requires recent meeting parity and public HTTP smoke checks
before creating the immutable daily terminal receipt. Failures and late missing
terminals feed `prod_sync_alert.py`; recovery is also announced.

Read-only status:

```bash
bash scripts/sync/daily_prod_upsert_status.sh
bash scripts/sync/daily_prod_upsert_status.sh 2026-10-07
```

Manual execution changes production data and is not authorized merely by this
documentation. Follow
[`briefs/PRODUCTION-OPERATIONS-CHECKLIST.md`](../../briefs/PRODUCTION-OPERATIONS-CHECKLIST.md)
and use only the authorization bound to the intended operation, code, tables,
and date/use limits.

## Supporting commands

| Command | Purpose |
|---|---|
| `sync_summary.sh` | Summarize recent development scrape results |
| `sync_report.sh` | Show the most recent development monitor report |
| `sync_error_report.sh` | Extract errors for a scrape date |
| `sync_checker.sh` | Run read-only post-scrape diagnostics |
| `daily_prod_upsert_status.sh` | Validate the production terminal and public health |
| `maintenance_prod_upsert.sh` | Guarded maintenance wrapper; not the daily scheduler |
| `prod_sync_alert.py` | Incident, repeat, and recovery notifications |
| `entity_gate_verdict.py` | Validate the entity gate for one dated run |

## Artifacts and success semantics

Operational artifacts live under `data/` and are intentionally not committed.
Important examples include:

- `data/sync/YYYY-MM-DD-summary.txt` — development scrape summary and metrics;
- `data/sync/entity-run-YYYY-MM-DD.json` — same-day entity pipeline state;
- `data/sync/prod-upsert-YYYY-MM-DD.terminal.json` — immutable production
  success receipt;
- `data/backups/daily-production/` — rolling restore-verified production
  backups and receipts; and
- attempt-specific preflight and meeting-parity reports.

Logs are diagnostic evidence, not success by themselves. The validated terminal
receipt is the authority for a completed production upsert.

## Related entry points

- [`scripts/README.md`](../README.md) — scraper and document-ingestion commands
- [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) — component and data flow
- [`docs/DOCUMENT-EXTRACTION.md`](../../docs/DOCUMENT-EXTRACTION.md) — OCR and
  layout evidence contract
