# Context observability and full-validation plan

日期：2026-08-11

工作树：`/Users/zzymima0000/Documents/Codex/manyselves-cost-control`

分支：`cost-control-experiments`

本文是 H4 的只读观测与验收计划。它不改变运行时策略，不启动 Provider，不恢复
run，不清理运行目录，也不把离线/fake 结果写成真实交付。执行前先确认 worktree、
HEAD、运行目录和进程状态；失败 run 和成功 baseline 都是受保护证据。

## 1. 当前 baseline：事实与边界

当前需要审计的 baseline 是 `report-135e68aa42`。项目约定的 run 证据根为：

```text
Work/runs/report-135e68aa42/
```

若该目录未挂载到当前 worktree，应记录 `fixture-not-mounted` 并停止该项审计；不得
用新的 run 替代，也不得从另一个 checkout 猜测内容。baseline 记录中已有以下观测
数字：

| 观测 | 可以陈述 | 不能由此推出 |
| --- | --- | --- |
| run id `report-135e68aa42` | 这是要复核的 run 身份 | 当前工作树 HEAD、Provider/model 或输入 fixture 与它相同 |
| 229 个工具错误分类 | 该 run 的工具错误分类计数，需按 `error_class/tool/phase/status` 原始行复核 | 229 个 Provider 网络失败、229 次重试、或 229 个可节省调用 |
| 28.137M token | baseline 记录中的总 Token 聚合（应由 usage 行逐行重算） | 金额、质量、时延，或与另一 run 的因果差异 |
| `cached_input_tokens` / `uncached_input_tokens` 等字段 | 账本已把输入分成 Provider 回报/估算以及 cached/uncached 维度，需保留原始 `usage_source` | 任何特定缓存实现、命中机制或货币节省；没有同输入、同模型、同价格的配对证据，不得做成本声明 |

`28.137M` 必须同时给出原始 JSONL 路径、行数、`usage_source` 分布、输入/输出/缓存
字段的重算结果。若总和与报告摘要不一致，摘要标为 `unverified`，不四舍五入掩盖
差异。`cached_input_tokens`、`cache_write_input_tokens`、`uncached_input_tokens` 是
Provider/ledger 观测字段；它们不是金额，也不替代 wire payload、context manifest 或
确定性 I/O 计量。本文不使用超出 Provider 指标证据的缓存术语。

### 1.1 只读 fixture index

复核 baseline 时只读取下列位置；具体文件不存在时记录缺失，不创建替代文件：

```text
Work/runs/report-135e68aa42/workflow-state.json
Work/runs/report-135e68aa42/requests/
Work/runs/report-135e68aa42/results/
Work/runs/report-135e68aa42/reviews/
Work/runs/report-135e68aa42/submissions/
Work/runs/report-135e68aa42/checkpoints/
Work/runs/report-135e68aa42/context-manifests/
Work/runs/report-135e68aa42/versions/
Work/runs/report-135e68aa42/delivery/
Work/runs/report-135e68aa42/logs/
.manyselves/usage/report-135e68aa42.jsonl
```

还要记录运行所使用的只读身份资料（若在 run manifest 中声明）：

- commit/branch、Provider route/model/profile 和 profile/schema digest；
- Inputs/Knowledge/Templates inventory 及每个 `ref + sha256 + size`；
- Prompt、tool schema、input contract 和 ContextManifest v3 digest；
- `task_id/task_attempt_id/provider_call_id` 的对应关系；
- delivery/version/current pointer、DOCX、render result 和 receipt 的 hash。

完整正文、工具参数和会话 trace 只能通过受控 forensic 路径读取；Provider 可见
manifest 只能保存 segment ref/hash/字符数和状态，不把正文复制到 manifest。

## 2. A–H 依赖与验证矩阵

依赖按列推进。后一列不能用“测试数量增加”代替前一列的 identity、hash、屏障或
交付证据。每项输出 `implemented`、`observed`、`unverified` 或 `blocked`，并附具体
路径和 hash。

