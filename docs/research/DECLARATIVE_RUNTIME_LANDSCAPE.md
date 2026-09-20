# 声明式 Agent Runtime 官方参考与架构决定

> 用途：校准 Manyselves 最终 Loader/Compiler/Kernel/Runtime/Capability 分层
>
> 原始 POC：2026-08-20
>
> 官方资料复核：2026-08-23
>
> 决定：`reference`，不新增生产编排依赖

## 1. 研究问题

本研究回答：

1. YAML/JSON 定义能否编译为真实执行图；
2. Workflow 控制流与 Agent 动态行为应如何分工；
3. State、Checkpoint、Conversation 和 Human Input 如何分层；
4. Python 领域操作如何在声明式工作流中接入；
5. Manyselves 是否应引入外部生产 Runtime；
6. Capability 是否应拥有自己的 Kernel。

结论不是选择一个“全能框架”，而是确定 Manyselves 自己的长期边界。

## 2. 最终研究结论

```text
one Stateless Kernel
+ one Generic Compiler/Runtime
+ file definitions
+ many Capability-owned Python Domain Runtimes/Tools
+ one generic FastAPI/React shell
```

每个 Capability 需要自己的 Python 领域实现，但不需要也不允许复制 Kernel。声明文件拥有控制流，Python 实现具体步骤。

Microsoft Agent Framework 和 LangGraph 都证明“编译后的通用执行图 + Python 节点/工具 + 外置状态”可行，但都不能直接替换 Manyselves 已有的 Definition、Contract、Conversation、Recovery、Tool Result、Artifact 和文件 Run 状态。因此继续采用内部轻量实现。

## 3. Microsoft Agent Framework

官方来源：

