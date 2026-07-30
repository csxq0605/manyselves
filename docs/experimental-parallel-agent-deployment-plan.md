# Manyselves 实验性并行加速、Agent 加速与部署独立性计划

> 本文件是同步 main 之前的历史设计记录。当前执行计划请使用
> `docs/experimental-cost-parallel-agent-orchestration-plan-v2.md`；V2 已纳入
> `main@0a5093e`、现有成本优化、默认 draft、图片证据追溯、MessageBus QoS
> 以及项目隔离 POSIX 卷优先的部署顺序。

## 1. 本计划的现场基线

- 实验分支：`cost-control-experiments`。
- 本轮同步提交：`fc7ddae`，只同步了上一次快照 `0aff4d7` 之后的 11 个新增文件差异；原 `manyselves-test-improvements` worktree 未修改。
- 既有成本控制提交：`5f5a5e3`。其请求合同精简、CAS、审查 preflight、delta recheck、阶段成本边界和三波协作继续保留。
- 参考设计：session `019fa2be-22cc-74a0-a4f6-22a210e3a674` 的三波协作不是待实现概念；当前分支已经实现 Wave 1、Barrier 1、稀疏 Wave 2、Barrier 2 和 Wave 3 并行写作。
- 当前仍只有 fake/unit 与定向回归证据；没有用本计划代码运行真实 Provider 全流程，因此本计划中的延迟、Token 和金额目标都不是已实现结论。

本文件只定义下一阶段实验，不在此提交中改动并行调度、Provider 调用或部署代码。

## 2. 当前实现的真实关键路径

### 2.1 已经并行的部分

1. Wave 1 五模块 discovery 并行：`ReportWorkflowRunner._module_collaboration()`。
2. Wave 2 只调度有 inbox 的模块并行回答。
3. Wave 3 五模块 authoring 并行，但显式 `review=False`。
4. 模块 Agent 身份、模块 reviewer session key、typed submission、两道 Barrier 和 current-run artifact hash 已具备。

对应代码主要在：

- `manyselves/core/reporting/workflow.py:927-988`
- `manyselves/core/reporting/workflow.py:4352-4639`
- `manyselves/core/reporting/agent_runner.py:1511-1597`

### 2.2 仍然串行的主耗时

1. Wave 3 必须等五个作者全部返回，才开始逐模块审查。
2. 五个 module review 在 `ReportWorkflowRunner.run()` 中按模块顺序执行。
3. Cross 每轮 finding 按 `owner_module_id` 逐个完成作者回改、机器检查和原模块 reviewer 回归审查。
4. 多模块局部报告与交付后多模块修订仍逐个模块执行。
5. Inputs manifest、解析、mapper、证据编号、Knowledge 投影主要在事件循环内同步串行执行。
6. Cross → Chief → Final audit → Delivery 是有意保持的权威依赖链。

串行审查与 Cross owner 修订的关键锚点还包括 `manyselves/core/reporting/review_lifecycle.py:1730-1956` 和 `manyselves/core/reporting/workflow.py:1683-1705`。

当前模块阶段近似为：

```text
max(Wave1)
+ max(Wave2)
+ max(Author_2.1 ... Author_2.5)
+ Review_2.1 + Review_2.2 + ... + Review_2.5
+ Cross + Chief + FinalAudit + Delivery
```

目标模块阶段应变为：

```text
max(
  Author_2.1 → Review/Revision_2.1,
  Author_2.2 → Review/Revision_2.2,
  ...
  Author_2.5 → Review/Revision_2.5
)
→ ModuleBarrier
→ Cross
→ Chief
→ FinalAudit
→ Delivery
```

### 2.3 现在不能直接并发完整 `_module_pipeline()` 的原因

现有 `tests/reporting/test_three_wave_workflow.py` 明确只允许 gather `review=False` 的 authoring pipeline。这个保护在并行安全底座完成前必须保留：

1. module review 会直接修改共享 `state`、completion refs 和兼容输出。
2. `ReportingAgentRunner` 会复用并原地重置可变 `AgentLoop`，当前没有 `(workflow_id, identity_key)` 级互斥。
3. lane completion 已落盘而全局 checkpoint 尚未记录时，恢复可能重复付费工作。
4. 多条 lane 同时产生 disputed、needs_input 或 escalate 时，可能并发调用同一个 Main identity。
5. 当前 Provider guard 只计数，不限制并发、RPM、TPM 或 429 后的全局冷却。