| 列 | 能力/依赖 | 必须先满足 | 只读验证与通过条件 | 失败处置 |
| --- | --- | --- | --- | --- |
| A | baseline、输入 fixture、identity | 固定 HEAD、run id、task/attempt、Inputs/Knowledge/Template digest | `workflow-state.json`、input snapshot、agent identity、profile/prompt/tool digest 一致；缺文件即 `fixture-not-mounted` | 保留当前状态，停止比较，不新建替代 run |
| B | UsageLedger 与 RoundReason | A 的 run/task/provider identity 可追溯 | 每个实际 Provider 行有 `round_reason`、`attempt_kind`、`provider_request_sent`；Token、cached/uncached、request/message/tool chars 与 `usage_source` 可重算；`provider_retry` 仅 attempt≥2 | 无法分类的行记 `unclassified`，不从 status 或 accepted/unknown 猜 retry |
| C | ContextManifest v3/provider observation | B 的 `provider_call_id` 可关联；manifest kind 与 task/provider 语义区分 | 13 类 segment 仅 ref/hash/chars；new/repeated/stable/dynamic/duplicate 计量；namespace 为 run/task/identity/revision；provider call id/ref/hash 双向匹配 | 正文泄漏、hash 不匹配或 kind 错误时 fail-closed，不使用该 observation |
| D | Provider pre-send gate 与恢复 | B/C 的 attempt journal 完整 | `not_sent` 可按策略重发；`definitely_rejected` 可受控重发；`accepted_or_unknown` 和 pending 不自动发第二次物理请求；hash-verified completed terminal 可零调用恢复 | 保留 pending/ambiguous 证据，等待显式 reconciliation；不得自动 cleanup/restart |
| E | 37 leaf / 5 module / owner all-ready | A–D 的 task/completion identity 和 typed artifact 有效 | Wave 1 恰 37 leaf discovery；Wave 2 只处理非空 IF inbox；Wave 3 恰 37 leaf author；5 个 module reducer barrier exact target set、current hash、无重复；owner lane completion 与 finding 一一对应 | 任一缺失/陈旧/重复冻结后续 admission；已完成叶子保留，可显式 resume 缺失项 |
| F | IF 与 Cross 闭环 | E 的 5 module all-ready | 每个 IF request 有 response 或明确 unresolved disposition；Cross 只在 5 个当前 module completion 通过 ref/hash/identity 后启动；owner revisions 过 preflight + 原 reviewer regression，再进入 Cross verdict/recheck | Cross 调用数应为 0（若 barrier 未通过）；保留 owner exception candidate |
| G | Chief / Final / render / publish | F 的 Cross closed、无未决 owner lane | Chief 输入绑定当前 Cross/module hashes；Final 等待当前 Chief candidate hash；verifier、render、fresh readable DOCX、version/delivery receipt/current pointer 全部匹配 | 只报告 incomplete/unverified；不以 streamed text、旧 DOCX 或计划字段替代完成 |
| H | CAS v3、存储与全量交付审计 | G 的 artifact graph 和 trusted refs 可追溯 | CAS v3 manifest、blob ref/hash/size/media type、logical/physical/reused bytes、delivery/version/receipt hash 一致；legacy v1/v2 可读；retention 仅 dry-run | 不执行删除；跨 version/view、错 hash、过期 pointer 或无 fencing token 的写入拒绝 |

推荐顺序是 `A → B → C → D → E → F → G → H`。可以并行读取独立 fixture，但不能
在前置列未通过时宣称后续列完成。

## 3. 观测合同与不可破坏不变量

### 3.1 RoundReason、attempt 和实际发送

允许的 `round_reason` 只有：

```text
direct_submit
evidence_lookup
long_output_continuation
semantic_correction
provider_retry
redundant_followup
tool_contract_error
```

每个新实际 Provider 行同时写：

```text
provider_request_sent: true
attempt_kind: provider_request
round_reason: <explicit value>
provider_call_id
context_manifest_ref
provider_call_hash
```

