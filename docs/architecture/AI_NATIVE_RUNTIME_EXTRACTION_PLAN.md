# Manyselves AI-Native 运行时抽取与迁移执行方案

> 文档性质：面向 Codex 的主实施方案
>
> 目标：从当前配电报告深度耦合实现中抽取业务无关的定义、编译和执行能力，在保持现有功能、接口、恢复和输出一致的前提下，使新场景能够通过定义和扩展进行组装
>
> 规范定位：[`docs/PROJECT_POSITIONING.md`](../PROJECT_POSITIONING.md)
>
> 编写日期：2026-08-19

## 1. 方案结论

Manyselves 不应通过“把当前 Python 流程翻译成 YAML”完成通用化，也不应把配电报告中的 Editor、Auditor、Cross、Chief 等协作模式提升为内核协议。

正确的迁移方式是建立一条可验证的编译链：

```text
Agent / Tool / Contract / Task / Workflow / Gate / Recovery Definitions
                              │
                              ▼
                 Definition Loader & Registry
                              │
                              ▼
                      Workflow Compiler
                              │
                              ▼
                       Resolved Plan
                              │
                              ▼
               Generic Action / Executor Runtime
                              │
                 State + Event → New State
                              │
                              ▼
          Existing AgentLoop / Tools / Artifacts / Events
```

配电报告继续作为完整参考能力运行，但其流程逐步迁移为外部定义；新场景可以定义完全不同的 Agent、Tool、Schema、状态和控制流，而无需复制新的领域 Runner。

迁移必须采用 **兼容适配、双路径运行、行为等价验证、逐段替换**，不得先删除当前报告实现再重建。

## 2. 设计依据与参考实现

本方案重点参考以下项目，但不预先承诺直接依赖其中任意一个：

### 2.1 Microsoft Agent Framework Declarative Workflow

参考：

- <https://github.com/microsoft/agent-framework>
- `python/packages/declarative/agent_framework_declarative/`
- `declarative-agents/workflow-samples/`

值得借鉴的结构：

```text
YAML / JSON Definition
→ AgentFactory / WorkflowFactory
→ Declarative Action Models
→ DeclarativeWorkflowBuilder
→ Action Executor Graph
→ Workflow Runtime
```

其声明式工作流将每个 YAML Action 编译为真实 Executor 节点，支持顺序、条件、`Foreach`、`GotoAction`、Agent、Tool、外部输入和 Checkpoint。它是当前最接近 Manyselves 目标的实现参考。

### 2.2 LangGraph

参考：<https://github.com/langchain-ai/langgraph>

值得借鉴：

- 图状态和 Reducer；
- 条件边、循环和子图；
- Checkpointer、Interrupt 和 Durable Execution；
- 长时间 Agent 工作流的运行语义。

局限：LangGraph 主要提供代码式图运行时，Manyselves 仍需自行实现 Agent、Tool、Schema、Workflow 定义加载和编译。

### 2.3 Apache Burr

参考：<https://github.com/apache/burr>

值得借鉴：

- Action 显式声明读取和写入的状态；
- 应用作为状态机；
- 执行函数接收 State 并返回新 State；
- 状态保存、加载、回放和遥测。

它说明“内核无状态、业务状态外置”的核心可以保持很小。

### 2.4 Temporal 与 Restate

参考：

- <https://github.com/temporalio/sdk-python>
- <https://github.com/restatedev/restate>

它们解决耐久执行、可靠通信和跨进程恢复，不解决 Agent、Prompt、Tool、Schema 和声明式组装。当前阶段只作为未来多 Worker 或更强耐久执行的候选后端，不应阻塞本次抽取。

## 3. 当前仓库的实际耦合位置

以下是迁移必须面对的真实边界，不允许通过新建空目录假装完成分层。

### 3.1 `manyselves/core/loops/agent_loop.py`

当前同时承担：

- 通用模型循环；
- Provider 重试、流式响应和工具调用；
- 对话历史和上下文处理；
- Usage Ledger；
- 报告请求识别、报告恢复和报告终态回答；
- MiMo 报告成本展示等业务相关逻辑。

目标：保留模型和工具执行核心，将报告识别、报告终态文案和能力选择移出内核。

### 3.2 `manyselves/core/reporting/agent_runner.py`

当前集中实现：

