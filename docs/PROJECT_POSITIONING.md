# Manyselves 最终产品与架构定位

> 状态：规范性目标定义
>
> 适用范围：仓库结构、公共接口、Capability 包、运行时、FastAPI、React、测试与发布
>
> 实施状态：产品、核心架构与验收已完成；最终结论见 [`implementation/RUNTIME_EXTRACTION_STATUS.md`](implementation/RUNTIME_EXTRACTION_STATUS.md)

## 1. 一句话定位

**Manyselves 是一个文件定义驱动、可恢复、可扩展的多 Agent 应用运行时。**

> **A file-defined, recoverable, extensible multi-agent application runtime.**

它由一个业务无关的无状态 Kernel、一个通用 Workflow Compiler/Runtime、多个 Capability-owned Python Domain Runtime、一个通用 FastAPI 运行接口，以及按 Capability 独立组合的产品前端组成。

## 2. 最终系统形态

```text
Markdown / YAML / JSON Schema / Python Tool references
                         │
                         ▼
              Definition Loader & Registry
                         │
                         ▼
                  Workflow Compiler
                         │
                         ▼
             Resolved Plan + Stateless Kernel
                         │
                         ▼
           Generic Action / Executor Runtime
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
        Agents          Tools       Interactions
          │              │              │
          └──────────────┼──────────────┘
                         ▼
              State / Recovery / Events
                         │
                         ▼
       FastAPI / Capability Frontend / Outputs
```

Manyselves 不是把所有功能塞进“Kernel”，也不是让每个业务复制一套 Runner。系统只有一套通用编译和执行语义；每个 Capability 只提供自身定义和领域实现。

## 3. 一个 Kernel，一个 Runtime，多个 Capability

### 3.1 Stateless Kernel

Kernel 只认识：

- Definition、Contract、ResolvedPlan；
- Action、State、Event、Effect；
- 通用控制流和完成/等待/失败语义；
- Agent、Tool、Conversation、Recovery、Artifact 等端口。

Kernel 不认识 Provider、文件路径、数据库、FastAPI、React，也不认识 Reporting、Editor、Auditor、Cross、Chief、模块 `2.1`～`2.5` 或任何具体领域字段。

“无状态”表示 Kernel 本身不保存可变业务状态，而是执行纯转换：

```text
Plan + Current State + Event → New State + Effects
```

### 3.2 Generic Compiler/Runtime

通用层负责：

- 加载和注册文件定义；
- 校验引用、合同、变量和控制流；
- 生成并冻结 ResolvedPlan；
- 调度通用 Action Executor；
- 调用 Agent、Tool 和 Interaction 端口；
- 持久化 Run State、Conversation、Recovery 和 Events；
- 恢复 WAITING、FAILED 或中断的同一 Run；
- 把通用 Run 投影给应用层。

### 3.3 Capability Domain Runtime

每个 Capability 拥有自己的 Python 领域运行时，而不是自己的 Kernel。它可以包含：

- 领域模型、值对象和确定性规则；
- Python Tools 与外部系统适配；
- Capability 专属 Agent Invoker 和 Prompt/Task 绑定；
- 领域结果接受和转换；
- 渲染、发布、交付；
- 领域错误到通用 Runtime Outcome 的映射。

例如 `distribution-reporting` 可以拥有报告审查、证据、DOCX 和交付实现；`parameter-adjustment` 可以只拥有参数计算 Tool 和可选 Agent。二者都通过同一 Compiler、Kernel、Runtime Host 和 Run API 执行。

## 4. 文件定义与 Python 的职责

### 4.1 文件定义描述组合

Markdown、YAML 和 JSON Schema 应表达：

- Capability 元数据和定义索引；
- Agent 身份、Instructions、模型配置和 Tool 可见性；
- Task 目标、约束、输入输出 Contract 和 Recovery Policy；
- Tool 实现引用、Schema 和执行属性；
- Workflow 的顺序、条件、循环、并行、Join 和 Subworkflow；
- Conversation Key、RequestInput、输出和恢复引用。

定义必须先由确定性 Compiler 解析，不能直接交给模型自由解释。

