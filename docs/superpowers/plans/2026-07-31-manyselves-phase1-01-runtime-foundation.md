# Manyselves Phase 1 Runtime Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a tested application-layer composition root shared by PyQt and the future FastAPI service without changing protected core behavior.

**Architecture:** Characterize the current `ManyselvesApp` and `BackendAPIImpl`, then perform a behavior-preserving extraction into focused application modules. `RuntimeHost` owns lifecycle, `RuntimeFacade` owns supported commands/queries, and `LegacyRuntimeAdapter` contains any unavoidable private-member reads.

**Tech Stack:** Python 3.12, asyncio, Pydantic 2, pytest, pytest-asyncio, Ruff.

## Global Constraints

- Protected paths are `manyselves/core/**` and `manyselves/templates/**`.
- Preserve `BackendAPI` behavior and keep `manyselves.app.BackendAPIImpl` import-compatible.
- Preserve current provider validation, project structure creation, logging, MessageBus startup, LoopManager startup, rollback, debug, and shutdown behavior.
- Do not add FastAPI or frontend code in this plan.
- Every task must pass existing PyQt and core regression tests before commit.

---

### Task 1: Freeze Manifest and Current-Behavior Characterization

**Files:**
- Create: `docs/phase1/protected-paths.txt`
- Create: `docs/phase1/core-freeze-base.txt`
- Create: `docs/phase1/feature-parity.csv`
- Create: `scripts/check_phase1_core_freeze.py`
- Create: `tests/application/test_current_runtime_characterization.py`

**Interfaces:**
- Consumes: current `ManyselvesApp`, `BackendAPIImpl`, `MessageBus`, and `ensure_project_structure` behavior.
- Produces: `check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt` and the canonical parity CSV columns used by release verification.

- [ ] **Step 1: Write characterization tests before extraction**

```python
@pytest.mark.asyncio
async def test_backend_publishes_user_message_with_existing_mapping():
    bus = MessageBus()
    api = BackendAPIImpl(ConfigManager(), bus)
    seen: list[UserMessage] = []
    bus.subscribe(UserMessage, lambda message: _append_async(seen, message))
    processor = asyncio.create_task(bus.process_queue())
    await api.send_user_message("hello", "sub", message_id="m-1")
    await _wait_until(lambda: len(seen) == 1)
    bus.shutdown()
    await processor
    assert seen[0].agent_type == "main"
    assert seen[0].message_id == "m-1"


def test_project_startup_uses_canonical_structure(tmp_path: Path):
    ensure_project_structure(tmp_path)
    assert {"Inputs", "Knowledge", "Templates", "Work", "Outputs"} <= {
        child.name for child in tmp_path.iterdir()
    }
```

- [ ] **Step 2: Run the tests against the untouched implementation**

Run:

```powershell
uv run pytest tests/application/test_current_runtime_characterization.py tests/test_bus.py tests/test_manager.py -q
```

Expected: PASS. If a characterization assertion is wrong, correct the test to match observed behavior before extraction; do not alter runtime behavior.

- [ ] **Step 3: Add the freeze checker and initial parity schema**

`protected-paths.txt` contains exactly:

```text
manyselves/core/
manyselves/templates/
```

`feature-parity.csv` starts with this header:

```csv
id,module,feature,legacy_evidence,api_or_bridge,react_evidence,browser_test,electron_test,status,reviewer,rationale
```

Before implementation begins, populate one `planned` row for every observed PyQt capability under these families: `PROJECT`, `FILE`, `EDITOR`, `PREVIEW`, `PYTHON`, `CONV`, `MESSAGE`, `AGENT`, `TOOL`, `TASK`, `CONFIG`, `REPORT`, `DESKTOP`, `RECOVERY`, and `DEPLOY`. Every row must already contain the concrete PyQt file/class/method or existing test node in `legacy_evidence`; later plans fill the new implementation and test columns rather than adding forgotten legacy scope at release time.

The checker supports `--initialize <file>` to write the current pre-implementation commit SHA exactly once and refuse overwrite. Normal checks read that immutable SHA through `--base-file <file>`, call `git diff --name-only <sha> --`, normalize separators to `/`, print every protected match, and exit `1` when any match exists. It exits `2` for a missing/invalid baseline and `0` when clean.

