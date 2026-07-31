# 固定表格填报阶段实施与交接计划

> 计划编号：TABLE-FILL-01
> 状态：待实施，可直接交接
> 基线仓库：`/Users/zzymima0000/Documents/Codex/manyselves`
> 基线提交：`fdf168c9ee93f24826211f06bae6dc1d619f2188`
> 业务规则：`/Users/zzymima0000/Downloads/表格数据填充规则_V2.0_完整版.md`
> （文件正文标题为 V2.1）
> 本计划不包含“内核无状态化”，也不以其为前置条件。

## 1. 已确认的产品方案

本需求是在 Cross 与 Chief 之间增加一个独立的“表格填报”环节：

```text
模块专家写作与模块审查
  → Cross 跨模块审查及闭环
  → 表格填报 Agent
  → 确定性表格校验
  → Chief 汇编
  → 独立全文审计
  → Markdown/DOCX 渲染与交付
```

“位于 Cross 与 Chief 之间”是执行顺序；表格在报告中的实际位置由表格规则固定。
例如某表指定属于 `2.3.1.1` 且位于“现状描述”之前，运行时必须按该目标和锚点
插入，不允许 Agent、Chief 或 renderer 临时选择位置。

表格的标题、列定义、列顺序、目标小节、插入锚点、触发条件、空值规则、排序规则
和图片要求是固定输入。表格填报 Agent 只负责根据 Cross 闭环后的子模块内容及其
证据，填写“位置、现状/问题总结、风险总结、建议、补充图片”等单元格。

## 2. 职责边界

### 2.1 表格填报 Agent 负责

- 读取 Cross 已闭环的五个模块及所有最小子模块正文；
- 读取当前 run 的 Evidence、Source 和照片清单；
- 按固定列要求提炼位置、现状、风险、建议等内容；
- 在行级绑定支撑该行的 `E-*` 和 `P-*`；
- 对规则要求的数值执行明确计算，或调用已有安全计算工具；
- 按规则提交“生成表格”或“抑制表格并回写说明”；
- 只提交 typed result，不直接编辑 Markdown、DOCX 或批准模块正文。

### 2.2 运行时负责

- 冻结并提供表格定义、目标小节和插入锚点；
- 校验表格数量、列、顺序、必填值、空值、证据、图片、公式和排序；
- 将通过校验的表格包持久化，并写完成引用；
- 在 Chief 输入中提供只读表格包；
- 将表格和图片按固定锚点装配进 Markdown/DOCX；
- 将表格包摘要写入报告版本、交付清单和 receipt；
- 在 resume/revision 时拒绝复用与当前模块、规则或证据摘要不匹配的旧结果。

### 2.3 其他角色不得做的事

- Cross 不填表，只负责模块关系和跨模块一致性闭环；
- Chief 不生成、删减、改写或移动表格，只综合批准正文；
- Final auditor 不替 Agent 修表，只报告表文不一致、遗漏或不可追溯问题；
- renderer 不推断业务含义，只执行已经确定的表格和锚点合同；
- 表格 Agent 不产生批准模块中不存在的新项目事实；发现事实缺口时按规则标注。

## 3. 当前代码基线

| 当前事实 | 代码位置 | 改造含义 |
| --- | --- | --- |
| 完整报告在 Cross 完成后直接进入 Chief | `manyselves/core/reporting/workflow.py::ReportWorkflowRunner.run`、修订流程对应分支 | 在两个 Cross → Chief 路径都插入表格阶段 |
| Cross 包装入口为 `_cross_review()` | `manyselves/core/reporting/workflow.py` | 表格阶段只接受其 completion ref |
| Chief 输入包含五个已批准模块和 Cross completion ref | `manyselves/core/reporting/input_contracts.py::ChiefEditorInput` | 增加受保护的表格完成引用 |
| 现有 `tables` 由 Chief 提交 | `manyselves/core/reporting/agentic_models.py::EditedReportSubmission` | 将表格作者权从 Chief 移走 |
| 现有表格只含标题、表头、字符串行和证据 | `TableSubmission`、`TableSubmissionInput` | 增加目标小节、固定定义 ID、行级证据和图片 |
| canonical Markdown 使用全局 `tables` | `manyselves/core/reporting/report_markdown.py` | 改为按目标小节和锚点装配 |
| 图片当前按最小子模块放置，但形成独立图证汇总 | `manyselves/core/reporting/rendering/pds_docx_renderer.py::_place_photo_tokens` | 业务表行有图片列时优先写入对应单元格 |
| checkpoint 只有 Cross/Chief/Final 引用 | `manyselves/core/reporting/workflow.py::FullReportCheckpoint` | 增加表格输入、结果、校验和 completion 引用 |

