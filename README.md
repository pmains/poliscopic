# Poliscopic — Arizona Public Governance & Development Intelligence

Extract, organize, and persist public governance materials and development
permit data from Arizona jurisdictions.

> ## ⚠ Before ANY production operation
>
> **Read and complete [`briefs/PRODUCTION-OPERATIONS-CHECKLIST.md`](briefs/PRODUCTION-OPERATIONS-CHECKLIST.md).**
>
> That file is the **single authority** for production operations. It classifies the
> operation, defines the ordered gates (scheduler state and production hold → source
> commit → manifest digest → tests → read-only preflight → protected backup →
> digest-bound plan → human authorization → one bounded execution → terminal receipt
> → postconditions and HTTP checks → rollback decision → scheduler re-enable), and
> states the STOP condition for each.
>
> **Only one production mutation kind per authorization.** There are six operation
> kinds (`OP-DEV`, `OP-CODE`, `OP-SCHEMA`, `OP-REPAIR`, `OP-RECON`, `OP-RESTORE`) and
> no checklist run or authorization may cover more than one of them.
>
> **Current posture: production writing is PAUSED and the release is BLOCKED.**
> `maricopa-prod-sync` and `maricopa-sync-checker` are disabled; the 3 AM
> `maricopa-daily-sync` run is development-only and remains enabled. The OpenClaw
> scheduler is the source of truth for job state, and a code change must never
> re-enable a disabled job.
>
> Note: `.gitignore` ignores the `docs/` and `data/` trees. Anything under those
> paths is local and **non-authoritative**; the tracked `briefs/` copy governs.

## What it does

Poliscopic ingests and displays two categories of public data:

### Meeting & Agenda Tracking

- BOS, P&Z, Board of Adjustment, Board of Health, Drainage Review Board,
  Transportation Advisory Board, Industrial Development Authority, Tempe City
  Council, and other Tempe boards
- Agenda item and supporting document extraction
- Vote tracking and case-number cross-referencing

### Development Permit Analysis

Structured permit data from **four jurisdictions** with cross-jurisdiction
category and work-type normalization:

| Jurisdiction | Records | Source | Coverage |
|---|---|---|---|
| **City of Phoenix** | ~728,000 | PDD CSV Export | 2004–present |
| **Maricopa County** | ~150,000 | Weekly XLSX reports | 2012–present |
| **City of Tempe** | ~19,000 | ArcGIS FeatureServer | 2019–present |
| **City of Chandler** | ~1,700 | DSActiveProjects | 2017–2025 |

Phoenix provides the richest dataset with valuation, zoning, parcel numbers,
contractor, owner, and completion dates. Data is normalized into a shared
category model (Residential, Commercial, Industrial, Mixed-Use, Other) and
work type model (New Construction, Addition, Alteration, Trade, Demolition,
Infrastructure, Unknown).

### Web App

Bootstrap 5 Flask UI with:
- Permit overview with summary charts and category breakdowns
- Year, jurisdiction, category, and work-type filter panel
- Summary and raw-permit views with pagination
- Meeting browser with sync status badges and detail pages
- Jurisdiction-aware member rosters and voting records

## Requirements

- Python 3.9+
- Playwright (with Chromium browser) — for browser-backed scraping
- PyMuPDF and pdfplumber — native text, word geometry, and table extraction
- `pdftotext` (poppler-utils) — layout-preserving PDF fallback
- Tesseract 5 — local TSV OCR for image-only pages
- SQLAlchemy
- Flask (with Flask-Caching for route-level caching)
- openpyxl (for XLSX permit report parsing)
- xlrd (for legacy XLS permit report parsing)

Install:

```bash
pip install -r requirements.txt
playwright install chromium
brew install poppler        # macOS — provides pdftotext
brew install tesseract      # macOS — OCR with word boxes/confidence
```

### Dependencies

Core:
```
flask
flask-caching
sqlalchemy
playwright
openpyxl
xlrd
```

