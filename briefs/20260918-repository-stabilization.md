# 2026-09-18 — Repository stabilization and the deployment-source policy

**Branch:** `codex/repository-stabilization-20260918` (local; not pushed)
**Base:** `3f3c043c0d0ce487b5cc6dcedf4d847cb7cc4565` (`main`)
**Prior state:** 385 dirty entries (307 untracked, 66 modified, 12 deleted)

This brief records the stabilization checkpoint and, more importantly, the rule
that replaces the practice it was cleaning up.

---

## MANDATORY: read the production operations checklist first

**Before ANY production operation, complete
[`briefs/PRODUCTION-OPERATIONS-CHECKLIST.md`](PRODUCTION-OPERATIONS-CHECKLIST.md).**

That tracked document is the **single authority** for production operations. It
classifies the operation into exactly one of six kinds (`OP-DEV`, `OP-CODE`,
`OP-SCHEMA`, `OP-REPAIR`, `OP-RECON`, `OP-RESTORE`) and defines the ordered gates
from scheduler-state confirmation through to scheduler re-enable, each with an
operator action, exact entry point, expected result, evidence artifact, explicit
STOP condition, and whether human authorization is required.

It exists so that production safety does not depend on model context, chat history,
or memory files. This brief is the **history**; the checklist is the **procedure**.

**Current posture:** production writing is **PAUSED** and the release is **BLOCKED**.
The 3 AM `maricopa-daily-sync` (`<daily-sync-job-id>`) is
enabled but development-only; `maricopa-sync-checker`
(`<sync-checker-job-id>`) and `maricopa-prod-sync`
(`<prod-sync-job-id>`) remain **disabled**, and a code change
must never re-enable either.

---

## The rule: deployment never uses the ambient checkout

**Deployments publish a clean, versioned source snapshot plus an allowlist. They
never publish the ambient working checkout.**

The checkout that this brief stabilizes had accumulated 306 untracked files, 78
tracked modifications and 12 deletions — including runtime pid files, notebooks,
personal notes and binary image uploads. Anything deployed from that state would
have carried whatever happened to be lying in the tree. That is the operational
control failure this work addresses.

Concretely:

1. **Deploy source is a snapshot**, created from a clean worktree of a reviewed
   commit — not from the day-to-day checkout.
2. **The release manifest is an allowlist.** `scripts/ops/build_release_manifest.py`
   enumerates what ships and excludes by prefix; anything not matched is absent
   from the release by construction.
3. **KG checkpoint paths must not ship.** `scripts/kg/` is excluded by prefix,
   with exactly two narrow exceptions (`scripts/kg/__init__.py` and
   `scripts/kg/stage2_parentage_contract.py`) because the reconciliation path
   imports that module at import time. Any new exception must be allowlisted
   explicitly and deliberately.
4. **Git cleanliness is not release readiness.** A clean tree means "every path
   is accounted for". It says nothing about whether the code is correct. Brief 039
   records the release as **BLOCKED** regardless of this commit series.

---

## What was done

**Preservation first, before anything was staged.** Nothing was pushed, deployed,
synced, scraped, or restarted. No history was rewritten.

Recovery material (local, mode 0600, outside version control under `data/archive/`):

| Artifact | Purpose |
|---|---|
| `data/archive/repo-stabilization-20260919T022211Z/repo-all-refs.bundle` | complete history bundle (110,883,287 bytes; `git bundle verify` → "The bundle records a complete history") |
| `.../tracked-edits.patch` | binary patch of all tracked modifications/deletions (530,920 bytes) |
| `.../status-porcelain.txt` | exact `git status --porcelain` at capture (385 entries) |
| `.../pin.txt` | HEAD, branch and capture timestamp |
| `.../tree-manifest-referenced.json` | the immutable worktree manifest bound to this capture |
| `.../classification.json` | machine-readable classification of all 385 paths |

## Classification of the 385 dirty paths

```
tests                          147
kg_implementation               82
operational_runtime             60
generated_runtime_private       51
unexplained_or_obsolete         25   (depth-1 scripts/*.py; re-classified by name)
application_newsletter_ui       17
documentation_briefs             3
```

**Generated / private material — excluded from commits, preserved on disk:**

```
.server.pid, PYEOF                       runtime / stray artifacts
YIMBY Scores.ipynb, YIMBY Scores.xlsx    notebooks and spreadsheets
.ipynb_checkpoints/                      notebook state
DREAMS.md                                personal/internal notes
static/uploads/ (45 files)               runtime image uploads
featured-photos/ (29 files)              environment-local photo library
```

These are now covered by `.gitignore`. They remain **on disk**; exclusion from
commits is not deletion. `featured-photos/` and `static/uploads/` are flagged for
an explicit later decision: if they are genuine deploy assets rather than
environment-local material, they need a deliberate asset path, not a blanket
ignore.

## The `contracts/` removal (12 files) — reviewed, not silently accepted

The 12 deleted files are the "Queen → specialist" delegation schemas
(`task-brief`, `task-result`, and the researcher/analyst/worker/writer
extensions). The deletion is **intentional**, on three independent grounds:

1. They describe the specialist agent framework retired on 2026-08-26 whose
   framework was removed on 2026-08-31.
2. The directory was removed whole, not partially — not an accident.
3. `scripts/ops/build_release_manifest.py` line 79 already lists `contracts/` in
   its EXCLUDE prefixes, so no release expects them.

