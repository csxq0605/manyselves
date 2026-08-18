# Runtime Protocol and Provider Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining Plan 02 acceptance blockers by giving tool/debug events stable recoverable identity, replacing stale provider registries transactionally, and keeping preset writes inside mutation ownership.

**Architecture:** New AgentLoop messages explicitly carry tool-call and Agent identity while legacy messages remain decodable and are normalized once at the EventBroker boundary. RuntimeHost replaces the complete LoopManager for provider changes, and SettingsService owns persistence, replacement, and recovery to a definite outcome. Public snapshots reuse the existing fail-closed sanitizer before exposing tool/debug values.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, FastAPI, pytest, Ruff, existing RuntimeHost/RuntimeFacade/EventBroker boundaries.

## Global Constraints

- Preserve the Phase 1 single-process, single-Runtime architecture.
- Legacy messages may omit the new identity fields; every new AgentLoop production publisher must set them explicitly.
- The only authorized protected-core change is message construction in `manyselves/core/loops/agent_loop.py`.
- Do not modify ProviderManager, prompts, tools, Agent scheduling, Reporting core, or templates.
- Provider-affecting mutations replace the complete LoopManager; model-only mutations do not restart.
- HTTP/SSE tool and debug data must pass through the existing fail-closed sanitizer.
- Do not stage or commit `docs/phase1/implementation-status.md`.
- Do not start React Plan 03 until this plan and a fresh Plan 02 acceptance review pass.

---

### Task 1: Add Compatible Tool and Debug Identity at the Production Source

**Files:**
- Modify: `manyselves/interfaces/types.py`
- Modify: `manyselves/core/loops/agent_loop.py`
- Modify: `tests/test_types.py`
- Modify: `tests/test_agent_loop.py`
- Modify: `tests/core/loops/test_agent_loop_debug.py`
- Modify: `tests/interfaces/test_api_debug_message.py`

**Interfaces:**
- Produces: `ToolCallMessage.tool_call_id: str | None`, `ToolResult.tool_call_id: str | None`, and `ApiDebugMessage.agent_type: AgentId` with JSON aliases `toolCallId` and `agentId`.
- Guarantees: every AgentLoop-produced call/result pair has one non-empty shared ID; every AgentLoop-produced debug message has the publishing Agent ID.

- [ ] **Step 1: Write protocol compatibility and serialization tests**

Add tests that exercise both Python field names and wire aliases:

```python
def test_tool_identity_is_optional_for_legacy_decode_and_serializes_by_alias():
    legacy = ToolCallMessage(agent_type="main", tool_name="read", arguments={})
    assert legacy.tool_call_id is None

    current = ToolCallMessage.model_validate({
        "agent_type": "main",
        "tool_name": "read",
        "arguments": {},
        "toolCallId": "call-1",
    })
    assert current.tool_call_id == "call-1"
    assert current.model_dump(mode="json", by_alias=True)["toolCallId"] == "call-1"


def test_debug_identity_defaults_for_legacy_and_serializes_by_alias():
    legacy = ApiDebugMessage(
        model="m", tokens_in=1, tokens_out=2, duration_ms=3, status="success"
    )
    assert legacy.agent_type == "main"

    current = ApiDebugMessage(
        agent_type="dynamic-agent",
        model="m", tokens_in=1, tokens_out=2, duration_ms=3, status="success",
    )
    assert current.model_dump(mode="json", by_alias=True)["agentId"] == "dynamic-agent"
```

- [ ] **Step 2: Write AgentLoop publication tests**

Extend existing direct-report and provider tool-call tests to collect both message types and assert correlation:

```python
calls = [item for item in published if isinstance(item, ToolCallMessage)]
results = [item for item in published if isinstance(item, ToolResultMsg)]
assert calls[0].tool_call_id
assert results[0].tool_call_id == calls[0].tool_call_id
```

In the provider tool-call fixture, also assert the published ID equals the fake provider's `tool_call.id`. In debug tests, construct a non-main AgentLoop and assert every collected `ApiDebugMessage.agent_type` equals that loop's Agent ID.

