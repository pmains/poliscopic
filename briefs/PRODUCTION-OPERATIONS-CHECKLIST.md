# PRODUCTION OPERATIONS CHECKLIST

**This file is the single authority for production operations.**

Canonical path: `briefs/PRODUCTION-OPERATIONS-CHECKLIST.md` (Git-tracked).

`.gitignore` ignores the entire `docs/` tree and the entire `data/` tree. Anything
under those paths is **local and non-authoritative**, including any convenience copy
of this procedure. If a copy of this checklist exists elsewhere, this file wins.

**Production safety must not depend on model context, chat history, or memory
files.** If an operator cannot do it from this document plus the repository, the
operation does not proceed.

Related history (context only — this checklist is self-contained):
* `briefs/20260918-repository-stabilization.md` — **tracked** operational record.
* `docs/briefs/039-containment-freeze-and-recovery.md` — **local, NOT tracked**
  (under ignored `docs/`). Cited by path for history; not version-controlled and
  not authoritative.

---

## 0. How to use this document

Work top to bottom. Every gate has six fields:

| Field | Meaning |
|---|---|
| **Operator action** | what the human does |
| **Entry point** | the exact command/tool to run |
| **Expected result** | what a pass looks like |
| **Evidence artifact** | path where the proof is written |
| **STOP if** | the explicit condition that halts the operation |
| **Human authorization** | whether a named human must authorize at this gate |

**A STOP condition is a stop, not a retry.** On STOP: freeze, preserve evidence,
record an incident entry, and return to the operator. Never re-run a production
mutation to "see if it works this time."

Run gates in order. Skipping a gate is a STOP condition.

---

## 1. Operation classification — choose EXACTLY ONE

Record the chosen kind verbatim in the authorization (Gate G8).

| Kind | Scope | Mutates production? |
|---|---|---|
| `OP-DEV` | development scrape / ingestion | **No** — no production access |
| `OP-CODE` | code deployment | Yes |
| `OP-SCHEMA` | schema migration | Yes |
| `OP-REPAIR` | bounded production data repair | Yes |
| `OP-RECON` | ordinary dev-to-production reconciliation | Yes |
| `OP-RESTORE` | rollback / restore | Yes |

### Separation rule (mandatory)

**No checklist run and no authorization artifact may cover more than one
production mutation kind.** `OP-CODE`, `OP-SCHEMA`, `OP-REPAIR`, `OP-RECON`, and
`OP-RESTORE` each require their own classification, their own plan, their own
digest, and their own human authorization. A code deployment and a data
reconciliation are never one operation, even when performed back to back.

`OP-DEV` touches no production. It requires **Gate G1 only**, plus its own
development-side evidence. Gates G2–G13 are not applicable to `OP-DEV`, and no
production authorization may be issued under `OP-DEV`.

---

## 2. Gate sequence

Summary (detail follows, in this order):

| Gate | Purpose | Blocks |
|---|---|---|
| G1 | live scheduler state and production hold | everything |
| G2 | clean/versioned Git source and exact commit | code, schema, repair, reconcile, restore |
| G3 | release allowlist / manifest digest | code, schema, repair, reconcile, restore |
| G4 | tests and known-failure accounting | all production kinds |
| G5 | read-only pinned-target identity and integrity snapshot | all production kinds |
| G6 | protected backup, SHA-256, inventory, scratch restore validation | all production kinds |
| G7 | exact digest-bound plan and rollback ownership | all production kinds |
| G8 | explicit human authorization quoting kind and digest | all production kinds |
| G9 | one bounded execution | all production kinds |
| G10 | immutable terminal receipt | all production kinds |
| G11 | database postconditions and public HTTP 200 checks | all production kinds |
| G12 | rollback decision | all production kinds |
| G13 | only then scheduler re-enable, with recorded ids/state | all production kinds |

---

### G1 — Confirm live scheduler state and production hold

**Operator action:** Read the live scheduler state and the local production hold.
Confirm every production-writing job is disabled and that a hold is present, with
its reason readable. Do not change scheduler state at this gate.

**Entry point:**
```
openclaw cron list --all --json
cat <production-hold-path>            # the fail-closed hold file, if present
```

> `--all` is **required**. `openclaw cron list` excludes disabled jobs by default
> (`--all   Include disabled jobs (default: false)`), so without it the two
> **disabled** jobs this gate must confirm are invisible — and a check that cannot
> see a job cannot confirm it is off.
>
> Change scheduler state only at G13: `openclaw cron enable <id>` /
> `openclaw cron disable <id>`.

**Expected result:** The three jobs below are in exactly this state, and no
production-writing job is enabled:

| Name | Job id | Required state |
|---|---|---|
| `maricopa-daily-sync` | `<daily-sync-job-id>` | **enabled** — development scrape only |
| `maricopa-sync-checker` | `<sync-checker-job-id>` | **disabled** |
| `maricopa-prod-sync` | `<prod-sync-job-id>` | **disabled** |

The OpenClaw scheduler is the source of truth for ids and state. A code change must
never re-enable a disabled job.

**Evidence artifact:** `data/audit/<UTCSTAMP>-g1-scheduler-state.txt` (captured
scheduler listing + hold contents; hold path and reason, no credentials).

**STOP if** any production-writing job is enabled; the hold is missing, unreadable,
or has no readable reason; a job id does not match the table; or live state cannot
be read at all. **Remain held — do not proceed and do not re-enable anything.**

**Human authorization:** Not required at this gate (read-only). Required later, at G8.

---

### G2 — Clean/versioned Git source and exact commit

**Operator action:** Confirm the deploy source is a clean, versioned snapshot — never
the ambient dirty checkout — and record the exact commit and manifest digest.

**Entry point:**
```
git status --porcelain
git rev-parse HEAD
git diff --check main...HEAD
```

