# 配电报告流程架构迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在全新仓库中打通 AutoReport 式本地 GUI/消息/任务运行职责与 Nexgent 式声明式 Agent/工作流配置，形成可独立运行的端到端配电报告流程骨架。

**Architecture:** 使用分层 Python 包：领域模型不依赖框架，工作流层加载 Markdown/frontmatter 与 YAML 并驱动 MessageBus/TaskBoard，应用层提供确定性流程 Agent，基础设施层将状态写入当前客户项目，PyQt GUI 只负责文件工作区、文件查看和主 Agent 对话。两个参考仓库只用于阅读，不是依赖。

**Tech Stack:** Python 3.12、Pydantic 2、PyYAML、PyQt6、pytest、pytest-asyncio、pytest-qt、Hatchling。

## Global Constraints

- 所有源码与虚拟环境只能位于 `/Users/zzymima0000/Documents/Codex/autoreport-power-distribution`。
- 客户运行数据只能写入用户打开的客户项目目录，测试夹具只能写入仓库内 `.test-projects/`。
- 不导入 `AutoReport` 或 `Nexgent` 的 Python 包，不使用相对路径访问两个参考仓库。
- GUI 只包含项目文件树、文件查看器和主 Agent 对话，不增加管理仪表盘。
- 任务一必须同时实现 Markdown/frontmatter Agent 配置和 YAML `pipeline`/`parallel` 工作流配置。
- 所有生产行为严格遵守 RED-GREEN-REFACTOR。

---

### Task 1: Project skeleton and typed domain carriers

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/pds_report/__init__.py`
- Create: `src/pds_report/domain/models.py`
- Create: `tests/unit/test_domain_models.py`

**Interfaces:**
- Produces: Pydantic types `ReportRequest`, `ProjectManifest`, `ParsedArtifact`, `EvidenceItem`, `CoverageMatrix`, `ModuleTask`, `ModuleDraft`, `ReviewIssue`, `OutputArtifact`.
- Produces: enums `CoverageStatus`, `ReviewSeverity`, `RunStatus`.

- [ ] **Step 1: Write failing domain tests**

```python
def test_report_request_keeps_execution_requirements_separate():
    request = ReportRequest(
        task="write_report",
        target_modules=["2.4"],
        execution_requirements=["deep_reasoning"],
    )
    assert request.task == "write_report"
    assert request.execution_requirements == ["deep_reasoning"]


def test_evidence_item_requires_source_location():
    with pytest.raises(ValidationError):
        EvidenceItem(id="ev-1", fact="主柜温度为 80°C")