不发送的 gate/rebuild 记录必须是 `provider_request_sent=false`，并使用
`attempt_kind=pre_send_rebuild|pre_send_block`。`provider_retry` 只能用于
`attempt >= 2` 且确实安全重试的请求；`accepted_or_unknown` 不得被标成 retry，
也不能因为 error/status/turn kind 缺字段而推导为 retry。

### 3.2 ContextManifest v3

Provider observation 的 `manifest_kind` 必须是
`provider_context_observation`；durable typed task state 与它分开读取。每个
manifest segment 只能是以下 hash-only 类型：

```text
system_prompt, task_contract, task_state_capsule,
knowledge_slice, evidence_slice, input_contract, module_skill,
artifact_ref, message, tool_result, completed_result_ref,
response_tail, tool_schema
```

每个 segment 至少记录 `ref`、`sha256`、`chars`、`new|repeated`、
`stable|dynamic`、`first_occurrence` 与 duplicate 标记；汇总记录
`new_chars`、`repeated_chars`、`stable_chars`、`dynamic_chars`、
`duplicate_chars`、`repeated_stable_chars`、`repeated_dynamic_chars` 以及
tool-result/completed-result/evidence duplicate 子项。stable prefix 只计量，不得
因为重复而阻断请求。

重复 tracking 的 key 必须同时包含：

```text
run_id + task_id + identity_key + revision + segment_sha256
```

共享 index 采用 append-first 或显式跨线程/进程锁；不能用无锁整文件
read-modify-write。`provider_call_id`、相对 `provider_call_ref` 和
`provider_call_hash` 在 observation 与 UsageLedger 行之间双向匹配；任一 mismatch
都 fail-closed。

### 3.3 Gate 与恢复不变量

1. pre-send guard 只在 Provider request 尚未离开进程时阻断；不在正在执行的
   Provider turn 或 typed submission 中硬截断。
2. `completed` terminal 必须有当前 run/task/attempt、payload/result hash、checkpoint
   或 completion 的完整链；恢复优先读取并验证它，Provider calls 应为 0。
3. `pending` 或 `accepted_or_unknown` 表示 Provider 可能已接收；在 reconciliation
   前不得创建第二个物理请求。
4. `not_sent`、可证明的 `definitely_rejected` 与 `accepted_or_unknown` 三类处置不能
   合并成一个 retry 状态；每类都要留下 attempt record。
5. 迟到的错误、结果、heartbeat、event 或 publish 必须由 task attempt、session、
   identity lease/fencing 拒绝，不能覆盖新 attempt。
6. Context/telemetry 写入失败不能伪造 typed completion；失败证据保留，后续动作由
   显式控制面决定。

## 4. 37、5、owner all-ready 及 IF/Cross

### 4.1 37 leaf 与 5 module barrier

“37”是固定 leaf 责任数，不是可随意压缩的调用数：

- Wave 1：37 个独立 discovery task，各有 envelope、session、result、completion；
- Barrier 1：按固定模块归并为 5 份 module discovery barrier；
- Wave 2：只给存在非空 interface request（IF）inbox 的 target leaf 派发 response，
  不为没有请求的叶子制造 Provider 调用；
- Barrier 2：每个已请求 IF 有 response 或明确 unresolved，形成 37 leaf bundle
  的完整集合/缺失集合；
- Wave 3：37 个独立 author task 逐叶落盘，按 module reducer 生成 5 个
  `ModuleSubmission`/module completion；
- module all-ready：恰好固定目标 5 个、无重复 task/subject、current input/bundle/
  review hash 一致、completion schema/version 正确。

不能把“当前完成了 5 个 module reducer”改写成“只运行了 5 个 leaf”，也不能通过
跳过 leaf、module review、Cross 或 Final 制造成本下降。

### 4.2 owner all-ready 与 Cross

IF response 只闭合 typed request，不恢复 live peer chatter。每个 request 至少保留：

