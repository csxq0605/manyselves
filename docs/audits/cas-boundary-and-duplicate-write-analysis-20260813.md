# CAS 适用边界与重复写入分析

日期：2026-08-13  
代码分支：`cost-control-experiments`  
代码基线：`85392a3`（删除同步 retention 扫描与未接入运行时的 compactor）  
运行数据：`/Users/zzymima0000/Documents/Codex/test-improvements`  
代表性成功运行：`report-474eeb7648`

## 1. 结论

CAS 不是重复写入治理器，也不是生命周期清理器。CAS 只适合解决这一类问题：

> 同一份体积较大、写成后不再修改的字节，需要被 delivery、report version、当前输出视图等多个逻辑位置引用。

CAS 不应承担以下职责：

- 判断某次业务写入是否有必要；
- 在交付结束时扫描整个 workspace；
- 把临时文件事后“提升”为 CAS view；
- 代替 run 生命周期和临时目录清理；
- 对每个小 JSON、Markdown、checkpoint 做全局内容寻址；
- 用复杂校验流程掩盖生产者重复落盘的问题。

正确策略是“先不写重复内容，确有多个逻辑视图时再共享一个实体”：

1. 在每个生产者处取消无必要写入；
2. 运行中间数据只写 run-local 状态；
3. 大型不可变数据首次进入项目时 ingest 一次；
4. delivery、version、Outputs 只保存引用或只读视图；
5. 成功后只清理当前 run 的固定临时路径，不做全局扫描。

## 2. 当前实测

审计范围为 `Work`、`Outputs`、`.manyselves`，符号链接不计为实体副本。

| 指标 | 结果 |
|---|---:|
| 实体文件 | 62,109 |
| 符号链接 | 752 |
| 实体文件总量 | 1,499,071,247 B |
| SHA-256 完全重复组 | 2,226 |
| 不同 inode 的实体冗余 | 911,942,215 B |
| 实体冗余比例 | 60.83% |

冗余来源：

| 来源 | 冗余字节 | 占全部冗余 | 主要处理方式 |
|---|---:|---:|---|
| 每个 run 重复解压图片 | 850,588,494 B | 93.27% | 消除重复解压并清理 staging |
| Context checkpoint 重复状态 | 35,558,370 B | 3.90% | 相同 hash 不新增序号文件 |
| 其他 run 中间产物 | 12,621,746 B | 1.38% | 按生命周期收敛 |
| 每个 run 的 Knowledge 文本缓存 | 5,070,878 B | 0.56% | 项目级共享只读缓存 |
| Delivery/version 快照 | 3,931,641 B | 0.43% | 单一实体加引用 |
| 模块生命周期快照 | 3,209,010 B | 0.35% | 最终输出使用当前版本视图 |
| Validation/final Markdown | 955,928 B | 0.10% | 只保留 canonical 正文 |
| Outputs 杂项 | 6,148 B | 0.001% | 可忽略或移除 `.DS_Store` |

因此，继续增强全局 CAS 扫描最多是在次要位置做事后修补，不能解决占冗余 93.27% 的图片 staging 副本。

## 3. CAS 当前实际职责

当前有效路径：

- `ContentAddressedStore.ingest_file()`：`manyselves/core/artifacts/content_store.py:64`
- `ContentAddressedStore.persist_with_policy()`：`manyselves/core/artifacts/content_store.py:145`
- `ContentAddressedStore.link_view()`：`manyselves/core/artifacts/content_store.py:216`
- `ReportingService.snapshot_content()`：`manyselves/core/reporting/service.py:135`
- 策略入口 `CasPolicy`：`manyselves/core/artifacts/storage_policy.py:173`

合理使用 CAS 的对象：

- 最终报告 DOCX；
- 来源索引 DOCX；
- 报告模板；
- 原始现场图片；
- 确实需要多个长期视图的大型不可变二进制。

这些对象的共同特征是体积较大、内容不可变、hash 可稳定标识、需要多个逻辑路径。

不应进入 CAS 的对象：

- provider attempt、lease、checkpoint 等小型状态 JSON；
- 正在修订的模块正文；
- 可追加的 usage/conversation JSONL；
- run-local 临时解析结果；
- 仅为调试存在的中间快照；
- 能通过 `ref + hash + typed metadata` 表达的重复 envelope。

## 4. 重复写入的代码来源

### 4.1 图片 staging：最大问题

生产路径：

1. `manyselves/core/reporting/preparation.py:76` 调用 `extract_wps_images()`；
2. 图片写入 `Work/runs/<run>/preparation-workers/.../assets`；
3. `manyselves/core/reporting/service.py:1648-1659` 再将图片规范化到 run assets；
4. 规范化路径已经由 `snapshot_content()` 转为共享内容视图；
5. 但 preparation worker 的实体图片未删除。

