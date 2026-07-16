# ThreadKeeper persistent workers: behavioral specification

- Status: accepted implementation mandate; architecture baseline
- Date: 2026-07-15
- Branch: `agent/threadkeeper-persistent-workers`
- Base commit: `a2c62eb`

## Purpose and non-goals

ThreadKeeper will support two delegation modes with deliberately different
lifecycle semantics:

1. `delegate`: the existing synchronous, ephemeral, bounded-turn specialist
   call that returns one bounded digest.
2. `spawn-persistent` / `dispatch-worker`: an asynchronous, durable task whose
   execution may span worker processes and host restarts and whose state can be
   queried, cancelled, resumed, and audited.

Persistent mode reuses ThreadKeeper personas, routing, escalation/budget
policy, task contracts, tool sandbox, accounting, transcripts, queue, and
optional adjudication. It is not a second orchestration stack and does not
change `delegate` behavior. Raising the existing eight-turn cap is not a
persistent-worker implementation.

## Entities and ownership

- **Persistent task**: immutable identity, objective, persona/model binding,
  tool capabilities, task contract, budgets, creator/source provenance, and
  creation/expiry times. The parent owns creation and acceptance.
- **Attempt**: one worker claim of a task. It has an attempt ID, lease/claim
  metadata, checkpoint input, start/finish events, usage, and outcome. A task
  may have multiple attempts, but at most one active lease.
- **Checkpoint**: immutable worker-produced continuation state linked to the
  prior checkpoint and attempt. A checkpoint is evidence, not authorization;
  resuming still revalidates current policy and budgets.
- **Event**: append-only lifecycle/audit record with task ID, sequence, event
  ID, timestamp, actor, prior/new state, attempt/checkpoint references, and
  payload digest.
- **Inbox item**: bounded parent-to-worker input linked to a task and source
  event. Delivery is at-least-once; processing is idempotent by inbox item ID.
- **Result candidate**: bounded output plus transcript/evidence references.
  Tasks requiring adjudication are not complete until explicitly accepted.
- **Supervisor**: Python effects adapter that polls, atomically claims, runs,
  checkpoints, and writes receipts. It does not decide legal lifecycle
  transitions or widen capabilities.
- **Lifecycle policy**: MeTTa rules defining legal transitions, terminal
  states, authorization requirements, and budget/intervention verdicts.

All mutable records are scoped by deployment/agent instance and task ID.
ProtoMegaBot and ProtoMegaBot2 must never share a run directory, queue, inbox,
checkpoint store, workspace, memory store, credentials, PID/lock, or logs.

## Lifecycle

Canonical task states:

`CREATED -> QUEUED -> CLAIMED -> RUNNING`

From `RUNNING`, a task may become:

- `CHECKPOINTED -> QUEUED` when more work remains;
- `WAITING_INPUT -> QUEUED` after an authorized inbox item arrives;
- `NEEDS_ADJUDICATION -> COMPLETED` after acceptance;
- `COMPLETED` for a final result that does not require adjudication;
- `CANCEL_REQUESTED -> CANCELLED` at the next cooperative boundary;
- `FAILED_RETRYABLE -> QUEUED` when retry policy and remaining budget allow;
- `FAILED_TERMINAL` when policy denies retry or a hard invariant fails;
- `EXPIRED` when the task or lease expires under policy.

`COMPLETED`, `CANCELLED`, `FAILED_TERMINAL`, and `EXPIRED` are terminal.
No terminal task may return to a runnable state under the same task ID. A new
task linked by `supersedes` is required.

## Legal-transition and recovery invariants

1. A task ID is globally unique within one deployment and never reused.
2. State changes use compare-and-swap semantics over `(task-id, version,
   prior-state)` and append one event. Replays with the same idempotency key
   return the existing receipt.
3. Only `QUEUED` tasks can be claimed. A successful claim creates one attempt
   and lease before provider/tool effects occur.
4. A crash after claim but before terminal receipt leaves a recoverable stale
   attempt. Recovery never assumes the effect did not occur; it records
   `LEASE_EXPIRED`, inspects durable receipts/checkpoints, and either requeues
   under policy or fails for operator review.
