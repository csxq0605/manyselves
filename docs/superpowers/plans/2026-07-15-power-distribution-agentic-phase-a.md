# 配电安全专家多智能体 Phase A Implementation Plan

> **状态（2026-07-16）：** 本文是已完成并被全量工作流取代的历史 Phase A 计划。当前实现与后续变更以 `2026-07-16-power-distribution-workflow-alignment.md` 和同日设计文档为准。下方出现的旧交接源目录标签只描述当时的导入来源，不再构成运行时目录协议。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留 AutoReport PyQt GUI 和既有解析底座的前提下，交付一个真实可运行的 2.4 配电设备与元件风险分析纵向样板，由 Planner、2.4 Specialist、Evidence Auditor 三个独立 Agent Loop 自主研究、协作、返工并生成可溯源 DOCX。

**Architecture:** 内层继续使用 AutoReport 的模型—工具循环，但把固定 `AgentType` 改成动态 Agent ID，并用 Markdown + YAML frontmatter 注册每个角色、工具和预算；外层新增只负责阶段、依赖、会话隔离、结果汇合与返工的 WorkflowRunner。项目证据、本地参考和网络来源分别进入 `E-*`、`R-*`、`W-*` 账本，正文保持自然写作，隐藏 Claim Ledger 负责审计，最终通过收资包渲染器的本地适配层生成 DOCX。

**Tech Stack:** Python 3.12、Pydantic 2、PyYAML、asyncio、现有 Anthropic/OpenAI Provider 抽象、httpx、python-docx、PyQt6、pytest/pytest-asyncio。

## Global Constraints

- Python 版本保持 `>=3.12`，不引入 Nexgent 运行时或新的 Agent 框架依赖。
- AutoReport 是唯一代码底座；保留现有 PyQt GUI、项目目录树、预览、Provider、AgentLoop、MessageBus、TaskBoard 与解析/归一化能力。
- 用户只与 Main Agent 对话；Phase A 仅正式支持 `target_modules=["2.4"]`，其他组合必须明确返回 `unsupported_phase`，不能回退到确定性 Worker 冒充多智能体完成。
- 导入阶段只把选中的参考资料复制进项目 `Knowledge/`；运行时对 `Knowledge/` 下任意目录名中的支持文件一视同仁，均可形成 `R-*` 来源和引用。
- 运行时不得依据导入前的 01/02 标签、目录名、文件名或标题做白名单、黑名单或名称匹配；未选择的资料应留在 `Knowledge/` 之外。
- 客户现场事实只能来自项目资料与人工确认，形成 `E-*`；本地参考 `R-*` 和网络资料 `W-*` 均不能填补不存在的现场事实。
- Agent 自主决定直接写作、搜索本地参考、联网、询问同伴、继续行动或结束；外层 Workflow 不实现固定研究状态机。
- 正文不强制“事实—证据—风险—建议”重复结构；关键事实、数值和结论以自然脚注标记，完整可溯源关系保存在隐藏 Claim Ledger 与文末证据索引。
- 旧本地提示词资料仅可在开发期用于抽象匿名测试断言；运行测试不得读取项目 `Knowledge/` 之外的交接源目录。
- DOCX 沿用交接包 `core/docx_renderer.py` 的确定性排版能力；移除 Dify 包装，并禁用会新增、改写或替换 Agent 正文的内容生成 fallback。
- Phase A 的三条模型会话必须相互隔离；跨 Agent 仅传递 `TaskEnvelope`、类型化消息和已持久化成果。
- 任何 Agent 自然结束但未调用 `submit_result` 或 `report_blocked` 时只重试提醒一次；第二次仍未提交则保存原始输出并标记 `incomplete`。
- 2.4 局部返工必须进入原 2.4 Agent 的同一 session，最多 2 轮；Auditor 使用独立 session，不继承作者聊天历史。
- 单元测试使用脚本化 Fake Provider 和 Fake Web Backend，不访问真实网络或真实 LLM；真实联网只通过显式配置启用。

---

## Phase A 边界与验收路径

本计划有意只实现规格中的第一个可独立验收子项目：`ReportRequest(2.4)` → 既有收资/解析/证据归一化 → Planner Agent → 2.4 Specialist Agent → Evidence Auditor Agent → 同会话局部返工 → Claim/Citation 组装 → DOCX。2.1、2.2、2.3、2.5、Cross-module Reviewer 和 Chief Editor 不在本计划中；只有 Phase A 纵向样板通过后，才分别编写全量并行模块和跨模块编辑计划。

Phase A 完成必须同时证明三条行为路径：

1. 2.4 Specialist 判断已有证据足够，直接提交自然正文；
2. 2.4 Specialist 自主调用 `search_reference_library` 后提交，且 `R-*` 只用于专业解释；
3. Auditor 提交 blocking issue，Workflow 将修订任务送回同一 2.4 session，第二稿通过并渲染。

联网研究另用 Fake Web Backend 证明 `web_search` → `open_source` → `W-*` 闭环；不开启联网时不影响直接写作与本地研究。

## File Structure

### 新建文件

- `autoreport/core/reporting/agentic_models.py`：工作流任务、来源、研究笔记、Claim Ledger、提交成果和运行状态的唯一类型定义。
- `autoreport/core/reporting/prompts.py`：身份 system prompt 与动态 XML task context 的严格分离组装器。
- `autoreport/core/reporting/research/reference_library.py`：检索项目 `Knowledge/` 全部支持文件的本地参考后端。
- `autoreport/core/reporting/research/web.py`：可替换 WebResearchBackend、Brave 实现及 URL 打开白名单。
- `autoreport/core/reporting/research/__init__.py`：研究后端公开接口。
- `autoreport/core/reporting/source_ledger.py`：稳定分配/持久化 `R-*`、`W-*` 来源编号。
- `autoreport/core/reporting/claim_ledger.py`：验证 Claim 与来源边界，生成正文脚注计划和证据索引。
- `autoreport/core/reporting/agent_runner.py`：通过 MessageBus 驱动单个 AgentLoop，并等待类型化完成消息。
- `autoreport/core/reporting/workflow.py`：Phase A 外层阶段、返工、预算与中断恢复。
- `autoreport/core/reporting/preparation.py`：从旧 Service 提取的确定性收资、解析、证据归一化和覆盖评估。
- `autoreport/core/reporting/delivery.py`：Claim/Source Ledger、Markdown、审计记录与 DOCX 的唯一交付入口。
- `autoreport/core/tools/reporting_research_tools.py`：`search_project_evidence`、`search_reference_library`、`web_search`、`open_source`。
- `autoreport/core/tools/reporting_collaboration_tools.py`：`query_peer`、`reply_peer`、`submit_result`、`report_blocked`。
- `autoreport/core/reporting/rendering/pds_docx_renderer.py`：从交接包提取的确定性渲染核心，不含 Dify 或正文生成 fallback。
- `autoreport/core/reporting/rendering/handoff_docx.py`：把 Agent 成果与 CitationPlan 转成渲染器输入的适配层。
- `autoreport/templates/reporting/agents/report-planner.md`：Planner 稳定身份。
- `autoreport/templates/reporting/agents/module-2.4-specialist.md`：2.4 配电设备与元件风险诊断专家稳定身份。
- `autoreport/templates/reporting/agents/evidence-auditor.md`：独立证据审计员稳定身份。
- `tests/reporting/test_agentic_models.py`、`test_prompts.py`、`test_source_ledger.py`、`test_claim_ledger.py`、`test_agent_runner.py`、`test_workflow.py`：新增核心单元测试。
- `tests/reporting/research/test_reference_library.py`、`test_web.py`：本地/联网研究边界测试。
- `tests/reporting/rendering/test_handoff_docx.py`：渲染不改写正文与脚注/索引测试。
- `tests/reporting/integration/test_agentic_phase_a_24.py`：脚本化三 Agent 端到端验收。

### 修改文件

- `autoreport/interfaces/types.py`：消息和任务的 Agent 标识迁移为 `str`，新增类型化工作流消息。
- `autoreport/interfaces/__init__.py`：导出 `AgentId` 与新消息类型。
- `autoreport/core/reporting/config.py`：扩展 Agent frontmatter 字段与工具/Carrier 校验。
- `autoreport/core/reporting/store.py`：新增按 run 保存成果、来源和会话记录的方法。
- `autoreport/core/loops/agent_loop.py`：动态 Agent ID、显式 system prompt、最大 turns 和完成工具门禁。
- `autoreport/core/loops/bus.py`：新增带 predicate/timeout 的一次性等待。
- `autoreport/core/loops/manager.py`：按 Agent Registry 构造隔离 Loop 与角色级工具集合。
- `autoreport/core/tools/task_board.py`、`agent_tools.py`、`task_tools.py`：接受动态字符串 ID。
- `autoreport/app.py`、`autoreport/gui/main_window.py`、`autoreport/utils/agent_labels.py`：去除固定五 Agent 枚举假设。
- `autoreport/core/reporting/service.py`：Phase A 活跃入口切换到 AgenticWorkflowRunner；非 2.4 请求明确拒绝。
- `autoreport/core/tools/reporting_tool.py`：向 Main Agent 返回真实阶段、阻塞或产物，不调用旧 Worker。
- `autoreport/templates/reporting/workflows/phase-a.yml`：声明三 Agent pipeline 与最大返工次数。
- `autoreport/core/reporting/rendering/__init__.py`：导出新适配器。
- `tests/test_agent_loop.py`、`test_manager.py`、`test_task_board.py`、`test_prompt_loader.py`：动态 ID 回归测试。
- `tests/reporting/test_config.py`、`test_service.py`、`test_tool.py`、`rendering/test_docx.py`：更新为 Agentic Phase A 行为。

### 保留但退出 Phase A 活跃链的文件

- `autoreport/core/reporting/workers/generic.py`
- `autoreport/core/reporting/workers/module_24.py`
- `autoreport/core/reporting/workers/orchestrator.py`
- `autoreport/core/reporting/rendering/docx.py`

这些文件不在本计划中删除，以减少与既有测试和用户改动冲突；但 `ReportingService` 和 `RunReportingWorkflowTool` 不得再引用它们。任务 12 用导入图断言锁定这一边界。

---

### Task 1: 建立 Agentic 工作流类型与持久化边界

**Files:**
- Create: `autoreport/core/reporting/agentic_models.py`
- Modify: `autoreport/core/reporting/store.py`
- Test: `tests/reporting/test_agentic_models.py`
- Test: `tests/reporting/test_store.py`

**Interfaces:**
- Consumes: 现有 `ReportingModel`、`ModuleDraft`、`ReviewIssue`、`ReportingStore.write_json()`。
- Produces: `TaskEnvelope`、`SourceRecord`、`ResearchNote`、`ClaimRecord`、`ModuleSubmission`、`PlanSubmission`、`AuditSubmission`、`AgentResult`、`AgentRunStatus`、`CitationPlan`；`ReportingStore.write_run_model(run_id: str, relative: str, model: BaseModel) -> Path`。

- [ ] **Step 1: 写严格类型与来源边界失败测试**

```python
# tests/reporting/test_agentic_models.py
import pytest
from pydantic import ValidationError

from autoreport.core.reporting.agentic_models import (
    AgentResult,
    AgentRunStatus,
    ClaimRecord,
    ModuleSubmission,
    SourceKind,
    SourceRecord,
    TaskEnvelope,
)


def test_task_envelope_keeps_dynamic_context_out_of_identity():
    envelope = TaskEnvelope(
        task_id="task-24",
        run_id="run-1",
        agent_id="module-2.4-specialist",
        objective="完成 2.4 配电设备与元件风险分析",
        input_refs=["evidence.jsonl"],
        allowed_outputs=["module_submission"],
    )
    assert envelope.agent_id == "module-2.4-specialist"
    assert envelope.revision == 0


def test_project_fact_claim_cannot_use_only_reference_source():
    with pytest.raises(ValidationError, match="project_fact.*E-"):
        ClaimRecord(
            id="C-001",
            module_id="2.4",
            text="现场断路器存在过热",
            claim_type="project_fact",
            source_ids=["R-001"],
        )


def test_source_prefix_must_match_kind():
    with pytest.raises(ValidationError, match="source id"):
        SourceRecord(
            id="W-001",
            kind=SourceKind.LOCAL_REFERENCE,
            title="参考条款",
            locator="Knowledge/标准/a.md",
        )


def test_agent_result_requires_typed_payload_when_completed():
    with pytest.raises(ValidationError, match="payload"):
        AgentResult(
            task_id="task-24",
            run_id="run-1",
            agent_id="module-2.4-specialist",
            session_id="session-24",
            status=AgentRunStatus.COMPLETED,
        )


def test_module_submission_preserves_free_form_markdown():
    submission = ModuleSubmission(
        module_id="2.4",
        markdown="## 2.4 配电设备与元件\n\n温升与连接状态应结合分析。",
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    assert "事实：" not in submission.markdown
```

```python
# tests/reporting/test_store.py
from autoreport.core.reporting.agentic_models import SourceKind, SourceRecord
from autoreport.core.reporting.store import ReportingStore


def test_write_run_model_stays_beneath_run_directory(tmp_path):
    store = ReportingStore(tmp_path)
    source = SourceRecord(
        id="R-001",
        kind=SourceKind.LOCAL_REFERENCE,
        title="参考资料",
        locator="Knowledge/标准/a.md",
    )
    path = store.write_run_model("run-1", "ledgers/sources/R-001.json", source)
    assert path == tmp_path / "Work/runs/run-1/ledgers/sources/R-001.json"
    assert '"id": "R-001"' in path.read_text(encoding="utf-8")
```

- [ ] **Step 2: 运行测试并确认因模块/方法缺失而失败**

Run: `uv run pytest tests/reporting/test_agentic_models.py tests/reporting/test_store.py -q`

Expected: collection FAIL，包含 `ModuleNotFoundError: autoreport.core.reporting.agentic_models`。

- [ ] **Step 3: 实现严格模型与 run 目录写入**

```python
# autoreport/core/reporting/agentic_models.py
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import CoverageMatrix, EvidenceItem, PhotoAsset


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceKind(StrEnum):
    PROJECT_EVIDENCE = "project_evidence"
    LOCAL_REFERENCE = "local_reference"
    WEB = "web"


SOURCE_PREFIX = {
    SourceKind.PROJECT_EVIDENCE: "E-",
    SourceKind.LOCAL_REFERENCE: "R-",
    SourceKind.WEB: "W-",
}


class TaskEnvelope(StrictModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    allowed_outputs: list[str] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)
    prior_result_ref: str | None = None
    issue_refs: list[str] = Field(default_factory=list)


class PreparationResult(StrictModel):
    input_refs: list[str] = Field(min_length=1)
    evidence_items: list[EvidenceItem]
    coverage: CoverageMatrix
    photo_assets: list[PhotoAsset] = Field(default_factory=list)


class SourceRecord(StrictModel):
    id: str = Field(min_length=3)
    kind: SourceKind
    title: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    publisher: str | None = None
    published_at: str | None = None
    accessed_at: str | None = None
    scope_note: str | None = None
    content_sha256: str | None = None

    @model_validator(mode="after")
    def id_matches_kind(self) -> "SourceRecord":
        if not self.id.startswith(SOURCE_PREFIX[self.kind]):
            raise ValueError("source id prefix does not match kind")
        return self


class ResearchNote(StrictModel):
    id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    synthesis: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    applicability: str = Field(min_length=1)


class ClaimRecord(StrictModel):
    id: str = Field(pattern=r"^C-")
    module_id: Literal["2.4"]
    text: str = Field(min_length=1)
    claim_type: Literal[
        "project_fact", "technical_interpretation", "risk_judgment", "recommendation"
    ]
    source_ids: list[str] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    footnote_required: bool = True
    unresolved: bool = False

    @model_validator(mode="after")
    def project_fact_uses_evidence(self) -> "ClaimRecord":
        if self.claim_type == "project_fact" and not any(
            source_id.startswith("E-") for source_id in self.source_ids
        ):
            raise ValueError("project_fact requires at least one E-* source")
        return self


class ModuleSubmission(StrictModel):
    kind: Literal["module_submission"] = "module_submission"
    module_id: Literal["2.4"]
    markdown: str = Field(min_length=1)
    claims: list[ClaimRecord]
    source_ids: list[str]
    unresolved_questions: list[str]
    revision: int = Field(ge=0)


class PlanSubmission(StrictModel):
    kind: Literal["plan_submission"] = "plan_submission"
    module_tasks: list[TaskEnvelope] = Field(min_length=1)
    rationale: str = Field(min_length=1)


class AuditSubmission(StrictModel):
    kind: Literal["audit_submission"] = "audit_submission"
    module_id: Literal["2.4"]
    approved: bool
    issues: list[dict]
    checked_claim_ids: list[str]


Submission = Annotated[
    ModuleSubmission | PlanSubmission | AuditSubmission,
    Field(discriminator="kind"),
]


class AgentRunStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class AgentResult(StrictModel):
    task_id: str
    run_id: str
    agent_id: str
    session_id: str
    status: AgentRunStatus
    payload: Submission | None = None
    raw_output: str = ""
    reason: str | None = None

    @model_validator(mode="after")
    def completed_has_payload(self) -> "AgentResult":
        if self.status == AgentRunStatus.COMPLETED and self.payload is None:
            raise ValueError("completed result requires payload")
        return self


class CitationEntry(StrictModel):
    marker: int = Field(ge=1)
    claim_id: str
    source_ids: list[str] = Field(min_length=1)


class CitationPlan(StrictModel):
    entries: list[CitationEntry]
    evidence_index_markdown: str
```

