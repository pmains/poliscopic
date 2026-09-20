# Scripts — Entry Points

These are the command-line entry points for the Poliscopic data pipeline.
Library modules live in `scraper/`, `db/`, `docs/`, etc. — this directory
is for things you actually invoke.

---

## Scraping

| Script | What it does |
|---|---|
| **`run_pipeline.py`** | Orchestrates a daily scrape of all jurisdictions. Called by cron. Spawns `scrape_agendas.py` workers. |
| **`scrape_agendas.py`** | Scrapes a single jurisdiction. Usage: `python scrape_agendas.py <jurisdiction> --sync` |

**Flow:**
```
run_pipeline.py  ─┬─ spawns ── scrape_agendas.py  (jurisdiction A)
                  ├─ spawns ── scrape_agendas.py  (jurisdiction B)
                  └─ spawns ── scrape_agendas.py  (… up to N workers)
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
run_pipeline.py  ──scrape done──▶ check_docs.py   (doc availability)
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
