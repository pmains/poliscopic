# Scripts — Entry Points

These are the command-line entry points for the Poliscopic data pipeline.
Library modules live in `scraper/`, `db/`, `docs/`, etc. — this directory
is for things you actually invoke.

---

## Scraping

| Script | What it does |
|---|---|
| **`sync/runner.py`** | Canonical daily/weekly scheduler. Builds its 40-source plan from `scraper/source_registry.py`, runs resource-aware batches, and records state under `data/sync/`. |
| **`scrape_agendas.py`** | Compatibility entry point for one source. Usage: `python scripts/scrape_agendas.py <source> --sync`. |
| **`run_pipeline.py`** | Legacy scheduler retained for compatibility; new operations should use `sync/runner.py`. |

**Flow:**
```
sync/runner.py  ─┬─ invokes ── scraper/main.py  (serial browser sources)
                ├─ invokes ── scraper/main.py  (parallel HTTP sources)
                └─ records ── data/sync/state.json
```

---

## Post-scrape checks

| Script | What it does |
|---|---|
| **`check_docs.py`** | Checks supporting-document availability after scrape. Flags missing/rotten URLs. |
| **`check_minutes.py`** | Discovers newly posted minutes PDF URLs for completed meetings. |

Both run independently of the pipeline — can be called after a scrape
or on a separate schedule.

```
sync/runner.py  ──scrape done──▶ check_docs.py   (doc availability)
                                check_minutes.py (minutes URL discovery)
```

---

## Document ingestion

| Script | What it does |
|---|---|
| **`ingest_docs.py`** | Downloads supporting-document PDFs and runs the governed layout cascade (native word boxes → Poppler fallback → Tesseract TSV). Retains plain text plus immutable layout evidence. Runs concurrently and includes URL, size, and PDF safety checks. |

```
ingest_docs.py  ──download──▶  safety check  ──extract──▶  DB write
                               ├─ safe      → extract text
                               ├─ quarantine→ skip, keep file for review
                               └─ reject    → remove, log reason
```

The extraction contract is documented in
[`docs/DOCUMENT-EXTRACTION.md`](../docs/DOCUMENT-EXTRACTION.md). Do not add a
new direct plain-text OCR or `pdftotext` writer for stored supporting documents;
route it through `scripts/docs/extract.py` so page geometry and lineage survive.

---

---

## Other

| Script | What it does |
|---|---|
| **`email_article.py`** | Sends article emails via SMTP. |
| **`social.py`** | Social media posting. |
| **`task_utils.sh`** | Shared cron runner with PID tracking and log management. |
