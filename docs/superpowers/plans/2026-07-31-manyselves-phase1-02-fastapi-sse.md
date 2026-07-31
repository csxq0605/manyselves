# Manyselves Phase 1 FastAPI and SSE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the shared Runtime through a versioned, secure, tested REST and recoverable SSE service while keeping one process-local Runtime.

**Architecture:** FastAPI lifespan creates one `RuntimeHost` and `RuntimeFacade`. Route modules translate HTTP DTOs to application commands; project/filesystem services remain outside core; `EventBroker` maps MessageBus messages into stable envelopes with bounded replay.

**Tech Stack:** FastAPI, Pydantic 2, Uvicorn, Gunicorn, uvicorn-worker, HTTPX, pytest, pytest-asyncio.

## Global Constraints

- API prefix is `/api/v1` and errors use one structured envelope.
- One process, one worker, one Runtime, one active workspace.
- Mutations require a valid deployment token and control lease.
- Filesystem APIs cannot resolve outside the selected project root.
- SSE is resumable but non-authoritative; REST snapshots repair missed state.
- Do not serialize internal `Message` objects directly.

---

### Task 1: Add Server Dependencies, Settings, Factory, and Lifespan

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `manyselves/webapi/__init__.py`
- Create: `manyselves/webapi/routes/__init__.py`
- Create: `manyselves/webapi/schemas/__init__.py`
- Create: `manyselves/webapi/settings.py`
- Create: `manyselves/webapi/dependencies.py`
- Create: `manyselves/webapi/lifespan.py`
- Create: `manyselves/webapi/main.py`
- Create: `manyselves/webapi/schemas/bootstrap.py`
- Create: `manyselves/webapi/routes/bootstrap.py`
- Create: `tests/webapi/test_lifecycle.py`

**Interfaces:**
- Consumes: `RuntimeHost.create()`, `RuntimeFacade`.
- Produces: `create_app(settings: WebSettings | None = None) -> FastAPI`; app state keys `runtime_host`, `runtime_facade`, `event_broker`.

- [ ] **Step 1: Write lifespan and health tests**

```python
@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_one_runtime(web_settings, fake_runtime_host):
    app = create_app(web_settings)
    app.dependency_overrides[get_runtime_host] = lambda: fake_runtime_host
    async with LifespanManager(app):
        assert fake_runtime_host.start_count == 1
        assert app.state.runtime_facade is not None
    assert fake_runtime_host.stop_count == 1


async def test_ready_is_503_until_runtime_is_ready(async_client):
    response = await async_client.get("/api/v1/health/ready")
    assert response.status_code == 503


async def test_bootstrap_returns_one_coherent_snapshot(ready_client):
    response = await ready_client.get("/api/v1/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert {"runtime", "project", "conversations", "agents", "settings"} <= set(body)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/webapi/test_lifecycle.py -q`
Expected: FAIL because `manyselves.webapi` does not exist.

- [ ] **Step 3: Add dependencies and implement the app factory**

Run:

```powershell
uv add fastapi "uvicorn[standard]" gunicorn uvicorn-worker python-multipart
```

Define settings exactly:

```python
class WebSettings(BaseSettings):
    data_root: Path
    initial_project_id: str
    access_token: SecretStr
    allowed_origins: list[str] = []
    sse_replay_capacity: int = 2000
    sse_client_queue_capacity: int = 500
    control_lease_seconds: int = 60
```

`create_app()` registers only `/api/v1` routes, request-id middleware, configured CORS origins, and the application lifespan. It exposes a coherent bootstrap DTO assembled under the RuntimeFacade read lock. Export `app = create_app()` for Gunicorn; constructing the FastAPI object at import time is allowed, but no Runtime, workspace, bus task, or provider is created until lifespan starts.

- [ ] **Step 4: Run focused tests and direct startup smoke**

Run:

```powershell
uv run pytest tests/webapi/test_lifecycle.py -q
uv run ruff check manyselves/webapi tests/webapi
uv run uvicorn manyselves.webapi.main:create_app --factory --host 127.0.0.1 --port 8010
```

