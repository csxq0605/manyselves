# Plan 02 Final Gate Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three reproduced Plan 02 Gate B blockers without expanding Phase 1 scope or starting React.

**Architecture:** FastAPI lifespan retains incomplete shutdown ownership and refuses to construct another Runtime until cleanup finishes. ConversationService owns cancellation-definite store/live transactions with exact compensation and fail-closed consistency handling. Health readiness failures use the existing ErrorEnvelope and an explicit OpenAPI 503 contract.

**Tech Stack:** Python 3.12, asyncio, FastAPI, Pydantic v2, HTTPX ASGI transport, pytest, Ruff, existing RuntimeFacade/RuntimeHost/EventPayload boundaries.

## Global Constraints

- Fix only the three reproduced blockers: lifespan cleanup ownership, conversation store/live compensation, and health 503 contract consistency.
- Preserve the Phase 1 single-process, single-worker, single-Runtime, single-workspace architecture.
- Do not modify `manyselves/core/**`, `manyselves/templates/**`, ProviderManager, prompts, tools, Agent scheduling, Reporting semantics, SSE behavior, workspace APIs, or deployment topology.
- Do not start React Plan 03.
- Reuse `await_owned()`, `RuntimeFacade.fail_consistency()`, `RuntimeConsistencyFailedError`, `ApiError`, and `ErrorEnvelope`; do not duplicate cancellation or error-envelope infrastructure.
- Caller cancellation must not abandon lifecycle cleanup, conversation synchronization, or compensation.
- Do not stage or commit `docs/phase1/implementation-status.md`.
- Run tests in WSL Python 3.12 at `/tmp/manyselves-sanitizer-py312/bin/python`; do not use the Windows `.venv`.
- Each task must complete RED, GREEN, commit, and independent task review before the next task begins.

---

### Task 1: Retain Failed Normal-Shutdown Ownership Across Lifespan Re-entry

**Files:**
- Modify: `manyselves/webapi/lifespan.py`
- Modify: `manyselves/webapi/main.py`
- Modify: `tests/webapi/test_sse.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`

**Interfaces:**
- Produces: private `LifespanCleanupOwnership` with host/facade/service/broker references and completed-stage tracking.
- Produces: `app.state._lifecycle_cleanup_pending: LifespanCleanupOwnership | None`.
- Changes: startup failure and normal shutdown use the same `_cleanup_owned_runtime()` boundary.
- Guarantees: a new host cannot be constructed until prior ownership cleanup completes.

- [ ] **Step 1: Write the failed-shutdown re-entry RED test**

Extend the existing split-shutdown fixtures and replace manual cleanup in `test_shutdown_persistent_producer_failure_retains_downstream_for_retry` with an actual second lifespan attempt. The test must preserve one host factory counter and a release flag:

```python
@pytest.mark.asyncio
async def test_failed_normal_shutdown_blocks_new_runtime_until_cleanup_finishes(
    tmp_path: Path,
) -> None:
    hosts: list[ResourceRuntimeHost] = []
    release_cleanup = False

    def build_host() -> ResourceRuntimeHost:
        host = ResourceRuntimeHost(AppConfig())
        hosts.append(host)
        return host

    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = build_host

    with pytest.raises(RuntimeError, match="producer stop failed"):
        async with app.router.lifespan_context(app):
            old_host = app.state.runtime_host

            async def stop_producers() -> None:
                if not release_cleanup:
                    raise RuntimeError("producer stop failed")

            old_host.stop_producers = stop_producers

    assert len(hosts) == 1
    assert app.state._lifecycle_cleanup_pending is not None
    assert app.state.lifecycle_active is False

    with pytest.raises(RuntimeError, match="Previous lifespan cleanup is incomplete"):
        async with app.router.lifespan_context(app):
            pass
    assert len(hosts) == 1

    release_cleanup = True
    async with app.router.lifespan_context(app):
        assert len(hosts) == 2
        assert app.state.runtime_host is not old_host
```