### 4.2 Python 实现能力

Python 负责模型或外部系统真正要执行的操作：

- Provider/Agent 调用；
- 文件、检索、计算和第三方系统 Tools；
- Pydantic/Schema 领域模型转换；
- 领域结果接受、渲染和交付；
- 与通用端口对接的 Capability Binding。

Python 不应隐藏 Workflow。一个 Python Tool 如果内部执行完整 Author→Auditor→Cross→Chief→Delivery 流程，就破坏了定义驱动边界。

## 5. 目标仓库边界

```text
manyselves/
├── kernel/
│   ├── definitions/
│   ├── contracts/
│   ├── workflow/
│   ├── recovery/
│   └── ports/
├── runtime/
│   ├── agents/
│   ├── tools/
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
├── application/
├── webapi/
└── frontend/                 # 当前 distribution-reporting 配电安全产品前端
```

如果多个 Reporting Capability 以后共享证据、审查或渲染机制，可以在出现第二个真实使用者后提取 `domains/reporting/`。该层仍是 Reporting 领域框架，不是 Kernel。

## 6. `core` 的最终处理

历史 `manyselves/core` 需要按语义拆分，而不是整体改名：

| 内容 | 最终归属 |
| --- | --- |
| 通用 Agent/Provider/Tool/Conversation 执行机制 | `runtime/` |
| 通用状态、恢复和事件机制 | `kernel/` 或 `runtime/`，按纯逻辑/副作用区分 |
| Reporting 模型、审查、渲染和交付 | `capabilities/distribution_reporting/domain` 或 `runtime` |
| 隐藏业务流程的整流程 Runner | 删除 |
| 无生产引用的旧兼容 Executor/Adapter | 删除或明确隔离，不进入发布路径 |

最终声明式 Reporting 不得继承、包装或把执行委托给 Legacy `ReportWorkflowRunner`。旧流程可作为迁移时的 Characterization 来源，但不是公共接口或完成条件。

## 7. 通用应用接口与 Capability 产品前端

FastAPI 只提供通用资源：

- Capability 和 Workflow 发现；
- Workflow Input Schema；
- Run 创建、查询、取消和恢复；
- Interaction 输入；
- Outputs、Artifacts、Events 和 Cost。

Capability 通过 Registry/Binding 接入，不由路由硬编码具体服务。

每个可交付 Capability 拥有自己的产品前端。当前仓库的 `frontend/` 是
`distribution-reporting` 配电安全服务 Demo 的前端：用户通过 Main 对话启动六类报告操作，
并在同一产品界面查看运行、WAITING、产物和成本。它不是用于选择所有 Capability 的平台控制台。

产品前端仍消费通用运行投影：

- JSON Schema；
- Run 状态；
- Waiting Interaction；
- Event；
- Value/Artifact Output；
- Cost/Usage。

其它 Capability 若成为产品，需要建立自己的前端应用、路由和交互设计；不能把它追加到配电安全前端的 Capability 选择器中。多个前端可以共享 Schema 表单、WAITING、Output、Event、Cost 等无业务语义的 UI 基础组件，也可以共同调用同一 FastAPI Run API，但共享组件不得通过 Capability ID 承载业务流程分支。通用 Run Workspace 只可作为开发/诊断 Harness，不是统一产品入口。

## 8. Recovery 是 Runtime 能力，不是旧 Runner 特权

最终生产路径必须支持：

- 普通文字结束但需要结构化提交时，在原 Conversation 纠正；
- Schema 校验失败时，把精确错误返回原 Conversation；
- Max Token 和 Tool Slice Continuation；
- No-progress 停止；
- 已完成 Tool Result 复用；
- Conversation/Session 身份复用；
- Same-run 已完成结果恢复；
- WAITING 输入在嵌套 Subworkflow/Parallel 中恢复而不重放已完成兄弟。

Runtime 只认识通用事件与动作。具体纠正 Prompt、业务合同和接受逻辑属于 Capability。

## 9. 配电报告的正确地位

配电安全报告是第一个复杂 Capability，用于证明：