- [ ] **Step 3: Run the identity tests and verify RED**

Run:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/test_types.py tests/interfaces/test_api_debug_message.py tests/test_agent_loop.py tests/core/loops/test_agent_loop_debug.py -q
```

Expected: the new tests fail because the identity fields do not exist and AgentLoop does not propagate provider/direct-call identity.

- [ ] **Step 4: Add compatible interface fields**

Use validation and serialization aliases without changing Message-wide alias behavior:

```python
tool_call_id: str | None = Field(
    default=None,
    validation_alias=AliasChoices("tool_call_id", "toolCallId"),
    serialization_alias="toolCallId",
)
```

Add the field to both tool message classes. Add the following to `ApiDebugMessage`:

```python
agent_type: AgentId = Field(
    default="main",
    validation_alias=AliasChoices("agent_type", "agentId"),
    serialization_alias="agentId",
)
```

- [ ] **Step 5: Propagate identity at every AgentLoop publication site**

- For provider tool calls, pass `tool_call.id` to the call and every success/failure result branch.
- Add `from uuid import uuid4` to AgentLoop, then for the direct reporting resume route allocate `tool_call_id = f"tool-{uuid4().hex}"` once before publishing and reuse it for all results.
- Pass `agent_type=self.agent_type` at both ApiDebugMessage publication sites.
- Do not change surrounding control flow or provider/tool behavior.

- [ ] **Step 6: Run focused tests and protected-scope audit**

Run the Step 3 command again and expect PASS. Then run:

```powershell
git diff --name-only 2a6864c -- manyselves/core manyselves/templates
git diff --check
```

Expected: the protected-path output is exactly `manyselves/core/loops/agent_loop.py`; diff check is clean.

- [ ] **Step 7: Commit the protocol source change**

```powershell
git add manyselves/interfaces/types.py manyselves/core/loops/agent_loop.py tests/test_types.py tests/test_agent_loop.py tests/core/loops/test_agent_loop_debug.py tests/interfaces/test_api_debug_message.py
git diff --cached --check
git commit -m "feat(core): correlate tool and debug events"
```

---

### Task 2: Correlate Legacy Events and Expose a Sanitized Recoverable Snapshot

**Files:**
- Create: `manyselves/webapi/events/identity.py`
- Create: `manyselves/webapi/schemas/runtime.py`
- Modify: `manyselves/application/models.py`
- Modify: `manyselves/application/runtime_state.py`
- Modify: `manyselves/webapi/events/broker.py`
- Modify: `manyselves/webapi/events/mapper.py`
- Modify: `manyselves/webapi/schemas/bootstrap.py`
- Modify: `manyselves/webapi/routes/bootstrap.py`
- Modify: `manyselves/webapi/schemas/agents.py`
- Modify: `manyselves/webapi/routes/agents.py`
- Modify: `tests/application/test_runtime_facade.py`
- Modify: `tests/webapi/test_event_mapper.py`
- Modify: `tests/webapi/test_sse.py`
- Modify: `tests/webapi/test_lifecycle.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`
- Modify: `tests/webapi/test_openapi_contract.py`

**Interfaces:**
- Produces: `ToolIdentityNormalizer.normalize(message: Message) -> Message`.
- Produces: bounded `RuntimeToolSnapshot` rows keyed by `tool_call_id` with arguments/result/error.
- Produces: public `RuntimeSnapshotResponse.from_runtime(snapshot)` and sanitized Agent debug entries.
- Consumes: Task 1 identity fields and the existing `EventPayloadSanitizer`.

- [ ] **Step 1: Write legacy normalization tests**

Add focused broker tests using a fixed stream ID:

```python
legacy_call = ToolCallMessage(agent_type="main", tool_name="read", arguments={"path": "a"})
legacy_result = ToolResult(agent_type="main", tool_name="read", result="ok")
await broker.publish_internal(legacy_call)
await broker.publish_internal(legacy_result)
events = broker.replay.snapshot()
assert events[0].payload["toolCallId"]
assert events[1].payload["toolCallId"] == events[0].payload["toolCallId"]
```

Add cases for two same-name outstanding calls, explicit production IDs, and an unmatched legacy result. Explicit IDs must never be replaced; unmatched results must receive a distinct orphan ID.

- [ ] **Step 2: Write projection and public-sanitization tests**

Exercise two same-name calls and their out-of-order explicit results:

```python
projection.observe(ToolCallMessage(
    agent_type="main", tool_name="read", tool_call_id="call-1",
    arguments={"path": "a"},
))
projection.observe(ToolCallMessage(
    agent_type="main", tool_name="read", tool_call_id="call-2",
    arguments={"path": "b"},
))
projection.observe(ToolResult(
    agent_type="main", tool_name="read", tool_call_id="call-2",
    result={"value": 2},
))
rows = {item.tool_call_id: item for item in projection.build(manager, {})["tools"]}
assert set(rows) == {"call-1", "call-2"}
assert rows["call-1"].status == "running"
assert rows["call-2"].result == {"value": 2}
```

Add HTTP bootstrap/debug tests containing bearer tokens, API keys, an opaque object, a cycle, and a depth-limit value. Assert the public JSON contains the redaction sentinel and never contains the secrets or object repr. Add a dynamic-Agent ApiDebugMessage and assert it appears only under that Agent.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/application/test_runtime_facade.py tests/webapi/test_event_mapper.py tests/webapi/test_sse.py tests/webapi/test_lifecycle.py tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py -q
```