Place this test in `tests/webapi/test_conversations_agents_reporting.py`, which already imports `WebSettings`, `SecretStr`, `get_runtime_host`, and `ResourceRuntimeHost`. Also assert the old bus/broker remain owned while pending and are closed before the second host becomes ready.

- [ ] **Step 2: Write sibling-close and cancellation RED tests**

Add `test_failed_service_close_attempts_safe_siblings_and_retries_only_pending_stages` with one `ResourceRuntimeHost`, an `events: list[str]`, and wrappers around Reporting, Python, conversation, broker, and bus closes. Make Reporting raise `RuntimeError("reporting close failed")` on its first call only. The first lifespan exit must raise that exact error while still recording Python, conversation, and broker closes; it must not record bus stop. On the second lifespan entry, assert only Reporting is retried, bus stop then runs, the pending slot clears, and only after that does the factory construct host number two.

Add `test_cancelled_pending_shutdown_cleanup_finishes_before_propagating_cancellation` with a failed first lifespan that leaves Reporting pending. Its retrying Reporting close must set `close_started` and wait on `close_release`. Start the second lifespan in a task, wait for `close_started`, cancel the task twice, and assert the task is still pending and the host factory count is one. Set `close_release`; then assert the old cleanup completes, no new host is constructed, the pending slot clears, and the task raises `CancelledError` only after those assertions become true.

Update the existing parametrized `test_shutdown_owned_stage_failure_stops_before_dependencies` to express barriers rather than first-error short-circuiting: begin-shutdown failure prevents all later stages; producer failure prevents every service and bus stage; a Reporting, Python, conversation, or broker failure still attempts every other safe sibling but prevents bus stop; bus failure occurs only after all prior stages complete. Add `broker` to the service-stage parameter cases.

- [ ] **Step 3: Run the lifecycle tests and verify RED**

Run:

```powershell
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_sse.py tests/webapi/test_conversations_agents_reporting.py -q -k "shutdown or cleanup or lifespan or pending or sibling or cancellation"
```

Expected: the re-entry test constructs a second host or lacks `_lifecycle_cleanup_pending`; the sibling test stops at the first close failure; the cancellation test releases ownership early.

- [ ] **Step 4: Generalize lifecycle ownership**

In `lifespan.py`, add:

```python
@dataclass(slots=True)
class LifespanCleanupOwnership:
    host: Any
    facade: RuntimeFacade | None
    reporting: ReportingFacade | None
    python_runs: PythonRunService | None
    conversations: ConversationService | None
    broker: EventBroker | None
    completed: set[str] = field(default_factory=set)
```

Replace `_startup_ownership`, `_expose_startup_ownership`, and `_cleanup_failed_startup` with lifecycle-named equivalents. `_cleanup_owned_runtime(ownership)` must:

- skip stages present in `completed`;
- require `begin_shutdown` and producer stop before downstream cleanup;
- retry producer stop with the existing two-attempt rule;
- attempt Reporting, Python, conversation, and broker closes independently, collecting failures and marking successes complete;
- run bus/legacy host stop only after all prerequisites are complete;
- return all failures plus observed caller cancellation without losing ownership.

Initialize `app.state._lifecycle_cleanup_pending = None` in `create_app()`.

Import and use the existing `manyselves.application.async_ownership.await_owned` primitive for every owned cleanup stage, then remove the lifespan-local `_await_definite` duplicate after all call sites are migrated. If cleanup succeeds after caller cancellation, clear ownership and then raise `CancelledError`; if cleanup also fails, keep the first cleanup failure primary and add only a generic cancellation note.

- [ ] **Step 5: Use the ownership boundary on entry, startup failure, and normal shutdown**

At entry, retry pending ownership before `resolve_runtime_host()`. On failure retain it and deactivate the attempted lifespan. On startup or shutdown failure, store the same ownership object before clearing `lifecycle_active`. Clear exposed resources and the pending slot only after cleanup completes.

Preserve the first cleanup error as the raised error and attach additional stage failures only as notes. Do not stop the bus while producer or service ownership remains incomplete.

Rename the two existing `tests/webapi/test_sse.py` assertions from `_startup_cleanup_pending` to `_lifecycle_cleanup_pending`; for the retained facade assertion, use the dataclass attribute `.facade` instead of dictionary indexing.

