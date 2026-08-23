# Final Architecture Consolidation Status

> Manyselves 最终定义驱动架构的唯一跨会话实施状态
>
> Runtime State、Provider Trace、Conversation、Artifact 和 Event Log 不属于本文件
>
> 状态：**重新开放，尚未完成**

## Program

- Repository: `csxq0605/manyselves`
- Plan base: `agent/declarative-runtime-plan`
- Implementation branch: `agent/declarative-runtime-implementation`
- Draft PR: <https://github.com/csxq0605/manyselves/pull/3>
- Governing architecture: [`../architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md`](../architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)
- Execution protocol: [`../CODEX_AUTONOMOUS_EXECUTION.md`](../CODEX_AUTONOMOUS_EXECUTION.md)

## Current position

- Current FA work package: `FA-02 — 抽取通用 Agent/Conversation/Recovery Runtime`
- Current slice: `让 AgentExecutionService 继续直接拥有 continuation/correction/no-progress/result-reuse 调度；Capability 只提供窄领域策略`
- Current branch at slice start: `agent/declarative-runtime-implementation`
- HEAD at slice start: `bceba87 Capability: own module lane runtime models`
- Program status: `in progress`
- Final real-test status: `not started for the final architecture`
- Blockers: `none known`
- Next automatic action: `先固定 Runtime-owned recovery loop Characterization，再把 Reporting runner 内的 continuation/correction/no-progress/completed-result 分支收敛为 Capability strategy + Generic Runtime 调度`

## Why the prior completion claim is reopened

原 `WP-00`～`WP-12` 和后续审计建立了真实可用的 Definition Loader/Registry、Workflow Compiler、ResolvedPlan、Stateless Kernel、WorkflowRuntimeHost、Capability definitions、Generic Run API/React，以及声明式 Reporting 的大量文件控制流。

但是，历史“完成”结论采用了现在已经废止的兼容目标：

- Legacy Reporting Runner 保持默认；
- 声明式 Reporting Runner 可以继承旧 `ReportWorkflowRunner`；
- 通用 Workflow Projection 可以把 ReportingFacade 作为 Capability Host；
- `manyselves/core/reporting` 可以继续混合通用 Agent 机制、Reporting 领域逻辑和旧流程；
- 旧 API/旧 Run/双路径行为等价被视为最终完成条件。

用户已经明确：不需要继续以旧流程兼容为目标。最终项目必须形成一个 Stateless Kernel、一个 Generic Compiler/Runtime、Capability-owned Domain Runtime/Tools、文件定义系统和 Generic FastAPI/React。因此旧的兼容式真实 Run 成功不能证明新的最终架构完成。

## Confirmed implemented foundation

以下基础已有生产代码和 focused/affected 测试证据，但仍需在后续拆分中保持：

| Foundation | Current evidence | Completion meaning |
| --- | --- | --- |
| Definition files | Capability/Agent/Task/Tool/Contract/Workflow/Recovery 可从包加载 | 基础已实现，不证明领域实现已正确归属 |
| Registry/Catalog | 两个 Capability 可发现 | 基础已实现，不证明 Application 已与 ReportingFacade 解耦 |
| Compiler | 父子 Workflow、定义快照和完整 Plan 可冻结/恢复 | 核心基础已实现 |
| Stateless Kernel | State/Event 转换和 Host 调度已存在 | Kernel 中立性仍需持续边界审计 |
| Generic Runtime Host | Tool/Agent/Conversation/Subworkflow/Parallel/WAITING 可执行 | Agent/Recovery 生产实现仍部分借用 Reporting 代码 |
| Reporting definitions | Module/Cross/Chief/Final/Delivery 大量流程已文件化 | 不等于声明式 Runner 已脱离 Legacy 父类 |
| Neutral Capability | `parameter-adjustment` 通过同一 Compiler/Host | 证明 Kernel 可中立使用 |
| Generic API/UI | Schema 表单、WAITING、Events、Outputs、Cost 已有通用投影 | 仍需移除 Application 的 Reporting host 耦合并重新审计 |

## Current design-to-production differences

此表是当前实施的权威差异列表。每个切片完成后必须用实时源码更新。