- 报告 Agent 身份和 Session 复用；
- Agent Prompt 和 Skill 注入；
- TaskEnvelope 与输入合同；
- 当前任务 Tool 裁剪；
- 结构化提交 Schema；
- 普通文字结束后的提交纠正；
- Max Token 和 Tool Slice Continuation；
- No-progress 判断；
- Provider Context 和 Usage 记录；
- 任务结果恢复。

目标：先为这些能力建立通用接口，再逐项抽取；在通用执行器达到行为等价前，保留 Legacy Adapter。

### 3.3 `manyselves/core/reporting/workflow.py`

当前集中实现：

- 报告 Operation；
- 准备、模块、Cross、Chief、Final 和 Delivery 的控制流；
- 并行 Lane、Barrier、恢复和 Checkpoint；
- 成本阶段边界；
- 报告状态与最终交付。

目标：逐段转为声明式 Workflow Action，不把报告节点名称变成内核 Action Kind。

### 3.4 `manyselves/core/reporting/review_lifecycle.py`

当前集中实现报告领域的 Finding、Revision、Verdict、Local Regression 和 Cross 回写。

目标：第一阶段全部保留在配电报告 Capability；只抽取通用 Action、Conversation 复用和恢复语义。只有出现第二个真实场景共享同一逻辑后，才考虑抽取更高层子工作流模板。

### 3.5 `manyselves/core/reporting/parallel_runtime.py`

当前包含 Task Attempt、Identity Lease、Lane、Barrier 和 Recovery Store 等可复用原语。

目标：通过依赖分析和中立测试区分：

- 真正业务无关、应迁入 Runtime 的执行原语；
- 仅服务报告业务、应留在 Capability 的类型。

### 3.6 `manyselves/core/tools/registry.py`

当前主要从 Python 签名推导 Tool 输入 Schema，输出合同和错误合同不统一。

目标：增加显式 ToolDefinition 和输出合同，同时保留现有 Tool 类作为实现适配器。

### 3.7 FastAPI、React 与部署

当前 `manyselves/application/`、`manyselves/webapi/` 和 `frontend/` 已经提供账户、项目、会话、事件、文件、报告和部署封装。

目标：先保持现有报告 API 正常，再增加通用 Capability、Workflow、Run、Interaction、Output 和 Cost 投影；不先重写服务器。

## 4. 目标模块边界

目标结构是语义边界，不要求第一轮立即完成物理移动。

```text
manyselves/
├── kernel/                         # 不导入 reporting、webapi、gui 或任何 Capability
│   ├── definitions/                # 定义模型、Loader、Registry
│   ├── contracts/                  # Pydantic / JSON Schema 合同注册和兼容检查
│   ├── workflow/                   # Action、表达式、Compiler、ResolvedPlan、State
│   ├── executors/                  # 通用 Action Executor 接口和基础 Executor
│   ├── recovery/                   # 通用恢复事件、策略和控制器
│   └── ports/                      # State、Conversation、Tool、Artifact、Event 端口
│
├── runtime/                        # 将现有 Manyselves 实现接入 Kernel
│   ├── agent_adapter.py            # AgentLoop / Provider 适配
│   ├── conversation_adapter.py     # Session / Conversation Key
│   ├── tool_adapter.py             # 现有 ToolRegistry 适配
│   ├── artifact_adapter.py         # ArtifactGateway 适配
│   ├── state_store.py              # 文件状态持久化
│   ├── event_adapter.py            # MessageBus / SSE 映射
│   └── legacy_reporting_adapter.py # 迁移期间兼容当前报告 Runner
│
├── capabilities/
│   └── distribution_reporting/     # 当前配电报告参考能力
│       ├── capability.yaml
│       ├── agents/
│       ├── tasks/
│       ├── contracts/
│       ├── workflows/
│       ├── gates/
│       ├── recovery/
│       ├── prompts/
│       ├── skills/
│       ├── tools/
│       └── adapters/               # 报告专属确定性转换和校验
│
├── application/                    # 应用服务与生命周期
├── webapi/                         # FastAPI
└── ...
```

### 4.1 强制依赖方向

```text
kernel ← runtime ← application/webapi/gui
kernel ← capability definitions
runtime ← capability executable adapters
```

禁止：

