# Manyselves 项目定位基线

> 状态：目标架构与产品定位的规范说明
>
> 适用范围：README、架构设计、实现计划、Codex 任务拆分、能力包设计与服务端封装
>
> 当前参考分支：`feature/react-fastapi-manyselves`

## 1. 一句话定位

**Manyselves 是一个面向复杂专业任务的、定义驱动且可恢复的多 Agent 应用运行时。**

> **A definition-driven, recoverable multi-agent application runtime for complex professional workflows.**

Manyselves 通过一个与具体业务无关的执行内核，将外部定义的 Agent、工具、Schema、状态变量、条件、循环、门禁、恢复策略和用户交互编译为可执行工作流，并通过 FastAPI、React 和服务器部署封装形成完整应用。

## 2. 项目的核心目标

Manyselves 的目标不是为某个领域预置固定 Agent 身份或固定协作流程，而是让一组独立定义可以被加载、校验、组装、执行、恢复和迁移：

- Agent 的身份、职责、边界、模型、工具、输入输出和上下文规则；
- Tool 的实现、输入输出合同、适用场景和执行属性；
- Task 的目标、约束、输入、输出和完成条件；
- Workflow 的顺序、条件、循环、并行、会话复用、任务分发和用户交互；
- Gate 的结构校验、确定性条件和业务验收要求；
- Recovery Policy 的结构化纠正、同会话续写、重复结果复用和无进展处理；
- Skill、知识和项目资料的加载范围；
- 前端与服务端对 Run、会话、文件、结果和成本的统一封装。

这些内容应主要通过 Markdown、YAML、JSON Schema、Pydantic 合同或结构化定义表达。只有新的可执行能力、确定性校验或外部系统接入需要 Python 实现。

## 3. “无状态内核”的准确含义

无状态内核不表示工作流没有状态，也不表示每一次模型调用都从零开始。

它表示：

- 内核不内置配电报告、能效管理、电费调度、科研实验等业务规则；
- 内核不内置 Editor、Auditor、Cross Reviewer、Energy Manager 等领域身份；
- 内核不依赖某个业务 Runner 中的隐藏变量决定下一步；
- 内核根据外部定义和持久化的当前状态执行 Action；
- Action 完成后返回结构化结果和状态变更；
- 下一次执行可以重新读取相同定义与状态并得到一致的调度判断。

状态属于一次 Run、Conversation、Task 或 Artifact；业务规则属于 Capability；内核只负责通用执行语义。

## 4. 内核应认识的通用概念

内核只应认识以下业务无关概念：

- `Definition`：可加载和校验的定义；
- `Capability`：一组可安装、可迁移的领域定义和扩展；
- `Action`：工作流中的声明式动作；
- `Executor`：Action 对应的运行实现；
- `Workflow`：Action、状态、条件、循环和交互的组合；
- `Conversation`：Agent 可复用或临时的会话身份与上下文；
- `Run`：一次工作流运行；
- `State`：Run 当前可持久化的状态；
- `Contract`：输入输出的结构化要求；
- `Recovery Policy`：Agent 或 Tool 执行不完整时的处理规则；
- `Interaction`：工作流和用户或前端之间的结构化交互；
- `Artifact`：文件、结构化结果和其他可传递产物。

`Cross`、`Editor`、`Auditor`、模块 `2.1`、SOC、实验参数等都只能存在于具体 Capability 中，不能成为内核前提。

## 5. 定义优先，而不是业务代码写死

目标执行链为：

```text
Markdown / YAML / Schema / Python Tool References
                         │
                         ▼
              Definition Loader & Registry
                         │
                         ▼
                  Workflow Compiler
                         │
                         ▼
                 Executor Graph / Plan
                         │
                         ▼
                   Generic Runtime
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
               FastAPI / React / Outputs
```

定义不会直接交给模型自由解释。它们必须先经过 Loader、Registry 和 Compiler，解析为真实 Agent、Tool、Schema、状态和 Executor 节点，并在执行前完成引用、变量、合同和控制流校验。

## 6. 可恢复执行是核心能力

Manyselves 的恢复设计主要用于处理模型执行的不确定性，而不是把所有基础设施异常都复杂化。

通用运行时应支持：

- 模型只返回自然语言、没有提交规定结构时，在原 Conversation 中纠正；
- 输出未通过 Schema 时，把精确校验错误返回原 Conversation；
- 输出达到 Token 上限时，在原 Conversation 中继续；
- Tool 轮次到达边界时继续当前任务，而不是重建任务；
- 已完成的纯读 Tool 结果可按任务和输入版本复用；
- 连续调用没有产生新状态、结果或有效会话事件时停止；
- 审查、修订或其他业务循环是否复用同一 Conversation，由工作流定义决定；
- 工作完成必须由结构化结果、状态和 Gate 判断，不能只相信模型声称完成。