Document extraction uses a governed native → Poppler → Tesseract cascade and
stores coordinate-aware layout artifacts alongside retained text. See
[Document Extraction and Layout Evidence](docs/DOCUMENT-EXTRACTION.md).

## Usage

### Web App

```bash
python app.py
# Opens at http://127.0.0.1:5001/meetings
```

Browse meetings and public bodies with filters, supporting documents, voting
records, member information, and pagination.

### Database

```bash
# Initialize/migrate the database
python scripts/agenda_scraper.py --init-db
```

## Board of Supervisors (BOS)

Commands default to BOS when no subcommand is given. The `bos` subcommand is
optional for BOS operations.

### Sync a single meeting

```bash
python scripts/agenda_scraper.py --sync --meeting-id=4449
```

Or with explicit subcommand:

```bash
python scripts/agenda_scraper.py bos --sync --meeting-id=4449
```

### Sync a date range

```bash
# Discover and sync all meetings between two dates
python scripts/agenda_scraper.py bos --sync --start-date=2025-01-01 --end-date=2025-01-31

# Resume previously failed/partial/pending meetings (no date range needed)
python scripts/agenda_scraper.py bos --sync --retry-failed
```

### Sync resumable flags

| Flag | Description |
|---|---|
| `--retry-failed` | Only process meetings with `failed`, `partial`, or `pending` status |
| `--force` | Re-sync everything, including already-complete meetings |
| `--retry-count N` | Max retry attempts per meeting (default 3) |
| `--skip-complete` | Skip complete meetings when using `--meeting-id` |
| `--include-manual-review` | Include `manual_review` meetings in retry operations |

### Status & inspection

```bash
# Summary of sync status across all meetings
python scripts/agenda_scraper.py --status

# List failed/partial meetings
python scripts/agenda_scraper.py --failed

# List meetings needing manual review (image-based agendas)
python scripts/agenda_scraper.py --failed --include-manual-review
```

### Vote syncing

```bash
# Extract roll-call votes from a meeting's summary page
python scripts/agenda_scraper.py bos --sync-votes --meeting-id=4449
```

## Planning & Zoning (PZ)

Use the `pz` subcommand. All date flags use YYYY-MM-DD format.

```bash
# Sync P&Z meetings by date range
python scripts/agenda_scraper.py pz --sync --start-date=2026-01-01 --end-date=2026-05-01

# Sync a single P&Z meeting by ID
python scripts/agenda_scraper.py pz --sync --meeting-id=3734

# Limit the number of meetings from a date range search
python scripts/agenda_scraper.py pz --sync --start-date=2026-01-01 --limit=5
```

When no start/end date is given, PZ defaults to the last 90 days.

### How P&Z sync works

1. **Search** — queries the AgendaCenter search page for PZ meetings
2. **Overview page** — visits the meeting's document-index page
3. **Agenda PDF** — identifies the actual agenda document (not staff reports)
4. **PDF parsing** — downloads the agenda PDF, extracts real agenda items
   (numbered items with case numbers, project names, applicants, etc.)
5. **Staff reports** — staff-report documents from the overview page are linked
   to agenda items by case number as supporting documents

The overview page (document index) is **not** treated as the agenda. ZIPPOR
meetings (Zoning Infrastructure Policy Procedure Ordinance Review) are
supported but use a different PDF format with slightly different item
structure.

## Body-Scoped Identity

All bodies (BOS, PZ, ADJ, DRAIN, Health, TAB, IDA, Tempe bodies) use separate
`body` namespaces so meeting IDs never collide. This allows one-click
cross-referencing: when a PZ case number appears on a BOS agenda item (or vice
versa), both meetings can be found without ID prefix hacks.

Body-scoped filtering is available in the web UI.

## Routes