当前 `REPORT_TAXONOMY` 包含多级最小子模块，但只有 `2.3.1`，没有
`2.3.1.1`。如果最终表格目录确实指定 `2.3.1.1`，必须先把它作为正式 taxonomy
叶节点加入并更新结构测试；不得让 renderer 接受一个 taxonomy 中不存在的标题。

## 4. 表格规则固化

业务规则文件存在显示编号不一致：正文从表 5 跳到表 8，附录又把 SPD 等表重编号
为表 6 开始；标题称 19 表，通用规则称 21 种，另有 `表15~N` 动态集合。实现不应
使用显示表号作为主键。

新增配电报告专用目录，例如：

```text
manyselves/templates/reporting/tables/
├── catalog.yaml
├── schemas/
└── source-rule-audit.md
```

`catalog.yaml` 中每张表至少固化：

```yaml
id: pds.spd_not_in_service
source_aliases: ["正文表8", "附录表6"]
target_section_id: "2.3.3"
placement: before_current_state
title: 电涌保护装置(SPD)未投入清单表
columns:
  - {id: location, title: 位置, required: true}
  - {id: attachment, title: 附件, required: true, kind: photo_or_text}
trigger: spd_problem_exists
empty_notice: "本次未发现SPD未投入问题，或未收到相关检查数据。"
sort: [location]
```

首版目录仍属于本需求的业务配置，不要求先建设通用 DefinitionBundle。加载器必须
使用安全 YAML schema、拒绝未知字段、拒绝目录逃逸，并在每个 run 中保存
catalog version 和 SHA-256。

规则文件中每一张表都必须进入一份“规则覆盖矩阵”，至少包含：

- 稳定 ID、原表号 aliases、标题；
- 目标章节/最小子模块；
- 精确插入锚点；
- 触发/始终生成条件；
- 固定列和每列内容要求；
- 数据/正文/知识/图片来源；
- 计算、阈值、筛选和排序；
- 单元格空值策略；
- 空表回写文本；
- 是否为动态表及实例命名规则。

“表号有冲突”不能导致实现者自行改表名或列；显示编号最终以实际报告模板和规则
覆盖矩阵为准，稳定 ID 保持不变。

## 5. Typed 合同

建议新增以下合同。具体类名允许遵循仓库命名规范调整，但所有字段语义必须保留。

### 5.1 `TableFillInput`

```yaml
kind: table_fill_input
run_id: report-...
cross_review_completion_ref: Work/runs/.../reviews/cross-completion.json
approved_modules:
  "2.1": {revision: 2, content_ref: ...}
table_catalog_ref: Work/runs/.../definitions/table-catalog.yaml
table_catalog_sha256: ...
evidence_ref: Work/runs/.../evidence/evidence.json
source_ledger_ref: Work/runs/.../ledgers/sources.json
photo_manifest_ref: Work/runs/.../evidence/photo-manifest.json
```

运行时应把固定表定义作为合同的一部分或只读引用提供；Agent 不得自行填写
`headers`、`target_section_id` 或 `placement`。

### 5.2 `TableFillSubmission`

```yaml
kind: table_fill_submission
tables:
  - definition_id: pds.harmonic_over_limit
    disposition: generated
    rows:
      - cells:
          location: 1#配电室
          current_or_problem: THDu为...
          risk: ...
          recommendation: ...
          photo: {photo_id: P-0003, fallback_text: 未提供}
        evidence_ids: [E-0012]
  - definition_id: pds.some_optional_table
    disposition: suppressed
    suppression_reason: missing_input
    notice_text: 本次未收到...
```

要求：

- `definition_id` 必须覆盖当前 catalog 判定为适用的全部表；
- `generated` 与 `suppressed` 互斥；
- 单元格使用列 ID，不允许 Agent 改列名或增加列；
- 每一项目事实行必须有当前 run 的 `E-*`；
- 图片只允许使用当前照片清单中的 `P-*`；
- 不允许空字符串；必须使用表级允许的“—/待补充/未测量/未收到/N/A”等值；
- 总结类单元格可以由 Agent 撰写，但不得改变证据中的数值、位置和风险等级。