| Final design requirement | Current production evidence at FA-00 start | Difference | Planned correction |
| --- | --- | --- | --- |
| Declarative Reporting 不依赖 Legacy Runner | `DeclarativeReportWorkflowRunner` 仍继承 `ReportWorkflowRunner` | 最终路径仍借用旧流程宿主和领域服务集合 | FA-02/FA-03 提取通用 Runtime 与 Reporting Domain Runtime，改为组合 |
| Generic Application 不认识 Reporting service | Workflow routes/binding 构造仍把 ReportingFacade/host 传入 Capability factory | 通用应用依赖具体 Capability | FA-04 建立通用 Capability Runtime Binding/Services |
| Reporting Python 归 Capability 所有 | Module Lane/Cohort 的 6 个 Contract 类型已真正迁入 Capability；其余领域模型、Agent runner、review/render/delivery 仍在 `manyselves/core/reporting` | 物理和语义归属仍部分混合 | FA-03/FA-05 继续分类迁移 |
| Generic Agent/Recovery 不依赖 Reporting | `AgentRecoveryDriver` 已在 Runtime；`AgentExecutionService` 已直接拥有 AgentLoop 创建/恢复/启停、Conversation session 复用、单轮 publish/wait 和真实 session ID | continuation/correction/no-progress/completed-result 策略与 typed-result 解码仍由 Reporting runner 主调 | FA-02 继续提取，Reporting 仅提供领域 Prompt/Tools/结果绑定 |
| 文件 Workflow 是唯一流程所有者 | 文件流程已细化，但父 Runner/service 仍可拥有整流程入口 | 生产图仍有第二流程宿主 | FA-03 删除继承和整流程控制入口 |
| 单一生产入口 | Legacy/declarative engine selection 仍存在，Legacy 默认 | 仍是双路径产品 | FA-04/FA-05 移除旧 selector/default/entry |
| 旧兼容代码不在发布图 | Sequential/ControlFlow old executor、legacy adapter、Reporting facade/runner 债务仍存在 | 无生产用途和旧产品入口尚未系统删除 | FA-05 按引用与行为测试删除 |
| 文档直面最终形态 | 六份权威规范/状态已在 FA-00 改写为最终形态 | 当前无已知规范差异；后续持续与代码同步 | 每个切片更新本表 |

## FA work package progress

| Work package | Status | Required evidence |
| --- | --- | --- |
| FA-00 Facts and final specification | `completed` | 六份规范一致；三项生产耦合审计；完成矩阵；diff-check |
| FA-01 Architecture boundary characterization | `completed` | 5 项生产边界 Characterization 已取得真实 RED，并以 strict xfail 保持可逐项收敛；Capability 顶层导入纯度已转绿 |
| FA-02 Generic Agent/Conversation/Recovery Runtime | `in progress` | 中立 Agent/Tool/Recovery 行为与 Reporting 受影响测试 |
| FA-03 Distribution Reporting Domain Runtime | `pending` | 无 Legacy 继承/委托；文件 Workflow 全链执行 |
| FA-04 Generic Capability Runtime Binding | `pending` | 两个 Capability 同一 start/query/input/output/events/cost 接口 |
| FA-05 Ownership convergence and legacy deletion | `pending` | 生产/发布图无旧 Runner/Facade/selector/adapter；领域代码归 Capability |
| FA-06 Generic FastAPI/React surface | `pending` | Schema/WAITING/Output/Event/Cost，无 Capability-ID 流程分支 |
| FA-07 Completion audit and final real test | `pending` | focused/affected checks、build、发布物审计、一次真实测试 |

## Historical evidence retained

历史实现和真实测试仍有价值，但只作为迁移基线：

- 原 `WP-00`～`WP-12` 已实现 definitions/compiler/kernel/runtime/capability/API/UI 的大量基础；
- 2026-08-20 的 Microsoft Agent Framework/LangGraph 隔离 POC 决定为 `reference`；
- focused selections 曾覆盖 Runtime/Compiler/Reporting/API/Frontend；根据用户约束没有执行全量回归；
- `report-declarative-33ed40175b` 曾在真实 Provider、真实项目和浏览器中完成并交付，证明旧兼容式声明路径的业务能力；
- 该历史 Run 不证明移除 Legacy 宿主、ReportingFacade 和 `core/reporting` 混合边界后的最终路径。

详细历史提交、测试数字和旧发现保留在 Git history 与 Draft PR；不再把数百行历史流水复制进当前状态文件。

