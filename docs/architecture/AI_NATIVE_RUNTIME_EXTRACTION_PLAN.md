# Manyselves 最终定义驱动架构收敛方案

> 文档性质：规范性架构与主实施方案
>
> 目标：把现有实现收敛为一个 Stateless Kernel、一个通用 Compiler/Runtime、一个文件定义系统、多个 Capability-owned Domain Runtime，以及一个通用 FastAPI/React 产品外壳
>
> 当前状态：重新开放，尚未完成；见 [`../implementation/RUNTIME_EXTRACTION_STATUS.md`](../implementation/RUNTIME_EXTRACTION_STATUS.md)
>
> 原 `WP-00`～`WP-12`：作为已完成的抽取历史保留在 Git 中，不再定义最终完成条件

## 1. 决定

Manyselves 的最终目标不是“新声明式路径能够旁路运行，同时 Legacy Reporting Runner 继续作为默认和实现宿主”。兼容式抽取已经完成了有价值的 Definition、Compiler、Kernel、Runtime Host、Capability Package 和通用 UI 基础，但它不是最终架构。

本方案废止以下旧目标：

- Legacy 默认必须保留；
- Declarative Runner 继承或包装 Legacy Runner；
- Generic Application 通过 ReportingFacade/Reporting Host 启动 Capability；
- 旧 Reporting API、旧 Run 恢复和双路径 A/B 是完成门槛；
- `manyselves/core/reporting` 可以继续同时充当通用机制、领域实现和流程宿主。

最终架构只有一条生产执行链：

```text
Capability files
→ Definition Loader / Registry
→ Workflow Compiler
→ Resolved Plan
→ Stateless Kernel
→ Generic Workflow Runtime Host
→ Capability-owned Agent/Tool/Interaction implementations
→ State / Recovery / Events
→ Generic Application / FastAPI / React / Outputs
```

旧代码只作为 Characterization 来源和迁移中的临时实现，不再是公共边界或产品承诺。

## 2. 架构原则

### 2.1 一个 Kernel

Kernel 必须是纯业务无关状态转换：

```text
transition(plan, state, event) -> state, effects
```

它不执行 I/O，不调用 Provider，不知道 Capability ID，不包含 Reporting 角色、模块、字段或路由。

### 2.2 一个 Compiler/Runtime

所有 Capability 使用同一个 Loader、Registry、Compiler、ResolvedPlan、Action Executor Registry、Workflow Runtime Host、State Store、Event Projection 和 Interaction Resume 语义。

Capability 不得复制这些组件，也不得在自己的 Runner 中形成第二状态机。

### 2.3 每个 Capability 一个领域 Runtime

Capability 可以拥有自己的 Python 领域实现：

```text
capabilities/<id>/
├── capability.yaml
├── agents/ tasks/ tools/ contracts/ workflows/ recovery/
├── domain/
└── runtime/
    ├── binding.py
    ├── tools.py
    ├── agent_invokers.py
    ├── interactions.py
    ├── rendering.py
    └── delivery.py
```

这不是 Capability Kernel。领域 Runtime 只实现通用 Executor 请求的具体操作，并返回通用 Outcome/ActionResult。

### 2.4 文件负责控制流，Python 负责能力

YAML/Markdown/Schema 决定 Agent/Task/Tool、顺序、条件、循环、并行、Join、Subworkflow、Conversation、Interaction 和输出。Python 实现确定性操作、Provider 边界、领域转换、渲染和交付。

禁止用粗粒度 Python Tool 隐藏完整工作流，也禁止为领域步骤增加 Kernel Action Kind。

## 3. 最终模块边界

