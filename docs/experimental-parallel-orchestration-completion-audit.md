# 并行调度、Agent 编排与架构加速完成审计

日期：2026-08-11
工作树：`/Users/zzymima0000/Documents/Codex/manyselves-cost-control`
分支：`cost-control-experiments`
V2 提交前基线：`aa63771f9fd1346cb89e136065fc5d3ee2a99a29`
关系：`main@3babc0ca079fe15337b09f0ec4911bf638c39ae5` 是该分支祖先；V2 实现、测试、
部署参考适配和锁文件必须作为同一个原子提交验收，最终真实测试身份以提交后的 HEAD 为准。

## 1. 当前范围

2026-08-11 起，成本、共享上下文和运行加速重新作为 Agent 编排的联合约束。当前实现
在 Wave 1/2/3 都保持真实逐叶 dispatch/session，并以独立 artifact/completion 支持
失败隔离；不变输入通过内容寻址引用和按叶收窄的确定性上下文包复用。每个 dispatch 内的实际 Provider turns
继续由 UsageLedger 计数。Provider RPM/TPM admission 和真实费用
A/B 仍是独立验收项；`observe` 不能被描述为已经强制费用上限。

本审计只回答三件事：

1. 并行任务是否具备安全调度、完整编排、恢复和确定性归并；
2. 架构是否已完成可验证的本地/headless 加速；
3. 哪些外部生产集成和真实 Provider 最优性仍不能宣称完成。

## 2. 逐项实现审计