- [Declarative Workflows Overview](https://learn.microsoft.com/en-us/agent-framework/workflows/declarative)
- [Agent Framework Python Workflow samples](https://github.com/microsoft/agent-framework/tree/main/python/samples/03-workflows)
- [Declarative workflow samples](https://github.com/microsoft/agent-framework/tree/main/python/samples/03-workflows/declarative)

### 3.1 官方当前能力

官方文档明确描述：YAML 定义被转换为可执行 Workflow Graph；Python `WorkflowFactory` 从 YAML 创建工作流。Action 覆盖变量、条件、循环、Goto、Agent、Function Tool、MCP、外部输入、Conversation 和 EndWorkflow。

官方 Python samples 还覆盖：

- 顺序、条件、循环；
- fan-out/fan-in；
- Subworkflow；
- Human-in-the-loop；
- Checkpoint 与 Resume；
- Executor 输入输出事件；
- YAML Workflow 调用 Python Function Tool。

这支持 Manyselves 的决定：Workflow 应由文件 Action 组合，而业务 Python 通过 Tool/Executor 边界接入。

### 3.2 适用边界

Microsoft 官方同时区分：标准和频繁变化的编排适合 Declarative，复杂自定义逻辑和既有 Python 集成适合 Programmatic。Manyselves 对此采用“声明式控制流 + Capability Python 领域步骤”的组合，而不是企图把所有业务算法写成 YAML。

### 3.3 既有 POC 观察

隔离 POC 使用 `agent-framework-declarative==1.0.2`，未加入项目依赖：

- 两个字面 Conversation ID 分别保留消息历史；
- Checkpoint Storage 保存图步状态；
- 自定义 Agent 收到消息历史，但没有得到 Manyselves 所需的稳定 Session/Conversation Key 边界；
- 条件依赖 PowerFx/.NET，在当时 macOS 环境缺少 `dotnet` 时失败；
- 当时 `GotoAction` 指向 `ConditionGroup` 外部 ID 暴露 Builder 内部 ID 问题。

这些版本性观察只解释当时 `reference` 决定，不作为今天评价上游质量的普遍结论。

### 3.4 对 Manyselves 的约束

借鉴：

- YAML → Factory/Compiler → Executor Graph；
- 少量通用 Action；
- Function Tool 作为 Python 扩展；
- Subworkflow、HITL、Checkpoint 和事件。

不采用：

- 上游 Definition 公共格式；
- PowerFx；
- 上游 Conversation/Checkpoint 作为第二权威状态；
- 新生产依赖。

## 4. LangGraph

官方来源：

- [Graph API overview](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [Persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx)
- [LangGraph official repository](https://github.com/langchain-ai/langgraph)

### 4.1 官方当前模型

官方 Graph API 把图定义为：

```text
State + Nodes + Edges → compile() → executable graph
```

Node 是读 State、执行工作并返回 partial update 的 Python 函数；Edge 决定下一节点；Reducer 合并并行更新。官方要求图在运行前 compile，并提供条件边、Send、Command、Subgraph、Runtime Context 和可视化。

官方 Workflow/Agent 文档区分：

- Workflow 有预先确定的代码路径；
- Agent 动态决定工具和求解过程。

这正好支持 Manyselves 把跨 Agent 流程放在文件 Workflow，把单个 Agent 内部动态 Tool 使用放在 Agent Runtime。

### 4.2 Persistence 的启发与差异

官方 Persistence 将 Checkpointer 定义为 thread 范围的图状态快照，将 Store 定义为跨 thread 的应用数据。它支持 Conversation continuity、HITL、fault tolerance 和 resume。

Manyselves 已有冻结 ResolvedPlan、WorkflowState、Conversation、Tool Result、Artifact 和 Event Log。如果接入 LangGraph Checkpointer，会引入第二套权威状态和迁移边界。当前没有证明这种替换收益大于成本。

官方还说明启用 Checkpointer 时，恢复可能从 Node 起点重新执行，Node 的副作用需要能够安全重执行。Manyselves 已有 Action 级 Same-run 结果复用和完成兄弟保留语义，不能直接假设节点重执行等价。

### 4.3 既有 POC

隔离 POC 使用 `langgraph==1.2.11`，未加入生产依赖，验证了条件回边、两个 Conversation 状态、`Send` 动态分支、Reducer Join 和 Checkpoint。

它没有提供 Manyselves 所需的外部 Capability/Agent/Task/Tool/Contract/Recovery Definition Compiler，也不直接提供当前 Provider Session、领域结果或 Artifact 边界。

### 4.4 对 Manyselves 的约束

借鉴：

- State/Node/Edge 分离；
- 编译后执行；
- 条件、循环、并行和 Subgraph；
- thread state 与长期 store 分工；
- 明确恢复和副作用重执行语义。

不采用：

- LangGraph State/Checkpointer 作为第二生产状态；
- Reducer/Channel/Command 成为 Manyselves 公共定义；
- 新生产依赖。

## 5. Apache Burr

官方仓库：[apache/burr](https://github.com/apache/burr)

Burr 的价值是小型显式 State/Action 模型：Action 读取状态、执行普通 Python/LLM/Tool、返回新状态，Transition 决定下一 Action。它支持“Kernel 逻辑无状态、Run State 外置”的设计。

Manyselves 不引入 Burr；其 reads/writes 思想只作为 Compiler Def-Use 参考。

## 6. Temporal 与 Restate

官方来源：

- [Temporal Python SDK](https://github.com/temporalio/sdk-python)
- [Restate](https://github.com/restatedev/restate)

它们解决多 Worker、跨进程耐久执行、可靠通信和重试，不解决 Agent/Prompt/Tool/Schema/Capability 文件编译。当前单机文件状态与应用部署没有证明需要替换耐久后端，因此延后。

## 7. Capability Domain Runtime 决定

外部参考共同显示，通用 Runtime 节点最终仍需调用应用 Python 代码。Manyselves 因此明确三层：

```text
Stateless Kernel
      ↓
Generic Executor Runtime
      ↓
Capability Domain Runtime / Python Tools
```

Capability Domain Runtime 可以实现复杂领域功能，但不得：

- 复制 State Machine/Compiler/Host；
- 以整流程 `run()` 取代文件 Workflow；
- 让通用 Runtime 认识领域类型；
- 让 Application/FastAPI/React 依赖具体 Capability 服务。

若未来多个 Reporting Capability 共享领域机制，可以提取 Reporting Domain Framework；它仍在领域层，不上移为 Kernel。

## 8. Adopt/Adapt/Reference 决定

| 候选 | 决定 | 原因 |
| --- | --- | --- |
| Microsoft Agent Framework | `reference` | 最接近文件定义/执行图，但会引入第二套 Definition/Conversation/Checkpoint 边界 |
| LangGraph | `reference` | 图调度和持久化成熟，但需要自建全部 Manyselves Definition/Capability 绑定并迁移状态 |
| Burr | `reference` | State/Action 思想有用，替换收益不足 |
| Temporal/Restate | `defer` | 当前没有多 Worker/跨机器耐久需求 |

当前内部 Loader、Registry、Compiler、Stateless Kernel 和 Runtime Host 是生产方向。不新增外部编排依赖。

## 9. 对最终实施的直接约束

1. 文件 Workflow 编译为真实通用 Action/Executor，不是运行时自然语言解释；
2. Workflow 拥有跨步骤控制流，Agent 拥有单次任务内的动态 Tool 决策；
3. Python Tools/Invokers 属于 Capability；
4. State/Recovery/Event 保持一套权威实现；
5. 不引入 MAF/LangGraph 的内部 ID、PowerFx、Channel、Reducer 或 Checkpoint 格式作为公共定义；
6. 不为参考框架新增生产依赖；
7. 不新增 Gate、Hash、CAS 或额外一致性链；
8. Legacy Runner 不再是适配目标，最终路径必须直接组合通用 Runtime 与 Capability Domain Runtime。

## 10. 何时重新评估

只有出现以下真实需求才重新评估外部 Runtime：

- 多 Worker 或跨机器执行；
- 当前文件 Store 无法满足的耐久等待；
- 现有 Compiler/Host 无法表达的动态并行或 Subgraph；
- 有第二个生产系统愿意承担迁移和依赖成本；
- 隔离 POC 证明可以保留 Manyselves 的 Conversation、Recovery、Tool Result、Artifact 和单一权威 State。

重新评估前仍需官方源码、官方文档、同场景 POC 和生产边界影响说明。
