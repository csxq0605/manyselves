# Manyselves 输入输出成本、并行调度、加速与 Agent 编排计划 V2

> 2026-08-11 架构更新：成本控制重新成为 Agent 优化的硬约束。37 个固定叶子仍是
> 独立逻辑 task、artifact 和 completion，但不再等同于 37 个独立 Agent dispatch/session。
> Wave 1 采用模块共享上下文 discovery batch，Wave 2 采用模块共享 inbox batch，Wave 3
> 采用一次模块 authoring 会话并通过 `write_result_part` 逐叶落盘。默认完整路径的三波
> 独立 Agent dispatch 上界由 `37 + 非空叶子 inbox + 37` 收敛为
> `5 + 非空目标模块 + 5`。每个 dispatch 内的检索/工具/提交 Provider turns 仍由
> UsageLedger 按实际请求计数，不把 dispatch 数伪装成网络调用数。

## 1. 当前基线与规划边界

- 当前实验分支：`cost-control-experiments`。
- main 同步基线：`main@3babc0ca079fe15337b09f0ec4911bf638c39ae5`。
- 主要功能同步提交：`4a1f375`，父提交为原实验计划 `9d1dfcb` 与
  main `0a5093e`；后续版本同步提交：`a0df065`，父提交为 `7ae6954` 与
  main `a84d409`。
- `main` 已是当前实验分支祖先；本计划不再安排一次重复同步。
- 用户指定的主要成本问题依据是 session `019fa740-4976-7232-b9e9-17a9959a2d5e` 的存储与内容重复审计。
- session `019fa2be-22cc-74a0-a4f6-22a210e3a674` 只作为三波协作和安全并行的补充设计依据，不能代替前者。
- `5f5a5e3` 已经实现输入合同精简、结果分段、CAS、审查 preflight、delta recheck、阶段成本边界和三波协作；本计划不把这些能力重新列为“尚未实现”。
- main 的 evidence/photo traceability、默认 `draft`、decision reconciliation 和 MessageBus 日志汇总已经进入当前分支。
- 当前 V2 工作树以显式 cost-worktree `PYTHONPATH` 和
  `QT_QPA_PLATFORM=offscreen` 运行全量非集成回归，结果为
  `1526 passed, 6 deselected`；`compileall` 与 `git diff --check` 通过。
- 尚无当前 V2 基线的真实 Provider 完整报告、真实 Token/金额对比、DOCX 目视检查和匹配 receipt，因此本文中的降本、加速数字都是验收目标，不是已实现结果。

本文件取代 `experimental-parallel-agent-deployment-plan.md` 作为后续实施顺序；旧文件保留为合并 main 之前的设计记录。

## 2. 已完成优化与剩余问题

| 领域 | 当前已经实现 | 仍需解决 |
| --- | --- | --- |
| Provider 输入 | Prompt 不再重复内嵌完整 submission schema/example；任务专属工具 schema；已落盘长正文以 ref/hash 代替 | Cross/Chief/Final 仍可能获得过大的完整合同；每轮历史和工具结果仍会重发；缺少 task context budget |
| Provider 输出 | 同一模块会话多次 `write_result_part` 逐叶落盘；正文与小型 typed commit 分离；revision/Chief 工具收窄 | submit-only 仍可能先输出普通文字再付费纠正；缺少 forced tool choice、按 task 的输出/轮次档位 |
| 审查成本 | 确定性 preflight；module delta recheck；Chief completion 可恢复 | 五个 module review 仍串行；Cross owner 回改仍串行；语义 lifecycle 缺少 round/stagnation gate |
| 协作 | Wave 1、Barrier 1、稀疏 Wave 2、Barrier 2、Wave 3 并行写作已实现 | author 完成后不能立即进入本模块审查；没有受限完整 module lane |
| 存储 | CAS、Delivery v2、ReportVersion v2、停止新增 legacy 双写、retention dry-run | CAS 命中前仍重复读取、复制和哈希；telemetry、SourceLedger 和事件仍有重复 I/O |
| 证据与图片 | run-scoped 图片、稳定 P-*、`source_image_id`、`primary_evidence_id`、E-ID 重映射及来源索引已实现 | per-file 并行后必须保持编号和主绑定确定性；不能按 worker 完成顺序合并 |
| 成本控制 | UsageLedger、实际 Provider payload 指纹、observe/warn/pause-at-boundary、same-run resume | 没有 Provider 并发/RPM/TPM 准入；没有 typed-task/lifecycle 预测性 admission；成本指标尚未覆盖 CPU、I/O、bus 和 CAS 重哈希 |
| Agent 编排 | typed envelope、稳定 reviewer identity、每任务清理 working memory、严格 finding-response-verdict | 同一 identity 没有互斥；AgentDefinition 的 model/effort 未真正路由 Provider；任务拓扑仍隐含在大型 imperative workflow 中 |
| 事件 | main 已把逐 delta DEBUG 日志降到 TRACE，并在 shutdown 汇总 published count | MessageBus 仍是无界单 FIFO、单消费者、串行 callback；stream flood 可拖延 terminal/result |
| 部署 | 项目指南已固定无界面服务端和文件/进度/对话三块界面 | 当前仍是 PyQt 启动、进程内任务、run 锁和共享 Outputs；无持久队列、项目租约或鉴权 |