- `kernel` 导入 `manyselves.core.reporting`；
- `kernel` 出现模块 `2.1`、Cross、Chief、SOC、实验参数等领域字段；
- `AgentLoop` 根据领域关键词决定具体业务流程；
- Capability 直接修改 Kernel State，必须通过 Action Result；
- Workflow Definition 引用未注册的 Agent、Tool、Contract 或 Action Kind。

## 5. 定义模型

第一版只实现完成配电报告迁移和第二个中立示例所需的定义，不建设无限扩展 DSL。

### 5.1 `CapabilityDefinition`

负责索引一组定义：

```yaml
id: distribution-reporting
version: 1.0.0
agents: ./agents
workflows: ./workflows
tasks: ./tasks
contracts: ./contracts
tools: ./tools
gates: ./gates
recovery: ./recovery
```

它只是定义入口，不是运行状态或重复日志。

### 5.2 `AgentDefinition`

至少包含：

- ID、版本、说明；
- Instructions 或 Markdown Prompt；
- Model/Profile 选择；
- 可用 Tool；
- 接受和产生的 Contract；
- Skill / Knowledge Selector；
- Conversation 默认模式；
- 资源和输出限制。

现有 reporting AgentDefinition 通过 Adapter 接入，不先重写全部 Prompt。

### 5.3 `ToolDefinition`

至少包含：

- ID、版本；
- Python/HTTP/MCP/内置实现引用；
- Input Contract；
- Output Contract；
- 错误合同；
- `side_effect`、`parallel_safe`、`reuse_result`；
- 模型是否可见；
- 使用说明。

### 5.4 `ContractDefinition`

支持两种实现：

- `pydantic`：引用当前 Pydantic Model；
- `json_schema`：加载 JSON Schema。

统一暴露：

```python
validate(value) -> validated_value
json_schema() -> dict
is_assignable_to(other) -> bool
```

### 5.5 `TaskDefinition`

用于复用 Agent 调用要求，至少包含：

- Agent 引用；
- Objective 模板；
- Input / Output Contract；
- Constraints；
- Tool Policy；
- Recovery Policy；
- 完成条件。

### 5.6 `WorkflowDefinition`

工作流由通用 Action 和状态变量组成，不包含固定领域协议。

### 5.7 `GateDefinition`

Gate 是结构化校验和确定性条件的组合：

- Contract 校验；
- 表达式；
- Python Validator Tool；
- Pass、Fail、Wait 或跳转结果。

### 5.8 `RecoveryPolicyDefinition`

用于通用 Agent/Tool 执行恢复：

- `invalid_structured_output`；
- `natural_language_without_submission`；
- `max_tokens`；
- `tool_slice_boundary`；
- `no_progress`；
- `tool_contract_error`；
- `provider_error`。

具体纠正文字可以由 Capability 覆盖，但恢复动作必须是通用动作。

## 6. 第一版通用 Action 集合

### 6.1 状态

- `SetVariable`
- `AppendVariable`
- `MergeVariable`

### 6.2 控制流

- `If`
- `ConditionGroup`
- `ForEach`
- `Parallel`
- `Join`
- `Goto`
- `Subworkflow`
- `EndWorkflow`

### 6.3 Agent 与 Conversation

- `CreateConversation`
- `ResolveConversation`
- `InvokeAgent`
- `ResetConversation`

Conversation 是否复用由 Workflow 的 `conversationKey` 或 `conversationId` 决定，内核不规定哪些角色必须保持身份。

### 6.4 Tool 与校验

- `InvokeTool`
- `ValidateContract`
- `EvaluateGate`

### 6.5 交互与结果

- `RequestInput`
- `WaitInput`
- `PublishResult`
- `FailWorkflow`

任何报告专属操作都必须表达为这些 Action 的组合、Subworkflow 或报告 Tool，不能增加 `CrossReviewAction` 等内核类型。

## 7. Compiler 与 Resolved Plan

### 7.1 编译阶段

Compiler 必须在模型调用前完成：

1. Definition 引用解析；
2. Action Kind 注册检查；
3. Action ID 唯一性；
4. Agent、Tool、Task、Contract、Gate 和 Subworkflow 引用检查；
5. 表达式和变量引用解析；
6. 基础 Def-Use 数据流检查；
7. 上游输出与下游输入合同兼容检查；
8. Conversation Key 与 Agent 绑定检查；
9. `Goto`、条件、循环、Parallel 和 Join 控制流检查；
10. 循环最大次数或退出条件检查；
11. 最终输出和完成节点检查。

