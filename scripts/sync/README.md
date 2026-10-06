# Sync Pipeline — `scripts/sync/`

The daily sync pipeline discovers, extracts, and persists meeting data from all
Maricopa County jurisdictions. It runs as a fire-and-forget cron pipeline with
a separate checker for post-sync analysis and auto-remediation.

## Files

### Pipeline stages (execution order)

| File | Stage | Purpose |
|---|---|---|
| `sync_log.sh` | **Scrape + entities** | THE daily command. Wraps `run_pipeline.py` (all jurisdictions: agendas, minutes, supporting docs) + Step 7 `detect_entities.py` with structured logging, gzips output to `data/sync/YYYY-MM-DD.log.gz`, writes a summary file. |
| `sync_launcher.sh` | **Launch (legacy)** | Former cron entry point with concurrency guards. The live cron now calls `sync_log.sh` directly — only needed if re-wiring the guard. |
| `sync_checker.sh` | **Verify (not scheduled)** | Checks sync completion and runs `sync_monitor.py` analysis. Not currently wired to any cron job. |
| `sync_monitor.py` | **Analyze** | Post-sync diagnostics: counts by status, recent failures, stuck jobs, orphans. Auto-remediation for known error patterns. Writes report to `data/sync/YYYY-MM-DD-monitor.txt`. |

### Production deploy wrapper

| File | Purpose |
|---|---|
| `sync_prod.sh` | Pre-check (verifies daily scrape succeeded), then launches `sync.sh` (project root) in background for full prod deploy + DB sync. |

### User-facing utilities

| File | Purpose |
|---|---|
| `sync_report.sh` | Read the latest monitor report. `--json` for machine-readable output. |
| `sync_summary.sh` | Table of last N days of sync summaries. `--json` for machine-readable. |
| `sync_error_report.sh` | Extract errors from a specific day's scrape log. |
| `prod_sync_alert.py` | Opens a production-data incident after 5:00 AM, sends hourly local notifications, alerts immediately on hard failure, and announces recovery. SMTP delivery is separately opt-in. |
| `../ops/verify_meeting_prod_parity.py` | Requires read-only dev/prod meeting parity before daily production success is recorded. |

## Data flow

```
cron (3 AM) → sync_log.sh --parallel
                  ↓
            run_pipeline.py (scrape: agendas, minutes, supporting docs)
                  ↓
            detect_entities.py (Step 7: 6-phase entity pipeline)
                  ↓
            data/sync/YYYY-MM-DD.log.gz + summary + entities log
```

## Usage

```bash
# THE daily command — scrape everything + run entity pipeline (what the 3 AM cron runs)
bash scripts/sync/sync_log.sh --parallel

# User utilities
scripts/sync/sync_summary.sh              # Last 7 days overview
scripts/sync/sync_summary.sh 14 --json    # Last 14 days as JSON
scripts/sync/sync_report.sh               # Today's monitor report
scripts/sync/sync_error_report.sh         # Today's errors

# Production deploy (after daily scrape completes)
scripts/sync/sync_prod.sh                 # Full: deploy code + sync data
scripts/sync/sync_prod.sh --code-only     # Code only, skip DB sync
```