## 3. main 大改对成本计划的实际影响

### 3.1 默认 `draft` 改变了可比较成本口径

当前代码默认 `missing_evidence_policy="draft"`，只有用户明确要求暂停确认时才使用 `ask`。

这带来两种相反影响：

1. 减少准备阶段的 decision/resume 往返；
2. 缺证项目仍继续五模块、Cross、Chief 和 Final，完整运行成本可能高于旧版 `ask` 的准备期暂停。

因此禁止把旧 `ask` 暂停 run 与新 `draft` completed run 直接比较。请求 policy 与后续 decision action 必须分开记录：

```text
missing_evidence_policy = draft | skip | ask | block
evidence_decision_action = supplement | draft | skip | stop
```

成本比较再分为：

- completed cohort：`draft`、`skip`、`ask→draft/skip`、补资充分后的 `ask→supplement`；
- 非交付控制 cohort：`ask pending`、`block`、`stop`。

completed cohort 固定 operation、target modules、coverage/preparation digest、decision trace 和交付门槛；控制 cohort 只比较到暂停/阻断状态的确定性成本。成本优化不能通过偷偷跳过固定模块或子模块实现。

### 3.2 图片追溯主要增加确定性 I/O，不是模型输出

`PhotoAsset.primary_evidence_id`、图片来源索引和 run-scoped assets 会增加：

- WPS/OOXML 解析；
- 文件哈希与 CAS ingest；
- Evidence/Photo adjacency 构建；
- Markdown/DOCX 来源索引；
- version manifest 字节。

这些应计入 deterministic CPU/I/O 与 storage telemetry，不能错误归因到 Provider output Token。

### 3.3 evidence decision 与 cost boundary 是两套恢复命名空间

必须显式区分：

```text
evidence decision: decision_id + action
cost boundary: run_id + boundary_id
workflow/lane resume: run_id + semantic task/completion key
```

相同 evidence action 可以 reconciliation，冲突 action 必须拒绝；cost resume 只释放一个新的安全成本窗口；lane resume 只复用已验证 completion。三者不得共享模糊的 `resume=True` 语义。

### 3.4 MessageBus 日志降噪不等于并行安全

`manyselves/core/loops/bus.py` 的新改动只减少日志格式化和磁盘输出。当前仍然：

- `asyncio.Queue` 无界；
- 一个消费循环；
- subscriber callback 逐个 `await`；
- `wait_for` 对消息执行 predicate；
- Provider 的 content/thinking delta 可产生数万条 `AgentResponse`。

因此在扩大 Agent 并发前，必须先保护 terminal/result/control 消息不被流式 delta 饿死。

### 3.5 Web MVP 先保留项目隔离 POSIX 卷

main 新项目指南明确：MVP 先以每项目隔离的 POSIX 持久卷复用现有 Path、checkpoint、DOCX 和 version 语义。对象存储与完全无共享文件系统仍应通过端口预留，但不能阻塞第一版 Headless/Web。

## 4. 当前真实瓶颈

### 4.1 输入成本

1. `ReportingAgentRunner.run()` 会把完整 input contract JSON 内联进 task message。
2. 每个 Provider follow-up 都重新发送 working history 与完整当前工具 schema。
3. `Cross`、`Chief` 和 final auditor 存在读取大篇全文或大工具结果的路径。
4. `ReferenceLibrary.search()` 每次 `rglob()` 并重读 Knowledge。
5. `ProjectEvidenceIndex.get/search()` 每次重读和解析 evidence JSONL。
6. context/provider manifest 在 Provider 调用前同步序列化完整 messages/tools，并无锁 read-modify-write 共享 hash index。

### 4.2 输出成本

1. OpenAI-compatible adapter 当前对工具使用 `tool_choice="auto"`；submit-only 任务仍可能产生额外 correction。
2. correction、tool continuation 和 max-token continuation 没有独立 `turn_kind`，难以归因浪费。
3. Agent 的 `maxTokens/maxTurns/effort` 来自角色定义，但没有结合 task kind、phase 和输入规模。
4. `draft` 默认要求保留完整模块范围，不能通过减少章节换取低输出，只能减少重复、纠正和无效文字。

### 4.3 工作流与工具耗时

1. 全五模块当前只并行 Wave 1、Wave 2 和 Wave 3 author。
2. 五个 module review 在 `workflow.py:1018-1062` 串行。
3. Cross owner revision 在 review lifecycle 内串行。
4. proper-subset `module_report` 逐模块执行完整 pipeline，且不进入 Cross。
5. manifest parse、mapper、WPS 图片解析和证据归一化在 async 函数内同步串行。
6. 同一模型轮次的工具调用在 `AgentLoop._handle_tool_calls()` 中逐个执行。

### 4.4 Agent 编排与 Provider

1. `ReportingAgentRunner` 为所有 reporting Agent 复用同一个 `llm_provider`。
2. `AgentDefinition.model/effort` 主要进入配置/Prompt，不能证明实际路由了不同 Provider/model。
3. `_sessions[(workflow_id, identity_key)]` 保存可变 AgentLoop，并在下一 typed task 原地 reset/mutate，没有 identity mutex。
4. `ReportingRunBudget.snapshot()` 明确记录 `limits_enforced=False`；当前 guard 计数但不限制并发、RPM、TPM 或共享 429 cooldown。
5. discovery、response、author、review、revision 和 recheck 没有统一的持久执行档位与 attempt provenance。