Expected: failures show missing correlation, overwritten tools, missing public fields, and unsanitized/raw snapshot values.

- [ ] **Step 4: Implement the bounded identity normalizer**

In `identity.py`, maintain bounded FIFO queues by `(agent_id, tool_name)` and a monotonically increasing legacy counter. `normalize()` returns the original message when an explicit ID exists; otherwise it returns `message.model_copy(update={"tool_call_id": resolved_id})`.

The normalizer must remove evicted IDs from outstanding queues and must never attach an unmatched result to another tool name or Agent.

- [ ] **Step 5: Normalize once before mapping and observation**

Give EventBroker one normalizer instance and change publication order to:

```python
message = self._identity_normalizer.normalize(message)
context = self._context_resolver(message, sequence)
event = self._mapper.map(message, context=context)
if self._observer is not None:
    self._observer(message)
```

Update EventMapper tool/debug payloads to include `toolCallId` and `agentId`.

- [ ] **Step 6: Expand the recoverable application model**

Change `RuntimeToolSnapshot` to contain:

```python
tool_call_id: str
agent_id: str
name: str
arguments: Any
status: Literal["running", "completed", "failed"]
result: Any = None
error: str | None = None
```

Change RuntimeStateProjection to an insertion-ordered, bounded `dict[tool_call_id, RuntimeToolSnapshot]`. Calls insert/update only their ID; results update only their ID. Add `error` and the correct `agent_id` to debug rows.

- [ ] **Step 7: Add the public snapshot adapter**

Define strict WebAPI DTOs in `schemas/runtime.py`. Map application rows with one `EventPayloadSanitizer`:

```python
sanitizer = EventPayloadSanitizer()
arguments = sanitizer.sanitize_field("arguments", item.arguments)
result = sanitizer.sanitize_field("result", item.result)
error = sanitizer.sanitize_field("error", item.error)
```

Use the public DTO as `BootstrapSnapshot.runtime`. Apply the same sanitizer to Agent debug error fields. Do not mutate the internal application snapshot.

- [ ] **Step 8: Run focused suites and OpenAPI contract tests**

