# Manyselves 成本控制实验分支交接说明

> 现场日期：2026-07-31
>
> 交接分支：`cost-control-experiments`
>
> 最新 main 同步点：`main@a84d409e7c07e936da70d62a06d0c868ac7a6f93`
>
> main 同步合并：`a0df065c273444e8b32c9709324b5bbc4f88b59a`

## 1. 文档入口与效力

| 文档 | 用途 | 状态 |
| --- | --- | --- |
| 本文件 `experimental-cost-control-handoff.md` | 交接入口、已做/未做边界、Git 与验证说明 | 当前 |
| `experimental-cost-control-plan.md` | Phase A–D 的设计、实现范围和初始验收说明 | 已实现机制说明；其中旧基线 SHA/测试数只作历史记录 |
| `experimental-cost-parallel-agent-orchestration-plan-v2.md` | 输入输出成本、并行调度、加速、Agent 编排和部署的后续实施顺序 | 当前唯一执行计划 |
| `experimental-parallel-agent-deployment-plan.md` | 合并 main 之前的第一版并行/部署设计 | 历史记录，不按此继续排期 |

交接时应先读本文件，再读 V2 的第 5–11 节。不要把旧计划中的阶段编号与
V2 的 `M0/P0–P5/D0–D4` 混用。

## 2. Git 与分支关系

- 原始 worktree：`/Users/zzymima0000/Documents/Codex/manyselves`，
  `main@a84d409`，版本 `1.2.1`。
- 实验 worktree：`/Users/zzymima0000/Documents/Codex/manyselves-cost-control`，
  分支 `cost-control-experiments`。
- 最新 main 已通过 `a0df065` **单向合入实验分支**；main 是当前实验分支的
  祖先。
- 成本控制和三波并行实验从未反向进入 main；main 的完整模块流水线仍是串行。
- `origin/cost-control-experiments` 仍停在 `9d1dfcb`。本地后续同步、V2 和本
  交接说明在推送前都只存在于本地，不能让接手人仅从远端分支开始工作。

关键提交：

| 提交 | 内容 |
| --- | --- |
| `0aff4d7` | 实验快照基线 |
| `5f5a5e3` | Phase A–D 成本控制、CAS、preflight、三波协作及测试 |
| `fc7ddae` | 同步证据决策改进 |
| `9d1dfcb` | 第一版并行/部署计划；当前远端实验分支终点 |
| `4a1f375` | 单向合入 main 至 `0a5093e` |
| `7ae6954` | main 同步后的 V2 重规划与实验分支文档修正 |
| `a0df065` | 单向合入 main 的 1.2.1 发布提交 `a84d409` |

## 3. 已经完成的优化

下表中的“完成”表示代码机制已进入实验分支并有 fake/unit/offline 回归证据，
不表示已经通过真实 Provider 测出实际 Token、金额或端到端时延下降。

| 领域 | 已实现机制 | 主要代码/测试证据 |
| --- | --- | --- |
| Provider 输入 | Prompt 不再重复内嵌完整 submission schema/example；按任务生成较窄工具 schema；已持久化长正文在后续历史中改用 `artifact_ref`、字符数和 SHA-256 标记 | `manyselves/core/reporting/prompts.py`、`submission_contracts.py`、`agent_runner.py`、`manyselves/core/loops/agent_loop.py::_compact_persisted_result_part_call`、`tests/reporting/test_agent_runner.py` |
| Provider 输出 | `write_result_parts` 支持先整批校验再落盘；正文分段持久化后，以小型 typed submission 提交引用；revision/Chief 可使用收窄后的工具集合 | `manyselves/core/tools/reporting_collaboration_tools.py::WriteResultPartsTool`、`manyselves/core/reporting/agent_runner.py`、`tests/reporting/test_agent_runner.py` |
| 存储 | 项目级 SHA-256 CAS；Delivery v2、ReportVersion v2 使用 blob 引用/兼容视图；新写入停止部分 legacy 双写；retention 生成 dry-run 计划 | `manyselves/core/artifacts/content_store.py`、`reporting/delivery.py`、`versions.py`、`retention.py` 及对应测试 |
| 审查成本 | 付费语义审查前执行确定性 module preflight；module recheck 发送 changed content、相关 finding/evidence 和未改内容 hash；Chief completion 可按当前 run/ref/hash 恢复 | `manyselves/core/reporting/review_preflight.py`、`review_lifecycle.py`、`workflow.py` 及对应测试 |
| 成本计量与暂停 | UsageLedger 扩展 Provider usage、cache、message/tool schema、阶段和 payload 指纹；`observe/warn/pause_at_boundary` 只在安全 checkpoint 边界处理，并支持同 run 恢复 | `manyselves/core/usage_ledger.py`、`reporting/cost_control.py`、`workflow.py::ReportingRunBudget`、`tests/reporting/test_cost_control.py`、`tests/test_usage_ledger.py` |
| 跨模块协作与当前并行 | Wave 1 五模块 discovery 并行，Barrier 1；仅有 inbox 的 Wave 2 response 并行，Barrier 2；Wave 3 五模块 author 并行 | `manyselves/core/reporting/module_collaboration.py`、`workflow.py::_module_collaboration`、`workflow.py` 中 author `asyncio.gather(... review=False)`、`tests/reporting/test_three_wave_workflow.py` |
| main 同步能力 | 默认缺证 `draft`、evidence/photo traceability、decision reconciliation、MessageBus DEBUG 日志汇总已进入实验分支 | main 合并 `4a1f375` 与 `a0df065`；相关 reporting、mapper、bus 代码和测试 |

## 4. 当前实际执行边界

