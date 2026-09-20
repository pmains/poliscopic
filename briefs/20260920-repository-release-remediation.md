# Repository release remediation checklist

**Branch:** `codex/repository-stabilization-20260918`
**Starting commit:** `09eb7a2`
**Base:** `main` (direct ancestor; branch is 50 commits ahead before final integration)
**Initial Git state:** clean
**Release state:** STABILIZED — integrated into `main`; production remains separately paused

## Objective

Turn the clean but unintegrated stabilization branch into a reviewed, testable
release candidate that can be safely fast-forwarded into `main`. Git cleanliness
alone is not release readiness.

## Non-goals and boundaries

- Do not delete ignored operational data, backups, credentials, uploads, or
  audit evidence to make the repository appear cleaner.
- Never run `git clean -fdX` in this workspace.
- Do not deploy, sync, scrape, restart services, alter schedulers, or write to
  production as part of repository remediation.
- Do not rewrite history or squash the recovery trail merely to shorten it.
- Preserve the intentional `contracts/` retirement unless review finds a live
  dependency.
- Every batch ends with tests, `git diff --check`, a clean worktree, and an
  update to this checklist.

## Phase 1 — establish a trustworthy baseline

- [x] Confirm no tracked, staged, or ordinary untracked changes at `09eb7a2`.
- [x] Confirm `main` is a direct ancestor: `main...HEAD = 0 behind / 43 ahead`.
- [x] Record scope: 543 changed paths (457 added, 74 modified, 12 deleted).
- [x] Run the complete suite once from the clean starting commit using
  `--continue-on-collection-errors`.
- [x] Persist the exact command, log, elapsed time, totals, and failing node IDs.
- [x] Classify every error/failure into:
  1. collection/import defect,
  2. test infrastructure/fixture defect,
  3. genuine product regression,
  4. obsolete contract/test,
  5. external or environment dependency.

**Exit:** every non-pass has an owner, category, and reproducible command.

## Phase 2 — restore test collection and infrastructure

- [x] Fix collection/import defects first; do not mask them with skips.
- [x] Fix the repository-owned import/fixture path defects that prevented the
  Tempe pipeline tests from reaching their assertions.
- [x] Give legitimate external dependencies explicit availability checks and
  truthful skips; never convert an application failure into a skip.
- [x] Rerun the affected file after each bounded fix.
- [x] Rerun full collection and record the new denominator.

**Exit:** zero unexplained collection errors; test infrastructure reaches the
code it claims to test.

## Phase 3 — remediate assertion failures

- [x] Group failures by common root cause rather than editing tests one by one.
- [x] Fix product code when behavior violates a current contract.
- [x] Update a test only when a tracked current contract proves it obsolete.
- [x] Add a regression test for each corrected product defect.
- [x] Keep production interlock and protected-target safeguards fail-closed.

**Exit:** full suite passes, or every remaining non-pass is an explicitly
approved external-dependency skip with no hidden collection failure.

## Phase 4 — audit the 43-commit release candidate

- [x] Confirm the 12 `contracts/` deletions have no remaining imports or runtime
  consumers.
- [x] Review scraper, ingest, sync, deployment, and production-interlock entry
  points against their operational contracts.
- [x] Review application/newsletter changes independently from KG tooling.
- [x] Review schema and data-migration code for target and transaction guards.
- [x] Build the release manifest from the reviewed tree.
- [x] Prove exclusions: `.env`, `data/`, backups, audit evidence, tests, docs,
  local uploads, caches, and non-allowlisted KG tooling.
- [x] Import the application and run the focused operational suite.

**Exit:** exact release contents and operational behavior are reviewable and all
release gates are green.

## Phase 5 — integrate

- [x] Record final full-suite and release-manifest evidence.
- [x] Update the stabilization and production-operation records.
- [x] Fast-forward `main` to the reviewed commit (no ambient-worktree deploy).
- [x] Tag the stabilization/release point.
- [x] Keep production authorization and deployment as separate operations under
  `briefs/PRODUCTION-OPERATIONS-CHECKLIST.md`.

**Exit:** `main` contains the reviewed release candidate, the worktree is clean,
and deployment remains independently controlled.

## Batch log

### Batch 0 — checklist and baseline launch

- Checklist created from clean commit `09eb7a2`.
- No source, data, scheduler, service, or database mutation.
- Full-suite command:
  `.venv/bin/python -u -m pytest tests/ -q --continue-on-collection-errors`
- Log: `data/repository-remediation-full-suite-20260920-0030.log`
- Result: **131 failed, 3,970 passed, 27 skipped, 17 errors, 8 subtests
  passed in 84.69 seconds**.