```python
# add to autoreport/core/reporting/store.py
from pydantic import BaseModel

    def write_run_model(self, run_id: str, relative: str, model: BaseModel) -> Path:
        safe_run_id = Path(run_id).name
        if safe_run_id != run_id or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("run path must stay beneath Work/runs/<run_id>")
        return self.write_json(
            f"Work/runs/{safe_run_id}/{relative}",
            model.model_dump(mode="json"),
        )
```

- [ ] **Step 4: 运行模型与存储测试**

Run: `uv run pytest tests/reporting/test_agentic_models.py tests/reporting/test_store.py -q`

Expected: `7 passed`。

- [ ] **Step 5: 提交类型边界**

```bash
git add autoreport/core/reporting/agentic_models.py autoreport/core/reporting/store.py tests/reporting/test_agentic_models.py tests/reporting/test_store.py
git commit -m "feat: add agentic report workflow models"
```

### Task 2: 将固定 AgentType 迁移为动态 Agent ID

**Files:**
- Modify: `autoreport/interfaces/types.py`
- Modify: `autoreport/interfaces/__init__.py`
- Modify: `autoreport/core/tools/task_board.py`
- Modify: `autoreport/core/tools/agent_tools.py`
- Modify: `autoreport/core/tools/task_tools.py`
- Modify: `autoreport/core/loops/agent_loop.py`
- Modify: `autoreport/core/loops/manager.py`
- Modify: `autoreport/app.py`
- Modify: `autoreport/gui/main_window.py`
- Modify: `autoreport/utils/agent_labels.py`
- Test: `tests/test_task_board.py`
- Test: `tests/test_agent_loop.py`
- Test: `tests/test_manager.py`

**Interfaces:**
- Consumes: existing enum values through `normalize_agent_id(value: str | AgentType) -> str` compatibility adapter.
- Produces: `AgentId = str`; all message/task `agent_type`, `source_agent`, `target_agent` fields are `str`; `LoopManager.get_loop(agent_id: str) -> AgentLoop | None`.

- [ ] **Step 1: 写任意注册 ID 可路由的失败测试**

```python
# append to tests/test_task_board.py
def test_task_board_accepts_registry_agent_ids():
    board = TaskBoard()
    task = board.create_task(
        source="report-planner",
        target="module-2.4-specialist",
        brief="完成 2.4",
    )
    assert board.get_todolist("module-2.4-specialist") == [task]
    assert set(board.get_all_tasks()) == {"report-planner", "module-2.4-specialist"}
```

```python
# append to tests/test_agent_loop.py
def test_agent_loop_keeps_dynamic_agent_id(loop_dependencies):
    loop = AgentLoop(agent_type="evidence-auditor", **loop_dependencies)
    assert loop.agent_id == "evidence-auditor"
    assert loop.agent_type == "evidence-auditor"
```

```python
# append to tests/test_manager.py
def test_manager_loop_lookup_uses_string_id(manager):
    manager._loops["module-2.4-specialist"] = object()
    assert manager.get_loop("module-2.4-specialist") is manager._loops["module-2.4-specialist"]
```

- [ ] **Step 2: 运行聚焦测试并确认枚举校验失败**

Run: `uv run pytest tests/test_task_board.py::test_task_board_accepts_registry_agent_ids tests/test_agent_loop.py::test_agent_loop_keeps_dynamic_agent_id tests/test_manager.py::test_manager_loop_lookup_uses_string_id -q`

Expected: FAIL，Pydantic 报告 `agent_type/source_agent/target_agent` 不是合法 `AgentType`，或 `AgentLoop` 缺少 `agent_id`。

- [ ] **Step 3: 增加兼容适配器并替换生产类型注解**

```python
# replace AgentType-only annotations in autoreport/interfaces/types.py
from typing import TypeAlias

AgentId: TypeAlias = str


def normalize_agent_id(value: str | AgentType) -> str:
    return value.value if isinstance(value, AgentType) else str(value)

# UserMessage/AgentResponse/ToolCallMessage/ToolResult/StatusChange/
# TaskItem/TaskUpdateMessage/QueueUpdateMessage/ReportMessage/SystemNotice fields:
# agent_type: AgentId
# source_agent: AgentId
# target_agent: AgentId
```

```python
# core replacement pattern in autoreport/core/loops/agent_loop.py
from ...interfaces.types import AgentId, normalize_agent_id

    def __init__(self, agent_type: AgentId | AgentType, ...):
        self.agent_id = normalize_agent_id(agent_type)
        self.agent_type = self.agent_id

# replace comparisons
if self.agent_id != AgentType.MAIN.value:
    ...

# replace serialized labels
return self.agent_id
```

```python
# replace get_all_tasks in autoreport/core/tools/task_board.py
    def get_all_tasks(self) -> dict[str, dict[str, list[TaskItem]]]:
        agent_ids = sorted(
            {task.source_agent for task in self._tasks}
            | {task.target_agent for task in self._tasks}
        )
        return {
            agent_id: {
                "todolist": self.get_todolist(agent_id),
                "waitlist": self.get_waitlist(agent_id),
            }
            for agent_id in agent_ids
        }

    @staticmethod
    def _agent_label(agent_type: str) -> str:
        return agent_type.replace("_", " ").replace("-", " ").title()
```

在 `agent_tools.py`、`task_tools.py`、`manager.py`、`app.py`、`main_window.py` 和 `agent_labels.py` 中执行同一机械规则：容器键、消息值和方法参数统一用规范化字符串；只有向后兼容入口可以接受 `AgentType`，内部不得调用 `.value`。`LoopManager.get_loop` 的完整实现为：

```python
    def get_loop(self, agent_id: str | AgentType) -> AgentLoop | None:
        return self._loops.get(normalize_agent_id(agent_id))
```

- [ ] **Step 4: 用检索确认生产代码不再依赖枚举迭代或 `.value`**

Run: `rg -n 'for .* in AgentType|agent_type\.value|source_agent\.value|target_agent\.value' autoreport`

Expected: no output；兼容枚举定义本身可以保留。

- [ ] **Step 5: 运行 Loop/TaskBoard/GUI 回归测试**

Run: `uv run pytest tests/test_task_board.py tests/test_agent_loop.py tests/test_manager.py tests/test_main_window.py -q`

Expected: PASS。

- [ ] **Step 6: 提交动态 Agent ID 迁移**

```bash
git add autoreport/interfaces autoreport/core/loops autoreport/core/tools/task_board.py autoreport/core/tools/agent_tools.py autoreport/core/tools/task_tools.py autoreport/app.py autoreport/gui/main_window.py autoreport/utils/agent_labels.py tests/test_task_board.py tests/test_agent_loop.py tests/test_manager.py
git commit -m "refactor: support registry-defined agent ids"
```

### Task 3: 扩展 Agent Registry 并严格分离身份与任务上下文

**Files:**
- Modify: `autoreport/core/reporting/config.py`
- Create: `autoreport/core/reporting/prompts.py`
- Test: `tests/reporting/test_config.py`
- Test: `tests/reporting/test_prompts.py`

**Interfaces:**
- Consumes: `TaskEnvelope.model_dump_json()`、Markdown + YAML frontmatter。
- Produces: `AgentDefinition(name, description, model, tools, disallowed_tools, max_turns, effort, memory, background, reads, writes, instructions, source_path)`；`PromptAssembler.system_prompt(definition) -> str`；`PromptAssembler.task_message(envelope, shared_artifacts) -> str`。

- [ ] **Step 1: 写 frontmatter 与禁止注入失败测试**

```python
# append to tests/reporting/test_config.py
def test_load_nexgent_style_agent_definition(tmp_path):
    path = tmp_path / "agent.md"
    path.write_text(
        """---
name: module-2.4-specialist
description: 配电设备与元件风险诊断专家
model: inherit
tools: [search_project_evidence, search_reference_library, submit_result]
disallowedTools: [exec]
maxTurns: 12
effort: high
memory: task
background: true
reads: [evidence_items]
writes: [module_drafts]
---
<role_and_perspective>从设备机理与运行条件综合判断。</role_and_perspective>
""",
        encoding="utf-8",
    )
    definition = load_agent_definition(path)
    assert definition.name == "module-2.4-specialist"
    assert definition.disallowed_tools == ["exec"]
    assert definition.max_turns == 12
```

```python
# tests/reporting/test_prompts.py
from autoreport.core.reporting.agentic_models import TaskEnvelope
from autoreport.core.reporting.config import load_agent_definition
from autoreport.core.reporting.prompts import PromptAssembler


def test_system_prompt_contains_identity_but_not_runtime_corpus(tmp_path):
    identity = tmp_path / "agent.md"
    identity.write_text(
        "---\nname: auditor\ndescription: 审计员\ntools: [submit_result]\nmaxTurns: 4\n---\n"
        "<role_and_perspective>独立核验。</role_and_perspective>",
        encoding="utf-8",
    )
    prompt = PromptAssembler.system_prompt(load_agent_definition(identity))
    assert "独立核验" in prompt
    assert str(tmp_path) not in prompt


def test_task_context_is_xml_and_separate_from_system(tmp_path):
    envelope = TaskEnvelope(
        task_id="t1", run_id="r1", agent_id="auditor", objective="审计 2.4"
    )
    message = PromptAssembler.task_message(envelope, ["drafts/2.4.json"])
    assert message.startswith("<task_context>")
    assert "<objective>审计 2.4</objective>" in message
    assert "<shared_artifact>drafts/2.4.json</shared_artifact>" in message
```

- [ ] **Step 2: 运行测试并确认旧 AgentDefinition 不接受新字段**

Run: `uv run pytest tests/reporting/test_config.py tests/reporting/test_prompts.py -q`

Expected: FAIL，包含 `extra_forbidden` 或 `PromptAssembler` 导入失败。

- [ ] **Step 3: 实现 Registry 字段与 PromptAssembler**

```python
# replace AgentDefinition in autoreport/core/reporting/config.py
class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    model: str = "inherit"
    tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list, alias="disallowedTools")
    max_turns: int = Field(default=8, ge=1, le=40, alias="maxTurns")
    effort: Literal["low", "medium", "high"] = "medium"
    memory: Literal["task", "session"] = "task"
    background: bool = True
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    instructions: str = Field(min_length=1)
    source_path: Path

    @property
    def id(self) -> str:
        return self.name

    @model_validator(mode="after")
    def tool_sets_do_not_overlap(self) -> "AgentDefinition":
        overlap = sorted(set(self.tools) & set(self.disallowed_tools))
        if overlap:
            raise ValueError(f"tools also listed in disallowedTools: {overlap}")
        return self
```

```python
# autoreport/core/reporting/prompts.py
from xml.sax.saxutils import escape

from .agentic_models import TaskEnvelope
from .config import AgentDefinition


class PromptAssembler:
    @staticmethod
    def system_prompt(definition: AgentDefinition) -> str:
        return (
            f"<agent_identity name=\"{escape(definition.name)}\">\n"
            f"<description>{escape(definition.description)}</description>\n"
            f"{definition.instructions.strip()}\n"
            "</agent_identity>"
        )

    @staticmethod
    def task_message(envelope: TaskEnvelope, shared_artifacts: list[str]) -> str:
        inputs = "\n".join(
            f"<input_ref>{escape(ref)}</input_ref>" for ref in envelope.input_refs
        )
        artifacts = "\n".join(
            f"<shared_artifact>{escape(ref)}</shared_artifact>"
            for ref in shared_artifacts
        )
        constraints = "\n".join(
            f"<constraint>{escape(value)}</constraint>" for value in envelope.constraints
        )
        issues = "\n".join(
            f"<issue_ref>{escape(ref)}</issue_ref>" for ref in envelope.issue_refs
        )
        return (
            "<task_context>\n"
            f"<task_id>{escape(envelope.task_id)}</task_id>\n"
            f"<run_id>{escape(envelope.run_id)}</run_id>\n"
            f"<revision>{envelope.revision}</revision>\n"
            f"<objective>{escape(envelope.objective)}</objective>\n"
            f"{inputs}\n{artifacts}\n{constraints}\n{issues}\n"
            "</task_context>"
        )
```

更新 `load_agent_definitions()` 以 `definition.name` 为键；`load_agent_definition()` 同时兼容旧 `id/role` 文件时，先在解析数据上执行以下一次性规范化，直到 Task 10 替换打包身份文件：

```python
if "name" not in data and "id" in data:
    data["name"] = data.pop("id")
if "description" not in data and "role" in data:
    data["description"] = data.pop("role")
```

- [ ] **Step 4: 运行配置和 Prompt 测试**

Run: `uv run pytest tests/reporting/test_config.py tests/reporting/test_prompts.py -q`

Expected: PASS。

- [ ] **Step 5: 提交身份/上下文分层**

```bash
git add autoreport/core/reporting/config.py autoreport/core/reporting/prompts.py tests/reporting/test_config.py tests/reporting/test_prompts.py
git commit -m "feat: load role-aware agent identities"
```

### Task 4: 实现项目本地参考搜索与 `02` 硬隔离

**Files:**
- Create: `autoreport/core/reporting/research/__init__.py`
- Create: `autoreport/core/reporting/research/reference_library.py`
- Create: `autoreport/core/reporting/source_ledger.py`
- Create: `autoreport/core/tools/reporting_research_tools.py`
- Test: `tests/reporting/research/test_reference_library.py`
- Test: `tests/reporting/test_source_ledger.py`

**Interfaces:**
- Consumes: workspace `Path`、`ReportingStore`。
- Produces: `ReferenceLibrary.search(query: str, limit: int = 5) -> list[ReferenceHit]`；`SourceLedger.register_local(hit) -> SourceRecord`；`SearchReferenceLibraryTool.__call__(query, limit) -> dict`。

- [ ] **Step 1: 写只搜索 01 且永不触碰 02 的失败测试**

```python
# tests/reporting/research/test_reference_library.py
from autoreport.core.reporting.research.reference_library import ReferenceLibrary


def test_reference_search_reads_arbitrary_knowledge_subdirectories(tmp_path):
    standards = tmp_path / "Knowledge/标准"
    manuals = tmp_path / "Knowledge/供应商/手册"
    standards.mkdir(parents=True)
    manuals.mkdir(parents=True)
    (standards / "温升.md").write_text("连接点温升需要结合负荷与环境判断", encoding="utf-8")
    (manuals / "低压柜.md").write_text("连接点温升需要复核负荷", encoding="utf-8")

    hits = ReferenceLibrary(tmp_path).search("连接点温升")

    assert {hit.relative_path for hit in hits} == {
        "Knowledge/标准/温升.md",
        "Knowledge/供应商/手册/低压柜.md",
    }


def test_reference_search_does_not_fallback_to_whole_knowledge(tmp_path):
    knowledge = tmp_path / "Knowledge"
    knowledge.mkdir()
    (knowledge / "loose.md").write_text("断路器参考", encoding="utf-8")
    assert ReferenceLibrary(tmp_path).search("断路器") == []
```