完整报告的现状是：

```text
Wave 1 discovery 并行
  → Barrier 1
  → 稀疏 Wave 2 response 并行
  → Barrier 2
  → 五模块 author 并行
  → 2.1 至 2.5 各模块 review/revision/recheck 按固定顺序串行
  → Cross 串行
  → Chief 串行
  → Final audit/revision 串行
  → verifier/render/publish
```

因此：

- 可以说“实验分支已有三波协作和 Wave 3 模块写作并行”；
- 不可以说“main 已并行”；
- 不可以说“五条完整 module pipeline 已并行”；
- 不可以把 MessageBus 日志降噪说成 MessageBus 已具备并行 QoS；
- 不可以把 fake/unit/offline 通过说成真实报告已完成。

## 5. 尚未实现，按 V2 继续

| 阶段 | 下一能力 | 当前状态 |
| --- | --- | --- |
| `M0` | 固定 post-main fixture，按 policy/cohort 建真实 Provider、CPU/I/O、storage、bus 分层基线 | 未完成 |
| `P0` | 精确 task terminal/result identity、attempt-scoped append-only result、exact-key waiter、MessageBus control/stream QoS | 未实现 |
| `P1` | TaskSpec、semantic key、lane journal、单写 reducer、Provider 并发/RPM/TPM admission、identity lease/fencing、歧义恢复 | 未实现 |
| `P2` | task context budget、Prompt 分段、index/search-first、forced submission、输出/轮次档位、共享 registry/memoization 安全门 | 未实现 |
| `P3` | 受限 `author → preflight → review → revision → recheck` module lanes，以及 Cross-owner lanes | 未实现 |
| `P4` | ProviderRouter 真正按 task 选择 model/effort，并做角色/档位 A/B | 未实现 |
| `P5` | 确定性 per-file preparation/reducer、Knowledge/Evidence index、SourceLedger batch、trusted verified-blob handle、pure-read 工具并行 | 未实现 |
| `D0–D4` | Headless、本地持久调度、项目隔离 POSIX 卷、Web 三界面、生产加固、可选对象存储 | 未实现 |

当前正确的下一步是 `M0 + P0a`，而不是直接把五个完整
`_module_pipeline()` 放进 `asyncio.gather()`。只有 P0/P1 的 terminal identity、
Bus QoS、Provider admission、identity lease、lane journal 和恢复不变量通过后，
才进入 P3。

## 6. 验证证据与未验证边界

在最新 main 同步提交 `a0df065` 上，使用显式 cost-worktree `PYTHONPATH`
复跑非集成回归，结果为：

```text
1374 passed, 6 deselected in 29.97s
compileall passed
git diff --check passed
```

之所以必须显式指定 `PYTHONPATH`，是因为现有 Python 环境的 editable package
可能指向 main worktree；直接调用其 `pytest` 曾导入错误 checkout，该结果已作废。

仍未完成：

1. 当前实验分支的真实 Provider 完整报告；
2. 与固定旧基线同口径的 Provider 调用、Token、缓存、金额和 p50/p95 对比；
3. 当前 run 的五模块/Cross/Chief/Final 完整性；
4. 新鲜 DOCX 的目视检查与匹配 delivery receipt；
5. retention 删除执行器；目前只有 dry-run；
6. Provider 价格表配置；未配置时不能输出虚构金额。

## 7. 接手后的最小操作

先确认没有落到 main 或旧远端分支：

```bash
cd /Users/zzymima0000/Documents/Codex/manyselves-cost-control
git status --short --branch
git log --oneline --decorate -12
git merge-base --is-ancestor main HEAD
```

使用当前机器已有环境复跑时，必须把实验 worktree 放在 import 优先级：

```bash
PYTHONPATH=/Users/zzymima0000/Documents/Codex/manyselves-cost-control \
QT_QPA_PLATFORM=offscreen \
/Users/zzymima0000/Documents/Codex/manyselves/.venv/bin/python \
  -m pytest -q -m "not integration"
```

然后：

1. 记录 Provider/model/profile、fixture revision、policy 和 Prompt/tool digest；
2. 执行 V2 `M0`，先采集不改变调用行为的分层基线；
3. 独立 feature flag 实现 `P0a`，先过 stream-flood、late-terminal、wrong-attempt、
   crash/resume 测试；
4. 按 `P0b → P1 → P2 → P3` 推进，Cross/Chief/Final 保持串行；
5. 真实 Provider 验证失败时保留当前 run，先审计 typed submission、checkpoint、
   result/review/conversation/usage 和 receipt，不自动重启新 run。

## 8. 交接时必须保留的不变量

1. specialist 负责正文、Claim 和 `E-*` 绑定，原 reviewer 拥有 finding/verdict。
2. typed submission、checkpoint、completion 和 artifact/receipt hash 链才是完成边界。
3. 默认 `draft` 不得通过跳过固定模块或子模块来“降本”。
4. provider attempt 与 task attempt 分开；sent-but-unpersisted 必须标记
   `ambiguous`，不能静默重试。
5. 同一 identity 同时只有一个 typed task；迟到 heartbeat/result/publish 必须由
   lease epoch/fencing 拒绝。
6. Cross 只在所有 module completion 通过确定性 barrier 后启动；Cross、Chief、
   Final 和 publish 保持权威串行。
7. per-file worker 不分配最终 `E-*`/`P-*`；证据和图片绑定由确定性单写 reducer
   按原始顺序完成。
8. 成本控制覆盖输入、输出、重试、continuation、检索/索引 I/O、CAS 重哈希、
   bus、并发、恢复和部署状态，不只统计输出 Token。