- Exact failed/error node IDs are present in the terminal summary in that log.
- Classification remains consistent with the earlier audit: 94 stale Stage-2
  evidence bindings; repository API/test drift; test fixture/model drift; and
  one Python 3.14/Torch import-time compatibility error. No new failure/error
  population was introduced by the recovery-audit commit.

### Batch 1 — scraper test collection and lazy ML dependency

- Six county scraper test modules loaded the retired
  `scripts/agenda_scraper.py` monolith. They now import the supported
  `scripts/scraper/` package and current `scraper.county.*` modules. No
  compatibility monolith was resurrected and no test was hidden with a skip.
- `scripts/entities/role_classifier.py` no longer imports
  `sentence_transformers` (and therefore Torch/Transformers) during module
  collection. The dependency is imported only by `load_model()`, the real
  inference path. Fake-model batching and orchestration imports remain light.
- County scraper focused result: **326 passed, 1 skipped, 11 subtests passed**.
- Role-classifier focused result: **10 passed**.
- Full-suite verification after Batch 2 confirmed all seven collection errors
  removed; the exact evidence is recorded below.
- The next apparent blocker was `tests/test_tempe_permits.py` importing the
  missing `Permit` ORM model. That diagnosis was corrected by the reachability
  audit in Batch 2: the test and three jurisdiction modules were retired
  remnants, not live consumers, so the unused model was not restored.

### Batch 2 — retire the unreachable permit product surface

- An initial diagnosis treated the remaining jurisdiction permit scraper files
  as live consumers and began restoring their missing ORM model. Owner review
  correctly challenged that assumption.
- Reachability audit found no registered `/permits` route, no CLI or pipeline
  dispatch, and no active sync declaration for `permits` or `permit_reports`.
  The permit templates, three scraper modules, and `test_tempe_permits.py` are
  orphaned remnants of a retired product surface.
- The uncommitted ORM restoration and test-path edits were fully reverted.
- No permit data or database schema was changed. Existing tables and historical
  rows remain preserved.
- Reachability and release-surface searches found no remaining consumer of the
  three jurisdiction permit scrapers, three permit templates, or their obsolete
  Tempe test. Those nine orphaned artifacts were removed rather than restoring
  an unused ORM model merely to satisfy an obsolete test.
- The broken permit card was removed from the otherwise retained home template,
  and README claims about live permit routes, scrapers, caches, and tests were
  corrected. Generic meeting/document references to permit matters remain.
- `table_role_inventory.py` now classifies `permits` and `permit_reports` as
  preserved historical/local-excluded tables that must never be propagated.
- Database tables, historical rows, and compatibility migrations remain
  untouched. No schema, data, service, scheduler, scrape, sync, deployment, or
  production operation was performed.
- Focused operational/governance result: **256 passed**. The release manifest
  built successfully and contains no retired permit artifact.
- Complete-suite command:
  `.venv/bin/python -u -m pytest tests/ -q --continue-on-collection-errors`
- Complete-suite log:
  `data/repository-remediation-full-suite-after-permit-retirement-unsandboxed-20260920-0125.log`
- Result: **131 failed, 4,306 passed, 28 skipped, 9 errors, 19 subtests
  passed in 75.19 seconds**. The 131 failed-node set is byte-for-byte identical
  to Batch 0. The eight removed errors are the six retired-monolith scraper
  imports, the role-classifier import-time ML failure, and the obsolete permit
  test. All nine remaining errors are the previously known Tempe pipeline
  fixture/module gap.
- A restricted-run log is retained separately; its 31 additional PostgreSQL
  setup errors were sandbox execution refusals, not repository failures. The
  unsandboxed run above is the authoritative Batch 2 comparison.
- `py_compile` and `git diff --check` pass. A clean post-commit worktree remains
  required before this batch is closed.

### Batch 3 — restore the Tempe pipeline test seam

- Four stale imports in `tests/test_tempe_pipeline.py` referenced the removed
  `scraper.jurisdictions.tempe_summary` monolith. They now import the supported
  `scraper.jurisdictions.tempe.council_summary` module used by the live scraper.
- Focused result: **33 passed, 4 failed, 0 errors**. The four assertion failures
  are the existing OnBase nested-item parsing defects and move to Phase 3; none
  is hidden or converted to a skip.
- Complete-suite command:
  `.venv/bin/python -u -m pytest tests/ -q --continue-on-collection-errors`
- Complete-suite log:
  `data/repository-remediation-full-suite-after-tempe-import-20260920-0148.log`