Run the Step 3 command and expect PASS, then export and check the updated DTO contract:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python scripts/export_openapi.py frontend-contract/openapi.json
/tmp/manyselves-sanitizer-py312/bin/python scripts/export_openapi.py --check frontend-contract/openapi.json
```

Expected: PASS; generated components contain required `toolCallId`/`agentId`, and no secret or machine path appears in the artifact.

- [ ] **Step 9: Commit recoverable identity**

```powershell
git add manyselves/application/models.py manyselves/application/runtime_state.py manyselves/webapi/events/identity.py manyselves/webapi/events/broker.py manyselves/webapi/events/mapper.py manyselves/webapi/schemas/runtime.py manyselves/webapi/schemas/bootstrap.py manyselves/webapi/routes/bootstrap.py manyselves/webapi/schemas/agents.py manyselves/webapi/routes/agents.py tests/application/test_runtime_facade.py tests/webapi/test_event_mapper.py tests/webapi/test_sse.py tests/webapi/test_lifecycle.py tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py frontend-contract/openapi.json
git diff --cached --check
git commit -m "feat(webapi): recover correlated runtime events"
```

---

### Task 3: Replace the Complete LoopManager for Provider Mutations

**Files:**
- Create: `manyselves/application/async_ownership.py`
- Modify: `manyselves/application/runtime_host.py`
- Modify: `manyselves/application/settings_service.py`
- Modify: `manyselves/application/backend_api.py`
- Modify: `manyselves/webapi/routes/settings.py`
- Modify: `tests/application/test_runtime_host.py`
- Modify: `tests/application/test_pyqt_runtime_compatibility.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`

**Interfaces:**
- Produces: `RuntimeHost.replace_loop_manager(*, recovery: bool = False) -> None`.
- Produces: owned-task outcome helper that records caller cancellation without abandoning the task.
- Produces: `to_thread_non_abandoning(function, *args, **kwargs)` built on the same owned-task outcome primitive.
- Changes: SettingsService receives RuntimeHost rather than calling `LoopManager.restart()` through BackendAPI.

- [ ] **Step 1: Write fresh-manager and stale-provider tests**

Use a factory that creates distinct managers with distinct provider registries. Start the host, mutate the active provider, and assert:

```python
previous = host.loop_manager
await service.mutate(change_provider, restart_reason="provider_configuration_changed")
assert host.loop_manager is not previous
assert host.loop_manager.provider_registry is not previous.provider_registry
assert "removed-provider" not in host.loop_manager.provider_registry
```

Cover clearing credentials, disabling a provider, removing the active provider, and changing its connection fields. Add a model-only test asserting the factory count and manager identity do not change.

- [ ] **Step 2: Write failure recovery and repeated-cancellation tests**

Create controlled fake managers whose stop/start methods wait on Events. Cover cancellation during old-manager stop, replacement start, failed-replacement cleanup, and restored-manager start.

For replacement failure, assert:

```python
assert manager.config == old_config
assert config_path.read_bytes() == original_bytes
assert host.is_ready
assert host.loop_manager is recovery_manager
assert host.loop_manager is not previous_manager
```

When recovery also fails, assert host is not ready and the raised error contains a recovery diagnostic without any API key value.

- [ ] **Step 3: Run lifecycle/settings tests and verify RED**

Run:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/application/test_runtime_host.py tests/application/test_pyqt_runtime_compatibility.py tests/webapi/test_conversations_agents_reporting.py -q
```

Expected: failures prove the current path reuses LoopManager/ProviderManager and cancellation can interrupt recovery.

- [ ] **Step 4: Implement owned-task outcome handling**

In `async_ownership.py`, create a helper that starts one task, shields it until done, temporarily consumes repeated caller cancellation, and returns both the task result/error and whether cancellation was requested. It must not convert a successful committed transition into a rollback.

The caller re-raises `CancelledError` only after it has decided whether the configuration committed or recovered.
Build `to_thread_non_abandoning()` by passing `asyncio.to_thread(function, *args, **kwargs)` through the same primitive, so Task 4 does not duplicate cancellation logic.

- [ ] **Step 5: Implement RuntimeHost replacement and recovery modes**

`replace_loop_manager()` must:

- hold `_lifecycle_lock`;
- retain the resolved workspace even while no manager is bound;
- allow normal replacement only from READY;
- allow `recovery=True` from FAILED after the old configuration is restored;
- stop and unbind the old manager;
- construct a new manager through `_loop_manager_factory`;
- start it and set READY only after success;
- clean a failed candidate and remain FAILED if startup does not complete.

