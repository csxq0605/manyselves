# 声明式 Agent 运行时与工作流内核调研

> 调研用途：支持 Manyselves 从配电报告垂直实现迁移到定义驱动、可恢复的通用 Agent 应用运行时
>
> 调研日期：2026-08-19
>
> 主实施方案：[`docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md`](../architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)

## 1. 调研问题

本次调研不寻找一个可以直接替换 Manyselves 的“全能 Agent 框架”，而是回答：

1. 是否已有框架证明 Agent 和 Workflow 可以通过 YAML/JSON 定义并编译为执行节点；
2. 哪个框架最适合作为 Manyselves 的声明式编译与执行参考；
3. 哪些能力应直接采用、适配或仅参考；
4. 如何保留 Manyselves 已有的 AgentLoop、Tool、Artifact、结构化合同和模型恢复能力；
5. 是否应在当前阶段引入新的运行时依赖。

## 2. 当前结论

### 2.1 最接近的参考：Microsoft Agent Framework Declarative Workflow

官方仓库：<https://github.com/microsoft/agent-framework>

重点路径：

```text
python/packages/declarative/
python/packages/declarative/agent_framework_declarative/
python/packages/declarative/agent_framework_declarative/_workflows/
declarative-agents/agent-samples/
declarative-agents/workflow-samples/
```

其核心链路为：

```text
YAML / JSON
→ AgentFactory / WorkflowFactory
→ Declarative Models
→ DeclarativeWorkflowBuilder
→ Executor Graph
→ Workflow Runtime
```

已经验证的能力：

- YAML Agent；
- Model、Provider、Connection、Instructions 和 Output Schema；
- YAML Workflow；
- `SetVariable`、`InvokeAgent`、`ConditionGroup`、`Foreach`、`GotoAction`；
- Tool、HTTP、MCP 和外部输入；
- 每个 Action 编译为真实 Executor；
- Checkpoint、暂停和恢复；
- 同一工作流中维护多个 Conversation；
- 通过 Conversation ID 重复调用同一 Agent；
- DeepResearch 示例中的停滞检测、重规划、Goto 和有限重启。

### 2.2 对 Manyselves 的直接启发

Manyselves 不应把配电报告协作模式抽为内核协议，而应采用：

```text
少量通用 Action
+ Agent / Tool / Contract Registry
+ Workflow Factory / Compiler
+ Action Executor Registry
+ Workflow State
```

报告中的 Editor、Auditor、Cross、Chief 只是 Workflow YAML 中引用的 Agent Definition 和业务状态值。

### 2.3 采用决定尚未做出

Microsoft Agent Framework 当前只被确定为首选 POC 对象。需要通过自定义 Adapter 验证：

- 能否复用 Manyselves AgentLoop；
- 能否保持当前 Tool 和 Artifact 语义；
- 能否保留结构化提交纠正和 Continuation；
- Conversation 能否按 Manyselves Key 稳定绑定；
- Workflow Checkpoint 是否与当前项目文件状态兼容；
- 依赖体积、Python 版本、许可证和升级成本是否可接受。

在 POC 之前不得直接将其加入生产依赖。

## 3. Microsoft Agent Framework 详细观察

### 3.1 Agent Definition

其声明式 Agent 可以定义：

- `kind`；
- 名称和说明；
- Model 和 Provider；
- Connection；
- Instructions；
- Tool；
- Input / Output Schema。

这证明 Agent 身份和模型配置可以独立于业务 Runner 进行加载。

### 3.2 Workflow Definition

其 Workflow YAML 使用通用动作，而不是预置行业流程。

DeepResearch 示例通过以下动作组装多 Agent 流程：

```text
SetVariable
CreateConversation
InvokeAzureAgent
ConditionGroup
SendActivity
GotoAction
EndConversation
```

同一组通用 Action 实现了：

- Research、Planner、Manager、Summary 等多个 Agent；
- 独立状态 Conversation 和任务 Conversation；
- 事实提取；
- 计划生成；
- 进展判断；
- 停滞计数；
- 事实和计划重建；
- Goto 回到先前动作；
- 有限重启和结束。

因此 Manyselves 不需要在 Kernel 中创建 `ReviewCycle`、`CrossCycle` 或其他领域动作。

### 3.3 WorkflowFactory

`WorkflowFactory`：

- 加载 YAML；
- 校验基础结构；
- 创建或解析 Agent；
- 注册 Tool；
- 将配置交给 `DeclarativeWorkflowBuilder`；
- 将 Input Definition 转成 JSON Schema；
- 可注入 Checkpoint Storage。

### 3.4 DeclarativeWorkflowBuilder

Builder 使用：

```text
action kind → executor class
```

映射创建真实节点。

它负责：

- 顺序边；
- If / ConditionGroup 条件边；
- Foreach 初始化、循环体、Next、回边和 Join；
- Goto 前向边或后向边；
- Action ID 和必填字段校验；
- Agent、Tool、HTTP 和 MCP Executor；
- 最大迭代次数。

Manyselves 的 Compiler 至少需要达到同等基础能力，并额外补充 Contract、变量 Def-Use、Conversation 绑定和领域无关性检查。

## 4. LangGraph

官方仓库：<https://github.com/langchain-ai/langgraph>

定位：低层、状态化、长时间 Agent 编排框架。

值得借鉴：

- StateGraph；
- Reducer；
- 条件边；
- 循环和子图；
- Checkpointer；
- Interrupt / Human-in-the-loop；
- Durable Execution；
- Streaming 和事件。

对 Manyselves 的意义：

- 可以作为 Executor Graph 的底层运行时；
- 可以减少自研图调度和 Checkpoint 的工作量；
- 不会替代 Definition Loader、AgentFactory、ToolFactory、ContractRegistry 和 WorkflowCompiler。