因此，实施顺序必须是“先隔离状态和身份，再并行闭环”，不能先删除 AST 保护测试。

## 3. 必须保持的业务与审计语义

1. specialist 仍是其模块正文、Claim 和 `E-*` 绑定的唯一责任人。
2. 原 module reviewer 仍拥有 Finding 与 ResolutionVerdict；机器检查只做确定性 preflight。
3. Cross reviewer 仍只负责跨模块关系、传播、冲突和联合验证。
4. Chief 仍只做综合层，不重写已批准模块专业结论。
5. Final auditor 必须等待当前 Chief candidate hash 固定后启动。
6. Main 只处理明确的例外、用户输入依赖和风险接受；普通 finding 不升级给 Main。
7. Barrier 1 前不得执行 Wave 2；Barrier 2 前不得执行最终写作。
8. Cross 前必须存在所有目标模块当前 subject 对应的有效 module completion。
9. 同一 reviewer identity 的 initial、revision recheck 和 verdict 不得并发。
10. 成本暂停只发生在 cohort/stage 安全边界；不能取消正在完成 typed submission 的 Agent。
11. typed payload、completion、checkpoint 和 hash 链才是完成证据；文件存在或普通文本都不够。
12. 不为追求延迟而投机启动 Chief、删除审查角色、统一降低 `max_tokens`，或把五条无界 pipeline 一次 gather。

## 4. 目标调度结构

```text
Inputs project revision
  ↓
per-file parse/map workers
  ↓
deterministic preparation reducer
  ↓
Wave 1 cohort ── Barrier 1
  ↓
sparse Wave 2 cohort ── Barrier 2
  ↓
bounded module lanes
  ├─ author 2.1 → preflight → auditor 2.1 → revision/recheck → lane completion
  ├─ author 2.2 → preflight → auditor 2.2 → revision/recheck → lane completion
  ├─ ...
  └─ author 2.5 → preflight → auditor 2.5 → revision/recheck → lane completion
  ↓
Module Barrier / single-writer reducer
  ↓
Cross initial review
  ↓ findings by owner
bounded Cross owner lanes
  ↓
Cross revision barrier → same Cross reviewer recheck
  ↓
Chief → Final audit/revision loop → deterministic delivery
```

调度原则：

- 并发单位是有 typed input、幂等键、私有写路径和 completion 的 lane，不是任意协程。
- Provider 并发与 workflow lane 并发分开控制；创建五条 lane 不等于同时发出五个 Provider 请求。
- 单写者 coordinator 按固定模块顺序合并 lane 结果、推进 barrier、写全局 checkpoint 和发布状态事件。
- 一个 lane 失败时，已经开始的其他 lane 可以完成并持久化；但任何 lane 未通过时 Cross 不得启动。
- lane 需要 Main 时先持久化 exception candidate；当前安全边界到达后，由 coordinator 串行调用 Main。首版不得跨 module、Cross 或 final lifecycle 合并决定；只有同一 lifecycle/scope 新增 typed batch schema，且能逐 finding 绑定 subject、response、verdict 和 decision 时，才允许批量。

## 5. 实施阶段

### Phase P0：并行安全与可观测底座

#### P0.1 Provider 全局准入

新增 `ProviderAdmissionController`，由真实网络 attempt 使用异步 context manager 获取和释放 lease：

- 全局并发上限；
- provider/model 维度并发上限；
- RPM/TPM 窗口或保守 token reservation；
- 429/限流后的共享 cooldown；
- 带 jitter 的退避；
- 连续限流时降低并发、稳定后缓慢恢复；
- 高优先级 recheck 不得永久饿死普通 authoring；
- queue wait、active time、retry 和 lease 结果写入 usage。

初始实验配置建议从全局并发 3 开始，通过 fake latency 和真实灰度数据调整；这不是产品硬编码值。

准入必须包住 `AgentLoop._chat_with_retries()` 中每个真实 Provider attempt 的 `try/finally`。仅在现有 `before_provider_attempt` hook 中 acquire 不足以保证取消、observer 异常或 retry 后正确释放。

Provider lease 只覆盖真实网络 attempt，不在 retry backoff 期间占用；成功、错误与取消路径都执行 reconciliation/release。调度器为尚未启动的 author 保留容量，避免快模块的 audit/recheck 抢占全部 Provider slot。

#### P0.2 identity 互斥

新增 `IdentityLeaseManager`：