```text
request_id, source/target module, target leaf,
question/contract hash, response or unresolved disposition,
response artifact ref/hash, task/attempt/session identity
```

Cross initial 前必须满足：

1. 五份当前 module completion 均通过 run/module/subject/input hash 验证；
2. module reviewer 的 finding/verdict 与原 owner identity 对齐；
3. 任何 Cross finding 按 `owner_module_id` 唯一分组；
4. 每个 owner lane 完成 changed subject、response、machine validation、local
   regression completion，并由原 module reviewer 回归确认；
5. `CrossOwnerBarrier` 的 owner 集合精确匹配 finding 集合后，才允许同一 Cross
   reviewer 继续 verdict/recheck。

任一 owner lane 失败时，Cross verdict/recheck 与后续 Chief 调用数必须为 0；其余
已完成 lane 保留并可显式 resume。

## 5. Chief、Final、CAS v3 与交付闭环

### 5.1 Chief / Final 顺序

```text
5 module all-ready
  → Cross initial
  → owner lanes / CrossOwnerBarrier
  → same Cross reviewer verdict/recheck
  → Chief candidate + typed completion
  → Final auditor/revision
  → deterministic verifier
  → render fresh DOCX
  → delivery/version receipt and current pointer
```

Chief 的候选稿、输入 contract、Cross completion、module refs、当前补充约束都必须
绑定当前 run/ref/hash。Final 只能读取当前 Chief candidate hash；旧 session、裸文本、
旧 revision 或只通过结构 lint 的稿件不能提升为交付。完成判断要求同时满足：

- `workflow-state.json` 为当前 run 的 terminal `completed`；
- 5 modules、Cross、Chief、Final 状态和 typed completion 完整；
- verifier/structure/evidence gates 通过，gap/disposition 明确；
- fresh readable DOCX 已生成并能读取关键段落/表格；
- render result、version manifest、delivery receipt 与输出 hash 一致；
- usage/provider/context observation 与该 run/task/attempt 对齐。

### 5.2 CAS v3

CAS v3 验证分为内容寻址和并发指针两层，不能混用缩写：

1. canonical blob 以 `sha256`、`size`、`media_type` 和可信 provenance 标识；相同
   digest 只能有一个 canonical blob；
2. delivery/version manifest 使用 immutable blob refs/兼容视图，不产生新的普通正文
   副本；v1/v2 loader 仍可读；
3. `current/latest` pointer 更新要校验 expected project/report revision、manifest
   hash 和 fencing token；失败、部分交付、过期 owner 不得推进 pointer；
4. trusted verified-blob handle 只能在同一 ingestion lineage、digest、size 和
   provenance 全部一致时复用；外来/legacy ref 仍需完整验证；
5. 记录 CAS source/read/rehash/logical/physical/reused bytes，并与 Provider chars/
   tokens 分开归因；
6. retention 目前只能生成 dry-run plan 和 usage summary，不执行删除，不清理
   active/pinned/quarantine 或历史 baseline。

## 6. 全量离线验证命令

以下命令只使用 fake/unit/offline 路径；显式把实验 worktree 放在 import 优先级，
避免 editable package 指向另一个 checkout。命令不包含 Provider 凭据，也不触发
run recovery、cleanup、merge 或 release。