### 7.2 `ResolvedPlan`

每个 Run 只保存一份已解析计划：

```text
Work/runs/<run-id>/resolved-plan.json
```

它用于固定本次运行实际使用的定义和 Executor 连接，不是重复的调用日志。

ResolvedPlan 至少包含：

- Workflow ID 和版本；
- 已解析 Action；
- Agent、Tool、Contract 和 Recovery 引用；
- 控制流边；
- Conversation 绑定表达式；
- 最终输出合同。

### 7.3 `WorkflowState`

只保存调度需要的权威状态：

```text
run_id
workflow_id
status
variables
conversations
actions
outputs
waiting_input
```

完整过程由 Event Log 记录，不为同一事实生成多套 Manifest。

## 8. Executor 接口

```python
class ActionExecutor(Protocol):
    kind: str

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        ...
```

`ActionResult` 只能表达：

- 输出值；
- 状态 Patch；
- 事件；
- 跳转或后续控制信号；
- 完成、等待或失败状态。

Executor 不得直接修改其他 Action 的状态。

## 9. `InvokeAgentExecutor` 的通用恢复要求

该 Executor 是迁移的关键。第一版必须复用当前成熟逻辑，而不是另写一个简化 Agent Loop。

### 9.1 Conversation 绑定

```text
AgentDefinition + conversationKey → Conversation Record / AgentLoop Session
```

支持：

- `ephemeral`：每次新会话；
- `run`：同一 Run 内按 Key 复用；
- `persistent`：由外部 Store 管理。

内核不理解 Key 中的业务含义。

### 9.2 输出完成

完成必须满足：

- 当前 Action 的输出 Contract 已知；
- Agent 返回结构化结果；
- 结果通过 Contract 校验；
- 当前 Task / Action 状态绑定正确；
- Action Result 已持久化。

### 9.3 恢复事件

- 自然语言结束但要求结构化输出：原 Conversation 纠正；
- Schema 错误：原 Conversation 只修正校验错误；
- Max Token：原 Conversation 继续；
- Tool Slice：原 Conversation 继续；
- Tool Result 已完成：直接复用；
- No Progress：按 Recovery Policy 停止或跳转；
- Provider 错误：按错误类别执行有限重试或失败。

### 9.4 迁移策略

先实现：

```text
Generic InvokeAgentExecutor
        │
        ▼
Legacy ReportingAgentAdapter
        │
        ▼
Current ReportingAgentRunner
```

在行为测试通过后，逐项把 Session、Tool Binding、Contract、Correction 和 Continuation 抽入通用 Runtime。

## 10. 行为等价迁移方法

### 10.1 不直接比较 LLM 文本

真实模型输出不稳定。迁移使用脚本化 Provider / Fake Agent 运行相同输入。

### 10.2 标准语义事件

旧实现和新实现都映射为：

```text
workflow.started
action.started
conversation.created
agent.invoked
tool.invoked
contract.validated
branch.selected
action.completed
action.failed
workflow.completed
output.published
```

比较字段：

- Logical Action / Task ID；
- Agent Definition ID；
- Conversation Key；
- Input / Output Contract；
- Tool ID；
- 分支和循环顺序；
- 恢复动作；
- 最终输出状态。

不比较：

- 时间戳；
- 随机 UUID；
- 临时路径；
- Provider Token 数；
- 非业务 Log 文本。

### 10.3 配电报告等价门槛

必须确认：

- 五模块范围不变；
- 模块 Agent 和 Auditor 会话复用关系不变；
- Cross Finding 路由回对应模块的行为不变；
- Local Regression 和原 Reviewer 复核行为不变；
- Barrier 和 Gate 顺序不变；
- 结构化输入输出合同不变；
- 恢复后不重新执行已经完成的结果；
- 最终 Markdown、DOCX、输出路径和交付校验保持兼容；
- 现有 Reporting 测试全部通过。

## 11. Codex 工作包

工作包按四阶段连续执行，中间不设置人工验收断点：

```text
Stage 1: WP-00..WP-01 — 基线与 Definition 层
Stage 2: WP-02..WP-06 — 无状态 Kernel 与通用 Runtime
Stage 3: WP-07..WP-10 — Reporting 全流程迁移与 Capability 包
Stage 4: WP-11..WP-12 — 通用 API/UI 投影与第二 Capability
```