### 4.5 系统与存储成本

1. CAS 当前实现物理去重，但 `ingest_file()` 命中前仍全量复制/哈希到 staging。
2. Delivery ingest 后，ReportVersion 可能再次对同一 DOCX、module、index 和图片读取/哈希。
3. SourceLedger 每次 register 都重新加载并整体写回 registry。
4. stream delta、进度、terminal 和 durable domain event 仍共用同一 MessageBus。

## 5. 必须保持的不变量

1. specialist 继续负责本模块专业正文、Claim 和 E-* 绑定。
2. 原 module reviewer 继续拥有 finding 与 verdict；机器 preflight 不代替语义审查。
3. Cross 只在所有目标 module completion 通过 ref/hash/identity 验证后启动。
4. Cross reviewer、Chief、Final auditor 和 Main 各自串行。
5. 同一 identity 活跃 typed task 数不超过 1。
6. 同模块 Wave 1 → Wave 2 → author → revision/recheck 保持依赖顺序。
7. 不恢复 live peer chatter；跨模块协作继续只使用 bounded typed request/response/bundle。
8. 默认 `draft` 时固定模块和子模块不能被跳过，缺证内容必须保留不确定性标记。
9. per-file worker 不分配最终 E-*、P-*、R-* 或 W-*。
10. E-ID 分配与 `PhotoAsset.primary_evidence_id` 重映射由一个 deterministic reducer 原子完成。
11. 成本暂停不截断正在执行的 Provider 回合或 typed submission。
12. completion、checkpoint、decision、artifact 与 receipt 的 hash/identity 链才是完成证据。
13. Main 只处理用户命令、缺资、例外和风险接受，不参与普通 finding 往返。
14. 服务端项目写租约与内部 module lane 并行是两层概念：同一项目只有一个写 operation 可以被 admission 并执行，其内部私有 lanes 共享同一 lease；第二个同项目写任务必须在执行前排队或拒绝，不能先运行再竞争 publish。

## 6. 目标调度与 Agent 拓扑

```text
Main conversation/control plane
  └─ operation + immutable project revision + supplement lineage
       ↓
Coordinator / single-writer reducer
  ├─ deterministic preparation workers
  │    └─ preparation snapshot + evidence/photo binding barrier
  ├─ Wave 1A: 37 logical leaf discoveries / 5 module-shared Agent batches
  │    └─ per-leaf completion → 5 module-local discovery reducers → Barrier 1
  ├─ sparse Wave 2: logical target-leaf inboxes / at most 5 module-shared calls → Barrier 2
  ├─ Wave 3: 37 logical leaf drafts / 5 module-shared authoring sessions
  │    └─ write_result_part per leaf → authoring barrier → ModuleSubmission
  ├─ bounded module lanes
  │    ├─ author 2.1 → preflight → auditor 2.1 → revision/recheck
  │    ├─ author 2.2 → preflight → auditor 2.2 → revision/recheck
  │    ├─ ...
  │    └─ author 2.5 → preflight → auditor 2.5 → revision/recheck
  ├─ Module Barrier
  ├─ Cross initial
  ├─ bounded Cross-owner lanes
  ├─ Cross owner barrier → same Cross reviewer verdict/recheck
  └─ Chief → Final audit/revision → verifier → render → publish

ProviderAdmissionController
  ├─ weighted task admission
  ├─ provider/model concurrency
  ├─ RPM/TPM reservation
  └─ shared retry/cooldown

Event planes
  ├─ durable domain/control/terminal events
  └─ bounded, coalesced transient token/thinking deltas
```

角色档位：

| Task kind | 默认能力 | 输出策略 | 并行边界 |
| --- | --- | --- | --- |
| deterministic preparation/preflight/render | 不调用 LLM | 稳定 artifact/completion | 文件级 worker，reducer 单写 |
| Wave 1A leaf discovery | 紧凑 typed 档 | 模块 batch 内含独立 leaf submissions | 5 个模块可并行；每叶 completion 独立恢复 |
| Wave 2 leaf response | 紧凑 typed 档 | 模块 batch 回答非空 leaf inbox，再按 request_id 拆分 | 最多 5 个目标模块并行；每叶 response 独立恢复 |
| Wave 3 leaf author | 高能力写作档 | 模块会话逐 leaf `write_result_part` + 小 commit | 5 个模块可并行；正文分段按 leaf 恢复 |
| module auditor | 高能力审查档 | finding/verdict | 不同模块 session 可并行，同模块串行 |
| specialist revision | 高能力局部档 | changed sections only | 不同模块可并行 |
| Cross/Chief/Final | 高能力全局档 | index/search-first，必要时全文 | 各角色自身串行 |
| Main | 会话与决策档 | command/decision | 同一会话单 turn；不进入报告 lane |

## 7. 分阶段实施

### M0：post-main 语义冻结与分层基线