```bash
cd /Users/zzymima0000/Documents/Codex/manyselves-cost-control
export PYTHONPATH=/Users/zzymima0000/Documents/Codex/manyselves-cost-control
export QT_QPA_PLATFORM=offscreen
PYTHON=/Users/zzymima0000/Documents/Codex/manyselves/.venv/bin/python

# H4 文档与代码边界的静态检查
$PYTHON -m compileall -q manyselves
git diff --check

# 全量非 integration 离线回归
$PYTHON -m pytest -q -m 'not integration'

# 成本/上下文/恢复核心定向回归
$PYTHON -m pytest -q \
  tests/test_usage_ledger.py \
  tests/reporting/test_context_manifest_v3.py \
  tests/reporting/test_context_state.py \
  tests/reporting/test_reporting_context_rebase_integration.py \
  tests/core/loops/test_context_gate.py \
  tests/core/loops/test_agent_loop_retry.py \
  tests/reporting/test_provider_admission.py \
  tests/reporting/test_ready_supervisor.py

# 37/5/IF/Cross/Chief/Final/CAS v3 定向回归
$PYTHON -m pytest -q \
  tests/reporting/test_parallel_orchestration.py \
  tests/reporting/test_interface_cross_closure.py \
  tests/reporting/test_cross_owner_concurrency.py \
  tests/reporting/test_module_auditor_concurrency.py \
  tests/reporting/test_chief_final_contracts.py \
  tests/reporting/test_cas_migration.py \
  tests/reporting/test_versions_v3.py \
  tests/reporting/test_delivery.py \
  tests/reporting/test_three_wave_workflow.py
```

离线结果只报告测试数、失败测试、fixture 路径和代码 HEAD；不得把它们写成真实
Provider calls、真实金额、A/B 或交付完成。若测试生成临时目录，验证后只记录路径和
状态；本计划不授权自动删除。

## 7. 只读交付审计路径

对一个声称 completed 的 run，按以下顺序读取和校验，不调用 Provider：

```text
1. workflow-state.json
   └─ run/task terminal、stage、failure/decision、current revision
2. requests/ + input snapshot/manifest
   └─ operation、policy、Input/Knowledge/Template inventory + hashes
3. results/ + submissions/ + checkpoints/
   └─ typed payload、result/completion/checkpoint identity/hash
4. module artifacts/reviews/
   └─ 5 module current subject、finding/verdict、原 reviewer identity
5. IF inbox/bundles + Cross artifacts
   └─ request disposition、owner lane completion、CrossOwnerBarrier、Cross verdict
6. Chief/Final artifacts
   └─ candidate/current hash、Final audit/revision、gap/disposition
7. context-manifests/provider-calls/ + .manyselves/usage/<run>.jsonl
   └─ provider_call_id/ref/hash、RoundReason、send bit、segments、Token/chars/source
8. versions/ + delivery/ + render result
   └─ version/receipt/current pointer、DOCX readability、all hash links
9. CAS canonical blobs and storage summary
   └─ blob existence/size/media type、logical/physical/reused/rehash bytes、dry-run only
```

审计报告每一项使用以下状态词：

```text
observed       当前 fixture 有可读取原始证据
implemented    当前代码/测试证明机制存在，但没有 run 级事实
unverified     需要 fixture、Provider 或人工目视证据
blocked        前置 identity/hash/barrier 不满足，未继续推断
```

最终交付只能在 `observed` 的 run 级证据同时覆盖 5 modules、Cross、Chief、Final、
fresh DOCX 和 matching receipt 时称为 completed delivery。offline/fake、planned
topology、旧 run、旧 DOCX、单独 usage 汇总和 streamed text 都只能作为辅助证据。

## 8. 明确不做的事情

本 H4 计划不授权以下动作：

- 不运行 Provider A/B，不声明调用、Token、延迟或费用下降；
- 不使用 28.137M 或 cached/uncached 字段推导金额，不输出无价格表的费用估算；
- 不通过降低 `max_tokens`、删减专家上下文、跳过审查或增加 continuation 制造“节省”；
- 不把 37 个固定 leaf 改成 5 个 leaf；5 是 module reducer/all-ready 数，不是 leaf
  责任数；
- 不把 Cross、Chief、Final 变成可任意并行或可跳过的角色；
- 不自动 cleanup、retention delete、覆盖 baseline、恢复失败 run 或启动新 run；
- 不合并分支、不提交、不发布、不打 tag；
- 不把 `RoundReason`、ContextManifest v3、CAS v3 或离线测试的存在写成真实 Provider
  行为；
- 不使用超出当前 Provider 指标的缓存术语或推断。