| 要求 | 状态 | 实现证据 | 验收证据 |
| --- | --- | --- | --- |
| 精确 task/attempt/result identity | 完成 | `agent_runner.py`、`agentic_models.py`、`parallel_runtime.py::TaskAttemptStore` | `test_agent_runner.py`、`test_parallel_runtime.py` |
| control/terminal 与 stream QoS 隔离 | 完成 | `loops/bus.py` 的 control queue、stream coalesce、terminal fence 与指标 | `test_bus.py` |
| identity mutex、项目写租约和 fencing | 完成 | `parallel_runtime.py`、`store.py`、render/delivery/version publish fence | `test_parallel_runtime.py`、`test_distributed_runtime.py`、renderer stale-fence test |
| lane journal、completion、单写 reducer 和 exact barrier | 完成 | `LaneTaskSpec`、`LaneAttemptRecord`、`LaneCompletion`、`WorkflowReducer` | missing/duplicate/order/recovery tests |
| 固定叶子子模块成为独立任务 | 完成 | taxonomy 的 37 个 fixed leaf 在 Wave 1 和 Wave 3 分别拥有 TaskDispatched、session、typed result、artifact、context digest 和 completion；Wave 2 对每个非空 leaf inbox 同样独立调度 | independent Wave 1/2 tests、`test_wave_three_dispatches_and_persists_37_independent_leaf_results`、AgentRunner schema tests |
| Wave 1A 子模块发现与模块内 reducer | 完成 | 37 个 `SubmoduleDiscoverySubmission` 真实独立执行；逐叶落盘后形成 2.1–2.5 的 `ModuleSubmoduleDiscoveryBarrier` | exact leaf coverage、knowledge-change scoped rerun、barrier recovery tests |
| Wave 2 精确叶子接口闭环 | 完成 | request 绑定 requester/target module+submodule；每个非空 leaf inbox 独立回答并形成 response completion | exact request-id、sparse leaf dispatch、wrong-leaf/schema tests |
| Wave 3 子模块写作与模块内 reducer | 完成 | 37 个单叶 author task 独立执行并物化 `SubmoduleDraftSubmission`/Claim/completion，再归并为既有 `ModuleSubmission` | 37 physical leaf authors、并发边界、Claim materialization、resume tests |
| 叶子输入绑定恢复和失败隔离 | 完成 | 每个 leaf artifact 写 exact-context completion；失败冻结新分派但保留已启动成功叶，恢复只调度失败和未启动叶 | leaf scheduler failure/resume、knowledge-change scoped rerun tests |
| 五模块完整私有 lane 并行 | 完成 | `workflow.py` 的 bounded module lanes；默认独立模块并发为 5 | `test_three_wave_workflow.py` 的 overlap、failure-drain、barrier tests |
| Cross-owner lane 并行与权威 recheck 串行 | 完成 | `review_lifecycle.py` 的 owner grouping、bounded lanes、`CrossOwnerBarrier` | `test_agent_workflow.py` 的 overlap/deferred-Main/reviewer-owner tests |
| 动态任务排序和公平稳定 tie-break | 完成 | `scheduling.py::AdaptiveTaskScheduler`，历史时延驱动 LCP-first | `test_scheduling.py`、benchmark dispatch order |
| TaskExecutionProfile 和 ProviderRouter 真正进入执行路径 | 完成 | `execution_runtime.py`；Service、revision、headless worker 共用 router；部署目录加载 `execution-profiles.json` | `test_execution_runtime.py`、`test_headless_runtime.py` |
| 分阶段时延和并发安全 usage telemetry | 完成 | queue/context/serialization/TTFT/provider/tool timing；process-safe ledger | `test_usage_ledger.py`、`test_agent_runner.py` |
| 所有声明输入无损保留 | 完成 | profile 只能增加 headroom；大工具结果分页落盘；`open_tool_result` 永远可用；无正文截断 | `test_execution_runtime.py`、`test_agent_loop.py`、document/reference tests |
| 排队后输入、Knowledge、Templates 和显式 source refs 不漂移 | 完成 | `input_snapshot.py` 的 trusted frozen inventory；render request 同时记录逻辑 ref 与 snapshot ref | `test_input_snapshot.py`、`test_headless_runtime.py`、`test_service_boundary.py` |
| per-file 准备并行但 E/P/primary binding 单写确定 | 完成 | `preparation.py` + manifest-order reducer + 原子 preparation completion | `test_preparation.py`、preparation crash/staging tests |
| Knowledge/Evidence 一次构建、SourceLedger batch、photo adjacency | 完成 | `reference_library.py`、`project_evidence.py`、`source_ledger.py` | 对应 research/source ledger tests |
| trusted CAS 贯穿 delivery/version，避免已知 lineage 重哈希 | 完成 | `TrustedBlobHandle`、trusted view、scrub/metrics | content/delivery/version tests；delivery→version `cas_rehash_bytes=0` |
| 纯读工具受限并行、结果顺序不变 | 完成 | tool side-effect 分类；仅全 pure-read batch 并行 | `test_agent_loop.py` |
| durable run queue、idempotency、cancel、crash reclaim | 完成 | `job_runtime.py`、`headless_runtime.py` | `test_headless_runtime.py` |
| Provider 结果不确定时不自动重试 | 完成 | service 标记 `ambiguous`；队列不自动 claim，只有显式 resume 可继续 | ambiguous outcome regression |
| 无 Qt API/worker 分离与显式 RuntimePaths | 完成 | `headless_service.py`、`headless_runtime.py` | fresh-process no-PyQt、API no-secret tests |
| Web 文件/进度/Main 对话、授权与 cursor replay | 完成 | `web_runtime.py::ReportingApi/ReportingASGIApp` | API reconstruction、ASGI route、project/path/conversation isolation tests |
| 生产安全端口和本地参考适配 | 完成 | identity/secret/egress/policy/audit/metrics/backup/deployment identity | `test_production_runtime.py` |
| 无共享文件系统扩展端口 | 参考实现完成 | object store、portable materializer、revision pointer CAS | `test_artifact_ports.py` |

### 2.1 当前三波任务拓扑

```text
Preparation / immutable snapshot
  → Wave 1A: 37 个真实独立 leaf discovery / 配置化限并发
  → 5 个模块内 discovery barrier/reducer
  → Barrier 1: 生成精确 requester leaf → target leaf request index
  → Wave 2: 每个非空 target leaf inbox 独立 Agent dispatch
  → Barrier 2: 37 个 leaf collaboration bundle + 5 个兼容 module bundle
  → Wave 3: 37 个真实独立 leaf author task，按配置限并发并逐叶落盘
  → 5 个模块内 authoring barrier/reducer
  → 既有 ModuleSubmission / module review lanes
  → Module Barrier → Cross-owner lanes → Chief → Final → render/publish
```