### 5.3 `TableFillCompletion`

完成记录至少包含：

- input、catalog、五模块正文、Evidence、Source、photo manifest 的摘要；
- typed submission ref；
- deterministic validation ref；
- 生成/抑制表格清单和目标锚点；
- 表格行数、图片数、notice 数；
- Agent result/conversation ref；
- completion hash 和时间。

只有 completion 记录成功落盘后，工作流才能进入 Chief。

## 6. 工作包与提交顺序

### T0：规则覆盖矩阵和 catalog

涉及：

- 新增 `manyselves/templates/reporting/tables/`；
- 为规则文件全部表定义稳定 ID；
- 将正文/附录编号冲突写入 `source_aliases`；
- 明确每张表的目标小节和固定锚点；
- 校验 catalog 可加载、ID 唯一、列 ID 唯一、目标 taxonomy 有效。

完成标准：

- 规则文件中的每个表定义都能在覆盖矩阵中定位；
- 任一表缺目标、锚点、列、触发或空表规则时加载失败；
- 相同 catalog 内容产生相同 SHA-256。

建议提交：`feat(reporting): add validated table rule catalog`

### T1：独立 Agent 身份和输入/输出合同

涉及：

- `manyselves/templates/reporting/agents/table-filler.md`；
- `manyselves/core/reporting/config.py` carrier 列表；
- `manyselves/core/reporting/input_contracts.py`；
- `manyselves/core/reporting/agentic_models.py`；
- `manyselves/core/reporting/submission_contracts.py`；
- `manyselves/core/reporting/agent_runner.py`；
- `manyselves/core/tools/reporting_collaboration_tools.py`。

Agent 只获得必要的只读研究/文档、图片检查、计算和 `submit_result` 能力，不获得
直接修改报告文件的工具。

完成标准：

- 错列、额外列、未知表、空单元格、无效 Evidence/Photo、漏提交适用表均被拒绝；
- Agent 能从批准子模块正文形成合法表格；
- 该合同不引用无状态内核项目中的未实现类型。

建议提交：`feat(reporting): add typed table-filler contract`

### T2：Cross → 表格填报 → Chief 编排和恢复

涉及：

- `manyselves/core/reporting/workflow.py`；
- 初始 full report 路径；
- revision report 路径；
- `FullReportCheckpoint` 及 resume 校验；
- cost boundary/activity 名称和进度通知。

新增活动建议：

```text
table-fill
table-fill-validation
revision-table-fill
revision-table-fill-validation
```

表格结果的输入摘要只要有一项变化就视为 stale。修订流程首版可重新填报全部适用
表，优先保证一致性；后续再按受影响子模块做增量优化。

完成标准：

- 没有 Cross completion 不能开始填表；
- 没有 table completion 不能开始 Chief；
- resume 能复用摘要完全一致的 table completion；
- 修改模块、catalog、Evidence 或照片后不能误用旧表；
- fail/waiting 状态保留当前 run，不自动另起 run。

建议提交：`feat(reporting): insert table-fill stage before chief`

### T3：从 Chief 移除表格作者权

涉及：

- `ChiefEditorInput` 增加 `table_fill_completion_ref` 或受保护表格包；
- Chief prompt 删除“提交 tables”的要求，改为不可修改的只读输入；
- `EditedReportSubmission.tables` 先进入兼容弃用，再从 Agent 输出中移除；
- `ReportAssetAssembler` 从 table completion 构建批准表格。

完成标准：

- Chief 返回表格字段会被拒绝，或兼容期只允许空数组；
- 即使 Chief 重写过渡文字，表格行、列、目标和锚点摘要不变；
- 表格内容不再依赖 Chief 是否在一次长 submission 中正确生成。

建议提交：`refactor(reporting): make filled tables runtime-owned`

### T4：最小子模块锚点和表内图片渲染

涉及：

- `manyselves/core/reporting/report_markdown.py`；
- `manyselves/core/reporting/rendering/pds_docx_renderer.py`；
- `manyselves/core/reporting/rendering/v2_docx_renderer.py`；
- 必要时为子模块正文增加稳定 block marker，但不要求重写全部正文模型。

推荐的兼容实现是运行时在 compose 阶段识别已验证的最小子模块标题，并在对应的
“现状描述”稳定标题/marker 前插入表格 token；不允许按模糊关键词选择章节。

表内图片要求：

