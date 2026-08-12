# Manyselves 成本控制实验分支交接说明（历史记录）

> **架构状态（2026-08-12）：本文件只保留历史实验与证据，不是生产执行说明。**
> 旧实验曾把 37 个 taxonomy 终端小节拆成 Wave 1/2/3 的独立任务；该运行架构已废止。
> 当前生产路径只调度五个模块 lane。taxonomy 小节仍保留为 `submodule_narratives`、
> Claim/finding target 和渲染标题，但不再拥有独立 task/session/result/completion/recovery
> 身份，也不再使用 `submodule_task_concurrency`。旧 Wave/leaf 产物应作为不可变历史证据保留。
> 本文件后续段落中对 37-leaf/Wave 的描述均为历史快照，不得据此排期或恢复运行时。

> 现场日期：2026-08-11
>
> 交接分支：`cost-control-experiments`
>
> 最新 main 同步点：`main@3babc0ca079fe15337b09f0ec4911bf638c39ae5`
>
> main 同步合并：`a0df065c273444e8b32c9709324b5bbc4f88b59a`

## 1. 文档入口与效力

| 文档 | 用途 | 状态 |
| --- | --- | --- |
| 本文件 `experimental-cost-control-handoff.md` | 历史实验入口、已做/未做边界、Git 与验证说明 | 历史记录；非生产架构 |
| `experimental-cost-control-plan.md` | Phase A–D 的设计、实现范围和初始验收说明 | 已实现机制说明；其中旧基线 SHA/测试数只作历史记录 |
| `experimental-cost-parallel-agent-orchestration-plan-v2.md` | 输入输出成本、并行调度、加速、Agent 编排和部署的历史实施顺序 | 历史计划；不覆盖五模块生产路径 |
| `experimental-parallel-agent-deployment-plan.md` | 合并 main 之前的第一版并行/部署设计 | 历史记录，不按此继续排期 |

交接时应先读本文件，再读 V2 的第 5–11 节。不要把旧计划中的阶段编号与
V2 的 `M0/P0–P5/D0–D4` 混用。

## 2. Git 与分支关系

- 原始 worktree：`/Users/zzymima0000/Documents/Codex/manyselves`，
  `main@3babc0c`。
- 实验 worktree：`/Users/zzymima0000/Documents/Codex/manyselves-cost-control`，
  分支 `cost-control-experiments`。
- 最新 main 已通过 `a0df065` **单向合入实验分支**；main 是当前实验分支的
  祖先。
- 旧三波并行实验从未反向进入 main；当前实验分支已删除其运行入口、合同与恢复状态，
  并改为固定五模块 lane 与五 Cross owner 的双 barrier 架构。
- V2 提交前 `origin/cost-control-experiments` 与本地都停在 `aa63771`；V2 提交后
  远端仍需另行显式推送，不能把本地新 HEAD 冒充远端已发布身份。

关键提交：

| 提交 | 内容 |
| --- | --- |
| `0aff4d7` | 实验快照基线 |
| `5f5a5e3` | Phase A–D 成本控制、CAS、preflight、三波协作及测试 |
| `fc7ddae` | 同步证据决策改进 |
| `9d1dfcb` | 第一版并行/部署计划；已作为历史记录 |
| `4a1f375` | 单向合入 main 至 `0a5093e` |
| `7ae6954` | main 同步后的 V2 重规划与实验分支文档修正 |
| `a0df065` | 单向合入 main 的 1.2.1 发布提交 `a84d409` |
| `aa63771` | main 同步、Provider 歧义恢复与 V2 实现前基线 |
| `20d9d26` | V2 编排、lane、部署参考实现与离线验证 |
| `f7c2881` | 叶子协作上下文收窄与恢复边界 |
| `8249903` | Wave 3 改为 37 个独立 leaf author task |
| `fb3e832` | Wave 1/2/3 全部改为独立 leaf dispatch/recovery |
| `HEAD` | sibling-shared context + leaf delta；首次与纠错共用任务专属样例 |

## 3. 已经完成的优化

下表中的“完成”表示代码机制已进入实验分支并有 fake/unit/offline 回归证据，
不表示已经通过真实 Provider 测出实际 Token、金额或端到端时延下降。

