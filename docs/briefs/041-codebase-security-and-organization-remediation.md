# 041 — Codebase security and organization remediation

**Status:** 🚧 Phases 1–4 verified; Phase 5 adapter migration and Phase 6 decomposition continue (2026-10-03)
**Owner:** poliscopic agent

## Objective

Remediate the security, reproducibility, and maintainability findings from the
2026-10-01 codebase review without interrupting daily scraping, editorial
publishing, or production synchronization.

## Constraints

- Preserve the production-operation gates and database-tier protections.
- Do not mix production data mutation with application refactoring.
- Land small, testable phases; do not rewrite the scraper in one change.
- Preserve concurrent work already present in the dirty checkout.
- Record every meaningful checkpoint in `docs/briefs/progress/041.md`.

## Findings and ordered remediation

### Phase 1 — Public web security boundaries

- Internal annotation, entity-model, and KG-quality tools must be disabled by
  default, explicitly enabled, and authenticated when enabled.
- External `next` login redirects must be rejected.
- Scraped agenda text, entity snippets, and search headlines must be escaped;
  only application-generated `<mark>` and line-break presentation may survive.
- Add focused regression tests for each boundary.

**Result (2026-10-01):** Implemented and verified. Internal tools are disabled
by default, cannot override disabled admin mode, and require login when enabled.
External login redirects are rejected. Scraped agenda text, entity snippets,
and search headlines now escape source HTML while preserving only generated
highlight markup. The focused and adjacent suite passed 71 tests.

### Phase 2 — CSRF and production session configuration

- Inventory all POST/PUT/PATCH/DELETE routes and classify browser forms versus
  authenticated machine APIs.
- Enable Flask-WTF CSRF protection by default for browser routes.
- Give deliberate APIs explicit authentication and scoped exemptions rather
  than retaining a global exemption.
- Refuse production startup with the development secret or insecure cookies.
- Convert logout to POST.

**Result (2026-10-01):** Implemented and verified. Flask-WTF now protects
browser mutations; same-origin forms and JSON requests receive tokens in the
shared application shell. The newsletter blueprint retains its independently
tested session-token protocol through an explicit exemption. Logout is POST
only. Production tier startup rejects the development secret and insecure
cookies. Focused and adjacent verification passed 134 tests.

### Phase 3 — Read-only application startup

- Move schema initialization and default-data seeding out of `create_app()` and
  into explicit deployment commands.
- Make missing database configuration fail with an actionable error.
- Keep startup checks read-only and safe under multiple workers.

**Result (2026-10-01):** Implemented and verified. `create_app()` no longer
initializes schemas, runs FTS migrations, or seeds rows. Development/test setup
is explicit through `scripts/bootstrap_app_db.py`; the command refuses the
production tier, which remains governed by OP-SCHEMA. It seeds only
non-credential reference data and does not create known-password admin users.
The combined suite passed 137 tests, and an isolated test-tier bootstrap
completed successfully.

### Phase 4 — Reproducible packaging and quality tooling

- Add `pyproject.toml` with the supported Python version and complete dependency
  groups for web, scraping, ML, and development.
- Add a lock/constraints workflow, pytest configuration, formatter/linter, and
  type-checker configuration.
- Remove runtime `sys.path` manipulation as modules become installable.

**Checkpoint (2026-10-01):** Added PEP 621 project/dependency metadata, explicit
core/editorial/ML/development groups, Python `>=3.10,<3.15`, and baseline pytest,
Ruff, and mypy settings. The compatibility `requirements.txt` now matches all
20 declared core dependencies exactly. Automatic setuptools discovery is
explicitly disabled during the flat-layout migration so packaging cannot
silently include runtime data or workspace directories. Metadata generation,
dependency health, and 137 tests pass. A cross-platform lock and installable
source layout remain open.