- `photo_id` 对应的图片进入指定行和列；
- 等比缩放并限制单元格宽高；
- 保留 caption/替代文字及可审计 ID；
- 图片缺失或损坏时按表级规则显示 fallback，若规则要求图片必备则交付失败；
- 已进入业务表的图片不再重复进入独立图证汇总表。

完成标准：

- 表格只出现一次；
- 表格准确出现在目标最小子模块和固定锚点；
- Markdown 审计投影与 DOCX 的表名、列、行和图片对应；
- 多页表格、长文本、连续图片不越出版心。

建议提交：`feat(reporting): render filled tables at fixed leaf anchors`

### T5：全文审计、交付和版本

涉及：

- Final review input 加入只读表格包和校验摘要；
- final deterministic gate 增加表文一致性检查；
- delivery manifest、receipt、report version 增加 catalog/table completion 摘要；
- 错误必须指向 `definition_id`、row、column、Evidence/Photo。

完成标准：

- Final auditor 能看见表格内容和对应模块，但不能直接改表；
- 表文数值、位置或风险冲突时不能交付；
- 交付包可证明使用了哪个 catalog、哪次 table completion。

建议提交：`feat(reporting): audit and version filled table package`

### T6：回归与真实运行验收

最低测试矩阵：

1. 每张固定表的标题、列、触发和空表规则；
2. 有数据、全部正常、缺数据、部分空值、计算缺参、图片缺失；
3. `<50%` 有效列时的“数据不完整”标记；
4. 严重度/位置、超标率和行动优先级排序；
5. 同一 Evidence 的正文数值与表格数值一致；
6. 多张表落在同一最小子模块时的稳定顺序；
7. `2.3.1.1` 等深层叶节点的精确锚点；
8. full run、revision、resume 和 stale completion；
9. Chief 不能改变表格；
10. Markdown、DOCX、receipt 和 report version 一致。

建议命令：

```bash
.venv/bin/pytest -q tests/reporting/test_agent_workflow.py
.venv/bin/pytest -q tests/reporting/test_assets.py
.venv/bin/pytest -q tests/reporting/rendering/test_pds_docx_renderer.py
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q -m 'not integration'
.venv/bin/python -m compileall -q manyselves tests
git diff --check
```

真实交付只有同时满足以下条件才算完成：

- 当前 run 为 `completed`；
- 五模块、模块审查、Cross、table fill、Chief、Final 均有当前 run completion；
- 规则要求的全部表被生成或有合规 suppression notice；
- 产生新鲜、可读、位置正确且图片可见的 DOCX；
- delivery receipt 与 table/catalog/completion 摘要一致。

## 7. 交接方式

接手者按以下顺序阅读：

1. 本计划；
2. 表格规则 MD 的通用规则、逐表定义、执行流程、质量清单和附录；
3. `workflow.py` 的 full/revision Cross → Chief 两条调用链；
4. `TableSubmission*`、`ChiefEditorInput`、`EditedReportSubmission`；
5. `report_markdown.py`、`assets.py` 和两个 DOCX renderer；
6. 现有 table/photo/workflow 测试。

交接时必须附带：

- 实际实施分支、基线 commit 和 `git status --short`；
- catalog 覆盖矩阵及尚未解决的规则冲突；
- 每个工作包对应 commit；
- 测试命令和完整结果；
- 一个当前 run 的 table input/submission/validation/completion 示例；
- 最终 Markdown/DOCX/receipt 路径和 SHA-256；
- 未完成项、失败项和是否做过真实 Provider 运行。

当前主工作树在本计划编写时已有多项与 taxonomy、mapper、workflow、renderer 和
测试有关的未提交修改。接手者不得使用 `git reset --hard`、`git checkout --`
或清理命令覆盖这些变更；实施前必须记录实时状态，并在明确基线后使用独立分支或
独立 worktree。

## 8. 与无状态内核计划的协调

- 本项目不等待无状态内核，直接在当前报告运行时实现；
- 无状态化项目不得为了自己的抽象改变本计划的表格职责、位置或列；
- 两项目可以并行执行 TABLE-FILL 的 T0/T1 与 STATELESS 的 K0/K1；
- 两项目都将修改 workflow/contract 时，先冻结并合入表格阶段，再由无状态化项目
  将完整的 Cross → Table Fill → Chief 流程迁入 capability bundle；
- 任何联合验收都必须分别报告“表格功能是否完成”和“内核迁移是否完成”。
