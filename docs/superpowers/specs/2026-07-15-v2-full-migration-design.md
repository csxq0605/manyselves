# 配电报告 V2/V3 全量本地迁移设计

## 1. 目标与权威来源

本设计把 `work/local-multi-agent-report-plan.html` 作为产品目标规格，把以下资产作为现有业务行为与回归依据：

1. `work/运营专家-动态版-Harness直连版-0.2.6-版本历史展示版.yml`：现有 Dify/Harness 行为基线。
2. `work/配电安全报告工具V2-交接/插件源码目录` 与 `pds_report_tools_v3_dynamic-0.2.8.difypkg`：最新解析、质量检查、DOCX 渲染和 report state 实现资产。
3. `work/配电安全报告工具V2-交接/知识库源目录`：报告结构、证据、图片、KU、跨 KU、第三章和质量合同。
4. `work/写作上传材料/S2-1收资表.xlsx`、`S4-4诊断工作用表.xlsx`、`S4-6评估总表.xlsx`：Phase A 真实验收资料。

版本治理先冻结为“HTML 目标 + Harness 0.2.6 行为 + 插件 0.2.8 源码 + 当前知识库”。交接 README 仍指向 0.1.13，且 `0.2.3.difypkg` 内部标为 0.2.4，因此不能把文件名顺序当成语义版本真相。

## 2. 产品边界

- AutoReport 是唯一 GUI、会话、MessageBus、TaskBoard、工具和 Agent runtime。
- Nexgent 只贡献 Markdown/frontmatter 与 phase/pipeline/parallel 的配置思想，不接入第二套 runtime。
- 不复刻 Dify 画布；把有价值的资料状态、规则版本、章节推理和 DOCX 输出拆成边界明确的本地组件。
- `work/` 永远只读。运行产物只写当前客户项目的 `Work/` 与 `Outputs/`。
- GUI 仍只有项目文件、预览和主 Agent 对话，不新增证据仪表盘、Agent 管理页或 Skill 管理页。

## 3. 分层架构

### 3.1 Intake 与解析

`ProjectManifest` 为每个文件记录稳定 ID、SHA256、格式、用途、适配器、解析状态和错误。适配器按文件类型隔离：

- `SpreadsheetAdapter`：Excel、WPS DISPIMG、单元格、公式和嵌入媒体。
- `DocumentAdapter`：PDF/Word 的页、段、表与图片定位。
- `ImageAdapter`：图片元数据、OCR 结果和人工描述。
- DWG/视频在 Phase A 只记录未支持状态与人工摘要入口，不伪造解析结果。

三张核心表使用专用映射器：

- S2-1：资料项、具备情况、有效性和缺口。
- S4-4：现场问题明细、设备/位置/柜号、测量值、同列或相邻列图片绑定。
- S4-6：固定 taxonomy、汇总结论、建议、优先级和跨表对照，不作为现场图片或问题位置的首选来源。

### 3.2 证据与覆盖

`EvidenceItem` 是唯一可证明客户现场事实的载体。必须包含：来源文件、工作表/页码、单元格或行列、对象、事实、状态、数值/单位、时间、图片引用、可信度、待核实标记及目标 module/submodule。

方法论、FAQ、KU 和写作规则不能成为客户事实。它们通过 `ModuleSkill` 告诉 Worker 如何判断。

`CoverageMatrix` 按固定子模块计算 `ready/pending/blocked`：

- `ready`：必需事实满足，可以形成确定性结论。
- `pending`：可以生成带待核实标记的草稿。
- `blocked`：没有形成该子模块最低限度判断所需的证据。

缺资策略由 `ReportRequest.missing_evidence_policy` 决定 `ask/skip/draft/block`，不能用“有任意 Excel 行”替代覆盖判断。

### 3.3 模块写作与审校

Planner 为 2.1-2.5 及子模块创建独立 `ModuleTask`，每个任务只得到相关 EvidenceItem、KU、写作合同、预算和待核实项。

五个模块 Worker 分别拥有产业模块，不按“现状/风险/建议”横向拆 Agent。每个 `Claim` 保存事实、判断、风险机制、建议、evidence_ids、skill_version 和状态。

审校顺序：

1. 确定性 schema/引用/阈值检查。
2. Evidence Auditor 检查 claim 与证据是否匹配。
3. Revision Router 只返工责任子模块，最大两轮。
4. Cross-module Reviewer 检查设备名、数值、等级和行动冲突。
5. Chief Editor 只改结构与表达，不改受保护 claim 与 evidence_ids。

### 3.4 ReportState 与 DOCX

`ReportState` 保存固定目录树、批准模块、问题、图片 manifest、表格模型、图表模型、第三章汇总、规则/模型版本和审校历史。

本地 Renderer 从交接包模板和渲染规则迁移，但只消费已校验的 ReportState，不继续承担专业判断。输出包括：

- `Outputs/Reports/配电安全专家咨询报告.docx`
- `Outputs/Reports/render-log.json`
- 模块草稿、审校记录与运行摘要。

生成后执行结构检查、DOCX 打开检查、图片/表格检查，并渲染每页 PNG 做视觉回归。

## 4. 分阶段交付

### Phase A：2.4 纵向样板

必须真实读取三张核心工作表，形成 2.4 子模块证据，绑定 S4-4 DISPIMG 图片，完成一次缺资分支、一次局部返工，并生成可打开的 DOCX。以下均为门禁：

- 96.99% 负荷率只能是 80%-100% 关注项，不能写成 >100% 过载/火灾既成事实。
- 问题行的地点、设备、柜号和图片必须来自同一证据上下文；缺图写“待补充照片”。
- 重要/定量/法规 claim 必须有 evidence_ids。
- 无关模块在局部返工后保持不变。
- DOCX 目录不重复，第三章标题无 Markdown 残留，表格与图片不溢出。

### Phase B：五模块完整报告

扩展全部 taxonomy 与 ModuleSkill，运行五模块并行、跨模块审查、总编、第三章与完整 DOCX。一次对话可稳定生成完整报告。

### Phase C：受控能力完善

专家修改先写 `SkillCandidate`，绑定失败样本与适用范围；回归评测通过且用户确认后才发布 `SkillVersion`。支持查看历史与回退，旧报告继续记录原 Skill 版本。

## 5. 测试与完成定义

- 所有新行为严格 RED-GREEN-REFACTOR。
- 单元测试覆盖解析映射、DISPIMG、阈值、coverage、claim 审校、返工、ReportState 与渲染。
- 集成测试使用 `work/写作上传材料` 的只读副本进入项目 `Inputs/`，验证真实输出。
- DOCX 必须结构检查和逐页视觉检查同时通过。
- 完成审计逐条映射 HTML 节点、迁移矩阵、A/B/C 门禁和交接约束；任何缺失都保持目标未完成。

## 6. 明确不采用的方案

1. **直接复制 0.2.8 单体插件**：速度快，但把 Dify 平台补丁、HTTP 状态和 4000 行单体解析器继续耦合，无法形成目标的可审计本地架构。
2. **把 Dify 插件作为子进程调用**：短期复用多，但保留第二套状态和工具 schema，不符合 AutoReport 单一 runtime。
3. **仅扩写现有占位 handler**：会继续让配置化 Agent 名存实亡，也无法证明五模块真实隔离和局部返工。

采用适配器式迁移：以交接实现为行为参考，按目标载体与职责重新组织，并通过同一批真实资料做对照回归。