Run:

```powershell
uv run python scripts/check_phase1_core_freeze.py --initialize docs/phase1/core-freeze-base.txt
uv run python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: the first command records the current commit; the second reports no protected changes.

- [ ] **Step 4: Test the checker with a temporary Git repository fixture**

```python
def test_freeze_checker_rejects_protected_change(tmp_path: Path):
    repo = init_repo(tmp_path, {"manyselves/core/sample.py": "before\n"})
    baseline_file = repo / "core-freeze-base.txt"
    baseline_file.write_text(git_head(repo) + "\n", encoding="utf-8")
    (repo / "manyselves/core/sample.py").write_text("after\n", encoding="utf-8")
    result = run_checker(repo, base_file=baseline_file)
    assert result.returncode == 1
    assert "manyselves/core/sample.py" in result.stdout
```

Run: `uv run pytest tests/application/test_current_runtime_characterization.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the baseline artifacts**

```powershell
git add docs/phase1/protected-paths.txt docs/phase1/core-freeze-base.txt docs/phase1/feature-parity.csv scripts/check_phase1_core_freeze.py tests/application/test_current_runtime_characterization.py
git diff --cached --check
git commit -m "test: freeze phase one runtime behavior"
```

### Task 2: Extract BackendAPIImpl Without Behavior Changes

**Files:**
- Create: `manyselves/application/__init__.py`
- Create: `manyselves/application/backend_api.py`
- Modify: `manyselves/app.py`
- Create: `tests/application/test_backend_api.py`

**Interfaces:**
- Consumes: `BackendAPI`, `ConfigManager`, `MessageBus`, `LoopManager`.
- Produces: `manyselves.application.backend_api.BackendAPIImpl`; `manyselves.app.BackendAPIImpl` remains the same class object through import re-export.

- [ ] **Step 1: Write import and behavior tests**

```python
def test_app_reexports_backend_api_impl():
    from manyselves.app import BackendAPIImpl as LegacyImport
    from manyselves.application.backend_api import BackendAPIImpl
    assert LegacyImport is BackendAPIImpl


@pytest.mark.asyncio
async def test_sync_conversation_preserves_session_and_messages(fake_loop_manager):
    api = BackendAPIImpl(ConfigManager(), MessageBus())
    api.set_loop_manager(fake_loop_manager)
    await api.sync_agent_conversation(
        "main",
        [{"role": "user", "content": "hello"}],
        session_id="s-1",
        clear_pending=True,
    )
    loop = fake_loop_manager.get_loop("main")
    assert loop._current_session_id == "s-1"
    assert [(m.role, m.content) for m in loop._conversation_history] == [("user", "hello")]
```

- [ ] **Step 2: Run tests and confirm the new import fails**

Run: `uv run pytest tests/application/test_backend_api.py -q`
Expected: FAIL with `ModuleNotFoundError: manyselves.application`.

- [ ] **Step 3: Move the class verbatim and re-export it**

Create `application/backend_api.py` with the existing class body and required imports. Replace the class body in `app.py` with:

```python
from .application.backend_api import BackendAPIImpl
```

Do not rename methods, normalize fields, alter private loop access, or add Web-specific types in this extraction.

- [ ] **Step 4: Run focused and legacy tests**

Run:

```powershell
uv run pytest tests/application/test_backend_api.py tests/interfaces tests/test_agent_routing.py tests/test_app_stderr_filter.py -q
uv run ruff check manyselves/application manyselves/app.py tests/application
```

Expected: PASS.

- [ ] **Step 5: Commit the extraction**

```powershell
git add manyselves/application manyselves/app.py tests/application/test_backend_api.py
git diff --cached --check
git commit -m "refactor: extract backend api adapter"
```

### Task 3: Add Shared RuntimeHost Lifecycle

**Files:**
- Create: `manyselves/application/errors.py`
- Create: `manyselves/application/runtime_host.py`
- Modify: `manyselves/app.py`
- Create: `tests/application/test_runtime_host.py`

