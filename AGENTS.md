# AGENTS.md — Manyselves Codex 实施约定

本文件适用于整个仓库。所有 Codex 任务必须先读取本文件及其引用的规范文档。

## 1. 必读顺序

1. [`docs/PROJECT_POSITIONING.md`](docs/PROJECT_POSITIONING.md)
2. [`docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md`](docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)
3. [`docs/research/DECLARATIVE_RUNTIME_LANDSCAPE.md`](docs/research/DECLARATIVE_RUNTIME_LANDSCAPE.md)
4. 当前工作包涉及的源码和测试

旧文档可以说明历史行为，但不能覆盖以上三份文档中的目标定位和迁移边界。

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

## 3. 不可违反的不变量

### 3.1 Kernel 业务无关

Kernel 中不得出现或依赖：

- reporting 包；
- 模块 `2.1`～`2.5`；
- Editor、Auditor、Cross、Chief 等领域角色；
- 能效、调度、实验等具体领域字段；
- FastAPI、React 或 PyQt 界面逻辑。

若一个拟议的“通用抽象”当前只能用配电报告说明，先用中立场景验证，否则留在 Capability。

### 3.2 不删除现有恢复能力

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

### 3.3 不把业务流程抽成 Kernel Action

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

### 3.4 定义必须先编译再执行

Agent、Tool、Contract、Task、Gate、Recovery 和 Workflow 引用必须由确定性 Compiler 校验。模型不得通过自然语言直接修改 WorkflowState 或宣布某个未校验流程可执行。

### 3.5 状态与日志分工

只持久化调度所需的权威 State 和一份 ResolvedPlan。完整执行轨迹进入 Event Log。不要为相同事实新增多套 Manifest。

Hash 只用于内容变化、缓存、旧结果绑定和最终交付一致性，不建立无处不在的 Hash 链。

### 3.6 兼容优先

在新执行路径完成行为等价验证前：

- 默认保留 Legacy Reporting Runner；
- 不删除旧 Run 的读取和恢复能力；
- 不改变公开输出合同；
- 不移动全部 reporting 代码；
- 通过 Feature Flag 或 Adapter 切换新旧路径。

## 4. Codex 执行循环

每个任务按以下顺序执行。

### 4.1 确认工作包

从主方案的 `WP-*` 中选择一个工作包或一个更小的垂直切片。

开始前明确：

```text
Goal
Current behavior
Files to inspect
Allowed change scope
Invariants
Tests
Research triggers
Done condition
```

### 4.2 仓库勘察

先搜索当前实现和测试，确认：

- 真实入口；
- 调用关系；
- 状态所有者；
- 输入输出合同；
- 恢复路径；
- 已有测试覆盖。

不要根据文件名或旧文档猜行为。

### 4.3 Characterization First

当修改现有行为边界时，先增加 Characterization Test 或语义 Trace，再重构。

特别适用于：

- `agent_loop.py`；
- `reporting/agent_runner.py`；
- `reporting/workflow.py`；
- `reporting/review_lifecycle.py`；
- `reporting/parallel_runtime.py`；
- Tool Result、Conversation 和 Recovery。

### 4.4 小型垂直切片

优先完成一条端到端切片：

```text
Definition
→ Loader
→ Compiler
→ Executor
→ State
→ Test
```

不要一次只创建大量抽象类，也不要一次跨 Kernel、全报告流程、API 和前端。

### 4.5 验证

至少运行：

```bash
uv run ruff check <changed-python-paths> <changed-test-paths>
uv run pytest <focused-tests> -q
```

工作包完成前再运行受影响的完整测试集合。涉及前端时先读取 `frontend/package.json`，运行其中真实存在的 lint、typecheck、test 或 build 脚本，不猜命令。

若环境缺少依赖，记录真实阻塞；不要声称测试通过。

## 5. 实施中调研规则

出现以下情况必须暂停编码并调研：

1. 外部框架 API 或行为不确定；
2. 当前仓库行为无法由代码和测试确认；
3. 新抽象只有 reporting 一个使用者；
4. 新依赖会改变状态、执行或部署边界；
5. Conversation、Recovery、Tool Result、Parallel 或 Checkpoint 语义有歧义；
6. 计划采用 Microsoft Agent Framework、LangGraph、Burr、Temporal、Restate 或其他运行时；
7. 旧文档、代码和测试互相冲突。

