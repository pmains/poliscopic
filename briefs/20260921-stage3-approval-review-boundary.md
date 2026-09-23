# Stage 3 approval review boundary

**Date:** 2026-09-21
**Status:** NOT APPROVED / NOT APPLIED — no approval record exists
**Why this file exists:** `/docs/` is ignored on this branch, so a durable record of the
review boundary has to live somewhere tracked. This is that record.

## What this is

A local-only review surface for the Stage 3 processing-receipt continuation. A human
reviews a digest-bound proposal and, if they choose, records an authorization. Recording
an authorization **executes nothing**.

## How to open it

```
cd /Users/pmains/Code/openclaw/poliscopic
.venv/bin/python scripts/kg/stage3_approval_serve.py
```

Then open `http://127.0.0.1:5002/kg/stage3-approval/` (override the port with
`STAGE3_APPROVAL_PORT`).

Do **not** reach this surface through `app.py`. The main application binds `0.0.0.0` with
`debug=True`, and the Werkzeug debugger permits arbitrary code execution; an authorization
surface must never be exposed that way. The approval routes are no longer registered in the
main application at all.

## The security properties, and why each exists

| Property | Why |
|---|---|
| Disabled by default; answers 404 unless `POLISCOPIC_STAGE3_APPROVAL_UI=1` | The blueprint existing must never be enough to expose a usable approval endpoint. |
| Not registered in the main application | Removing the route entirely is stronger than gating it, so production startup cannot expose it even if the flag is misconfigured. |
| Loopback `remote_addr` required | Defence in depth, never a substitute for the flag: a proxy can make a remote caller look local. Disabled-and-remote is still refused. |
| Session-bound CSRF token, constant-time compared, consumed on success | Without it, any client that could reach the route could manufacture an approval. Absent, wrong, cross-session, and replayed tokens are all refused. |
| `debug=False`, `use_reloader=False`, loopback bind, asserted in code | Debug mode on an authorization surface is a code-execution path. |
| Typed name is attribution, not authentication | The record names its reviewer; the local-only binding is what makes the record trustworthy. |

The ledger at `data/kg-approvals/kg-stage3-approval-record-<proposal digest>.json` is created
once, mode 0600, published with an exclusive atomic link so it cannot replace an earlier
decision. Its name and directory match no runner terminal, preflight, or checkpoint glob.

## The operation presented

| Binding | Value |
|---|---|
| proposal | `f6a06b59e8da8bfdaef5e460a3659961931d6f3a19f0dd2dd237fa33c6202f60` |
| plan | `73359d2df800b5d8f4e1a399bfc925141e4617e82f0509354470e6f4478eb2f9` |
| design packet | `ae86c35948ca6e214e5699e799897811b2517d285e003c4126c7c6a44fea852c` |
| receipt set | `95f8e5d04636624c679144e5b9e8f938abf1bfc816bb39f69cfc22bb05bd8a0c` (6,095) |
| backup receipt | `3eb11bbec9bdbff7c9e7ecd5a46a0f17709c318cdebbac4425fe03d3afbccd7e` |
| schedule | `117da8dd098d7483de49985e52545f28d1817e71c4293fb00714a2c2950616b2` |

Cursor **6100** · **58,628** new receipts · **120** batches · **500** rows per batch ·
**996** held rows after the cursor · **5** held rows inside the consumed prefix · stop on
first failure · append-only · no `supporting_documents.swept_at` rewrite.

**Replay, stated correctly:** the 6,095 existing receipts were *classified* as replay no-ops
inside the consumed prefix, which sits **before** the cursor. The continuation starts after
them; it does not iterate, re-process, or replay them.

## What the page refuses

Blank or missing reviewer name; any unchecked acknowledgement of the six; altered approval
wording; a form-supplied digest that does not match the bound proposal; stale, tampered,
malformed, or obsolete artifacts; a schedule disagreeing with the reviewed counts; any target
other than the exact development target; a missing, wrong, cross-session, or replayed form
token; and a duplicate submission.