实测 `P-0016.png` 单文件 14,770,143 B，在 14 个历史 run 的 preparation worker 中各有一个不同 inode 的实体副本，另有一个 canonical 实体。CAS 已经正确保存最终共享内容，却没有阻止 CAS 之前的重复解压。

修复方向：

- 以输入文件 SHA-256 建项目级解析缓存；
- 相同输入直接复用图片 manifest 与 canonical blobs；
- 首次解析使用 run-local staging；
- reducer 完成、规范化视图和 evidence binding 验证后立即删除 staging assets；
- 失败或 ambiguous run 保留 staging，等待显式恢复/放弃。

### 4.2 Outputs/Modules、delivery、version 三份模块正文

`manyselves/core/reporting/review_lifecycle.py:1136` 在每次模块审查完成时直接重写 `Outputs/Modules/<module>.md`。交付随后在 `delivery.py:123` 按 storage policy 再发布，report version 又在 `versions.py:453` 对 materialized 项复制。

最终五个模块因此同时存在于：

- `Outputs/Modules`；
- run delivery package；
- report version。

模块正文体积不大，CAS 并非必要。更简单的设计是：

- run 内只保留最终批准的 `ModuleSubmission`；
- report version 保留唯一不可变模块 Markdown；
- `Outputs/Modules` 与 delivery module path 使用当前 version 的只读视图；
- 审查过程中不要持续改写项目全局 Outputs。

### 4.3 Context checkpoint 同内容重复写

`manyselves/core/reporting/context_state.py:607` 的 `TaskStateStore.save()` 每次都会分配新 sequence 并写 `items/<sequence>-<hash>.json`。即使 computed hash 与 current 相同，也会产生新实体文件。

修复方向：

- save 前比较 current hash；
- hash 相同直接返回 current path；
- 只有状态真实变化时追加 sequence；
- terminal 后可将历史压缩为状态变化记录，保留 current 与关键边界。

### 4.4 Provider context manifest 近似二次增长

`manyselves/core/reporting/agent_runner.py:1243` 为每次 Provider call 写一个 manifest。manifest 虽然不写完整消息正文，但每次都重新列出完整历史的 message/segment hash 元数据。随着会话增长，后续 manifest 越来越大。

`report-474eeb7648` 有 309 个 provider-call manifest，`context-manifests` 约 19 MiB。单个后期 manifest 可接近 100 KiB，其中绝大部分组件标为 repeated。

修复方向：

- 每次调用只写 attempt identity、canonical request hash、token/字符统计和本轮增量；
- 稳定 system/tool schema 只记录一次定义，后续引用其 hash；
- 历史消息组件使用 previous manifest ref 加 delta；
- recovery 只依赖 pre-send disposition、terminal/result binding，不依赖重写完整组件列表。

### 4.5 每个 run 重建 Knowledge 文本缓存

`manyselves/core/reporting/research/knowledge_context.py:78` 将 index 固定在 `Work/runs/<run>/indexes/knowledge`，使同一 Knowledge 文本在不同 run 中重复写入。

修复方向：

- 项目级 `Work/indexes/knowledge/<inventory_digest>` 保存不可变文本和索引；
- run 只保存 inventory digest、selected section refs、输入 snapshot binding；
- Knowledge 变更产生新 digest，不覆盖旧 cache。

### 4.6 `.manyselves` 完整 result_data 重复嵌入

`manyselves/core/conversations.py:363` 的展示字段原本只保留最多 2,000 字符；但 `manyselves/gui/main_window.py:1885,1895` 又将工具返回的完整 dict 写入 `result_data`。

结果是同一份 Knowledge、Evidence 或模块正文同时存在于业务 artifact、Provider trace 和 GUI conversation JSONL。

实测：

- 全 `.manyselves` 长字符串精确重复约 6.76 MB；
- `report-474eeb7648` 的 `.manyselves` 与其 Work/version 之间有约 7.38 MB 跨区重复长文本；
- 两个统计范围相互重叠，不可相加。

修复方向：

- conversation 只写 preview、artifact_ref、sha256、content type；
- GUI 展开时按需读取 artifact，不在 JSONL 嵌入完整 dict；
- completed run 长期保留 typed terminal、session summary 和 usage aggregate；
- 原始会话按明确调试保留期处理。

### 4.7 Final Markdown 多份完全相同

`report-474eeb7648` 的相同最终 Markdown 同时存在于：

- `validation/report-chief-candidate-r1.md`；
- `validation/report-delivery-final.md`；
- `report-versions/.../01-canonical_markdown.md`。

