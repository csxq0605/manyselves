# AGENTS.md — Manyselves Codex 实施约定

本文件适用于整个仓库。所有 Codex 任务必须先读取本文件及其引用的规范文档。

## 1. 必读顺序与优先级

1. [`docs/PROJECT_POSITIONING.md`](docs/PROJECT_POSITIONING.md)
2. [`docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md`](docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)
3. [`docs/CODEX_AUTONOMOUS_EXECUTION.md`](docs/CODEX_AUTONOMOUS_EXECUTION.md)
4. [`docs/research/DECLARATIVE_RUNTIME_LANDSCAPE.md`](docs/research/DECLARATIVE_RUNTIME_LANDSCAPE.md)
5. 若存在，读取 [`docs/implementation/RUNTIME_EXTRACTION_STATUS.md`](docs/implementation/RUNTIME_EXTRACTION_STATUS.md)
6. 当前阶段涉及的源码和测试

这些文件的职责不同：

- `PROJECT_POSITIONING.md` 决定产品与架构定位；
- `AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md` 决定工作包内容、依赖和迁移顺序；
- `CODEX_AUTONOMOUS_EXECUTION.md` 决定 Codex 的执行节奏、人工验收门、长期分支和恢复方式；
- `DECLARATIVE_RUNTIME_LANDSCAPE.md` 保存外部调研基线；
- `RUNTIME_EXTRACTION_STATUS.md` 保存当前实施位置。

若旧文档与以上规范冲突，以上述顺序为准。特别是：主实施方案中的工作包是设计、测试、提交和回滚边界，**不是要求用户逐个重新派发的任务边界**。

## 2. 项目目标

Manyselves 正在从配电报告垂直多 Agent 系统迁移为：

> 定义驱动、可恢复、业务无关的多 Agent 应用运行时。

目标执行链：

```text
Definitions
→ Loader / Registry
→ Workflow Compiler
→ Resolved Plan
→ Generic Action / Executor Runtime
→ Existing AgentLoop / Tools / Artifacts / Events
```

配电安全报告是第一个复杂参考 Capability，不是 Kernel 固定身份或固定流程。

## 3. 自主连续实施模式

运行时抽取项目默认采用：

> **一次总任务、按工作包依赖顺序自主连续执行、关键人工验收门暂停。**

Codex 接到总任务后必须：

1. 从 `WP-00` 开始；
2. 完成当前工作包或更小垂直切片；
3. 运行真实存在的自动测试；
4. 创建聚焦提交并更新实施状态；
5. 若未到人工验收门且没有真实阻塞，自动进入下一工作包；
6. 到达 `HG-*` 人工验收门时，先推送、给出精确人工测试步骤并停止；
7. 收到 `APPROVE_GATE HG-XX` 后，从原分支和原状态继续，不重复已完成工作。

不得因为“一个工作包结束”就要求用户重新下达下一工作包。

长期实施使用一个实施分支和一个 Draft PR。每个工作包或垂直切片仍应独立提交，以便验证和回滚；除非用户另有要求，不为每个工作包重新开 PR。

完整规则和人工门见 `docs/CODEX_AUTONOMOUS_EXECUTION.md`。

## 4. 不可违反的不变量

### 4.1 Kernel 业务无关

Kernel 中不得出现或依赖：

- reporting 包；
- 模块 `2.1`～`2.5`；
- Editor、Auditor、Cross、Chief 等领域角色；
- 能效、调度、实验等具体领域字段；
- FastAPI、React 或 PyQt 界面逻辑。

若一个拟议的“通用抽象”当前只能用配电报告说明，先用中立场景验证，否则留在 Capability。

### 4.2 不删除现有恢复能力

必须保留并逐步通用化：

- 结构化提交纠正；
- Schema 校验失败后的原 Conversation 修正；
- Max Token Continuation；
- Tool Slice Continuation；
- No-progress 检测；
- 已完成 Tool Result 复用；
- 原 Conversation / Session 复用；
- Same-run 已完成结果恢复。

不得以“简化架构”为理由先删除后补。

### 4.3 不把业务流程抽成 Kernel Action

Kernel Action 必须是通用执行动作，例如：

```text
SetVariable
If
ConditionGroup
ForEach
Parallel
Join
Goto
CreateConversation
ResolveConversation
InvokeAgent
InvokeTool
ValidateContract
EvaluateGate
RequestInput
PublishResult
EndWorkflow
```

禁止新增 `CrossReviewAction`、`ModuleAuditAction` 等业务动作。

### 4.4 定义必须先编译再执行