**Expected result:** `git status --porcelain` is empty; `HEAD` resolves to one
40-character commit recorded verbatim; `git diff --check main...HEAD` exits 0.

**Evidence artifact:** `data/audit/<UTCSTAMP>-g2-source.txt` containing the commit
SHA, branch, and the empty-status proof.

**STOP if** the working tree is dirty, the commit is unknown/ambiguous, or whitespace
errors are present. A plan is valid only for the exact `code_hashes` it recorded —
a different commit invalidates it.

**Human authorization:** Not required at this gate.

---

### G3 — Release allowlist / manifest digest

**Operator action:** Build or verify the release manifest and confirm the deploy
source is a declared allowlist, not an unbounded tree copy.

**Entry point:**
```
# Write to a PER-OPERATION, unique, non-reused path:
python scripts/ops/build_release_manifest.py --out data/release/<operation>-<OPERATION_ID>/manifest.json
```

`<OPERATION_ID>` is the unique operation id (UTC stamp plus operation kind), created
once for this operation and reused by every gate. The fixed path
`data/bridge/release-manifest.json` is **no longer canonical evidence** — it is a
mutable path that gets overwritten, so it cannot witness a specific operation.

> **Known gap (blocker).** `build_release_manifest.py` writes with plain
> `write_text` + `chmod 0600`; it does **not** create the file exclusively
> (`O_EXCL`). Immutability therefore currently rests on the operator choosing a
> fresh per-operation path, not on the tool. Exclusive creation is a **missing
> implementation** (see §6 Blockers).

**Expected result:** The command exits 0 and writes a manifest at the per-operation
path, with a file count and a content digest. The digest is recorded verbatim; it is
bound into the plan (G7) and the authorization (G8). The path must not already exist.

**Evidence artifact:** `data/release/<operation>-<OPERATION_ID>/manifest.json` (mode
0600) plus its digest recorded in `data/audit/<OPERATION_ID>-g3-manifest.txt`.

**STOP if** the manifest fails to build; the file list is empty; the digest cannot be
computed; or the manifest contains paths outside the declared allowlist.

**Human authorization:** Not required at this gate.

---

### G4 — Tests and known-failure accounting

**Operator action:** Run the focused safety suites relevant to the operation kind and
account for every known failure explicitly. Do not summarize unknown failures away.

**Entry point:**
```
python -m pytest tests/test_sync_checker_no_production.py \
    tests/test_deploy_release_fixture.py tests/test_sync_prod_safe_default.py \
    tests/test_morning_sync_readiness.py tests/test_production_boot_safeguards.py -q
```

**Expected result:** **Every test relevant to the proposed operation passes.** A test
is relevant unless its irrelevance is positively established and recorded. Known
**unrelated** failures may be separately recorded with a named cause — but they do
not compensate for a relevant failure, and "accounted for" is **not** sufficient. A
green focused suite is still not release readiness.

**Evidence artifact:** `data/audit/<OPERATION_ID>-g4-tests.txt` — the exact command,
the pass/fail/skip counts, the relevance determination for each failure, and the
separately-recorded unrelated failures.

**STOP if** any test relevant to the operation fails; the relevance of any failure is
unknown or unestablished; the suite cannot be collected; or a failure is dismissed by
assertion rather than by evidence. **Unknown relevance is a STOP.**

**Human authorization:** Not required at this gate.

---

### G5 — Read-only pinned-target identity and integrity snapshot

**Operator action:** Confirm the pinned production target identity and take a
read-only integrity snapshot. Change nothing.

**Entry point:**
```
python scripts/ops/production_preflight.py \
  --output data/audit/<UTCSTAMP>-g5-preflight.json
```

The command checks the production interlock before credentials or engine creation,
pins the configured host and database, proves one PostgreSQL `REPEATABLE READ`,
`READ ONLY` transaction, and captures the DB-observed address, port, cluster
identifier, schema signature, Stage-0 metrics, and every declared public-body
reference representation in one coherent snapshot. Existing integrity debt is
recorded factually; G5 establishes identity and baseline truth and does not silently
redefine known debt as clean.

`scripts/ops/verify_morning_sync_readiness.py` remains diagnostic-only historical
artifact validation and is not a substitute for this live preflight.

**Expected result:** Exit 0 and a new canonical JSON artifact, mode 0600, created
exclusively with a digest over the complete snapshot.

**Evidence artifact:** `data/audit/<UTCSTAMP>-g5-preflight.json` (target identity +
preflight digest).

**STOP if** the live target differs from the pinned target; the preflight cannot run
read-only; or a write is attempted by a supposedly read-only step.

**Human authorization:** Not required at this gate.

---

### G6 — Protected backup, SHA-256, inventory, scratch restore validation

**Operator action:** Confirm a protected pre-operation backup exists, is nonzero, has
been inventoried, is hash-verified, **has an off-volume or independently protected
copy**, and — mandatorily — that a **restore from it has SUCCEEDED** into a scratch
environment. Never restore over production.

**A successful restore verification is MANDATORY before any operation that can mutate
database state.** Capture the restore target, the restored object counts, and the
result. An unverified backup is not a backup for this purpose.

**Retries resume from the earliest unverified phase; they do not restart the dump.**
Once a dump is complete, nonzero, mode 0600, hash-bound to its baseline and copied
off-volume with matching bytes/hash, treat it as an immutable reusable input. A
failure in scratch creation, restore, comparison, or teardown must be retried against
that same archive after re-validating every binding. Do **not** run `pg_dump` again
unless the dump itself is missing, incomplete, altered, or bound to the wrong target,
proposal, preflight, or snapshot. Each retry uses a fresh scratch name and its own
write-once attempt receipt; it never overwrites the source artifacts.