Expected: tests PASS; the smoke process reaches application startup and reports a clear configuration error when required environment values are absent. Stop it after confirming startup behavior.

- [ ] **Step 5: Commit the server composition root**

```powershell
git add pyproject.toml uv.lock manyselves/webapi tests/webapi/test_lifecycle.py
git diff --cached --check
git commit -m "feat: add fastapi runtime host"
```

### Task 2: Add Error Envelope, Deployment Authentication, and Control Routes

**Files:**
- Create: `manyselves/webapi/errors.py`
- Create: `manyselves/webapi/security.py`
- Create: `manyselves/webapi/schemas/common.py`
- Create: `manyselves/webapi/schemas/control.py`
- Create: `manyselves/webapi/routes/health.py`
- Create: `manyselves/webapi/routes/control.py`
- Create: `tests/webapi/test_security_and_control.py`

**Interfaces:**
- Consumes: `ControlLeaseService` and `WebSettings.access_token`.
- Produces: `ErrorEnvelope`, bearer-token dependency, lease acquire/heartbeat/release endpoints.

- [ ] **Step 1: Write authentication and lease tests**

```python
async def test_mutation_rejects_missing_access_token(async_client):
    response = await async_client.post("/api/v1/control/lease", json={"clientId": "c-1"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_second_client_receives_lease_conflict(authed_client):
    first = await authed_client.post("/api/v1/control/lease", json={"clientId": "c-1"})
    second = await authed_client.post("/api/v1/control/lease", json={"clientId": "c-2"})
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONTROL_LEASE_HELD"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/webapi/test_security_and_control.py -q`
Expected: FAIL because routes and handlers are missing.

- [ ] **Step 3: Implement stable aliases and errors**

```python
class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
    request_id: str = Field(alias="requestId")
```

Use constant-time token comparison. Return `401 AUTH_REQUIRED`, `403 AUTH_INVALID`, `409 CONTROL_LEASE_HELD`, and `423 CONTROL_LEASE_REQUIRED`. Never log bearer or lease tokens.

- [ ] **Step 4: Run security tests**

Run:

```powershell
uv run pytest tests/webapi/test_security_and_control.py -q
uv run ruff check manyselves/webapi tests/webapi
```

Expected: PASS.

- [ ] **Step 5: Commit authentication and control**

```powershell
git add manyselves/webapi tests/webapi/test_security_and_control.py
git diff --cached --check
git commit -m "feat: secure api mutations with control leases"
```

### Task 3: Add Project Registry and Revision-Safe File API

**Files:**
- Create: `manyselves/application/project_registry.py`
- Create: `manyselves/application/workspace_files.py`
- Create: `manyselves/application/preview_service.py`
- Create: `manyselves/webapi/schemas/projects.py`
- Create: `manyselves/webapi/schemas/files.py`
- Create: `manyselves/webapi/routes/projects.py`
- Create: `manyselves/webapi/routes/files.py`
- Create: `tests/application/test_workspace_files.py`
- Create: `tests/webapi/test_projects_and_files.py`

**Interfaces:**
- Consumes: configured `data_root`, `ensure_project_structure`, RuntimeFacade mutation lock.
- Produces: project CRUD/activation, file tree/read/write/create/rename/delete/upload/download, byte ranges, and normalized preview DTOs.

- [ ] **Step 1: Write path escape, revision conflict, and activation tests**

```python
def test_resolve_rejects_parent_escape(workspace_files):
    with pytest.raises(UnsafeWorkspacePath):
        workspace_files.resolve("../outside.txt")


def test_write_rejects_stale_revision(workspace_files):
    current = workspace_files.read_text("Inputs/a.txt")
    workspace_files.write_text("Inputs/a.txt", "server", current.revision)
    with pytest.raises(FileRevisionConflict):
        workspace_files.write_text("Inputs/a.txt", "client", current.revision)


async def test_project_activation_requires_idle_runtime(authed_controller_client, running_facade):
    response = await authed_controller_client.post("/api/v1/projects/p2/activate")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_BUSY"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/application/test_workspace_files.py tests/webapi/test_projects_and_files.py -q`
