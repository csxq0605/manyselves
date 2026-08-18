# Runtime Protocol and Provider Stabilization Design

Date: 2026-08-02

## Purpose

Close the two residual Plan 02 review blockers without broadening the Phase 1
architecture. The stabilization must give React one recoverable tool/debug identity
contract and must ensure a successful provider mutation cannot leave a stale
provider instance or credential active.

This work starts a new, explicitly authorized stabilization cycle. It is not a
second fix round under the exhausted Plan 02 final-review wave.

## Current Failure Modes

1. `RuntimeStateProjection` keys tools by Agent ID. Consecutive or concurrent
   calls overwrite one another, and snapshots have no stable tool-call identity,
   arguments, result, or error.
2. `ApiDebugMessage` has no Agent identity, so the projection currently assigns
   every debug record to `main`.
3. Preset synchronization validates a lease inside a mutation transaction but
   performs the actual filesystem write after releasing that transaction.
4. Settings mutations call `LoopManager.restart()`. That method reuses the same
   `ProviderManager`, whose registry retains removed, disabled, or credential-cleared
   provider objects.
5. Settings rollback restores configuration bytes but restart/recovery can still be
   interrupted before the live runtime reaches a definite state.

## Constraints

- Preserve the Phase 1 single-process, single-Runtime architecture.
- Preserve decoding compatibility for legacy in-process messages that do not carry
  the new identity fields.
- Every new production publisher must emit explicit stable identities.
- The only authorized protected-core change is message construction in
  `manyselves/core/loops/agent_loop.py`.
- Do not modify ProviderManager, Agent scheduling, prompts, tools, Reporting core,
  or templates.
- Provider changes rebuild the entire process-local LoopManager; model-only changes
  do not restart it.
- Tool/debug data exposed over HTTP or SSE remains fail-closed and secret-redacted.
- Do not start React Plan 03 until this cycle passes implementation review, automated
  Gate B, and a fresh Plan 02 acceptance review.

## Considered Approaches

### Selected: explicit protocol identity plus application-layer manager replacement

Add compatible identity fields to interface messages, populate them at the actual
AgentLoop publication sites, and propagate the same identity through SSE and the
recoverable snapshot. Reconfigure providers by replacing the complete LoopManager
from the application layer.

This is the smallest approach that removes ambiguity and stale provider objects
without refactoring ProviderManager.

### Rejected: WebAPI-only synthetic correlation

Correlating by Agent, tool name, and arrival order cannot reliably distinguish
same-name concurrent calls, retries, or interleaved results. It would leave the
frontend contract probabilistic.

### Rejected: ProviderManager hot reload

Adding registry clearing, in-place replacement, and rollback to ProviderManager
would create substantially more protected-core state combinations and could affect
the desktop runtime. Phase 1 does not need this broader abstraction.

## Protocol Design

### Tool identity

`ToolCallMessage` and `ToolResult` gain a wire alias `toolCallId`. The decode model
accepts the field as absent for legacy publishers, but every AgentLoop production
path supplies it explicitly.

- Normal LLM tool calls use the provider's existing `tool_call.id`.
- Direct internal tool routes allocate one `tool-{uuid4().hex}` before publishing the
  call and reuse it for every success or failure result.
- A call and its result always carry the same ID.

The EventBroker normalizes legacy messages before invoking either EventMapper or the
runtime-state observer. For a missing call ID it creates a process-local legacy ID
and queues it by `(agent_id, tool_name)`. A missing result ID consumes the oldest
matching outstanding ID. An unmatched legacy result receives an explicit orphan ID
instead of being attached to an unrelated call. This compatibility path is bounded
and is not used by new AgentLoop messages.

### Debug identity

`ApiDebugMessage` gains a wire alias `agentId`. Legacy decoding defaults to `main`.
Both AgentLoop debug publication sites explicitly use `self.agent_type`, so dynamic
Agent records retain their true identity.

### Public event and snapshot identity

EventMapper includes `toolCallId` in tool event payloads and `agentId` in debug
payloads. RuntimeStateProjection keys records by `tool_call_id`, retains a bounded
ordered collection, and stores:

- tool-call ID;
- Agent ID and tool name;
- sanitized-at-output arguments;
- running, completed, or failed status;
- sanitized-at-output result and error.

The broker passes the same normalized message instance to EventMapper and
RuntimeStateProjection. Therefore SSE and the next REST/bootstrap snapshot expose
the same identity, including for a legacy message normalized at the broker boundary.

## Secret-Safety Boundary

RuntimeStateProjection may retain process-local raw values so it can represent the
real accepted state. It must not serialize them directly to a client.

The WebAPI snapshot adapter converts the application RuntimeSnapshot into public
DTOs and applies the existing fail-closed event payload sanitizer separately to
arguments, result, error, and debug error fields. A value the sanitizer cannot
classify safely becomes the redaction sentinel. Tests cover bearer tokens, API keys,
opaque objects, cycles, and deeply nested values.

The application-layer snapshot remains useful to trusted in-process clients, while
the HTTP boundary owns the public security policy.

## Provider Reconfiguration Design

### Settings transaction