## What it never does

No database connection; no preflight; no authorized packet; no backfill; no runner. A test
asserts those names never appear in the route module and that the apply machinery raises if
touched on either path. Approving records authorization only.

## Next

A later, separately bounded task consumes an approved record to generate the distinct
authorized packet and a fresh preflight. Until a human submits through the local surface and
that task runs, nothing is authorized and nothing has executed.

---

## Consumption outcome (2026-09-23): AUTHORIZED, NOT APPLIED — preflight blocked

### The human decision consumed

| Field | Value |
|---|---|
| record | `data/kg-approvals/kg-stage3-approval-record-f6a06b59e8da8bfdaef5e460a3659961931d6f3a19f0dd2dd237fa33c6202f60.json` |
| record digest | `95590e342db23701344b2c20811839dbd44377ecaf381470bb003a1c9820eead` |
| reviewer | Peter Mains |
| approved at | 2026-09-22T15:20:08.935879+00:00 |

The consumer `scripts/kg/stage3_approval_consumer.py` is pinned to this exact lineage: the record
path, record digest, reviewer, proposal digest, acknowledgements, review boundary, execution block,
and scope are all constants, and there is no CLI flag that can widen acceptance. Counts are
re-derived from the artifacts rather than read from the record, so a record cannot broaden its own
scope. The verbatim approval text and its sha256 travel into the packet.

### Authorized packet

`data/kg-plans/kg-stage3-processing-receipt-authorized-apply-20260923T125526Z.json`
digest `1e36d27ee2a3f2a32e2d2d4972b5981f9050ee158e5d2a4a72eb3d227ce9b8d5`

Built with the canonical authorization builder, then bound to the human record: record digest,
approver, approved-at, and the verbatim approval text. `state: authorized`, `enabled: true`, bound
to code digest `29e8f351d66e96d25541a4e6e46ff5d084d5a29475b98c8268a322ceb66640a8` and to the plan,
design packet, receipt set, backup receipt, and schedule. Derived scope matched the reviewed scope
exactly, including the **five** held rows in the consumed prefix.

### Fresh preflight: REFUSED — drift found

```
PreflightRefused: the backup receipt is stale: 150818s old exceeds 86400s
```

The bound backup receipt (`3eb11bbe…cd7e`) was created `2026-09-21T19:01:59.826071+00:00`. At
preflight time it was 150,827s old — **41.9 hours against a 24-hour window** (`MAX_BACKUP_AGE_SECONDS
= 86400`, `store_backup.py:26`) — over by 64,427s (17.9 hours). No preflight artifact was written;
the refusal happens before any artifact is produced.

**This was not repaired, and must not be repaired by a consumer.** The approval record binds the
*old* backup digest. Producing a fresh backup yields a new receipt digest, which no longer matches
the record, so the packet would have to be regenerated against a backup the human never reviewed.
That is a change of approved scope, and it requires a new human approval — not a waiver of the
freshness guard, and not a silent re-bind.

### State of the world

- **AUTHORIZED, NOT APPLIED.** Nothing executed, no batch started.
- No database row, schema object, DDL, migration, or lock was created. The only database activity
  was the SELECT-only preflight attempt, which refused before writing anything.
- No terminal receipt exists: nothing under `data/kg-receipts` was created or modified.
- Nine pre-existing authorized packets (2026-09-20, pre-approval lineage) bind the **superseded**
  plan. They are inert: `validate` refuses them with "does not bind the current dry plan" and
  "does not bind the current disabled packet". They carry no approval-record binding and so are not
  "another approval"; they are stale artifacts, reported rather than deleted.

### What would unblock this

A fresh restore-verified backup **and a new human approval** over the resulting bindings. The
existing approval cannot authorise a run against a backup taken after it was signed.