### 5.1 来源

技术调研按以下优先级：

1. 官方 GitHub 源码和测试；
2. 官方文档；
3. 官方设计文档 / ADR；
4. 最小可运行 POC。

### 5.2 记录

有实际架构决定时写入：

```text
docs/research/decisions/<NNN>-<topic>.md
```

使用主方案中的研究模板。没有改变实现选择的普通查阅不需要新增文档。

### 5.3 POC 隔离

外部框架 POC 放在：

```text
research/<framework>-spike/
```

POC 不得直接替换当前报告生产路径。

## 6. Definition 与 Compiler 约束

### 6.1 第一版定义范围

只实现当前工作包需要的：

- CapabilityDefinition；
- AgentDefinition；
- ToolDefinition；
- ContractDefinition；
- TaskDefinition；
- WorkflowDefinition；
- GateDefinition；
- RecoveryPolicyDefinition。

不要提前建设无限 DSL。

### 6.2 Contract

优先适配现有 Pydantic Model，再增加 JSON Schema。所有边界统一暴露：

```python
validate(value)
json_schema()
is_assignable_to(other)
```

不得用 `dict[str, Any]` 逃避已知合同。

### 6.3 Conversation

Agent 身份连续性由：

```text
AgentDefinition + conversationKey
```

决定。

同一 Key 是否复用、何时重置，来自 Workflow Definition 和 State，不来自角色名称。

### 6.4 Compiler 必查

- ID 和引用；
- Action Kind；
- 变量 Def-Use；
- Contract 兼容；
- Conversation Key 与 Agent 绑定；
- Goto / Loop / Parallel / Join；
- 最大迭代或退出条件；
- 最终输出和完成节点。

## 7. Reporting 迁移规则

### 7.1 迁移顺序

严格按主方案：

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
```

### 7.2 Cross 不是内核概念

配电报告的 Cross 回写、Local Regression、原 Editor 和原 Auditor 会话复用必须由 Reporting Workflow、State、Schema 和 Adapter 组合实现。

新场景可以完全不存在这些概念。

### 7.3 行为等价

使用 Scripted / Fake Provider 比较标准语义事件，不比较随机 ID、时间戳或真实模型文本。

在迁移完成前，现有 `tests/reporting/` 是必须保留的验收基线。

## 8. 新依赖政策

引入新的运行时依赖前必须：

- 完成对应研究决定；
- 提供最小 POC；
- 说明为何现有依赖不能满足；
- 比较对 AgentLoop、Tool、Artifact、Recovery 和部署的影响；
- 提供回退路径。

当前没有预先决定采用 Microsoft Agent Framework 或 LangGraph。

## 9. PR 与提交要求

每个 PR 只处理一个工作包或一个垂直切片。

说明必须包含：

- 做了什么；
- 为什么；
- 当前行为基线；
- 保持了哪些不变量；
- 新增或修改了哪些接口；
- 测试；
- 研究决定；
- 回退方式；
- 尚未完成的后续工作。

禁止把计划、POC 和生产迁移混在一个提交中。

## 10. 停止条件

出现以下情况时停止当前实现并报告：

- 无法证明新路径与当前行为兼容；
- 需要删除恢复能力才能继续；
- Compiler 无法确定输入输出兼容；
- 新抽象明显泄漏领域词汇；
- 测试显示旧 Run 或当前报告输出不兼容；
- 外部依赖选择尚未完成研究 Gate；
- 任务范围已经超出当前工作包。

停止不等于放弃。应保存已验证事实、失败 POC 和下一步决策所需信息。

## 11. 第一项代码任务

默认从主方案 `WP-00` 开始：

1. 定义标准语义事件；
2. 为当前报告最小模块 Lane 建立 Trace Adapter；
3. 使用现有 Scripted / Fake Runner 生成稳定 Snapshot；
4. 建立 Kernel Import Boundary 测试骨架；
5. 不改变任何生产调度。

除非用户明确指定其他工作包，不得跳过基线直接大规模重构。