```text
key = workflow_id + identity_key
active typed tasks per key <= 1
```

lease 必须在 `ReportingAgentRunner.run()` 读取或创建 `_sessions` 之前取得，并覆盖 working-memory reset、tool registry 切换和本次 typed task 完成。

#### P0.3 lane journal 与单写者 reducer

新增通用：

- `LaneTaskSpec`
- `LaneAttemptRecord`
- `LaneCompletion`
- `CohortBarrier`
- `WorkflowReducer`

attempt ID 与 semantic task key 分离，semantic key 使用 canonical JSON/tuple digest，而不是字符串拼接。不同阶段使用当时已经存在的确定性输入：

```text
discovery/response:
  run + stage + module + input-contract hash + preparation/artifact digests
author:
  run + module + authoring-context hash + collaboration-bundle hash + preparation hash
review/revision:
  run + module/owner + lifecycle + round + subject hash + findings/input hash
```

lane 的业务 subject/review artifact 只写 `Work/runs/<run_id>/lanes/<lane-id>/` 及自己的 module/review 私有路径。全局 `state`、checkpoint、barrier、兼容 Outputs 只由 reducer 更新；usage/event 使用并发安全 append，SourceLedger 使用单写者或事务性 upsert/ID allocation，identity registry 由 coordinator 或显式锁维护。provider manifests、research usage/memory 等共享写入也必须逐项归类，不能笼统视为 lane 私有。

#### P0.4 指标

在现有 UsageLedger 之外记录：

- `queue_wait_ms`
- `context_build_ms`
- `telemetry_io_ms`
- `provider_active_ms`
- `provider_ttft_ms`
- `tool_active_ms`
- `tool_cache_hit`
- `lane_wall_ms`
- `stage_wall_ms`
- `critical_path_ms`
- `inflight_requests`
- `inflight_reserved_tokens`
- `rate_limit_count`
- `retry_count`
- `typed_correction_turns`
- `resume_reused_completion`

并发后不能继续让每次调用无锁 read-modify-write 共享 `provider-hash-index.json`。新增 concurrency-safe、append-first 的 `TelemetryWriter`：每次调用先写 immutable manifest，共享 hash index 由单写者异步归并；telemetry 序列化与 I/O 不占用 Provider lease。每个 attempt 还必须写显式 `turn_kind`（`task_initial`、`tool_followup`、`submission_correction`、`tool_slice_continuation`、`max_tokens_continuation`、`guard`）以及 `tool_choice_requested/enforced/provider_supported`。

先用 fake provider 建立同一 fixture 的串行与并发基线，再改调度。

#### P0.5 typed-task 与 lifecycle 成本闸门

Provider attempt lease 只限制单次真实请求，并不能约束 module/Cross/final 的语义循环。增加三级控制：

1. Provider attempt admission：限制并发、RPM/TPM 和 reservation。
2. typed-task admission：阈值或硬失败触发后不再启动新的 author/review/revision typed task，已经开始的任务完整结束。
3. lifecycle round/stagnation gate：一轮 finding → response → verdict 完整结束后才 checkpoint/pause；达到上限进入 `needs_decision`，不得自动接受 finding 或伪造完成。

暂停边界是“当前已经开始的 typed task/语义轮次排空”，不必等待五条完整 module lane 全部走完，避免阈值越过后的多轮成本超调。

### Phase P1：Barrier 2 后的 bounded module lanes

把当前“并行 authoring → 串行五审”改为五条独立 module lane：

```text
author
→ deterministic structure/preflight
→ original module auditor
→ scoped specialist revision
→ same-auditor verdict/recheck
→ ModuleLaneCompletion
```

`ModuleLaneCompletion` 只表示成功，并至少绑定：

- run、module、lane、task IDs；
- authoring context hash 和 collaboration bundle hash；
- 最终 subject ref、revision 与 SHA-256；
- module review completion ref 与 SHA-256；
- reviewer identity/session key；
- deterministic completion schema/version。

耗时、Provider calls、Token、retry、错误、worker 与 lease 写入 `LaneAttemptRecord`；`needs_input/disputed/escalate` 写入 `LaneExceptionCandidate`。retry 次数不同不得改变成功 completion hash，reducer 只接受验证通过的 `ModuleLaneCompletion`。

实施要求：

