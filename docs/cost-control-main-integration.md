# 成本控制主线合入说明

> 状态：已按 `main@a84d409` 拆分成本控制机制；本次范围不包含任何
> Agent 并行、三波协作或并行部署实现。

## 1. 合入目标

本次合入只减少完整报告在串行执行路径上的重复输入、无效输出、重复存储、
无效审查和不可归因消耗。业务语义、责任边界和执行顺序保持不变：

```text
模块 2.1 写作 → 独立审查 → 定向修订/复核
  → 模块 2.2
  → 模块 2.3
  → 模块 2.4
  → 模块 2.5
  → Cross → Chief → Final audit → render/publish
```

`manyselves/core/reporting/workflow.py::ReportWorkflowRunner.run` 仍按
`pending_modules` 顺序逐模块等待 `_module_pipeline()` 完成。代码中没有五模块
author `gather`、完整 module lane `gather`、Wave 1/2 调度或协作 Barrier。

## 2. 已合入机制

### 2.1 Provider 输入与输出

- `reporting/prompts.py` 不再在任务消息中重复完整 submission schema/example；
  Provider 仍以 `submit_result` 的严格工具 schema 为机器合同。
- `agent_runner.py::_task_submission_schema` 按当前任务收窄 module、review、
  revision、Chief 和 Final 的 Provider 可见 schema。
- `agent_loop.py::_compact_persisted_result_part_call` 把已经持久化的长正文替换为
  artifact ref、字符数和 SHA-256，避免后续轮次重复发送正文。
- `WriteResultPartsTool` 支持正文分段批量校验后写入；小型 typed submission
  只提交引用和合同字段。
- revision/Chief 只获得当前任务需要的工具集合，降低无关工具 schema 成本。

### 2.2 计量与安全边界

- OpenAI-compatible 与 Anthropic adapter 记录 adapter 后的 request/message/
  tool-schema 字符数和 Provider usage；缺少 Provider usage 时保留估算来源标记。
- `UsageLedger.summarize()` 可按 stage 汇总 uncached input、cache read/write、
  output、总 Token、时延和 Provider attempt。
- `ReportingRunBudget` 与 `StageCostController` 支持：
  - `observe`：只记录，不改变执行；
  - `warn`：在完成 checkpoint 后记录告警；
  - `pause_at_boundary`：只在安全阶段边界暂停，同一 run 可显式恢复。
- 默认仍为 `observe`。未配置版本化价格表时不生成虚构金额。

### 2.3 审查成本

- `review_preflight.py` 在首次付费 module review 前执行确定性结构与绑定检查；
  失败时回到原作者修订，不伪造 reviewer finding 或 verdict。
- module recheck 使用 changed narratives、changed/prior Claim semantics、相关
  finding/evidence 和未改内容 hash，不再重复发送全部未改正文。
- Chief/aggregate completion 以当前 run、输入、envelope、artifact ref 和
  SHA-256 绑定；只有上下文完全吻合才允许恢复，避免为省调用误用陈旧结果。

### 2.4 存储

- `ContentAddressedStore` 在项目内以 SHA-256 保存 canonical blob。
- Delivery v2 和 ReportVersion v2 通过 blob ref 与兼容视图复用相同内容；
  v1 receipt/version 继续可读。
- conversation trace 使用小 manifest 和压缩 CAS 内容。
- `ReportingRetentionPlanner` 只生成 usage/retention dry-run 计划，不自动删除
  任何历史运行或交付物。

## 3. 明确未合入

以下内容不在本次代码范围内：

1. `module_collaboration.py`、discovery/response submission 和协作 bundle；
2. Wave 1、Barrier 1、Wave 2、Barrier 2、Wave 3；
3. 五模块 author 并行、完整 module pipeline 并行、Cross owner 并行；
4. Provider 并发/RPM/TPM admission、identity lease/fencing、lane journal、
   single-writer reducer 和 ambiguous attempt 恢复；
5. MessageBus control/terminal/stream QoS 改造；
6. Headless scheduler、Web 界面、项目租约或对象存储 worker。

这些主题只进入
[并行 Agent 编排与部署调研计划](parallel-agent-orchestration-research-plan.md)，
不会因本次合入而被描述为已实现。

## 4. 兼容与回退边界

- 报告模块、Cross、Chief、Final 和 publish 的权威顺序不变。
- specialist 仍负责正文、Claim 和 `E-*` 绑定；原 reviewer 仍负责 finding 和
  verdict。
- typed submission、checkpoint、completion、artifact 与 matching receipt
  仍是完成证据。
- `observe` 模式不因旧的 attempt/token 参数中止正在运行的 Provider 回合。
- retention 没有删除执行器，因此本次不会主动清理历史状态。

## 5. 验证与未验证事实

合入验收至少要求：

```text
PYTHONPATH=<main-worktree> QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest -q -m "not integration"
.venv/bin/python -m compileall -q manyselves tests
git diff --check
```

本文件只记录代码与离线验证边界。真实 Provider 的调用数、Token、缓存、金额、
p50/p95、完整五模块/Cross/Chief/Final、DOCX 目视检查和 matching receipt 必须
由合入后的固定基线实验单独证明；在此之前不声明具体降本比例。

本次拆分在 `main@a84d409` 上的实际离线结果：

```text
1357 passed, 6 deselected in 30.76s
compileall passed
Markdown parse passed
git diff --check passed
```

测试数低于含三波实验的分支，是因为本次主动排除了协作模型、三波工作流及其
测试，不是把失败测试改为跳过。