- [ ] **Step 6: Run lifecycle GREEN and regression suites**

Run:

```powershell
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_lifecycle.py tests/webapi/test_sse.py tests/webapi/test_conversations_agents_reporting.py -q -k "lifespan or shutdown or cleanup or pending or sibling or cancellation"
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_lifecycle.py tests/webapi/test_sse.py -q
```

Expected: all pass; a failed cleanup owns exactly one old Runtime, second entry cannot construct a host, and successful retry precedes new startup.

- [ ] **Step 7: Commit lifecycle ownership**

```powershell
git add manyselves/webapi/lifespan.py manyselves/webapi/main.py tests/webapi/test_sse.py tests/webapi/test_conversations_agents_reporting.py
git diff --cached --check
git commit -m "fix(webapi): retain failed shutdown ownership"
```

- [ ] **Step 8: Run an independent Task 1 review**

Review only the Task 1 commit against the approved design. Require the reviewer to trace startup failure, normal shutdown failure, retry failure, retry success, and repeated caller cancellation. Do not begin Task 2 until the review reports no Critical or Important finding; fix and recommit any direct Task 1 defect before proceeding.

---

### Task 2: Make Conversation Create, Activate, Delete, and Clear Compensatable

**Files:**
- Modify: `manyselves/application/conversation_service.py`
- Modify: `manyselves/webapi/routes/conversations.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`

**Interfaces:**
- Changes: `ConversationService.create()` becomes async.
- Produces: private operation snapshot covering exact sessions metadata, affected/new JSONL paths, current IDs, affected live histories, and routing maps.
- Produces: one private owned-mutation helper shared by create/activate/delete/clear.
- Consumes: `await_owned()`, `RuntimeFacade.fail_consistency()`, `RuntimeConsistencyFailedError`.

- [ ] **Step 1: Make the HTTP fake expose live-session state**

Extend the existing `_Backend` test fake so successful `sync_agent_conversation()` records:

```python
self.live_sessions: dict[str, str | None] = {}
self.live_histories: dict[str, list[dict]] = {}
self.sync_attempts = 0
self.sync_fail_on_attempt: int | None = None
self.sync_started = asyncio.Event()
self.sync_release: asyncio.Event | None = None
```

Increment `sync_attempts`, set `sync_started`, await `sync_release` when provided, then raise when either `sync_failures_remaining` is non-zero or the current attempt equals `sync_fail_on_attempt`. Only a successful call may replace `live_sessions[agent_type]` and `live_histories[agent_type]`. This lets tests compare actual store and live state and inject a second-Agent failure after the first Agent has already synchronized.

- [ ] **Step 2: Write parametrized store/live compensation RED tests**

Add a parametrized HTTP test for `create`, `activate`, `delete`, and `clear`. Before each request capture:

```python
sessions_before = sessions_path.read_bytes()
files_before = {path: path.read_bytes() for path in conversations_root.rglob("*.jsonl")}
current_before = dict(service.store._current_session_ids)
live_before = dict(host.backend.live_sessions)
```

Set `host.backend.sync_failures_remaining = 1`, execute the mutation, then assert the response fails and every captured value is restored exactly. Also assert no new JSONL remains after create/clear and the deleted target JSONL is restored after delete.

Before capturing `live_before`, call the real `await service._sync(agent_id, clear_pending=True)` once for each affected Agent so the fake starts from a coherent live baseline. Reset `sync_attempts` after seeding. For the shared-delete case, set `sync_fail_on_attempt = 2` and prove that Agent one changes before Agent two fails, then both Agents return to their exact prior live session/history during compensation.

Name the tests:

```python
test_conversation_mutations_restore_exact_store_and_live_state_after_sync_failure
test_delete_shared_active_session_restores_every_affected_agent_after_partial_sync
```

- [ ] **Step 3: Write cancellation and compensation-failure RED tests**