1. 使用有上限的 supervisor；一个 lane 失败不取消其他已经付费执行的兄弟 lane。
2. author 一完成即可进入自己的 audit，不等待最慢作者。
3. 每条 lane 只能合并自己的模块私有状态。
4. reducer 验证全部 completion 后才写 Module Barrier。
5. 同时覆盖 full report 和多模块 `module_report`；单模块仍自然退化为一条 lane。
6. completion 已写、Barrier 未写时模拟崩溃，resume 不得增加该 lane 的 Provider 调用。
7. P0/P1 完成后，才把旧 AST 测试替换为 identity、barrier、single-writer 和 crash-recovery 不变量测试。

### Phase P2：Cross owner lanes 与多模块修订

Cross reviewer 的 initial/recheck 继续串行，但同一轮不同 owner 的回改可并行：

```text
Cross findings
→ deterministic group by owner_module_id
→ [specialist revision → machine check → original module reviewer regression] × N
→ CrossOwnerBarrier
→ same Cross reviewer verdict
```

把 `run_cross_review()` 内当前按 owner 的循环拆为私有 `CrossOwnerLane`。每条 completion 返回 revised subject、responses、machine validation、local regression completion 及 hashes；Cross reducer 按固定模块顺序合并。

多模块局部修订使用同一套 lane，不另建第二套并行框架。scope expansion、未授权模块和 Main exception 仍由 coordinator 串行处理。

收益主要体现在存在多个 Cross findings 或多模块修订时的 p95 长尾。

### Phase P3：确定性准备、检索与工具执行加速

#### P3.1 per-file preparation workers

当前 async preparation 内部实际执行同步解析和 mapper。改为：

```text
immutable project input snapshot
→ per-file FileExtractionResult workers
→ deterministic sort/merge
→ stable E-* / photo IDs
→ run-scoped preparation snapshot
```

worker 不分配最终 `E-*`、`R-*`、`W-*` 或 photo ID，不写共享 state。完成顺序不得影响最终编号。

解析器可进入受限 thread/process executor；大型 XLSX/DOCX 解析不得阻塞主事件循环。

#### P3.2 一次构建、内存复用

- `ProjectEvidenceIndex` 以 preparation hash、完整文件 inventory digest 和 parser/index schema version 为 key，一次加载并复用，不在每次 search/get 重新解析 JSONL。
- 为 Knowledge 建立 immutable `ReferenceIndexSnapshot`：key 同样绑定完整 inventory digest、parser/index schema version；统一扫描一次、内存复用。
- 五个 module Knowledge context 是 snapshot 上的纯投影，可以并行计算；R-* 登记和文件写入仍由单写者确定性完成。
- Agent 主动 `search_reference_library` 使用同一 snapshot，不再每次 `rglob()` 和重读全部文档。
- `SourceLedger` 建立 run-scoped `(kind, locator/id, content_sha256)` 内存索引，提供 `register_many()`；单写者一次分配 R/W ID、一次持久化，不再为每个 hit 重读并整体重写 registry，且 ID 分配结果与 worker 完成顺序无关。多 worker adapter 以唯一约束或 CAS 保持相同语义。
- special-topic Knowledge 等不依赖 Cross 的静态上下文在 dispatch 阶段提前构建并 hash 绑定。

#### P3.3 工具执行策略

为 Tool 增加执行元数据：

- `pure_read`
- `run_local_write`
- `external_network`
- `terminal`
- `requires_order`

标注前先建立完整的副作用清单；名称像 read/search 的工具也可能写 research usage、memory 或 SourceLedger，不能仅凭名称标为 `pure_read`。

同一模型轮次中参数已经完整、互不依赖的 `pure_read` 工具可以受限并行，并按原 tool-call 顺序回填结果。写工具、terminal 工具、search→open 依赖链和 SourceLedger ID 分配不得盲目并行。

同步磁盘工具要转为真正的异步 I/O 或受限 executor；仅对 `async def` 做 gather 但内部仍同步读盘，不算真实加速。

### Phase P4：各 Agent 的输入、模型轮次与缓存优化

#### P4.0 任务执行档位必须真正控制运行时

新增 `TaskExecutionProfile`，按 `allowed_outputs`、`input_contract_kind` 和 phase 决定：

- 实际 provider/model；
- reasoning effort；
- max output；
- context budget；
- tool-round/slice 上限；
- Provider admission 权重。

当前 `AgentDefinition.model/effort` 主要进入 Prompt，而所有 reporting Agent 仍共用一个 `llm_provider`；不能把 Prompt 中写了某个 model 当成已完成路由。Wave 1/2 是紧凑类型化任务，不应自动继承正式长文 authoring 的 32k 以上输出配置。

