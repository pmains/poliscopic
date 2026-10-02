# 042 — Production operationalization and jurisdiction architecture

**Status:** 🚧 Active (2026-10-02)
**Owner:** poliscopic agent

## Objective

Turn the current remediation work into deployable, observable production
operations while making government authority—not geographic containment—the
organizing principle for scraper code and metadata.

The Apple-Silicon embedding sidecar is a completed experiment and is parked:
its present compute cost is too high for routine operation. It is not part of
this roadmap.

## Government-authority invariant

- Maricopa County, each city or town, MAG, and Valley Metro are independent
  jurisdictions/authorities in Poliscopic.
- A municipality's location inside a county is geographic context, not an
  ownership or reporting relationship.
- MAG and Valley Metro are regional authorities, not Maricopa County
  departments.
- Public bodies belong to one governing authority. Extraction sources may be
  many-to-one with that authority: for example, several Phoenix source
  commands all populate the City of Phoenix jurisdiction.
- No `parent_jurisdiction` hierarchy will be introduced. Future geography,
  service-area, membership, or state-enabling relationships must be modeled as
  separate typed relationships.

## Workstreams

### 1. Repository hygiene

- Classify modified and untracked files as runtime source, tests, durable
  documentation, generated evidence, local operations state, or disposable
  artifacts.
- Keep generated runs, downloads, databases, credentials, caches, and model
  artifacts out of version control without deleting operator data.
- Remove stale duplicated maps and contradictory operational guidance.
- Make every newly versioned runtime directory explicit in `.gitignore`.

### 2. Coherent landing and deployment

Prepare reviewable batches with independent verification and rollback notes:

1. security/startup and database-tier boundaries;
2. route/session and newsletter transaction ownership;
3. packaging compatibility slices;
4. scheduler/workflow source and daily-production verification;
5. government/source registry plus scraper-adapter extractions;
6. documentation and artifact-boundary cleanup.

Do not mix production data mutation into these code batches. Deployment must
preserve the existing production interlock and standing-operation scopes.

### 3. Daily production check

One daily receipt must prove, by source and governing authority:

1. scheduled extraction completed or emitted a named failure;
2. development persistence recorded meetings, items, and documents;
3. approved sync copied eligible changes to production;
4. document download and native/OCR extraction reached a terminal state;
5. new meeting/article/document content is searchable on production;
6. the daily newsletter was published to the website when scheduled;
7. failures and stale stages triggered one actionable notification.

Counts alone are insufficient. The receipt must include freshness timestamps,
source failures, OCR backlog/failure counts, sync reconciliation, and public
search/page probes.

### 4. Scraper decomposition

- Keep platform transport/parsing in `scraper.platforms`.
- Keep shared extraction behavior in `scraper.common`.
- Keep every government-specific adapter under `scraper.jurisdictions`,
  including Maricopa County and regional authorities.
- Treat source commands as extraction implementations, not jurisdiction
  identities. Declare their canonical authority explicitly in the source
  registry.
- Move inline handlers from `scraper/main.py` only with persistence, status,
  failure, and dispatcher regression tests.

## Verification

- Both scheduler tiers must retain all 40 source commands.
- Every scheduled source must reference a canonical government authority.
- Multiple sources for one authority must remain visibly grouped without being
  collapsed into one command.
- County, municipal, and regional authorities must remain peers; no implicit
  parent/child field is permitted.
- Each landing batch needs focused tests, scoped lint/compile checks, and a
  clean whitespace diff before deployment.

## Decisions reserved for the operator

- Credential rotation and revocation.
- Rewriting frozen KG evidence or historical authorization records.
- Any production schema/data migration outside an already approved standing
  operation.