```python
# tests/reporting/test_source_ledger.py
from autoreport.core.reporting.source_ledger import SourceLedger


def test_same_local_source_receives_stable_r_id(tmp_path):
    ledger = SourceLedger(tmp_path, "run-1")
    first = ledger.register_local("标准摘录", "Knowledge/标准/a.md", "abc")
    second = ledger.register_local("标准摘录", "Knowledge/标准/a.md", "abc")
    assert first.id == second.id == "R-001"


def test_project_evidence_keeps_existing_e_id(tmp_path):
    ledger = SourceLedger(tmp_path, "run-1")
    source = ledger.register_project(
        evidence_id="E-001",
        title="红外检测记录",
        locator="Inputs/红外.xlsx#Sheet1!B2",
        content="连接点 86℃",
    )
    assert source.id == "E-001"
```

- [ ] **Step 2: 运行测试并确认研究模块缺失**

Run: `uv run pytest tests/reporting/research/test_reference_library.py tests/reporting/test_source_ledger.py -q`

Expected: collection FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现明确根目录、文本切片与稳定来源编号**

```python
# autoreport/core/reporting/research/reference_library.py
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReferenceHit:
    title: str
    relative_path: str
    snippet: str
    score: int


class ReferenceLibrary:
    TEXT_SUFFIXES = {".md", ".txt", ".csv", ".html"}

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.root = (self.workspace / "Knowledge").resolve()

    def search(self, query: str, limit: int = 5) -> list[ReferenceHit]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms or not self.root.is_dir():
            return []
        hits: list[ReferenceHit] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in self.TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            lowered = text.casefold()
            score = sum(lowered.count(term) for term in terms)
            if score == 0:
                continue
            first = min(lowered.find(term) for term in terms if term in lowered)
            start = max(0, first - 160)
            snippet = " ".join(text[start : first + 440].split())
            hits.append(
                ReferenceHit(
                    title=path.stem,
                    relative_path=path.relative_to(self.workspace).as_posix(),
                    snippet=snippet,
                    score=score,
                )
            )
        return sorted(hits, key=lambda hit: (-hit.score, hit.relative_path))[:limit]
```

```python
# autoreport/core/reporting/source_ledger.py
import hashlib
import json
from pathlib import Path

from .agentic_models import SourceKind, SourceRecord


class SourceLedger:
    def __init__(self, workspace: Path, run_id: str):
        self.path = Path(workspace) / f"Work/runs/{Path(run_id).name}/ledgers/sources.json"
        self.records: list[SourceRecord] = []
        if self.path.exists():
            self.records = [SourceRecord.model_validate(item) for item in json.loads(self.path.read_text())]

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([record.model_dump(mode="json") for record in self.records], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def register_local(self, title: str, locator: str, content: str) -> SourceRecord:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        for record in self.records:
            if record.kind == SourceKind.LOCAL_REFERENCE and record.locator == locator and record.content_sha256 == digest:
                return record
        record = SourceRecord(
            id=f"R-{sum(r.kind == SourceKind.LOCAL_REFERENCE for r in self.records) + 1:03d}",
            kind=SourceKind.LOCAL_REFERENCE,
            title=title,
            locator=locator,
            content_sha256=digest,
        )
        self.records.append(record)
        self._persist()
        return record

    def register_project(
        self, evidence_id: str, title: str, locator: str, content: str
    ) -> SourceRecord:
        if not evidence_id.startswith("E-"):
            raise ValueError("project evidence id must start with E-")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        for record in self.records:
            if record.id == evidence_id:
                if record.locator != locator or record.content_sha256 != digest:
                    raise ValueError(f"conflicting project evidence id: {evidence_id}")
                return record
        record = SourceRecord(
            id=evidence_id,
            kind=SourceKind.PROJECT_EVIDENCE,
            title=title,
            locator=locator,
            content_sha256=digest,
        )
        self.records.append(record)
        self._persist()
        return record
```

```python
# core of autoreport/core/tools/reporting_research_tools.py
from pathlib import Path

from ..reporting.models import EvidenceItem
from ..reporting.research.reference_library import ReferenceLibrary
from ..reporting.source_ledger import SourceLedger
from .registry import Tool


class SearchReferenceLibraryTool(Tool):
    name = "search_reference_library"
    description = "按当前专业问题搜索项目 01 参考文档；结果是参考来源，不是现场事实。"

    def __init__(self, library: ReferenceLibrary, ledger: SourceLedger):
        self.library = library
        self.ledger = ledger

    async def __call__(self, query: str, limit: int = 5) -> dict:
        hits = self.library.search(query, limit)
        return {
            "hits": [
                {
                    "source_id": self.ledger.register_local(hit.title, hit.relative_path, hit.snippet).id,
                    "title": hit.title,
                    "locator": hit.relative_path,
                    "snippet": hit.snippet,
                }
                for hit in hits
            ]
        }


class SearchProjectEvidenceTool(Tool):
    name = "search_project_evidence"
    description = "搜索已归一化的客户项目证据，返回 E-*、事实和精确文件定位。"

    def __init__(self, workspace: Path):
        self.path = Path(workspace) / "Work/evidence.jsonl"

    async def __call__(self, query: str, limit: int = 10) -> dict:
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not self.path.exists() or not terms:
            return {"hits": []}
        items = [
            EvidenceItem.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ranked = []
        for item in items:
            text = f"{item.subject} {item.fact} {item.value or ''} {item.unit or ''}".casefold()
            score = sum(text.count(term) for term in terms)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
        return {
            "hits": [item.model_dump(mode="json") for _, item in ranked[:limit]]
        }
```

- [ ] **Step 4: 运行隔离测试并扫描运行代码中的 02 字面量**

Run: `uv run pytest tests/reporting/research/test_reference_library.py tests/reporting/test_source_ledger.py -q`

Expected: `4 passed`。

Run: `rg -n 'Knowledge/' autoreport/core/reporting autoreport/core/tools`

Expected: Knowledge 只作为统一项目参考根目录出现；不得存在按子目录名或文件名区分引用资格的逻辑。

- [ ] **Step 5: 提交本地参考搜索**

```bash
git add autoreport/core/reporting/research autoreport/core/reporting/source_ledger.py autoreport/core/tools/reporting_research_tools.py tests/reporting/research tests/reporting/test_source_ledger.py
git commit -m "feat: add opt-in local reference research"
```

### Task 5: 增加可选联网研究与搜索结果 URL 白名单

**Files:**
- Create: `autoreport/core/reporting/research/web.py`
- Modify: `autoreport/core/reporting/research/__init__.py`
- Modify: `autoreport/core/reporting/source_ledger.py`
- Modify: `autoreport/core/tools/reporting_research_tools.py`
- Test: `tests/reporting/research/test_web.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient`、`SourceLedger`。
- Produces: `WebResearchBackend.search(query: str, limit: int) -> list[WebSearchHit]`、`open(url: str) -> OpenedWebSource`；`BraveWebResearchBackend`；`DisabledWebResearchBackend`；`WebSearchTool`；`OpenSourceTool`。

- [ ] **Step 1: 写禁用联网、白名单打开和 W 编号失败测试**

```python
# tests/reporting/research/test_web.py
import pytest

from autoreport.core.reporting.research.web import (
    DisabledWebResearchBackend,
    OpenedWebSource,
    WebSearchHit,
)
from autoreport.core.reporting.source_ledger import SourceLedger
from autoreport.core.tools.reporting_research_tools import OpenSourceTool, WebSearchTool


class FakeWebBackend:
    def __init__(self):
        self.allowed_urls: set[str] = set()

    async def search(self, query: str, limit: int = 5):
        hit = WebSearchHit(
            title="机构技术说明", url="https://example.org/guide", snippet="连接温升机理"
        )
        self.allowed_urls.add(hit.url)
        return [hit]

    async def open(self, url: str):
        if url not in self.allowed_urls:
            raise ValueError("URL was not returned by web_search")
        return OpenedWebSource(
            title="机构技术说明", url=url, text="完整技术说明", publisher="Example Institute"
        )


@pytest.mark.asyncio
async def test_web_tools_create_w_source_only_after_search(tmp_path):
    backend = FakeWebBackend()
    ledger = SourceLedger(tmp_path, "run-1")
    search = WebSearchTool(backend)
    opener = OpenSourceTool(backend, ledger)

    result = await search(query="配电连接温升", limit=3)
    opened = await opener(url=result["hits"][0]["url"])

    assert opened["source_id"] == "W-001"
    assert opened["publisher"] == "Example Institute"


@pytest.mark.asyncio
async def test_open_rejects_url_not_returned_by_search(tmp_path):
    with pytest.raises(ValueError, match="not returned"):
        await OpenSourceTool(FakeWebBackend(), SourceLedger(tmp_path, "run-1"))(
            url="https://untrusted.example/"
        )


@pytest.mark.asyncio
async def test_disabled_web_backend_explains_configuration():
    with pytest.raises(RuntimeError, match="BRAVE_SEARCH_API_KEY"):
        await DisabledWebResearchBackend().search("断路器", 3)
```

- [ ] **Step 2: 运行测试并确认 Web 类型缺失**

Run: `uv run pytest tests/reporting/research/test_web.py -q`

Expected: collection FAIL，包含无法导入 `WebSearchHit`。

- [ ] **Step 3: 实现协议、禁用后端和 Brave 后端**

```python
# autoreport/core/reporting/research/web.py
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class WebSearchHit:
    title: str
    url: str
    snippet: str
    publisher: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class OpenedWebSource:
    title: str
    url: str
    text: str
    publisher: str | None = None
    published_at: str | None = None


class WebResearchBackend(Protocol):
    async def search(self, query: str, limit: int = 5) -> list[WebSearchHit]: ...
    async def open(self, url: str) -> OpenedWebSource: ...


class DisabledWebResearchBackend:
    async def search(self, query: str, limit: int = 5) -> list[WebSearchHit]:
        raise RuntimeError("web research is disabled; set BRAVE_SEARCH_API_KEY to enable it")

    async def open(self, url: str) -> OpenedWebSource:
        raise RuntimeError("web research is disabled; set BRAVE_SEARCH_API_KEY to enable it")


class BraveWebResearchBackend:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        self.allowed_urls: set[str] = set()

    async def search(self, query: str, limit: int = 5) -> list[WebSearchHit]:
        response = await self.client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 10)},
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
        )
        response.raise_for_status()
        hits = [
            WebSearchHit(
                title=item["title"],
                url=item["url"],
                snippet=item.get("description", ""),
            )
            for item in response.json().get("web", {}).get("results", [])
        ]
        self.allowed_urls.update(hit.url for hit in hits)
        return hits

    async def open(self, url: str) -> OpenedWebSource:
        if url not in self.allowed_urls:
            raise ValueError("URL was not returned by web_search")
        response = await self.client.get(url)
        response.raise_for_status()
        text = " ".join(response.text.split())[:30000]
        return OpenedWebSource(title=url, url=url, text=text)
```

为 `SourceLedger` 增加稳定 `W-*` 注册：

```python
    def register_web(
        self,
        title: str,
        url: str,
        content: str,
        publisher: str | None = None,
        published_at: str | None = None,
    ) -> SourceRecord:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        for record in self.records:
            if record.kind == SourceKind.WEB and record.locator == url and record.content_sha256 == digest:
                return record
        record = SourceRecord(
            id=f"W-{sum(r.kind == SourceKind.WEB for r in self.records) + 1:03d}",
            kind=SourceKind.WEB,
            title=title,
            locator=url,
            publisher=publisher,
            published_at=published_at,
            content_sha256=digest,
        )
        self.records.append(record)
        self._persist()
        return record
```

```python
# append to autoreport/core/tools/reporting_research_tools.py
class WebSearchTool(Tool):
    name = "web_search"
    description = "按需搜索公开网络资料；搜索结果不能作为客户现场事实。"

    def __init__(self, backend: WebResearchBackend):
        self.backend = backend

    async def __call__(self, query: str, limit: int = 5) -> dict:
        hits = await self.backend.search(query, limit)
        return {"hits": [hit.__dict__ for hit in hits]}


class OpenSourceTool(Tool):
    name = "open_source"
    description = "打开本次 web_search 返回的 URL，并把网页登记为 W-* 来源。"

    def __init__(self, backend: WebResearchBackend, ledger: SourceLedger):
        self.backend = backend
        self.ledger = ledger

    async def __call__(self, url: str) -> dict:
        opened = await self.backend.open(url)
        source = self.ledger.register_web(
            opened.title, opened.url, opened.text, opened.publisher, opened.published_at
        )
        return {"source_id": source.id, **opened.__dict__}
```

- [ ] **Step 4: 运行联网工具单元测试**

Run: `uv run pytest tests/reporting/research/test_web.py -q`

Expected: `3 passed`，无网络请求。

- [ ] **Step 5: 提交可选联网研究**

```bash
git add autoreport/core/reporting/research autoreport/core/reporting/source_ledger.py autoreport/core/tools/reporting_research_tools.py tests/reporting/research/test_web.py
git commit -m "feat: add optional source-ledger web research"
```

### Task 6: 增加类型化协作、提交工具与 MessageBus 一次性等待

**Files:**
- Modify: `autoreport/interfaces/types.py`
- Modify: `autoreport/core/loops/bus.py`
- Create: `autoreport/core/tools/reporting_collaboration_tools.py`
- Test: `tests/test_message_bus.py`
- Test: `tests/reporting/test_collaboration_tools.py`

**Interfaces:**
- Consumes: `AgentResult`、`TaskEnvelope`、`ReportingStore`、`MessageBus.publish()`。
- Produces: `PeerQueryMessage`、`PeerReplyMessage`、`AgentResultMessage`；`MessageBus.wait_for(message_cls, predicate, timeout) -> Message`；四个协作工具。

- [ ] **Step 1: 写消息匹配与提交持久化失败测试**

```python
# append to tests/test_message_bus.py
import asyncio

import pytest

from autoreport.interfaces.types import AgentResultMessage


@pytest.mark.asyncio
async def test_wait_for_resolves_only_matching_task(bus):
    waiter = asyncio.create_task(
        bus.wait_for(AgentResultMessage, lambda message: message.task_id == "wanted", 1.0)
    )
    await asyncio.sleep(0)
    await bus.publish(
        AgentResultMessage(
            task_id="other", run_id="r1", agent_type="auditor", result_path="other.json"
        )
    )
    await bus.publish(
        AgentResultMessage(
            task_id="wanted", run_id="r1", agent_type="auditor", result_path="wanted.json"
        )
    )
    assert (await waiter).result_path == "wanted.json"
```

```python
# tests/reporting/test_collaboration_tools.py
import pytest

from autoreport.core.reporting.store import ReportingStore
from autoreport.core.tools.reporting_collaboration_tools import (
    PeerMessageRouter,
    QueryPeerTool,
    ReplyPeerTool,
    SubmitResultTool,
)
from autoreport.interfaces.types import AgentResultMessage, UserMessage


@pytest.mark.asyncio
async def test_submit_result_persists_typed_payload_and_publishes(bus, tmp_path):
    received = []
    bus.subscribe(AgentResultMessage, lambda message: received.append(message))
    tool = SubmitResultTool(
        agent_id="module-2.4-specialist",
        session_id="session-24",
        run_id="run-1",
        task_id="task-24",
        store=ReportingStore(tmp_path),
        bus=bus,
    )
    result = await tool(
        payload={
            "kind": "module_submission",
            "module_id": "2.4",
            "markdown": "设备分析正文",
            "claims": [],
            "source_ids": [],
            "unresolved_questions": [],
            "revision": 0,
        }
    )
    assert result["status"] == "completed"
    assert received[0].task_id == "task-24"
    assert (tmp_path / received[0].result_path).exists()


@pytest.mark.asyncio
async def test_query_peer_waits_for_isolated_peer_reply(bus):
    PeerMessageRouter(bus)

    async def planner_reply(message):
        if not isinstance(message, UserMessage) or message.agent_type != "report-planner":
            return
        await ReplyPeerTool(bus, "report-planner")(
            task_id="task-24",
            query_id=message.message_id,
            target_agent="module-2.4-specialist",
            target_session_id="session-24",
            answer="规划范围只包含 2.4，现场事实仍以 E-* 为准。",
            source_ids=[],
        )

    bus.subscribe(UserMessage, planner_reply)
    result = await QueryPeerTool(
        bus, "task-24", "module-2.4-specialist", "session-24"
    )(
        target_agent="report-planner",
        question="是否需要扩展到 2.5？",
        artifact_refs=["Work/coverage.json"],
    )
    assert result["status"] == "replied"
    assert "只包含 2.4" in result["answer"]
```