It must not stop the shared MessageBus and must not call `LoopManager.restart()`.

- [ ] **Step 6: Make SettingsService coordinate commit or recovery**

Change SettingsService to hold RuntimeHost. Its provider path is:

```python
before = manager.config.model_copy(deep=True)
persisted = capture_exact_bytes()
mutation(manager.config)
manager.save_config()
outcome = await await_owned(host.replace_loop_manager())
```

If replacement fails, restore memory and exact bytes, then await an owned
`host.replace_loop_manager(recovery=True)`. The replacement error stays primary.
If replacement succeeds but caller cancellation was recorded, leave the committed
configuration live and re-raise cancellation after the transaction reaches a
definite state.

- [ ] **Step 7: Remove the stale restart path from Web settings**

Construct SettingsService with the RuntimeHost. Provider-affecting fields call the
new replacement path. Model-only fields retain the no-restart compatibility rule.
Keep `BackendAPIImpl.restart_agents()` for the existing desktop event contract, but
do not use `restart_agents_and_wait()` for Web settings.

- [ ] **Step 8: Run focused lifecycle and compatibility suites**

Run the Step 3 command and expect PASS. Also run:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/application/test_runtime_facade.py tests/webapi/test_lifecycle.py -q
```

Expected: PASS with one shared bus, a fresh manager for provider changes, no manager replacement for model-only changes, and definite recovery under repeated cancellation.

- [ ] **Step 9: Commit transactional provider replacement**

```powershell
git add manyselves/application/async_ownership.py manyselves/application/runtime_host.py manyselves/application/settings_service.py manyselves/application/backend_api.py manyselves/webapi/routes/settings.py tests/application/test_runtime_host.py tests/application/test_pyqt_runtime_compatibility.py tests/webapi/test_conversations_agents_reporting.py tests/application/test_runtime_facade.py tests/webapi/test_lifecycle.py
git diff --cached --check
git commit -m "fix(application): rebuild providers transactionally"
```

---

### Task 4: Keep Preset Writes Inside Mutation Ownership

**Files:**
- Modify: `manyselves/webapi/routes/settings.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`

**Interfaces:**
- Consumes: Task 3 owned thread/task helper and RuntimeFacade mutation transaction.
- Guarantees: lease validation, mutation ownership, and the complete `sync_presets` write share one interval.

- [ ] **Step 1: Write mutation-exclusion and cancellation tests**

Patch `sync_presets` with a blocking function controlled by threading Events. Start the HTTP sync request, wait until the worker enters, then start another mutation and assert it cannot enter before the sync worker is released.

Cancel the sync request while the worker is blocked, release it, and assert the worker completes before the second mutation acquires ownership. Retain the invalid-lease 423 test.

- [ ] **Step 2: Run preset ownership tests and verify RED**

Run:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_conversations_agents_reporting.py -q -k "preset and (lease or mutation or cancellation)"
```

Expected: the second mutation enters while the current implementation performs the actual sync outside the transaction, or cancellation abandons transaction ownership early.

- [ ] **Step 3: Move the real write into the transaction**

Keep the transaction open around the non-abandoning thread call:

```python
async with request.app.state.runtime_facade.mutation_transaction(lease_token):
    downloaded = await to_thread_non_abandoning(sync_presets)
```

The helper must wait for the worker's definite result before propagating caller cancellation. Preserve the existing sanitized `PRESET_SYNC_FAILED` envelope.

- [ ] **Step 4: Run focused and full settings tests**