Expected: FAIL because services and routes do not exist.

- [ ] **Step 3: Implement canonical path and revision behavior**

```python
class FileContent(BaseModel):
    path: str
    revision: str
    size: int
    modified_at: datetime = Field(alias="modifiedAt")
    content: str


class SaveFileRequest(BaseModel):
    base_revision: str = Field(alias="baseRevision")
    content: str
```

Resolve with `candidate = (project_root / relative_path).resolve(strict=False)` and require `candidate.is_relative_to(project_root.resolve())`. Reject symlink traversal after checking each existing parent. Save through a temporary sibling and `Path.replace`; compute revision as SHA-256 of bytes. Upload streams to a temporary file with configured size limit, then atomically moves it into place.

`PreviewService` returns bounded normalized DTOs: PDF metadata plus range URL; image MIME/dimensions plus content URL; sanitized SVG bytes; spreadsheets as sheet names, row/column counts and a configured row window; DOCX as ordered paragraph/table/image blocks; Markdown/text as UTF-8 content with encoding errors reported explicitly. It never returns a server absolute path and never evaluates document macros or active content.

- [ ] **Step 4: Run file tests including adversarial paths**

Run:

```powershell
uv run pytest tests/application/test_workspace_files.py tests/webapi/test_projects_and_files.py -q
uv run ruff check manyselves/application manyselves/webapi tests/application tests/webapi
```

Expected: PASS for `..`, absolute paths, encoded separators, symlink escape, stale revision, duplicate names, interrupted upload, and valid atomic save cases.

- [ ] **Step 5: Commit project and filesystem APIs**

```powershell
git add manyselves/application manyselves/webapi tests/application tests/webapi
git diff --cached --check
git commit -m "feat: add revision safe workspace api"
```

### Task 4: Add Conversation, Agent, Settings, and Reporting Routes

**Files:**
- Create: `manyselves/application/conversation_service.py`
- Create: `manyselves/application/reporting_facade.py`
- Create: `manyselves/application/python_run_service.py`
- Create: `manyselves/application/maintenance_service.py`
- Create: `manyselves/webapi/schemas/conversations.py`
- Create: `manyselves/webapi/schemas/agents.py`
- Create: `manyselves/webapi/schemas/reporting.py`
- Create: `manyselves/webapi/schemas/settings.py`
- Create: `manyselves/webapi/schemas/operations.py`
- Create: `manyselves/webapi/schemas/maintenance.py`
- Create: `manyselves/webapi/routes/conversations.py`
- Create: `manyselves/webapi/routes/agents.py`
- Create: `manyselves/webapi/routes/reporting.py`
- Create: `manyselves/webapi/routes/settings.py`
- Create: `manyselves/webapi/routes/operations.py`
- Create: `manyselves/webapi/routes/maintenance.py`
- Create: `tests/webapi/test_conversations_agents_reporting.py`

**Interfaces:**
- Consumes: `ConversationStore`, `RuntimeFacade`, existing reporting service/store, `ConfigManager`.
- Produces: complete command/query endpoints needed by the React parity plans.

- [ ] **Step 1: Write representative contract tests for every resource group**