**Checkpoint (2026-10-02):** Added a uv cross-platform lock covering Python
3.10–3.14 and all core/editorial/ML/development dependencies (194 resolved
packages; lock SHA-256 `85b1582388459bb84b41045a59335f26c1ec806c6d00e3cf53f152ffb4c993cf`).
The project remains explicitly `package = false`, so locking does not change
the flat-tree production entry point. A frozen Python 3.12 dry-run resolved a
149-package all-extras environment without modifying `.venv`. The adjacent
security/startup regression slice passed 108 tests. `uv 0.11.16` still reports
the lock stale under `uv lock --check`, although an immediately repeated
`uv lock` leaves the file byte-identical; do not promote that check to CI until
the discrepancy is isolated.

The bounded source-layout migration will proceed by compatibility slices:

1. add installed-mode import/WSGI tests while the flat runtime remains the
   production authority;
2. introduce `src/poliscopic/` and migrate shared database/configuration code,
   retaining explicit compatibility shims for existing commands;
3. migrate the web application, templates, and static-resource lookup while
   retaining root `app.py` as the deployment shim;
4. migrate scraper and operational imports only after the Phase 5 source
   registry stabilizes;
5. remove `sys.path` manipulation only from migrated paths, leaving historical
   one-off evidence tooling untouched unless it becomes an active runtime.

**Package slice 1 (2026-10-02):** Created the `src/poliscopic/` package and
moved the authoritative database-tier resolver to `poliscopic.db.tier`.
`scripts/db/tier.py` is now a compatibility alias, so existing `db.tier`
callers and the canonical import resolve to the same module object. Setuptools
discovery is constrained to `src/poliscopic*`; `MANIFEST.in` excludes tests
from the source distribution. The focused tier/production-safety suite passed
180 tests. A wheel and sdist built successfully, the wheel contained only the
three intended package modules plus metadata, and a clean Python 3.12
environment installed that wheel and imported the tier resolver outside the
repository. Production entry points remain unchanged.

**Package slice 2 (2026-10-02):** Added `poliscopic.paths` as the explicit
application-root contract and migrated database configuration to
`poliscopic.db.config`, retaining `scripts/db/config.py` as the legacy alias.
Resolution now checks `POLISCOPIC_PROJECT_ROOT`, a verified source checkout,
then a verified working directory. An installed library outside a checkout
returns no application root and never searches arbitrary parent directories
for `.env`; explicit environment configuration remains sufficient. Pytest now
declares `src`, `scripts`, and the repository root as its compatibility import
paths. The expanded tier/config safety suite passed 185 tests, and a clean
Python 3.12 wheel installation imported config from `/tmp` with no project root
and an explicit in-memory test database.

**Package slice 3 (2026-10-02):** Moved the independent SQLAlchemy model
registry and the engine/session boundary to `poliscopic.db.models` and
`poliscopic.db.core`, in that order. Legacy `db.models` and `db.core` imports
are module aliases, preserving one `Base`, one metadata table registry, and one
engine/session state. The database-tier, configuration, production-safety,
persistence, and newsletter database suite passed 265 tests. The rebuilt wheel
was installed in a clean Python 3.12 environment outside the checkout; with an
explicit in-memory test URL it loaded all 20 ORM tables, constructed the engine,
and executed `select 1`. The larger `db` facade and query/persistence modules
remain on the legacy path pending the Phase 6 ownership split.

**Package slice 4 (2026-10-02):** Moved the dependency-leaf date helper,
meeting-attendance reconciler, vote normalization/tally helpers, and structured
P&Z detail persistence, the pure SQL read-only classifier, and pre-engine sync
target validation into `poliscopic.db`, retaining canonical module aliases for
legacy import paths. This extends installed-mode availability without pulling
migrations, scraper dispatch, or production data mutation into the wheel.
Identity gates, P&Z provenance, session, persistence, SQL safety, and target-
refusal tests cover the boundary. An offline wheel inspection confirmed the
initial modules are packaged; setuptools discovery covers adjacent canonical
modules, and generated checkout build artifacts were removed.