每个工作包仍是 Characterization、实现、focused/affected tests、提交和回滚边界。四阶段和全部工作包完成前不调用真实 Provider、不要求真实项目/浏览器/服务器人工验证；完成后统一生成一次真实测试交接。Legacy Reporting Runner 在最终真实测试前继续可用并保持默认，声明式路径必须完整实现且可显式选择。

每个工作包必须独立完成、可验证、可回滚。不得跨越多个抽象边界一次提交。

### WP-00：冻结定位与基线

目标：建立实现前基线。

任务：

- 阅读本文件与 `docs/PROJECT_POSITIONING.md`；
- 列出当前报告入口、Agent 调用、Tool、Contract、恢复和输出边界；
- 建立 Kernel 禁止依赖清单；
- 选择最小可重复 Fake Provider 流程；
- 为现有配电报告生成标准语义 Trace Adapter。

完成条件：

- 不改变生产行为；
- 所有现有测试通过；
- Trace Snapshot 可重复。

### WP-01：Definition Models、Loader 与 Registry

新增：

```text
manyselves/kernel/definitions/
manyselves/kernel/contracts/
```

实现：

- Capability、Agent、Tool、Task、Contract、Gate、Recovery 和 Workflow 定义模型；
- YAML、JSON 和 Markdown Frontmatter Loader；
- Registry；
- Pydantic Contract Adapter；
- 引用错误和重复 ID 错误。

限制：

- 不接入现有 Runtime；
- 不移动报告代码；
- 不设计报告字段。

测试：

- 有效定义加载；
- 无效 YAML；
- 重复 ID；
- 不存在引用；
- Pydantic 与 JSON Schema 合同校验。

### WP-02：Workflow State、Action Models 与最小 Compiler

实现：

- WorkflowState；
- ResolvedAction / ResolvedPlan；
- Executor Registry；
- `SetVariable`、`InvokeTool`、`ValidateContract`、`EndWorkflow`；
- 最小顺序 Compiler；
- File State Store。

验证场景：一个不使用 Agent 的中立工作流。

完成条件：定义可编译、执行、恢复状态并生成最终结构化输出。

### WP-03：Tool Adapter

实现：

- ToolDefinition → 当前 Tool 实例；
- 显式 Input / Output Contract；
- 统一 Tool Outcome；
- RunToolResultIndex 适配为通用 Tool Result Cache；
- `side_effect`、`parallel_safe` 和 `reuse_result`。

完成条件：现有文件 Tool 和一个确定性 Tool 可通过声明式 `InvokeTool` 调用。

### WP-04：Conversation Registry 与 Legacy Agent Adapter

实现：

- Conversation Key；
- Conversation Record；
- `CreateConversation`、`ResolveConversation`；
- `InvokeAgentExecutor` 接口；
- Legacy ReportingAgentAdapter；
- Agent Definition 和 Task Definition 绑定。

验证：

- 两个 Agent Definition；
- 独立 Conversation；
- 相同 Key 再调用使用原 Conversation；
- 不同 Key 不共享历史。

### WP-05：通用 Agent Recovery Controller

从当前 ReportingAgentRunner 提取或适配：

- 结构化提交纠正；
- Schema 错误纠正；
- Max Token Continuation；
- Tool Slice Continuation；
- No-progress 检测；
- 已完成 Tool Result 复用。

要求：

- 先写 Characterization Tests；
- 保持当前报告行为；
- Capability 可以提供纠正 Prompt 模板；
- Runtime 只认识通用恢复事件和动作。

### WP-06：声明式控制流

研究 Gate：完成 `R-01` 和 `R-02` 后再实现。

实现：

- `If` / `ConditionGroup`；
- `Goto`；
- `ForEach`；
- `Parallel` / `Join`；
- `Subworkflow`；
- 最大迭代和退出校验；
- Def-Use 检查。

验证：

- 中立循环场景；
- 中立并行 Join 场景；
- 不存在任何 reporting import。

### WP-07：迁移一个完整模块 Lane

只迁移一个模块的：

```text
Author → Auditor → 条件返修 → 原 Author → 原 Auditor → 完成
```

注意：这些只是配电报告 Workflow Definition 中的 Agent 和 Action，不是 Kernel Action Kind。

要求：

- 使用当前 Pydantic 合同；
- 使用当前 Agent Prompt 和 Tool；
- 使用 Conversation Key 保持原身份；
- 与旧流程使用相同脚本化输出；
- 标准语义 Trace 等价。

