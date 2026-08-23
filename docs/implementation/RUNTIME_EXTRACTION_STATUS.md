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

- Current FA work package: `FA-03 — 建立 Distribution Reporting Domain Runtime`
- Current slice: `FA-03/M3 Contracts/Inputs 已完成；下一步 M4 迁移 Capability 自有 State/Recovery/Storage/Source Ledger`
- Current branch at slice start: `agent/declarative-runtime-implementation`
- HEAD at slice start: `e023be9 Runtime: own completed agent result reuse`
- Program status: `in progress`
- Final real-test status: `not started for the final architecture`
- Blockers: `none known`
- Next automatic action: `整文件迁移 models.py 到 Capability reporting models，删除无生产引用的 Cross lazy alias，并更新 ReportRequest YAML/所有消费者；不保留 Core shim/re-export`

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
| Reporting Python 归 Capability 所有 | Module Lane/Cohort、Cross Owner、Chief/Final Chapter、Final Review、taxonomy、Reporting 基础模型以及完整 agentic schema registry 已迁入 Capability；包装模型仍引用待迁移的 Core input/runtime primitive，Agent runner、review/render/delivery 也仍在 `manyselves/core/reporting` | 主要领域 Schema 已真实归位，但 input/runtime primitives 与完整领域执行实现尚未归位 | FA-03/FA-05 接下来按无环 DAG 迁移 input/contracts、领域服务与执行实现，而非继续停留在外层 wrapper |
| Generic Agent/Recovery 不依赖 Reporting | `AgentRecoveryDriver` 与 `AgentExecutionService` 已拥有 AgentLoop/session/turn 生命周期，并在生产路径驱动 natural correction、max-token/tool-slice、typed progress→NO_PROGRESS 和 pre-session completed-result reuse；Provider=0、session 未创建、旧 typed result 原样返回 | FA-02 列出的生产主调债务已清完；Reporting 正确保留 durable-progress 算法/阈值、correlation、typed decode、领域 Prompt/结果持久化。测试专用 `runtime/agent_adapter.py` 仍是 FA-05 删除债务 | FA-03 使用该通用服务组合 Capability Domain Runtime；FA-05 删除无生产用途的旧 adapter |
| 文件 Workflow 是唯一流程所有者 | 文件流程已细化，但父 Runner/service 仍可拥有整流程入口 | 生产图仍有第二流程宿主 | FA-03 删除继承和整流程控制入口 |
| 单一生产入口 | Legacy/declarative engine selection 仍存在，Legacy 默认 | 仍是双路径产品 | FA-04/FA-05 移除旧 selector/default/entry |
| 旧兼容代码不在发布图 | Sequential/ControlFlow old executor、legacy adapter、Reporting facade/runner 债务仍存在 | 无生产用途和旧产品入口尚未系统删除 | FA-05 按引用与行为测试删除 |
| 文档直面最终形态 | 六份权威规范/状态已在 FA-00 改写为最终形态 | 当前无已知规范差异；后续持续与代码同步 | 每个切片更新本表 |

## FA work package progress