They remain recoverable from the parent commit and from the bundle.

---

## Remaining failures — do not mistake this tree for healthy

The full suite is **not green**: 110 failures, 19 collection errors, plus 25
skipped and 1 xfailed. Details and causes: Brief 038 §1 and Brief 039 §C.

Two repository-owned defects must be fixed before the operational suites are
meaningful: the missing modules behind the 19 collection errors
(`scripts/agenda_scraper.py`, `scripts/capture_fixtures.py`), and the empty test
schema built by `tests/conftest.py:70-73`, which makes data-dependent tests fail
at `no such table` instead of exercising application code.

## Related records

* `docs/briefs/037-production-push-execution-2026-09-18.md` — gate execution (superseded)
* `docs/briefs/038-oversight-review-2026-09-18.md` — oversight audit
* `docs/briefs/039-containment-freeze-and-recovery.md` — freeze, role declaration,
  recovery packet, governance gates

`docs/` is intentionally outside version control per the existing `.gitignore`
policy; those briefs therefore live outside this repository and are referenced by
path only.

---

## Final review corrections (2026-09-18, commit `593c979`)

An independent review of this branch found two defects and required corrections.
They are fixed in one narrowly scoped commit, `593c979`.

### A. Shadowed local `time` imports — removed

`scripts/scraper/main.py` imports `time` at module level (line 7) but two
functions re-imported it locally as `import time as _time` (about lines 411 and
3114). The local imports shadow the module-level binding, which the AST guard in
`tests/test_bos_sync_path.py` correctly rejects. Both local imports are removed
and the 13 `_time.` call sites now use the module-level `time`. Behaviour is
unchanged — same module, same calls.

Flagged, fixed elsewhere: the same file uses `_sc_time` at about lines
3583–3641 but binds it **nowhere**, in HEAD, in the previous commit, or now
(confirmed by AST: used, never bound). That is a pre-existing latent `NameError`
on a debug/logging path, it predates this work, and it was deliberately left for
its own batch rather than folded into a bounded correction.

### B. Whitespace defects — cleared mechanically

Exactly the `git diff --check` findings, nothing else:

```
12 files: redundant blank line at EOF removed
 2 files: one trailing-whitespace line each stripped
15 files changed, 15 insertions(+), 37 deletions(-)
```

### Verification after the fix

```
git diff --check main...HEAD                    exit 0          (must be 0: MET)
git status --short                              empty           (must be: MET)
tests/test_bos_sync_path.py                     7 passed        (was 2 failed)
focused operational suite (clean worktree)      107 passed, 3 skipped, 0 failed
  — before this fix:                            105 passed, 2 failed, 3 skipped
compileall app.py routes scripts/db scripts/ops scripts/sync scripts/scraper
                                                exit 0
import app from the clean worktree              OK, 97 URL rules
release manifest, built FROM the clean worktree 105 entries; scripts/kg/ = 2,
                                                both allowlisted
  data/ .env tests/ docs/ contracts/ checkpoints/   absent
```

### The full suite is still NOT green — this remains explicit

```
full suite after this fix:  110 failed, 3615 passed, 27 skipped, 17 errors
```

The 110 failures and 17 collection errors were **not** worked on in this task, by
instruction. `test_sync_data_integrity` remains skipped without a populated
archive, and it was **not** unskipped — it requires a real archive via
`POLISCOPIC_ARCHIVE_DB`. Nothing here changes the release position: **the release
remains BLOCKED** per Brief 039. Git cleanliness, and a green focused operational
suite, are still not release readiness.

---

## Final operational fix — Surprise CivicClerk branch (commit `e396aea`)

A second review confirmed the Surprise CivicClerk sync branch in
`scripts/scraper/main.py` was not a latent debug-path concern but a hard blocker:
it could not execute at all.

```
_sc_time      referenced 11 times, bound NOWHERE
_surprise_t0  used for the total-elapsed calculation, bound NOWHERE
              the first _sc_time.time() call precedes search_meetings,
              so the branch raised NameError deterministically when invoked
```

### The fix

1. The branch now uses the existing module-level `time` import consistently —
   all 11 `_sc_time` call sites rewritten; no `_sc_time` name remains anywhere.
2. `_surprise_t0` is bound as the **first statement of the branch**, before any
   work starts, and feeds the total-elapsed calculation as before.

No logic, control flow, network call, or database write was changed. Only the
timing instrumentation became valid; scraper semantics and ordering are
otherwise untouched.

### Regression

`tests/test_surprise_civicclerk_timing.py` proves, statically and dynamically:

* no `_sc_time` remains in the module;
* inside the branch every underscore-prefixed timing name is bound before its
  first use — ordered by **source position**, not AST traversal order (the first
  attempt used `ast.walk` per statement and produced false positives for
  `_t_mtg`/`_t_evt`, because `ast.walk` is breadth-first and mis-orders names
  inside a loop body);
* `_surprise_t0` is bound at the start of the branch;
* the branch actually **reaches its mocked `search_meetings` call** without
  `NameError`.

`search_meetings` is mocked to return an empty list, so the branch returns before
`get_session()` — no live HTTP and no database mutation.

**Negative control** (using the preserved pre-fix file, not a synthetic edit):
the same static check reports `['_sc_time', '_surprise_t0']` as used-before-bound
against the pre-fix source, and `[]` against the fixed source. The check genuinely
catches the defect rather than passing vacuously.