### WP-08：迁移模块 Cohort 与 Barrier

实现五个模块实例的并行/受限并发组装和 Join，不改变现有业务门禁。

验证：

- Lane 独立；
- 完成模块复用；
- Barrier 只在全部通过后释放；
- 失败 Lane 不影响已完成 Lane。

### WP-09：迁移 Cross、Chief、Final 与 Delivery

Cross 必须由配电报告 Workflow、状态字段和 Adapter 表达，Kernel 不新增 Cross 概念。

迁移顺序：

1. Cross Initial；
2. Finding 路由；
3. 对应模块原 Conversation 回改；
4. Local Regression；
5. Cross Recheck；
6. Cross Barrier；
7. Chief；
8. Final；
9. Render / Delivery。

完成条件：配电报告全流程语义 Trace、合同、输出和现有测试等价。

### WP-10：Capability 包与导入边界

目标：使配电报告成为独立参考能力。

任务：

- 建立 `capabilities/distribution_reporting/`；
- 迁移 Agent、Task、Workflow、Gate、Recovery 和 Contract 索引；
- 报告 Python Validator / Transform 保留为 Capability Adapter；
- 添加 Kernel import boundary 测试；
- 保留兼容导入路径，分阶段弃用旧位置。

### WP-11：通用 FastAPI 与 React 投影

新增通用接口：

```text
GET  /capabilities
GET  /workflows
GET  /workflows/{id}/input-schema
POST /runs
GET  /runs/{id}
POST /runs/{id}/input
GET  /runs/{id}/outputs
GET  /runs/{id}/cost
```

迁移策略：

- 保留现有 Reporting API 作为兼容 Adapter；
- React 先读取通用 Run Projection；
- Capability 可以提供专属结果 View；
- 不把工作流编排器暴露为业务用户必需界面。

### WP-12：第二能力证明

新增一个中立、最小 Capability，故意不包含 Editor、Auditor、Cross 或报告结构。

建议测试流程：

```text
输入参数
→ 固定 Tool 运行
→ ValidateContract
→ 条件判断
→ 参数调整 Agent（可选）
→ Goto Tool
→ 输出
```

目的：证明 Kernel 只依赖通用 Action、Conversation、Tool 和 Contract。

## 12. 实施中即时调研机制

Codex 不应在不确定处凭印象继续实现。出现以下情况必须暂停编码并执行调研：

1. 外部框架 API 或能力可能已变化；
2. 当前仓库行为无法从代码和测试确定；
3. 拟新增的“通用抽象”目前只被配电报告使用；
4. 两个方案会显著改变依赖、状态模型或迁移成本；
5. 恢复、Conversation、Tool Result 或控制流语义存在歧义；
6. 计划引入 LangGraph、Microsoft Agent Framework、Temporal、Restate 或其他新依赖；
7. 现有测试与文档互相冲突。

### 12.1 调研来源优先级

技术问题只使用：

1. 官方 GitHub 仓库源码和测试；
2. 官方文档；
3. 官方设计文档或 ADR；
4. 必要时运行最小 POC。

不以博客二手总结作为架构决定依据。

### 12.2 调研记录位置

```text
docs/research/decisions/<NNN>-<topic>.md
```

模板：

```markdown
# 问题

## 为什么阻塞当前工作包

## 当前仓库事实

## 外部来源

## 最小 POC

## 观察结果

## 决定

## 被拒绝的方案

## 对当前工作包的影响

## 验证方式
```

### 12.3 研究不能替代交付

调研必须对应一个明确决策或 POC。若调研无法改变当前工作包的实现选择，则不新增研究文档。

## 13. 必做研究任务

### R-01：Microsoft Agent Framework Declarative POC

问题：Manyselves 是否可以直接使用或适配其 Declarative Workflow，而不失去当前 AgentLoop、Tool、Artifact 和恢复能力？

POC：

- 自定义 Agent Adapter；
- 自定义 Tool Executor；
- 两个 Conversation；
- 条件和 Goto；
- Checkpoint；
- 同 Conversation 重试；
- 输出 Schema。

决策结果只能是：

- `adopt`：直接采用核心包；
- `adapt`：采用 Workflow Runtime，自己保留定义层或 Agent 层；
- `reference`：只参考结构，在 Manyselves 内实现轻量版本。