**Result (2026-10-03):** Phase 4 is implemented and verified. The project now
installs the canonical `src/poliscopic` package through `uv sync --frozen`;
uv 0.12.23 owns a current 193-package lock and explicitly excludes unsupported
WebAssembly targets. A frozen development sync succeeded. The rebuilt wheel was
installed outside the checkout and imported the canonical repository and all 20
ORM tables. Root application and command files remain explicit compatibility
shims rather than competing package implementations.

### Phase 5 — Canonical jurisdiction/source registry

- Replace hard-coded database IDs and duplicate display-name dictionaries with
  joins or the canonical database registry.
- Introduce one declarative source specification containing aliases, adapter,
  bodies, accepted arguments, schedule tier, and sync behavior.
- Generate CLI choices, daily/weekly schedules, and coverage checks from it.

**Checkpoint (2026-10-02):** Introduced the first typed source-registry slice
for all 40 scheduled scraper commands. It is now authoritative for execution
group/mode, date-window support, daily arguments, and weekly overrides. The
runner's compatibility group constants, coverage set, command construction,
dry-run plan, and Goodyear weekly body expansion are derived from it; the CLI
also derives recognition of every scheduled command from the same registry.
Registry/CLI regression coverage passed 97 tests, and both daily and weekly
dry-run plans cover all 40 commands. Adapter, alias, and complete body catalogs
remain to be folded into the registry before Phase 5 is complete.

**Checkpoint (2026-10-02):** Separated the scheduled identity from the actual
CLI invocation and action flags. This corrected `phoenix-aem-results`, which
now invokes the existing `phoenix-aem --sync-results` handler instead of a
nonexistent command. Glendale, Surprise CivicClerk, and weekly Goodyear body
scopes are typed catalogs rather than embedded argument strings. Import-time
validation rejects unsafe action metadata and malformed body catalogs. Alias
ownership now covers the verified `phoenix` → `phoenix-rss` compatibility name,
and CLI recognition derives from scheduled identities, invocation commands,
and aliases. Dispatch review also corrected `buckeye-granicus` to invoke the
existing `buckeye --sync` handler rather than falling into an unrelated generic
path. Adapter ownership remains open and will be added only from verified
implementation contracts.

**Adapter slice 1 (2026-10-02):** Flagstaff, Yuma, Youngtown, and Litchfield
Park now declare their standalone adapter module and callable in the registry.
The main scraper dispatches these sources through that metadata; loadability
and end-to-end dispatch contracts are tested. Remaining inline handlers will
move only when their boundaries are equally explicit.

**Adapter slice 2 (2026-10-02):** Gilbert Planning Commission now exposes a
separate fetch function and a registry-compatible `sync(args)` persistence
boundary. Its former inline database loop was removed from the main scraper;
focused dispatch contracts pass 22 tests, and both scheduler tiers retain all
40 source commands.

**Adapter slice 3 (2026-10-02):** Fountain Hills now owns its CivicClerk
configuration and full `sync(args)` persistence boundary in the jurisdiction
module. The registry dispatches it through that adapter, eliminating the
duplicate inline handler and body map from the main scraper. The combined
adapter/package slice passes 86 tests with scoped lint and compile checks.

**Adapter slice 4 (2026-10-02):** Apache Junction now owns its Legistar fetch,
item/document association, persistence, and status boundary in the
jurisdiction module. The registry dispatches it through `sync(args)` and the
inline main branch is removed. The consolidated safe slice passes 115 tests;
the daily plan continues to cover all 40 scheduled sources.

**Adapter slice 5 (2026-10-02):** Queen Creek now owns its Granicus RSS/PDF
fetch and persistence boundary in the jurisdiction module. Removing the inline
handler also removed a stale second body-code map whose keys did not match the
parser's canonical slugs, causing non-council meetings to fall back to council.
A focused regression freezes Planning and Zoning identity; the weekly plan
retains all 40 sources.

**Adapter slice 6 (2026-10-02):** Paradise Valley now owns its Granicus
meeting-metadata persistence in the jurisdiction module. Registry dispatch
replaces the inline database branch, and dead parser locals were removed. The
consolidated package/adapter safety slice passes 120 tests.