> **Same-volume hardlinks are NOT protection.** A hardlink protects the bytes against
> cleanup of the original directory entry; it does **not** protect against loss of the
> volume, filesystem corruption, or deletion of the volume. The local retention hold
> is therefore **insufficient on its own** for a production mutation. Production
> backup evidence requires an off-volume or independently protected copy.
>
> **A code restart is not assumed DB-neutral.** Current application startup may
> perform schema work. Absent positive evidence that a given restart is DB-neutral,
> treat a deploy/restart as potentially mutating and require this gate.

**Entry point:**
```
# create/verify the pre-operation backup and record digests
#   <backup tool for the tier>            (see docs/ops/OPS.md for the tool)
shasum -a 256 <backup-path>
# scratch-restore validation into a throwaway database

# after a verify-phase failure, resume the completed archive (no new pg_dump)
python scripts/ops/production_retirement_backup.py \
  --proposal <proposal> --preflight <preflight> \
  --resume-from <SOURCE_UTC_TAG> \
  --expect-dump-bytes <BYTES> --expect-dump-sha256 <SHA256>
```

**Expected result:** Backup file is **nonzero** and has a recorded SHA-256; its
inventory entry exists; a scratch restore was either performed successfully or its
absence is recorded as an explicit open risk. Zero-byte files are preserved but are
**never** called valid backups.

**Evidence artifact:** `data/backups/<tier>-<purpose>-<UTCSTAMP>.dump` plus its
`.sha256`, with the receipt and scratch-restore note in
`data/audit/<UTCSTAMP>-g6-backup.txt`.

**STOP if** the scratch restore is failed, absent, zero-byte, or otherwise unverified;
the backup is zero-byte, missing, unhashable, or its digest changes between creation
and use; or there is no off-volume / independently protected copy. **A failed or
unverified restore is a STOP — not an acceptable documented risk.** A NOT NULL column
added because no backup existed is exactly the failure this gate prevents.

**Human authorization:** Not required at this gate, but a **verified** backup and a
**successful** restore proof must exist before G8.

---

### G7 — Exact digest-bound plan and rollback ownership

**Operator action:** Produce the plan for **this one operation kind**, binding: the
exact commit (G2), the manifest digest (G3), the target identity and preflight digest
(G5), the backup receipt (G6), the operation kind (§1), an expiry/nonce, and the
rollback owner by name.

**Entry point:**
```
# NO GENERAL IMMUTABLE OPERATION-PLAN BUILDER EXISTS.
# This gate has no implementation for a general plan artifact. STOP.
```

**This is a blocker, not a step.** `python scripts/db/sync_prod.py --plan-only` was
previously shown here and has been **removed**: that option **does not exist**.
`scripts/db/sync_prod.py` supports exactly `--schema-only`, `--bootstrap-schema`,
`--status`, `--reconcile`, `--reconcile-only`, and `--reconcile-dry-run`.

For the record, neither proximity option is a general operation-plan builder:
`--status` prints a sync-lag report and transfers nothing, and `--reconcile-dry-run`
previews which production rows would be deleted. **Neither produces an immutable,
digest-bound, single-kind plan artifact**, so neither can satisfy this gate. Use only
a command whose semantics actually match the gate; do not present pseudocode as
executable.

Until a real planner exists for the operation kind, this gate is **unsatisfiable**
and the production operation is **blocked**.

**Expected result:** N/A — no implementation currently exists.

**Evidence artifact:** `data/release/<operation>-<OPERATION_ID>/plan.json` (mode 0600,
exclusive creation where implemented) and its digest in
`data/audit/<OPERATION_ID>-g7-plan.txt`. **Not currently obtainable.**

**STOP if** any binding is missing; the plan spans more than one operation kind; the
plan digest changes after being recorded; or no rollback owner is named.

**Human authorization:** Not required at this gate; the plan is what G8 authorizes.

---

### G8 — Explicit human authorization quoting operation kind and digest

**Operator action:** A named human authorizes **this exact operation**. The
authorization must be recorded **verbatim** and must bind ALL of: the exact human
approval text as given, the operation kind (§1), the exact plan or release digest
(G3/G7), the scope of the change, the author's identity, and the time.

**An agent may RECORD an approval that was already given. An agent may NOT create,
infer, broaden, paraphrase, or self-issue one.** An agent that drafts an approval and
then treats it as granted has self-authorized, which is prohibited. Authorization is
**single-use**: it covers one operation, and any retry, rollback, or second execution
needs a fresh approval.

Authorization is not implied by a green test run, a healthy website, a prior merge
receipt, an earlier approval of a different operation, or a document that merely
describes this gate.

**Entry point:**
```
# RECORD ONLY — transcribe the human's approval verbatim. Never compose it.
# (no issue/apply entry point exists; issuance is DISABLED)
```

**Expected result:** An authorization record containing the verbatim approval text,
the operation kind, the exact digest, the scope, the named author, and the UTC time —
all matching each other. Single-operation and single-use.

**Evidence artifact:** `data/release/<operation>-<OPERATION_ID>/authorization.json`
(mode 0600, exclusive creation where implemented).

**STOP if** authorization is absent; the approval text is paraphrased rather than
verbatim; an agent composed or inferred it; the quoted kind differs from the
classification; the quoted digest does not match the plan or release; the scope has
been broadened; or the authorization is for a different operation, commit, or an
expired window. **Runtime enforcement of this gate does not yet exist** — see §6.

**Human authorization:** **Required — this is the authorization gate.** No human
authorization at G8 means the operation does not proceed.

---

### G9 — One bounded execution

**Operator action:** Execute the authorized operation exactly once, bounded, with
logging. Not in a loop, not repeated to clear a transient error.

**Entry point:**
```
# the single approved entry point for the operation kind, run once
```