5. Provider calls and arbitrary tools are at-least-once across crash recovery.
   Tools with external effects require their own idempotency key/receipt or
   explicit adjudication. The system must not claim exactly-once execution.
6. Queue claim is at-most-once per attempt via atomic rename/lease. Task-level
   completion is effectively-once through versioned terminal transitions and
   result-event idempotency.
7. Cancellation is cooperative. A request is durable immediately; no new
   provider/tool effect may begin after the worker observes it. In-flight
   effects may finish and must be recorded before `CANCELLED`.
8. Checkpoints are immutable, hash-linked, bounded, and written atomically.
   Resume verifies the task contract, persona/config integrity, checkpoint
   chain, current policy, cancellation state, and remaining budgets.
9. Token, tool-call, wall-clock, attempt, retry, queue-age, and optional cost
   budgets are monotone task-level counters. Restart never resets them.
10. Result delivery to the parent is at-least-once by event ID. Parent
    consumption is idempotent, and an acknowledged terminal result is never
    silently overwritten.
11. Every detector has a consumer: cancellation stops new effects; exhausted
    budget blocks claim/resume; lease expiry triggers recovery; adjudication
    status blocks acceptance; invalid lineage fails closed.

## Authorization and capability model

Creation binds the task to a validated persona, explicit tool subset, allowed
paths, forbidden actions, mutation mode, adjudication requirement, and budget
envelope. A worker may narrow but never widen them. Resume uses the current
intersection of original authorization and current deployment policy. Identity
and external message provenance come from authenticated adapters, never parsed
display text. Persistent workers retain the existing exclusions on recursive
delegation, channel sends, parent memory mutation, and arbitrary MeTTa unless a
future policy version explicitly adds a reviewed capability.

## MeTTa/Python boundary

MeTTa owns meaning-bearing decisions:

- legal lifecycle transitions and terminality;
- claim/resume/retry/cancel/adjudication verdicts;
- capability and policy intersection;
- budget eligibility and escalation/routing policy;
- interpretation of progress, failure class, and result acceptance.

Python owns bounded effects:

- deterministic encoding, hashes, timestamps, atomic append/rename/fsync;
- queue/inbox/checkpoint storage and compare-and-swap receipts;
- process launch, leases, signal handling, and supervisor integration;
- provider/tool mechanics and mechanically observed usage/error facts.

Every crossing has a versioned typed input, decision or error atom, and
idempotency key. Python failures return explicit facts; absence or exceptions
never imply permission or success. A Python parity implementation may support
provider-free tests, but MeTTa remains the authoritative policy when enabled.

## Threat model

Relevant threats include forged queue/checkpoint/result files; symlink/path
escape; stale or concurrent workers; replayed claims/results/inbox items;
partial writes and crash windows; task-contract or persona substitution;
budget reset on restart; Unicode/control-character audit forgery; malicious
provider/tool output; indirect mutation through file/shell tools; unauthorized
external messaging; secret leakage; denial of service via unbounded records;
and cross-instance state contamination. Controls include no-follow bounded
reads, atomic writes/renames, checksums/hash chains, leases/version checks,
strict schemas, capability intersections, monotone counters, bounded payloads,
idempotency IDs, explicit adjudication, and deployment-scoped roots.

## Current implementation evidence and gap

At base commit `a2c62eb`, ThreadKeeper already has a strong effects substrate:
queue-only dispatch, immutable task records with SHA-256 sidecars, atomic claim
and retained done/failed artifacts, bounded queue drains, a supervised worker
loop with lock/status metadata, stale-lock reporting, stop files/signals,
dispatch cancellation, task contracts, quotas, token accounting, transcript
records, hash-chained indexes, patch-proposal mode, and adjudication candidates.

The remaining gap is inbox/result delivery and supervisor integration. The
provider-free core now
has stable spawn/status/cancel surfaces, immutable attempt leases and
checkpoint chains, stale-attempt recovery assessment/recording, and an explicit
provider-free requeue effect. It also validates a closed set of positive task
budget limits and keeps idempotent usage deltas in a bounded, hash-linked
task-local ledger. Verified aggregate status reports consumed/remaining limits;
an exhausted limit blocks claim and explicit requeue before queue/provider/tool
effects. Completed queued attempts now create a bounded immutable result receipt
before their token counters are appended to the ledger. If ledger append fails,
a matching claim retry replays the verified receipt without repeating the queue
effect; receipt and token-total tampering fail closed. Recovery intentionally stops at
`FAILED_RETRYABLE`; `requeue_persistent` separately verifies lineage, recreates
the bounded queue record, and CAS-records its digest. Requeue may not be
inferred from an expired lease.