Add `test_cancelled_conversation_mutation_keeps_lease_until_sync_finishes(resources)`. Set `host.backend.sync_release` to a new unset Event, start a create request with `asyncio.create_task`, and wait for `host.backend.sync_started`. Cancel that task twice, then start a second create request using the same lease. Assert both tasks remain incomplete while `sync_release` is unset. Set `sync_release`, assert the first mutation leaves `sessions.json`, the active session ID, and `host.backend.live_sessions["main"]` coherent, then assert the first task raises `CancelledError` and the second request completes only after the first mutation releases the lease-backed transaction.

Add `test_conversation_compensation_failure_fails_runtime_closed(resources, monkeypatch)`. Configure the backend to raise `RuntimeError("sk-sync-secret")` once, and monkeypatch the new durable snapshot-restore helper to raise `RuntimeError("sk-restore-secret")`. Wrap `app.state.runtime_facade.fail_consistency` so an Event proves it was awaited. Execute create, assert that Event is set and `host.is_ready is False`, then assert the exact HTTP 500 error object is `{"code": "RUNTIME_CONSISTENCY_FAILED", "message": "Runtime consistency could not be guaranteed", "retryable": False, "details": {}}` and neither injected secret occurs in `response.text`.

- [ ] **Step 4: Run conversation tests and verify RED**

Run:

```powershell
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_conversations_agents_reporting.py -q -k "sync_failure or compensation or cancelled_conversation or shared_active"
```

Expected: current store/session changes survive failed sync, cancellation ends the owned interval early, or compensation failure becomes `INTERNAL_ERROR` without failing the Runtime closed.

- [ ] **Step 5: Add the operation snapshot**

Keep the existing edit/resend `ConversationTransactionSnapshot` compatible. Add a separate private mutation snapshot in `conversation_service.py` that records:

```python
@dataclass(frozen=True, slots=True)
class ConversationMutationSnapshot:
    sessions_existed: bool
    sessions_bytes: bytes
    existing_jsonl_paths: frozenset[Path]
    affected_files: dict[Path, tuple[bool, bytes]]
    current_session_ids: dict[str, str]
    backend_histories: dict[str, list[dict[str, Any]]]
    loop_histories: dict[str, list[Any] | None]
    streams: dict[tuple[str, str, str], str]
    message_sessions: dict[tuple[str, str], str]
```

Implement exact private helpers with these names:

```python
def _snapshot_mutation(self, affected_agents: set[str], deleted_session_id: str | None = None) -> ConversationMutationSnapshot
async def _restore_mutation_snapshot(self, snapshot: ConversationMutationSnapshot) -> None
async def _run_mutation(self, operation: Callable[[], Awaitable[_T]], snapshot: ConversationMutationSnapshot) -> _T
```

`_snapshot_mutation` must capture every existing `**/{deleted_session_id}.jsonl` path before delete, not only the requesting Agent's file. It captures the old live history for the activating Agent, and every affected Agent history for shared delete. Capture the pre-operation JSONL path set for all four operations so compensation can remove any file created as a side effect.

- [ ] **Step 6: Implement exact restore and fail-closed compensation**

`_restore_mutation_snapshot` must restore sessions bytes/existence, restore every captured per-Agent JSONL, delete every JSONL path absent from `existing_jsonl_paths`, restore current IDs/routing maps, restore every affected loop/backend history and session ID, and fsync the conversation directory.

Wrap the complete internal operation in:

```python
outcome = await await_owned(operation())
if outcome.error is not None:
    raise outcome.error
if outcome.cancellation_requested:
    raise asyncio.CancelledError
return outcome.result()
```

Inside `_run_mutation`, compensate every store/live sync failure. If `_restore_mutation_snapshot` fails, await `facade.fail_consistency()`, add only generic cleanup notes, and raise `RuntimeConsistencyFailedError()` from the restore failure. Perform name/session existence checks before mutation so validation failures do not enter compensation.

- [ ] **Step 7: Move synchronization into the public service methods**

Make create async and perform create/switch/sync inside its owned transaction. Apply the same helper to activate/delete/clear. Delete must sync every affected Agent and compensate already-synced Agents if a later Agent fails.