**Catalog slice (2026-10-02):** Moved the CLI's jurisdiction/body display map
into the source-registry module and made consumers receive copies. Top-level
`--list-bodies` and the Scottsdale/Gilbert choice parsers no longer maintain
parallel dictionaries. The first audit found only Flagstaff, Yuma, Youngtown,
and Litchfield Park with the stable `sync(args)` shape; Gilbert Planning and
Fountain Hills, Apache Junction, Queen Creek, and Paradise Valley now meet that
contract after bounded persistence-handler refactors. Extracting another source
requires the same explicit boundary rather than metadata-only dispatch. Both
schedule dry-runs retain complete 40-source coverage.

### Phase 6 — Session ownership and module decomposition

- Introduce transaction/session context managers for routes and services.
- Split scraper dispatch, admin services, migrations, query repositories, and
  persistence along ownership boundaries while preserving public interfaces.
- Keep production behavior behind regression and contract tests.

**Checkpoint (2026-10-02):** Added `session_scope()` and
`transaction_scope()` at the canonical packaged database boundary without
changing the existing `get_session()` contract. The scopes close on every
exit, roll back failed work, commit successful transactions exactly once, and
roll back commit failures. Theme preview and topic reads now use explicit
read-session ownership; login and unique-slug lookup use the same boundary;
notification creation is the first transaction-scoped write pilot. Focused
lifecycle, compatibility, web-security, and application checks passed 33
tests. The first packaged query repository now owns the public-body directory
query: `/bodies` supplies the scoped session, the repository fetches bodies in
one query instead of one query per jurisdiction, and route/integration tests
preserve filtering and rendering. A rebuilt wheel contains and imports the
repository outside the checkout. The body-detail route now uses that repository
as well: one caller-owned session loads the body, jurisdiction, and member
projections; the former per-member session helper and its N+1 query pattern are
no longer used. Latest-membership projection and real route rendering are
covered by tests. Article detail, archive, tag, search, and front-page reads now
use scoped sessions, with render-after-close and 404 integration coverage. The
front-page feed projects articles, tags, meetings, and trending metadata within
one owned session, and its stale duplicate body-name map has been removed. The
Flask-Login user loader also owns a scoped read. The member-vote API, legacy
member redirect, and inferred-abstention review now close their read sessions
on success and early return as well. Broader route migration remains
incremental.

**Route slice 2 (2026-10-02):** Completed read-session ownership across the
members and meetings blueprints. Member detail uses a render-safe projection;
member analytics and all meeting/case/document reads close through scoped
ownership on normal, missing-record, and early-return paths. Neither blueprint
retains direct `get_session()` or `session.close()` calls. Supporting-document
detail projects OCR/native extracted text before the session closes. Both
modules are Ruff-clean; meeting cleanup also removed identical duplicate map
keys without changing their effective values.

**Route slice 3 (2026-10-02):** Migrated bounded admin reads covering the
dashboard/editorial lists, subscribers, suggestions, notifications, image
library/API, article search, and style checks. Fourteen authenticated request
paths verify session exit across rendered pages, JSON responses, and missing
records. The admin module is now Ruff-clean; cleanup also removed a local
`url_for` shadow that could break the missing Bluesky-draft redirect before any
external post attempt. Mutation routes retain their established commit
behavior until converted in explicit transaction slices.

**Transaction slice 1 (2026-10-02):** Notification mark-read/delete and seven
bounded editorial actions now use `transaction_scope()`: delete, promote,
archive, priority, feature, reorder, and suggestion dismissal. Each operation
has one commit boundary with automatic rollback/close. Article promotion calls
the FTS synchronizer only after the transaction exits successfully. Integration
coverage verifies both persisted effects and scope exit counts.