1. 固定当前 merge SHA、Prompt/tool schema digest、Provider/model 和 fixture revision。
2. 建立 `policy=draft|skip|ask|block` 与 `decision_action=supplement|draft|skip|stop` 两维 fixture。
3. completed cohort 只纳入通过相同正确性与交付门槛的 run；ask pending/block/stop 作为非交付控制 cohort 单独统计。
4. 分别采集：
   - Provider calls、uncached/cache-read/cache-write/output Token；
   - adapter 后 canonical SDK payload 的 request/message/tool-schema chars；
   - response content、thinking、tool-call arguments 与 tool-result chars；
   - `usage_source=provider|estimated`、`request_metric_source`；
   - accepted/discarded/correction output 与 continuation；
   - preparation/index/render CPU 与 I/O；
   - CAS source/read/rehash/logical/physical bytes；
   - bus queue、event、log bytes；
   - stage、Agent、end-to-end wall time。
   上述 payload chars 是进入 SDK adapter 的规范化字符数，不是 HTTP wire bytes。
5. 基线采集提交不得改变 Provider 调用行为。
6. 同时冻结部署契约：文件/进度/对话三块界面、Main 五种 operation、用户/项目成员边界、事件字段、预计项目/run 并发量；后续 D0-D3 不得自行扩展业务界面。

### P0：terminal identity contract 与 MessageBus QoS

#### P0.1 精确 result identity

先区分两类 attempt：

- `task_attempt_id`：一次 typed task dispatch/requeue 的身份；correction、tool follow-up 和 Provider retry 不改变它；
- `provider_attempt_id`：一次真实网络请求的身份。

入站 task command、typed result、非流式 final response 与 error 统一携带同一 execution correlation，或收敛为一个 `TaskTerminal` envelope。至少绑定：

```text
workflow_id
run_id
task_id
task_attempt_id
agent_id
identity_key
session_id
lifecycle_id
input_contract_ref + sha256
subject_ref + sha256（适用时）
result_ref + sha256 + schema_version（终态适用时）
```

result 使用 append-only、attempt-scoped 路径：

```text
Work/runs/<run>/results/attempts/<task>/<task_attempt_id>.json
```

P0a 先完成 immutable attempt result：先持久化再发布，waiter 直接读取并校验该 attempt ref。P0 的串行 compatibility projection 只能由 coordinator 以 expected task attempt 更新，迟到 result writer 不得覆写固定 `results/<task>.json`；P1c 引入 reducer 后，再由 reducer 通过 expected current 原子提升 canonical result。

persisted record 内嵌 correlation、input/subject identity，以及只对 typed payload canonical JSON 计算的 `payload_sha256`。完整序列化 artifact 的 `result_sha256` 在写完后计算，只放 terminal envelope、sidecar 或 completion 中；result 文件不得递归包含自己的完整文件 hash。

`one_turn()` 的 result、final response 和 error 三个 waiter 全部 exact-key；保持“先注册全部 waiter，再 publish task”的顺序。收到 persisted result 后再次验证 task attempt、session、input/subject/result hash。旧 attempt 的迟到 result/final/error 都不得唤醒或终止新 waiter。

#### P0.2 两类事件平面

- terminal/control/domain：不丢、不合并，按 task/attempt 严格顺序；
- token/thinking delta、动画 tick 和可重新计算的细粒度百分比：按 runtime/task coalesce、限速和有界缓冲；
- run/stage/module/review/decision/failure/artifact/completion 状态变化属于持久 domain event，不能按 stream 合并或丢弃；
- durable/reducer callback 仍按需要串行，不能盲目 gather 共享状态写入；
- GUI/Web stream projection 使用独立 bounded mailbox/worker，慢 subscriber 不得反压 Provider 或 reducer；
- coalesce key 至少包含 workflow/run/task/task-attempt/runtime/message/channel；
- 每任务维护 monotonic sequence 与 terminal fence，terminal 后的旧 pending delta 不得把 UI 从 completed 回退到 running；
- shutdown 定义 drain terminal、保存最终消息、拒绝新任务的顺序。

记录 queue depth/high-water、publish→dispatch latency、callback duration、waiter fanout、coalesced/dropped delta 和 terminal latency。

P0 只保证进程内不主动丢 terminal/control 和正确顺序；跨崩溃真相仍是 persisted result/completion。domain event 的 outbox、持久 sequence 与 cursor/replay 在 D1 实现，不能把 P0 测试冒充 durability。

### P1：并发安全、成本准入与恢复底座

#### P1.0 TaskSpec、lane journal 与 reducer 合同

任何 P3 lane 启用前先实现：

- `LaneTaskSpec`
- `LaneAttemptRecord`
- `LaneCompletion`
- `LaneExceptionCandidate`
- `CohortBarrier`
- `WorkflowReducer`

attempt ID 与 semantic task key 分离。semantic key 使用 canonical JSON digest，并绑定 `run_id`、stage/task kind、module/owner、expected identity、execution profile/schema version：

- discovery：preparation digest + discovery input contract；
- response：Barrier 1 digest + inbox/discovery digests + response contract；
- author：preparation + authoring context + collaboration bundle；
- review/revision：subject + findings/input + lifecycle/round。

lane 的业务 subject/review artifact 写私有路径；usage/event 并发安全 append；全局 state、checkpoint、barrier、兼容 Outputs 与 canonical result 只由 reducer 按 expected revision 原子提升。

`LaneCompletion` 只表示成功并绑定 semantic input/profile digests、subject ref/hash/revision、review completion ref/hash、identity/session、task attempt 和 schema version。Provider attempts、Token、retry、worker/lease 写 `LaneAttemptRecord`；needs-input/disputed/escalate 写 `LaneExceptionCandidate`。retry 次数不得改变成功 completion hash。