- [ ] **Step 2: 运行测试并确认新消息/工具缺失**

Run: `uv run pytest tests/test_message_bus.py::test_wait_for_resolves_only_matching_task tests/reporting/test_collaboration_tools.py -q`

Expected: collection FAIL，包含无法导入 `AgentResultMessage`。

- [ ] **Step 3: 实现类型化消息与安全的一次性等待**

```python
# append to autoreport/interfaces/types.py
class PeerQueryMessage(Message):
    type: MessageType = MessageType.PEER_QUERY
    task_id: str
    query_id: str
    source_agent: str
    target_agent: str
    source_session_id: str
    question: str
    artifact_refs: list[str] = Field(default_factory=list)


class PeerReplyMessage(Message):
    type: MessageType = MessageType.PEER_REPLY
    task_id: str
    query_id: str
    source_agent: str
    target_agent: str
    target_session_id: str
    answer: str
    source_ids: list[str] = Field(default_factory=list)


class AgentResultMessage(Message):
    type: MessageType = MessageType.AGENT_RESULT
    task_id: str
    run_id: str
    agent_type: str
    result_path: str
```

同时在 `MessageType` 中加入 `PEER_QUERY = "peer_query"`、`PEER_REPLY = "peer_reply"`、`AGENT_RESULT = "agent_result"`。

```python
# add to autoreport/core/loops/bus.py
    async def wait_for(self, message_cls, predicate, timeout: float):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        async def callback(message):
            if not future.done() and predicate(message):
                future.set_result(message)

        self.subscribe(message_cls, callback)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.unsubscribe(message_cls, callback)
```

- [ ] **Step 4: 实现四个工具，所有跨 Agent 内容只经类型化消息**

```python
# autoreport/core/tools/reporting_collaboration_tools.py
from uuid import uuid4

from ..reporting.agentic_models import AgentResult, AgentRunStatus
from ..reporting.store import ReportingStore
from ...interfaces.types import (
    AgentResultMessage, PeerQueryMessage, PeerReplyMessage, UserMessage,
)
from .registry import Tool


class SubmitResultTool(Tool):
    name = "submit_result"
    description = "提交当前任务的类型化最终成果；成功提交后本轮结束。"

    def __init__(self, agent_id, session_id, run_id, task_id, store, bus):
        self.agent_id = agent_id
        self.session_id = session_id
        self.run_id = run_id
        self.task_id = task_id
        self.store: ReportingStore = store
        self.bus = bus

    async def __call__(self, payload: dict) -> dict:
        result = AgentResult(
            task_id=self.task_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            status=AgentRunStatus.COMPLETED,
            payload=payload,
        )
        path = self.store.write_run_model(
            self.run_id, f"results/{self.task_id}.json", result
        )
        relative = path.relative_to(self.store.workspace).as_posix()
        await self.bus.publish(
            AgentResultMessage(
                task_id=self.task_id,
                run_id=self.run_id,
                agent_type=self.agent_id,
                result_path=relative,
            )
        )
        return {"status": "completed", "result_path": relative}


class ReportBlockedTool(SubmitResultTool):
    name = "report_blocked"
    description = "说明无法可靠完成任务的具体原因和所需补充资料。"

    async def __call__(self, reason: str) -> dict:
        result = AgentResult(
            task_id=self.task_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            status=AgentRunStatus.BLOCKED,
            reason=reason,
        )
        path = self.store.write_run_model(self.run_id, f"results/{self.task_id}.json", result)
        relative = path.relative_to(self.store.workspace).as_posix()
        await self.bus.publish(
            AgentResultMessage(
                task_id=self.task_id, run_id=self.run_id,
                agent_type=self.agent_id, result_path=relative,
            )
        )
        return {"status": "blocked", "result_path": relative}


class QueryPeerTool(Tool):
    name = "query_peer"
    description = "向指定 Agent 提出一个带成果引用的专业问题。"

    def __init__(self, bus, task_id: str, agent_id: str, session_id: str):
        self.bus, self.task_id, self.agent_id = bus, task_id, agent_id
        self.session_id = session_id

    async def __call__(self, target_agent: str, question: str, artifact_refs: list[str]) -> dict:
        query_id = f"q-{uuid4().hex[:12]}"
        waiter = __import__("asyncio").create_task(
            self.bus.wait_for(
                PeerReplyMessage,
                lambda message: (
                    message.query_id == query_id
                    and message.target_agent == self.agent_id
                    and message.target_session_id == self.session_id
                ),
                120.0,
            )
        )
        await __import__("asyncio").sleep(0)
        await self.bus.publish(PeerQueryMessage(
            task_id=self.task_id, query_id=query_id, source_agent=self.agent_id,
            target_agent=target_agent, source_session_id=self.session_id,
            question=question, artifact_refs=artifact_refs,
        ))
        reply = await waiter
        return {
            "query_id": query_id,
            "status": "replied",
            "answer": reply.answer,
            "source_ids": reply.source_ids,
        }


class ReplyPeerTool(Tool):
    name = "reply_peer"
    description = "回复一个已有 query_id，并列出支持回复的来源。"

    def __init__(self, bus, agent_id: str):
        self.bus, self.agent_id = bus, agent_id

    async def __call__(
        self, task_id: str, query_id: str, target_agent: str,
        target_session_id: str, answer: str, source_ids: list[str],
    ) -> dict:
        await self.bus.publish(PeerReplyMessage(
            task_id=task_id, query_id=query_id, source_agent=self.agent_id,
            target_agent=target_agent, target_session_id=target_session_id,
            answer=answer, source_ids=source_ids,
        ))
        return {"query_id": query_id, "status": "replied"}


class PeerMessageRouter:
    def __init__(self, bus):
        self.bus = bus
        self.bus.subscribe(PeerQueryMessage, self._route_query)

    async def _route_query(self, message):
        artifacts = "".join(
            f"<artifact_ref>{ref}</artifact_ref>" for ref in message.artifact_refs
        )
        await self.bus.publish(UserMessage(
            agent_type=message.target_agent,
            source=message.source_agent,
            message_id=message.query_id,
            session_id=f"peer:{message.query_id}:{message.target_agent}",
            summary=message.question,
            content=(
                "<peer_query>"
                f"<task_id>{message.task_id}</task_id>"
                f"<query_id>{message.query_id}</query_id>"
                f"<source_agent>{message.source_agent}</source_agent>"
                f"<source_session_id>{message.source_session_id}</source_session_id>"
                f"<question>{message.question}</question>{artifacts}"
                "</peer_query>"
            ),
        ))
```

- [ ] **Step 5: 运行协作测试**

Run: `uv run pytest tests/test_message_bus.py tests/reporting/test_collaboration_tools.py -q`

Expected: PASS。

- [ ] **Step 6: 提交协作协议**

```bash
git add autoreport/interfaces/types.py autoreport/core/loops/bus.py autoreport/core/tools/reporting_collaboration_tools.py tests/test_message_bus.py tests/reporting/test_collaboration_tools.py
git commit -m "feat: add typed agent workflow results"
```

### Task 7: 让 AgentLoop 运行角色级 Prompt、工具预算与完成门禁

**Files:**
- Modify: `autoreport/core/loops/agent_loop.py`
- Modify: `autoreport/core/loops/manager.py`
- Create: `autoreport/core/reporting/agent_runner.py`
- Test: `tests/test_agent_loop.py`
- Test: `tests/test_manager.py`
- Test: `tests/reporting/test_agent_runner.py`

**Interfaces:**
- Consumes: `AgentDefinition`、`PromptAssembler`、`AgentResultMessage`、按角色构造的 `ToolRegistry`。
- Produces: `AgentLoop(..., agent_definition, system_prompt, required_completion_tools)`；`AgentRunner.run(envelope, session_id=None, timeout=...) -> AgentResult`；`LoopManager.create_registry_loops(definitions, tool_factory)`。

- [ ] **Step 1: 写会话隔离、角色工具和一次提醒失败测试**

```python
# tests/reporting/test_agent_runner.py
import pytest

from autoreport.core.reporting.agentic_models import AgentRunStatus, TaskEnvelope
from autoreport.core.reporting.agent_runner import AgentRunner


@pytest.mark.asyncio
async def test_runner_preserves_session_for_revision(fake_loop_manager, tmp_path):
    runner = AgentRunner(fake_loop_manager, fake_loop_manager.bus, tmp_path)
    first = await runner.run(TaskEnvelope(
        task_id="t-24-r0", run_id="run-1", agent_id="module-2.4-specialist",
        objective="初稿", revision=0,
    ), session_id="session-24")
    second = await runner.run(TaskEnvelope(
        task_id="t-24-r1", run_id="run-1", agent_id="module-2.4-specialist",
        objective="根据审计问题修订", revision=1,
    ), session_id="session-24")
    assert first.session_id == second.session_id == "session-24"


@pytest.mark.asyncio
async def test_runner_marks_incomplete_after_one_submit_reminder(non_submitting_manager, tmp_path):
    result = await AgentRunner(non_submitting_manager, non_submitting_manager.bus, tmp_path).run(
        TaskEnvelope(task_id="t1", run_id="r1", agent_id="auditor", objective="审计")
    )
    assert result.status == AgentRunStatus.INCOMPLETE
    assert non_submitting_manager.loop.turn_count == 2
    assert result.raw_output
```

```python
# append to tests/test_manager.py
def test_registry_loop_receives_only_declared_tools(registry_manager, agent_definitions):
    registry_manager.create_registry_loops(agent_definitions, registry_manager.tool_factory)
    tools = registry_manager.get_loop("evidence-auditor").tools.get_all()
    assert set(tools) == set(agent_definitions["evidence-auditor"].tools)
```

- [ ] **Step 2: 运行测试并确认 Runner/Registry 构造缺失**

Run: `uv run pytest tests/reporting/test_agent_runner.py tests/test_manager.py::test_registry_loop_receives_only_declared_tools -q`

Expected: FAIL，包含 `ModuleNotFoundError: ...agent_runner`。

- [ ] **Step 3: 为 AgentLoop 增加角色级参数，不改变现有 Provider 协议**

```python
# additions to AgentLoop.__init__ in autoreport/core/loops/agent_loop.py
        agent_definition: AgentDefinition | None = None,
        system_prompt: str | None = None,
        required_completion_tools: frozenset[str] = frozenset(),
    ):
        self.agent_definition = agent_definition
        self._explicit_system_prompt = system_prompt
        self._required_completion_tools = required_completion_tools
        self._completion_tool_called = False
        self._max_turns = agent_definition.max_turns if agent_definition else config.max_tool_iterations

# at system prompt construction
if self._explicit_system_prompt is not None:
    return self._explicit_system_prompt

# after successful tool execution
if tool_name in self._required_completion_tools:
    self._completion_tool_called = True

# replace loop iteration budget
if iteration >= self._max_turns:
    break
```

Manager 按定义创建 Loop；ToolFactory 必须对每个名称返回新 Tool 实例，避免把带 task/session 状态的工具跨 Agent 共享：

```python
from ..tools.reporting_collaboration_tools import PeerMessageRouter

# once in LoopManager.__init__
self._peer_message_router = PeerMessageRouter(self.bus)

    def create_registry_loops(self, definitions, tool_factory) -> None:
        for agent_id, definition in definitions.items():
            registry = ToolRegistry()
            for tool_name in definition.tools:
                if tool_name in definition.disallowed_tools:
                    raise ValueError(f"{agent_id} disallows declared tool {tool_name}")
                registry.register(tool_factory(agent_id, tool_name))
            self._loops[agent_id] = AgentLoop(
                agent_type=agent_id,
                workspace=self.workspace,
                tools=registry,
                bus=self.bus,
                config=self.config_manager.config.agents.defaults,
                llm_provider=self.provider,
                agent_definition=definition,
                system_prompt=PromptAssembler.system_prompt(definition),
                required_completion_tools=frozenset({"submit_result", "report_blocked"}),
                loop_manager=self,
                task_board=self.task_board,
            )
```

- [ ] **Step 4: 实现 AgentRunner 的等待、单次提醒与原始输出保全**

```python
# autoreport/core/reporting/agent_runner.py
import asyncio
from pathlib import Path
from uuid import uuid4

from .agentic_models import AgentResult, AgentRunStatus, TaskEnvelope
from .prompts import PromptAssembler
from ...interfaces.types import AgentResponse, AgentResultMessage, UserMessage


class AgentRunner:
    def __init__(self, loop_manager, bus, workspace: Path):
        self.loop_manager = loop_manager
        self.bus = bus
        self.workspace = Path(workspace)

    async def run(
        self,
        envelope: TaskEnvelope,
        session_id: str | None = None,
        timeout: float = 180.0,
    ) -> AgentResult:
        loop = self.loop_manager.get_loop(envelope.agent_id)
        if loop is None:
            raise KeyError(f"agent is not registered: {envelope.agent_id}")
        session_id = session_id or f"s-{uuid4().hex}"
        raw: list[str] = []

        async def capture(message):
            if message.agent_type == envelope.agent_id and not message.streaming:
                raw.append(message.content)

        self.bus.subscribe(AgentResponse, capture)
        try:
            for attempt in range(2):
                result_waiter = asyncio.create_task(
                    self.bus.wait_for(
                        AgentResultMessage,
                        lambda message: message.task_id == envelope.task_id,
                        timeout,
                    )
                )
                final_waiter = asyncio.create_task(
                    self.bus.wait_for(
                        AgentResponse,
                        lambda message: (
                            message.agent_type == envelope.agent_id
                            and message.message_id == envelope.task_id
                            and not message.streaming
                        ),
                        timeout,
                    )
                )
                await asyncio.sleep(0)
                content = PromptAssembler.task_message(envelope, envelope.input_refs)
                if attempt == 1:
                    content += (
                        "\n<completion_reminder>请把已有工作整理后调用 submit_result；"
                        "若确实无法可靠完成，调用 report_blocked。不要继续扩展任务。</completion_reminder>"
                    )
                await self.bus.publish(UserMessage(
                    agent_type=envelope.agent_id,
                    source="workflow",
                    message_id=envelope.task_id,
                    session_id=session_id,
                    summary=envelope.objective,
                    content=content,
                ))
                try:
                    done, pending = await asyncio.wait(
                        {result_waiter, final_waiter},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    await asyncio.sleep(0)
                    if result_waiter.done() and not result_waiter.cancelled():
                        message = result_waiter.result()
                        result_path = self.workspace / message.result_path
                        return AgentResult.model_validate_json(
                            result_path.read_text(encoding="utf-8")
                        )
                    if not done:
                        raise TimeoutError
                except TimeoutError:
                    pass
                finally:
                    for pending_task in (result_waiter, final_waiter):
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(result_waiter, final_waiter, return_exceptions=True)
            return AgentResult(
                task_id=envelope.task_id,
                run_id=envelope.run_id,
                agent_id=envelope.agent_id,
                session_id=session_id,
                status=AgentRunStatus.INCOMPLETE,
                raw_output="\n\n".join(raw),
                reason="agent ended twice without submit_result or report_blocked",
            )
        finally:
            self.bus.unsubscribe(AgentResponse, capture)
```

在 `UserMessage` 和 `AgentLoop` 中同时加入以下确定实现，使 session 选择不是隐式全局状态：