| Work package | Status | Required evidence |
| --- | --- | --- |
| FA-00 Facts and final specification | `completed` | 六份规范一致；三项生产耦合审计；完成矩阵；diff-check |
| FA-01 Architecture boundary characterization | `completed` | 5 项生产边界 Characterization 已取得真实 RED，并以 strict xfail 保持可逐项收敛；Capability 顶层导入纯度已转绿 |
| FA-02 Generic Agent/Conversation/Recovery Runtime | `completed` | Session/turn、定义 Tool/Agent、correction/continuation/no-progress、pre-session result reuse 均由中立 Runtime 生产主调；focused/affected 与生产 spy 证据 |
| FA-03 Distribution Reporting Domain Runtime | `in progress` | 无 Legacy 继承/委托；文件 Workflow 全链执行 |
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
- Cross Owner 所有权切片将 `DeclarativeCrossOwnerPipelineOutcome` 和 `DeclarativeMainExceptionUserInput` 真正迁入 `capabilities/distribution_reporting/runtime/models/cross_owner.py`，旧 Core 不保留定义、alias 或 re-export；Contract 路径、`__module__`、round-trip 和 extra-forbid 的 3 项 Characterization 实现前 RED、实现后转绿。旧 Contract model 路径从 35 个减少为 33 个。
- 该 Cross affected 选择暴露了一个真实恢复偏差：全局 `module_review_completion_refs` 同时被当作 initial baseline 和 Cross lifecycle completion，导致重试读取错误 subject。修复后 `_restore_completed_module_review` 只按当前 `review_root + revision` 读取 canonical `completion-rN.json`，pipeline 未 Reduce 前不改写全局 baseline，最终 Reduce 再发布 r1；严格 subject 绑定未放宽，也未增加 fallback、Gate、Hash、CAS 或锁。Definition package `30 passed`，Cross concurrency 受影响文件 `22 passed`，精确恢复 `2 passed`，Cohort + Runner resume `2 passed`；Ruff、compileall、路径扫描和 `git diff --check` 通过。
- Chief Chapter 所有权切片将 `DeclarativeChiefChapterAgentResult`、`DeclarativeChiefChapterContext`、`DeclarativeChiefChapterOutcome` 真正迁入 `capabilities/distribution_reporting/runtime/models/chief_chapter.py`，旧 Core 不保留定义、alias 或 re-export。所有权/旧 Core 暴露的 3 项 Characterization 实现前 RED、实现后转绿；Definition package、Chief cohort 与 Runner affected 合计 `36 passed`，Ruff、compileall、旧路径扫描和 `git diff --check` 通过。直接指向 Core Reporting 的 Contract model 路径从 33 个减少为 30 个；新模型仍依赖待后续迁移的 `ChiefChapterLaneInput`、`ChiefChapterLaneSubmission` 和 `TaskEnvelope`，因此不把本切片误报为 Capability 边界完成。
- Final Chapter 所有权切片将 `DeclarativeFinalChapterAgentResult`、`DeclarativeFinalChapterContext`、`DeclarativeFinalChapterOutcome` 真正迁入 `capabilities/distribution_reporting/runtime/models/final_chapter.py`，旧 Core 不保留定义、alias 或 re-export；Final review cycle 也直接消费 Capability Outcome。3 项 Characterization 实现前 RED、实现后转绿；Definition package、Final cohort/nested recovery 与 Runner affected 合计 `45 passed`，主工作区复核选择 `5 passed`，Ruff、compileall、旧路径扫描、5 项架构 strict xfail 和 `git diff --check` 通过。直接 Core Contract 路径从 30 个降至 27 个；`FinalChapterLaneFindingSubmission`、`TaskEnvelope` 和 `FinalChapterLaneInput` 的传递依赖仍待后续领域基础模型迁移。
- Final Review 所有权切片按最小无环连通组件迁移 7 个 YAML 直引模型和其嵌套的 `DeclarativeFinalVerdictRecord`，共 8 个类型进入 `capabilities/distribution_reporting/runtime/models/final_review.py`；只迁 7 个会形成 Capability→Core→Capability 循环，因此没有采用。旧 Core 不保留定义、alias 或 re-export，流程函数仍原地且语义未变。3 项 Characterization 实现前 RED、实现后转绿；Definition package、Final cycle/nested recovery 与 Runner affected `48 passed`，主工作区复核选择 `5 passed`，Ruff、compileall、旧路径扫描、5 项架构 strict xfail 和 `git diff --check` 通过。直接 Core Contract 路径从 27 个降至 20 个；8 个模型仍引用待迁移的 agentic/input 基础类型，因此不声称完整边界完成。
- FA-03/M0 将完整的 run-scoped Reporting taxonomy 从 `core/reporting/taxonomy.py` 真实迁到 `capabilities/distribution_reporting/domain/taxonomy.py`，删除旧文件且不留 alias、re-export 或 shim；48 个生产/测试消费者统一改用 Capability 路径。独立子进程 import Characterization 实现前因新模块缺失取得 `ModuleNotFoundError` RED，迁移后证明不会加载 `manyselves.core.reporting`；旧路径引用为 0。Agent focused `37 passed`，主工作区 taxonomy/models/ownership 选择 `18 passed`，新模块/ownership Ruff、全部消费者 import-order Ruff、compileall、5 项架构 strict xfail 和 `git diff --check` 通过。`hashlib` 仅是旧 workbook snapshot 行为原样搬迁，没有新增或扩展 Hash/CAS 逻辑。
- FA-03/M1 将完整 Reporting 基础模型组件从 `core/reporting/models.py` 真实迁到 `capabilities/distribution_reporting/runtime/models/reporting.py`，包括 19 个模型/枚举、15 个常量/类型别名、`chapter_section_ids` 与 `ReportRequest`；旧文件、Core re-export、无消费者的 Cross lazy alias 均删除，不保留 shim。`distribution_reporting_input.yaml` 及 77 个生产/测试直接消费者改用 Capability 路径；Capability 树内直接 Core Reporting 路径由当前 HEAD 的 23 个降到 20 个。两项物理归属/独立导入 Characterization 实现前 `2 failed`、实现后转绿；Agent affected 选择累计 `216 passed`，主工作区模型/taxonomy/定义投影/Binding 复核 `20 passed`，定向 Ruff、全部改动 import-order Ruff、compileall、旧路径扫描、5 项架构 strict xfail 和 `git diff --check` 通过。原 validator/schema/default/SHA 字段保持不变，未新增或扩大 Hash、CAS、锁、Gate 或验证逻辑。
- FA-03/M2 将 52 个类、3 个函数和 19 项 `SUBMISSION_INPUT_TYPES` 组成的完整 agentic schema registry 从 `core/reporting/agentic_models.py` 真实迁到 `capabilities/distribution_reporting/runtime/models/agentic.py`；旧文件删除且不留 alias、re-export 或 shim。42 个生产消费者、35 个测试消费者和 10 个 YAML 动态模型路径已更新；首次迁移后的 ownership RED 暴露审计漏记的 3 个 `core/tools` 消费者，扩大扫描补齐后旧路径归零。新组件只依赖 sibling Reporting 模型、Capability taxonomy、yaml/Pydantic，不导入 Core Reporting；Capability 树内直接 Core Reporting 路径由 20 个降到 14 个，其中 YAML Contract 只剩 10 个。Characterization 从独立导入 `1 failed`、首次 ownership `1 failed` 转为 `3 passed`；Agent affected 五组累计 `216 passed`，主工作区 ownership/agentic/definition 复核 `62 passed`，定向 Ruff、全部改动 import-order Ruff、compileall、AST 逻辑等价、旧路径扫描、5 项架构 strict xfail 和 `git diff --check` 通过。所有 schema/validator/default 逻辑保持不变，未新增或扩大 Hash、CAS、锁、Gate 或验证逻辑。
- FA-03 实时生产链复审确认：Generic `/runs` 虽先选择 definition-owned Binding，但 Distribution binding 仍调用 `ReportingFacade.start_declarative → ReportingRunController → ReportingService._execute_locked → DeclarativeReportWorkflowRunner(ReportWorkflowRunner).run`；父 `run()` 做完 prepare/dispatch 后才通过子类 `_run_module_lanes()` 把 `WorkflowRuntimeHost` 嵌入中段，Host 返回后再回到旧 `run()`。因此当前不是“一个顶层 Host”，文件 Workflow 也不是唯一控制流。render/distill/aggregate 仍完全绕过 Host，Capability 也只声明一个 `distribution-reporting` entrypoint。FA-03 顺序据此修正为 M3 Contracts/Inputs、M4 Capability State/Recovery、M5 Review models/Cross contracts、M6 Preparation/Deterministic domain、M7 Rendering/Materialized Delivery、M8 Review Action Runtime 与五个文件 entrypoint；不得把 13 模块 Legacy SCC 整体换目录冒充解耦。
- FA-03/M3 将 `submission_contracts.py` 和 `input_contracts.py` 作为单向无环组件分别迁到 Capability `runtime/contracts/submissions.py` 与 `runtime/models/inputs.py`，旧文件删除且不留 alias、re-export 或 shim；Submissions 的 4 个生产/2 个测试消费者、Inputs 的 13 个生产/19 个测试消费者以及 5 个 YAML 动态模型路径全部更新。独立导入 Characterization 实现前 `1 failed`，迁移后 ownership/schema/example/field-guidance/动态 Contract `4 passed`；Agent affected 五组累计 `197 passed`，主工作区 ownership/definition/agentic/chapter contract 复核 `71 passed`，定向 Ruff、全部改动 import-order Ruff、compileall、归一化 AST 语义等价、旧路径扫描、5 项架构 strict xfail 和 `git diff --check` 通过。仅删除两个被 Python 后值覆盖的重复死键和两个未使用 import，未改变 validator/default/判断语义，也未新增 Hash、CAS、锁、Gate 或依赖。Capability 对 Core Reporting 的剩余直接耦合已缩为 `module_lane.py` 的 state/review types、4 个 Cross YAML 和待删 compatibility adapters。
- FA-02 新增 `runtime/agent_execution.py`：服务直接管理 `(workflow_id, conversation_key)` session registry、AgentLoop 创建/恢复/启动/复用、单轮消息发布与 terminal 等待、turn completion 和 workflow close；该模块只依赖中立 `AgentSessionLoop`/`AgentMessageBus` 结构协议，不导入历史 `core` 具体类。Reporting 生产路径已改用 `start_or_restore`、`dispatch_turn`、`wait_until_turn_complete` 和 `close_workflow`，不再直接 `loop.start/stop`、`bus.wait_for` 或构造 `UserMessage`。
- AgentExecutionService Characterization 先因模块不存在取得 RED；Runtime execution/adapter/recovery `13 passed`，Reporting stable-session/persisted-recovery/auditor-isolation/max-token/tool-slice/no-progress/correction `8 passed`，声明式 recovery policy 选择 `5 passed`。定向 Ruff、compileall、5 项架构 strict xfail 和 `git diff --check` 通过。新服务未新增 Gate、Hash、CAS、锁或生产依赖；Reporting 原有 correlation/hash 仍原地保留且未复制。
- FA-02 恢复循环继续收敛：Runtime 用 `AgentRecoveryCompleted/Required/Stopped` 分型观察直接分派 RecoveryController 的动作，Capability 端口统一为 async 的 terminal 解释、领域消息构造、stop 持久化与 result reuse；移除了可冲突 boolean/optional 组合、sync/async 双态、`None` 拒绝语义和 action compatibility 判断门禁。Reporting 的真实 natural-language-without-submission 现在通过 `execute_with_recovery` 在原 session 执行 CORRECT/STOP/FAIL，Reporting 仅构造 correction Prompt、解码 typed result 和保留既有结果持久化。
- 上述恢复循环切片的 Runtime/Recovery/Reporting focused 选择 `22 passed`，定向 Ruff、compileall 和 `git diff --check` 通过。未新增 Hash、CAS、锁、Gate、attempt limit 或依赖；唯一异常分支是执行已声明 `FAIL` action 的必要语义。
- FA-02 continuation 生产切片把 max-token 与 tool-slice sentinel 转为 `AgentRecoveryRequired` 并交给 `AgentExecutionService.execute_with_recovery`；Runtime 现在执行 Controller 的 CONTINUE/STOP/FAIL 并通过同一个 `execution_session` dispatch 下一轮，Reporting 只保留领域 continuation Prompt、typed result 解码、已有进度快照与 continuation state 持久化。两项生产 spy Characterization 实现前 `2 failed`、实现后转绿；Agent/Runtime/Reporting focused `23 passed`，主工作区复核选择 `12 passed`，Ruff、compileall、5 项架构 strict xfail 和 `git diff --check` 通过。未复制或新增 Hash、CAS、lease、锁、Gate、attempt limit 或依赖；typed no-progress 与 completed-result reuse 仍明确为待迁移。
- FA-02 typed progress/no-progress 切片新增不可歧义的 `AgentRecoveryProgress(progressed|no_progress)`，Runtime 负责调用 `observe_progress`、形成 `NO_PROGRESS` directive 并执行 CONTINUE/STOP/FAIL；Reporting 删除私有 action 决策/兼容校验，只提供既有 durable/conversation snapshot、既有 no-progress 阈值、状态持久化和领域 stop result。生产观察 Characterization 实现前 `2 failed`、实现后转绿；Agent/Runtime/Reporting focused `25 passed`，主工作区复核 `13 passed`，Ruff、compileall、5 项架构 strict xfail 和 `git diff --check` 通过。Runtime 未复制 Reporting 的 hash snapshot 或阈值，未新增 Gate、Hash、CAS、lease、锁、attempt limit 或依赖；completed-result reuse 仍为下一项债务。
- FA-02 最终 completed-result 切片新增 pre-session `AgentExecutionService.recover_completed_result`，在 Reporting 已完成既有 correlation/terminal/identity 验证和 typed decode 后，由 Runtime 主调 `COMPLETED_TOOL_RESULT` 的默认/声明式 REUSE_RESULT、STOP、FAIL。生产 spy Characterization 实现前 `1 failed`，实现后 Provider 调用仍为 `0`、Runtime session registry 为空、原 session identity 和旧 typed result 原样复用。Runtime/Reporting affected `26 passed`，主工作区复核 `15 passed`，Ruff、compileall、5 项架构 strict xfail 和 `git diff --check` 通过；未复制或新增 Hash、CAS、lease、锁、Gate、attempt limit、action compatibility 校验或依赖。至此 FA-02 规范列出的生产 Agent/Conversation/Recovery 主调债务清完，测试专用 Legacy adapter 留到 FA-05 删除。
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
| Generic Agent/Tool/Conversation/Recovery | `achieved for the production execution path; final legacy-adapter removal audit remains in FA-05` |
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

1. 执行 FA-03 Reporting Domain Runtime 解耦；
2. 执行 FA-04 通用 Binding；
3. 执行 FA-05 旧路径删除和物理归属收敛；
4. 执行 FA-06 产品表面复审；
5. 执行 FA-07 自动完成审计；
7. 只在全部自动证据通过后进行最终一次真实测试。

## Resume instruction

新会话执行：

```text
Run git status -sb, git branch --show-current, git log -1 --oneline.
Read AGENTS.md and this status file completely.
Continue from Current FA work package / Next automatic sequence.
Do not restore Legacy compatibility as a target and do not ask between slices.
```