每份 183,232 B，三者为不同 inode。

修复方向：

- validation 保存 subject ref、subject hash 和 validation result；
- report version 保存唯一 canonical Markdown；
- delivery/Outputs 引用 canonical Markdown；
- 若 candidate 与 canonical hash 相同，不再生成第二份正文。

## 5. 已删除的无效 CAS 周边逻辑

提交 `85392a3` 已删除：

- delivery/resume 中同步执行的 `ReportingRetentionPlanner.generate()`；
- 自动生成 `Work/storage-usage.json` 与 `Work/retention-plan.json`；
- `archive_pending/archive_failed/delivered_with_archive_warning` 作为新运行阶段；
- 未接入报告运行时的 `CompletedRunOutputCompactor`；
- `compact_reporting_storage.py`；
- 仅供 compactor 使用的 `promote_existing_to_view()`、`materialize_view()`；
- 对应的专用测试。

现在新交付在 receipt 和 report version 成功后直接写 `delivered`：`manyselves/core/reporting/workflow.py:6344-6345`。旧 archive 状态只做读取兼容，恢复时规范化为 delivered，不进行 workspace 扫描。

保留 `ReportingRetentionPlanner.preview()`（`retention.py:103`）仅用于显式、手动、只读审计。它不属于交付链，也不写 summary。

## 6. 成功后的清理边界

清理不应扫描全项目，也不应以“找到相同 SHA”作为删除授权。建议新增 run-local finalizer，并同时满足：

1. 顶层 `ReportingRunResult.status == completed`；
2. workflow delivery completed；
3. receipt、report version、output owner 同 run 且 hash 一致；
4. 五模块、Cross、Chief、Final completion 均存在；
5. 当前 run 未 active、未 ambiguous、未 needs_decision；
6. 仅操作固定 allowlist 路径，不跟随外部 symlink。

### 可立即列入成功后清理 allowlist

- `Work/runs/<run>/preparation-workers/`；
- render 临时文件；
- 与 canonical hash 完全相同的 validation candidate 正文；
- run-local Knowledge text cache（项目级共享 cache 落地后）；
- 相同 hash 的重复 context-state item（先修 save no-op）；
- 未被 terminal/result/session summary 引用的 tool-result artifact。

### 不可自动清理

- 运行中、失败、ambiguous、needs_decision 的 run；
- delivery receipt、manifest、version、owner；
- 最终 report/module/source/evidence/claim artifacts；
- Provider attempt disposition 与 terminal/result binding；
- 用户显式 pinned 的审计数据；
- 无法证明引用闭包的旧格式数据。

## 7. 推荐实施顺序

### P0：直接减少最大实体写入

1. 图片解析缓存按输入 SHA 复用；
2. preparation staging 在规范化成功后清理；
3. GUI conversation 取消完整 `result_data`；
4. ContextState 相同 hash 保存 no-op。

### P1：收敛最终产物视图

1. `Outputs/Modules` 改为 report version 当前视图；
2. delivery/version/Outputs 的模块和 Markdown 只保留一个实体；
3. Knowledge 文本迁移到项目级 immutable cache；
4. validation 保存 ref/hash，不复制 subject 正文。

### P2：收敛审计元数据

1. Provider call manifest 改为 previous-ref + delta；
2. completed run 生成 compact audit index；
3. 原始 conversation/provider manifests 设置明确保留策略；
4. 加入 run-local allowlist finalizer，不恢复全局 retention 扫描。

## 8. 验收指标

下一轮真实运行应至少满足：

- 相同输入重复运行时，`preparation-workers` 图片新增实体字节接近 0；
- 成功完成后不存在 preparation worker 图片实体；
- 每个图片 SHA 只有一个 canonical 实体；
- `Outputs/Modules` 不再是五个普通文件副本；
- 相同 ContextManifest hash 不新增 sequence item；
- conversation JSONL 不含完整 `result_data`；
- Provider manifest 总量随调用数近似线性增长，不随完整历史近似二次增长；
- delivery 尾部没有全 workspace scan；
- receipt/version/owner 与最终四个 Reports 视图仍通过 hash、DOCX 可读性及 current-run 校验。

## 9. 审计口径说明

- 字节统计是 2026-08-13 本地 workspace 快照，不是容量预测；
- 完全重复按文件 size + SHA-256 分组，并用 inode 区分真实实体副本；
- 符号链接只计目录项，不计目标文件第二份实体；
- JSON/JSONL 内容重复另按长度至少 500 B 的字符串叶节点计算；
- “可清理”是设计分类，本次没有删除任何运行数据；
- 所有历史失败运行仍应保留，直到有显式归档/放弃决定。