- Result: **129 failed, 4,317 passed, 28 skipped, 0 errors, 19 subtests
  passed in 76.83 seconds**. Phase 2 now has no collection or setup error.
  Nine former Tempe setup errors reach their assertions, and two Tempe vote
  persistence tests that failed in the prior contaminated run now pass.
- No product code, data, schema, service, scheduler, scrape, sync, deployment,
  or production target was changed.

### Failure-impact map after Batch 3

The 129 failures are not one release block and must not be worked as an
undifferentiated queue:

| Class | Count | Disposition |
|---|---:|---|
| Consumed Stage 2 plan/evidence bindings | 94 | preserve evidence; regenerate or archive only under the Stage 2/3 governance contract; not a morning-scrape blocker |
| Retired `inspect_db.py` CLI tests | 18 | remove obsolete tests and README claims; the CLI was deleted in July 2026 |
| Chandler minutes vote parser | 7 | live ingestion coverage; repair immediately |
| Tempe nested OnBase agenda parsing | 4 | genuine shared parser defect; next product-code batch |
| Deprecated `PublicBodyMember` tests | 3 | remove/update tests to the active `Person` + `BodyMembership` model |
| Persistence contract drift | 2 | update stale error-prefix and placeholder-name fixtures; preserve active behavior |
| Production-target marker centralization | 1 | safety-contract review; do not weaken the tier guard merely to green the test |

Priority is therefore: daily scrape/ingest/sync safety, shared Stage 3 parsers,
then governed disposition of consumed Stage 2 evidence. A zero-failure total is
not itself the release criterion.

### Batch 4 — operational triage and Chandler vote repair

- Removed the obsolete `test_inspect_db.py` suite and six CLI parser tests for
  the same deleted tool; removed the corresponding README commands and project
  entry. No replacement compatibility CLI was invented.
- Updated seven Chandler vote tests to the supported jurisdiction module. That
  exposed a live `NameError` (`re` used without import) and incomplete split-vote
  handling in the active parser.
- Chandler minutes parsing now extracts the bounded roll-call roster, classifies
  carried/failed count outcomes, records named dissenters, and assigns inferred
  majority votes only when a roster is present. The current-council surname map
  is a fallback only when minutes name dissenters without a roll-call block.
- Corrected the shared roll-call name/role delimiter regex to remove an invalid
  character-class warning.
- Removed two tests for the intentionally abstract, deprecated
  `PublicBodyMember`; retained and corrected the active inferred-absence test.
- Updated persistence fixtures to assert the classified `[UNKNOWN]` error and
  use a real-person name in an isolated meeting, so name validation remains
  enforced without cross-test contamination.
- Focused Chandler/persistence/voting/name-validation result: **76 passed**.
- Established morning/deploy/sync operational suite: **193 passed, 3 skipped**.
  No production, development database, scrape, sync, deploy, service, scheduler,
  or network mutation occurred.
- Complete-suite command:
  `.venv/bin/python -u -m pytest tests/ -q --continue-on-collection-errors`
- Complete-suite log:
  `data/repository-remediation-full-suite-after-operational-triage-20260920-0220.log`
- Result: **99 failed, 4,327 passed, 28 skipped, 0 errors, 19 subtests
  passed in 76.30 seconds**. Every remaining failure belongs to exactly one of
  three explicit classes: 94 consumed Stage 2 artifact/binding tests, four
  Tempe nested-item parser assertions, and one production-marker
  centralization assertion. There are no unexplained operational failures.

### Batch 5 — shared OnBase linkage and target-authority repair

- Extended the shared OnBase agenda-number grammar from numeric-only identifiers
  to Tempe's nested `4A` / `7B1` form. The four Tempe failures now pass, restoring
  section ordering, category inheritance, and nested item linkage used by Stage
  3 document/event work.
- Centralized the exact production hostname and all production host markers in
  `db.tier`. The body-code merge runtime, production integrity packet, and
  disabled Stage 3 alias runner now consume that authority instead of embedding
  competing strings. Every production refusal remains fail-closed.
- Parser-focused result: **44 passed, 2 skipped**. Tier-entrypoint result:
  **33 passed**. `py_compile` and `git diff --check` pass.
- Complete-suite command:
  `.venv/bin/python -u -m pytest tests/ -q --continue-on-collection-errors`
- Complete-suite log:
  `data/repository-remediation-full-suite-after-parser-and-tier-repair-20260920-0250.log`
- Result: **97 failed, 4,329 passed, 28 skipped, 0 errors, 19 subtests
  passed in 76.36 seconds**. All 97 failures are now KG artifact-binding tests:
  94 consumed Stage 2 artifacts and three intentionally stale Stage 3 B2 alias
  plans after the safety-critical apply runner changed. There are **zero
  non-KG failures**.