```text
manyselves/
├── kernel/
│   ├── definitions/          # models, loader, registry, catalog
│   ├── contracts/            # Pydantic / JSON Schema adapters
│   ├── workflow/             # action models, compiler, plan, state, pure transitions
│   ├── recovery/             # recovery events/policy reduction，不执行 Provider
│   └── ports/                # agent/tool/conversation/state/event/artifact protocols
├── runtime/
│   ├── workflow_host.py
│   ├── agents/               # generic Agent invocation/session/correction/continuation
│   ├── tools/                # generic Tool binding/outcome/result reuse
│   ├── conversations/
│   ├── interactions/
│   ├── state/
│   └── events/
├── capabilities/
│   ├── distribution_reporting/
│   │   ├── capability.yaml
│   │   ├── agents/ tasks/ tools/ contracts/ workflows/ recovery/
│   │   ├── domain/
│   │   └── runtime/
│   └── parameter_adjustment/
├── application/              # generic catalog/run/projection services
├── webapi/                   # generic HTTP transport
└── frontend/                 # generic Schema/Run/Event/Output UI
```

目录可以分切片迁移，但生产依赖必须逐步收敛到该结构。禁止用“物理移动风险”作为永久保留语义混杂的理由。

## 4. 强制依赖规则

### 4.1 Kernel

- 不导入 `manyselves.runtime`、`manyselves.capabilities`、`manyselves.core.reporting`、`application`、`webapi`；
- 不出现任何 Capability ID 或领域角色；
- 只依赖标准库和定义/合同所需的小型基础依赖；
- ActionResult 通过 Event 进入 transition，Executor 不直接篡改权威 State。

### 4.2 Runtime

- 依赖 Kernel 公开模型和端口；
- 不导入具体 Capability；
- 不用 Capability ID 选择 Agent、Tool、Recovery 或 Projection；
- 通用恢复机制不包含 Reporting Prompt 或业务结果类型。

### 4.3 Capability

- 依赖 Kernel/Runtime 公开边界；
- 自己装配定义引用到 Python 实现；
- 领域类型、Prompt、Validator、Renderer、Delivery 留在包内；
- 不拥有 Workflow Host、通用 Store、通用 Run API 或前端路由；
- Capability 间默认不互相导入。

### 4.4 Application/Web/API/UI

- 通过 Catalog/Registry 发现 Capability；
- 通过通用 Runtime Binding 启动、查询和恢复 Run；
- 不把 ReportingFacade 作为通用 Host；
- 不因 Capability ID 分支展示表单、WAITING、Events、Outputs 或 Cost。

## 5. 定义系统

最终文件定义至少包括：

- `CapabilityDefinition`：索引包内定义和 Runtime Binding；
- `AgentDefinition`：身份、Instructions、Model/Profile、Tools、Contracts、Conversation；
- `ToolDefinition`：实现引用、Input/Output Contract、模型可见性和执行属性；
- `ContractDefinition`：Pydantic 或 JSON Schema；
- `TaskDefinition`：Agent、目标、输入输出、Tools、Recovery；
- `WorkflowDefinition`：通用 Actions、变量、控制流、输入输出；
- `RecoveryPolicyDefinition`：通用事件到通用动作的策略。

Gate 不是必需层。只有真实业务定义需要且现有 Contract/If/Recovery 无法表达时才可引入；本轮默认不新增 Gate。

Compiler 在 Provider 调用前完成：

1. 定义与实现引用解析；
2. Action Kind 和 ID 校验；
3. Agent/Task/Tool/Contract/Recovery/Subworkflow 解析；
4. 变量 Def-Use 与表达式检查；
5. 输入输出合同兼容；
6. Conversation Key 与 Agent 绑定；
7. If/Goto/Loop/Parallel/Join/Subworkflow 控制流；
8. 所有可达路径的完成或等待终点；
9. 输出合同；
10. 本 Run 完整 Plan 和定义快照冻结。

## 6. 通用 Runtime

### 6.1 Action Executor

```python
class ActionExecutor(Protocol):
    kind: str

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult: ...
```

Executor 只返回输出、Patch、Event 和控制信号。Runtime Host 把它转换为 Kernel Event，再由 Kernel 产生权威状态。

### 6.2 Agent

通用 Agent Runtime 负责：