解析后的执行档位必须成为可恢复合同，而不是瞬时配置。attempt、lane completion 和 resume 校验至少持久化：

- `execution_profile_id/version`；
- resolved provider endpoint class、model、reasoning effort；
- prompt-segment digests 与 input-contract digest；
- tool-schema digest/version；
- provider-adapter/cache-policy version。

上述 provenance 或 cache namespace 改变时必须显式失效或执行有记录的迁移；同一审查生命周期中不得静默漂移。

#### P4.1 静态与动态 Prompt 分层

引入 provider-neutral `PromptSegment`：

- identity/rule segment；
- role Skill segment；
- immutable Knowledge/evidence index segment；
- current task contract segment；
- delta/recheck segment。

每段带 digest、cache eligibility 和数据敏感级别。Anthropic 可把稳定前缀映射为 cache-control block；OpenAI-compatible provider 使用其原生缓存能力或只做 telemetry，不能伪造 cache hit。

缓存键至少绑定 project/run snapshot digest、provider endpoint、model、system Prompt hash、tool schema hash、Agent identity 和 task phase。项目证据不得跨 run 复用，auditor 上下文不得跨 module identity 复用。

不恢复跨 typed task 的全部原始对话。继续以 typed contract 和 completion 为真相，只缓存稳定前缀，避免把旧任务内容带入新审查。

每个 task/profile 必须设置 context budget；缓存命中不能替代输入减量。取消普遍的 160k minimum-open，Cross、Chief 和 final auditor 默认先接收结构索引、目标 section slice、search 结果与 artifact ref；只有首次全局语义审查确有需要时才显式打开完整内容。recheck 继续使用 changed content + unchanged hash，不重复发送未变全文。不能把当前允许的 160k/320k 工具结果上限当成每轮应读满的目标。

每次请求分别记录 stable-prefix、dynamic-contract 和 tool-result 的字符数与 Token，并将 context build、序列化、Provider queue/active、tool 和 telemetry I/O 时间拆开。这样才能区分“缓存更好”与“上下文真的更小”。

#### P4.2 减少 correction 与 continuation

按 output kind 与显式 `turn_kind` 统计：

- 普通文字结束率；
- `submit_result` correction 次数；
- max-token continuation 次数；
- 每轮工具调用数；
- 从首次响应到 typed completion 的额外调用数。

只针对高频失败合同修 Prompt/schema。不得用统一降低 max_tokens 的方式制造更多 continuation；长正文继续使用 result parts 和小型 commit。

进一步减少可避免的付费轮次：

- 当当前唯一允许工具是 `submit_result` 时，首轮使用 provider-native forced tool choice；
- 当长正文 parts 已全部 ready 时，把下一轮工具集收窄为 `list_result_parts + submit_result`，最终强制 `submit_result`；
- Provider adapter 必须显式支持或拒绝 tool-choice 能力，不能静默假装已强制；
- attempt 必须记录 `tool_choice_requested/enforced/provider_supported`，不能用缺失分类的 telemetry 推断 correction 已消失；
- 为每个 typed task 设置 provider-round/slice 上限；达到上限只能在当前 Provider/tool 回合完整结束后写可恢复状态，不能伪造 completed 或截断正在提交的 payload。

submit-only 任务的 `submission_correction` 目标为 0；其他任务分别统计 correction、continuation 和 incomplete，而不是合并成一个失败数。

#### P4.3 能力分级路由

引入 `ProviderRouter` 接口，但首版默认仍可指向同一 Provider：

- Wave 1/2、确定性格式纠正可作为候选轻量档；
- specialist、Cross、Chief、module/final auditor 默认保持高能力档；
- 只有相同 fixture 的合同正确率、finding 召回、事实边界和真实灰度全部不劣化，才允许某类任务切换模型；
- 同一审查生命周期中不得因重试或复审无记录地更换模型。

目标是用数据选择模型，不是先假设“小模型一定更省”。

## 6. 与未来独立部署共用的架构

### 6.1 目标边界

唯一产品方向是：

```text
无界面服务端
+ 浏览器文件界面
+ 浏览器进度界面
+ 浏览器对话界面
```

业务 UI 不暴露 Agent、Provider/API Key、Prompt、Skill、内部队列或运维控制。

### 6.2 需要抽出的端口