```python
# autoreport/interfaces/types.py, in UserMessage
session_id: str | None = None

# autoreport/core/loops/agent_loop.py, in __init__
self._conversation_history_by_session: dict[str, list[LLMMessage]] = {}

# at the beginning of _process_message
session_id = message.session_id or message.message_id or f"interactive:{self.agent_id}"
self._current_session_id = session_id
self._conversation_history = self._conversation_history_by_session.setdefault(session_id, [])

# after every append/trim assignment to self._conversation_history
self._conversation_history_by_session[session_id] = self._conversation_history
```

在 `tests/test_agent_loop.py` 增加两个明确断言：用 session `s-a`、`s-b` 各处理一轮后两个历史 list 不是同一对象；再对 `s-a` 处理修订后只增加 `s-a` 的历史长度。

- [ ] **Step 5: 运行 Loop/Runner/Manager 测试**

Run: `uv run pytest tests/test_agent_loop.py tests/test_manager.py tests/reporting/test_agent_runner.py -q`

Expected: PASS。

- [ ] **Step 6: 提交独立 Agent Loop 运行能力**

```bash
git add autoreport/core/loops/agent_loop.py autoreport/core/loops/manager.py autoreport/core/reporting/agent_runner.py autoreport/interfaces/types.py tests/test_agent_loop.py tests/test_manager.py tests/reporting/test_agent_runner.py
git commit -m "feat: run isolated role-defined agent loops"
```

### Task 8: 定义三个人类化专家身份与 Phase A 外层流程声明

**Files:**
- Create/Replace: `autoreport/templates/reporting/agents/report-planner.md`
- Create/Replace: `autoreport/templates/reporting/agents/module-2.4-specialist.md`
- Create/Replace: `autoreport/templates/reporting/agents/evidence-auditor.md`
- Modify: `autoreport/templates/reporting/workflows/phase-a.yml`
- Modify: `autoreport/core/reporting/config.py`
- Test: `tests/reporting/test_config.py`
- Test: `tests/reporting/test_prompts.py`

**Interfaces:**
- Consumes: Task 3 的 Agent Registry schema 和 PromptAssembler。
- Produces: `phase-a-agentic` 配置，三种角色的完整身份、独立工具集合和 `maxRevisions: 2`。

- [ ] **Step 1: 写身份完整性、写作自由度和语料隔离失败测试**

```python
# append to tests/reporting/test_config.py
def test_phase_a_registry_contains_three_real_agents():
    agents, workflow = load_packaged_workflow()
    assert set(agents) == {
        "report-planner",
        "module-2.4-specialist",
        "evidence-auditor",
    }
    assert [phase.id for phase in workflow.phases] == ["planning", "module-2.4-pipeline"]
    assert workflow.max_revisions == 2


def test_runtime_identities_do_not_embed_local_corpora_or_rigid_prose():
    agents, _ = load_packaged_workflow()
    joined = "\n".join(agent.instructions for agent in agents.values())
    assert "事实—证据—风险—建议" not in joined
    assert "事实：" not in joined
```

- [ ] **Step 2: 运行测试并确认旧打包 Agent 集合不匹配**

Run: `uv run pytest tests/reporting/test_config.py::test_phase_a_registry_contains_three_real_agents tests/reporting/test_config.py::test_runtime_identities_do_not_embed_local_corpora_or_rigid_prose -q`

Expected: FAIL，当前 Registry 仍包含 manifest/parser/module-worker 等旧身份。

- [ ] **Step 3: 写 Planner 稳定身份**

```markdown
<!-- autoreport/templates/reporting/agents/report-planner.md -->
---
name: report-planner
description: 配电安全咨询项目经理兼技术策划负责人
model: inherit
tools: [search_project_evidence, query_peer, reply_peer, submit_result, report_blocked]
disallowedTools: [search_reference_library, web_search, open_source, exec]
maxTurns: 8
effort: high
memory: task
background: true
reads: [report_request, coverage_matrix, evidence_items]
writes: [module_tasks]
---
<role_and_perspective>
你以资深配电安全咨询项目经理兼技术负责人的视角工作。你先理解客户真正要解决的决策问题，再组织专家工作；目录是边界，不是思考模板。
</role_and_perspective>
<mission>
把本轮请求、证据覆盖和固定 2.4 目录转化为可执行的专家任务。说明每项任务必须回答的问题、可用证据、缺口、交付物和通过标准，但不要替专家预设搜索次数、论证结论或段落句式。
</mission>
<default_posture>
先区分已知现场事实、尚待核实的信息和仅用于解释的方法知识。证据不足以形成可靠 2.4 报告时明确阻塞；证据足够时允许专家直接分析，不强迫研究动作。
</default_posture>
<owned_decisions>
你负责任务范围、依赖、预算和完成标准；不代替模块专家作设备机理判断，不代替审计员批准正文。
</owned_decisions>
<tools_and_loop>
按需要查看项目证据或向责任 Agent 询问。完成后调用 submit_result 提交 plan_submission；无法可靠规划时调用 report_blocked。
</tools_and_loop>
<collaboration>
跨 Agent 只传递任务、问题、来源编号和成果引用，不转发你的完整会话。
</collaboration>
<completion_standard>
任务目标清晰、输入引用真实存在、现场事实边界明确、2.4 完成标准可审计，且未把写作限制成固定四段结构。
</completion_standard>
<deliverables>
PlanSubmission，包含唯一的 2.4 TaskEnvelope 与简洁策划理由。
</deliverables>
```

- [ ] **Step 4: 写 2.4 Specialist 稳定身份**