| 领域 | 已实现机制 | 主要代码/测试证据 |
| --- | --- | --- |
| Provider 输入 | Prompt 不重复粘贴完整 schema/example；首次 `submit_result` 工具 Schema 保留当前 module/leaf/revision/request ids 的唯一任务专属样例；Wave 1/2/3 使用同模块共享 ref + 当前叶子 inline delta；已持久化长正文在后续历史中改用 `artifact_ref`、字符数和 SHA-256 标记 | `manyselves/core/reporting/prompts.py`、`submission_contracts.py`、`agent_runner.py`、`workflow.py::_shared_module_context_ref`、`workflow.py::_leaf_knowledge_excerpt`、`manyselves/core/loops/agent_loop.py::_compact_persisted_result_part_call`、`tests/reporting/test_agent_runner.py`、`test_three_wave_workflow.py` |
| Provider 输出 | Wave 3 每个 leaf author 独立持久化正文和 typed commit；37 个结果完成后才由确定性 reducer 生成模块；一次完整 JSON-object 字符串包装会无损解码，真正的结构错误反馈复用首次任务专属样例，避免错误身份样例诱发第二次同错重试；revision/Chief 使用收窄工具集合 | `manyselves/core/tools/reporting_collaboration_tools.py`、`manyselves/core/reporting/agent_runner.py`、`tests/reporting/test_collaboration_tools.py` |
| 存储 | 项目级 SHA-256 CAS；Delivery v2、ReportVersion v2 使用 blob 引用/兼容视图；新写入停止部分 legacy 双写；retention 生成 dry-run 计划 | `manyselves/core/artifacts/content_store.py`、`reporting/delivery.py`、`versions.py`、`retention.py` 及对应测试 |
| 审查成本 | 付费语义审查前执行确定性 module preflight；module recheck 发送 changed content、相关 finding/evidence 和未改内容 hash；Chief completion 可按当前 run/ref/hash 恢复 | `manyselves/core/reporting/review_preflight.py`、`review_lifecycle.py`、`workflow.py` 及对应测试 |
| 成本计量与暂停 | UsageLedger 扩展 Provider usage、cache、message/tool schema、阶段和 payload 指纹；`observe/warn/pause_at_boundary` 只在安全 checkpoint 边界处理，并支持同 run 恢复 | `manyselves/core/usage_ledger.py`、`reporting/cost_control.py`、`workflow.py::ReportingRunBudget`、`tests/reporting/test_cost_control.py`、`tests/test_usage_ledger.py` |
| 子模块协作、写作与归并（历史） | 旧实验曾通过叶子 scheduler 调度 Wave 1/2/3；这些机制已从生产路径删除。保留旧代码引用和 run 产物只用于审计，不得重新启用 | 历史 `workflow.py`/`test_three_wave_workflow.py` 记录 |
| main 同步能力 | 默认缺证 `draft`、evidence/photo traceability、decision reconciliation、MessageBus DEBUG 日志汇总已进入实验分支 | main 合并 `4a1f375` 与 `a0df065`；相关 reporting、mapper、bus 代码和测试 |

## 4. 当前实际执行边界

旧实验快照（非当前生产执行路径）是：

```text
Wave 1A：37 个真实独立 leaf discovery / 配置化限并发（历史）
  → 5 个模块内 discovery barrier/reducer
  → Barrier 1
  → Wave 2：仅非空 target-leaf inbox；每个非空 leaf 独立 Agent dispatch
  → Barrier 2：37 个 leaf bundle
  → Wave 3：37 个真实独立 leaf author task，按配置限并发并逐叶落盘
  → 5 个模块内 authoring barrier/reducer → ModuleSubmission
  → bounded module review/revision/recheck lanes
  → Cross 串行
  → Chief 串行
  → Final audit/revision 串行
  → verifier/render/publish
```

以上仅描述历史实验，不代表当前生产执行。当前生产路径为五个模块 lane 并行：每个 lane
完成模块写作、Evidence Auditor 审计、定向修订和原 Auditor 复核，再统一进入 Cross →
Chief → Final → verifier/render/publish。taxonomy 小节只作为文档结构和审查作用域。

历史证据边界：

- 只能说“历史提交与旧 run 产物曾实现 Wave 1/2/3 leaf dispatch”；当前工作树不得再将
  它描述为可执行能力，也不得恢复旧 leaf session/result/checkpoint；
- 不可以说“main 已并行”；
- 可以说“五条模块 Editor/Auditor pipeline 与五条 Cross owner pipeline 已有离线并行、
  exact-five barrier 和恢复门禁证据”；
- 不可以把 MessageBus 日志降噪说成 MessageBus 已具备并行 QoS；
- 不可以把 fake/unit/offline 通过说成真实报告已完成。

## 5. 已完成边界与真实验证剩余项

| 阶段 | 能力 | 当前状态 |
| --- | --- | --- |
| `M0` | 固定 post-main fixture，按 policy/cohort 建真实 Provider、CPU/I/O、storage、bus 分层基线 | 未完成 |
| `P0–P3` | terminal/result identity、Bus QoS、lane journal、lease/fencing、固定五 module/Cross-owner lanes | 离线实现完成 |
| `P4` | ProviderRouter/profile 执行路径 | plumbing 完成；真实模型/effort A/B 未完成 |
| `P5` | 确定性 preparation、Knowledge/Evidence index、SourceLedger batch、trusted blob、pure-read 工具并行 | 离线实现完成 |
| `D0–D4` | Headless、持久队列、项目隔离、Web API、生产安全端口、可选对象存储 | 本地参考适配完成；外部生产集成未完成 |

当前正确的下一步是以新 V2 commit 固定真实测试身份，执行 `M0` Provider-backed
完整报告与 paired A/B；不能再用旧 `aa63771` 或未提交工作树作为真实测试身份。

## 6. 验证证据与未验证边界

在 V2 提交前工作树上，使用显式 cost-worktree `PYTHONPATH`
复跑非集成回归，结果为：

```text
1532 passed, 6 deselected in 43.27s
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