**Expected result:** One execution, one run directory, one log. The run either
commits within its transaction or aborts; no partial state is left behind.

**Evidence artifact:** the run log plus the run directory (preserved until confirmed
resolved).

**STOP if** the operation is asked to run a second time without a new G7/G8 cycle;
the transaction does not commit or roll back cleanly; or the process is retried
automatically. **Freeze production-writing jobs and preserve the run directory.**

**Human authorization:** Covered by G8 — but a **new** G8 is required for any retry,
for any rollback, and for any second execution.

---

### G10 — Immutable terminal receipt

**Operator action:** Confirm a terminal receipt was written by the operation itself,
binding the target, the plan digest, the schema capabilities, and the canonical
references.

**Entry point:**
```
ls -la data/body-code-merge/prod/<operation>-prod-receipt-*.json
```

**Expected result:** Exactly one terminal receipt for this execution, with a terminal
kind marker, a success record, the pinned target identity, a fresh UTC timestamp, a
backup proof reference, and the plan digest. A prep receipt or an older receipt from
a previous operation is **not** acceptable.

**Evidence artifact:** `data/body-code-merge/prod/<operation>-prod-receipt-<UTCSTAMP>Z.json`.

**STOP if** no terminal receipt exists; the receipt is a prep/plan receipt; the
receipt belongs to an earlier operation; the digest does not match G7; or the receipt
is missing any binding. **Never fabricate a missing receipt.** A missing receipt means
the operation is unproven — record it as an incident.

**Human authorization:** Not required at this gate.

---

### G11 — Database postconditions and public HTTP 200 checks

**Operator action:** Verify postconditions in the database read-only, **and**
independently verify the public site responds. These answer different questions.

**Entry point:**
```
# read-only postcondition queries for the operation kind
curl -s -o /dev/null -w '%{http_code}\n' https://poliscopic.com/
```

**Expected result:** Every stated postcondition holds (row counts, invariants, zero
dangling references, no duplicates), **and** key public routes return HTTP 200.

**Evidence artifact:** `data/audit/<UTCSTAMP>-g11-postflight.json` (database
postconditions) and `data/audit/<UTCSTAMP>-g11-http.txt` (HTTP status codes).

**STOP if** any postcondition fails, or any public route is not 200. **A website
returning 200 does not prove database integrity** — both checks are required and
neither substitutes for the other.

**Human authorization:** Not required at this gate.

---

### G12 — Rollback decision

**Operator action:** Compare G11 results against the plan's success criteria and
decide explicitly: accept, or roll back using the G6 backup and the named G7 rollback
owner.

**Entry point:**
```
# rollback path from the G7 plan, if invoked (this is a new operation: new G7/G8)
```

**Expected result:** A recorded decision — accept or roll back — with the evidence it
rests on. If rolling back, the rollback is itself a fresh operation of kind
`OP-RESTORE` requiring its own G7 and G8.

**Evidence artifact:** `data/audit/<UTCSTAMP>-g12-rollback-decision.json`.

**STOP if** postconditions are partial/failed and the decision is left unrecorded;
or a rollback is attempted without a new authorization. Ambiguous state means remain
frozen.

**Human authorization:** **Required if rolling back** (a new `OP-RESTORE`
authorization).

---

### G13 — Only then scheduler re-enable, with recorded ids/state

**Operator action:** Only after G11 passes and G12 records an accept, re-enable the
specific production-writing job(s) that were disabled. Record exact ids and the new
state. This is the only gate permitted to change scheduler state.

**Entry point:**
```
openclaw cron list --all --json          # --all is required to see disabled jobs
openclaw cron enable <id>                # enable ONLY the specific job, by id
```

**Expected result:** The intended job(s) enabled, by exact id, with the previous and
new state recorded. Everything not required by this operation stays disabled.

**Evidence artifact:** `data/audit/<UTCSTAMP>-g13-scheduler-change.txt` (ids, prior
state, new state, and the G11/G12 evidence they rest on).

**STOP if** G11 is not fully green, G12 does not record an accept, or the enable
cannot be scoped to a specific job id. **Never enable a job as part of a code
change** and never bulk-enable.

**Human authorization:** **Required** — re-enabling production writing is itself an
authorized act.

---

## 3. Morning workflow (3 AM run)

1. The **3 AM `maricopa-daily-sync`
   (`<daily-sync-job-id>`) may run while production is held.** It is
   development-only and writes no production data.
2. The checker performs **development analysis only** — inspect development scrape
   state, run analysis, relaunch the development scrape within its bounded policy,
   write local markers, report.
3. **Production reconciliation never follows automatically.** A clean analysis is not
   production authorization. The checker's former steady-state production call was
   removed; it now reports that production synchronization is separately gated and
   paused.
4. **An old merge receipt cannot authorize a new operation.** A receipt proves one
   past merge. It is not currency, not readiness, and not authorization for anything
   new. Age alone must not be treated as validity.
5. **Failure or missing evidence means remain held — not retry production.** If the
   morning run fails, or expected evidence is absent, the correct action is to stay
   in the held posture, record the finding, and escalate. Never re-run a production
   mutation to produce missing evidence.

---

## 4. Emergency / incident rules

1. **Freeze production-writing jobs first.** Disable by id before diagnosing. Do not
   diagnose while production writing is enabled.
2. **Preserve logs and backups before repair.** Copy evidence aside before any
   corrective action; repair must not overwrite the evidence of the fault.
3. **Never fabricate a missing receipt.** A missing or lost receipt means the
   operation is unproven. Record the gap as an incident. Do not reconstruct, infer,
   or re-date a receipt.
4. **Distinguish `preserved-unverified` from restore-tested.** Presence of bytes
   means preserved, nothing more. Only a completed restore test supports a stronger
   claim. Label honestly and never upgrade a label without the matching test.