#### P1.1 并发安全 telemetry

- immutable per-call manifest append-first；
- `provider-hash-index` 由单写者归并；
- telemetry I/O 不占用 Provider lease；
- 写 `turn_kind`：`task_initial`、`tool_followup`、`submission_correction`、`tool_slice_continuation`、`max_tokens_continuation`、`guard`；
- 记录 context build、serialization、telemetry I/O、queue wait、TTFT、Provider active 和 tool time。

#### P1.2 Provider admission

新增全局 `ProviderAdmissionController`：

- 全局与 provider/model 并发上限；
- RPM/TPM/token reservation；
- shared 429 cooldown 与 jitter backoff；
- retry backoff 不持有网络 lease；
- weighted fairness，为尚未启动的 author 保留容量，避免快模块 audit 饥饿慢作者。

Provider attempt lease 只覆盖真实网络请求。另设 coordinator typed-task admission：

- 按 `TaskExecutionProfile` 预留预计 attempt/Token，terminal 后 reconciliation/release；
- 成本阈值或 hard failure 后冻结新的 typed task/lane admission；
- 已启动 typed submission 完整排空；
- 记录最大 overshoot，并证明它受已预留在途任务上界约束。

第三层 lifecycle round/stagnation gate 必须在 P3 前启用：只在完整 finding → response → verdict 后 checkpoint/pause；达到上限进入 `needs_decision`，不能自动接受 finding。

#### P1.3 identity lease 与执行档位

- `(workflow_id, identity_key)` typed task mutex；
- identity lease 必须在 `_sessions.get/create` 之前取得，覆盖 working-memory reset、config/tools/provider hook 原地变更，直到 typed terminal 持久化；
- `TaskExecutionProfile` 决定 resolved provider route、model、effort、context budget、max output、tool rounds、admission weight；
- P1 同时完成 `ProviderRouter` plumbing，但默认 route 仍为 inherited/same provider；P4 才允许模型 A/B；
- resolved profile/version、Prompt segment digest、input/tool schema digest、provider adapter/cache policy 写入 attempt 和 completion；
- 同一 lifecycle 不得静默更换模型、schema 或 cache namespace。

#### P1.4 三类恢复故障注入

分别测试 evidence decision、cost boundary 和 lane checkpoint：

- decision resolved 后崩溃；
- request/supplement 写入后崩溃；
- workflow 启动后崩溃；
- typed result 已保存但 completion 未写；
- completion 已写但 barrier 未写。

只有已验证 typed result/completion 的恢复才要求新增 Provider call 为 0。decision 已 resolved 但专业工作尚未开始时，后续 Provider 调用是合法的；已发送但响应未持久化的 request 标记 `ambiguous`，不得隐藏式自动重试。

### P2：输入与输出成本控制

#### P2.1 输入减量

1. 每个 task/profile 设置 context budget。
2. Prompt 拆为 identity/rules、role skill、immutable index、task contract、delta/recheck segments。
3. Cross/Chief/Final 默认 index/search-first；按 section slice 打开，不使用普遍 160k minimum-open。
4. recheck 发送 changed content + unchanged hash，不重传未改全文。
5. default-draft 使用确定性 gap matrix 和 scoped uncertainty constraints，不让五个 Agent 分别重复解释同一缺口。
6. 工具 schema 按 phase 收窄；result parts ready 后只保留 list/submit 工具。
7. Knowledge 与 Evidence snapshot 在 P5 完成前至少提供 run-scoped memoization。

#### P2.2 输出减量

1. submit-only task 使用 provider-native forced tool choice。
2. Provider 不支持时显式记录，不伪造 enforced。
3. 长正文继续使用 result parts，不统一降低 max_tokens。
4. 为 discovery/response/review/recheck 设置独立输出和 round 上限。
5. lifecycle round/stagnation 达上限后进入 `needs_decision`，不能伪造通过。
6. 记录 accepted output、discarded/correction output 和最终 artifact chars，识别“写了但未进入成果”的浪费。

#### P2.3 开 lane 前的共享读写收敛

P3 前必须先完成最小并发安全版本：

- run-scoped Knowledge/Evidence immutable memoization，避免每条 lane 重扫/重解析；
- SourceLedger 与 EvidenceResearchMemory 使用单写者、append-first 或等价并发安全合同；
- provider/context telemetry 不做无锁整文件 read-modify-write；
- 完整工具副作用清单，识别看似 read/search 但会写 usage、memory 或 ledger 的工具。

P5 再实现完整 batch index、per-file worker 和 pure-read 工具并行；这里先消除会被 module lanes 放大的同步共享热点。

### P3：bounded workflow lanes

#### P3.1 module lanes

入口分开定义：

- full report：Barrier 2 与每模块 bundle 验证后启动；
- proper-subset module report：preparation + dispatch 后启动，bundle absent，完成后写 `PartialModuleBarrier` 并局部交付，绝不进入 Cross。

full report 不再“全部 author 完成 → 五审串行”，两类入口都使用：

```text
author → deterministic preflight → original auditor
       → scoped revision → same-auditor recheck
       → successful ModuleLaneCompletion
```

要求：