```

- [ ] **Step 2: Run tests and confirm import failure**

Run: `.venv/bin/pytest tests/unit/test_domain_models.py -q`
Expected: FAIL because `pds_report.domain.models` does not exist.

- [ ] **Step 3: Add package metadata and minimal Pydantic models**

Use `src` packaging, Python `>=3.12`, runtime dependencies `pydantic`, `pyyaml`, `PyQt6`, and dev dependencies `pytest`, `pytest-asyncio`, `pytest-qt`, `ruff`.

Each carrier must have explicit fields from the approved design and `extra="forbid"`. IDs and source locations must reject blank strings.

- [ ] **Step 4: Run tests and lint**

Run: `.venv/bin/pytest tests/unit/test_domain_models.py -q`
Expected: PASS.

Run: `.venv/bin/ruff check src/pds_report/domain tests/unit/test_domain_models.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore src tests/unit/test_domain_models.py
git commit -m "feat: define report workflow domain models"
```

### Task 2: Nexgent-style declarative configuration

**Files:**
- Create: `src/pds_report/workflow/config.py`
- Create: `src/pds_report/resources/agents/*.md`
- Create: `src/pds_report/resources/workflows/phase-a.yml`
- Create: `tests/unit/test_workflow_config.py`

**Interfaces:**
- Produces: `AgentDefinition`, `PhaseDefinition`, `WorkflowDefinition`.
- Produces: `load_agent_definition(path: Path) -> AgentDefinition`.
- Produces: `load_workflow(path: Path, agents: Mapping[str, AgentDefinition]) -> WorkflowDefinition`.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_loads_markdown_frontmatter_and_body(agent_file):
    agent = load_agent_definition(agent_file)
    assert agent.id == "manifest-builder"
    assert agent.writes == ["project_manifest"]
    assert "扫描" in agent.instructions


def test_workflow_rejects_unknown_agent(workflow_file, agents):
    with pytest.raises(ConfigurationError, match="unknown-agent"):
        load_workflow(workflow_file, agents)
```

Also cover duplicate IDs, unsupported modes, unknown dependencies, cycles, and undeclared reads/writes.

- [ ] **Step 2: Run tests and confirm loader is missing**

Run: `.venv/bin/pytest tests/unit/test_workflow_config.py -q`
Expected: FAIL because loader is absent.

- [ ] **Step 3: Implement frontmatter/YAML loaders and validation**

Split a Markdown file at `---`, parse frontmatter with `yaml.safe_load`, retain the remaining body as instructions, and validate all agent IDs before building the workflow DAG. Supported phase modes are exactly `pipeline` and `parallel`.

- [ ] **Step 4: Add packaged Agent and Phase A resources**

Define these agents: `manifest-builder`, `artifact-parser`, `evidence-normalizer`, `coverage-evaluator`, `report-planner`, `module-worker`, `evidence-auditor`, `revision-router`, `project-delivery`.

Define ordered phases: intake pipeline, coverage pipeline, module parallel, quality pipeline.

- [ ] **Step 5: Verify configuration tests**

Run: `.venv/bin/pytest tests/unit/test_workflow_config.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pds_report/workflow/config.py src/pds_report/resources tests/unit/test_workflow_config.py
git commit -m "feat: add declarative agent workflow configuration"
```

### Task 3: AutoReport-style MessageBus and TaskBoard

**Files:**
- Create: `src/pds_report/workflow/events.py`
- Create: `src/pds_report/workflow/bus.py`
- Create: `src/pds_report/workflow/tasks.py`
- Create: `tests/unit/test_message_bus.py`
- Create: `tests/unit/test_task_board.py`

**Interfaces:**
- Produces: `Event`, `EventType`, `MessageBus.publish()`, `MessageBus.subscribe()`.
- Produces: `Task`, `TaskStatus`, `TaskBoard.create()`, `start()`, `complete()`, `fail()`, `block()`, `skip_dependents()`.

- [ ] **Step 1: Write failing MessageBus tests**

```python
@pytest.mark.asyncio
async def test_publish_delivers_event_to_typed_subscriber():
    bus = MessageBus()
    received = []
    bus.subscribe(EventType.AGENT_STARTED, received.append)
    await bus.publish(Event(type=EventType.AGENT_STARTED, source="planner"))
    assert [event.source for event in received] == ["planner"]
```

- [ ] **Step 2: Write failing TaskBoard transition tests**

```python
def test_failed_task_skips_dependent_tasks():
    board = TaskBoard()
    board.create("a", "agent-a")
    board.create("b", "agent-b", needs=["a"])
    board.fail("a", "boom")
    board.skip_dependents("a")
    assert board.get("b").status is TaskStatus.SKIPPED
```

Cover invalid transitions, unique IDs, blocked reasons, revision count, and snapshots.

- [ ] **Step 3: Run both tests and confirm failures**

Run: `.venv/bin/pytest tests/unit/test_message_bus.py tests/unit/test_task_board.py -q`
Expected: FAIL because runtime classes are missing.

- [ ] **Step 4: Implement minimal bus and task state machine**

Subscribers may be synchronous or asynchronous. Task transition methods must validate the current state and publish no implicit success.

- [ ] **Step 5: Verify tests**

Run: `.venv/bin/pytest tests/unit/test_message_bus.py tests/unit/test_task_board.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pds_report/workflow tests/unit/test_message_bus.py tests/unit/test_task_board.py
git commit -m "feat: add message bus and task board"
```

### Task 4: Configured workflow execution engine

**Files:**
- Create: `src/pds_report/agents/base.py`
- Create: `src/pds_report/workflow/runner.py`
- Create: `tests/unit/test_workflow_runner.py`

**Interfaces:**
- Produces: `AgentContext(project_root, run_id, state, bus, task_board)`.
- Produces: `AgentPort.run(context) -> Mapping[str, object]`.
- Produces: `WorkflowRunner.run(workflow, agents, context) -> RunResult`.

- [ ] **Step 1: Write failing pipeline test**

```python
@pytest.mark.asyncio
async def test_pipeline_passes_state_to_next_agent():
    agents = {
        "first": RecordingAgent({"number": 1}),
        "second": IncrementingAgent(),
    }
    result = await runner.run(pipeline_workflow, agents, context)
    assert result.state["number"] == 2
```

- [ ] **Step 2: Write failing parallel and failure propagation tests**

Verify parallel agents overlap using `asyncio.Event`, outputs merge without conflicting keys, conflicting writes fail, and a failed phase skips all dependent phases.

- [ ] **Step 3: Run tests and confirm missing runner**

Run: `.venv/bin/pytest tests/unit/test_workflow_runner.py -q`
Expected: FAIL.

- [ ] **Step 4: Implement runner**

Create TaskBoard entries from configuration. Execute pipeline agents serially and parallel agents with `asyncio.gather`. Publish run/phase/agent events. Merge only declared outputs. On exception mark the task failed, skip dependent tasks, persist the error in `RunResult`, and never emit `RUN_COMPLETED`.

- [ ] **Step 5: Verify runner tests**

Run: `.venv/bin/pytest tests/unit/test_workflow_runner.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pds_report/agents/base.py src/pds_report/workflow/runner.py tests/unit/test_workflow_runner.py
git commit -m "feat: execute configured agent workflows"
```

### Task 5: Project-local persistence and intake

**Files:**
- Create: `src/pds_report/infrastructure/project_store.py`
- Create: `src/pds_report/infrastructure/intake.py`
- Create: `tests/unit/test_project_store.py`
- Create: `tests/unit/test_intake.py`

**Interfaces:**
- Produces: `ProjectStore.create()`, `write_json()`, `write_jsonl()`, `write_text()`, `save_run()`.
- Produces: `scan_project(project_root: Path) -> ProjectManifest`.

- [ ] **Step 1: Write failing project layout test**

```python
def test_create_project_uses_only_project_root(project_root):
    store = ProjectStore(project_root)
    store.create()
    assert (project_root / "Inputs").is_dir()
    assert (project_root / "Work" / "runs").is_dir()
    assert (project_root / "Outputs" / "Reviews").is_dir()
```

- [ ] **Step 2: Write failing intake determinism test**

Create two input files and assert stable SHA-256 IDs, relative paths, format classification, and no scan of `Work`/`Outputs`.

- [ ] **Step 3: Run tests and confirm failure**

Run: `.venv/bin/pytest tests/unit/test_project_store.py tests/unit/test_intake.py -q`
Expected: FAIL.

- [ ] **Step 4: Implement store and scanner**

Atomic writes use a sibling `.<name>.pending` file inside the same customer project, followed by `Path.replace`. Never call `tempfile`.

- [ ] **Step 5: Verify tests**

Run: `.venv/bin/pytest tests/unit/test_project_store.py tests/unit/test_intake.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pds_report/infrastructure tests/unit/test_project_store.py tests/unit/test_intake.py
git commit -m "feat: persist report runs inside customer projects"
```

### Task 6: Deterministic Phase A agents and request service

**Files:**
- Create: `src/pds_report/app/request_parser.py`
- Create: `src/pds_report/agents/builtin.py`
- Create: `src/pds_report/app/service.py`
- Create: `tests/unit/test_request_parser.py`
- Create: `tests/integration/test_phase_a_flow.py`

**Interfaces:**
- Produces: `parse_report_request(message: str) -> ReportRequest`.
- Produces: `build_builtin_agents(store) -> Mapping[str, AgentPort]`.
- Produces: `ReportApplication.run_message(project_root, message) -> ApplicationReply`.

- [ ] **Step 1: Write failing request parsing tests**

```python
def test_deep_reasoning_is_execution_requirement_not_task():
    request = parse_report_request("写作配电报告，要求深度思考，先做2.4")
    assert request.task == "write_report"
    assert request.target_modules == ["2.4"]
    assert request.execution_requirements == ["deep_reasoning"]
```

- [ ] **Step 2: Write failing end-to-end flow test**

Create a repository-local `.test-projects/phase-a` customer project with one Markdown input. Run the application and assert creation of `manifest.json`, `evidence.jsonl`, `coverage.json`, one module draft, one review record, and a run snapshot. Assert every claim references a real evidence ID.

- [ ] **Step 3: Run tests and confirm failures**

Run: `.venv/bin/pytest tests/unit/test_request_parser.py tests/integration/test_phase_a_flow.py -q`
Expected: FAIL.

- [ ] **Step 4: Implement request parser and built-in agents**

The parser recognizes report intent, `2.1`-`2.5`, deep reasoning, pending-draft permission, and skip instructions. It raises `RequestValidationError` for blank or non-report requests.

Built-in agents exchange only formal carriers. Text/Markdown/CSV inputs produce parsed artifacts and traceable evidence; unsupported files remain in the manifest with pending parsing status. The worker emits a provisional draft, the auditor deterministically checks evidence references, and delivery writes artifacts through `ProjectStore`.

- [ ] **Step 5: Verify unit and integration tests**

Run: `.venv/bin/pytest tests/unit/test_request_parser.py tests/integration/test_phase_a_flow.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pds_report/app src/pds_report/agents/builtin.py tests/unit/test_request_parser.py tests/integration/test_phase_a_flow.py
git commit -m "feat: connect phase a report workflow"
```

### Task 7: PyQt project workspace and main Agent conversation

**Files:**
- Create: `src/pds_report/gui/main_window.py`
- Create: `src/pds_report/gui/worker.py`
- Create: `src/pds_report/__main__.py`
- Create: `tests/gui/test_main_window.py`

**Interfaces:**
- Produces: `MainWindow(project_root: Path, application: ReportApplication)`.
- Produces: CLI `pds-report --project PATH` and headless `pds-report --headless --project PATH --message TEXT`.

- [ ] **Step 1: Write failing GUI structure test**

```python
def test_main_window_exposes_only_workspace_viewer_and_chat(qtbot, app, project_root):
    window = MainWindow(project_root, app)
    qtbot.addWidget(window)
    assert window.file_tree.objectName() == "projectFileTree"
    assert window.preview.objectName() == "filePreview"
    assert window.chat_input.objectName() == "mainAgentInput"
```

- [ ] **Step 2: Write failing interaction test**

Double-click a text file and assert preview content. Send a report message, wait for the background worker, then assert the conversation contains a result and the file model sees generated outputs.

- [ ] **Step 3: Run GUI tests and confirm failure**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/gui/test_main_window.py -q`
Expected: FAIL.

- [ ] **Step 4: Implement three-pane GUI and background flow worker**

Use `QFileSystemModel`/`QTreeView`, `QTextBrowser`, and a conversation pane with `QListWidget`, `QLineEdit`, and `QPushButton`. Run async application work off the UI thread and send results back with Qt signals. Do not add evidence, task, coverage, timeline, or configuration pages.

- [ ] **Step 5: Verify GUI tests**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/gui/test_main_window.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pds_report/gui src/pds_report/__main__.py tests/gui/test_main_window.py
git commit -m "feat: add local report project workspace"
```

### Task 8: Independent-install and complete architecture verification

**Files:**
- Create: `README.md`
- Create: `tests/integration/test_reference_independence.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Validates the public install, headless entry point, GUI entry point, packaged resources, and reference-repository independence.

- [ ] **Step 1: Write failing package resource test**

Install editable package, change current directory to the customer project, and run the headless entry point. Assert the packaged Agent/workflow resources load without accessing sibling repositories.

- [ ] **Step 2: Run the independence test and confirm failure**

Run: `.venv/bin/pytest tests/integration/test_reference_independence.py -q`
Expected: FAIL until package data and entry point are complete.

- [ ] **Step 3: Finalize packaging and usage documentation**

Include resources as package data. Document local setup, GUI launch, headless smoke command, customer project layout, configuration format, and the exact boundary of AutoReport/Nexgent references.

- [ ] **Step 4: Run complete verification**

Run: `.venv/bin/ruff check src tests`
Expected: PASS.

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q`
Expected: all tests PASS.

Run: `.venv/bin/pds-report --headless --project .test-projects/smoke --message "写作配电报告，要求深度思考，先做2.4"`
Expected: exit 0 and JSON reply naming generated project files.

Run: `rg -n "\.\./AutoReport|\.\./Nexgent|Documents/Codex/AutoReport|Documents/Codex/Nexgent" src tests pyproject.toml README.md`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add README.md pyproject.toml tests/integration/test_reference_independence.py
git commit -m "docs: verify independent report workflow application"
```

### Task 9: Requirement audit and repository handoff

**Files:**
- Modify only files required to fix audit findings.

**Interfaces:**
- Consumes: approved design, PDF/HTML plan, all test evidence.
- Produces: a clean, committed repository with requirement-by-requirement evidence.

- [ ] **Step 1: Audit every approved design requirement**

Map each acceptance item to a source file and a test or runnable command. Treat missing evidence as incomplete work and fix it before continuing.

- [ ] **Step 2: Re-run fresh verification after audit fixes**

Run: `.venv/bin/ruff check src tests`

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q`

Run: `.venv/bin/pds-report --headless --project .test-projects/final-smoke --message "写作配电报告，要求深度思考，先做2.4"`

Expected: all commands exit 0 with no warnings or failures.

- [ ] **Step 3: Verify Git state**

Run: `git status --short --branch`
Expected: clean `main` branch with the private GitHub `origin`; no push is performed unless separately requested.

- [ ] **Step 4: Commit audit fixes if any**

```bash
git add src tests README.md pyproject.toml
git commit -m "test: complete architecture migration audit"
```