### Verification after `e396aea`

```
tests/test_surprise_civicclerk_timing.py     4 passed
tests/test_bos_sync_path.py                  7 passed (11 passed together with the above)
focused operational suite                    111 passed, 3 skipped, 0 failed
    — before this fix:                       107 passed, 3 skipped
compileall app.py routes scripts/db scripts/ops scripts/sync scripts/scraper
exit 0
import app                                   OK, 97 URL rules
git diff --check main...HEAD                 exit 0
git status --short                           empty
```

### Still not green — unchanged and still explicit

The full suite was **not** addressed in this task and remains not green: the 110
failures and 17 collection errors stand as recorded above, and
`test_sync_data_integrity` stays skipped without a populated archive. **The release
remains BLOCKED.**

---

## Batch 1 — remove the checker's production mutation

### The problem

`scripts/sync/sync_checker.sh` invoked the root `sync.sh` in its **steady state** —
the "analysis clean" branch — not on an edge case. That single call combined a
full-tree code deployment with production reconciliation, and it bypassed the
newer readiness path entirely:

```bash
# scripts/sync/sync_checker.sh:252, inside the "analysis clean" branch
echo "=== Syncing to poliscopic.com ==="
bash "$PROJECT_ROOT/sync.sh" 2>&1
```

Because it fired precisely when analysis came back clean, it ran routinely.

### The change

The call and all associated "syncing to production" behaviour are removed. After a
clean analysis the checker marks local development analysis complete and reports
that production synchronization is separately gated and paused:

```
=== Production synchronization: PAUSED (separately gated) ===
  This checker only inspects development scrape state and relaunches the
  development scrape within its bounded policy. Production synchronization
  is a separate, reviewed, explicitly gated operation and is NOT performed
  here. A clean development analysis is not production authorization.
```

The checker retains only its legitimate development duties: inspect development
scrape state, run analysis, relaunch the development scrape within its bounded
per-day policy, write local markers, and report. It references no `sync.sh`,
`sync_prod.sh`, `sync_prod.py`, `deploy_release`, SSH, `rsync`, production URL, or
production credential — verified by grep and by the regression's static scanner.
Diff scope: 1 file, +5 / -8.

### Regression (`tests/test_sync_checker_no_production.py`)

Two independent proofs, because a grep-only test would be weak:

**A. Static** — no production entry point token appears anywhere in the checker.

**B. Dynamic** — every reachable checker state branch is **executed** in a temp
sandbox that is a self-contained `PROJECT_ROOT`. Canary executables are planted at
every production entry-point path (`sync.sh`, `scripts/sync/sync_prod.sh`,
`scripts/db/sync_prod.py`, `scripts/ops/deploy_release.sh`, `deploy_code.sh`) and on
the `PATH` (`ssh`, `scp`, `rsync`, `curl`, `wget`). Each canary appends to a log. If
any branch reached production, the canary would fire. Branches exercised:
`running`, `never-launched`, `launched-no-summary`, `analysis-clean`,
`already-analyzed`, `monitor-only`.

### Two findings worth recording

1. **The tail fallback is unreachable.** A `monitor-only` state — a stray monitor
   file with no other markers — is caught by Case 2, not the tail. My first draft
   asserted it reached the fallback and failed. Rather than paper over it, the test
   now carries `test_tail_fallback_is_unreachable_by_case_ordering`, which
   **exhaustively enumerates all 8** `(LAUNCHED, SUMMARY, ANALYSIS)` combinations
   and proves every one is claimed by a case, so the fallback is defensive dead
   code. A separate test still asserts the unreachable tail contains no production
   call, so it cannot become a hiding place.
2. **The negative control is a real artifact, not a hand-written stub.**
   `tests/fixtures/sync_checker_prefix_batch1.sh` is **byte-identical to
   `git show HEAD:scripts/sync/sync_checker.sh`** at `182c191` (verified with
   `cmp`), captured before the fix. The dynamic harness run against it **fires the
   `sync.sh` canary**, and the static scanner flags it — so both halves are
   demonstrated to catch the removed call rather than pass vacuously.

### Verification after the Batch 1 commit

```
tests/test_sync_checker_no_production.py     14 passed
focused scheduler/sync safety suites         67 passed
tests above + operational suite              132 passed, 2 skipped, 0 failed
bash -n scripts/sync/sync_checker.sh         OK
compileall                                    exit 0
import app                                   OK, 97 URL rules
build_release_manifest.py --out <temp>       exit 0, 105 files
git diff --check main...HEAD                 exit 0
git status --short                           empty
```

### Scheduler state — OpenClaw scheduler is the source of truth

Recorded as established by the manager on 2026-09-18. A code change must never
re-enable a disabled job; job state is changed through the scheduler.

| Name | Job id | State |
|---|---|---|
| `maricopa-daily-sync` | `<daily-sync-job-id>` | **enabled** — 3 AM Phoenix, development scrape only |
| `maricopa-sync-checker` | `<sync-checker-job-id>` | **disabled** |
| `maricopa-prod-sync` | `<prod-sync-job-id>` | **disabled** |

The 3 AM `maricopa-daily-sync` run is **development-only** and stays enabled as
intended. **Production writing stays paused** until the reviewed gates are
satisfied. Neither disabled job may be re-enabled by this change.