领域与 workflow 代码不得直接依赖 PyQt、单机 `Path`、进程内 asyncio task 或具体数据库。先定义：

- `RunRepository`
- `ArtifactRepository`
- `ProjectRevisionRepository`
- `ConversationRepository`
- `AgentSessionRepository`
- `TaskQueue`
- `EventLog`
- `LeaseService`
- `TransactionalSchedulerStore`
- `ProviderGateway` / `ProviderRouter`
- `RendererGateway`
- `WorkspaceMaterializer`
- `AuthorizationService` / `AccessPolicy`
- `ProgressProjection`
- `SecretProvider`
- `Clock`

现有本地实现作为 adapter 保留；实验并行先只使用 local adapter，证明接口不改变报告语义。

### 6.3 持久化调度模型

当前 `MessageBus`、`TaskBoard`、`ReportingRunController._tasks`、router session index 和 AgentRunner sessions 都是进程内真相。目标替换为：

- durable job record；
- append-only/replayable event；
- outbox；
- worker lease、heartbeat 与到期接管；
- job idempotency key；
- attempt 与 completion 分离；
- distributed identity lease；
- central Provider admission state；
- 显式 cancellation token。

事件 envelope 至少包含 `event_id`、`run_id`、单调 sequence、schema version、correlation id 和 causation id。job/state 变更与 event outbox 必须在同一事务提交。

每次 job/identity claim 生成单调 `lease_epoch` / fencing token；job、identity、reducer、event 和 publish 的所有写入都校验 token。旧 token 的 heartbeat、completion、event 与 publish 一律拒绝，避免到期旧 worker 覆盖新 owner。

Provider 通常无法提供 exactly-once。每次调用前先持久化 request fingerprint 和 `started`；如果 worker 在“Provider 已接收、响应尚未持久化”的窗口崩溃，attempt 必须进入明确的 `ambiguous` 状态，由恢复策略或用户决定处理，不能自动重试并把重复成本隐藏掉。一个 typed task 首版采用 sticky lease，只在 typed task 边界迁移 worker。

补资、例外或决策等待必须持久化 decision/continuation，释放 worker、identity lease 与 Provider reservation。用户命令按同一 run/version CAS 恢复；重复提交同一 decision 幂等，冲突 action 拒绝。

lane journal、cohort barrier 和 reducer 正是未来 queue job、worker completion 和 coordinator 的本地版本，不能另写一套服务端协议。

### 6.4 存储独立性

1. Inputs 先形成 immutable project revision；run 只引用 revision digest。
2. run artifacts 使用逻辑 URI 与 hash，不把本地绝对路径写入领域合同。
3. 现有 CAS 可作为 local object-store adapter；将来换对象存储时 digest、size、media type 和 compatibility view 合同不变。
4. `Outputs/` 只作为本地兼容/export view，不再是服务端唯一真相。
5. 在仍写共享 Outputs 的过渡期，同一项目的 new/resume/aggregate/render/revise 必须有项目级写租约。
6. 完成 immutable project revision 和原子 publish 后，可把长时间计算改为 run 并发，只在项目 revision 创建与最终 publish 窗口持有项目写租约。
7. UsageLedger、SourceLedger 和 EvidenceResearchMemory 的进程内锁不能作为多 worker 正确性保证；服务端 adapter 必须使用事务、唯一约束或 compare-and-swap。
8. Provider 凭证只由 `SecretProvider` 从运行环境或密钥服务解析，不能进入业务数据库、事件、日志、artifact 或浏览器响应。
9. API、coordinator、Agent worker 和 render worker 之间只传逻辑 `ArtifactRef`，不传可假定共享挂载的本地路径。需要 python-docx、openpyxl 或图片解析时，由 `WorkspaceMaterializer` 在 worker 临时沙箱只读 materialize 输入；输出先 ingest 对象存储，再提交 completion。
10. 最终发布写 immutable delivery manifest，再以 CAS 更新 `current/latest` pointer；失败、部分交付或过期 fencing token 不得推进 pointer。

### 6.5 Headless API 与 Worker

建议分为：

- API/control plane：project/file、run create/resume/cancel/status、conversation command、artifact download；
- event stream：SSE 或 WebSocket，从原始 domain/ops event 生成经授权、脱敏、稳定 schema 的业务 `ProgressProjection`；
- deterministic workers：intake、mapping、validation、render；
- Agent workers：discovery、response、author、review、Cross、Chief、final；
- coordinator：barrier、reducer、exception 和 publish；
- browser：文件、进度、对话三个业务视图；只显示文件处理、报告阶段、等待补资和交付状态，不暴露 Agent 身份、内部 lane/queue、Prompt 或 Provider。