这里的 reducer 是运行时确定性单写组件，不是第 38/43 个自由写作 Agent。它只接受固定
taxonomy 的完整 leaf 集合，校验 identity、source、Claim、completion hash 和当前输入摘要，
然后生成下游既有模块合同。任何缺 leaf、重复 leaf、错模块、错 request 或过期 context 都
不能越过对应 barrier。

## 3. 故障注入结果

- typed result、completion、barrier 分离持久化；缺 completion/缺 barrier 时只恢复已验证成果；
- leaf completion 同时绑定 input refs、计划、Knowledge、discovery/inbox/bundle 和请求约束；
- 一个模块的 Knowledge 变化只使该模块的 Wave 1A leaf 失效，其他模块 leaf 不重跑；
- 任一叶子失败时不发布依赖它的 reducer；已完成兄弟叶 completion 保留，恢复只重派失败或未启动叶子；
- worker/identity/project lease 被重授后，旧 epoch 不能完成、checkpoint 或 publish；
- preparation completion 发布前失败不暴露部分 final snapshot；
- renderer 在 publish fence 失效时只删除临时文件，不暴露 DOCX；
- API 进程退出后，新 worker 可从持久队列重建并完成；
- `ambiguous` job 不会被后台 worker 自动重领，显式 resume 后才执行；
- queued run 后改写原输入不会改变本 run 的实际字节。

## 4. 同输入加速基准

命令：

```bash
PYTHONPATH=/Users/zzymima0000/Documents/Codex/manyselves-cost-control \
/Users/zzymima0000/Documents/Codex/manyselves/.venv/bin/python \
scripts/benchmark_reporting_orchestration.py --repetitions 5 --concurrency 5
```

固定 fixture digest：
`ac3abc5a2de816cde7ff9e2ea927760ae7a579eedd4944fcba9f12d389c76bfd`。

| 指标 | 串行基线 | 自适应并行 | 结果 |
| --- | ---: | ---: | ---: |
| run p50 | 190.531 ms | 82.544 ms | 2.308x |
| run p95 | 191.030 ms | 82.668 ms | 2.311x |
| 调用数 | 25 | 25 | 完全相同 |
| 输出 SHA-256 集合 | 5/5 | 5/5 | 完全相同 |

该基准证明真实 wall-clock 重叠、LCP-first dispatch、调用数不增加和输出身份不变；
它是确定性 fake-latency 编排基准，不是 Provider SLA 或真实报告交付证据。

## 5. 回归结果

```text
子模块编排定向回归：128 passed
reporting 非集成：523 passed
全量非集成：1530 passed, 6 deselected
git diff --check：passed
ruff：当前复用环境未安装 ruff 模块，本轮未计入通过证据
compileall：passed
```

## 6. 完成结论和禁止过度声明

可以称为完成：当前 POSIX 项目卷架构下的核心并行任务调度、Agent 责任编排、
确定性归并、故障恢复、headless 本地服务和 Web MVP 实现。

不能称为完成或“全局最优”：

1. 尚未对同一真实 Provider、同一完整报告 fixture 做 paired A/B，因此不能证明真实
   Provider 报告 p50/p95 已达到最优；
2. `IdentityProvider`、Secret、对象存储和无共享文件系统是端口/参考适配，尚未接入某个
   用户指定的企业 SSO、云 Secret Manager、数据库/队列或对象存储部署；
3. D4 是可选扩展，当前正式执行路径仍以每项目隔离 POSIX 卷为准；
4. 调度器是有历史反馈和公平 tie-break 的 LCP-first 启发式，不是任意 DAG、任意 Provider
   和任意故障分布下的数学全局最优证明；
5. 没有当前真实 Provider completed run、五模块/Cross/Final、可读 DOCX 与匹配 receipt，
   不得把本次 fake/unit/offline 验收写成真实报告已交付。

所以准确表述是：**核心并行编排实现已完成并通过离线验收；真实 Provider 最优性与具体
生产基础设施接入仍是部署/实跑验证项，不是已经完成的事实。**
