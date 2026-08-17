# 并行 Agent 编排与部署调研计划

> 文档性质：调研和实验准入计划，不是当前实现说明。
>
> 前置边界：成本控制先以纯串行形态进入主线；所有并行原型只能在后续独立实验
> 分支中进行，不得直接改变主线报告顺序。

## 1. 调研目标

调研需要回答四个问题：

1. 串行成本控制后的真实 Provider、CPU/I/O、存储和端到端基线是什么；
2. 当前任务 identity、MessageBus、Provider、checkpoint 和恢复模型能否安全
   支撑多个同时活动的模块任务；
3. 在相同质量和交付门槛下，哪一段并行能够降低关键路径，而不会放大 Token、
   429、重试、重复上下文或恢复歧义；
4. 哪些本地 Path/锁/事件合同必须先抽象，才能进入无界面服务端与浏览器
   “文件、进度、对话”三块界面。

调研不以“能运行 `asyncio.gather()`”作为成功标准。成功必须同时满足 typed
completion、原 reviewer 复核、崩溃恢复、成本上限、质量门槛和匹配交付收据。

## 2. 当前代码事实

| 主题 | 当前事实 | 主要证据 |
| --- | --- | --- |
| 模块调度 | 五个模块在 `pending_modules` 循环内依次等待完整 `_module_pipeline()` | `manyselves/core/reporting/workflow.py::ReportWorkflowRunner.run` |
| Agent session | `ReportingAgentRunner._sessions` 缓存可变 `AgentLoop`，同 identity 尚无持久互斥或 fencing token | `manyselves/core/reporting/agent_runner.py` |
| Provider attempt | 已有 attempt telemetry 和安全边界 guard，但没有并发/RPM/TPM admission | `manyselves/core/loops/agent_loop.py`、`reporting/workflow.py::ReportingRunBudget` |
| MessageBus | 单个无界 `asyncio.Queue`；consumer 逐个等待 subscriber callback | `manyselves/core/loops/bus.py::MessageBus` |
| 结果身份 | typed result 依赖 workflow/task/agent/session 路由，但没有独立的 durable `task_attempt_id` 与 late-terminal 拒绝合同 | `reporting/message_router.py`、`reporting/agent_runner.py` |
| 检索与登记 | Reference/Evidence/Source 路径仍有逐调用扫描或 read-modify-write；扩大并发可能先放大 I/O | `reporting/research/reference_library.py`、`project_evidence.py`、`source_ledger.py` |
| 部署 | 当前是 PyQt + 进程内任务 + 项目路径；不是 headless queue/worker 服务 | `manyselves/app.py`、`docs/project-guide-zh.md` |

这些事实意味着：在做 module lane 前，必须先研究 terminal identity、Bus QoS、
Provider admission、identity lease、单写归并和 sent-but-unpersisted 恢复。

## 3. 调研不变量

1. specialist 负责本模块正文、Claim 与 `E-*` 绑定。
2. 原 module reviewer 负责 finding/verdict；preflight 不能替代语义审查。
3. 同一 module 内 `author → review → revision → same-reviewer recheck` 保持顺序。
4. Cross 只能在所有目标 module completion 通过确定性 Barrier 后启动。
5. Cross initial/recheck、Chief、Final audit 与 publish 保持权威串行。
6. 同一 `(workflow_id, identity_key)` 同时只允许一个活动 typed task。
7. Provider 已接受但响应未持久化时标记 `ambiguous`，禁止隐藏式自动重试。
8. per-file worker 不分配最终 `E-*`、`P-*`、`R-*` 或 `W-*`；编号和绑定由
   确定性单写 reducer 按原始顺序完成。
9. 成本比较必须保持相同输入 revision、policy、Provider/model/profile、
   Prompt/tool digest、模块范围和交付质量门槛。
10. 任一实验失败时保留当前 run、日志、typed submission、checkpoint 和产物，
    不自动新建 run 掩盖失败。

## 4. 调研阶段与产出

### R0：冻结串行真实基线

任务：

- 固定 cost-only 主线 SHA、fixture revision、Provider/model/profile、价格表版本、
  Prompt/tool digest 和 `missing_evidence_policy`；
- 分开记录 `draft|skip|ask|block` 与实际 decision action；
- 对同一可交付 fixture 先做 1 次 smoke，再做至少 3 组串行配对运行；
- 采集 Provider calls、uncached/cache input、cache write、output、correction、
  continuation、tool result、queue/TTFT/provider/tool/context 时间、CPU/I/O、
  CAS read/rehash/logical/physical bytes和 bus event/log bytes；
- 验证五模块、module reviews、Cross、Chief、Final、可读 DOCX 与 matching
  receipt。

产出：

- `baseline-manifest.json`：所有固定维度和代码/合同 hash；
- `run-metrics.jsonl`：逐 attempt 与逐 stage 原始数据；
- `quality-gates.json`：结构、finding、证据、DOCX 与 receipt 结果；
- 基线报告：只陈述可复现的绝对数，不提前推导并行收益。

退出条件：至少一个当前主线真实 Provider run 完整交付；失败 run 已完成
evidence-first 审计。

### R1：terminal identity 与 MessageBus QoS 调研

任务：

- 定义 `task_attempt_id` 与 `provider_attempt_id`，明确 correction/follow-up/
  retry 是否改变 identity；
- 设计 attempt-scoped append-only result 与统一 `TaskTerminal` envelope；
- 设计 exact-key waiter，使旧 attempt 的迟到 result/error/terminal 不能唤醒新任务；
- 对 MessageBus 的 control/terminal/durable domain event 与 transient stream
  delta 分级；
- 通过 stream flood、late terminal、wrong attempt、cancel、subscriber error、
  crash/resume 故障注入验证。