SettingsService continues to execute inside the RuntimeFacade mutation transaction.
It captures:

- a deep copy of the current in-memory configuration;
- whether the configuration file existed;
- the configuration file's exact original bytes.

It applies and structurally validates the mutation, persists the new configuration,
and requests a full runtime reconfiguration only for provider-affecting fields.
Model-only changes persist without reconfiguration.

### Full LoopManager replacement

RuntimeHost gains an application-layer operation that replaces the LoopManager for
the current workspace using its existing factory and shared MessageBus.

The operation:

1. owns the RuntimeHost lifecycle lock;
2. stops the current manager to a definite outcome;
3. unbinds it from the host/backend;
4. constructs a fresh LoopManager, which necessarily constructs a fresh
   ProviderManager;
5. binds and starts the replacement;
6. exposes READY only after startup succeeds.

It never calls `LoopManager.restart()` for provider configuration changes and never
reuses the previous provider registry.

### Failure and cancellation recovery

Stop, start, cleanup, and recovery run in owned tasks awaited through shield loops.
Repeated caller cancellation is recorded but cannot abandon an owned transition.

If replacement startup fails:

1. the failed replacement is cleaned to a definite outcome;
2. SettingsService restores the old in-memory configuration;
3. SettingsService restores the exact old file bytes, or removes a newly created
   file when none existed before;
4. RuntimeHost constructs another fresh LoopManager from the restored configuration;
5. the original error remains primary; persistence or runtime recovery errors are
   attached as diagnostics without exposing secrets.

If recovery succeeds, the HTTP mutation fails but the previous configuration is
live on a fresh provider registry. If recovery fails, RuntimeHost remains explicitly
FAILED/unready and the API returns a structured non-retryable consistency error. It
must never return 200 while a stale or uncertain provider remains active.

## Preset Synchronization Ownership

The deployment bearer, control lease, RuntimeFacade mutation transaction, and the
actual `sync_presets` filesystem operation share one ownership interval.

The blocking synchronization runs in a thread task that cannot be abandoned by
caller cancellation. The mutation lock is released only after the thread reaches a
definite success or failure. A competing mutation, maintenance/quiesce request, or
new lease owner cannot write shared state concurrently with an in-flight sync.

## Protected-Core Freeze

The stabilization review must show that the only protected-core path changed from
`2a6864c` is:

`manyselves/core/loops/agent_loop.py`

Allowed edits in that file are limited to supplying `tool_call_id` on tool call and
result messages and `agent_type` on debug messages. No control flow, prompt, provider,
tool, Reporting, scheduling, or persistence behavior may change there.

After an independent diff review accepts that exception, update
`docs/phase1/core-freeze-base.txt` to the new audited hash. The review report records
the old base commit, new commit, exact protected file, and reason. The regular freeze
script must then pass with every other protected hash unchanged.

## Testing Strategy

### Protocol and projection

- Two same-name calls have distinct IDs and retain separate records.
- Each success/failure result updates only its matching call.
- Provider IDs and direct-route generated IDs are propagated unchanged.
- Legacy missing-ID call/result pairs remain decodable and receive one consistent
  bounded compatibility ID; unmatched results cannot attach to another call.
- Dynamic Agent debug messages remain attributed to their Agent.
- SSE and REST snapshots expose identical call IDs.
- Public snapshot arguments, result, and errors redact secrets and unsafe objects.

### Provider lifecycle

- Clearing, disabling, removing, or changing the active provider produces a new
  LoopManager and ProviderManager.
- The new registry contains only currently enabled/configured providers.
- The next Agent request cannot reach an old provider object or credential.
- Model-only changes do not replace the manager.
- Replacement failure restores old memory, exact YAML bytes, and a newly constructed
  old-configuration runtime.
- Cancellation injected during stop, replacement start, failed-instance cleanup,
  and recovery cannot leave an indeterminate host state.

### Preset ownership

- Invalid leases fail before synchronization starts.
- A second mutation and maintenance/quiesce wait while synchronization owns the
  mutation transaction.
- Caller cancellation waits for thread completion before releasing ownership.

### Final verification

- focused interface/AgentLoop tests;
- focused application lifecycle/settings/projection tests;
- focused WebAPI settings/SSE/OpenAPI tests;
- all application and WebAPI suites separately and combined;
- Ruff and deterministic OpenAPI artifact check;
- protected-path diff audit showing only the authorized AgentLoop file;
- updated protected-core freeze check;
- independent implementation review followed by a fresh Plan 02 acceptance review.

## Non-Goals

- Multi-process Runtime coordination.
- ProviderManager hot reload.
- Changes to Agent prompts, tool behavior, Reporting orchestration, or templates.
- React implementation before Plan 02 acceptance.
- Database, Redis, Milvus, MinIO, tenant, or deployment-topology work.

## Acceptance

The cycle is complete only when no old provider instance can be used after a
successful provider mutation, tool/debug identities recover consistently across SSE
and REST, preset writes stay inside mutation ownership, all automated gates pass,
the minimal protected-core exception is independently accepted, and Plan 02 receives
a clean fresh acceptance review.