**Interfaces:**
- Consumes: `BackendAPIImpl`, `ConfigManager`, `MessageBus`, `LoopManager`, `ensure_project_structure`, `add_project_logging`.
- Produces: `RuntimeHost.create()`, `start(workspace)`, `stop()`, `is_ready`, `workspace`, `backend`, `bus`, and `loop_manager`.

- [ ] **Step 1: Write lifecycle tests with injected factories**

```python
@pytest.mark.asyncio
async def test_runtime_host_starts_bus_before_loops(tmp_path, fake_components):
    host = RuntimeHost._for_test(**fake_components)
    await host.start(tmp_path)
    assert fake_components["events"][:2] == ["bus-started", "loops-started"]
    assert host.is_ready is True
    assert host.workspace == tmp_path.resolve()


@pytest.mark.asyncio
async def test_runtime_host_stops_only_owned_tasks(tmp_path, fake_components):
    unrelated = asyncio.create_task(asyncio.Event().wait())
    host = RuntimeHost._for_test(**fake_components)
    await host.start(tmp_path)
    await host.stop()
    assert not unrelated.cancelled()
    unrelated.cancel()
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/application/test_runtime_host.py -q`
Expected: FAIL because `RuntimeHost` does not exist.

- [ ] **Step 3: Implement RuntimeHost and typed startup errors**

```python
class RuntimeStartupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeHost:
    @classmethod
    def create(cls, config_manager: ConfigManager | None = None) -> "RuntimeHost":
        config = config_manager or ConfigManager()
        bus = MessageBus()
        backend = BackendAPIImpl(config_manager=config, bus=bus)
        return cls(config_manager=config, bus=bus, backend=backend)

    async def start(self, workspace: Path) -> None:
        is_valid, _available = self.config_manager.validate_api_keys()
        if not is_valid:
            raise RuntimeStartupError("NO_PROVIDER_KEYS", "No provider API keys are configured")
        resolved = workspace.resolve()
        add_project_logging(resolved)
        ensure_project_structure(resolved)
        self.loop_manager = LoopManager(resolved, self.config_manager, self.bus)
        self.backend.set_loop_manager(self.loop_manager)
        self._bus_task = asyncio.create_task(self.bus.process_queue(), name="manyselves-message-bus")
        await self.loop_manager.start()
        self._workspace = resolved
        self._is_ready = True

    async def stop(self) -> None:
        self._is_ready = False
        self.bus.shutdown()
        if self.loop_manager is not None:
            await self.loop_manager.stop()
        if self._bus_task is not None:
            await self._bus_task
```

Track the bus processor task on the host. `stop()` shuts down the bus, stops the loop manager, and awaits only tasks owned by the host. It must not cancel every task in the process. `ManyselvesApp.startup()` delegates to the host and converts `RuntimeStartupError(code="NO_PROVIDER_KEYS")` back to its existing `False` result.

- [ ] **Step 4: Run lifecycle, application, and GUI smoke tests**

Run:

```powershell
uv run pytest tests/application tests/test_cli.py tests/test_headless.py tests/gui/test_main_window_agent_switching.py -q
uv run python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: PASS and no protected changes.

- [ ] **Step 5: Commit the shared composition root**

```powershell
git add manyselves/application/errors.py manyselves/application/runtime_host.py manyselves/app.py tests/application/test_runtime_host.py
git diff --cached --check
git commit -m "refactor: share runtime lifecycle across clients"
```

### Task 4: Add RuntimeFacade, Command Serialization, and Control Lease

**Files:**
- Create: `manyselves/application/models.py`
- Create: `manyselves/application/control.py`
- Create: `manyselves/application/legacy_runtime_adapter.py`
- Create: `manyselves/application/runtime_facade.py`
- Create: `tests/application/test_control.py`
- Create: `tests/application/test_runtime_facade.py`

**Interfaces:**
- Consumes: `RuntimeHost.backend`, `RuntimeHost.loop_manager`, `ConversationStore`, current TaskBoard state.
- Produces: `RuntimeFacade`, `ControlLeaseService`, command models, `AcceptedCommand`, `RuntimeSnapshot`.

- [ ] **Step 1: Write lease and serialization tests**

```python
@pytest.mark.asyncio
async def test_mutating_commands_are_serialized(started_facade):
    first = asyncio.create_task(started_facade.send_user_message(message_command("one")))
    second = asyncio.create_task(started_facade.send_user_message(message_command("two")))
    await asyncio.gather(first, second)
    assert started_facade.backend.call_order == ["start:one", "end:one", "start:two", "end:two"]