```python
async def test_send_message_returns_accepted(authed_controller_client):
    response = await authed_controller_client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "92e7fd4c-3d85-4b57-8dd4-8ffcfb16ff11"},
        json={"content": "hello", "source": "user"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


async def test_conversation_round_trip_preserves_store_format(authed_controller_client, workspace):
    created = await authed_controller_client.post("/api/v1/conversations", json={"name": "Review"})
    session_id = created.json()["sessionId"]
    await authed_controller_client.patch(f"/api/v1/conversations/{session_id}", json={"name": "Final"})
    metadata = json.loads((workspace / ".manyselves/conversations/sessions.json").read_text("utf-8"))
    assert any(item["id"] == session_id and item["name"] == "Final" for item in metadata)


async def test_python_run_is_project_scoped_and_returns_operation(authed_controller_client):
    response = await authed_controller_client.post(
        "/api/v1/operations/python",
        json={"path": "Work/example.py", "arguments": []},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


async def test_maintenance_quiesce_rejects_active_runtime(authed_controller_client, running_facade):
    response = await authed_controller_client.post("/api/v1/maintenance/quiesce")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_BUSY"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/webapi/test_conversations_agents_reporting.py -q`
Expected: FAIL because routes are absent.

- [ ] **Step 3: Implement thin resource adapters**

Conversation routes call `ConversationService`, which delegates to `ConversationStore` without rewriting persisted records. Agent routes build locked application commands. Settings responses mask provider secrets and expose only `configured: bool`. Reporting routes call `ReportingFacade`, which wraps the current reporting boundary and returns DTOs containing run, state, waiting-input, checkpoint, evidence, revision, and output metadata.

`PythonRunService` accepts only an existing project-relative `.py` path, runs with the project as working directory, captures bounded stdout/stderr, applies timeout and interrupt, publishes operation events, and documents that this is trusted server execution rather than a security sandbox. `MaintenanceService` acquires the mutation lock, refuses while Agent/Reporting/Python operations are active, flushes durable stores, exposes quiesced state to health/snapshot, and releases only with the matching maintenance token.

Required command responses are `202 AcceptedCommand`; resource creation is `201`; queries are `200`; missing resources are `404`; busy transitions are `409`; invalid state transitions are `422`.

- [ ] **Step 4: Run contracts plus existing conversation/reporting tests**

Run:

```powershell
uv run pytest tests/webapi/test_conversations_agents_reporting.py tests/test_conversations.py tests/reporting/test_service_boundary.py tests/reporting/test_store.py -q
```

Expected: PASS and persisted fixture bytes remain compatible.

- [ ] **Step 5: Commit resource routes**

```powershell
git add manyselves/application manyselves/webapi tests/webapi
git diff --cached --check
git commit -m "feat: expose conversations agents and reporting api"
```

### Task 5: Add Stable Event Mapping, Replay Buffer, and SSE

**Files:**
- Create: `manyselves/webapi/events/__init__.py`
- Create: `manyselves/webapi/events/models.py`
- Create: `manyselves/webapi/events/mapper.py`
- Create: `manyselves/webapi/events/replay.py`
- Create: `manyselves/webapi/events/broker.py`
- Create: `manyselves/webapi/routes/events.py`
- Create: `tests/webapi/test_event_mapper.py`
- Create: `tests/webapi/test_sse.py`

**Interfaces:**
- Consumes: every existing subtype of `manyselves.interfaces.types.Message`.
- Produces: `EventEnvelope`, `EventBroker.publish_internal()`, `/api/v1/events`, Last-Event-ID replay and resync.

- [ ] **Step 1: Write complete mapper and replay tests**

```python
@pytest.mark.parametrize("message,event_type", [
    (AgentResponse(agent_type="main", content="x"), "agent.message.completed"),
    (ToolCallMessage(agent_type="main", tool_name="read_file", arguments={}), "tool.started"),
    (StatusChange(agent_type="main", status="thinking"), "agent.status.changed"),
    (ReportMessage(agent_type="main", task_id="task-1", content="progress", report_type="progress"), "report.status.changed"),
    (Error(source="main", message="failed"), "system.error"),
])
def test_internal_message_maps_to_stable_event(message, event_type):
    event = EventMapper().map(message, context=event_context())
    assert event.type == event_type
    assert event.schema_version == 1


def test_replay_requests_resync_when_cursor_is_evicted():
    replay = ReplayBuffer(capacity=2)
    for sequence in range(1, 4):
        replay.append(event(sequence))
    assert replay.after("evt-1").requires_resync is True
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/webapi/test_event_mapper.py tests/webapi/test_sse.py -q`
Expected: FAIL because event infrastructure is missing.

