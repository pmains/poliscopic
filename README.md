# Poliscopic

Poliscopic is a public-meeting ingestion, search, and civic-intelligence
platform for Arizona. It collects meetings, agenda items, minutes, votes, and
supporting documents from independent municipal, county, and regional
governments; extracts searchable text and layout-aware OCR evidence; and serves
the resulting record through a Flask website and an editorial/newsletter
workflow.

Tempe, Maricopa County, MAG, and the other covered governments are modeled as
peer jurisdictions. Geographic containment does not imply that a city reports
to a county.

## What is in this repository

- Multi-platform scrapers for OnBase, Legistar, Granicus, CivicClerk,
  AgendaQuick/Destiny, NovusAgenda, and jurisdiction-specific sources.
- A PostgreSQL data model for jurisdictions, public bodies, meetings, agenda
  items, documents, people, votes, cases, entities, and articles.
- A governed document pipeline that retains searchable text plus page/word/table
  evidence and uses Tesseract for scanned pages.
- A Flask application for meetings, calendars, public bodies, members, search,
  articles, topics, and newsletters.
- Daily scrape, validation, guarded development-to-production upsert, and
  editorial workflow tooling.

Historical permit tables remain in the database for compatibility, but permits
are not a current product surface or ingestion workflow.

## Repository map

```text
app.py                    Development web entry point
routes/                   Flask application factory and route blueprints
src/poliscopic/           Installable package: configuration, paths, database
                          core/models, and repository modules
scripts/scraper/          Source registry, platform adapters, jurisdiction
                          adapters, CLI, and scrape orchestration
scripts/docs/             Document acquisition, extraction, and layout evidence
scripts/entities/         Entity and event extraction
scripts/sync/             Daily scraping, verification, and production-upsert
                          orchestration
workflows/                Newsletter/editorial workflow definitions and runner
templates/, static/       Web presentation assets
tests/                    Unit, integration, safety, and operations tests
briefs/                   Durable production-operation contracts and records
docs/                     Public architecture and extraction documentation
data/                     Generated local data, logs, receipts, and backups;
                          intentionally excluded from Git
```

Canonical reusable code lives under `src/poliscopic/`. Root application files
and some modules under `scripts/` remain explicit compatibility or operational
entry points.

See [Architecture](docs/ARCHITECTURE.md) for component boundaries and data flow,
[Document Extraction](docs/DOCUMENT-EXTRACTION.md) for the OCR/layout contract,
and [Scripts](scripts/README.md) for command entry points.

## Requirements

- Python 3.10 through 3.14; Python 3.12 is the recommended baseline.
- PostgreSQL for persistent development and production data.
- [uv](https://docs.astral.sh/uv/) 0.12.x for the locked environment, or `pip`
  as a compatibility fallback for the core runtime.
- Chromium installed through Playwright for browser-backed sources.
- Poppler (`pdftotext`) and Tesseract 5 for document extraction.

On macOS, install the non-Python dependencies with:

```bash
brew install poppler tesseract
```

## Install for development

Clone the repository, then create the locked environment:

```bash
git clone git@github.com:pmains/poliscopic.git
cd poliscopic
uv sync --extra dev
uv run playwright install chromium
```

Add `--extra editorial` for optional editorial API integrations or `--extra ml`
for model experiments. The ML extra is large and is not needed for the web app,
scrapers, OCR pipeline, or ordinary tests.

If uv is unavailable, the core runtime can be installed with:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Copy the environment template and set a development database URL:

```bash
cp .env.example .env
```

At minimum, development needs:

```dotenv
POLISCOPIC_DB_TIER=development
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/poliscopic_dev
FLASK_SECRET_KEY=replace-with-a-random-local-secret
POLISCOPIC_COOKIE_SECURE=false
```

The database resolver fails closed: a development process refuses a
production-looking target, the test tier accepts only local SQLite, and the
production tier accepts only a production-classified PostgreSQL target.
Credentials belong in `.env` or the deployment secret store, never in Git.

Create or update a non-production application database explicitly:

```bash
uv run python scripts/bootstrap_app_db.py
```

Application startup does not create schemas or seed rows. The bootstrap command
refuses the production tier; production schema work follows the separately
authorized `OP-SCHEMA` procedure.

## Run the application

```bash
uv run python app.py
```

The development server listens on `http://127.0.0.1:5001` by default. Set
`FLASK_PORT` to use another port.

Important public routes include:

- `/` — articles and upcoming meetings
- `/calendar` and `/meetings` — meeting discovery
- `/meetings/<body>/<meeting_id>` — meeting and agenda detail
- `/bodies` and `/bodies/<slug>` — jurisdictions, bodies, and rosters
- `/members/<id>` — member history and voting record
- `/search` — searchable civic records

Administrative and annotation tools are disabled by default and require both
explicit configuration and an authenticated administrator.

## Scrape and inspect data

The source registry is the authority for scheduled scraper identity,
jurisdiction ownership, invocation mode, and date-window support.

```bash
# Show the complete daily plan without running sources
PYTHONPATH=scripts uv run python scripts/sync/runner.py --dry-run

# Run one registered source (example)
uv run python scripts/scrape_agendas.py peoria --sync --year=2026

# Inspect document-ingestion backlog
PYTHONPATH=scripts uv run python scripts/ingest_docs.py --status
```

Source-specific options are available with `--help`. New jurisdiction adapters
belong under `scripts/scraper/jurisdictions/`; reusable vendor behavior belongs
under `scripts/scraper/platforms/`; shared source metadata belongs in
`scripts/scraper/source_registry.py` and
`scripts/scraper/jurisdiction_registry.py`.

## Test and lint

```bash
uv run pytest
uv run ruff check src routes scripts tests workflows
```

Tests default to an isolated temporary SQLite database. Tests that exercise
external sites or machine-local operational artifacts may be skipped unless
their prerequisites are present.

## Daily operations and production safety

Development scraping and production synchronization are distinct lanes:

1. The daily scraper writes and verifies development data.
2. The production upsert lane checks same-day lineage, entity quality,
   authorization, a restore-verified backup, meeting parity, and public HTTP
   health before recording a successful terminal receipt.
3. Editorial workflows create, verify, publish, and send newsletters under
   their own authorization contract.

Production mutation is never implied by a code change or a successful scrape.
Before any production operation, read and follow
[the production operations checklist](briefs/PRODUCTION-OPERATIONS-CHECKLIST.md).
It defines the operation classes (`OP-CODE`, `OP-SCHEMA`, `OP-REPAIR`,
`OP-RECON`, `OP-RESTORE`), required approvals, backups, postconditions, and
rollback rules. Current job state belongs in terminal receipts and status tools,
not in this README.

Operational commands and status probes are documented in
[scripts/sync/README.md](scripts/sync/README.md).

## Data and local configuration

The following are deliberately not versioned:

- `.env` files and credentials
- local agent/editor/MCP configuration
- downloaded documents, database snapshots, logs, receipts, and backups under
  `data/`
- browser-test output and local editorial drafts
- uploaded production media and model checkpoints

`.env.example` is the only environment template intended for source control.
Do not add secrets, host certificates, database dumps, or generated OCR/model
artifacts to the repository.

## Contributing

Keep changes in coherent batches and include tests for scraper parsing,
database ownership, and safety boundaries. Do not infer government hierarchy
from geography. Do not add direct plain-text document writers: persisted
supporting-document text must pass through the governed extraction API so its
source and layout evidence remain traceable.