Agent、Tool、Contract、Task、Gate、Recovery 和 Workflow 引用必须由确定性 Compiler 校验。模型不得通过自然语言直接修改 WorkflowState 或宣布某个未校验流程可执行。

### 4.5 状态与日志分工

运行时只持久化调度所需的权威 State 和一份 ResolvedPlan。完整执行轨迹进入 Event Log。不要为相同事实新增多套 Manifest。

实施项目仅维护一份 `RUNTIME_EXTRACTION_STATUS.md`，用于跨 Codex 会话接续；它不是运行时状态设计。

Hash 只用于内容变化、缓存、旧结果绑定和最终交付一致性，不建立无处不在的 Hash 链。

### 4.6 兼容优先

在新执行路径完成行为等价验证前：

- 默认保留 Legacy Reporting Runner；
- 不删除旧 Run 的读取和恢复能力；
- 不改变公开输出合同；
- 不移动全部 reporting 代码；
- 通过 Feature Flag 或 Adapter 切换新旧路径；
- 未经人工验收门批准，不切换默认路径。

## 5. 每个工作包内部的执行循环

工作包是连续实施中的局部边界。开始一个工作包前，Codex应自行明确：

```text
Goal
Current behavior
Files to inspect
Allowed change scope
Invariants
Tests
Research triggers
Done condition
Human gate, if any
```

### 5.1 仓库勘察

先搜索当前实现和测试，确认：

- 真实入口；
- 调用关系；
- 状态所有者；
- 输入输出合同；
- 恢复路径；
- 已有测试覆盖。

不要根据文件名或旧文档猜行为。

### 5.2 Characterization First

当修改现有行为边界时，先增加 Characterization Test 或语义 Trace，再重构。

特别适用于：

- `agent_loop.py`；
- `reporting/agent_runner.py`；
- `reporting/workflow.py`；
- `reporting/review_lifecycle.py`；
- `reporting/parallel_runtime.py`；
- Tool Result、Conversation 和 Recovery。

### 5.3 小型垂直切片

优先完成一条端到端切片：

```text
Definition
→ Loader
→ Compiler
→ Executor
→ State
→ Test
```

不要一次只创建大量抽象类，也不要一次跨 Kernel、全报告流程、API 和前端形成一个不可验证的大改动。

### 5.4 验证

至少运行：

```bash
uv run ruff check <changed-python-paths> <changed-test-paths>
uv run pytest <focused-tests> -q
```

工作包完成前再运行受影响的完整测试集合。涉及前端时先读取 `frontend/package.json`，运行其中真实存在的 lint、typecheck、test 或 build 脚本，不猜命令。

若环境缺少依赖，记录真实阻塞；不要声称测试通过。

### 5.5 自动进入下一工作包

当前工作包完成、测试通过、未到人工门且不存在真实阻塞时：

- 提交当前变更；
- 更新 `RUNTIME_EXTRACTION_STATUS.md`；
- 自动开始依赖已满足的下一工作包；
- 不询问“是否继续”。

## 6. 实施中调研规则

出现以下情况必须暂停当前编码并先调研：

1. 外部框架 API 或行为不确定；
2. 当前仓库行为无法由代码和测试确认；
3. 新抽象只有 reporting 一个使用者；
4. 新依赖会改变状态、执行或部署边界；
5. Conversation、Recovery、Tool Result、Parallel 或 Checkpoint 语义有歧义；
6. 计划采用 Microsoft Agent Framework、LangGraph、Burr、Temporal、Restate 或其他运行时；
7. 旧文档、代码和测试互相冲突。

“暂停编码并调研”不等于等待用户。Codex应先自行使用以下来源完成调研：

1. 当前仓库源码和测试；
2. 官方 GitHub 源码和测试；
3. 官方文档；
4. 官方设计文档 / ADR；
5. 最小可运行 POC。

如果调研能够确定不改变既定公共边界的实现方案，应记录必要结论后继续执行。

只有当结论涉及新的生产依赖、长期公共接口、现有兼容语义或关键架构不变量时，才进入 `HG-01` 架构人工门。

### 6.1 研究记录

有实际架构决定时写入：

```text
docs/research/decisions/<NNN>-<topic>.md
```

没有改变实现选择的普通查阅不需要新增文档。

### 6.2 POC 隔离

外部框架 POC 放在：

```text
research/<framework>-spike/
```

POC 不得直接替换当前报告生产路径。

## 7. Definition 与 Compiler 约束

### 7.1 第一版定义范围

按主方案和当前工作包逐步实现：

- CapabilityDefinition；
- AgentDefinition；
- ToolDefinition；
- ContractDefinition；
- TaskDefinition；
- WorkflowDefinition；
- GateDefinition；
- RecoveryPolicyDefinition。

