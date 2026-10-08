# Poliscopic architecture

This document describes stable component boundaries and data flow. It avoids
machine names, row counts, schedules, and current incident state because those
change independently of the software.

## Domain model

Poliscopic treats every government source as belonging to a jurisdiction. A
jurisdiction is an independent public authority, not a geographic containment
tree:

- municipal governments, such as Tempe, Goodyear, or Youngtown;
- county government, such as Maricopa County;
- regional authorities, such as MAG or Valley Metro; and
- other state-created public bodies represented by their own authority.

A `PublicBody` belongs to one jurisdiction. Meetings belong to public bodies;
agenda items, documents, attendance, votes, and cases belong to meetings or
items. This prevents source identifiers from colliding and prevents a city from
being modeled as subordinate to a county merely because it lies within the
county geographically.

## System flow

```text
government sources
        |
        v
source registry -> platform/jurisdiction adapters -> development PostgreSQL
                                                   |
                    +------------------------------+------------------+
                    |                              |                  |
                    v                              v                  v
             document ingest               entity/event work   Flask dev app
          native text / OCR / layout             flows          and QA tools
                    |                              |
                    +------------------------------+
                                                   |
                                                   v
                                  guarded, upsert-only production sync
                                                   |
                                                   v
                                      production PostgreSQL + web app

development data -> editorial workflows -> articles/newsletters -> web/email
```

The ordinary data path is intentionally two-lane:

1. Scrapers and extraction write to development.
2. A separately authorized production operation validates lineage, quality,
   backups, parity, and public health before upserting approved tables.

No scraper writes directly to production. Code deployment, schema change,
repair/delete work, data reconciliation, and restore are distinct production
operation classes.

## Source ingestion

### Registries

`scripts/scraper/jurisdiction_registry.py` defines government authorities and
their kinds. `scripts/scraper/source_registry.py` defines scheduled sources,
their jurisdiction, invocation identity, execution mode, date-window support,
and body selections. Scheduler coverage and CLI dispatch derive from that
metadata.

### Adapters

Scraper code is organized by responsibility:

```text
scripts/scraper/common/          source-independent parsing and lifecycle tools
scripts/scraper/platforms/       reusable vendor/platform behavior
scripts/scraper/jurisdictions/   government-specific adapters and policy
scripts/scraper/county/          Maricopa County body adapters
scripts/scraper/main.py          compatibility dispatcher/orchestration
scripts/scraper/cli.py           command parsing
```

New code should prefer these boundaries. A platform module should not decide
government hierarchy or production policy. A jurisdiction module should reuse
platform behavior rather than fork it. Every scheduled source must be registered
and validated before it can enter the daily plan.

### Persistence

Scrapers persist normalized records through the database layer. Source identity
and public-body identity are explicit, and upserts avoid duplicating stable
government records. Failed, partial, pending, complete, and manual-review states
remain visible rather than being treated as equivalent success.

## Document extraction

Supporting documents are acquired separately from meeting discovery. Persisted
document text must flow through the governed extractor:

```text
source URL and lineage
        -> bounded download and type/size validation
        -> native PDF words/tables when usable
        -> Poppler layout fallback when needed
        -> rendered-page Tesseract TSV for scanned material
        -> retained search text + immutable layout evidence
```

Layout artifacts retain hashes, page accounting, word boxes, confidence, table
cells, method, and tool versions under ignored runtime storage. Downstream entity
and event extraction can therefore point back to page geometry rather than an
unverifiable character window. See
[Document Extraction](DOCUMENT-EXTRACTION.md) for the governing contract.

## Database boundary

The canonical database implementation is under `src/poliscopic/db/`:

- `tier.py` classifies and validates database targets;
- `config.py` resolves the declared tier and URL;
- `core.py` owns engines, sessions, and scoped transaction helpers;
- `models.py` owns the SQLAlchemy registry and ORM models; and
- `repositories/` contains query boundaries that return render-safe data.

Modules under `scripts/db/` are compatibility aliases or legacy callers during
the source-layout migration. They must not create a second ORM registry or an
independent database-tier policy.

The three database tiers are:

| Tier | Intended target | Rule |
|---|---|---|
| `development` | PostgreSQL database classified as development | Refuses production-like targets |
| `test` | Temporary/local SQLite | Refuses shared databases |
| `production` | PostgreSQL database classified as production | Must be declared explicitly |

The web application does not initialize or migrate a schema during import.
Development/test bootstrap is explicit; production schema work uses an approved
schema operation.

## Web application

`routes.create_app()` is the Flask application factory. Public blueprints cover
meetings, public bodies, members, articles, themes, topics, entities, podcast,
and newsletter behavior. Administrative and annotation blueprints are
configuration-gated and require authentication when enabled.

Routes should own a bounded database session and project ORM objects into data
that remains safe to render after the session closes. Scraped text is untrusted
input and must be escaped before any trusted highlighting markup is introduced.
Browser mutations use CSRF protection; machine/custom-token endpoints are
classified explicitly rather than globally exempted by accident.

## Editorial and newsletter system

Article routes and the newsletter service manage publication, subscriptions,
confirmation, delivery, and image selection. Production editorial writes use a
guarded operation with their own authorization and terminal state. A successful
email send is not proof that an article was published, and a successful publish
is not proof that a data scrape completed.

## Scheduling and production synchronization

`scripts/sync/runner.py` constructs the registered scrape plan. Shell wrappers
under `scripts/sync/` run the daily scrape, record metrics, perform completion
checks, and expose operator status.

The production upsert lane is designed to fail closed and loudly. Its normal
gates include:

- a successful same-day scrape/entity lineage;
- an approved, code-bound production authorization;
- a fresh production preflight;
- a snapshot-consistent backup verified by scratch restore;
- the entity-quality gate;
- upsert-only synchronization of the authorized table set;
- development/production meeting parity;
- public HTTP health checks; and
- an immutable successful terminal receipt.

Failures create or update an incident; success after an incident emits recovery.
Current scheduler state and most recent results are runtime facts and belong in
status commands and receipts, not source documentation. See
[Sync operations](../scripts/sync/README.md) and the
[production checklist](../briefs/PRODUCTION-OPERATIONS-CHECKLIST.md).

## Repository boundaries

Source control contains code, tests, public documentation, workflow definitions,
and production-operation contracts. It excludes credentials; local IDE, agent,
and MCP configuration; downloads; database snapshots; backups; logs; generated
receipts; OCR artifacts; browser-test reports; uploads; and model checkpoints.

The root `.gitignore` is the authority for that boundary. Do not use ignored
runtime artifacts as required installation inputs. If a command needs private
configuration, document the variable or external prerequisite without checking
in its value.