- `AgentDefinition + conversationKey` 解析稳定 Conversation；
- 调用 Provider/Agent Loop；
- 绑定声明的 Tool 和 Contract；
- 结构化结果校验；
- Usage/Event 投影；
- 通用 Recovery 事件与执行动作。

Capability Agent Invoker 负责 TaskEnvelope、领域 Prompt、领域 Tool 选择和结果转换，但不得拥有整条 Workflow。

### 6.3 Tool

Tool Runtime 以 ToolDefinition 解析 Python 实现，校验输入输出并生成统一 Outcome/Event。领域工具实现在 Capability，通用缓存或已完成结果复用在 Runtime。

### 6.4 Interaction 与恢复

WAITING 必须是 WorkflowState 的通用状态，嵌套路径按外到内 Action/Subworkflow 段表达。提供输入时只恢复目标叶子及祖先，不重放已完成 Parallel 兄弟。

### 6.5 Recovery

最终路径保留：结构化纠正、Schema 修正、Max Token、Tool Slice、No-progress、已完成 Tool Result 复用、原 Conversation/Session、Same-run 完成结果恢复。

Kernel 只规约事件/状态；Runtime 实现通用机制；Capability 提供 Prompt、合同和领域接受逻辑。不得让这些能力只存在于 `ReportingAgentRunner` 才可使用。

## 7. Distribution Reporting Capability

Reporting 是复杂领域实现，不是平台内核。它应拥有：

- ReportRequest、Evidence、Finding、Revision、Verdict、Delivery 等模型；
- Editor/Auditor/Cross/Chief/Final 的定义和 Prompt；
- 报告专属 Task/Tool/Contract/Recovery；
- 领域准备、接受、转换、审查、渲染和交付实现；
- Capability Runtime Binding。

它不得拥有：

- 独立 Workflow State Machine；
- 声明式 Runner 对 Legacy `ReportWorkflowRunner` 的继承；
- 由 Python `run()` 决定完整模块/Cross/Chief/Final/Delivery 顺序；
- 通用 Application 的 Host 身份；
- 通用 FastAPI/React 分支。

现有 `manyselves/core/reporting` 必须分类迁移：

| 类别 | 动作 |
| --- | --- |
| 业务中立 Agent/Tool/Conversation/Recovery 机制 | 提取到 `runtime/` |
| Reporting 领域模型和步骤实现 | 移入 Capability `domain/` / `runtime/` |
| 旧整流程编排、双路径选择和仅兼容入口 | 删除 |
| 暂时仍被最终路径使用的实现 | 先补 Characterization，再拆分；状态中明确债务 |

## 8. Generic Application / FastAPI / React

通用应用表面必须覆盖：

```text
GET  /capabilities
GET  /workflows
GET  /workflows/{id}/input-schema
POST /runs
GET  /runs/{id}
POST /runs/{id}/input
POST /runs/{id}/cancel
GET  /runs/{id}/outputs
GET  /runs/{id}/events
GET  /runs/{id}/cost
```

具体 URL 可维持当前已发布形式，但语义不得依赖 ReportingFacade 或 Capability-ID 分支。

React 通用 Run Workspace 根据 JSON Schema 生成输入，根据 waitingInput 生成继续表单，根据 Outputs 显示值/Artifact，根据 Events 和 Cost 投影状态。专属 View 是可选扩展，不能替代通用页面。

## 9. 最终架构收敛工作包

原 `WP-00`～`WP-12` 已完成基础抽取。本轮使用 `FA-*`（Final Architecture）作为新的依赖、提交和状态边界。

### FA-00：重新建立事实基线与规范

目标：撤销错误的“兼容式终点已经完成”声明。

任务：

- 审计生产调用链和物理依赖；
- 分类 `core` 为通用机制、Capability 领域实现、旧兼容债务；
- 列出 Legacy/ReportingFacade/Runner 依赖；
- 重写规范和状态；
- 固定不变量和完成证据矩阵。

完成：文档明确最终形态和当前缺口，状态为实施中。

### FA-01：架构边界 Characterization

