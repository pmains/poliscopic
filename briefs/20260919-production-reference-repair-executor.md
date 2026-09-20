# Production reference repair — executor and G8 design

**Status:** design approved for implementation review only; production apply remains
forbidden. The current human approval covers G6 backup/copy/scratch verification and
explicitly does **not** authorize the 7,973-row repair.

## Scope

One operation only: `OP-REPAIR`, updating `public_body_id` on exactly:

| Table | Rows |
|---|---:|
| `public.meetings` | 1,124 |
| `public.agenda_items` | 6,849 |
| **Total** | **7,973** |

All 3,621 quarantined evidence entries remain excluded. No other table, column, row,
schema, deployment, sync, scrape, restart, or cleanup belongs to this operation.

## Required committed surface

1. `scripts/ops/production_reference_g8.py`: stdlib-only, operation-specific
   authorization validator and immutable single-use state. No database or network.
2. `scripts/ops/execute_production_reference_repair.py`: the only PostgreSQL mutation
   entry point for this operation.
3. `scripts/ops/production_reference_g7.py` schema v2: binds both committed modules,
   the complete code manifest, full preimages, and all gate evidence.
4. `scripts/ops/production_interlock.py`: one closed authorized route for this exact
   entry point and operation. Ordinary `check("OP-REPAIR")` continues to refuse.

No generic authorization issuer, generic mutating allow path, `--force`, environment
override, fallback, or caller-provided boolean is permitted.

## G7 v2 contract

The plan may become `G7-READY-FOR-HUMAN-AUTHORIZATION` only after it binds:

- G1 hold/scheduler evidence and G4 exact test receipt;
- exact clean commit and committed-blob manifest including validator and executor;
- exact candidate, reference evidence, G5, fresh G6 receipt, and G6 baseline digests;
- the exact six-part production target identity and schema digest;
- complete full preimages for every operation;
- exact operation and quarantine counts/digests;
- a 256-bit nonce, named rollback owner, and expiry no later than four hours after
  creation or the G6 expiry, whichever is earlier.

The plan itself still says `none - this plan authorizes nothing`.

## G8 authorization

G8 is manual and verbatim. A recorder may accept literal human-supplied text but may
not draft, paraphrase, or infer approval. The immutable mode-0600 artifact binds the
full G7 digest and nonce, exact commit, exact two-table/one-column scope and counts,
named human author, approval/expiry timestamps, verbatim text and its SHA-256.

The text must unambiguously name `OP-REPAIR` and quote the full G7 digest. A copied,
broadened, expired, mismatched, agent-authored, symlinked, or non-0600 authorization
is refused.

## Single-use state

Before credentials, URL resolution, engine creation, or network access, the validator
atomically creates and fsyncs a mode-0600 claim keyed by plan digest and nonce. The
claim is never deleted or overwritten. Any attempt consumes the authorization,
including connection or transaction failure; retry requires a new G7/G8 cycle.

The append-only state is `CLAIMED`, followed by exactly one immutable terminal:
`SUCCESS`, `FAILED`, or `COMMIT_UNCERTAIN`. Multiple or ambiguous terminals refuse
future execution. `COMMIT_UNCERTAIN` permits read-only reconciliation only—never an
automatic retry.

## Executor transaction

1. Validate and consume exact G7/G8 locally through the closed interlock route.
2. Resolve only the canonical production URL; accept no DSN/host/database override.
3. Begin one serializable transaction with reviewed lock and statement timeouts.
4. Recheck the exact target/cluster; take one fixed advisory transaction lock and
   reviewed table locks.
5. Select and lock all 7,973 rows in batches; require full preimage equality,
   uniqueness, completeness, schema binding, and quarantine exclusion.
6. Execute two parameterized set-based updates with hardcoded table/column allowlists.
   Require exact rowcounts of 1,124 and 6,849.
7. Before commit, reselect every target; prove expected `public_body_id`, unchanged
   unrelated columns, exact integrity/postcondition vector, and untouched quarantine.
8. Commit once. Write `SUCCESS` only after an acknowledged commit. Any precommit
   failure rolls back and writes `FAILED`; ambiguous acknowledgement writes
   `COMMIT_UNCERTAIN`.

No production table is used as the authorization ledger because the approved data
scope permits only the two sets of `public_body_id` changes.

## Terminal receipt

The immutable receipt binds attempt/timestamps, observed target, every upstream
digest, exact commit and nonce, isolation and locks, preimage counts/digest, update
rowcounts, postcondition/integrity results, quarantine proof, commit outcome, and a
canonical receipt digest. Errors record only safe class/code values, never secrets.

## Required tests

- authorization mismatch, paraphrase, scope broadening, wrong author/commit/target/
  nonce/digest, expiry, symlink/mode, and competing claims all refuse;
- generic `OP-REPAIR` and alternate entry points remain refused before network;
- advisory/table locks precede reads and updates;
- any missing, extra, duplicate, or drifted preimage refuses before update;
- exact two-update rowcounts; SQL can change only `public_body_id`;
- midpoint/postcondition failures roll back both tables;
- commit ambiguity is terminal and cannot retry;
- disposable PostgreSQL end-to-end proof verifies all 7,973 results, unrelated and
  quarantined rows unchanged, receipt correctness, and second-use refusal.

## Human decisions still required before implementation can become executable

1. Approve final G8 schema and exact scope wording/author identity policy.
2. Choose protected append-only state location and preservation policy.
3. Approve lock strength, timeouts, and maintenance window.
4. Approve the exact integrity/postcondition vector.
5. Confirm G6 maximum age of four hours at execution.
6. Approve G1/G4 machine bindings and focused suite definition.
7. Approve read-only handling of `COMMIT_UNCERTAIN`.
8. Separately authorize any production code deployment (`OP-CODE`). Deploying the
   executor cannot be authorized by the later data-repair approval.
9. Finally, provide a new verbatim G8 approval quoting the completed G7 digest. The
   current G6 approval is not that approval.

## Implementation checkpoint

Commit `e80563e` adds preparation-only versions of the closed G8 validator and
transaction mechanism:

- `production_reference_g8.py` requires canonical mode-0600 G7/G8 artifacts,
  exact operation/scope/counts, a named human and verbatim approval containing
  the full G7 digest; atomically consumes the nonce in a current-user-owned
  mode-0700 directory; and permits exactly one append-only terminal state.
- `execute_production_reference_repair.py` models the single serializable
  transaction, lock-before-read ordering, exact pre/postimage checks, two
  hardcoded `public_body_id` update paths, exact rowcounts, rollback, quarantine
  preservation, and commit-uncertain handling.

Supervisor review found and corrected integration defects before this checkpoint:
the first executor compared full physical rows to partial candidate preimages,
computed a digest incompatible with G6 ordering, accepted a caller-supplied
terminal callback, and its synthetic G8 test hid missing real context fields.
The corrected pair retains full physical rows for post-update unrelated-column
comparison, computes the exact G6 canonical preimage digest, obtains its terminal
writer only from the sealed G8 context, and has a real cross-module claim/context/
terminal integration test. Combined focused result: **48 passed**.

This checkpoint is intentionally **not executable against production**. There is
no concrete PostgreSQL adapter, CLI, interlock route, or G7-v2 artifact. The
current committed G7 preparation remains `G7-CANDIDATE-EXECUTOR-MISSING`; no G8
authorization can be issued or validated for it.