Update the route to `await service.create(body.name, body.agent_id)` and remove the direct private `_sync()` call. Add `RuntimeConsistencyFailedError` to the route's typed error mapping as HTTP 500, non-retryable.

Update the five direct test call sites in `tests/webapi/test_conversations_agents_reporting.py` from synchronous `service.create(name, agent_id)` calls to `await service.create(name, agent_id)`. The sixth repository call site is the route handled above.

- [ ] **Step 8: Run conversation GREEN and full covering file**

Run:

```powershell
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_conversations_agents_reporting.py -q -k "conversation or session"
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_conversations_agents_reporting.py -q
```

Expected: all pass; every failed operation restores exact durable/live state or makes the Runtime explicitly unavailable with the stable consistency envelope.

- [ ] **Step 9: Commit conversation transactions**

```powershell
git add manyselves/application/conversation_service.py manyselves/webapi/routes/conversations.py tests/webapi/test_conversations_agents_reporting.py
git diff --cached --check
git commit -m "fix(application): make conversations compensatable"
```

- [ ] **Step 10: Run an independent Task 2 review**

Review only the Task 2 commit against the approved transaction matrix. Require explicit inspection of create, activate, clear, single-Agent delete, shared-session multi-Agent delete, repeated caller cancellation, and restore failure. Do not begin Task 3 until the review reports no Critical or Important finding; fix and recommit any direct Task 2 defect before proceeding.

---

### Task 3: Unify Health 503 Runtime and OpenAPI Contracts

**Files:**
- Modify: `manyselves/webapi/routes/health.py`
- Modify: `tests/webapi/test_lifecycle.py`
- Modify: `tests/webapi/test_openapi_contract.py`
- Modify: `frontend-contract/openapi.json`

**Interfaces:**
- Produces: ready 200 status response unchanged.
- Produces: not-ready 503 `ErrorEnvelope` with code `RUNTIME_NOT_READY`.
- Produces: quiesced 503 `ErrorEnvelope` with code `MAINTENANCE_QUIESCED`.
- Produces: explicit OpenAPI 503 response referencing `#/components/schemas/ErrorEnvelope`.

- [ ] **Step 1: Strengthen the health runtime RED tests**

Replace the status-only readiness assertion with the exact envelope:

```python
assert response.status_code == 503
assert response.json()["error"] == {
    "code": "RUNTIME_NOT_READY",
    "message": "Runtime is not ready",
    "retryable": True,
    "details": {},
}
assert response.json()["requestId"]
```

Add `test_ready_is_503_with_quiesced_error_envelope`, using a ready fake host and `SimpleNamespace(quiesced=True)`, and assert code `MAINTENANCE_QUIESCED`, retryable true, and no raw state data.

- [ ] **Step 2: Write the OpenAPI 503 RED test**

Add to `test_openapi_locks_errors_security_sse_and_preview_semantics` or a focused test:

```python
readiness = schema["paths"]["/api/v1/health/ready"]["get"]
assert readiness["responses"]["503"]["content"]["application/json"]["schema"] == {
    "$ref": "#/components/schemas/ErrorEnvelope"
}
```

- [ ] **Step 3: Run health and OpenAPI tests and verify RED**

```powershell
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_lifecycle.py tests/webapi/test_openapi_contract.py -q -k "ready or health or openapi"
```

Expected: runtime returns `{"status":"not_ready"}`/`{"status":"quiesced"}` and the operation lacks an explicit 503 ErrorEnvelope.

- [ ] **Step 4: Raise typed ApiError from the health route**

Remove raw `Response` mutation. Import `ErrorEnvelope`, declare `responses={503: {"model": ErrorEnvelope}}` on the route, and raise:

```python
raise ApiError(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    code="RUNTIME_NOT_READY",
    message="Runtime is not ready",
    retryable=True,
)
```

For maintenance quiesce use code `MAINTENANCE_QUIESCED` and the safe existing maintenance message. Keep the endpoint unauthenticated and keep the ready response unchanged.

- [ ] **Step 5: Export and verify the canonical contract**