- [ ] **Step 3: Implement envelope, bounded queues, and SSE framing**

```python
class EventEnvelope(BaseModel):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    event_id: str = Field(alias="eventId")
    sequence: int
    type: str
    timestamp: datetime
    project_id: str | None = Field(alias="projectId")
    session_id: str | None = Field(alias="sessionId")
    agent_id: str | None = Field(alias="agentId")
    run_id: str | None = Field(alias="runId")
    message_id: str | None = Field(alias="messageId")
    payload: dict[str, Any]
```

Each client queue is bounded. Coalesce only `agent.message.delta` for the same message; never drop completed, failed, waiting-user, checkpoint, file-changed, or resync events. Emit SSE `id`, `event`, and one-line JSON `data`; send heartbeat comments without inserting replay events.

- [ ] **Step 4: Run event tests under slow-client and reconnect cases**

Run:

```powershell
uv run pytest tests/webapi/test_event_mapper.py tests/webapi/test_sse.py -q
uv run pytest tests/webapi -q
```

Expected: PASS for replay, eviction/resync, slow consumer, terminal-event preservation, cancellation cleanup, heartbeat, and monotonic sequence tests.

- [ ] **Step 5: Commit SSE support**

```powershell
git add manyselves/webapi/events manyselves/webapi/routes/events.py tests/webapi
git diff --cached --check
git commit -m "feat: add recoverable runtime event stream"
```

### Task 6: Lock OpenAPI Contract and Pass Gate B

**Files:**
- Create: `scripts/export_openapi.py`
- Create: `frontend-contract/openapi.json`
- Create: `tests/webapi/test_openapi_contract.py`
- Modify: `docs/phase1/feature-parity.csv`

**Interfaces:**
- Consumes: complete FastAPI app.
- Produces: deterministic OpenAPI artifact used by frontend client generation.

- [ ] **Step 1: Write deterministic schema tests**

```python
def test_openapi_has_required_resources():
    schema = create_app(test_settings()).openapi()
    required = {
        "/api/v1/bootstrap",
        "/api/v1/events",
        "/api/v1/projects",
        "/api/v1/conversations",
        "/api/v1/agents/{agent_id}/messages",
        "/api/v1/operations/python",
        "/api/v1/maintenance/quiesce",
    }
    assert required <= set(schema["paths"])


def test_all_operation_ids_are_unique():
    schema = create_app(test_settings()).openapi()
    ids = [op["operationId"] for path in schema["paths"].values() for op in path.values() if "operationId" in op]
    assert len(ids) == len(set(ids))
```

- [ ] **Step 2: Run the contract test**

Run: `uv run pytest tests/webapi/test_openapi_contract.py -q`
Expected: PASS after all routes are registered; fix duplicate or generated operation IDs explicitly.

- [ ] **Step 3: Export the canonical schema**

`scripts/export_openapi.py` constructs the app with deterministic test settings, serializes sorted/indented JSON with a final newline, and supports `--check` to compare without writing.

Run: `uv run python scripts/export_openapi.py frontend-contract/openapi.json`
Expected: the artifact is created and contains no secrets or machine-specific paths.

- [ ] **Step 4: Run Gate B and record parity evidence**

Run:

```powershell
uv run pytest tests/webapi -q
uv run ruff check manyselves/application manyselves/webapi tests/application tests/webapi
uv run python scripts/export_openapi.py --check frontend-contract/openapi.json
uv run python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: PASS and no protected changes. Add accepted `API-*` and `SSE-*` evidence rows to the matrix.

- [ ] **Step 5: Commit the contract gate**

```powershell
git add scripts/export_openapi.py frontend-contract/openapi.json tests/webapi/test_openapi_contract.py docs/phase1/feature-parity.csv
git diff --cached --check
git commit -m "test: lock phase one api contract"
```