## Current slice evidence

- FA-00 三项只读审计确认生产链仍为 `Generic /runs → Distribution binding → ReportingFacade → ReportingRunController → ReportingService → DeclarativeReportWorkflowRunner(ReportWorkflowRunner)`；82 个 Capability Contract 中有 41 个 model 路径仍指向 `manyselves.core.reporting.*`。
- FA-00 将 `AGENTS.md`、产品定位、主架构方案、自主执行协议、官方研究和本状态文件统一改写为最终定义驱动形态；`git diff --check` 通过。
- FA-01 新增 `tests/architecture/test_final_runtime_boundaries.py`。实现前 5 项均真实失败，分别覆盖 generic layer import、Binding host、Contract ownership、Runner inheritance 和五个文件入口；当前以 strict xfail 记录尚未修复的生产差异，任何完整修复都会先产生 XPASS，必须同步移除对应标记。
- FA-01 顶层 Capability 导入纯度 Characterization 先失败，随后移除 `distribution_reporting.__init__` 对 compatibility adapters 的 eager import；focused Capability tests `25 passed`，架构边界选择 `5 xfailed`，定向 Ruff 与 `git diff --check` 通过。
- FA-02 `AgentRecoveryDriver` Characterization 先因模块不存在而失败；实现后 `ReportingAgentRunner` 生产路径通过中立 Driver 执行 Recovery event、progress observation、Provider decision 和 attempt snapshot/restore。Runtime/Kernel/Agent Adapter focused 选择 `23 passed`，Reporting recovery 受影响选择 `11 passed, 91 deselected`。
- Generic `InvokeAgentExecutor` 现在从 `AgentInvocationOutcome.session_id` 回写并持久化 `ConversationRecord.external_session_id`，不再要求 Capability Invoker 私下修改 Conversation；新的 outcome-only Invoker Characterization 先失败后转绿。
- 上述 FA-02 切片的定向 Ruff、compileall、5 项架构 strict xfail 和 `git diff --check` 通过；新增代码扫描未发现 Gate、Hash、CAS 或锁逻辑。
- FA-03/FA-05 的首个物理所有权切片将 5 个 Module Lane Contract 类型真正迁到 `capabilities/distribution_reporting/runtime/models/module_lane.py`，删除旧 `core.reporting` 模块且不留 re-export；Capability definition/cohort/direct restore 选择 `30 passed`，旧 Python 路径零引用。41 个旧 Contract model 路径因此减少为 36 个。
- Module Lane 所有权切片的定向 Ruff、compileall、5 项架构 strict xfail 和 `git diff --check` 通过；新增代码扫描未发现 Gate、Hash、CAS 或锁逻辑。
- 紧随的 Module Cohort 所有权切片将 `DeclarativeModuleLaneOutcome` 真正迁入 `capabilities/distribution_reporting/runtime/models/module_cohort.py`，旧 Core 模块不再定义、别名或重导出该类；Contract 动态解析、Join/恢复字段 round-trip 和 extra-forbid Characterization 已覆盖。Definition package `27 passed`、Cohort `3 passed`、Runner affected `6 passed`；Ruff、compileall、旧路径/类扫描和 `git diff --check` 通过。旧 Contract model 路径进一步减少为 35 个。
- FA-02 新增 `runtime/agent_execution.py`：服务直接管理 `(workflow_id, conversation_key)` session registry、AgentLoop 创建/恢复/启动/复用、单轮消息发布与 terminal 等待、turn completion 和 workflow close；该模块只依赖中立 `AgentSessionLoop`/`AgentMessageBus` 结构协议，不导入历史 `core` 具体类。Reporting 生产路径已改用 `start_or_restore`、`dispatch_turn`、`wait_until_turn_complete` 和 `close_workflow`，不再直接 `loop.start/stop`、`bus.wait_for` 或构造 `UserMessage`。
- AgentExecutionService Characterization 先因模块不存在取得 RED；Runtime execution/adapter/recovery `13 passed`，Reporting stable-session/persisted-recovery/auditor-isolation/max-token/tool-slice/no-progress/correction `8 passed`，声明式 recovery policy 选择 `5 passed`。定向 Ruff、compileall、5 项架构 strict xfail 和 `git diff --check` 通过。新服务未新增 Gate、Hash、CAS、锁或生产依赖；Reporting 原有 correlation/hash 仍原地保留且未复制。
- 本阶段没有运行全量回归，没有调用 Provider/浏览器/服务器，没有新增 Gate、Hash、CAS、锁、校验链或生产依赖。