def test_expired_control_lease_cannot_mutate(clock):
    leases = ControlLeaseService(clock=clock, ttl=timedelta(seconds=30))
    lease = leases.acquire(client_id="c-1", actor_id="pilot")
    clock.advance(seconds=31)
    with pytest.raises(ControlLeaseRequired):
        leases.require(lease.token)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/application/test_control.py tests/application/test_runtime_facade.py -q`
Expected: FAIL because the application services do not exist.

- [ ] **Step 3: Implement exact command and snapshot models**

```python
class SendMessageCommand(BaseModel):
    command_id: UUID
    lease_token: str
    content: str = Field(min_length=1)
    agent_id: str
    message_id: str | None = None
    source: Literal["user", "main_agent"] = "user"


class AcceptedCommand(BaseModel):
    command_id: UUID
    status: Literal["accepted"] = "accepted"


class RuntimeSnapshot(BaseModel):
    ready: bool
    workspace: str | None
    agent_statuses: dict[str, str]
    active_session_id: str | None
    controller_client_id: str | None
```

Use a single `asyncio.Lock` inside `RuntimeFacade` for all runtime mutations and a bounded command-id cache to make duplicate submissions return the original acceptance without republishing. Put all reads of `_task_board`, `_conversation_history`, or other current private members in `LegacyRuntimeAdapter` with characterization tests.

- [ ] **Step 4: Run focused tests and type/lint checks**

Run:

```powershell
uv run pytest tests/application -q
uv run ruff check manyselves/application tests/application
uv run python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: PASS and no protected changes.

- [ ] **Step 5: Commit the stable application boundary**

```powershell
git add manyselves/application tests/application
git diff --cached --check
git commit -m "feat: add serialized runtime facade"
```

### Task 5: Prove PyQt Compatibility at the Foundation Gate

**Files:**
- Modify: `tests/application/test_current_runtime_characterization.py`
- Create: `tests/application/test_pyqt_runtime_compatibility.py`
- Modify: `docs/phase1/feature-parity.csv`

**Interfaces:**
- Consumes: completed `RuntimeHost`, `BackendAPIImpl`, PyQt `ManyselvesApp`.
- Produces: Gate A evidence and accepted baseline rows for startup, shutdown, send, interrupt, rollback, debug, provider, and model behavior.

- [ ] **Step 1: Add old-versus-new golden behavior tests**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send", "interrupt", "restart", "rollback", "debug"])
async def test_pyqt_adapter_matches_runtime_facade(action, legacy_harness, facade_harness):
    legacy_result = await legacy_harness.perform(action)
    facade_result = await facade_harness.perform(action)
    assert facade_result.normalized_events == legacy_result.normalized_events
    assert facade_result.workspace_files == legacy_result.workspace_files
```

- [ ] **Step 2: Run the comparison and full existing suite**

Run:

```powershell
uv run pytest tests/application tests/interfaces tests/gui tests/test_agent_loop.py tests/test_agent_tools.py -q
```

Expected: PASS.

- [ ] **Step 3: Record evidence in the parity matrix**

Add one row per validated behavior with stable IDs `RUNTIME-001` through `RUNTIME-008`; use repository-relative test node IDs as evidence and set `status=accepted` only for passing rows.

- [ ] **Step 4: Run Gate A exactly**

Run:

```powershell
uv run pytest tests/application tests/test_app_stderr_filter.py tests/test_bus.py tests/test_manager.py -q
uv run python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: PASS and zero protected changes.

- [ ] **Step 5: Commit Gate A evidence**

```powershell
git add tests/application docs/phase1/feature-parity.csv
git diff --cached --check
git commit -m "test: verify pyqt runtime compatibility"
```