### R-02：LangGraph 对照 POC

问题：LangGraph 作为底层图运行时是否比 MAF 更适合保留 Manyselves 当前执行语义？

POC 使用与 R-01 相同场景，比较：

- 自定义节点；
- Conversation 绑定；
- 循环和动态并行；
- Checkpoint；
- 状态和事件投影；
- 定义编译器工作量；
- 依赖复杂度。

### R-03：Burr State/Action 模型评估

问题：是否采用显式 `reads` / `writes` 作为 Action 数据流检查基础？

输出：决定 Workflow Definition 是否要求 Action 显式声明状态读写，或由表达式静态推导。

### R-04：耐久后端延后评估

只有出现以下真实需求时才评估 Temporal / Restate：

- 多 Worker；
- 跨机器任务；
- 长时间等待外部事件；
- 当前文件状态机无法满足的耐久执行。

当前不引入。

## 14. 测试与验收矩阵

### 14.1 Kernel

- 定义解析；
- 引用校验；
- Contract 兼容；
- 变量 Def-Use；
- 控制流；
- Conversation 绑定；
- Recovery Policy；
- State Store；
- Import Boundary。

### 14.2 Runtime Adapter

- 当前 AgentLoop 调用；
- 当前 Tool；
- Artifact；
- MessageBus；
- Usage Ledger；
- Result Cache；
- Provider 错误。

### 14.3 Reporting Parity

- 所有现有 `tests/reporting/`；
- 模块 Lane；
- Cross 回写；
- Local Regression；
- Chief / Final；
- Same-run Resume；
- 结构化纠正；
- 最终 DOCX 和 Output Validation；
- 标准语义 Trace。

### 14.4 Cross-domain Neutrality

- 中立 Capability 无 reporting import；
- 无 Editor / Auditor / Cross；
- 能完成循环、Tool、Agent、Schema 和输出；
- Kernel 源码不出现领域 ID。

## 15. 功能开关与迁移回退

迁移期间保留：

```yaml
runtime:
  workflow_engine: legacy | declarative | shadow
```

- `legacy`：现有报告 Runner；
- `declarative`：新执行器；
- `shadow`：Legacy 真实执行，新引擎用 Fake/Replay 输入生成语义 Trace 对比。

在完整等价验证前默认 `legacy`。

每个迁移阶段必须可以通过配置回退，不删除旧产物或旧 Run 的读取能力。

## 16. Commit 与 PR 纪律

Codex 每个 PR 只完成一个工作包或工作包中的一个垂直切片。

PR 必须包含：

- 当前问题和边界；
- 修改的抽象层；
- 未修改的行为；
- 新增定义或接口；
- Characterization / Unit / Integration Tests；
- 是否触发研究；
- 回退方式；
- 下一工作包依赖。

禁止：

- 一次移动全部 reporting 包；
- 同时修改 Kernel、全流程、API 和前端；
- 为了目录整洁做无行为证明的大规模重命名；
- 删除恢复逻辑后承诺未来补回；
- 将目标设计写成当前已实现功能。

## 17. 完成定义

Manyselves 的本轮通用化只有同时满足以下条件才算完成：

1. Kernel 不依赖任何领域 Capability；
2. Agent、Tool、Contract、Task、Workflow、Gate 和 Recovery 可由外部定义加载；
3. Workflow 可编译为可执行 ResolvedPlan；
4. Conversation 复用完全由定义和状态决定；
5. 当前模型恢复能力被保留并可由所有 Capability 使用；
6. 配电报告以定义驱动路径完成全流程并通过行为等价验证；
7. 第二个中立 Capability 不依赖报告身份或流程；
8. FastAPI 和 React 能通过通用 Run 接口启动并展示至少两种 Capability；
9. 任务完成后能够展示结果和成本；
10. 旧报告 Run 和兼容 API 在迁移窗口内仍可读取和恢复。

## 18. Codex 首个实现任务

在本方案合并后，Codex 的第一项代码任务不是重构 `AgentLoop`，而是执行 WP-00：

1. 建立标准语义事件类型；
2. 为当前报告最小模块 Lane 添加 Trace Adapter；
3. 用现有 Scripted/Fake Runner 生成稳定 Snapshot；
4. 建立 Kernel Import Boundary 测试骨架；
5. 不改变任何生产调度。

只有基线可观察、可重复之后，才能开始 WP-01。