产出：identity schema、状态机、事件分类、测试夹具和风险登记。此阶段只允许
实验原型，不改主线调度。

退出条件：所有 identity/QoS 故障用例有确定结果，且不会丢失 typed terminal。

### R2：Provider admission、identity lease 与恢复调研

任务：

- 设计 Provider/model 维度的并发、RPM、TPM 预约和共享 429 cooldown；
- 确保每次真实网络 attempt 在 `try/finally` 中释放 admission，retry backoff
  不占 lease；
- 设计 `(workflow_id, identity_key)` 租约、单调 `lease_epoch` 与 fencing；
- 将 deterministic completion、attempt record 和 exception candidate 分离；
- 定义 pre-send、known-success 和 `ambiguous` 三类恢复路径；
- 验证 stale heartbeat/result/event/publish 被拒绝。

产出：admission/lease 接口、恢复矩阵、容量模型、故障注入结果和 go/no-go 报告。

退出条件：同 identity 冲突、进程崩溃和 Provider 歧义均不会造成双写或静默重试。

### R3：上下文预算与 Agent 执行档位调研

任务：

- 按 task kind/profile 统计 stable prefix、dynamic contract、artifact slice、
  tool result 和历史字符/Token；
- 设计 structural index、section slice、search-first 和 delta recheck 上限；
- 研究 submit-only 的强制 tool choice，以及 correction/continuation 的独立归因；
- 验证 `AgentDefinition.model/effort` 是否真正路由到 Provider；若没有，提出
  ProviderRouter 与 execution provenance 合同；
- 对高能力/紧凑档做配对 A/B，质量门槛不变。

产出：context budget 表、档位策略、Provider 路由证据和质量/成本配对结果。

退出条件：上下文减量来自实际 adapter payload，而不是仅靠缓存命中或降低
输出上限制造更多 continuation。

### R4：受限 module lane 实验

前置：R1、R2、R3 全部通过。

候选拓扑：

```text
module 2.x:
author → deterministic preflight → original auditor
       → scoped revision → same-auditor recheck → ModuleLaneCompletion

all ModuleLaneCompletion
       → deterministic Module Barrier
       → Cross → Chief → Final → publish（仍串行）
```

任务：

- 先从并发度 2 开始，不直接启动五条 lane；
- 每条 lane 只写自己的 run-scoped 路径，共享状态由单写 reducer 归并；
- 比较串行/并发 2/并发 3 的 wall time、Provider queue、429、Token、重复上下文、
  finding 轮次、失败恢复和质量；
- 在 fixed fixture 上做 paired runs；只有样本量足以支持时才报告 p95；
- crash point 覆盖 author 后、review sent 后、revision 写入中和 Barrier 前。

退出条件：相同质量与交付门槛下出现稳定关键路径改善，且成本、错误率和恢复风险
未越过预先登记的上限；否则保持串行。

### R5：Cross owner lane、确定性准备和检索 I/O

前置：R4 通过。

- Cross initial 与 recheck 继续串行，只研究不同 owner 的 scoped revision/
  local regression 是否可并行；
- 文件解析可并行，但排序、E/P 编号、主图片绑定、SourceLedger 登记和 publish
  必须由 deterministic reducer 单写；
- 研究不可变 Knowledge/Evidence index、SourceLedger batch 和 verified blob
  handle，避免并发放大扫描、解析和重哈希；
- 只允许已证明无副作用的 pure-read 工具受限并行。

退出条件：不同执行次序产生完全相同的 canonical artifacts、编号、hash 和 receipt。

### D0–D4：部署边界调研

1. `D0`：抽象 Run/Artifact/Conversation/AgentSession/Queue/Event/Lease/Provider/
   Renderer/Secret 端口，不改变桌面执行；
2. `D1`：本地持久 scheduler、项目级写租约、事务性状态与重放；
3. `D2`：headless API 与授权、脱敏的 `ProgressProjection`；
4. `D3`：浏览器只提供文件、进度、对话三块业务界面；
5. `D4`：在明确需要多 worker 后再研究对象存储与无共享文件系统
   `ArtifactRef`/materializer。

部署调研不得把原始 Agent、Prompt、Provider、API Key、queue 或内部事件直接暴露
给业务用户，也不得把当前 PyQt 进程简单包成 HTTP 就宣称服务化完成。

## 5. 比较与决策方法

每个候选方案都与同一 cost-only 串行基线成对比较：

| 维度 | 必须记录 | 不接受的替代证据 |
| --- | --- | --- |
| 成本 | Provider attempt、输入/缓存/输出 Token、金额版本、重复上下文、CPU/I/O、storage | 只比较输出 Token |
| 时延 | queue、TTFT、provider active、tool、barrier、端到端 wall time | 只比较单个 Agent 时长 |
| 质量 | 固定章节、证据绑定、finding/verdict、Cross/Chief/Final gate | 只跑 unit test |
| 恢复 | cancel/crash/late result/ambiguous attempt/same-run resume | 自动重跑新 run |
| 交付 | 新鲜可读 DOCX、版本/manifest/hash、matching receipt | 旧 `Outputs/` 文件 |

决策只允许三种结论：

- `go`：满足所有安全、质量、成本与恢复门槛，可进入下一实验阶段；
- `revise`：有价值但合同或证据不足，保留串行主线并修订实验；
- `no-go`：收益不稳定或风险越界，关闭该并行方向。

## 6. 当前建议的下一步

先完成 `R0`，获得本次纯成本控制主线的真实 Provider 串行基线；随后只做
`R1` 的 identity/QoS 研究。没有 R1/R2 的故障注入证据前，不进入 module lane，
也不修改主线的五模块顺序。