- The ten B2 aliases remain unapplied and the runner remains disabled. The three
  Stage 3 failures are a correct fail-closed signal: both alias plans must be
  regenerated and superseded against the centralized target authority before
  any future B2h authorization is considered.

### Batch 6 — KG evidence/mechanics test separation

- Repaired the isolated Stage 2 S1 parentage fixture: it now carries every
  referenced jurisdiction and uses the authoritative Peoria alias
  `peoria-planning-zoning`, rather than the retired `peoria-pz` spelling.
- Added a test-only Stage 2 adapter that copies immutable recorded plans into a
  disposable directory and rebinds only their declared code hashes. Executor,
  transaction, rollback, reservation, and adversarial tests exercise those
  disposable current-code copies. Historical artifacts remain byte-identical
  and continue to refuse after drift.
- Historical backup/receipt tests now evaluate their structural and role
  contracts at the receipt's recorded admission time through test-only clocks.
  Production admission still uses the real clock and retains its 24-hour age
  limit.
- Recorded Stage 2 containment, repair, correction, schema, data, and event
  artifacts are asserted as stale historical evidence where their bound code or
  semantic registries have changed. Newly built synthetic plans remain the
  positive validator/mechanics cases.
- Stage 3 B2 gate tests use disposable current-code plan copies while the ten
  recorded aliases remain unapplied and their on-disk plans remain correctly
  stale after target-authority centralization.
- Replaced three tests that edited repository source/artifact files in place
  with in-memory byte probes or `tmp_path` artifacts. An interrupted test run
  can no longer leave those source files or the plan directory dirty.
- Focused repaired clusters: **395 passed**. Complete KG suite:
  **2,922 passed, 25 skipped, 0 failed in 58.16 seconds**. The two pytest cache
  warnings are sandbox write restrictions on `.pytest_cache`, not test or
  product failures.
- Complete repository suite, run outside the filesystem/network sandbox so its
  disposable PostgreSQL fixtures could bind loopback ports: **4,427 passed, 28
  skipped, 0 failed, 0 errors, 19 subtests passed in 100.71 seconds**. The same
  run inside the sandbox produced 31 setup errors solely because loopback socket
  binding was denied; no test assertion failed there either.
- No plan was regenerated, promoted, applied, or authorized. No database,
  service, scheduler, scrape, sync, deployment, network, or production target
  was touched.

### Batch 7 — final release audit and integration

- Independent release and application audits found two previously unrecorded
  release blockers: seven standalone production writers were not all routed
  through the central interlock, and the Python-only manifest omitted the web
  application's newsletter dependencies and changed templates.
- Added a dependency-light, fail-closed interlock caller and wired every
  identified production writer before URL resolution, engine creation, or write
  dispatch. Read-only modes use `OP-STATUS`; mutating modes use `OP-SCHEMA`,
  `OP-RECON`, or `OP-REPAIR`. Ordering and unavailable-authority regressions are
  pinned by `tests/test_production_writer_interlocks.py`.
- Retired the unreferenced root `migrate.sh` and `rollback.sh` playbooks. They
  pointed to obsolete entry points and the rollback path contained an unguarded
  `rsync --delete`; the supported production wrapper remains
  `scripts/ops/deploy_release.sh`.
- The release manifest now includes the newsletter service/mail/image runtime
  modules and an explicit allowlist of the 15 changed runtime templates. It
  remains an allowlist rather than a directory copy.
- Isolated test-tier application import passed with **97 URL rules**.
- Final focused safety suite: **169 passed**. Final targeted tier/interlock,
  manifest and checklist suite: **132 passed**. Interlock ordering hardening:
  **4 passed**.
- Final complete suite: **4,430 passed, 28 skipped, 0 failed, 0 errors, 19
  subtests passed in 78.48 seconds**. Evidence log:
  `data/repository-stabilization-final-full-20260920T133300Z.log`.
- Final clean-commit release manifest: **126 files**, digest
  `3e69fd6c0d62783133ab0220575e4f4a8e422ac0eff3fd0107c938556ad87abd`,
  at
  `data/release/repository-stabilization-clean-406b842-20260920T134000Z/manifest.json`.
  `.env`, `data/`, tests, docs, contracts, uploads, checkpoints, backups, and
  audit evidence are absent; `scripts/kg/` contains only the two explicitly
  allowlisted runtime dependencies.
- Production authorization, deployment, synchronization, services, schedulers,
  and databases were not changed. Production writing remains paused and is a
  separate operation under `briefs/PRODUCTION-OPERATIONS-CHECKLIST.md`.