- 使用 bounded supervisor 增量 admission；lane concurrency 与 Provider concurrency 分开，不得一次 gather 所有完整 pipeline；
- fast author 可以立即进入自己的 audit；
- module business artifacts 写 lane 私有路径；
- usage/event 并发安全 append；
- global state/checkpoint/barrier/Outputs 只由 reducer 写；
- completion 至少绑定 run/module/lane/task-attempt、semantic input/profile/preparation/bundle digests、subject ref/hash/revision、review completion ref/hash、reviewer identity/session 与 schema version；
- Module Barrier 验证 exact target set、无重复、固定排序和 current hash 后原子写入，再推进 checkpoint；
- 任一 hard failure 后冻结尚未 admission 的 lanes，排空已开始任务，持久化 exception candidate，且 Cross 调用数为 0；
- Main exception candidates 只在 cohort 排空后的安全边界串行处理。

P3 将把当前“并行 author 全部结束后，再按模块固定顺序审查”进一步改为 author 与各自 audit/revision 的跨模块重叠。默认启用前必须再次同步更新项目指南和业务进度合同；保留的是每个模块内部 author → audit → revision → recheck 的顺序闭环。

#### P3.2 Cross-owner 与修订 lanes

Cross initial 仍串行：

```text
Cross findings
→ group by distinct owner module
→ [specialist revision → preflight → original reviewer regression] × N
→ CrossOwnerBarrier
→ same Cross reviewer verdict/recheck
```

同 owner 不并发；Main exception、scope expansion、Cross verdict、Chief、Final 与 Delivery 保持串行。

任一 Cross-owner lane 失败时不得进入 Cross verdict/recheck；只有 exact owner completion set 通过 `CrossOwnerBarrier` 后，同一 Cross reviewer 才可继续。

### P4：Agent 编排优化

1. P1 已完成 TaskSpec、completion provenance 和 `ProviderRouter` plumbing；P4 只开启按 task profile 的模型/effort A/B。
2. Main conversation service 与 report coordinator 分离；报告 lanes 不并发调用 Main。
3. Wave 1、Wave 2、Wave 3、module/Cross-owner lanes 都不恢复 `query_peer/reply_peer` live chatter，只消费 typed artifacts。
4. 只有相同 M0 fixture 的合同正确率、finding 召回、事实边界和 completed 交付不劣化，才允许轻量任务切换模型。
5. admission key 使用实际 resolved route；同一 lifecycle 的 retry/recheck 不得无记录漂移。
6. Agent 数量不是优化目标；每个 Agent 必须对应独立责任、typed output 和验收，不增加“监督 Agent”来重复阅读相同全文。

### P5：确定性准备、索引、工具与 CAS I/O 加速

#### P5.1 per-file preparation

worker 只返回：

- source file identity/digest；
- provisional evidence；
- raw photo key、OOXML member、picture index；
- content digest/blob ref；
- local photo-evidence relation；
- parse/mapping gaps。

reducer 严格按 manifest 文件顺序、mapper evidence 顺序和 `cellimages.xml` picture 顺序归并；raw photo 的首个 evidence occurrence 决定 primary，然后统一做全局 E-ID 重映射：

1. 分配最终 E-* 与 P-*；
2. 原子重映射 `primary_evidence_id`；
3. 将 evidence 与 photo manifest 分别写入 staging；
4. 以单一 immutable preparation completion 原子发布两者 digest；
5. 之后才允许 module dispatch。

必须故障注入“只写完 evidence staging”“只写完 photo staging”“completion 发布前崩溃”，恢复后编号与绑定仍一致。

#### P5.2 一次构建与批量登记

- immutable Knowledge snapshot：inventory + parser/index schema version；
- immutable Evidence index：preparation + evidence/photo schema version；
- Agent 主动搜索复用同一 snapshot；
- SourceLedger 建 run-scoped 内存索引和 `register_many()`，单写分配 R/W ID；
- photo→evidence adjacency index，避免装配器反复执行 P×E 扫描。

#### P5.3 verified blob ref 贯穿

preparation、template、photo、delivery 和 version 传递：

```text
blob_ref + sha256 + size + media_type
```

当前 `resolve_blob(expected_sha256)` 仍会全量 `_digest()`，不能直接宣称消除重哈希。新增 trusted verified-blob handle/manifest：

- 同一受信 ingestion lineage 内只在首次 ingest 验证一次；
- 下游验证 handle、expected digest、size 和 provenance 后直接 link/view；
- 后台或周期性 scrub 重新校验 canonical blob；
- 外来、legacy 或跨信任边界 ref 仍执行全量 hash。

记录 `cas_ingest_source_bytes`、`cas_rehash_bytes` 与 `cas_hit_after_full_scan`。

#### P5.4 工具并行

先为工具建立副作用清单：

- pure read；
- run-local write；
- shared registry write；
- external network；
- terminal；
- requires order。

只有参数完整、互不依赖且没有隐藏 telemetry/ledger/memory 写入的 pure-read 工具可以受限并行；结果仍按原 tool-call 顺序回填。

## 8. 与部署独立性的对齐

阶段映射固定为：

```text
M0 deployment contract gate
→ D0（项目指南阶段 1：无界面运行时）
→ D1（阶段 2：持久任务和项目隔离）
→ D2（阶段 3：最小 Web API 和三块界面）
→ D3（阶段 4：生产加固）
→ D4（额外的对象存储/无共享文件系统扩展）
```