5. **Record every production write, even failed and rolled-back attempts.** A write
   that was rolled back still happened. Log its kind, target, digest, and outcome.
6. **Website health does not prove database integrity.** HTTP 200 means the app
   answered. Database postconditions must be checked separately and are the
   authority on data integrity.

---

## 5. Canonical evidence index and naming convention

All timestamps are UTC in the form `%Y%m%dT%H%M%SZ` (e.g. `20260918T183052Z`).
`<operation>` is the lower-case operation kind (e.g. `code`, `schema`, `repair`,
`recon`, `restore`).

| Artifact | Canonical path pattern | Authority |
|---|---|---|
| Release manifest | `data/release/<operation>-<OPERATION_ID>/manifest.json` (mode 0600) | per-operation; the fixed `data/bridge/release-manifest.json` is **not** canonical |
| Backup dump | `data/backups/<tier>-<purpose>-<UTCSTAMP>.dump` | local evidence |
| Backup digest | `data/backups/<tier>-<purpose>-<UTCSTAMP>.sha256` | local evidence |
| Plan (digest-bound) | `data/body-code-merge/prod/<operation>-prod-plan-<UTCSTAMP>Z.json` | local evidence |
| Preimage / archive | `data/archive/<purpose>-<UTCSTAMP>Z.json` | local evidence |
| Terminal receipt | `data/body-code-merge/prod/<operation>-prod-receipt-<UTCSTAMP>Z.json` | local evidence |
| Postflight snapshot | `data/audit/<UTCSTAMP>-g11-postflight.json` | local evidence |
| HTTP check | `data/audit/<UTCSTAMP>-g11-http.txt` | local evidence |
| Gate evidence | `data/audit/<UTCSTAMP>-g<N>-<slug>.txt\|json` | local evidence |
| Write ledger | `data/archive/audit-write-ledger/` | local evidence |
| Incident record | `data/audit/incidents/<UTCSTAMP>-<slug>.md` | local evidence |

Notes:

* **Every artifact above lives under ignored `data/`.** None of it is
  version-controlled. Version-controlled content is limited to the tracked
  `briefs/` tree and other tracked source paths.
* Evidence is **never** synced to production.
* Zero-byte files are preserved for evidence but are **never** called valid backups.
* Time boundaries matter: record the pre-change and post-change timestamps for the
  operation explicitly, so artifacts can be attributed to the correct side of a
  change.

---

## 6. What this document does not do

This checklist is a procedure and a set of obligations. It is **not** a safety
mechanism by itself, and passing a documentation check proves nothing about runtime
behavior.

* A validator test may assert that required headings, gate order, STOP language,
  operation separation, scheduler ids, and evidence fields are **still present in
  this text**. That is a **documentation integrity check only**. It does not verify
  that any gate was actually performed, and it cannot prevent a production write.
* Runtime safety comes from the code-level controls (fail-closed holds and
  single-operation authorization), which are the subject of separate work. Until
  those exist and are verified, the only live containment for production writing is
  scheduler state — which is a human-controlled setting, not a code guarantee.
* If this document and runtime behavior disagree, **stop and treat runtime behavior
  as untrusted** until reconciled.

---

## 6.0 Original blocker inventory and current status

This table records the originally missing implementations and their current status.
An implemented control does not satisfy a later gate. Any operation that depends on
an open control is **blocked**.

| # | Missing control | Consequence |
|---|---|---|
| B1 | **G5 live identity/integrity preflight.** | Implemented; each operation still requires a fresh snapshot and may remain blocked by recorded debt or later gates. |
| B2 | **G7 has no implementation.** No general immutable operation-plan builder exists. | Cannot produce the digest-bound single-kind plan that G8 authorizes. |
| B3 | **Exclusive creation is not implemented** for manifests/plans/authorizations. The manifest builder writes with plain `write_text`. | Immutability rests on the operator choosing a fresh path, not on the tool. |
| B4 | **G8 runtime enforcement does not exist.** Issuance is disabled and nothing enforces single-use at runtime. | An authorization is currently a procedure obligation, not an enforced control. |
| B5 | **No fail-closed production hold is wired into the entry points.** | The only live containment for production writing is scheduler state — a human-controlled setting, not a code guarantee. |
| B6 | **The stale-receipt path is still live in code.** `scripts/sync/sync_prod.sh` calls `scripts/ops/verify_morning_sync_readiness.py`, whose only notion of currency is age. | An old merge receipt can still satisfy that gate. This checklist documents the requirement; it does not enforce it. |
| B7 | **Off-volume preservation of existing production backup evidence.** | Implemented for the newest existing production dump; G6 still requires a fresh pre-operation backup and successful scratch restore for any future mutation. |

## 6.1 Blockers, and which controls now exist

Items marked **RESOLVED** are implemented in code and covered by tests. Items still
open remain genuinely missing. Nothing here claims G7, general immutable exclusive
evidence, off-volume backup, or release readiness.