## Phased implementation plan

1. **Lifecycle contract and read-only status**: implemented in
   `src/persistent_worker_lifecycle.metta` and `src/persistent_worker.py`.
   Immutable `threadkeeper.persistent-worker.task-manifest.v1` records and
   append-only, CAS-checked, hash-chained
   `threadkeeper.persistent-worker.event.v1` records project to bounded
   `threadkeeper.persistent-worker.status.v1` single-task/list responses.
   Readers reject unknown versions, invalid transitions, broken sequence/hash
   lineage, oversized files, symlinks, and non-regular records. These storage
   primitives perform no provider, tool, process, queue, or Telegram effects.
2. **Spawn and cancellation surface**: add `spawn-persistent` that creates one
   durable task via the existing validated queue path; add idempotent cancel
   requests and intervention tests. Preserve `delegate` byte-for-byte behavior.
   Implemented provider-free Python surfaces are `spawn_persistent`,
   `cancel_persistent`, and `run_persistent_queued_dispatch`. Spawn reuses
   normal persona/contract/tool-subset/escalation validation but forces the
   existing queue-only boundary. Cancellation durably creates the task-scoped
   queue token before its CAS lifecycle event, and the claim wrapper checks
   both status and token before atomically recording `CLAIMED`. A cancellation
   that wins first therefore prevents both the queue rename and subsequent
   provider/tool effects. Duplicate spawn/cancel IDs return existing state;
   conflicting spawn replays fail closed.
3. **Attempts, checkpoints, and recovery**: partially implemented. A claim now
   creates a versioned, bounded, hash-linked immutable attempt/lease before the
   queued dispatch effect. Versioned checkpoints are atomically created,
   bounded, idempotent by checkpoint ID, payload-hashed, and linked in a
   verified immutable chain. `recovery_assessment` fails closed on active,
   missing, mismatched, or corrupt attempt/checkpoint lineage;
   `recover_stale_attempt` idempotently records a verified expired attempt as
   `FAILED_RETRYABLE`. `requeue_persistent` now performs the separate explicit
   queue effect only after verifying attempt/checkpoint lineage and records the
   queue digest in an idempotent `FAILED_RETRYABLE -> QUEUED` CAS event. A new
   attempt now binds the latest verified checkpoint ID/digest into its immutable
   lease and passes that structured checkpoint to the queued runner; the normal
   runner validates its identity/payload and exposes it as bounded resume
   context. Spawn and explicit requeue now write immutable, bounded,
   manifest-bound enqueue receipts before their lifecycle CAS event. A retry
   after an event-write crash reuses the verified receipt rather than repeating
   the queue effect; corrupt or conflicting receipts fail closed. Enqueue
   operations are serialized per task where file locking is available.
4. **Budgets and inbox/results**: task-level attempt/token/time/tool accounting
   is implemented with bounded immutable usage events, idempotency IDs, attempt
   lineage checks, and pre-claim/requeue exhaustion gates. Completed queued
   attempts persist a self-hashed result receipt and automatically account
   strict input/output/total-token counters. Ledger-write retry reuses that
   receipt and never repeats the queued effect. Next, add idempotent inbox and
   result-delivery acknowledgements, then extend automatic accounting to
   mechanically observed runtime/tool counters as those compact fields become
   available from the queue runner.
5. **Supervisor and ProtoMegaBot2 canary**: provider-free boundary/restart/
   cancellation/intervention suite first; then deploy only to ProtoMegaBot2's
   isolated paths. Live provider/Telegram use remains separately gated.

Promotion requires pure MeTTa truth tables, Python boundary tests, mock-loop
intervention tests, restart/recovery and cancellation tests, budget exhaustion
tests, audit-chain verification, and an isolation audit proving no production
ProtoMegaBot path or process was touched.