当前报告系统中的结构化提交纠正、同身份续写、结果复用和审查闭环属于应被提取的可靠执行能力，不应在通用化过程中被删除。

## 7. 配电安全报告的正确地位

当前仓库中的配电安全报告是 **第一个复杂参考能力包**，不是 Manyselves 的产品边界，也不是内核的固定协作模型。

它用于验证运行时能够承载：

- 多个专业 Agent；
- 稳定 Conversation 和身份连续性；
- 结构化 Task 与结果合同；
- 并行 Lane、条件、循环和 Barrier；
- 审查、返修、局部回归和跨模块回写；
- 模型漏提交、输出截断和重复调用后的恢复；
- 文件、证据、Skill、产物和最终交付；
- React、FastAPI 和服务器部署封装。

未来的能效管理、电费与储能调度、科研模拟实验等只是其他 Capability 示例。它们可以定义完全不同的 Agent、Tool、Schema、状态和工作流，不要求存在 Cross、Editor 或 Auditor。

## 8. 当前仓库状态

当前仓库已经具备多项可复用基础：

- `AgentLoop`、Provider、MessageBus 和 Tool Registry；
- 严格的 Pydantic 输入输出合同；
- Task、Conversation、Artifact 和结果状态；
- 结构化提交纠正、Continuation、No-progress 检测和 Tool Result 复用；
- 配电报告中的复杂编排、门禁、恢复和交付；
- FastAPI、React、账户、项目、会话、事件和部署封装。

但当前实现仍然深度耦合：

- 报告路由和终态规则进入了通用 `AgentLoop`；
- Agent 会话、Tool 暴露、Schema、恢复和报告身份逻辑集中在 `ReportingAgentRunner`；
- 编排和状态迁移集中在报告 `workflow.py` 与 `review_lifecycle.py`；
- 角色、Skill、任务和流程仍有大量 Python 特殊分支；
- 配电报告尚未成为可独立安装和迁移的 Capability 定义包。

因此，Manyselves 当前处于从“配电报告垂直多 Agent 系统”向“定义驱动的通用 Agent 应用运行时”演进的阶段。

## 9. 产品承诺

后续架构和实现必须遵守以下承诺：

1. **定义而不是写死**：新增场景优先增加定义和 Tool 扩展，不复制领域 Runner。
2. **编译而不是猜测**：工作流、变量、Schema 和引用在执行前由确定性编译器校验。
3. **业务无关内核**：领域角色、字段和流程不得泄漏为内核原语。
4. **恢复而不是重做**：模型执行不完整时优先继续原任务和原 Conversation。
5. **合同驱动接口**：Agent、Tool 和工作流边界由结构化合同定义。
6. **参考能力保持等价**：配电报告迁移前后必须通过行为和输出等价验证。
7. **能力可迁移**：一个新场景应能以 Capability 包形式安装到其他项目或部署中。
8. **应用可部署**：运行时通过统一 FastAPI 与 React 外壳提供文件、对话、任务结果和成本。

## 10. 非目标

Manyselves 不是：

- 只面向配电报告的生成工具；
- 预置固定领域身份的 Agent 框架；
- 只支持 DAG 的批处理器；
- 让模型通过自然语言任意修改工作流状态的自由协作系统；
- 只封装模型 API 的聊天应用；
- 依靠大量重复 Manifest 代替清晰状态和日志的审计系统。

## 11. 对外描述

### 中文

> Manyselves 是一个面向复杂专业任务的、定义驱动且可恢复的多 Agent 应用运行时。它通过业务无关的 Action/Executor 内核，将外部定义的 Agent、工具、Schema、状态、条件、循环、门禁、恢复和交互编译为可执行工作流，并通过 FastAPI、React 和服务器部署封装形成完整应用。当前配电安全报告是第一个复杂参考能力包，用于验证这一运行模式，而不是限制系统只能服务于报告场景。

### English

> Manyselves is a definition-driven, recoverable multi-agent application runtime for complex professional workflows. Its business-agnostic Action/Executor kernel compiles externally defined agents, tools, schemas, state, control flow, gates, recovery policies, and interactions into executable workflows, then exposes them through a FastAPI and React application shell. The bundled power-distribution reporting system is the first complex reference capability, not the boundary of the platform.

## 12. 文档优先级

当 README、旧方案文档、历史交接文档或代码注释与本文件冲突时：

1. 本文件决定目标定位和通用化边界；
2. 现有代码和测试决定当前已实现行为；
3. 迁移计划必须明确区分当前事实、目标设计和待验证假设；
4. 在行为等价验证完成前，不得以目标定位宣称当前代码已完全通用化。