| Path | Description |
|---|---|
| `/` | Homepage — navigate to meetings and public bodies |
| `/meetings` | Meeting list with search, filter, and pagination |
| `/meetings/<body>/<meeting_id>` | Meeting detail — agenda items, documents, votes |
| `/bodies` | Public bodies index — all jurisdictions and their bodies |
| `/bodies/<slug>` | Body detail — paginated member roster |
| `/members` | Unified member index (redirects to /bodies) |
| `/members/<id>` | Individual member profile and voting record |
| `/members/<slug>/analytics` | Voting analytics for a member |
| `/c-number/<c_number_base>` | Case number revision history |

## Data Model

See `scripts/db.py` for the full SQLAlchemy model definitions.

Key entities:
- **Jurisdiction** — A county, city, or town (e.g., Maricopa County, City of Phoenix)
- **PublicBody** — A board, commission, or committee within a jurisdiction
- **PublicBodyMember** — A person who serves or served on a public body (title, district/seat, date range)
- **Meeting** — A meeting of a public body with agendas, documents, and voting records

## Database Schema (Legacy)

- **meetings** — sync status, item counts, retry tracking, body scope
- **agenda_items** — individual agenda items with C-numbers and case numbers
- **supporting_documents** — attachment documents linked to items
- **meeting_supervisors** — supervisor attendance per meeting
- **agenda_item_votes** — roll-call vote results per item
- **supervisor_votes** — individual supervisor votes
- **pz_item_details** — structured P&Z metadata (case number, district, project
  name, applicant, request, location, recommendation)
- **cases** — case numbers tracked across meetings
- **case_events** — event history per case (agenda appearance, hearings, votes)
- **jurisdictions** — Government jurisdictions (counties, cities, towns)
- **public_bodies** — Boards, commissions, committees within a jurisdiction
- **permits**, **permit_reports** — retained historical tables from the retired
  permit feature; no current route, scraper, or sync workflow writes them
- **public_body_members** — Membership roster for public bodies
- **meeting_attendance** — Per-meeting attendance records
- **member_votes** — Generalized vote records for non-BOS bodies
- **executive_session_participants** — BOS executive session advisors

The `sync_status` field tracks:
- `complete` — successfully extracted and persisted
- `partial` — items extracted but supporting docs failed
- `failed` — network/parse error, worth retrying
- `manual_review` — page loaded but image-based/unparseable format
- `pending` — discovered but not yet synced

## Project Structure

```
scripts/
  scraper/
    agenda_scraper.py      Main agenda/meeting scraper CLI
    ...
  db.py                    Persistence layer (SQLAlchemy models)
app.py                     Flask web application
templates/
  base.html                Base template
  meetings.html            Meeting list with pagination
  meeting_detail.html      Meeting detail with items/docs/votes
  ...
data/
  maricopa.sqlite          SQLite database
  phoenix/                 Phoenix discovery artifacts (ArcGIS metadata, samples)
```

## Tests

```bash
# Run the full test suite
python -m unittest discover -s tests
# or
python -m pytest tests/
```

## Performance Optimizations

### SQLite PRAGMAs

The following PRAGMAs are applied automatically on every connection:

| PRAGMA | Value | Effect |
|---|---|---|
| `journal_mode` | WAL | Concurrent reads + writes without lock contention |
| `synchronous` | NORMAL | Reduces fsync calls without risking corruption |
| `temp_store` | MEMORY | Temp tables/indices live in RAM |
| `cache_size` | -20000 | 20 MB page cache |
| `foreign_keys` | ON | Enforce referential integrity |

### Server-Side Caching

Flask-Caching caches the following routes:

| Route | Cache TTL | Notes |
|---|---|---|
| `/meetings` | 60s | Varies by query string (body, type, date, page) |
| `/meetings/<id>` | 120s | Per-meeting detail |
| `/members` | 120s | Member directory |

The cache directory is `.cache/flask-cache/` and is auto-created.

### Request Timing

Every request over 1 second is logged as a warning with the elapsed time:
Slow requests include their path and elapsed time in the warning log.

### Benchmarking

```bash
# Requires the Flask app to be running on :5001
python scripts/benchmark.py
```