| # | Control | Status |
|---|---|---|
| B1 | **G5 live identity/integrity preflight.** | **RESOLVED** — `scripts/ops/production_preflight.py` captures one pinned, repeatable-read/read-only, digest-bound production snapshot through one connection. No mutation path exists. |
| B2 | **G7 has no implementation.** No general immutable operation-plan builder exists. | **PARTLY RESOLVED** — an immutable, digest-bound, exclusively-created plan builder now exists for **one** operation kind only: `OP-REPAIR` of the reference propagation (`scripts/ops/plan_propagation.py`). A **general** planner is still missing, so G7 remains open for every other operation kind. See §6.4. |
| B3 | **Exclusive creation is not implemented** for manifests/plans/authorizations. | **OPEN** — immutability rests on the operator choosing a fresh path, not on the tool. |
| B4 | **G8 runtime enforcement does not exist.** | **PARTLY RESOLVED** — no authorization can be validated, so nothing can be enabled by it; issuance remains disabled. G8 itself is still a procedure obligation, not an enforced control. |
| B5 | **No fail-closed production hold was wired into the entry points.** | **RESOLVED** — `scripts/ops/production_interlock.py` is wired into every production mutation entry point and refuses before any network/SSH/DB/rsync/restart. See §6.2. |
| B6 | **The stale-receipt path gated production.** | **RESOLVED** — `scripts/sync/sync_prod.sh` no longer calls `verify_morning_sync_readiness.py`; receipts are diagnostic evidence only. See §6.2. |
| B7 | **Off-volume preservation of existing production backup evidence.** | **RESOLVED for preservation** — the newest existing production dump has a byte- and SHA-256-matching copy on the independent development host. **G6 remains OPEN** for any future mutation until a fresh backup is copied and restored successfully. |
| B8 | **Release/rollback mechanism could not be exercised once the interlock refused unconditionally.** | **RESOLVED** — the mechanism was split out of the production wrapper into a sourced library with no production defaults, and is exercised through a test-only sandbox-bound adapter. See §6.3. |

## 6.2 Implemented production interlock (what actually runs)

The shared authority is `scripts/ops/production_interlock.py` — standard library
only, no network, no database, no credentials.

**What it does**

* **Classifies** the requested operation kind: the six checklist kinds, plus the
explicit read-only kinds `OP-STATUS`, `OP-PREFLIGHT`, `OP-DEV`. An **unknown** kind
is refused rather than defaulted to safe.
* **Reads an external hold** at `data/interlock/hold.json` — outside Git, since
  `data/` is ignored. Only the **directory path** is overridable, and a path is not
  an authorization.
* **Fails closed** on missing, malformed, empty, unreadable, ambiguous (competing
  hold files, inverted window) or stale (expired / not yet effective) hold state.
* **Refuses every production-mutating kind** because authorization issuance and
  validation are **disabled**. A valid hold is a containment condition, **not a
  permission**; an absent hold is never authorization either.
* **Emits a concise machine-readable refusal** (JSON) suitable for receipts and
  logging, with a stable `code` and the observed hold state.
* **Refuses attempted environment bypass** — the mere presence of
  `POLISCOPIC_PRODUCTION_AUTHORIZED`, `POLISCOPIC_FORCE_PROD`,
  `POLISCOPIC_INTERLOCK_OFF`, `POLISCOPIC_SKIP_INTERLOCK`, or
  `POLISCOPIC_ALLOW_PRODUCTION` yields `BYPASS_ATTEMPT`.
* **Has no `--force`, no override flag, and no environment escape hatch.** A code
  commit or deployment cannot remove the external hold.

**Exit codes:** `0` allowed (read-only only), `3` refused (interlock),
`4` fail-closed usage / unknown kind.

**Where it is wired in (the guard runs FIRST in each — before credentials, before
URL resolution, before any engine, SSH, rsync or restart):**

| Entry point | Operation kind | Guard placement |
|---|---|---|
| `sync.sh` | `OP-CODE` | before `source .env`, before any rsync/SSH |
| `scripts/sync/sync_prod.sh` | `OP-RECON` | before nohup and before the readiness check |
| `scripts/db/sync_prod.py` | `OP-RECON` (or `OP-STATUS` for `--reconcile-dry-run`) | first statement of `main()`, before `_resolve_prod_url()` and before any engine |
| `scripts/ops/deploy_release.sh` | `OP-CODE` | before staging/rsync/restart (`--stage-only` writes to production filesystem, so it is gated too) |
| `scripts/ops/deploy_code.sh` | `OP-CODE` | before `source .env`, before any rsync/restart |
| `scripts/ops/reload_gunicorn.sh` | `OP-RESTORE` | before the SSH reload |

**Read-only modes preserved:** `--status` and `--reconcile-dry-run` remain usable,
and are allowed only because they are classified read-only and tested as such.

**Tests:** `tests/test_production_interlock.py` (47 tests) — hold-state failure modes,
environment bypass, unknown kinds, the no-escape-hatch AST check, and integration
tests that execute the REAL entry-point files in a sandbox with canaries on `PATH`
for `ssh`/`scp`/`rsync`/`systemctl`/`psql`/`curl`/`wget`/`nohup`, asserting exit 3
with an empty canary log.

**Still NOT claimed by this control:** G7, general immutable exclusive evidence,
fresh backup/restore proof and release readiness remain open as listed in §6.1. The
interlock refuses; it does not make production ready.

## 6.3 Release seam — how the deploy mechanism stays testable without a bypass

An unconditional interlock makes the release mechanism unreachable from the production
entry point. Resolving that by adding a caller-selected "non-production" mode, a
`--test` flag, or an environment switch would be an escape hatch, so instead the
deploy script is split into three parts:

| Part | Path | Nature |
|---|---|---|
| **Production wrapper** | `scripts/ops/deploy_release.sh` | The only operational entry point. Runs the interlock **before** the mechanism is sourced, then supplies the production context. Contains **no fixture/test/local mode**, so a caller cannot make it behave like a test. |
| **Mechanism library** | `scripts/ops/deploy_release_lib.sh` | **Sourced library**: no shebang, not executable, refuses to run as a command, **no production host/path/unit/service defaults**, requires its complete context from the wrapper. Ships only because the guarded wrapper sources it. |
| **Test-only adapter** | `tests/deploy_release_fixture_adapter.sh` | Binds the mechanism to a throwaway local sandbox. Refuses any writable path escaping the sandbox, refuses `host:path` routes outright, and provides no SSH, external URL, production path, real `systemctl` or remote rsync. |