所有 project/run/artifact/conversation/event 操作均由服务端 `AuthorizationService` 做 scope 校验；event replay 也必须逐 project/run 授权。artifact 下载使用短期签名或鉴权代理，不直接信任客户端 URI/run_id。上传限制大小、类型、文件名/path 与压缩包展开量，并在隔离进程解析。

先完成 headless bootstrap 与本地 adapter 测试，再选择数据库、队列和对象存储产品，避免业务 workflow 绑定某一基础设施。

## 7. 部署迁移阶段

### D0：端口化但仍单进程

- `ReportingService` 和 workflow 改为依赖端口；
- local filesystem、MessageBus、TaskBoard、Provider 和 renderer adapter 保持现状；
- GUI 继续工作；
- 新增不启动 `QApplication` 的 headless integration test。

### D1：持久 job/event/lease

- 先引入事务性 scheduler store，原子承载 job/lease/attempt/completion、run version/checkpoint、event outbox 与 Provider reservation；
- controller 不再以 `_tasks` dict 为权威；
- worker 崩溃后其他进程可按 lease 接管；
- event 可重放构建进度；
- identity 和 Provider admission 可跨进程；
- 幂等键保证不重复排队、不重复执行已知成功 completion；`started` 后结果未知的 Provider attempt 进入 `ambiguous`，不得自动重试。

### D2：对象存储与元数据数据库

- Artifact URI/CAS backend 替换；
- local transactional adapter 替换为部署级元数据数据库；run/project revision/completion/usage 合同不变；
- 兼容导出本地 DOCX；
- 验证旧 local v1/v2 artifacts 仍可读。

### D3：HTTP API 与浏览器三块界面

- 文件上传与版本状态；
- 稳定业务阶段进度，不暴露内部 lane/queue；
- Main 对话、例外与补资；
- artifact 下载；
- 断开浏览器不影响 worker；
- API/Worker 可独立重启和部署。
- 生产环境只保留 API 路线；D0 GUI 是过渡 adapter，不能形成第二套直连 runtime。

## 8. 验收门槛

### 8.1 正确性与恢复

- 任一时刻同一 identity 活跃 typed task 数不超过 1。
- Provider 活跃请求与 token reservation 永不超过配置。
- full report 的 Cross 首次调用前，五个固定 module completion 均通过 identity/ref/hash 验证；proper-subset `module_report` 不进入 Cross。
- 任一 lane 失败时 Cross 调用数为 0，其他已完成 lane 可从 completion 恢复。
- completion 已写、全局 barrier 未写的崩溃点恢复时，该 lane 新增 Provider 调用数为 0。
- 两个 Cross owner lane 可真实重叠，但 Cross recheck 只能在两份 local completion 都落盘后开始。
- 成本 pause 等待当前 cohort 排空后生效，不截断 typed submission。
- 确定性 fixture 的最终模块顺序、Cross 输入集合和结构 gate 与串行基线一致；`E-*` 精确一致。并发导致 R-/W-* 合法重编号时，以 `kind + locator + content_sha256` 比较语义等价，并逐 Claim 验证仍绑定正确 source；若要精确比较 ID，须先把稳定 source reservation/content-derived ID 提前实现。

### 8.2 性能与成本

fake latency provider 必须先证明协程有真实时间重叠。真实 Provider 灰度的初始目标：

实验协议固定同一 input/project revision、provider/model/router/execution profile 与 Provider admission 参数；仅纳入完成正确性门槛的 run，使用 paired 或 randomized 顺序，并预先声明样本量、统计窗口、p50/p95 置信规则及质量/finding 召回判定。先用纯 telemetry 版本冻结串行基线，lane scheduler、admission、forced tool choice、Prompt/cache 分别用独立 feature flag A/B，避免把多种变化混为一个“并行收益”。

