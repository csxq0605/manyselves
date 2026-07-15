# AutoReport 底座迁移与 Phase A 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 AutoReport 完整迁入独立新仓库，并在其现有 GUI 与 Agent 运行时中接入配电报告 Phase A 纵向流程。

**Architecture:** AutoReport 是唯一代码底座；配电领域载体与声明式配置作为其内部模块加入，现有 MessageBus、LoopManager、AgentLoop、TaskBoard、工具注册和 PyQt 工作区继续承担运行与交互。Nexgent 仅作为 frontmatter 和 phase/pipeline/parallel 设计参考。

**Tech Stack:** Python 3.12、PyQt6、Pydantic 2、PyYAML、OpenPyXL、pytest、pytest-asyncio、pytest-qt、Hatchling。

## Global Constraints

- 所有源码、虚拟环境、构建缓存与测试夹具只位于 `/Users/zzymima0000/Documents/Codex` 内。
- `/Users/zzymima0000/Documents/Codex/work` 只读，不写入任何项目或中间产物。
- AutoReport 复制到新仓库根目录，不嵌套仓库且不复制 `.git`。
- 保留新仓库自己的 Git 历史、远程与 `main`；用户已明确确认直接在该仓库重做。
- 新功能遵循 RED-GREEN-REFACTOR；底座原样迁移先做基线测试。

---

### Task 1: Replace the prototype tree with the AutoReport base

**Files:**
- Replace: repository root tracked application files
- Preserve: `docs/superpowers/specs/2026-07-14-flow-architecture-migration-design.md`
- Preserve: `docs/superpowers/plans/2026-07-14-flow-architecture-migration.md`

**Interfaces:**
- Produces: the complete AutoReport `autoreport` package and original test suite at the new repository root.

- [ ] Create an archive branch pointing at the last prototype commit.
- [ ] Remove only prototype application/test files from the working tree.
- [ ] Copy AutoReport files excluding `.git`, `.venv`, caches and build artifacts.
- [ ] Confirm there is no nested `AutoReport/`, `Nexgent/`, or `work/` directory.
- [ ] Commit the source-base migration.

### Task 2: Rebrand repository metadata without changing runtime behavior

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `README_zh.md`

**Interfaces:**
- Preserves: `autoreport` import package and `autoreport` CLI during migration.
- Produces: project metadata and documentation identifying the new配电报告 fork.

- [ ] Change distribution description, repository URLs, keywords and visible project identity.
- [ ] Document that the current milestone is the AutoReport base migration and that Phase A follows inside the same runtime.
- [ ] Build the package and confirm the original entry point remains importable.
- [ ] Commit metadata changes.

### Task 3: Establish the migrated AutoReport baseline

**Files:**
- Modify only when a reproducible environment incompatibility requires a targeted fix with a failing test.

**Interfaces:**
- Produces: a recorded baseline for unit, GUI-headless, lint and package build checks.

- [ ] Synchronize dependencies into the new repository's own `.venv` using a repository-local cache/temp path.
- [ ] Run the full original pytest suite with Qt offscreen.
- [ ] Run Ruff and package build checks.
- [ ] If failures occur, use systematic debugging and do not hide pre-existing failures.
- [ ] Commit only evidence or necessary compatibility fixes.

### Task 4: Add typed power-distribution carriers inside AutoReport

**Files:**
- Create: `autoreport/core/reporting/models.py`
- Create: `autoreport/core/reporting/__init__.py`
- Test: `tests/reporting/test_models.py`

**Interfaces:**
- Produces: `ReportRequest`, `ProjectManifest`, `ParsedArtifact`, `EvidenceItem`, `CoverageMatrix`, `ModuleTask`, `ModuleDraft`, `ReviewIssue`, `OutputArtifact`.

- [ ] Write model contract tests and confirm import failure.
- [ ] Implement strict Pydantic carriers with source-location requirements.
- [ ] Run focused and full tests.
- [ ] Commit the carrier layer.

### Task 5: Add declarative agent and workflow definitions

**Files:**
- Create: `autoreport/core/reporting/config.py`
- Create: `autoreport/templates/reporting/agents/*.md`
- Create: `autoreport/templates/reporting/workflows/phase-a.yml`
- Test: `tests/reporting/test_config.py`

**Interfaces:**
- Produces: `load_agent_definition()` and `load_workflow_definition()`.
- Consumes: carrier names exported by `autoreport.core.reporting.models`.

- [ ] Write failing frontmatter and DAG validation tests.
- [ ] Implement the minimal loader for `phase`, `pipeline`, and `parallel`.
- [ ] Add Phase A resources and verify package inclusion.
- [ ] Run focused and full tests, then commit.

### Task 6: Integrate Phase A with AutoReport runtime and project workspace

**Files:**
- Create: `autoreport/core/reporting/service.py`
- Create: `autoreport/core/reporting/store.py`
- Modify: `autoreport/core/loops/manager.py`
- Modify: relevant main-Agent prompt/tool registration files discovered from the migrated base
- Test: `tests/reporting/test_service.py`
- Test: `tests/reporting/test_runtime_integration.py`

**Interfaces:**
- Produces: `ReportingService.run(request, workspace) -> ReportingRunResult`.
- Reuses: AutoReport `MessageBus`, `TaskBoard`, project workspace, AgentLoop and tool registry.

- [ ] Write a failing integration test from `ReportRequest` to one project output.
- [ ] Write a failing missing-evidence test that must return `blocked` without fabricating facts.
- [ ] Implement project-local state and deterministic Phase A runner using existing runtime primitives.
- [ ] Expose the flow to the main Agent through the existing tool/loop path.
- [ ] Verify file-tree-visible outputs and conversation feedback without a new dashboard.
- [ ] Run full tests and commit.

### Task 7: Verify the independent application

**Files:**
- Modify: `README.md` and `README_zh.md` only for verified local-use instructions.

**Interfaces:**
- Produces: install, start, test and sample-project instructions that work from the new repository alone.

- [ ] Run full pytest, Ruff, wheel build and headless import/start smoke checks.
- [ ] Search for runtime references to sibling `AutoReport`, `Nexgent`, `work`, user-home temp paths and prototype `pds_report` imports.
- [ ] Confirm Git status and commit final verification evidence.