Activation, mode preservation, health-failure rollback and allowlist behaviour are all
still tested — through the sandbox-bound seam, not by weakening the wrapper.

## 6.4 Reference propagation contract and the OP-REPAIR plan builder

### The contract: `scripts/ops/propagation_contract.py`

One authoritative dev→prod reference-propagation contract, as a **general invariant**
— not a Chandler/Mesa special case, and not keyed to any specific body code.

**Established reference semantics** (traced from schema and sync code, not assumed):
TWO live representations exist, and the contract covers both.

| # | Representation | Points at | Notes |
|---|---|---|---|
| 1 | code string — `<table>.body` / `<table>.body_code` | `public_bodies.body_code` | `meetings.body` is NOT NULL, default `""` |
| 2 | integer FK — `<table>.public_body_id` | `public_bodies.id` | nullable on meetings/agenda_items; NOT NULL on body_seats/body_memberships |

**No `ForeignKey` constraint is declared for `public_body_id` anywhere in the models**, so
the database does not enforce representation 2. The contract does.

Contract rules (all fail closed): every propagated meeting must have its required
`public_bodies` parent selected first or already present with the expected identity;
parents are applied before dependents; **conflicting parent identities, missing parents,
duplicate aliases, retired codes and ambiguous mappings refuse the operation**; a clean
replay is a genuine no-op; postconditions require **zero newly introduced** dangling
references while preserving unrelated state; and a parent may not be removed while any
dependent still references it (both representations checked).

Sentinels `"__skip__"` and `""` are never promoted into `public_bodies`.

The ordinary sync path cannot silently omit reference tables: tests assert the REAL
`ALL_SYNC_TABLES` satisfies the dependency edges, that `public_bodies`/`meetings` are
neither excluded nor local-only, and that `RECONCILE_ORDER` stays children-first.

### The plan builder: `scripts/ops/plan_propagation.py` (G7 for OP-REPAIR only)

Offline and non-mutating. Consumes **explicit fixture/snapshot JSON**; there is no
URL/host/DSN option and no database import, so it cannot reach production. It emits a
unique, exclusively-created (mode 0600), never-overwritten plan at
`<out-dir>/repair-propagation-<UTCSTAMP>-<digest8>.json` carrying: exact source and
target fingerprints, operation kind `OP-REPAIR`, the ordered operations, expected
counts and postconditions, the rollback owner, and a SHA-256 digest binding the plan
body. It refuses placeholders, ambiguity, missing parents, untouched drift, and an
existing plan file.

**It is not an authorization.** Every plan carries `"authorization": "none - this plan
authorizes nothing"` and `"applies_anything": false`. There is no apply path in it.

**Scope limit, stated plainly:** this satisfies G7 for **this one operation kind**.
It is not the general immutable operation-plan builder, and blocker B2 stays open for
every other kind.

### Fixture-only applier

The transactional applier in the contract module is **fixture-only by construction**: its
only accepted argument is a live `sqlite3.Connection`, so it can never be aimed at
PostgreSQL or production — there is no URL, path or host parameter. A simulated
mid-operation failure proves zero partial state (full rollback).

## 7. Current status snapshot (informational)

* Production writing: **PAUSED**.
* Release: **STABILIZED** — the repository suite and release-surface gates are
  green. Production authorization and deployment remain separate and paused.
* Gate G6 backup posture: at least one nonzero pre-change production dump exists and
  is labeled `preserved-unverified`. There is **no** post-change production dump.
  No restore test has been performed.

## 6.5 Runtime enforcement — and corrections to earlier claims

**Correction:** §6.2 and §6.4 previously implied more enforcement than existed. Batch 3's
contract was a **plan/fixture-level proof only**; it was NOT enforced by ordinary sync, and
its tests asserted declaration membership and ordering — which do not make a per-row
invariant true. That overstatement is corrected here.

### The five layers, stated distinctly

| Layer | Where | What it actually guarantees |
|---|---|---|
| **Guarded entry point** | `scripts/db/sync_prod.py` (`main`) | Runs the fail-closed interlock BEFORE URL resolution or engine creation. Refuses every mutating mode (exit 3). `--reconcile-dry-run` is `OP-STATUS` (read-only) and is allowed through. |
| **Standalone maintenance writers** | `migrate_prod_db.py`, `cleanup_prod_db.py`, `backfill_supporting_documents.py`, `editorial_sync.py`, `remove_newsletter_articles.py`, `body_code_merge_prod.py`, `backfill_empty_body.py` | All route through `production_interlock_guard.py` before production URL resolution, engine creation, or write dispatch. Read-only modes are classified `OP-STATUS`; all mutation modes remain refused while authorization is disabled. |
| **Internal test seam** | `scripts/db/sync_runtime.py` (`run_sync`) | The sync MECHANISM, directly testable with explicit engines. No CLI, no URL resolution, no engine construction, no credentials, no interlock. Exists so mechanism tests do not have to bypass — or depend on — the guarded boundary. |
| **Runtime enforcement** | `scripts/db/sync_reference.py` | Parent-first apply; **fail-closed abort** before any dependent write when a required parent skipped; postconditions for BOTH representations; `parity_problems()` keeps the runtime consistent with the contract. |
| **Fixture-only proof** | `propagation_contract.apply_transactionally` | Transactional apply against a **disposable SQLite connection only** (no URL/path/host parameter), proving zero partial state on a simulated mid-operation failure. |
| **Plan-only proof** | `scripts/ops/plan_propagation.py` | An offline, digest-bound, never-overwritten `OP-REPAIR` plan. It **applies nothing and authorizes nothing**; it describes what the runtime enforces. |

### Runtime guarantees (implemented and proven)