不要提前建设无限 DSL。

### 7.2 Contract

优先适配现有 Pydantic Model，再增加 JSON Schema。所有边界统一暴露：

```python
validate(value)
json_schema()
is_assignable_to(other)
```

不得用 `dict[str, Any]` 逃避已知合同。

### 7.3 Conversation

Agent 身份连续性由：

```text
AgentDefinition + conversationKey
```

决定。

同一 Key 是否复用、何时重置，来自 Workflow Definition 和 State，不来自角色名称。

### 7.4 Compiler 必查

- ID 和引用；
- Action Kind；
- 变量 Def-Use；
- Contract 兼容；
- Conversation Key 与 Agent 绑定；
- Goto / Loop / Parallel / Join；
- 最大迭代或退出条件；
- 最终输出和完成节点。

## 8. Reporting 迁移规则

### 8.1 自动迁移顺序

严格按主方案依赖顺序自动推进：

```text
基线 Trace
→ Definitions
→ Minimal Runtime
→ Tool Adapter
→ Conversation / Agent Adapter
→ Recovery
→ Control Flow
→ One Module Lane
→ Module Cohort
→ Cross / Chief / Final / Delivery
→ Capability Package
→ Generic Server / Frontend
→ Second Neutral Capability
```

人工验收门位于这些阶段之间，但工作包不需要用户逐项派发。

### 8.2 Cross 不是内核概念

配电报告的 Cross 回写、Local Regression、原 Editor 和原 Auditor会话复用必须由 Reporting Workflow、State、Schema 和 Adapter 组合实现。

新场景可以完全不存在这些概念。

### 8.3 行为等价

使用 Scripted / Fake Provider 比较标准语义事件，不比较随机 ID、时间戳或真实模型文本。

在迁移完成前，现有 `tests/reporting/` 是必须保留的验收基线。

## 9. 新依赖政策

引入新的运行时依赖前必须：

- 完成对应研究决定；
- 提供最小 POC；
- 说明为何现有依赖不能满足；
- 比较对 AgentLoop、Tool、Artifact、Recovery 和部署的影响；
- 提供回退路径；
- 进入 `HG-01` 并取得人工批准。

当前没有预先决定采用 Microsoft Agent Framework 或 LangGraph。

## 10. 分支、提交和 PR 要求

运行时抽取项目默认使用：

```text
base: agent/declarative-runtime-plan
head: agent/declarative-runtime-implementation
one long-lived Draft PR
```

要求：

- 每个工作包或垂直切片独立提交；
- 提交信息包含 `WP-XX`；
- 到人工门前推送所有提交；
- Draft PR 描述持续更新当前阶段、测试和人工门；
- 不把 POC 与生产接入混在同一提交；
- 未经批准不合并 PR；
- 未经 `HG-05` 批准不切换新路径为默认。

如果任务不属于这次长期运行时抽取项目，仍可按普通单任务 PR 处理。

## 11. 人工验收门与停止条件

人工验收门、测试要求和恢复口令以 `docs/CODEX_AUTONOMOUS_EXECUTION.md` 为准。

到达人工门时必须先：

- 完成当前自动测试；
- 推送提交；
- 更新实施状态；
- 给出精确环境、命令、UI 步骤、通过标准、失败标准和回滚方式；
- 然后停止等待人工结果。

人工结果使用：

```text
APPROVE_GATE HG-XX
REJECT_GATE HG-XX: <reason>
SUPPLEMENT_GATE HG-XX: <facts or artifacts>
```

除人工门外，仅在以下情况停止：

- 无法证明新路径与当前行为兼容；
- 自动测试失败且无法在当前工作包修复；
- 需要删除恢复能力才能继续；
- Compiler 无法确定输入输出兼容；
- 新抽象明显泄漏领域词汇；
- 测试显示旧 Run 或当前报告输出不兼容；
- 必要外部研究无法完成；
- 需要人工批准破坏性操作、生产依赖或默认路径切换；
- 主方案存在无法自行消解的矛盾。

停止前必须保存可复现测试或 POC、提交已验证部分并更新唯一实施状态。

## 12. 启动和接续

首次执行从 `WP-00` 开始，并持续自动执行到 `HG-00`。

新的 Codex 会话接续时，先执行：

```text
读取 AGENTS.md；
读取 docs/implementation/RUNTIME_EXTRACTION_STATUS.md；
确认当前分支和最新提交；
从 Current human gate 或 Next automatic action 继续；
不得重复已完成的工作包。
```

除非用户明确改变总方案，不得跳过基线直接大规模重构，也不得在每个工作包结束后询问是否继续。