## Research decisions

- Microsoft Agent Framework: `reference`；官方当前 Declarative Workflow 证明 YAML→Executable Graph、Function Tool、HITL、Checkpoint/Subworkflow 可行。
- LangGraph: `reference`；官方当前 Graph API 证明 State/Node/Edge、compile、parallel/subgraph/persistence 可行，并明确 Workflow 与 Agent 的职责差异。
- Production runtime: 保持 Manyselves 内部轻量 Loader/Compiler/Kernel/Host；不新增外部编排依赖。
- Capability architecture: 每个 Capability 有 Domain Runtime/Tools，不复制 Kernel。
- Legacy compatibility: 已废止为最终要求；不新增 Adapter/Feature Flag 延长双路径。

完整来源和 POC 解释见 [`../research/DECLARATIVE_RUNTIME_LANDSCAPE.md`](../research/DECLARATIVE_RUNTIME_LANDSCAPE.md)。

## Active user constraints

- 默认只运行 focused tests 和受影响测试，不做全量回归，除非用户明确要求；
- Characterization First；
- Kernel 业务无关；
- 不把 Reporting 角色或流程变成 Kernel Action；
- 不新增生产编排框架；
- 默认禁止新增不必要的 Gate、判断门禁、Hash、CAS、锁和额外校验链；确有需要必须先解释并获得批准；
- 保留结构化纠正、Schema 原 Conversation 修正、Max Token、Tool Slice、No-progress、Tool Result 复用、Conversation/Session 复用和 Same-run 恢复；
- 中途不设置人工验收断点，自动推进到最终架构完成；
- 最后只进行一次真实 Provider/项目/浏览器测试；真实长任务不持续高频监控，失败保留同一 Run 和现场。

## Verification policy

每个切片记录实际命令。默认：

```bash
uv run ruff check <changed-python-and-test-paths>
uv run pytest -q <focused-and-affected-tests> --maxfail=3
git diff --check
```

前端只运行 focused Vitest、定向 ESLint、TypeScript 或受影响 build。任何窄选择都不得被描述为全仓库回归。

## Completion evidence matrix

最终完成前必须逐项填入当前证据：

| Requirement | Evidence status |
| --- | --- |
| Stateless Kernel business-neutral | `foundation present; final audit pending` |
| File definitions → complete ResolvedPlan | `foundation present; post-refactor audit pending` |
| One Generic Runtime Host | `foundation present; legacy host removal pending` |
| Generic Agent/Tool/Conversation/Recovery | `partial; session/turn lifecycle extracted, recovery-loop strategy pending` |
| Capability-owned Reporting Domain Runtime | `not achieved` |
| No Declarative→Legacy Runner inheritance/delegation | `not achieved` |
| Generic Capability Binding/Application | `partial; Reporting host removal pending` |
| Generic FastAPI/React for two capabilities | `foundation present; final neutrality audit pending` |
| Legacy Runner/Facade/selectors absent from production/release | `not achieved` |
| Recovery/Same-run behaviors on final path | `not yet reverified after final refactor` |
| Focused/affected checks and builds | `pending for final refactor` |
| Final real Provider/project/browser test | `not started` |
| Docs/code/tests/release consistent | `not achieved` |

只要一项仍为 partial、pending、missing 或 indirect，就不得宣称项目完成。

## Next automatic sequence

1. 执行 FA-02 通用 Runtime 提取；
2. 执行 FA-03 Reporting Domain Runtime 解耦；
3. 执行 FA-04 通用 Binding；
4. 执行 FA-05 旧路径删除和物理归属收敛；
5. 执行 FA-06 产品表面复审；
6. 执行 FA-07 自动完成审计；
7. 只在全部自动证据通过后进行最终一次真实测试。

## Resume instruction

新会话执行：

```text
Run git status -sb, git branch --show-current, git log -1 --oneline.
Read AGENTS.md and this status file completely.
Continue from Current FA work package / Next automatic sequence.
Do not restore Legacy compatibility as a target and do not ask between slices.
```