* **Reference parents are full-reference for transfer.** `public_bodies` and `jurisdictions`
  are re-sent in full every sync, so a dev parent absent on the target is transferred even
  when its `updated_at` predates the checkpoint. Under the previous incremental filter it was
  never re-selected — the missing-parent incident.
* **Transfer strategy and audit stamping are separate.** `public_bodies` is full-reference AND
  stamp-required: body-code writes must still advance `updated_at` (Brief 037 defense in
  depth), because an in-place rename that skips the stamp becomes invisible to audits.
* **Parents before dependents, with fail-closed abort.** Before any dependent table the
  runtime asserts every required parent applied with zero skips, and raises otherwise. The
  dependent is not written across an unsatisfied reference boundary, and the parent checkpoint
  is not advanced, so the skip is retried rather than silently bypassed.
* **Postconditions cover BOTH representations** —
  `body`/`body_code` → `public_bodies.body_code` AND `public_body_id` → `public_bodies.id`.
  No FK constraint exists for the latter in the models, so this is where it is enforced.
  **A query failure is a failure** — it is never swallowed, and it fails validation overall.
* **Sentinels** (`''`, `__skip__`) are invalid/unresolved: never promoted into
  `public_bodies`, never counted as valid references, and reported.
* **One dependency authority.** `DEPENDENCY_EDGES` comes from the contract; the reconcile
  order is owned by `db.sync_declarations`; the contract's order is a **derived projection**
  of it, pinned by a parity test that has a negative control.

### LIMITATION — transaction scope (not claimed)

**Cross-table atomicity is NOT implemented and is NOT claimed.** Ordinary sync commits per
chunk and per table; providing one transaction spanning a parent and its dependents needs a
larger redesign. What is implemented is **referentially safe staged execution**: parents
first, abort on parent failure, postconditions after. A mid-sync failure can therefore leave
parent-present / dependent-absent, which dangles nothing. The dangerous direction — a
dependent written without its parent — is what is eliminated.

### G7 status after Stage C

G7 is satisfied **only** for the `OP-REPAIR` reference-propagation plan builder, and that is
a **plan-only** proof. There is still no general immutable operation-plan builder, so blocker
B2 remains open for every other operation kind.

## 6.6 Complete public-body dependency enforcement

The runtime authority is `PUBLIC_BODY_DEPENDENTS` in
`scripts/ops/propagation_contract.py`. A metadata parity test requires it to name
every synchronized model table carrying `body`, `body_code`, or `public_body_id`.
It is not limited to `meetings`.

Before each named dependent table is written, the runtime verifies that every
distinct, non-sentinel parent required by the source table exists exactly once on
the target and has the same reviewed identity. Parent skip, missing or duplicate
parent, identity conflict, sentinel, introspection error, or query error is a STOP
before the dependent upsert. `assert_parity()` runs before the sync lock is taken.

Reference-parent checkpoints are deferred until table-count validation and both
reference-representation postconditions pass. Count or integrity query errors fail
the operation. The older read-only integrity gate imports this same dependency
authority; it is not a second list.

This is staged referential safety, **not** cross-table atomicity and not production
authorization. It does not clear G5, the remaining G7 operation kinds, G8, or the
backup requirements.

## 6.7 Snapshot-bound G6 and production-reference envelope preparation

Commit `9bf83ed` adds `scripts/ops/production_g6_backup.py`, a backup-only G6
workflow with no apply path. One exported PostgreSQL `SERIALIZABLE READ ONLY
DEFERRABLE` snapshot supplies the target baseline, all 7,973 proposal preimages,
and the PostgreSQL 18 dump. This closes the race in an older `baseline; then dump`
sequence, where writers could make those artifacts describe different states.

The workflow refuses unless the exact reviewed candidate, target, and cluster
match. It creates an exclusive mode-0600 public-only dump; checks the TOC; copies
it without overwrite to the independent Windows host; verifies size, SHA-256,
machine, and volume; strictly restores the off-volume bytes into a unique scratch
database; compares every table count, the public schema, integrity metrics, and
all scoped preimages; then force-drops the exact scratch database and proves it is
absent. A `VALID` receipt can be created only after teardown. The receipt records
the source and scratch locales without falsely claiming locale equality.

**Current G6 state (2026-09-20): COMPLETE for the reviewed candidate.** The
snapshot-bound public-schema dump is 1,683,501,738 bytes with SHA-256
`d2ff2b8d9e8bafbbc2146a2e18838af1f5e0b33eed0d60b617e500d2ad5a7521` and is
retained on `<off-volume-host>` under
`<off-volume-retention-path>/production-repair.dump`.
The retained image was restored locally on that Windows host into the unique
scratch database `poliscopic_g6_scratch_20260920_232500`. All table counts,
public-schema projection, integrity metrics, and all 7,973 candidate preimages
matched the snapshot baseline. The exact scratch database was then force-dropped
and absence proved. The mode-0600 terminal receipt is
`data/audit/20260920T023148Z-g6-receipt.json`, digest
`49ff26710882333863870a1815d61bbd5f59ce4d2810739769fd1c2f08ec5120`.
No repair was applied and neither production nor `poliscopic_dev` was modified.

The live work exposed and corrected Windows transport/verification defects,
including an unsafe 1,000-ID encoded-command batch; verification now uses
Windows-safe 200-ID batches. Focused G6/G7/G8/executor result: **91 passed**.

Commit `0c4589d` adds offline OP-REPAIR envelope preparation. It pins candidate
digest `13dba345...0085bc`, all 7,973 update-only operations, and the digest of
all 3,621 quarantined records, while independently verifying the evidence chain.
Because there is no committed production repair executor or G8 runtime validator,
the artifact is honestly labeled `G7-CANDIDATE-EXECUTOR-MISSING`, remains
`apply_blocked=true`, and says `none - this plan authorizes nothing`. It does not
satisfy G7 yet.