Run the Step 2 command and then:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_conversations_agents_reporting.py -q
```

Expected: PASS; invalid leases fail before the worker starts, mutation ownership spans the complete write, and cancellation cannot release ownership early.

- [ ] **Step 5: Commit preset ownership**

```powershell
git add manyselves/webapi/routes/settings.py tests/webapi/test_conversations_agents_reporting.py
git diff --cached --check
git commit -m "fix(webapi): serialize preset synchronization"
```

---

### Task 5: Audit the Core Exception and Re-clear Gate B

**Files:**
- Modify: `docs/phase1/core-freeze-base.txt`
- Modify: `docs/phase1/feature-parity.csv`
- Modify: `frontend-contract/openapi.json`
- Create: `docs/phase1/core-protocol-exception.md`
- Test: all focused and Gate B suites from Tasks 1–4

**Interfaces:**
- Consumes: all stabilization commits and their independent task reviews.
- Produces: an audited new protected-core baseline, current OpenAPI artifact, accurate parity evidence, and fresh Plan 02 acceptance evidence.

- [ ] **Step 1: Audit the protected-core range before changing the baseline**

Run:

```powershell
git diff --name-only 2a6864c..HEAD -- manyselves/core manyselves/templates
git diff 2a6864c..HEAD -- manyselves/core/loops/agent_loop.py
```

Expected: the name list contains exactly `manyselves/core/loops/agent_loop.py`; the diff contains only identity arguments at ToolCallMessage, ToolResult, and ApiDebugMessage construction sites.

Record base `2a6864c`, the reviewed stabilization commit range, the exact allowed file, the approved reason, and the reviewer conclusion in `docs/phase1/core-protocol-exception.md`.

- [ ] **Step 2: Update the freeze base to the audited implementation commit**

Run `git rev-parse HEAD`, replace the single line in `docs/phase1/core-freeze-base.txt` with that full 40-character commit ID, and immediately run:

```powershell
GIT_DIR=/mnt/d/yuanxi-algo/manyselves/.git/worktrees/phase1-completion GIT_WORK_TREE=/mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion /tmp/manyselves-sanitizer-py312/bin/python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: exit 0. Do not update the baseline if the independent protected-core review is not clean.

- [ ] **Step 3: Refresh contract and parity evidence**

Export the schema, confirm it is current, and update only the API/SSE/runtime parity rows supported by named automated tests:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python scripts/export_openapi.py frontend-contract/openapi.json
/tmp/manyselves-sanitizer-py312/bin/python scripts/export_openapi.py --check frontend-contract/openapi.json
```

Expected: both commands pass and the artifact exposes required `toolCallId`, `agentId`, sanitized runtime snapshot DTOs, existing security schemes, and existing SSE stream identity.

- [ ] **Step 4: Run frozen-code verification**

Run sequentially in the WSL Python 3.12 environment:

```powershell
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/test_types.py tests/interfaces/test_api_debug_message.py tests/test_agent_loop.py tests/core/loops/test_agent_loop_debug.py -q
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/application -q
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi -q
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/application tests/webapi -q
/tmp/manyselves-sanitizer-py312/bin/python -m ruff check manyselves/application manyselves/webapi manyselves/interfaces/types.py manyselves/core/loops/agent_loop.py tests/application tests/webapi tests/test_types.py tests/test_agent_loop.py tests/core/loops/test_agent_loop_debug.py tests/interfaces/test_api_debug_message.py scripts/export_openapi.py
/tmp/manyselves-sanitizer-py312/bin/python scripts/export_openapi.py --check frontend-contract/openapi.json
```

Then run the explicit Git-path freeze command from Step 2, `git diff --check`, and protected-path audits.

Expected: every command exits 0; no protected file other than the reviewed AgentLoop path differs from `2a6864c`; `docs/phase1/implementation-status.md` remains untracked and unstaged.

- [ ] **Step 5: Commit the audited gate**

```powershell
git add docs/phase1/core-freeze-base.txt docs/phase1/core-protocol-exception.md docs/phase1/feature-parity.csv frontend-contract/openapi.json
git diff --cached --check
git commit -m "test: re-lock phase one runtime contract"
```

- [ ] **Step 6: Run independent final reviews**

Generate one review package for the complete stabilization range. Require:

1. an implementation review that confirms protocol correlation, secret safety,
   fresh manager replacement, cancellation-definite recovery, preset ownership, and
   exact protected-core scope;
2. a fresh Plan 02 acceptance review against the locked Phase 1 plan and the updated
   OpenAPI/freeze evidence.

React Plan 03 may begin only when both reviews report no Critical or Important findings.
