# Plan 02 Final Gate Stabilization Design

## Status

Approved in conversation on 2026-08-02. This specification covers only the three Important findings reproduced by the final Plan 02 acceptance review.

## Goal

Close Gate B by guaranteeing single-Runtime ownership across failed lifespan shutdown, making ordinary conversation mutations definite and compensatable, and aligning health 503 responses with the unified HTTP/OpenAPI error contract.

## Non-goals

- Do not start React Plan 03.
- Do not change protected core, templates, ProviderManager, Agent scheduling, prompts, tools, Reporting behavior, SSE semantics, workspace APIs, or deployment topology.
- Do not introduce a transaction journal, database, second Runtime, multi-worker support, or a new recovery administration surface.
- Do not fix previously accepted Minor findings unless a covering change makes one unavoidable.

## Selected Approach

Use application-layer snapshot compensation and existing owned-task/fail-closed primitives.

This is preferred over live-first persistence because `ConversationStore` persists as part of its existing operations and reversing the order would require protected behavior changes. It is preferred over a journal or two-phase commit because the Phase 1 single-process Runtime already supplies exclusive mutation ownership, exact file snapshots, `await_owned()`, and `RuntimeFacade.fail_consistency()`.

## Architecture

The cycle contains exactly three independently reviewable changes:

1. Generalize lifespan cleanup ownership so failed startup and failed normal shutdown use one pending-cleanup model.
2. Make `ConversationService.create/activate/delete/clear` own store mutation, live synchronization, compensation, and cancellation-definite outcome.
3. Return the existing structured error envelope for health readiness failures and lock its explicit OpenAPI 503 schema.

All three changes remain inside `manyselves/application/**`, `manyselves/webapi/**`, their tests, and the generated OpenAPI artifact. The Runtime remains one process-local instance with one shared MessageBus and one active workspace.

## Lifespan Cleanup Ownership

### State

Replace the startup-specific pending concept with the private lifespan cleanup ownership slot `app.state._lifecycle_cleanup_pending`. It retains the existing host, facade, reporting service, Python-run service, conversation service, broker, and their completion state whenever cleanup does not finish.

`lifecycle_active` prevents concurrent lifespan entries. Clearing that flag does not discard pending ownership.

### Entry flow

At every lifespan entry:

1. acquire the existing lifecycle guard;
2. reject a concurrently active lifespan;
3. mark the entry active;
4. if pending cleanup exists, retry it to a definite outcome before constructing any new host, bus, broker, or service;
5. if retry still fails, retain pending ownership, deactivate this attempted entry, and raise without starting a new Runtime;
6. only after cleanup succeeds may normal startup construct the next Runtime.

### Shutdown flow

Normal shutdown and startup-failure cleanup use one cleanup executor and one ownership record. The executor tracks completed stages so retries are idempotent.

The dependency order is:

1. begin facade shutdown;
2. stop producers to a definite outcome, retaining the existing retry behavior;
3. close Reporting, Python-run, conversation, and broker ownership, attempting every safe sibling close and collecting failures;
4. stop the shared bus only after producers and owned services are closed;
5. clear exposed app-state references only after their owner is complete.

If the producer barrier fails, bus shutdown must not run. If a later sibling close fails, completed siblings remain recorded and are not recreated; the bus remains owned until a retry can finish the required closes. Every failure stores the ownership record before `lifecycle_active` is released.

### Required invariant

At no time may a second host become READY while any prior host, producer, broker, or shared bus remains owned by pending cleanup from the same FastAPI app.

## Conversation Definite Transactions

### Public interface

`ConversationService.create()` becomes async. The four public mutation methods are:

```python
async def create(name: str, agent_id: str = "main") -> dict[str, Any]
async def activate(session_id: str, agent_id: str = "main") -> dict[str, Any]
async def delete(session_id: str, agent_id: str = "main") -> str
async def clear(agent_id: str = "main") -> str
```

Routes continue to hold the existing `RuntimeFacade.mutation_transaction` and call only these public service methods. The create route no longer invokes private `_sync()`.

### Snapshot

Before a store mutation, capture one operation-scoped snapshot containing:

- exact `sessions.json` existence and bytes;
- the in-memory current-session mapping;
- exact existence and bytes for every JSONL file the operation can modify or delete;
- the pre-operation set of conversation JSONL paths so newly created files can be removed;
- backend and loop conversation histories for every affected Agent;
- `_streams` and `_message_sessions` routing maps.

Operation coverage is precise:

- create and clear record the pre-operation path set and remove newly created files during compensation;
- activate restores session metadata/current IDs and the affected Agent live history;
- delete additionally snapshots the target JSONL and every Agent whose active session points at the deleted session.

Snapshot restore writes exact old bytes, removes files that did not previously exist, restores in-memory maps, restores every affected live history, and fsyncs restored files/directories using the existing durability helpers.

### Owned outcome

Each complete store-mutate plus live-sync operation runs through the existing `await_owned()` primitive. Caller cancellation is recorded but cannot interrupt mutation, synchronization, or compensation and cannot release the route mutation transaction early.

Outcome precedence is:

1. committed success, followed by re-raising recorded caller cancellation;
2. operation/sync failure with successful compensation, followed by raising the original operation error;
3. compensation failure, followed by fail-closed consistency handling.

### Compensation failure

If compensation cannot restore both durable and live state:

1. call `RuntimeFacade.fail_consistency()` to stop producers to a definite outcome;
2. keep the Runtime unavailable for further mutation;
3. raise the existing safe `RuntimeConsistencyFailedError` from the compensation error;
4. expose HTTP 500, code `RUNTIME_CONSISTENCY_FAILED`, and `retryable=false` through the existing conversation error boundary;
5. never include durable content, Agent history, API keys, or raw secondary cleanup errors in the public message.

There is no partial-success response. A failed create/delete/activate/clear request either leaves the old durable/live state coherent or leaves the Runtime explicitly failed and non-writable.

## Health Error Contract

The unauthenticated readiness endpoint retains its successful response:

```json
{"status": "ready"}
```

Failure responses use the same `ErrorEnvelope` as every other API error:

- host absent or not ready: HTTP 503, code `RUNTIME_NOT_READY`, safe message, `retryable=true`;
- maintenance quiesced: HTTP 503, code `MAINTENANCE_QUIESCED`, safe message, `retryable=true`.

The route raises `ApiError` rather than mutating a raw `Response`. Its OpenAPI operation declares an explicit 503 `ErrorEnvelope` response, while the 200 response remains the health status object.

## Error and Cancellation Rules

- `CancelledError` never abandons cleanup, conversation sync, or compensation.
- A committed operation is not rolled back merely because the caller disconnected; cancellation is re-raised after the operation reaches its definite committed state.
- An operation failure remains primary when compensation succeeds.
- A compensation failure becomes the safe typed consistency failure and stops producers.
- Lifecycle cleanup errors retain pending ownership; they never authorize a new Runtime.
- New exception notes and HTTP messages must be generic and secret-safe.

## Testing Strategy

### Lifespan

- Inject producer-stop failure during normal shutdown and assert pending ownership is retained.
- Attempt a second lifespan while retry still fails and assert no second host/bus/broker is constructed.
- Release the failure, retry, and assert old cleanup completes before exactly one new Runtime starts.
- Inject a later service-close failure and assert safe sibling cleanup is attempted, bus ownership remains pending, and retry is idempotent.
- Repeat with caller cancellation during failed cleanup.

### Conversations

For create, activate, delete, and clear:

- inject live-sync failure after durable mutation;
- assert exact metadata/file bytes/current IDs/live histories are restored;
- assert newly created files are removed and deleted files are restored;
- inject caller cancellation while sync or compensation is blocked and assert the mutation lock remains owned until a definite outcome;
- inject compensation failure and assert producers stop, Runtime becomes unavailable, and HTTP returns secret-safe `RUNTIME_CONSISTENCY_FAILED`;
- assert successful operations retain their existing response and persistence behavior.

### Health and contract

- assert not-ready and quiesced 503 bodies use the exact `ErrorEnvelope` shape and stable codes;
- assert ready remains HTTP 200 with `{"status": "ready"}`;
- assert OpenAPI declares the explicit 503 envelope and canonical export remains deterministic.

## Gate B Acceptance

After each of the three tasks passes its independent review, run on the frozen final HEAD:

- identity protocol suite;
- complete application suite;
- complete WebAPI suite;
- combined application plus WebAPI suite;
- full Phase 1 Ruff command;
- deterministic OpenAPI export and `--check`;
- explicit Git-path protected-core freeze;
- `git diff --check` and protected-path audit.

The final reviewer rechecks only the three reproduced blockers plus regressions introduced by their fixes. Gate B is accepted only with no Critical or Important findings. React Plan 03 remains blocked until that verdict.

## Scope Fence

Any finding outside these three reproduced blockers is recorded and triaged but does not enter this cycle unless it is a direct regression or a Critical security/data-loss issue. No additional architectural redesign is permitted inside this cycle.