- module 阶段 p50 wall time 下降至少 30%；
- completed report 端到端 p50 wall time 下降至少 20%；
- module stage、单 Agent 与 completed report 同时报告 p95；样本不足时明确标记，仅报告描述性结果，不据不稳定 p50/p95 切默认开关；
- cold-cache 与 warm-cache 分组比较，分别拆出 queue wait、TTFT、Provider active、tool、context build 和 telemetry I/O；
- correction/continuation Provider attempts 按 task kind 比较；
- Provider 调用数和总 Token 不高于串行基线 5%，但 Token 只是金额代理；
- 未配置版本化价格表或 Provider 未返回真实费用时，不宣称货币成本下降，只分别报告 uncached、cache-read、cache-write 与 output Token；
- uncached input Token、correction turns 和重复检索不恶化；
- 429/retry 比率不恶化；
- cache hit 必须来自 Provider usage 或本地 index telemetry，不能推测；
- submit-only typed task 的 correction turn 为 0；
- Wave 1/2 的输出 Token p50 在合同正确率和 Barrier 驳回率不恶化时至少下降 50%；
- 每个 project revision 的 Knowledge 全扫描次数为 1，每个 run 的 evidence JSONL 解析次数为 1；
- 报告质量、审查 finding 生命周期、DOCX 可读性和 receipt 一项都不能降级。

如果延迟下降依赖调用/Token 明显增长，则不通过“整体加速”验收。

### 8.3 部署独立性

- server-only 安装与 import graph 不依赖、也不导入 PyQt；无 `QApplication` 进程可以创建、执行、恢复和交付一个 fake-provider run。
- API 进程退出不取消 worker。
- worker 在任意 lane completion 前后崩溃可恢复到安全终态或明确决策态；不承诺对 `ambiguous` Provider attempt 无条件自动续跑。
- 覆盖“Provider 已收到请求、worker 未保存响应”的故障窗口，并验证不会隐藏自动重试。
- 两个 worker 竞争同一 job 时只有一个获得 lease。
- 旧 owner 在 lease 到期后的迟到 heartbeat/completion/event/publish 均被 fencing token 拒绝，且不改变状态或 `current/latest`。
- 浏览器业务进度可由 event log 经授权投影后从零重放。
- 本地 Path adapter 与对象存储 adapter 通过同一合同测试。
- 不同 run 不通过共享 `Outputs/`、全局 Work scratch 或进程内锁互相污染。
- project/run/tenant 范围外的 artifact、conversation 和 event 访问全部拒绝。
- 上传的恶意 path、超限文件和压缩炸弹被拒绝或隔离；artifact URI/run_id 不能绕过授权。
- Provider 凭证不出现在数据库业务表、事件、日志、artifact 或前端响应中。
- API、worker 与 renderer 在无共享文件系统条件下可分别启动、重启和扩缩容。

## 9. 建议提交顺序

1. `P0a`：只补齐并发安全 telemetry，冻结串行基线。
2. `P0b`：`TaskExecutionProfile` plumbing、Provider admission、identity lease、取消与 retry fault injection；各自独立开关。
3. `P0c`：lane journal、single-writer reducer、barrier crash recovery 与 lifecycle 成本闸门。
4. `P1`：bounded module lanes 与 Module Barrier。
5. `P2`：Cross owner lanes、多模块修订复用。
6. `P3`：per-file preparation、immutable evidence/reference index、SourceLedger batch、工具副作用分类与异步策略。
7. `P4a`：上下文减量、Prompt segment cache 与 correction/continuation 优化。
8. `P4b`：submit-only forced tool choice、ProviderRouter 模型 A/B；分别用独立 feature flag 验证。
9. `D0`：端口化与 headless local bootstrap。
10. `D1-D3`：持久队列/事件/租约、对象存储、API 与浏览器三块界面。

每一阶段独立提交、独立开关、独立回归。建议保留：

```text
execution_mode = current_serial_review | bounded_lanes
provider_concurrency = configurable
tool_read_concurrency = configurable
storage_backend = local | object
event_backend = memory | durable
```

默认行为只有在对应正确性、恢复、成本和真实灰度门槛通过后才切换。

## 10. 明确不在本计划首轮做的事

- 不直接并发五条旧 `_module_pipeline(review=True)`。
- 不并发同一 identity。
- 不投机启动 Cross、Chief 或 Final。
- 不删除 module、Cross 或 final 审查。
- 不把 cost boundary 改回请求内硬中断。
- 不把 GUI 直接搬到服务器或保留远程桌面路线。
- 不在业务浏览器暴露内部 Agent/Provider/Prompt/queue。
- 不在没有真实 Provider 完成 run、五模块/Cross/final、可读 DOCX 和匹配 receipt 前宣称已经端到端跑通。