### D0：无界面本地运行时

- 配置、Provider、MessageBus、LoopManager 和 ReportingRunController 脱离 Qt 启动；
- 引入 Run/Artifact/ProjectRevision/Conversation/Event/Lease/Secret、`AuthorizationService/AccessPolicy` 等端口；
- 引入 `RuntimePaths/ConfigProvider`：配置根、服务状态根和项目存储根全部来自环境或部署参数，不依赖 `Path(__file__)` 推导源码目录；
- 提供独立 service bootstrap 与生命周期；
- local Path/CAS/MessageBus adapter 保持兼容；
- 干净 server-only 安装的 import graph、启动和完整 fake-provider workflow 不依赖 PyQt。

### D1：持久调度与项目隔离卷

- transaction store 原子保存 job/lease/attempt/completion/checkpoint/outbox/provider reservation；
- 每项目隔离 POSIX 持久卷；
- 项目写任务在 operation 执行前取得 fenced write lease；内部 module lanes 都属于同一 lease-owning job，不允许两个同项目写任务先并行运行、最后才竞争 publish；
- 写互斥覆盖 Main 的 `full_report/module_report/aggregate_existing/render_existing/distill_template_skill` 五种 operation，以及 same-run resume、post-delivery revision 和 publish；
- `cancel_requested` 通过 transaction store/run CAS 并发持久化，不等待当前项目租约；当前 lease owner 负责在安全边界停机，清理和最终状态写入仍校验 fencing token；
- queue idempotency key 至少为 `run_id + operation`；
- lease epoch/fencing token 进入 checkpoint、artifact、version、receipt 与 current pointer 的每次写入条件；旧 worker 在 lease 重新授予后恢复写入必须被拒绝；
- run admission 冻结 Inputs/Knowledge/Templates inventory、内容 digest、template skill/schema version；
- 普通运行中上传不改变已启动 run；supplement 形成有序 digest overlay，文件型 supplement 产生新 revision 并记录 lineage；
- 同 revision 的 run 可共享只读输入，但 Work/assets/results 必须隔离；
- 持久化 user、project membership、conversation ownership；
- domain event outbox 按 `project_id + run_id` 分区并提供单调 sequence；
- Provider 已接收但响应未保存时进入 `ambiguous`。

### D2：最小 API 与三块浏览器界面

- 文件、进度、Main 对话；
- 创建命令立即返回稳定 run_id；
- Main 会话绑定 `user_id + project_id + session_id`；
- 支持原 run_id resume、cancel、从已交付版本 revise，以及 decision/failure/delivery 状态；
- 进度以稳定排序的 module state collection 表示多个 active module，不暴露内部 agent/session/queue；
- durable domain event 通过授权后的 `ProgressProjection` 和 cursor 重放；
- transient token delta 合并/限速，不写入完整 durable event log；
- 所有 file/run/conversation/event/download 请求执行项目成员、路径和 run scope 授权；内部 usage、checkpoint、review 与 Agent 记录不通过通用文件 API 暴露；
- 可展示稳定业务角色标签，但禁止 agent_id、内部 session、queue 和内部 Agent 对话；
- verifier、version 和 receipt 成功后才投影 completed。

### D3：生产加固

- SSO/身份集成、权限穿透测试、安全审计与上传/下载加固；
- SecretProvider、出网白名单和默认拒绝；
- 容器、结构化日志、指标、告警；
- 备份恢复、容量、密钥轮换和故障演练；
- 明确数据/模型处理地区、可选外联审批、保留/删除审批和备份恢复目标；
- 镜像 digest、源码 commit 和数据库迁移可追溯。

### D4：可选对象存储与无共享文件系统扩展

- `WorkspaceMaterializer` 为 python-docx/openpyxl/图片解析提供临时沙箱；
- API、coordinator、Agent worker 和 renderer 只传逻辑 ArtifactRef；
- delivery manifest 保持 immutable；以 expected project/report revision + fencing token 做 compare-and-swap 条件更新 `current/latest`，这里的 CAS 是并发控制，不是内容寻址存储的缩写；
- API/worker/renderer 在无共享文件系统下分别扩缩容。

D4 不阻塞基于项目隔离 POSIX 卷的 D2 Web MVP。

## 9. 验收门槛

### 9.1 正确性与恢复

- `policy=draft|skip|ask|block` 与 `decision_action=supplement|draft|skip|stop` 合法组合分别通过，非法组合拒绝。
- stream flood 下 result/final/error 不丢失、不乱序、不被旧 task attempt 唤醒。
- attempt-scoped result persist 后、terminal publish 前崩溃可以 0-call 恢复；result hash 被改动时拒绝。
- waiter timeout/cancel 后无泄漏；terminal fence 后迟到 progress 不回退状态。
- same identity active typed task 永远不超过 1。
- worker 完成顺序任意时 E/P 编号与 primary binding 恒定。
- completion 已写、barrier 未写时恢复新增 Provider call 为 0。
- evidence decision、cost boundary 和 lane resume 参数不能混用。
- Cross 前所有目标 module completion 通过 identity/ref/hash 验证。
- proper-subset 多模块不依赖 Barrier 2，写 PartialModuleBarrier 且 Cross 调用数为 0。
- hard failure 冻结新 typed-task admission、排空已启动任务且成本 overshoot 有界。
- finding-response-verdict、来源绑定、最终结构、DOCX 和 receipt 不降级。