```markdown
<!-- autoreport/templates/reporting/agents/module-2.4-specialist.md -->
---
name: module-2.4-specialist
description: 配电设备与元件风险诊断专家
model: inherit
tools: [search_project_evidence, search_reference_library, web_search, open_source, query_peer, reply_peer, submit_result, report_blocked]
disallowedTools: [exec]
maxTurns: 12
effort: high
memory: session
background: true
reads: [evidence_items, coverage_matrix, review_issues]
writes: [module_drafts]
---
<role_and_perspective>
你是长期从事配电设备选型、安装验收、运行诊断和故障分析的高级工程师。你关注设备在具体负荷、环境、安装和运维条件下为什么会形成风险，以及哪些信息足以支持判断。
</role_and_perspective>
<mission>
围绕 2.4 的配置与选型、安装规范性、带病运行和末端抽查形成完整专业分析。正文要像专家给客户写的咨询报告：论点有层次，分析有因果和权衡，表达随证据与问题自然变化，不复读机械栏目。
</mission>
<default_posture>
先阅读任务与现场证据。若已有材料足够，可直接分析和写作；若概念、阈值、机理或现行资料需要校准，自主选择本地参考或联网；若另一 Agent 的成果会改变判断，提出明确问题。研究来源只能扩展解释，不能虚构现场设备、位置、数值或状态。
</default_posture>
<owned_decisions>
你负责 2.4 内事实选择、技术解释、风险判断、建议力度、信息缺口和正文结构。你可以提出多个可能机理并说明区分条件，不要把单个检测值直接写成唯一故障原因。
</owned_decisions>
<tools_and_loop>
每次工具结果返回后重新判断：证据是否足够、来源是否适用、是否需要继续、询问同伴或结束。不要为了展示工具能力而搜索。关键现场陈述引用 E-*，专业参考引用 R-* 或 W-*。
需要把证据照片放在相关分析附近时，使用标准 Markdown `![简短说明](photo://photo-id)`；只引用项目证据实际返回的 photo_id，不猜测文件路径。
</tools_and_loop>
<collaboration>
审计返工时保留原会话，逐项处理 blocking issue，同时保持全文连贯；不要只在末尾追加补丁段落。向同伴提问和回复时引用共享成果或来源编号。
</collaboration>
<completion_standard>
正文覆盖任务要求，关键事实/数值/判断可由 ClaimRecord 回溯，参考资料适用性已判断，缺口被诚实表达，建议与风险程度相称，语言自然丰富且没有固定四段复写。
</completion_standard>
<deliverables>
ModuleSubmission：自由 Markdown 正文、隐藏 claims、实际使用的 source_ids、未决问题和 revision。
</deliverables>
```

- [ ] **Step 5: 写独立 Auditor 稳定身份**

```markdown
<!-- autoreport/templates/reporting/agents/evidence-auditor.md -->
---
name: evidence-auditor
description: 配电报告证据与推理独立审计员
model: inherit
tools: [search_project_evidence, search_reference_library, web_search, open_source, query_peer, reply_peer, submit_result, report_blocked]
disallowedTools: [exec]
maxTurns: 10
effort: high
memory: task
background: true
reads: [evidence_items, module_drafts]
writes: [review_issues]
---
<role_and_perspective>
你是独立于作者的配电安全技术审计员。你的目标不是把文章改成某种统一文风，而是判断关键陈述是否真实、可追溯、推理充分且对客户有用。
</role_and_perspective>
<mission>
核验 2.4 正文与 Claim Ledger：现场事实是否有 E-*，专业解释是否误充现场事实，引用是否真的支持陈述，风险是否越过证据，建议是否与问题和不确定性相称。
</mission>
<default_posture>
从成果和来源独立判断，不继承作者聊天历史。只有在现有成果不足以完成审计时才搜索参考或联网。不要因为作者没有使用固定栏目而报错，也不要把个人写作偏好当成 blocking issue。
</default_posture>
<owned_decisions>
你负责批准、警告或阻塞。blocking 只用于会改变事实、结论、风险等级、建议或可追溯性的缺陷；一般表达优化记为 warning。
</owned_decisions>
<tools_and_loop>
核对来源内容与适用范围，必要时向作者提出具体问题。完成后提交 AuditSubmission；每个 issue 必须指出 claim/位置、问题、影响和可验证的修订标准。
</tools_and_loop>
<collaboration>
返工请求只包含类型化 issue 和成果引用，不发送你的完整思考过程。修订后重新独立审计更新稿。
</collaboration>
<completion_standard>
所有关键 Claim 已检查；项目事实边界没有被参考资料突破；批准结论可复核；问题足够具体，使作者能局部返工而不重写无关内容。
</completion_standard>
<deliverables>
AuditSubmission：approved、issues、checked_claim_ids。
</deliverables>
```

- [ ] **Step 6: 写 Phase A workflow 配置并扩展 schema**

```yaml
# autoreport/templates/reporting/workflows/phase-a.yml
id: phase-a-agentic
maxRevisions: 2
phases:
  - id: planning
    mode: pipeline
    agents: [report-planner]
  - id: module-2.4-pipeline
    mode: pipeline
    agents: [module-2.4-specialist, evidence-auditor]
    needs: [planning]
```

```python
# add alias to WorkflowDefinition in config.py
class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str = Field(min_length=1)
    max_revisions: int = Field(default=2, ge=0, le=4, alias="maxRevisions")
    phases: list[PhaseDefinition] = Field(min_length=1)

# narrow packaged registry to identities named by the workflow
def load_packaged_workflow() -> tuple[dict[str, AgentDefinition], WorkflowDefinition]:
    templates = Path(__file__).resolve().parents[2] / "templates" / "reporting"
    all_agents = load_agent_definitions(templates / "agents")
    workflow = load_workflow_definition(templates / "workflows" / "phase-a.yml", all_agents)
    active_ids = {agent_id for phase in workflow.phases for agent_id in phase.agents}
    return {agent_id: all_agents[agent_id] for agent_id in active_ids}, workflow
```

- [ ] **Step 7: 运行身份与 Prompt 测试**

Run: `uv run pytest tests/reporting/test_config.py tests/reporting/test_prompts.py -q`

Expected: PASS。

- [ ] **Step 8: 提交三角色身份**

```bash
git add autoreport/templates/reporting/agents/report-planner.md autoreport/templates/reporting/agents/module-2.4-specialist.md autoreport/templates/reporting/agents/evidence-auditor.md autoreport/templates/reporting/workflows/phase-a.yml autoreport/core/reporting/config.py tests/reporting/test_config.py tests/reporting/test_prompts.py
git commit -m "feat: define phase a expert agent identities"
```

### Task 9: 实现 Phase A 外层 Workflow 与同会话局部返工

**Files:**
- Create: `autoreport/core/reporting/workflow.py`
- Modify: `autoreport/core/reporting/planner.py`
- Modify: `autoreport/core/reporting/store.py`
- Test: `tests/reporting/test_workflow.py`

**Interfaces:**
- Consumes: `AgentRunner.run()`、现有 `build_manifest/evaluate_coverage/plan_modules`、`PlanSubmission/ModuleSubmission/AuditSubmission`。
- Produces: `AgenticWorkflowRunner.run(request) -> AgenticWorkflowResult`；状态 `completed | blocked | incomplete | failed | unsupported_phase`。

- [ ] **Step 1: 写只支持 2.4、独立 Auditor 和同 session 返工失败测试**

```python
# tests/reporting/test_workflow.py
import pytest

from autoreport.core.reporting.agentic_models import AgentResult, AgentRunStatus
from autoreport.core.reporting.models import ReportRequest
from autoreport.core.reporting.workflow import AgenticWorkflowRunner


class RecordingAgentRunner:
    def __init__(self, scripted_results):
        self.results = iter(scripted_results)
        self.calls = []

    async def run(self, envelope, session_id=None, timeout=180.0):
        self.calls.append((envelope.agent_id, envelope.revision, session_id))
        result = next(self.results)
        return result.model_copy(update={"session_id": session_id or result.session_id})


@pytest.mark.asyncio
async def test_phase_a_rejects_full_report_without_legacy_fallback(workflow_factory):
    runner = workflow_factory(agent_runner=RecordingAgentRunner([]))
    result = await runner.run(ReportRequest(instruction="生成完整报告"))
    assert result.status == "unsupported_phase"
    assert "仅支持 2.4" in result.error


@pytest.mark.asyncio
async def test_blocking_audit_returns_to_same_module_session(
    workflow_factory, planner_result, module_r0_result, blocking_audit_result,
    module_r1_result, approved_audit_result,
):
    recording = RecordingAgentRunner([
        planner_result, module_r0_result, blocking_audit_result,
        module_r1_result, approved_audit_result,
    ])
    result = await workflow_factory(agent_runner=recording).run(
        ReportRequest(instruction="生成 2.4", target_modules=["2.4"], missing_evidence_policy="draft")
    )
    module_calls = [call for call in recording.calls if call[0] == "module-2.4-specialist"]
    auditor_calls = [call for call in recording.calls if call[0] == "evidence-auditor"]
    assert result.status == "completed"
    assert module_calls == [
        ("module-2.4-specialist", 0, result.module_session_id),
        ("module-2.4-specialist", 1, result.module_session_id),
    ]
    assert auditor_calls[0][2] != result.module_session_id
    assert auditor_calls[1][2] != result.module_session_id
```

- [ ] **Step 2: 运行测试并确认 Workflow 模块缺失**

Run: `uv run pytest tests/reporting/test_workflow.py -q`

Expected: collection FAIL，包含 `ModuleNotFoundError: ...workflow`。

- [ ] **Step 3: 实现外层流程；专业判断只由 Agent 成果提供**

```python
# autoreport/core/reporting/workflow.py
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .agent_runner import AgentRunner
from .agentic_models import (
    AgentRunStatus, AuditSubmission, ModuleSubmission, PlanSubmission, TaskEnvelope,
)
from .models import ReportRequest
from .store import ReportingStore


class AgenticWorkflowResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    run_id: str
    status: str
    phases: list[str] = Field(default_factory=list)
    output_paths: list[Path] = Field(default_factory=list)
    error: str | None = None
    module_session_id: str | None = None


class AgenticWorkflowRunner:
    def __init__(self, workspace: Path, agent_runner: AgentRunner, preparation, delivery, max_revisions=2):
        self.workspace = Path(workspace)
        self.agent_runner = agent_runner
        self.preparation = preparation
        self.delivery = delivery
        self.max_revisions = max_revisions
        self.store = ReportingStore(workspace)

    async def run(self, request: ReportRequest) -> AgenticWorkflowResult:
        run_id = f"report-{uuid4().hex[:10]}"
        if request.target_modules != ["2.4"]:
            return AgenticWorkflowResult(
                run_id=run_id,
                status="unsupported_phase",
                error="Phase A 仅支持 2.4 纵向样板；未调用旧确定性 Worker。",
            )
        prepared = await self.preparation.prepare(run_id, request)
        planner = await self._run_typed(TaskEnvelope(
            task_id=f"{run_id}-plan", run_id=run_id, agent_id="report-planner",
            objective="为 2.4 专家定义问题、证据范围、缺口与完成标准",
            input_refs=prepared.input_refs, allowed_outputs=["plan_submission"],
        ), PlanSubmission)
        if not isinstance(planner, PlanSubmission):
            return self._terminal(run_id, "incomplete", "Planner 未提交类型化计划")
        task = planner.module_tasks[0].model_copy(update={"run_id": run_id, "revision": 0})
        module_session_id = f"{run_id}-module-2.4"
        submission = await self._run_typed(task, ModuleSubmission, module_session_id)
        if not isinstance(submission, ModuleSubmission):
            return self._terminal(run_id, "incomplete", "2.4 Agent 未提交模块成果", module_session_id)

        for revision in range(self.max_revisions + 1):
            audit = await self._run_typed(TaskEnvelope(
                task_id=f"{run_id}-audit-r{revision}", run_id=run_id,
                agent_id="evidence-auditor", objective="独立审计 2.4 正文与 Claim Ledger",
                input_refs=[f"results/{task.task_id}.json"],
                allowed_outputs=["audit_submission"], revision=revision,
            ), AuditSubmission, f"{run_id}-auditor-r{revision}")
            if not isinstance(audit, AuditSubmission):
                return self._terminal(run_id, "incomplete", "Auditor 未提交审计成果", module_session_id)
            if audit.approved:
                paths = await self.delivery.deliver(run_id, request, prepared, submission, audit)
                return AgenticWorkflowResult(
                    run_id=run_id, status="completed",
                    phases=["preparation", "planning", "module-2.4", "audit", "delivery"],
                    output_paths=paths, module_session_id=module_session_id,
                )
            if revision == self.max_revisions:
                return self._terminal(run_id, "blocked", "2.4 超过 2 轮局部返工", module_session_id)
            issue_path = self.store.write_json(
                f"Work/runs/{run_id}/reviews/issues-r{revision}.json",
                {"issues": audit.issues},
            ).relative_to(self.workspace).as_posix()
            task = task.model_copy(update={
                "task_id": f"{run_id}-module-2.4-r{revision + 1}",
                "revision": revision + 1,
                "prior_result_ref": f"Work/runs/{run_id}/results/{task.task_id}.json",
                "issue_refs": [issue_path],
                "objective": "处理 blocking issues，并保持全文连贯与未受影响内容稳定",
            })
            submission = await self._run_typed(task, ModuleSubmission, module_session_id)
            if not isinstance(submission, ModuleSubmission):
                return self._terminal(run_id, "incomplete", "2.4 修订未提交", module_session_id)

    async def _run_typed(self, envelope, expected_type, session_id=None):
        result = await self.agent_runner.run(envelope, session_id=session_id)
        if result.status != AgentRunStatus.COMPLETED or not isinstance(result.payload, expected_type):
            return result
        return result.payload

    def _terminal(self, run_id, status, error, module_session_id=None):
        return AgenticWorkflowResult(
            run_id=run_id, status=status, error=error, module_session_id=module_session_id
        )
```

`preparation.prepare()` 复用 `ReportingService` 现有 manifest、workbook parse、mapper、evidence、coverage 逻辑，但提取为无 LLM 的 `ReportPreparationService`；它只生产 `ProjectManifest/ParsedArtifact/EvidenceItem/CoverageMatrix`，不得调用 `plan_modules()` 形成专业写作步骤。给每个 `EvidenceItem.id` 统一改为 `E-001` 格式，并对每项执行：

```python
ledger.register_project(
    evidence_id=item.id,
    title=item.subject,
    locator=(
        f"{item.source.path}#"
        f"{item.source.sheet or ''}!{item.source.cell or item.source.page or ''}"
    ),
    content=f"{item.fact} {item.value or ''} {item.unit or ''}".strip(),
)
```

最终返回严格 `PreparationResult(input_refs, evidence_items, coverage, photo_assets)`；`input_refs` 至少包含 `Work/evidence.jsonl`、`Work/coverage.json` 和 `Work/runs/<run_id>/ledgers/sources.json`。

- [ ] **Step 4: 运行 Workflow 单元测试**

Run: `uv run pytest tests/reporting/test_workflow.py -q`

Expected: PASS，包含 full report 明确拒绝和同 session 返工路径。

- [ ] **Step 5: 提交外层 Phase A Workflow**

```bash
git add autoreport/core/reporting/workflow.py autoreport/core/reporting/planner.py autoreport/core/reporting/store.py tests/reporting/test_workflow.py
git commit -m "feat: orchestrate agentic module 2.4 workflow"
```

### Task 10: 构建隐藏 Claim Ledger、自然脚注与证据索引

**Files:**
- Create: `autoreport/core/reporting/claim_ledger.py`
- Test: `tests/reporting/test_claim_ledger.py`

**Interfaces:**
- Consumes: `ModuleSubmission`、`list[SourceRecord]`、`EvidenceItem`。
- Produces: `ClaimLedger.validate() -> list[LedgerIssue]`；`ClaimLedger.build_citation_plan() -> CitationPlan`；`ClaimLedger.annotate_markdown(markdown, plan) -> str`。

- [ ] **Step 1: 写引用真实性、边界和自然脚注失败测试**

```python
# tests/reporting/test_claim_ledger.py
import pytest

from autoreport.core.reporting.agentic_models import ClaimRecord, ModuleSubmission, SourceKind, SourceRecord
from autoreport.core.reporting.claim_ledger import ClaimLedger, ClaimLedgerError


def test_claim_ledger_rejects_unknown_source_id():
    submission = ModuleSubmission(
        module_id="2.4", markdown="现场连接温度为 86℃。", revision=0,
        source_ids=["E-999"], unresolved_questions=[],
        claims=[ClaimRecord(
            id="C-001", module_id="2.4", text="现场连接温度为 86℃",
            claim_type="project_fact", source_ids=["E-999"],
        )],
    )
    with pytest.raises(ClaimLedgerError, match="E-999"):
        ClaimLedger(submission, sources=[]).validate()


def test_citation_plan_adds_markers_only_to_key_claims():
    submission = ModuleSubmission(
        module_id="2.4",
        markdown="连接点实测 86℃，需结合负荷和环境复核。普通过渡句不需要脚注。",
        revision=0, source_ids=["E-001", "R-001"], unresolved_questions=[],
        claims=[
            ClaimRecord(id="C-001", module_id="2.4", text="连接点实测 86℃",
                        claim_type="project_fact", source_ids=["E-001"]),
            ClaimRecord(id="C-002", module_id="2.4", text="需结合负荷和环境复核",
                        claim_type="technical_interpretation", source_ids=["R-001"]),
        ],
    )
    sources = [
        SourceRecord(id="E-001", kind=SourceKind.PROJECT_EVIDENCE,
                     title="红外记录", locator="Inputs/a.xlsx#Sheet1!B2"),
        SourceRecord(id="R-001", kind=SourceKind.LOCAL_REFERENCE,
                     title="温升参考", locator="Knowledge/标准/a.md"),
    ]
    ledger = ClaimLedger(submission, sources)
    plan = ledger.build_citation_plan()
    rendered = ledger.annotate_markdown(submission.markdown, plan)
    assert rendered == "连接点实测 86℃[^1]，需结合负荷和环境复核[^2]。普通过渡句不需要脚注。"
    assert "E-001" in plan.evidence_index_markdown
    assert "R-001" in plan.evidence_index_markdown
```

- [ ] **Step 2: 运行测试并确认 ClaimLedger 缺失**

Run: `uv run pytest tests/reporting/test_claim_ledger.py -q`

Expected: collection FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现精确短语标记与证据索引**

```python
# autoreport/core/reporting/claim_ledger.py
from dataclasses import dataclass

from .agentic_models import CitationEntry, CitationPlan, ModuleSubmission, SourceRecord


class ClaimLedgerError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerIssue:
    claim_id: str
    message: str


class ClaimLedger:
    def __init__(self, submission: ModuleSubmission, sources: list[SourceRecord]):
        self.submission = submission
        self.sources = {source.id: source for source in sources}

    def validate(self) -> list[LedgerIssue]:
        issues = []
        for claim in self.submission.claims:
            unknown = [source_id for source_id in claim.source_ids if source_id not in self.sources]
            if unknown:
                issues.append(LedgerIssue(claim.id, f"unknown sources: {', '.join(unknown)}"))
            if claim.text not in self.submission.markdown:
                issues.append(LedgerIssue(claim.id, "claim text is not present in markdown"))
        if issues:
            raise ClaimLedgerError("; ".join(f"{i.claim_id}: {i.message}" for i in issues))
        return issues

    def build_citation_plan(self) -> CitationPlan:
        self.validate()
        entries = [
            CitationEntry(marker=index, claim_id=claim.id, source_ids=claim.source_ids)
            for index, claim in enumerate(
                (claim for claim in self.submission.claims if claim.footnote_required and claim.source_ids),
                start=1,
            )
        ]
        used_ids = []
        for entry in entries:
            for source_id in entry.source_ids:
                if source_id not in used_ids:
                    used_ids.append(source_id)
        lines = ["## 证据与参考来源索引", ""]
        for source_id in used_ids:
            source = self.sources[source_id]
            lines.append(f"- {source.id}｜{source.title}｜{source.locator}")
        return CitationPlan(entries=entries, evidence_index_markdown="\n".join(lines))

    def annotate_markdown(self, markdown: str, plan: CitationPlan) -> str:
        claims = {claim.id: claim for claim in self.submission.claims}
        rendered = markdown
        offset = 0
        matches = []
        for entry in plan.entries:
            text = claims[entry.claim_id].text
            start = markdown.find(text)
            if start < 0:
                raise ClaimLedgerError(f"{entry.claim_id}: claim text is not present in markdown")
            matches.append((start + len(text), entry.marker))
        for position, marker in sorted(matches):
            insertion = f"[^{marker}]"
            rendered = rendered[: position + offset] + insertion + rendered[position + offset :]
            offset += len(insertion)
        return rendered
```

- [ ] **Step 4: 运行 Claim Ledger 测试**

Run: `uv run pytest tests/reporting/test_claim_ledger.py -q`

Expected: `2 passed`。

- [ ] **Step 5: 提交隐藏审计与引用计划**

```bash
git add autoreport/core/reporting/claim_ledger.py tests/reporting/test_claim_ledger.py
git commit -m "feat: build traceable natural report citations"
```

### Task 11: 沿用交接包确定性 DOCX 核心且禁止改写 Agent 正文

**Files:**
- Create: `autoreport/core/reporting/rendering/pds_docx_renderer.py`
- Create: `autoreport/core/reporting/rendering/handoff_docx.py`
- Modify: `autoreport/core/reporting/rendering/__init__.py`
- Test: `tests/reporting/rendering/test_handoff_docx.py`

**Interfaces:**
- Consumes: `ModuleSubmission`、`CitationPlan`、打包 `report_template.docx`。
- Produces: `HandoffDocxRenderer.render(submission, citation_plan, output_path, annotated_markdown, photo_assets) -> Path`；渲染核心只消费已经写好的 Markdown和显式照片放置，不生成正文。

- [ ] **Step 1: 写原文保留、脚注上标和来源索引失败测试**

```python
# tests/reporting/rendering/test_handoff_docx.py
from docx import Document

from autoreport.core.reporting.agentic_models import CitationEntry, CitationPlan, ModuleSubmission
from autoreport.core.reporting.rendering.handoff_docx import HandoffDocxRenderer


def test_handoff_renderer_preserves_agent_prose_and_adds_source_index(tmp_path):
    sentence = "连接点的异常不能脱离负荷、电流路径与环境条件单独定性。"
    submission = ModuleSubmission(
        module_id="2.4", markdown=f"## 2.4 配电设备与元件风险\n\n{sentence}",
        claims=[], source_ids=["E-001"], unresolved_questions=[], revision=0,
    )
    plan = CitationPlan(
        entries=[CitationEntry(marker=1, claim_id="C-001", source_ids=["E-001"])],
        evidence_index_markdown="## 证据与参考来源索引\n\n- E-001｜红外记录｜Inputs/a.xlsx#S!B2",
    )
    output = tmp_path / "report.docx"
    HandoffDocxRenderer().render(submission, plan, output, annotated_markdown=f"{submission.markdown}[^1]")

    document = Document(output)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert sentence in text
    assert "E-001｜红外记录" in text
    assert "事实：" not in text
    assert any(run.font.superscript for paragraph in document.paragraphs for run in paragraph.runs if "1" in run.text)


def test_renderer_never_calls_structured_model_text_builder(monkeypatch, tmp_path):
    from autoreport.core.reporting.rendering import pds_docx_renderer

    def forbidden(*args, **kwargs):
        raise AssertionError("renderer attempted to generate prose")

    monkeypatch.setattr(pds_docx_renderer, "build_report_text_from_model", forbidden)
    submission = ModuleSubmission(
        module_id="2.4", markdown="## 2.4 配电设备与元件风险\n\n原始专家正文。",
        claims=[], source_ids=[], unresolved_questions=[], revision=0,
    )
    HandoffDocxRenderer().render(
        submission, CitationPlan(entries=[], evidence_index_markdown=""),
        tmp_path / "report.docx", annotated_markdown=submission.markdown,
    )


def test_standard_photo_uri_is_rendered_at_agent_selected_position(tmp_path):
    import base64
    from autoreport.core.reporting.models import PhotoAsset

    photo = tmp_path / "evidence.png"
    photo.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    ))
    submission = ModuleSubmission(
        module_id="2.4",
        markdown="## 2.4 配电设备与元件风险\n\n现场照片：\n\n![连接点](photo://photo-001)",
        claims=[], source_ids=["E-001"], unresolved_questions=[], revision=0,
    )
    asset = PhotoAsset(
        id="photo-001", path=photo, sha256="fixture", media_type="image/png",
        source_member="fixture/evidence.png",
    )
    output = tmp_path / "photo-report.docx"
    HandoffDocxRenderer().render(
        submission, CitationPlan(entries=[], evidence_index_markdown=""), output,
        annotated_markdown=submission.markdown, photo_assets=[asset],
    )
    assert len(Document(output).inline_shapes) == 1
```

- [ ] **Step 2: 运行测试并确认新适配器缺失**

Run: `uv run pytest tests/reporting/rendering/test_handoff_docx.py -q`

Expected: collection FAIL，包含 `ModuleNotFoundError: ...handoff_docx`。

- [ ] **Step 3: 机械复制收资包核心并记录来源**

Run:

```bash
cp /Users/zzymima0000/Documents/Codex/work/配电安全报告工具V2-交接/插件源码目录/core/docx_renderer.py autoreport/core/reporting/rendering/pds_docx_renderer.py
```

在复制文件顶部加入来源和约束，并修正模板路径：

```python
# Vendored from work/配电安全报告工具V2-交接/插件源码目录/core/docx_renderer.py
# Phase A uses only deterministic Markdown-to-DOCX layout. Structured-model prose
# synthesis remains unreachable from HandoffDocxRenderer and is guarded by tests.

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3]
    / "templates"
    / "reporting"
    / "report_template.docx"
)
```

将 `normalize_report_text()` 替换为只清除隐藏思考标签，不按文案关键词截断正文；同时给 `render_report_docx()` 增加默认保持兼容的开关：

```python
def normalize_report_text(report_text: str) -> str:
    return re.sub(
        r"<think>.*?</think>", "", report_text or "", flags=re.DOTALL | re.IGNORECASE
    ).strip()

# add keyword parameter to render_report_docx
allow_model_prose_generation: bool = True,

# replace the original structured-model validation condition
if (
    model is not None
    and allow_model_prose_generation
    and not is_structured_report_model(model)
):
    raise ValueError("report_model_json 不是有效的结构化报告模型")

# replace the original builder condition
if has_report_model and allow_model_prose_generation:
    report_text = build_report_text_from_model(model, report_text)
```

- [ ] **Step 4: 实现只传 Markdown 的适配器和上标后处理**

```python
# autoreport/core/reporting/rendering/handoff_docx.py
import re
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from ..agentic_models import CitationPlan, ModuleSubmission
from ..models import PhotoAsset
from .pds_docx_renderer import render_report_docx