先写会失败的测试证明最终边界尚未成立：

- Declarative Reporting 不得继承 `ReportWorkflowRunner`；
- Generic Application/WebAPI 不得以 ReportingFacade 作为 Capability Host；
- Runtime 不得导入具体 Capability；
- Capability 定义不得调用整流程兼容 Tool；
- React/Projection 不得出现 Capability-ID 流程分支。

测试必须检查生产接线，而不是只扫描类名。

### FA-02：抽取通用 Agent/Conversation/Recovery Runtime

目标：把业务无关机制从 `core`/Reporting Runner 中提取为通用服务。

垂直切片：

1. Conversation/Session Registry；
2. 定义驱动 Tool Binding；
3. Agent Invocation 与结构化结果；
4. Correction/Continuation/No-progress；
5. completed Tool Result 和 Same-run Agent result reuse；
6. Usage/Event 投影。

每个切片先 Characterization，并以中立 Capability 或脚本 Agent 证明通用性。

### FA-03：建立 Distribution Reporting Domain Runtime

目标：声明式 Reporting 通过组合 Capability Domain Runtime 执行，不继承 Legacy Runner。

顺序：

1. 提取 Reporting 领域上下文和服务对象；
2. 将领域准备/接受/渲染/交付实现绑定为 Capability Tools/Invokers；
3. 让文件 Workflow 成为唯一控制流所有者；
4. 将 Legacy `run()` 与声明式入口解耦；
5. 删除 `DeclarativeReportWorkflowRunner(ReportWorkflowRunner)` 继承；
6. 用最终 Runtime Host 运行 Reporting 全链。

不得通过一个新的 `DistributionReportingRunner.run()` 把旧编排换名保留。

### FA-04：通用 Capability Runtime Binding

目标：Application 只认识通用 Capability Binding/Run 协议。

任务：

- 让 Registry 加载每个 Capability 的 Runtime Binding；
- Distribution Reporting 自己装配领域服务；
- 移除 generic route/facade 对 ReportingFacade 的 Host 依赖；
- 统一 start/query/input/cancel/outputs/events/cost；
- 证明 Reporting 和 Parameter Adjustment 走同一接口。

### FA-05：物理归属与旧流程删除

目标：代码目录和依赖图与设计一致。

任务：

- 通用机制迁入 `runtime/`；
- Reporting 领域代码迁入 Capability；
- 更新导入和测试；
- 删除未使用的 Legacy Runner、Facade、Adapter、Engine selector、兼容 Tool 和旧默认入口；
- 删除只验证已废止双路径的测试；
- 保留并重定向验证真实业务语义的测试。

这不授权删除真实 Run 数据或用户产物。

### FA-06：通用产品表面收敛

目标：FastAPI/React 仅由定义与通用投影驱动。

任务：

- 重新审计所有 routes、facades、frontend branches；
- 移除 Reporting 特殊启动/恢复/输出分支；
- 验证复杂 Schema、WAITING、Events、Artifacts、Cost；
- 两个 Capability 通过同一 UI 流程可操作。

### FA-07：完成审计和最终真实测试

自动阶段：

- 生产调用图审计；
- Kernel/Runtime import boundary；
- definitions/compiler/runtime/capability/application/web/frontend focused + affected tests；
- 构建 wheel/frontend；
- 验证发布物包含定义和 Capability Runtime；
- 证明没有未授权的新 Gate/hash/CAS/编排依赖。

自动证据全部通过后，只进行一次真实测试：真实 Provider、真实项目、浏览器、长时间 Reporting Run、中立 Capability、WAITING 恢复、Outputs/Artifacts/Events/Cost。失败时保留同一 Run 和现场，不自动新建 Run。

## 10. Characterization 与删除策略

Characterization 固定的是语义，不是旧类：

- Conversation 和 Session 是否复用；
- Provider/Tool 调用次数；
- 结构化纠正和 Continuation；
- Parallel 兄弟是否重放；
- Reporting Finding/Revision/Review/Delivery 结果；
- Outputs/Artifacts/Events/Cost 投影。