### 9.2 输入输出成本

- 按 missing-evidence policy、cold/warm cache 和 task kind 分组。
- 分别报告 uncached/cache-read/cache-write/output Token，不用总 Token 冒充金额。
- submit-only correction 目标为 0，仅在 `provider_supported && tool_choice_enforced` cohort 作为硬门槛。
- Wave 1/2 在合同正确率与 barrier 驳回率不恶化时，输出 Token p50 至少下降 50%。
- module reviewer recheck 的 unchanged subject-body Token/字符为 0；总输入仍允许包含 delta、必要证据、合同和稳定前缀。
- 报告 wasted-output ratio：discarded/correction output ÷ total output。
- 没有版本化价格表或 Provider 真实费用时，不宣称货币成本下降。
- 来源索引、图片 manifest 和 deterministic output bytes 与 Provider output 分开归因。

### 9.3 加速与并行

- fake latency provider 证明真实时间重叠。
- fast author 的 audit 与 slow author 的 authoring 可重叠。
- module stage p50 目标下降至少 30%，同时报告 p95。
- completed report p50 目标下降至少 20%，同时报告 p95。
- 在同一 M0 条件的 completed paired A/B 中，Provider calls 不增加超过 5%，并分别约束 uncached、cache-read/cache-write 和 output Token。
- 429/retry、queue high-water 和 terminal latency不恶化。
- 样本不足时只报告描述性结果，不切默认开关。
- 只有真实 Provider completed run、五模块/Cross/Final 完整、可读 DOCX 和匹配 receipt 后，才允许切默认开关。

### 9.4 系统与部署

- MessageBus stream 内存和日志字节有界，durable event 不保存逐 token delta。
- Knowledge 每 project revision 全扫描 1 次；Evidence 每 run 解析 1 次。
- delivery→version 的 trusted known-blob `cas_rehash_bytes=0`；外来/legacy ref 仍完整验证。
- 不同项目安全并行；第二个同项目写任务在 operation 执行前即排队或拒绝，不能先运行再竞争 publish。
- API 退出不取消 worker，浏览器断线不影响 run。
- 慢浏览器不反压 Worker；断线按授权 cursor 恢复后，最终 Main 消息、业务进度和 completed 状态一致。
- 旧 worker 在项目 lease 重新授予后的 checkpoint/artifact/version/receipt/current 写入全部被 fencing token 拒绝。
- 已启动 run 不因普通项目上传改变 input digest、E/P 编号或图片主绑定；supplement lineage 可追溯。
- D2 可以使用项目隔离 POSIX 卷；完全无共享文件系统只作为 D4 门槛。

## 10. 建议提交顺序

1. `M0`：更新 post-main fixture、分层基线和只读 telemetry。
2. `P0a`：terminal/result identity envelope、immutable attempt-scoped result、keyed waiter 和串行 compatibility projection。
3. `P0b`：MessageBus control/stream QoS、coalescing 与指标。
4. `P1a`：append-first telemetry、turn kind。
5. `P1b`：Provider admission、identity lease、TaskExecutionProfile plumbing。
6. `P1c`：TaskSpec/semantic key、lane journal、canonical reducer 与三类 resume fault injection。
7. `P2a`：context budget、Prompt segments、index/search-first。
8. `P2b`：forced submit、输出/轮次档位和 accepted-output telemetry。
9. `P2c`：run-scoped memoization、共享 registry 单写合同和工具副作用清单。
10. `P3a`：bounded module lanes、Module/PartialModule Barrier。
11. `P3b`：Cross-owner lanes 和多模块修订复用。
12. `P4`：ProviderRouter 模型/effort A/B 与角色编排优化。
13. `P5a`：per-file preparation 与 evidence/photo reducer。
14. `P5b`：Knowledge/Evidence index、SourceLedger batch、trusted verified-blob handle。
15. `P5c`：pure-read 工具受限并行。
16. `D0-D4`：按 Headless、持久调度、Web MVP、生产加固、可选对象存储顺序推进。

每项独立提交、独立 feature flag、独立回归。建议保留：

```text
event_qos = legacy_fifo | control_and_coalesced_stream
execution_mode = current_serial_review | bounded_module_lanes
provider_admission = observe | enforce
forced_submission = off | supported_tasks
context_profile = legacy | budgeted
preparation_mode = serial | deterministic_workers
provider_route = inherited | task_profile
deployment_storage = isolated_posix | object_materialized
```

## 11. 下一步

下一实施项应是 `M0 + P0a`：

1. 冻结 `a0df065` 的分层串行基线；
2. 给 task command、result/final/error 增加 task-attempt/session/input/result hash 身份；
3. 实现 append-only attempt result、三个 exact-key waiter 与 stream-flood/late-terminal 测试；
4. 通过后严格按 `P0b → P1a/P1b/P1c → P2a/P2b/P2c` 完成 Bus QoS、准入、身份、恢复和共享读写 gate；全部通过后才进入 P3 module lanes。

不能直接从当前状态开始并发完整 module pipeline；MessageBus terminal 饥饿、迟到 result、同 identity 复用和恢复边界仍未解决。