class HandoffDocxRenderer:
    def render(
        self,
        submission: ModuleSubmission,
        citation_plan: CitationPlan,
        output_path: Path,
        *,
        annotated_markdown: str,
        photo_assets: list[PhotoAsset] | None = None,
    ) -> Path:
        report_text = self._expand_photo_uris(annotated_markdown.strip())
        if citation_plan.evidence_index_markdown.strip():
            report_text += "\n\n" + citation_plan.evidence_index_markdown.strip()
        _filename, payload = render_report_docx(
            report_text=report_text,
            filename=Path(output_path).name,
            active_skill_pack_json=None,
            session_policy_patch_json=None,
            effective_report_policy_json=None,
            section_reasoning_result_json=None,
            report_model={
                "photo_manifest": [
                    {"photo_id": asset.id, "local_path": str(asset.path)}
                    for asset in (photo_assets or [])
                ]
            },
            allow_model_prose_generation=False,
        )
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(payload)
        self._format_markers(output_path)
        return output_path

    @staticmethod
    def _expand_photo_uris(markdown: str) -> str:
        pattern = re.compile(r"!\[([^\]]*)\]\(photo://([A-Za-z0-9_-]+)\)")
        return pattern.sub(
            lambda match: (
                f"| {match.group(1) or '现场照片'} |\n"
                "| --- |\n"
                f"| [[PHOTO_REFS:{match.group(2)}]] |"
            ),
            markdown,
        )

    def _format_markers(self, path: Path) -> None:
        document = Document(path)
        pattern = re.compile(r"\[\^(\d+)\]")
        for paragraph in document.paragraphs:
            for run in list(paragraph.runs):
                parts = pattern.split(run.text)
                if len(parts) == 1:
                    continue
                run.text = parts[0]
                anchor = run._r
                for index in range(1, len(parts), 2):
                    anchor = self._insert_run_after(anchor, run._r, parts[index], True)
                    if index + 1 < len(parts):
                        anchor = self._insert_run_after(anchor, run._r, parts[index + 1], False)
        document.save(path)

    @staticmethod
    def _insert_run_after(anchor, style_source, text: str, superscript: bool):
        element = OxmlElement("w:r")
        properties = (
            deepcopy(style_source.rPr)
            if style_source.rPr is not None
            else OxmlElement("w:rPr")
        )
        for existing in list(properties.findall(qn("w:vertAlign"))):
            properties.remove(existing)
        if superscript:
            vert = OxmlElement("w:vertAlign")
            vert.set(qn("w:val"), "superscript")
            properties.append(vert)
        element.append(properties)
        text_element = OxmlElement("w:t")
        if text.startswith(" ") or text.endswith(" "):
            text_element.set(qn("xml:space"), "preserve")
        text_element.text = text
        element.append(text_element)
        anchor.addnext(element)
        return element
```

测试再读取包含 marker 的段落 run 顺序，断言组合文本为 `...条件单独定性。1`，并确认数字 run 的 `font.superscript is True`。

- [ ] **Step 5: 运行渲染单元测试并检查 DOCX 可重新打开**

Run: `uv run pytest tests/reporting/rendering/test_handoff_docx.py -q`

Expected: `3 passed`，`python-docx` 可打开生成文件且匿名测试图片实际嵌入。

- [ ] **Step 6: 提交交接包渲染适配**

```bash
git add autoreport/core/reporting/rendering/pds_docx_renderer.py autoreport/core/reporting/rendering/handoff_docx.py autoreport/core/reporting/rendering/__init__.py tests/reporting/rendering/test_handoff_docx.py
git commit -m "feat: adapt handoff docx renderer without prose synthesis"
```

### Task 12: 切换 ReportingService/Main Tool 到真实 Agentic Phase A

**Files:**
- Modify: `autoreport/core/reporting/service.py`
- Create: `autoreport/core/reporting/preparation.py`
- Create: `autoreport/core/reporting/delivery.py`
- Modify: `autoreport/core/tools/reporting_tool.py`
- Modify: `autoreport/core/loops/manager.py`
- Modify: `autoreport/core/prompts/loader.py`
- Modify: `autoreport/templates/prompts/main.md`
- Test: `tests/reporting/test_service.py`
- Test: `tests/reporting/test_tool.py`
- Test: `tests/test_prompt_loader.py`

**Interfaces:**
- Consumes: `AgenticWorkflowRunner`、`AgentRunner`、研究/协作 ToolFactory、`HandoffDocxRenderer`。
- Produces: `ReportingService(..., loop_manager, web_backend).run(request)`；Main 的 `run_reporting_workflow` 默认为 `["2.4"]` 并清楚说明 Phase A 范围。

- [ ] **Step 1: 写活跃链不导入旧 Worker 与 Main 返回真实状态失败测试**

```python
# replace focused expectations in tests/reporting/test_service.py
import inspect

from autoreport.core.reporting import service


def test_active_reporting_service_does_not_reference_deterministic_workers():
    source = inspect.getsource(service)
    assert "GenericModuleWorker" not in source
    assert "Module24Worker" not in source
    assert "draft_modules_parallel" not in source


@pytest.mark.asyncio
async def test_service_rejects_non_24_without_generating_files(agentic_service):
    result = await agentic_service.run(ReportRequest(instruction="生成全报告"))
    assert result.status == "unsupported_phase"
    assert result.output_paths == []
```

```python
# replace focused expectation in tests/reporting/test_tool.py
@pytest.mark.asyncio
async def test_tool_defaults_to_phase_a_24(tool_with_fake_service):
    payload = await tool_with_fake_service(instruction="分析配电设备风险")
    assert tool_with_fake_service.service.requests[0].target_modules == ["2.4"]
    assert payload["status"] in {"completed", "blocked", "incomplete"}
```

- [ ] **Step 2: 运行测试并确认旧 Worker 仍在活跃链**

Run: `uv run pytest tests/reporting/test_service.py tests/reporting/test_tool.py -q`

Expected: FAIL，`service.py` 源码仍包含 `Module24Worker` 和 `GenericModuleWorker`。

- [ ] **Step 3: 将 Service 缩减为 Agentic Workflow 门面**

```python
# autoreport/core/reporting/service.py
from pathlib import Path

from ..loops.bus import MessageBus
from ..loops.manager import LoopManager
from .agent_runner import AgentRunner
from .delivery import PhaseADeliveryService
from .models import ReportRequest
from .preparation import ReportPreparationService
from .research.web import DisabledWebResearchBackend, WebResearchBackend
from .workflow import AgenticWorkflowResult, AgenticWorkflowRunner


class ReportingService:
    def __init__(
        self,
        workspace: Path,
        *,
        bus: MessageBus,
        loop_manager: LoopManager,
        preparation,
        delivery,
        web_backend: WebResearchBackend | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.web_backend = web_backend or DisabledWebResearchBackend()
        self.runner = AgenticWorkflowRunner(
            self.workspace,
            AgentRunner(loop_manager, bus, self.workspace),
            preparation,
            delivery,
            max_revisions=2,
        )

    async def run(self, request: ReportRequest) -> AgenticWorkflowResult:
        return await self.runner.run(request)
```

`ReportPreparationService` 的 `prepare()` 采用以下固定骨架；把旧 `ReportingService._build_manifest()`、`_parse_artifacts()`、`_normalize_evidence()`、`_evaluate_coverage()` 的函数体机械移动为同名私有方法，不移动 `_plan_modules()` 以及其后的任何写作/审计/交付方法：

```python
# autoreport/core/reporting/preparation.py
from pathlib import Path

from .agentic_models import PreparationResult
from .coverage import evaluate_coverage
from .models import ReportRequest
from .source_ledger import SourceLedger
from .store import ReportingStore


class ReportPreparationService:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)

    async def prepare(self, run_id: str, request: ReportRequest) -> PreparationResult:
        self.store.ensure_layout()
        manifest = await self._build_manifest()
        parsed_artifacts = await self._parse_artifacts(manifest)
        evidence_items, photo_assets = await self._normalize_evidence(
            run_id, manifest, parsed_artifacts
        )
        coverage = evaluate_coverage(request, evidence_items)
        self.store.write_json("Work/coverage.json", coverage.model_dump(mode="json"))
        return PreparationResult(
            input_refs=[
                "Work/evidence.jsonl",
                "Work/coverage.json",
                f"Work/runs/{run_id}/ledgers/sources.json",
            ],
            evidence_items=evidence_items,
            coverage=coverage,
            photo_assets=photo_assets,
        )

    # _build_manifest uses build_manifest(self.workspace) and writes Work/manifest.json.
    # _parse_artifacts is the existing workbook-row parser, now returning list[ParsedArtifact].
    # _normalize_evidence is the existing s2-1/s4-4/s4-6 mapper pipeline, now assigning
    # E-001... IDs, writing Work/evidence.jsonl, and calling SourceLedger.register_project
    # exactly as specified in Task 9 for every EvidenceItem.
```

上述三个私有方法的移动完成后，用 `git diff --no-index` 对照旧方法，允许的差异只有：从 `state` 读写改为参数/返回值、`ev-` 攠为 `E-`、SourceLedger 登记和 imports；mapper、图片行绑定与解析错误处理不可改动。

`PhaseADeliveryService` 使用以下完整交付实现：

```python
# autoreport/core/reporting/delivery.py
from pathlib import Path

from .agentic_models import AuditSubmission, ModuleSubmission, PreparationResult
from .claim_ledger import ClaimLedger
from .models import ReportRequest
from .rendering.handoff_docx import HandoffDocxRenderer
from .source_ledger import SourceLedger
from .store import ReportingStore


class PhaseADeliveryService:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)

    async def deliver(
        self,
        run_id: str,
        request: ReportRequest,
        prepared: PreparationResult,
        submission: ModuleSubmission,
        audit: AuditSubmission,
    ) -> list[Path]:
        sources = SourceLedger(self.workspace, run_id).records
        ledger = ClaimLedger(submission, sources)
        citation_plan = ledger.build_citation_plan()
        annotated = ledger.annotate_markdown(submission.markdown, citation_plan)

        module_path = self.store.write_text("Outputs/Modules/2.4.md", annotated + "\n")
        review_path = self.store.write_json(
            "Outputs/Reviews/phase-a.json", audit.model_dump(mode="json")
        )
        self.store.write_json(
            f"Work/runs/{run_id}/ledgers/claims.json",
            {"claims": [claim.model_dump(mode="json") for claim in submission.claims]},
        )
        self.store.write_json(
            f"Work/runs/{run_id}/citation-plan.json",
            citation_plan.model_dump(mode="json"),
        )
        report_path = self.workspace / "Outputs/Reports/配电安全专家咨询报告.docx"
        HandoffDocxRenderer().render(
            submission,
            citation_plan,
            report_path,
            annotated_markdown=annotated,
            photo_assets=[
                asset.model_copy(update={
                    "path": asset.path if asset.path.is_absolute() else self.workspace / asset.path
                })
                for asset in prepared.photo_assets
            ],
        )
        return [module_path, review_path, report_path]
```

- [ ] **Step 4: 在 Manager 构建 Agentic 角色 Loop 和 task-scoped 工具**

`LoopManager._create_loops()` 先创建现有 Main GUI Loop，再加载 `load_packaged_workflow()`，对三角色调用 `create_registry_loops()`。ToolFactory 使用 `(agent_id, tool_name, active_task_context)` 构造工具；`submit_result/report_blocked` 的 run/task/session 在每次 `AgentRunner.run()` 前通过 `loop.bind_task_context(envelope, session_id)` 重建，不可在 Manager 启动时写死。

Main 工具注册改为：

```python
registry.register(
    RunReportingWorkflowTool(
        workspace=self.workspace,
        bus=self.bus,
        task_board=self._task_board,
        loop_manager=self,
        web_backend=self._create_web_backend_from_environment(),
    )
)
```

联网后端选择只读 `BRAVE_SEARCH_API_KEY`；缺失时使用 `DisabledWebResearchBackend`，不能在 system prompt 中声称网络一定可用。

- [ ] **Step 5: 更新 Main 工具范围和 Main 身份提示**

```python
# reporting_tool.py constructor additions
def __init__(self, workspace, bus, task_board, loop_manager, web_backend=None):
    self.workspace = Path(workspace).resolve()
    self.service = ReportingService(
        self.workspace,
        bus=bus,
        loop_manager=loop_manager,
        preparation=ReportPreparationService(self.workspace),
        delivery=PhaseADeliveryService(self.workspace),
        web_backend=web_backend,
    )

# __call__ default
target_modules=target_modules or ["2.4"]
```

将 Tool description 改为：

```python
description = (
    "Run the real multi-agent Phase A power-distribution workflow for module 2.4. "
    "The workflow may research, audit, revise, and generate a traceable DOCX. "
    "Other report modules are not implemented in Phase A and return unsupported_phase."
)
```

在 Main 身份提示中加入以下完整段落，不列出 `01/02` 内部路径：

```markdown
当用户请求配电设备与元件风险报告时，先确认当前项目资料已就绪，再调用 run_reporting_workflow。你负责解释进度、补资需求、阻塞和交付结果，不代替模块专家给出专业结论。当前正式能力边界是 2.4 纵向样板；若用户请求其他模块或全量报告，明确说明尚未迁移完成，不调用旧写作链生成看似完整的报告。
```

- [ ] **Step 6: 运行 Service、Tool、Prompt 测试**

Run: `uv run pytest tests/reporting/test_service.py tests/reporting/test_tool.py tests/test_prompt_loader.py -q`

Expected: PASS。

- [ ] **Step 7: 扫描活跃链中的旧 Worker 引用**

Run: `rg -n 'GenericModuleWorker|Module24Worker|draft_modules_parallel|事实：|风险：|建议：' autoreport/core/reporting/service.py autoreport/core/reporting/workflow.py autoreport/core/tools/reporting_tool.py autoreport/templates/reporting/agents`

Expected: no output。

- [ ] **Step 8: 提交运行入口切换**

```bash
git add autoreport/core/reporting/service.py autoreport/core/reporting/preparation.py autoreport/core/reporting/delivery.py autoreport/core/reporting/workflow.py autoreport/core/tools/reporting_tool.py autoreport/core/loops/manager.py autoreport/core/prompts/loader.py autoreport/templates/prompts/main.md tests/reporting/test_service.py tests/reporting/test_tool.py tests/test_prompt_loader.py
git commit -m "feat: activate agentic phase a reporting path"
```

### Task 13: 脚本化三 Agent 端到端验收与 GUI 产物可见性

**Files:**
- Create: `tests/reporting/integration/test_agentic_phase_a_24.py`
- Modify: `tests/reporting/integration/test_real_phase_a_24.py`
- Modify: `autoreport/gui/main_window.py`
- Modify: `tests/test_main_window.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: 完整 `RunReportingWorkflowTool`、Scripted Provider、Fake Web Backend、PyQt 项目树刷新。
- Produces: 三条 Agent 路径的可重复离线验收；GUI 显示 `Outputs/Modules`、`Outputs/Reviews`、`Outputs/Reports`；README 明确 Phase A 范围与研究配置。

- [ ] **Step 1: 写脚本化 Provider，证明 Agent 自主选择三种路径**