The same state and posture are recorded in the human-facing ops doc,
`docs/ops/OPS.md` (Cron Jobs section), together with the note that
the checker's former steady-state `sync.sh` call was removed in Batch 1.

**Governance note (not fixed here, outside this batch's scope):** `.gitignore:70`
ignores `docs/` wholesale, so `docs/ops/OPS.md` and every `docs/briefs/*` file are
**untracked**. The ops doc therefore is not version-controlled; this record in
`briefs/` is the tracked copy of the scheduler state.

### Still not green — unchanged and still explicit

The full suite remains **not green** (110 failures, 17 collection errors), and
`test_sync_data_integrity` stays skipped without a populated archive. The release
remains **BLOCKED**. Nothing in Batch 1 satisfies a release gate.

### Next recommended batch

**meetings / public_bodies propagation contract**, followed by **production
integrity repair planning** — the two items deferred from the containment review.

---

## CORRECTION to the Batch 1 record — the editorial finding

Batch 1's commit message claimed a fix for an "editorial defect" (a duplicated word).
That claim must be qualified.

While building Batch 2's duplicated-word guard I found the guard had a
**false-positive mode**: the pattern `\b(\w{3,})\s+\1\b` treats a hyphen as a word
boundary, so the phrase **"read-only only"** was reported as the duplicate "only only".
The guard is now tightened with a `(?<![\w-])` lookbehind and pinned by a test that
asserts "read-only only" is NOT a defect while "the the" IS.

**Consequence, stated plainly: I cannot reconstruct the original line, so the Batch 1
"editorial defect" may have been this same regex false positive rather than a genuine
duplicated word.** I therefore no longer claim a verified editorial defect in the
Batch 1 change. What is verified is that the guard is now correct in both directions.
The audit's own example wording ("receipt receipt") was not the text I found, which is
consistent with the finding having been a false positive rather than a real defect.

---

## Batch 2 — fail-closed production interlock, and the release seam

Implements checklist blockers B4, B5 and B6. Issuance of production authorization
remains **disabled**; this batch creates no way to authorize production.

### The interlock: scripts/ops/production_interlock.py

Standard library only — no network, no database, no credentials. It classifies the
requested operation kind (the six checklist kinds plus read-only `OP-STATUS`,
`OP-PREFLIGHT`, `OP-DEV`), reads an external hold at `data/interlock/hold.json`
(`data/` is gitignored, so the hold is NOT Git-tracked), and fails closed on missing,
malformed, empty, unreadable, ambiguous or stale hold state.

Because authorization issuance and validation are disabled, **every
production-mutating kind is refused unconditionally**. A valid hold is a containment
condition, **not a permission**; an absent hold is never authorization either. An
**unknown** kind is refused rather than defaulted to safe.

Refusals are concise machine-readable JSON with a stable `code`, and the mere presence
of `POLISCOPIC_PRODUCTION_AUTHORIZED`, `POLISCOPIC_FORCE_PROD`,
`POLISCOPIC_INTERLOCK_OFF`, `POLISCOPIC_SKIP_INTERLOCK` or
`POLISCOPIC_ALLOW_PRODUCTION` yields `BYPASS_ATTEMPT`. There is **no `--force`, no
override flag, and no environment escape hatch** — pinned by an AST test. Exit codes:
`0` allowed (read-only only), `3` refused, `4` fail-closed usage or unknown kind.

Wired in FIRST, before credentials, URL resolution, engine creation, SSH, rsync or
restart: `sync.sh` (`OP-CODE`), `scripts/sync/sync_prod.sh` (`OP-RECON`),
`scripts/db/sync_prod.py` (`OP-RECON`; `OP-STATUS` for `--reconcile-dry-run`),
`scripts/ops/deploy_release.sh` (`OP-CODE`), `scripts/ops/deploy_code.sh` (`OP-CODE`),
`scripts/ops/reload_gunicorn.sh` (`OP-RESTORE`).

### B6: the stale readiness gate is removed

`scripts/sync/sync_prod.sh` no longer calls `verify_morning_sync_readiness.py`. That
script validates LOCAL HISTORICAL artifacts and cannot establish current production
identity or integrity, so it must not permit production. Historical receipts are
diagnostic evidence only.

### The release seam — preserving mechanism coverage without a bypass

The unconditional interlock made `deploy_release.sh`'s staging/activation/health/
rollback mechanism unreachable, which had broken four fixture tests. The resolution was
NOT to rewrite them as refusal tests, and NOT to add a caller-selected
production/non-production switch. Instead the script was split:

1. **`scripts/ops/deploy_release.sh`** — the production wrapper. Runs the interlock
   before the mechanism is sourced, then supplies the production context. It has **no
   fixture seam at all**: there is no `POLISCOPIC_FIXTURE_ROOT`, no test mode and no
   local target, so a caller cannot make the production wrapper behave like a test.
2. **`scripts/ops/deploy_release_lib.sh`** — the mechanism, as a **sourced library**.
   No shebang, not executable, refuses to run as a command, contains **no production
   host/path/unit/service defaults**, and requires its complete context from the
   wrapper. It ships only because the guarded wrapper sources it.
3. **`tests/deploy_release_fixture_adapter.sh`** — a **test-only adapter** that binds
   the mechanism to a throwaway local sandbox. It refuses any writable path that
   escapes the sandbox, refuses `host:path` routes outright, and provides no SSH,
   external URL, production path, real `systemctl` or remote rsync.

The four original fixture tests still test successful activation, mode preservation,
health-failure rollback and allowlist behaviour — now through that local-only seam.

### Verification

```
tests/test_deploy_release_fixture.py              activation/modes/dotenv, rollback,
                                                  allowlist, wrapper refusal
tests/test_deploy_release_seam.py                 wrapper refuses before the mechanism;
                                                  no fixture seam; adapter sandbox escapes
                                                  rejected; library non-executable, no
                                                  defaults, refuses direct execution
tests/test_sync_prod_safe_default.py              interlock pins; stale gate asserted ABSENT
tests/test_production_interlock.py                47 tests
```

Two of my own new tests were wrong on first run and are recorded rather than hidden:
the flag scan mistook the interlock CLI's `--operation`/`--entry-point` for wrapper
flags, and the ordering check used a raw `str.index("nohup")` which matched the word in
the guard's own comment. Both are fixed (forbidden flag NAMES only; line-based ordering).

### Still NOT resolved

B1 (G5 has no implementation), B2 (G7 has no implementation), B3 (no exclusive
creation), B7 (no off-volume backup copy) all remain open. The interlock refuses; it
does not make production ready. The full suite is still not green and the release
remains BLOCKED.

---

## Batch 3 — reference propagation contract and dry repair planning

Development consolidated Chandler and Mesa body codes, but production received meeting
rows without all corresponding `public_bodies` reference rows, leaving meetings pointing
at absent body codes. This batch makes that state impossible to produce silently, and
plans the repair from evidence rather than improvising it. Development/local only.

### Established reference semantics (traced, not assumed)

Two live representations exist, and the contract covers BOTH:

| # | Representation | Points at | Notes |
|---|---|---|---|
| 1 | code string — `<table>.body` / `<table>.body_code` | `public_bodies.body_code` | `meetings.body` is NOT NULL, default `""` |
| 2 | integer FK — `<table>.public_body_id` | `public_bodies.id` | nullable on meetings/agenda_items; NOT NULL on body_seats/body_memberships |

**No `ForeignKey` constraint is declared for `public_body_id` anywhere in the models**, so
the database does not enforce representation (2). The contract does. Sentinels
`"__skip__"` and `""` are scrape placeholders that must never be promoted.

Ordering, as declared by the dependency authority: upserts parents-first
(`jurisdictions` → `public_bodies` → `meetings`), reconcile deletes children-first
(`RECONCILE_ORDER`).

### The contract — `scripts/ops/propagation_contract.py`

One authoritative invariant, NOT a Chandler/Mesa special case; the observed codes are
regression examples only and are not hard-coded. Fail-closed on: missing parents (either
representation), conflicting parent identity for one code, duplicate aliases (one code
declared with multiple ids), ambiguous alias mappings, retired codes, and sentinels. A
clean replay is a genuine no-op. Postconditions require zero NEWLY introduced dangling
references and preserved unrelated state. A parent cannot be deleted while any dependent
still references it.

Ordinary sync cannot silently omit reference changes: tests assert the REAL
`ALL_SYNC_TABLES` satisfies the dependency edges, that `public_bodies`/`meetings` are
neither excluded nor local-only, and that `RECONCILE_ORDER` stays children-first.

### The plan builder — `scripts/ops/plan_propagation.py` (G7 for OP-REPAIR only)

Offline and non-mutating. Consumes explicit fixture/snapshot JSON; no URL/host/DSN option
and no database or network import, so it cannot reach production. It emits a unique,
exclusively-created (0600), never-overwritten plan carrying source/target fingerprints,
operation kind `OP-REPAIR`, ordered operations, expected counts and postconditions, the
rollback owner, and a SHA-256 digest binding the plan body. It refuses placeholders,
ambiguity, missing parents, drift, and an existing plan file.

**It is not an authorization**: every plan carries `"authorization": "none - this plan
authorizes nothing"` and `"applies_anything": false`, and there is no apply path in it.

**Scope limit:** G7 is satisfied for THIS ONE operation kind. It is not the general
planner; blocker B2 remains open for every other kind.

### Fixture-only applier

The transactional applier is fixture-only by construction — its only accepted argument is
a live `sqlite3.Connection`, so it can never be aimed at PostgreSQL or production (no URL,
path or host parameter). A simulated mid-operation failure proves zero partial state via
full rollback.

### Regression fixtures (tests/test_propagation_contract.py)

Seven shapes, all synthetic and local: Chandler split (old registered code plus meetings
on the missing canonical code); Mesa equivalent; a missing parent referenced by many
meetings (`phoenix-gp` shape); the sentinel shape; a parent collision with non-identical
identity; clean replay/no-op; and rollback with zero partial state. Plus the
integer-FK representation, the deletion guard, postconditions and ordering.

### Verification

```
tests/test_propagation_contract.py    29 passed
tests/test_plan_propagation.py        23 passed
```

### Still NOT resolved / NOT claimed

B1 (G5 has no implementation), B3 (no exclusive creation outside this operation's plans),
B7 (no off-volume backup copy) remain open, and B2 is only PARTLY resolved. This batch
does **not** claim a general planner, production readiness, repaired production data, or a
completed backup. No production data was read. The full suite is still not green and the
release remains BLOCKED.

---

## Correction — the Batch 2 and Batch 3 green claims were INCOMPLETE

Both earlier batches reported green focused suites, and both claims were incomplete.

Batch 2 reported "220 passed" and Batch 3 reported "286 passed". Neither run included
`tests/test_db_sync_prod_decomposition.py`. I chose those file lists by hand and omitted
that file, so the suite was **not** green while I reported that it was.

### The regression this hid

Batch 2 wired the production interlock into `sync_prod.main()`. Two tests in
`test_db_sync_prod_decomposition.py` call `main()` directly and assert a clean return:

    test_default_sync_skips_schema_bootstrap_and_holds_one_lock_session  -> assert main() == 0
    test_schema_bootstrap_requires_an_explicit_flag                      -> assert main(bootstrap_schema=True) == 0

Both returned **3** (interlock refusal). Verified on a clean tree at HEAD with
`git status` empty, so this was the committed state. It went unreported for two batches.

### Clean-tree full-suite baseline at `b49964b`

Run as: `python -m pytest tests/ -q --continue-on-collection-errors`

```
131 failed, 3815 passed, 27 skipped, 17 errors, 8 subtests passed   (70.65s)
```

Note the plain `pytest tests/` run **interrupts** with 8 collection errors
(`test_board_of_adjustment`, `test_board_of_health`, `test_cli`,
`test_drainage_review_board`, `test_industrial_development_authority`,
`test_role_classifier_batching`, `test_tempe_permits`,
`test_transportation_advisory_board`), so `--continue-on-collection-errors` is required
to obtain a comparable full-suite number at all.

The 17 errors here differ from the 8 collection errors above: those 8 appear in the
reported 17 once collection is allowed to continue.

### Standing rule adopted

Curated focused counts are **not** acceptable evidence of suite health. Every batch
claim in this line of work must be backed by a full-suite run compared against this
baseline, and any new failure or error is a stop condition.

---

## Stage A — sync mechanism extracted behind a guarded boundary

Owner decisions applied: preserve mechanism coverage through a guarded CLI boundary plus
a directly testable internal runtime (the same architecture used for deploy), and keep
`sync_prod.py` a thin facade.

### What changed

| File | Role |
|---|---|
| `scripts/db/sync_runtime.py` (new) | The sync **mechanism**: `run_sync(dev_engine, prod_engine, ...)`. **No** CLI, `__main__` block, URL resolution, engine construction, credentials, environment reads, or interlock import. Takes already-constructed engines. |
| `scripts/db/sync_prod.py` | Now a **thin facade** (210 lines, was 352): runs the fail-closed interlock, resolves URLs, constructs engines, then delegates to `run_sync`. Every historical re-export is preserved. |
| `tests/test_db_sync_prod_decomposition.py` | The two substantive tests now drive `sync_runtime.run_sync` directly with mock engines, keeping their assertions about **schema bootstrap** and **lock lifetime**. Plus new tests that the facade refuses before URL resolution or the runtime, that it fails closed when the interlock is missing, and that the runtime has no CLI/URL-resolution/defaults. |

The deploy post precedent is followed: the mechanism is testable without going through —
and without bypassing — the guarded entry point. The substantive mechanism tests were
**not** rewritten as refusal-only tests.

### Stage A gate

```
tests/test_db_sync_prod_decomposition.py
tests/test_production_interlock.py
tests/test_sync_prod_safe_default.py              83 passed
```

### Honest scope note

`public_bodies` is still classified incremental and no reference enforcement is wired
yet — that is Stage B. This stage repairs the regression and the record only.

---

## Stage C — plan alignment, record correction, and baseline verification

### Plan aligned with the enforced runtime

`scripts/ops/plan_propagation.py` now records the transfer strategy
(`jurisdictions`/`public_bodies` full-reference), states that audit stamping is a separate
concern, restates the postconditions to match the runtime (zero remaining **scoped**
dangling; both representations; sentinels invalid), and carries explicit
`runtime_alignment`, `transaction_scope` and `applies_anything: false` fields. It remains
**non-applying and non-authorizing** (no apply path; `authorization: none`). New tests pin
that its apply order matches the runtime dependency authority and that its postconditions
name both representations.

### Record corrections — the five layers

Overstatements from Batch 3 are corrected in the checklist (§6.5). The distinctions are now
explicit and must not be blurred:

  * **guarded entry point** — `sync_prod.main` runs the interlock before URL resolution
  * **internal test seam** — `sync_runtime.run_sync`, testable with explicit engines
  * **runtime enforcement** — `sync_reference`: parent-first, fail-closed abort,
    postconditions for both representations
  * **fixture-only proof** — `apply_transactionally`, disposable SQLite only
  * **plan-only proof** — `plan_propagation`, applies and authorizes nothing

### Full-suite comparison, from a clean tree

```
                            failed  passed  skipped  errors
baseline (b49964b)            131    3815      27      17
after Stage A + B + C          131    3839      27      17
```

The FAILED/ERROR **sets** are byte-identical (148 entries each) — zero new failures or
errors, and none fixed by this work; the +24 passing rows are the new tests. Curated
focused counts are still not accepted as evidence of suite health.

### Remaining limits

Cross-table atomicity is **not** implemented and is **not** claimed: referentially safe
staged execution is what exists. G7 remains satisfied only for the `OP-REPAIR` plan builder
(plan-only). Blocker B2 stays open for every other operation kind, and B1/B3/B7 are
unchanged. Release remains BLOCKED.

---

## Direct oversight correction — complete public-body graph enforcement

The first Stage B implementation was still incomplete. It guarded only
`public_bodies -> meetings`, while the synchronized schema also carries public-body
references in `agenda_items`, `agenda_item_votes`, `body_memberships`, `body_seats`,
`case_events`, `executive_session_participants`, `meeting_attendance`,
`meeting_members`, `member_votes`, `pz_item_details`, and `supporting_documents`.
Several of those tables were ordered before `public_bodies`. The tested
`force_include_parent_keys()` helper was not called by the runtime, `assert_parity()`
was not called by `run_sync()`, and count-validation exceptions still logged without
failing. Those were review findings, not production incidents; no external operation
was run.

The correction now provides:

* one reviewed `PUBLIC_BODY_DEPENDENTS` map covering every synchronized model table
  with `body`, `body_code`, or `public_body_id`, with a metadata parity regression;
* parent-first apply order (`jurisdictions`, then `public_bodies`, then every
  public-body dependent) and children-first reconcile parity;
* runtime `assert_parity()` before the lock or any mutation;
* an exact all-source-row target-coverage check immediately before each dependent
  table, refusing sentinels, missing/duplicate parents, id/code/name identity drift,
  and inspection/query errors;
* deferred reference-parent checkpoints: they advance only after count validation
  and relationship postconditions succeed;
* count-validation exceptions and safety-path dangling-query exceptions fail closed;
* the older `reference_integrity_gate` consumes the same dependency authority rather
  than maintaining a second literal list.

Verification performed locally with no external access:

```
neighboring sync/propagation/interlock/deploy/BOS family  429 passed, 1 skipped
full suite (--continue-on-collection-errors)              131 failed, 3853 passed,
                                                           27 skipped, 17 errors
recorded clean baseline                                    131 failed, 3815 passed,
                                                           27 skipped, 17 errors
```

The pre-existing failure/error counts are unchanged; the additional passes are new
regressions from Stages A–C and this correction. Cross-table atomicity is still not
claimed: the safe failure direction is parent-present/dependent-absent. Release
remains blocked by the checklist's unresolved gates and the pre-existing suite state.

---

## Morning baseline triage — exact failure sets, not counts

The clean-tree full-suite baseline was classified before further release work. The
current sync/reference correction introduced **zero new failed or errored node ids**;
the exact 148-entry failed/error set remains the b49964b baseline while passing tests
increased from 3,815 to 3,853.

The baseline is not treated as generally green:

| class | result | release meaning |
|---|---:|---|
| stale, fail-closed Stage 2 evidence bindings | 94 failures | blocks reuse of those KG plans/receipts; regenerate before another Stage 2 operation |
| repository-owned API/test drift | 37 failures + 16 errors | open engineering debt; adjudicate before a general repository release |
| Python 3.14 / torch collection compatibility | 1 error | environment debt; pin or repair the supported test environment |
| regression introduced by current sync/reference work | **0** | exact node-id comparison, not equal counts, is the evidence |

For a bounded operational change, the minimum test rule is therefore: the complete
failed/error node-id set may not grow beyond the recorded b49964b baseline; every
changed-file and import-adjacent operational test must pass; and the applicable
production-operation checklist gates must be satisfied. Historical KG binding
failures are never waived for KG work — they require fresh artifacts — but they do
not become evidence that an unrelated sync module regressed. A focused green suite
still does not make the repository generally release-ready.

The previously untracked retention-audit implementation was independently reviewed,
passed its focused 3-test suite, and was preserved as the separate commit `5e3a40f`.
It performs a read-only, hardlink-aware inventory and grants no cleanup authority.

---

## G5 live production preflight — implemented and exercised read-only

Commit `2c4ce7f` adds the supported G5 entry point
`scripts/ops/production_preflight.py`. It checks the production interlock before
environment or engine resolution, validates the exact configured production host and
database, proves a single PostgreSQL `REPEATABLE READ` / `READ ONLY` transaction,
and captures one coherent identity, schema, Stage-0, and complete public-body
reference snapshot. The output is canonical JSON, digest-bound, mode 0600, and
created with `O_EXCL`. There is no mutation or apply path.

The first live read-only snapshot is
`data/audit/20260919T160000Z-g5-preflight.json`, digest
`1597b720ba9c6f91d03830c0e8088715e9176cfca0e6ee415df603f8e3e528a0`.
It bound database `poliscopic`, the pinned DigitalOcean hostname, observed server
address/port, PostgreSQL cluster system identifier, 45 public-schema tables, and the
read-only transaction proof.

G5 is now implementable, but the snapshot correctly shows production is **not clean**:

| metric | count |
|---|---:|
| `agenda_items.body` dangling | 569 |
| `agenda_items.public_body_id` dangling | 106 |
| `agenda_items.public_body_id` null | 8,201 |
| `meetings.body` sentinel/null | 218 |
| `meetings.public_body_id` dangling | 51 |
| `meetings.public_body_id` null | 1,412 |
| `member_votes.body` sentinel/null | 197 |
| `supporting_documents.body` dangling | 840 |
| Stage-0 graph-builder replay excess | 27,903 |
| unresolved relationship provenance | 1 |

These are factual baseline measurements, not automatic repair instructions. They do
not invalidate the read-only identity snapshot, and they do block any claim that
production has converged. A later plan must classify and bind the exact rows it will
change; G5 does not authorize that plan or any mutation.

### Off-volume preservation

The newest existing nonzero production dump,
`poliscopic-body-code-merge-20260918T185120Z.dump` (1,683,406,961 bytes), now has an
independent copy at
`<off-volume-host>:<off-volume-retention-path>`.
The source and destination SHA-256 are identical:
`2663fa6d983f14dbe0de8fbf4ced78092566767861039eff6122c749cfc12bbf`.
The verification receipt is
`data/audit/20260919T162500Z-off-volume-backup-copy.json` and is also copied beside
the remote dump.

This resolves the immediate volume-loss preservation gap for the existing evidence.
It does **not** satisfy G6 for a future operation: the dump predates later production
changes, and a fresh pre-operation backup plus successful scratch restore will still
be mandatory before any mutation.

### G5-bound row evidence

Commit `8aa7edd` adds a second read-only tool that refuses unless the live target and
cluster match the exact G5 artifact. The resulting evidence artifact is
`data/audit/20260919T161500Z-production-reference-evidence.json`, digest
`4d409a125ea2d32500deed82549c018d90fc8245920bb8f382f33c19df1172d8`,
bound to G5 digest `1597b720…e528a0`. All six categories were captured completely
(`truncated=false`) in one repeatable-read/read-only transaction.

The row-level evidence separates deterministic candidates from unresolved identity:

| category | rows | candidate-parent result |
|---|---:|---|
| meeting null/sentinel body | 218 | 218 have no candidate |
| meeting null/dangling `public_body_id` | 1,463 | 1,245 exactly one; 218 none |
| agenda-item dangling body | 569 | all are `phoenix-gp`; no registered candidate |
| agenda-item null/dangling `public_body_id` | 8,307 | 7,342 exactly one; 396 have two; 569 none |
| supporting-document dangling body | 840 | all are `phoenix-gp`; no registered candidate |
| member-vote null/blank body | 197 | 197 have no candidate through current upstream identity |

The 396 two-candidate agenda items are `glendale-cc` rows attached to meetings whose
actual bodies include `glendale-cocc` or `glendale-gsc`; this is a real identity
conflict, not a safe bulk backfill. The `phoenix-gp` rows expose a missing registry
parent shared across agenda items and documents. No repair was applied. A future
`OP-REPAIR` plan may include only exact row ids and before-values, must distinguish
the 1-candidate cohorts from the zero/two-candidate cohorts, and must bind both
evidence digests.

### Apply-blocked repair candidate

Commit `56a6879` adds an offline-only planner. Candidate artifact
`data/audit/20260919T164500Z-production-reference-repair-candidate.json`, digest
`13dba3455e03a423619405d75647143acef9bab9aca0b6879726fd919c0085bc`,
contains **7,973** exact-PK proposals and **3,621** quarantined evidence entries:

* 1,124 meeting and 6,849 agenda-item `public_body_id` proposals passed strict
  row/body/jurisdiction/upstream/candidate consistency checks;
* 121 one-candidate meetings and 493 one-candidate agenda items still disagreed on
  jurisdiction or upstream identity and were quarantined rather than promoted;
* all zero-candidate, multi-candidate, sentinel, blank-vote, `phoenix-gp`, and
  Glendale conflict rows remain quarantined.

This is explicitly `CANDIDATE-NOT-AUTHORIZABLE` and `apply_blocked=true`. It is not
a complete G7 plan and has no apply path. It records the still-missing exact commit,
release-manifest digest, fresh-backup receipt, expiry, nonce, and rollback owner.
Nothing in the candidate authorizes production or implies that the 7,973 rows have
been changed.

### G6/G7 preparation after the production-reference candidate

`9bf83ed` implements a fresh G6 backup and scratch-restore proof with no apply
path. Its central correction is one exported read-only PostgreSQL snapshot shared
by the baseline, all 7,973 proposal preimages, and `pg_dump`. The dump is
exclusive/mode 0600/public-only; the off-volume copy is hash/size/host/volume
verified; and those off-volume bytes are strictly restored into a disposable
Windows scratch database. Counts, schema, integrity, and every scoped preimage
must match before teardown and a `VALID` receipt. Supervisor verification:
36 focused tests passed; compile and `git diff --check` passed.

The live G6 run was not started. The execution environment requires explicit
approval to transfer a fresh production dump to the specific retention target
`<development-ssh-host>:<off-volume-retention-path>`; it rejected the launch before
execution. No production connection, dump, copy, or scratch lifecycle occurred.

`0c4589d` implements offline, immutable OP-REPAIR envelope preparation and pins
the reviewed candidate and exact proposal/quarantine populations. Review caught
and corrected an overclaim in the first draft: there is no committed repair
executor or G8 runtime validator, so this is **not** a valid G7 plan. Its status
is `G7-CANDIDATE-EXECUTOR-MISSING`, `apply_blocked=true`, with both missing
surfaces explicit. Supervisor verification: 45 focused tests passed and
`git diff --check` was clean. No authorization or apply path exists.

### Closed G8/executor preparation checkpoint

Commit `e80563e` adds the offline G8 single-use validator and a mocked, closed
transaction executor mechanism. Supervisor integration tests: **48 passed**.
Both remain preparation-only: no PostgreSQL adapter, CLI, interlock allow route,
or valid G7-v2 plan exists. They cannot reach production, and the current G7
candidate remains apply-blocked. The design and remaining human decisions are
recorded in `briefs/20260919-production-reference-repair-executor.md`.