```powershell
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python scripts/export_openapi.py frontend-contract/openapi.json
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python scripts/export_openapi.py --check frontend-contract/openapi.json
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi/test_lifecycle.py tests/webapi/test_openapi_contract.py -q
```

Expected: all pass and the artifact contains the explicit 503 ErrorEnvelope.

- [ ] **Step 6: Commit the health contract**

```powershell
git add manyselves/webapi/routes/health.py tests/webapi/test_lifecycle.py tests/webapi/test_openapi_contract.py frontend-contract/openapi.json
git diff --cached --check
git commit -m "fix(webapi): unify readiness error contract"
```

- [ ] **Step 7: Run an independent Task 3 review**

Review only the Task 3 commit. Require comparison of the ready 200 body, both runtime 503 envelopes, the explicit OpenAPI 503 schema, and the generated artifact. Do not begin Task 4 until the review reports no Critical or Important finding; fix and recommit any direct Task 3 defect before proceeding.

---

### Task 4: Re-run Gate B and Perform a Three-Blocker Acceptance Review

**Files:**
- Modify: `docs/phase1/feature-parity.csv`
- Verify: all production/test files from Tasks 1-3
- Do not modify or stage: `docs/phase1/implementation-status.md`

**Interfaces:**
- Consumes: reviewed Tasks 1-3.
- Produces: fresh final-HEAD Gate B evidence and exact named parity references.
- Produces: independent acceptance verdict limited to the three reproduced blockers and direct regressions.

- [ ] **Step 1: Update only exact automated parity evidence**

Add the new named tests only to the existing rows they prove:

- lifecycle single-Runtime cleanup ownership to `API-003`;
- conversation compensated mutations and fail-closed consistency to `API-006`;
- health runtime/OpenAPI envelope agreement to `API-002` or the existing health/API contract row.

Keep React, browser, Electron, deployment, manual, and external evidence empty/pending. Do not mark Phase 1 complete.

- [ ] **Step 2: Run final frozen-code tests sequentially**

```powershell
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/test_types.py tests/interfaces/test_api_debug_message.py tests/test_agent_loop.py tests/core/loops/test_agent_loop_debug.py -q
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/application -q
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi -q
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/application tests/webapi -q
```

Expected: every command exits 0 with no failures.

- [ ] **Step 3: Run final static and contract gates**

```powershell
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python -m ruff check manyselves/application manyselves/webapi manyselves/interfaces/types.py manyselves/core/loops/agent_loop.py tests/application tests/webapi tests/test_types.py tests/test_agent_loop.py tests/core/loops/test_agent_loop_debug.py tests/interfaces/test_api_debug_message.py scripts/export_openapi.py
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- /tmp/manyselves-sanitizer-py312/bin/python scripts/export_openapi.py --check frontend-contract/openapi.json
wsl.exe --cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion -- env GIT_DIR=/mnt/d/yuanxi-algo/manyselves/.git/worktrees/phase1-completion GIT_WORK_TREE=/mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion /tmp/manyselves-sanitizer-py312/bin/python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
git diff --check
git diff --name-only 2a6864c..HEAD -- manyselves/core manyselves/templates
```

Expected: Ruff/OpenAPI/freeze/diff exit 0; protected output remains exactly `manyselves/core/loops/agent_loop.py` from the previously approved exception, with no new Task 1-3 protected changes.

- [ ] **Step 4: Commit the refreshed evidence**

```powershell
git add docs/phase1/feature-parity.csv
git diff --cached --check
git commit -m "test: re-lock phase one gate b"
```

- [ ] **Step 5: Run independent final reviews**

Generate a review package from `3f4c19b` through final HEAD. Require:

1. a whole-cycle implementation review confirming no direct regression and no scope expansion;
2. a Plan 02 acceptance re-review that explicitly reproduces or verifies:
   - failed shutdown blocks second Runtime construction until cleanup succeeds;
   - conversation sync failure restores exact store/live state, and compensation failure fails closed;
   - health 503 runtime bodies equal their OpenAPI ErrorEnvelope contract.

Gate B is accepted only if both reviews report no Critical or Important findings. Update the controller-owned untracked implementation status only after the verdict; do not commit it.