```python
# tests/reporting/integration/test_agentic_phase_a_24.py
from autoreport.core.providers.base import LLMResponse, LLMStreamChunk, LLMToolCall


PLAN_PAYLOAD = {
    "kind": "plan_submission",
    "module_tasks": [{
        "task_id": "module-24",
        "run_id": "bound-by-workflow",
        "agent_id": "module-2.4-specialist",
        "objective": "依据项目证据完成 2.4 专业分析",
        "input_refs": ["Work/evidence.jsonl", "Work/coverage.json"],
        "constraints": ["现场事实只能引用 E-*"],
        "allowed_outputs": ["module_submission"],
        "revision": 0,
        "issue_refs": [],
    }],
    "rationale": "现有检测与检查记录足以形成 2.4 纵向样板。",
}

MODULE_PAYLOAD_DIRECT = {
    "kind": "module_submission",
    "module_id": "2.4",
    "markdown": "## 2.4 配电设备与元件风险\n\n连接点实测 86℃。该现象需要结合负荷与连接状态判断，不能仅凭温度给出唯一故障原因。",
    "claims": [{
        "id": "C-001", "module_id": "2.4", "text": "连接点实测 86℃",
        "claim_type": "project_fact", "source_ids": ["E-001"],
        "confidence": 1.0, "footnote_required": True, "unresolved": False,
    }],
    "source_ids": ["E-001"],
    "unresolved_questions": ["需确认检测时负荷电流"],
    "revision": 0,
}

MODULE_PAYLOAD_WITH_R001 = {
    **MODULE_PAYLOAD_DIRECT,
    "markdown": "## 2.4 配电设备与元件风险\n\n连接点实测 86℃。温升判断需结合负荷、环境与连接状态。",
    "claims": [
        MODULE_PAYLOAD_DIRECT["claims"][0],
        {
            "id": "C-002", "module_id": "2.4",
            "text": "温升判断需结合负荷、环境与连接状态",
            "claim_type": "technical_interpretation", "source_ids": ["R-001"],
            "confidence": 0.9, "footnote_required": True, "unresolved": False,
        },
    ],
    "source_ids": ["E-001", "R-001"],
}

MODULE_PAYLOAD_R0 = MODULE_PAYLOAD_DIRECT
MODULE_PAYLOAD_WITH_W001 = {
    **MODULE_PAYLOAD_DIRECT,
    "markdown": "## 2.4 配电设备与元件风险\n\n连接点实测 86℃。公开机构资料建议结合负荷与环境复核温升表现。",
    "claims": [
        MODULE_PAYLOAD_DIRECT["claims"][0],
        {
            "id": "C-003", "module_id": "2.4",
            "text": "公开机构资料建议结合负荷与环境复核温升表现",
            "claim_type": "technical_interpretation", "source_ids": ["W-001"],
            "confidence": 0.8, "footnote_required": True, "unresolved": False,
        },
    ],
    "source_ids": ["E-001", "W-001"],
}

MODULE_PAYLOAD_R1 = {
    **MODULE_PAYLOAD_DIRECT,
    "markdown": "## 2.4 配电设备与元件风险\n\n连接点实测 86℃。在缺少同步负荷电流时，该记录提示复核连接状态，但不足以单独确定故障原因。",
    "revision": 1,
}

AUDIT_BLOCKING_PAYLOAD = {
    "kind": "audit_submission",
    "module_id": "2.4",
    "approved": False,
    "issues": [{
        "module_id": "2.4", "claim_id": "C-001", "kind": "overclaim",
        "message": "缺少同步负荷，不能把单一温度写成确定故障。",
        "severity": "blocking", "round": 0,
    }],
    "checked_claim_ids": ["C-001"],
}

AUDIT_APPROVED_PAYLOAD = {
    "kind": "audit_submission",
    "module_id": "2.4",
    "approved": True,
    "issues": [],
    "checked_claim_ids": ["C-001"],
}


class ScriptedProvider:
    def __init__(self, scripts):
        self.model = "scripted-phase-a"
        self.scripts = {agent_id: iter(turns) for agent_id, turns in scripts.items()}
        self.calls = []

    def _next(self, messages, tools):
        system = messages[0].content
        agent_id = next(agent for agent in self.scripts if f'name="{agent}"' in system)
        response = next(self.scripts[agent_id])
        self.calls.append({
            "agent_id": agent_id,
            "messages": list(messages),
            "tool_names": [tool["name"] for tool in tools or []],
            "response": response,
        })
        return response

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        return self._next(messages, tools)

    async def chat_stream(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        response = self._next(messages, tools)
        yield LLMStreamChunk(
            delta=response.content or None,
            tool_calls=response.tool_calls,
            done=True,
            thinking=response.thinking,
        )


def tool_call(name, arguments):
    return LLMResponse(
        content="",
        tool_calls=[LLMToolCall(id=f"call-{name}", name=name, arguments=arguments)],
    )


def final_text(text):
    return LLMResponse(content=text, tool_calls=[])
```

创建三个测试，各自提供完整脚本：

```python
@pytest.mark.asyncio
async def test_direct_write_path_does_not_force_research(agentic_harness):
    harness.script(
        planner=[tool_call("submit_result", PLAN_PAYLOAD)],
        specialist=[tool_call("submit_result", MODULE_PAYLOAD_DIRECT)],
        auditor=[tool_call("submit_result", AUDIT_APPROVED_PAYLOAD)],
    )
    result = await harness.run_24()
    specialist_tools = harness.called_tools("module-2.4-specialist")
    assert "search_reference_library" not in specialist_tools
    assert "web_search" not in specialist_tools
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_local_research_path_registers_r_source(agentic_harness):
    agentic_harness.write_reference("温升.md", "温升判断需结合负荷、环境与连接状态")
    agentic_harness.script(
        planner=[tool_call("submit_result", PLAN_PAYLOAD)],
        specialist=[
            tool_call("search_reference_library", {"query": "温升 负荷 环境"}),
            tool_call("submit_result", MODULE_PAYLOAD_WITH_R001),
        ],
        auditor=[tool_call("submit_result", AUDIT_APPROVED_PAYLOAD)],
    )
    result = await agentic_harness.run_24()
    assert result.status == "completed"
    assert "R-001" in agentic_harness.read_sources()
    assert "温升判断需结合" in agentic_harness.read_module_markdown()


@pytest.mark.asyncio
async def test_audit_revision_reuses_specialist_session(agentic_harness):
    agentic_harness.script(
        planner=[tool_call("submit_result", PLAN_PAYLOAD)],
        specialist=[
            tool_call("submit_result", MODULE_PAYLOAD_R0),
            tool_call("submit_result", MODULE_PAYLOAD_R1),
        ],
        auditor=[
            tool_call("submit_result", AUDIT_BLOCKING_PAYLOAD),
            tool_call("submit_result", AUDIT_APPROVED_PAYLOAD),
        ],
    )
    result = await agentic_harness.run_24()
    sessions = agentic_harness.sessions("module-2.4-specialist")
    assert sessions[0] == sessions[1] == result.module_session_id
    assert agentic_harness.read_final_submission()["payload"]["revision"] == 1


@pytest.mark.asyncio
async def test_web_research_path_opens_search_result_and_registers_w_source(agentic_harness):
    agentic_harness.script(
        planner=[tool_call("submit_result", PLAN_PAYLOAD)],
        specialist=[
            tool_call("web_search", {"query": "electrical connection temperature", "limit": 3}),
            tool_call("open_source", {"url": "https://example.org/guide"}),
            tool_call("submit_result", MODULE_PAYLOAD_WITH_W001),
        ],
        auditor=[tool_call("submit_result", AUDIT_APPROVED_PAYLOAD)],
    )
    result = await agentic_harness.run_24()
    assert result.status == "completed"
    assert "W-001" in agentic_harness.read_sources()


@pytest.mark.asyncio
async def test_tool_failure_returns_to_same_loop_and_agent_can_finish(agentic_harness):
    agentic_harness.script(
        planner=[tool_call("submit_result", PLAN_PAYLOAD)],
        specialist=[
            tool_call("open_source", {"url": "https://not-searched.example/"}),
            tool_call("submit_result", MODULE_PAYLOAD_DIRECT),
        ],
        auditor=[tool_call("submit_result", AUDIT_APPROVED_PAYLOAD)],
    )
    result = await agentic_harness.run_24()
    assert result.status == "completed"
    followup_messages = agentic_harness.provider_calls("module-2.4-specialist")[1]["messages"]
    assert any("URL was not returned by web_search" in message.content for message in followup_messages)
```

fixture 生成的首项现场证据必须稳定为 `E-001`；上述本地研究版本的 technical_interpretation 使用工具真实返回的 `R-001`。测试文本为匿名合成内容，不从 `02` 复制。

- [ ] **Step 2: 运行端到端测试并确认缺失 fixture/接线失败**

Run: `uv run pytest tests/reporting/integration/test_agentic_phase_a_24.py -q`

Expected: FAIL，首次失败发生在 Agent Loop/ToolFactory/Delivery 尚未完整接线的具体断言，而不是访问网络。

- [ ] **Step 3: 完成 harness 接线与 GUI 目录刷新**

`agentic_harness` 建立最小项目：`Inputs/` 中写入现有匿名 s4-4/s4-6 fixture，`Knowledge/任意目录` 可选写入测试参考；创建 `MessageBus`、`TaskBoard`、`LoopManager`、ScriptedProvider、FakeWebBackend，启动 loops 后调用真实 `RunReportingWorkflowTool`，最终在 fixture teardown 中停止 loops。

GUI 项目树不新增报告专用模型，只确保现有刷新入口在收到 completed `SystemNotice` 后调用：

```python
if message.type == MessageType.SYSTEM_NOTICE and "流程已完成" in message.content:
    self.refresh_project_tree()
```

并用 `tests/test_main_window.py` 断言以下相对路径可见：

```python
assert window.project_tree_contains("Outputs/Modules/2.4.md")
assert window.project_tree_contains("Outputs/Reviews/phase-a.json")
assert window.project_tree_contains("Outputs/Reports/配电安全专家咨询报告.docx")
```

- [ ] **Step 4: 把旧 real_phase_a 测试改成准备层回归，不再断言确定性正文**

旧 `tests/reporting/integration/test_real_phase_a_24.py` 保留真实工作簿 mapper、E 编号、照片绑定和 coverage 断言；删除对 `Module24Worker` 固定句式、固定 claim kind 和旧 `DocxRenderer` 的断言。新端到端文件承担写作、审计和 DOCX 验收。

- [ ] **Step 5: 更新 README 的真实能力边界**

在 README 的报告部分写明：

```markdown
### 配电报告 Agentic Phase A

当前正式支持 2.4“配电设备与元件风险”纵向样板。Report Planner、2.4 Specialist 和 Evidence Auditor 分别运行独立模型—工具会话；专家可按需搜索项目本地参考或在配置 Brave Search API key 后联网，关键项目事实仍只能来自上传资料。系统生成模块 Markdown、隐藏 Claim/Source Ledger、审计记录和 DOCX。

Phase A 尚不把 2.1、2.2、2.3、2.5、跨模块审查和总编标记为可用；请求这些范围会返回 `unsupported_phase`，不会回退到旧确定性写作链。
```

- [ ] **Step 6: 运行端到端、准备层和 GUI 测试**

Run: `uv run pytest tests/reporting/integration/test_agentic_phase_a_24.py tests/reporting/integration/test_real_phase_a_24.py tests/test_main_window.py -q`

Expected: PASS；不需要 API key，不访问网络。

- [ ] **Step 7: 提交端到端纵向样板**

```bash
git add tests/reporting/integration/test_agentic_phase_a_24.py tests/reporting/integration/test_real_phase_a_24.py autoreport/gui/main_window.py tests/test_main_window.py README.md
git commit -m "test: verify agentic module 2.4 vertical slice"
```

### Task 14: 全量回归、静态边界审计与人工 DOCX 版式验收

**Files:**
- Modify only if verification reveals a defect: files already listed in Tasks 1–13.
- Test: complete `tests/` suite.

**Interfaces:**
- Consumes: Phase A 完整实现。
- Produces: 可重复验证记录；无旧 Worker 活跃引用、无 `02` 运行时输入、无未追踪生成文件。

- [ ] **Step 1: 运行 Ruff**

Run: `uv run ruff check autoreport tests`

Expected: `All checks passed!`。

- [ ] **Step 2: 运行完整测试套件**

Run: `uv run pytest -q`

Expected: PASS；被 `integration` marker 标记且需要真实 LLM 的测试可以显式 skip，但 Task 13 的脚本化集成测试必须运行。

- [ ] **Step 3: 审计知识和身份边界**

Run: `rg -n 'Knowledge/' autoreport/templates autoreport/core/reporting autoreport/core/tools`

Expected:

- `Knowledge/` 只作为统一项目参考根目录和 `R-*` 定位前缀出现；
- 不存在基于任意子目录名、编号或文件名的允许/拒绝分支。

- [ ] **Step 4: 审计活跃运行链和来源前缀**

Run: `rg -n 'GenericModuleWorker|Module24Worker|draft_modules_parallel' autoreport/core/reporting/service.py autoreport/core/reporting/workflow.py autoreport/core/tools/reporting_tool.py`

Expected: no output。

Run: `uv run pytest tests/reporting/test_agentic_models.py tests/reporting/test_claim_ledger.py tests/reporting/research -q`

Expected: PASS，证明 `E/R/W` 边界与 URL 白名单。

- [ ] **Step 5: 生成一份匿名 fixture DOCX 并渲染为页面图像检查**

Run: `uv run pytest tests/reporting/integration/test_agentic_phase_a_24.py::test_audit_revision_reuses_specialist_session -q --basetemp=/tmp/autoreport-phase-a-qa`

Expected: PASS，并在测试日志中打印生成 DOCX 的绝对路径。

使用文档技能的渲染脚本或 LibreOffice headless 将该 DOCX 转为 PDF/PNG；检查标题层级、表格宽度、中文字体、分页、脚注上标、证据索引，以及 Agent 原句未被渲染器替换。发现版式缺陷时只修改 `handoff_docx.py` 或 `pds_docx_renderer.py` 的确定性排版逻辑，重新执行 Task 11 与本步骤测试。

- [ ] **Step 6: 检查工作树只包含预期变更**

Run: `git status --short`

Expected: 无 `/tmp`、生成 DOCX、缓存、API 响应或真实客户资料进入仓库；实现提交后工作树 clean。

- [ ] **Step 7: 提交最终验证修正（仅在 Step 1–6 产生修正时执行）**

```bash
git add autoreport tests README.md
git commit -m "fix: close phase a verification gaps"
```

---

## Implementation Checkpoints

- Task 1–3 后：评审类型、动态 Agent ID 和 Prompt 分层；若 system prompt 仍包含动态项目上下文，停止继续实现。
- Task 4–7 后：评审研究来源边界、URL 白名单、类型化提交和 session 隔离；若 Agent 不能自主跳过搜索或工具状态跨 session 泄漏，停止继续实现。
- Task 8–10 后：用三个身份样例检查写作自由度、审计独立性和 Claim Ledger；若正文结构仍由 Python 固定拼接，停止继续实现。
- Task 11–13 后：运行脚本化三路径 E2E 并人工查看 DOCX；只有原文保留、来源可追溯、返工同 session 成立时进入全量回归。

## Definition of Done

- Main Agent 的活跃工具实际启动三个独立 Agent Loop，而不是 Python Worker。
- 2.4 Specialist 可以不搜索、搜索本地参考或搜索网络；选择由模型在自身 Loop 中作出。
- Planner、Specialist、Auditor 的 system prompt 不含项目资料、`01` 内容或 `02` 内容。
- `02` 没有任何运行时可达读取路径；`01` 只在 Tool 调用后进入对应 Agent session，并登记 `R-*`。
- `E-*`、`R-*`、`W-*` 和 Claim Ledger 可在 `Work/runs/<run_id>/ledgers` 中检查。
- blocking audit issue 最多触发 2 次局部返工，使用原 Specialist session；Auditor 每轮为独立 session。
- 模块正文自然、多样，不出现程序强制重复的“事实/风险/建议”块；关键声明有自然上标，文末有证据与参考来源索引。
- DOCX 使用交接包渲染核心，并有自动测试证明 `build_report_text_from_model()` 不可从新适配器触发。
- Phase A 非 2.4 请求返回 `unsupported_phase`，旧确定性 Worker 不在活跃链。
- Ruff、完整 pytest、脚本化 E2E、GUI 目录可见性和人工 DOCX 版式检查均通过。

## 后续独立计划（不在本次实现范围）

1. Phase B：新增 2.1、2.2、2.3、2.5 四个模块专家，外层按五条独立 pipeline 并行运行与分别返工。
2. Phase C：新增 Cross-module Reviewer、Chief Editor、全局 barrier、跨模块冲突返工与完整第 1/3 章。
3. 评测扩展：把 `02` 仅在离线开发环境中匿名抽象为更丰富的正反例与质量断言，仍保持生产运行时不可达。