待验证问题：

- 动态 Conversation 与现有 AgentLoop 如何绑定；
- 当前文件状态和 Artifact 如何接入 Checkpointer；
- Parallel Lane 与现有任务结果恢复如何映射；
- 图 State 是否会迫使当前报告状态大规模重写；
- 声明式 DSL 仍需多少自研代码。

## 5. Apache Burr

官方仓库：<https://github.com/apache/burr>

定位：用简单 Python Action 构建显式状态机。

核心启发：

```text
Action 读取明确状态
→ 执行普通 Python / LLM / Tool
→ 返回新状态
→ Transition 决定下一 Action
```

Burr 对 Manyselves 的主要价值不是作为首选依赖，而是提醒 Kernel 保持小型和显式：

- 无状态的是 Executor 函数；
- 状态属于 Application / Run；
- LLM 只是 Action 实现的一种；
- Action 的 Reads/Writes 可以用于数据流检查；
- 状态快照和执行轨迹可以支撑调试与回放。

待决定：Manyselves Workflow Definition 是否要求每个 Action 显式声明 `reads` / `writes`，还是由输入输出表达式自动推导。

## 6. Temporal

官方仓库：<https://github.com/temporalio/sdk-python>

定位：分布式、可扩展、耐久的长时间业务工作流引擎。

适合解决：

- 多 Worker；
- 跨进程和跨机器；
- Activity 重试；
- Timer、Signal、Update；
- Workflow Replay；
- 分布式耐久执行。

不直接解决：

- Agent Definition；
- Prompt；
- Tool 暴露；
- Schema；
- Skill；
- 声明式工作流包。

当前决定：不在第一阶段引入。只有真实出现多 Worker、跨机器或长时间外部等待需求时再评估。

## 7. Restate

官方仓库：<https://github.com/restatedev/restate>

定位：可靠执行、可靠通信、Durable Promise / Timer 和一致状态。

适合解决：

- 已完成步骤不重复执行；
- exactly-once 风格的可靠消息；
- Durable Entity / K-V State；
- 异步任务和状态化服务。

对 Manyselves 的意义与 Temporal 类似：它可能成为未来耐久执行后端，但不是 Definition Compiler。

当前决定：延后。

## 8. AutoGen

官方仓库：<https://github.com/microsoft/autogen>

值得借鉴：

- Core API、AgentChat 和 Extensions 分层；
- 消息传递；
- 事件驱动 Agent Runtime；
- Local / Distributed Runtime；
- 多 Agent Handoff、Selector、Swarm 和 Graph Chat。

现状：AutoGen 已进入维护模式，官方建议新项目采用 Microsoft Agent Framework。

当前决定：只参考 Core Runtime 分层，不作为新依赖候选。

## 9. 对比矩阵

| 维度 | Microsoft Agent Framework | LangGraph | Apache Burr | Temporal | Restate |
| --- | --- | --- | --- | --- | --- |
| 声明式 Agent | 强 | 弱/需自建 | 无 | 无 | 无 |
| 声明式 Workflow | 强 | 需自建 | 主要代码式 | 代码式 | 代码式 |
| Action → Executor 编译 | 强 | Node API | Action / State | Workflow / Activity | Handler / Invocation |
| 条件与循环 | 支持 | 支持 | 支持 | 支持 | 支持 |
| Conversation / Agent | 原生 Agent | 需集成 | 自定义 | 自定义 | 自定义 |
| Checkpoint / Resume | 支持 | 强 | 支持 | 很强 | 很强 |
| Human Input | 支持 | 支持 | 可实现 | Signal / Update | Promise / Invocation |
| 可复用 Manyselves AgentLoop | 待 POC | 待 POC | 容易包装 | Activity 包装 | Handler 包装 |
| 作为当前首选参考 | 是 | 对照 | 状态模型参考 | 延后 | 延后 |

## 10. 研究决定规则

### 10.1 不以框架知名度决定

选择依据：

- 是否能保留当前恢复行为；
- 是否支持自定义 Agent 和 Tool；
- 是否允许文件式项目状态；
- 是否能生成可解释 Executor Graph；
- 是否需要大规模重写当前代码；
- 是否支持 Python 3.12 和当前依赖；
- 许可证和长期维护；
- 新场景定义工作量；
- 测试、调试和部署复杂度。

### 10.2 不把 POC 写成生产迁移

所有外部框架先使用隔离 POC：

```text
research/<framework>-spike/
```

POC 不进入当前报告真实执行路径。

### 10.3 使用同一 POC 场景

MAF 和 LangGraph 必须运行相同最小场景：

```text
读取结构化输入
→ 创建两个 Conversation
→ Agent A 输出结构化结果
→ Tool 调用
→ 条件判断
→ 原 Agent A Conversation 纠正或续写
→ Agent B
→ 最终 Contract 校验
→ Checkpoint / Resume
```

比较结果才有意义。

## 11. 当前推荐

### 短期

1. 参考 Microsoft Agent Framework 的 Loader、Factory、Action Model 和 Builder；
2. 参考 Burr 的显式状态和 Reads/Writes；
3. 使用 LangGraph 做同场景对照 POC；
4. 不引入 Temporal / Restate；
5. 保留 Manyselves 当前 AgentLoop、Tool、Artifact、Recovery 和文件状态；
6. 先建立自己的通用接口与 Legacy Adapter。

### POC 后的三种可能结论

- `adopt`：直接采用 Microsoft Agent Framework Declarative Runtime；
- `adapt`：采用其 Workflow Runtime，Manyselves 保留 Agent、Tool、Contract 和 Recovery；
- `reference`：只参考架构，在 Manyselves 内实现轻量 Loader、Compiler 和 Executor Runtime。

在 POC 数据完成前，主实施方案不依赖具体结论。