**Transaction slice 2 (2026-10-02):** Completed admin ownership across article
forms, suggestions, tags, Skeet drafts, and image operations. Post-commit
callbacks sequence FTS after article commits. Image uploads compensate for
database failures; image deletes stage and restore files around the database
transaction. Immediate Bluesky posting uses a durable `posting` claim before
the network call and a second transaction to record the URI, leaving crashes
in a visible reconciliation state instead of silently permitting duplicate
posts. The admin blueprint has no direct session opens, commits, or closes.

**Route slice 4 (2026-10-02):** Newsletter routes now guarantee closure with
`session_scope()` while preserving the service layer's commit-owning public
contract. All newsletter behavior tests pass unchanged. No route blueprint now
calls `get_session()` or manually closes a session; the pending-subscriber
commit remains explicit until the newsletter service contract is deliberately
changed to caller-owned transactions.

**Transaction slice 3 (2026-10-02):** Newsletter service mutations now stage
and flush changes without committing. Public confirmation, subscription,
topic-management, unsubscribe, and abuse-log paths own their complete
transaction through `transaction_scope()`, eliminating the prior split commit
between rate-limit logging and subscriber updates. The newsletter suite passes
41 tests, including a service-level assertion that no mutation helper commits.

**Route ownership result (2026-10-03):** Every Flask route blueprint now uses
owned read or transaction scopes; no blueprint directly opens or manually closes
a database session. Admin, article, body, meeting, member, newsletter, theme, and
topic paths have render-after-close and mutation-boundary coverage. The remaining
Phase 6 work is structural decomposition shared with Phase 5, not hidden route
session ownership.

### Phase 7 — Repository boundaries and durable documentation

- Ignore `.env.*` while retaining `.env.example`; audit any previously exposed
  credential backups and rotate credentials when necessary.
- Separate runtime/generated artifacts from source, research, and operations.
- Reconcile which operational documentation and workflows must be versioned.
- Remove stale maps, comments, files, and contradictory README guidance.

**Checkpoint (2026-10-02):** Replaced the blanket `docs/` exclusion with a
selective boundary: private documentation remains ignored, while the brief
convention, brief 041, and its progress record are now visible to Git. The
broader documentation/versioning policy, artifact separation, stale-content
cleanup, and external credential rotation remain open.

**Runtime-source boundary (2026-10-02):** Reclassified `workflows/` from a
blanket-ignored private directory to versionable operational source. Workflow
definitions, instructions, runner/executor code, and rendering templates can
now follow code deployments; generated runs remain ignored under `data/runs/`
and repository-wide cache rules still exclude bytecode. While bringing the
runtime under quality checks, removed dead locals and made the configured
workflow run budget durable across the set-and-forget process chain. Updated
the scripts guide to name `scripts/sync/runner.py` as the canonical scheduler
instead of the retained legacy `run_pipeline.py`.

**Optional-runtime and publication cleanup (2026-10-02):** Stage 3F diagnostic
modules now defer Torch/Transformers/SetFit imports until command execution, so
pure helpers and the entire 7,861-test collection remain usable on Python 3.14
without initializing an optional ML stack. Replaced a personal absolute path
and a real host literal in tracked text with portable/test-safe placeholders;
the publication-sanitization boundary now passes. A full dirty-worktree run
provided a baseline of 7,751 passes and 30 skips. Its PostgreSQL subset passed
51/51 outside sandbox shared-memory restrictions; the remaining failures are
recorded in the progress log and belong to frozen evidence, authorization,
sync-contract, retirement-fixture, and OCR-diagnostic workstreams.
The now-versionable workflow runner also no longer embeds a personal fallback
recipient: owner-only recovery and failure alerts use
`NEWSLETTER_OWNER_EMAILS`, with a tested empty-configuration refusal.

## Verification contract

Each phase requires focused tests and a scoped diff review. Web-security phases
also require negative tests proving the old unsafe behavior is unavailable.
Application-startup tests must run only against the isolated test tier. No phase
is complete merely because code compiles.

## Out of scope

- Production database writes or deployment without a separate authorized
  production operation.
- Historical data cleanup unrelated to a remediation phase.
- Broad formatting churn across concurrently modified files.