- 大量 Agent 和稳定 Conversation；
- 文件定义的并行、循环、Barrier 和 Subworkflow；
- 结构化纠正与恢复；
- 领域审查、修订和跨模块回写；
- Artifact、DOCX、交付和成本投影。

Editor、Auditor、Cross、Chief、Final 只属于该 Capability。它们不能成为 Kernel Action、Runtime 类型、通用 FastAPI 路由或 React 分支。

## 10. 旧兼容不再是产品承诺

Legacy Runner 默认、双路径开关、旧 Reporting API、旧 Run 恢复和 Legacy/Declarative A/B 不再是最终架构要求。它们不得阻止：

- 声明式路径与 Legacy 父类解耦；
- Application 移除 Reporting 专属 Host；
- 删除未使用的兼容 Adapter；
- 将领域代码移入 Capability；
- 把通用机制移入 Runtime。

迁移期间仍应保护真实数据和已完成运行产物；“不要求兼容”不授权破坏性删除用户数据。

## 11. 产品承诺

1. **文件定义组合**：新流程优先修改定义，而不是复制 Runner。
2. **编译后执行**：引用、合同和控制流在 Provider 调用前确定。
3. **业务无关 Kernel**：领域语义永不成为 Kernel 原语。
4. **Capability-owned Python**：领域代码留在对应 Capability。
5. **通用恢复**：恢复能力通过 Runtime 端口供所有 Capability 使用。
6. **共享运行接口、独立产品前端**：所有 Capability 走相同 Run API；每个产品 Capability 独立组合自己的 React 前端。
7. **可观测且可接续**：权威 State、Event、Output 和 Cost 可以投影和恢复。
8. **最小生产依赖**：不为架构外观引入第二套编排运行时。

## 12. 非目标

Manyselves 不是：

- 配电报告专用生成器；
- 每个 Capability 一套 Kernel/Runner 的插件集合；
- 只支持 DAG 的批处理器；
- 让模型直接修改 State 的自由协作系统；
- 用 YAML 包装一个整流程 Python 函数的伪声明式系统；
- 通过兼容层无限保留两套生产路径的迁移工程；
- 只封装 Provider API 的聊天页面。

## 13. 完成标准

目标实现完成必须由当前生产路径证明：

- Definition Loader/Registry/Compiler 和 Stateless Kernel 业务无关；
- 通用 Runtime 执行 Agent/Tool/Interaction，并持久化 State/Recovery/Events；
- Distribution Reporting 的 Python 领域代码归 Capability 所有，且不继承 Legacy 整流程 Runner；
- 至少一个中立 Capability 通过同一链路执行；
- Application/FastAPI 不含 Capability-ID 业务分支；共享 React 基础组件不含业务分支，Capability 产品前端可直接组合自己的交互；
- 当前 `frontend/` 只暴露 Distribution Reporting 产品能力，不把中立测试 Capability 混入产品导航；
- Recovery 和 Same-run 恢复只依赖最终路径；
- 未使用的旧 Runner、Facade 和兼容入口不在生产调用图；
- 自动验证、构建和最终真实 Provider/项目/浏览器测试通过；
- 文档与实际代码一致。

当前完成结论以生产路径、自动验证、真实运行证据和最终用户验收共同成立为依据。历史过程中的失败、待验证项和阶段性限制继续保留为追溯记录，不覆盖最终完成状态。

## 14. 对外描述

### 中文

> Manyselves 是一个文件定义驱动、可恢复、可扩展的多 Agent 应用运行时。它把 Markdown、YAML、JSON Schema 和 Capability Python Tools 编译为业务无关的执行计划，通过一个无状态 Kernel 和通用 Agent/Tool/Interaction Runtime 运行，并以统一的 FastAPI、Events 和 Outputs 支撑按 Capability 独立设计的产品前端。

### English

> Manyselves is a file-defined, recoverable, extensible multi-agent application runtime. It compiles Markdown, YAML, JSON Schema, and capability-owned Python tools into business-neutral execution plans, runs them through a stateless kernel and a generic agent/tool/interaction runtime, and exposes a common FastAPI, events, and outputs surface to capability-specific product frontends.