旧类移除后，测试应通过 Capability/Runtime 的最终公共边界验证这些语义。不得为让旧测试继续导入旧类而保留无生产用途的 Adapter。

## 11. 实时设计差异审计

每个 FA 工作包开始和结束都生成一份短差异表，写入唯一状态文件：

| 设计要求 | 当前生产证据 | 差异 | 当前切片补救 | 验证 |
| --- | --- | --- | --- | --- |

必须主动检查：

- MRO/构造链是否仍含 Legacy Runner；
- generic route/facade 是否仍传 Reporting host；
- `core` 是否仍混合通用机制与领域编排；
- YAML 是否仍调用整流程 Tool；
- Capability 是否拥有自己的领域实现；
- Kernel/Runtime 是否无领域词汇和导入；
- 两个 Capability 是否走同一 API/UI；
- 文档完成声明是否有足够当前证据。

发现偏差时先补 Characterization，再修复并更新后续顺序；不得等到最终审计才处理。

## 12. 测试策略

默认只运行 focused 和受影响测试，不做全量回归，除非用户明确要求。

证明矩阵：

- Kernel：纯 transition、compiler、control flow、plan completeness、import boundary；
- Runtime：Agent/Tool/Conversation/Recovery/WAITING/State/Event；
- Capability：文件定义、Python binding、领域结果、Reporting 全流程；
- Application/API：catalog、run lifecycle、input、output、events、cost；
- Frontend：Schema forms、WAITING、outputs、events、cost、Capability neutrality；
- Packaging：wheel/frontend build、包内定义和 Python Runtime。

测试通过只证明覆盖到的行和行为。最终完成声明还需要生产调用链和发布物审计。

## 13. 新依赖、Gate、Hash、CAS

本轮不新增生产编排框架。Microsoft Agent Framework 和 LangGraph 的官方实现继续作为结构参考，不进入依赖。

默认禁止新增 Gate、Hash、CAS、锁和额外校验链。确有必要时，必须在编码前向用户说明具体问题、现有机制不足、替代方案、影响和回退，并获得明确批准。

现有业务中已经存在的相关逻辑可以在机械迁移中保持，但不得复制、扩展或拿来决定通用 Runtime 架构。

## 14. 回退与数据保护

每个 FA 切片用聚焦提交保持代码可回退。回退单位是提交，不是永久双生产路径。

不要求旧 API/Runner/Run 继续作为产品功能，但：

- 不删除真实项目、Run、Conversation 或 Artifact；
- 不覆盖已完成运行证据；
- 不对默认数据目录执行破坏性迁移；
- 必要的数据迁移必须单独设计和验证。

## 15. 完成定义

本计划只有在以下项目全部有当前证据时完成：

1. Kernel 是业务无关的纯状态转换；
2. Loader/Registry/Compiler 从文件定义生成完整 ResolvedPlan；
3. 所有生产 Action 由一个通用 Runtime Host 执行；
4. 通用 Agent/Tool/Conversation/Recovery/Event 机制不依赖 Reporting；
5. Distribution Reporting 拥有领域 Runtime，声明式路径不继承或包装 Legacy Runner；
6. 文件 Workflow 是 Reporting 控制流的唯一所有者；
7. Application/FastAPI 通过通用 Binding 启动任意 Capability；
8. React 通过 Schema/Run/Interaction/Event/Output/Cost 工作；
9. Reporting 与中立 Capability 使用同一生产链；
10. 未使用旧 Runner、Facade、兼容 Adapter 和 engine selector 不在生产和发布图；
11. Recovery、Conversation、Tool Result 和 Same-run 行为在最终路径验证；
12. focused/affected 自动验证和构建通过；
13. 最终真实 Provider/项目/浏览器测试通过；
14. 状态、文档、源码、测试和发布物一致。

历史真实 Run、Legacy 默认可用、双路径测试或一个声明式子流程成功，都不足以单独证明完成。
