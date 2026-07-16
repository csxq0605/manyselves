# 配电报告工作流对齐设计

## 目标

将当前五模块 Agent 技术样板收口为 `work/local-multi-agent-report-plan.html` 描述的可交付工作流，同时落实 2026-07-16 两轮审查确认的修正：运行时知识库只认项目 `Knowledge/`，不再识别 `01/02` 名称；所有生产依赖、状态和输出均位于仓库或当前项目内。

## 已批准的边界

- GUI 仍只有一个面向用户的 Main Agent；专业角色按报告任务临时运行。
- 项目事实、知识参考和网络来源继续分别记为 E-*、R-*、W-*。这一区分表达来源语义，不依赖目录名称。
- 交接资料中的 `01/02` 仅是导入阶段的选择：导入器复制 01 的内容到项目 `Knowledge/`，不复制 02。运行时不保留任何 01 白名单或 02 黑名单。
- `Knowledge/` 下任何受支持文件都可以被递归搜索、打开、登记为 R-* 并用于引用；安全边界是解析后的路径必须仍位于项目 `Knowledge/`。
- `Inputs/` 才能形成客户现场事实。Knowledge 内容可以支持方法、阈值、标准和解释，但不能单独证明客户现场状态。
- DOCX 渲染核心必须随 AutoReport 包发布；显式测试覆盖可注入替代核心，但生产默认不得依赖仓库外绝对路径。
- 2.1—2.5 及其固定子模块由程序合同约束。Agent 决定论证，但不得改变目录或让未请求、待核实和阻塞状态消失。
- 局部返工以 submodule_id 为最小保护边界；未被点名的子模块正文、Claim 和来源引用必须保持不变。
- Chief Editor 只能调整结构与表达。所有已批准 Claim 必须出现在保护清单中，并能在编辑稿中唯一定位。
- Phase C 采用项目内文件资产：候选、评测、发布版本和回退记录均写入项目，不增加 GUI 管理页面。

## 架构

### 1. 知识导入与运行时参考库

导入器负责一次性选择；`ReferenceLibrary` 只负责运行时索引。运行时根目录固定为 `<project>/Knowledge`，支持递归读取文本、Markdown、HTML、JSON、CSV、DOCX 和 PDF。所有打开操作使用 `Path.resolve()` 与 `is_relative_to()` 校验，禁止逃逸，但不检查文件夹名称。

`SourceLedger.register_local()` 接受任意 `Knowledge/...` 定位；`ClaimLedger` 只校验 R-* 对应的 SourceRecord 位于 Knowledge，不再出现 `01_页面导入知识库` 或 `02_本地skill提示词资料_禁止导入` 字面规则。

### 2. 收资与证据

Manifest 扫描后由适配器注册表按格式解析。三张核心 Excel 继续走专用 mapper；普通 XLSX、DOCX、PDF、文本和图片形成保留定位的 ParsedArtifact。DWG 和视频在没有本地解析器时形成 `manual_required` 结果而不是伪装成 Excel 或静默丢失。CoverageMatrix 仍是确定性门禁。

### 3. 请求控制

`ReportRequest` 的四项语义必须进入控制面：

- `target_modules` 决定 Planner 和实际启动的 Specialist；
- `execution_requirements` 进入 Planner 与每个模块任务约束；
- `missing_evidence_policy=ask|block` 在启动专业 Agent 前返回结构化 blocked 结果和缺口；
- `draft|skip` 分别要求显式待核实稿或跳过无证据子模块。

完整五模块请求继续进入跨模块审查、总编和 DOCX。局部请求只发布目标模块及审校结果，不冒充完整 DOCX；后续完整运行可消费已发布模块作为输入。

### 4. Agent 编排与状态

Python `ReportWorkflowRunner` 保留清晰的控制流，但必须读取 YAML 中的 pipeline agent、revisionAgent 和 maxRevisions，避免声明与执行漂移。每个 Agent 任务同步写入 TaskBoard，并在完成、阻塞或失败时更新状态。阶段 checkpoint 继续写入 `Work/runs/<run_id>/workflow-state.json`。

### 5. 模块 Skill

新增只读 `ModuleSkillLibrary`，从随包发布的 `templates/reporting/skills/<module>/` 加载版本化 Markdown。Planner 得到 Skill 索引；Specialist 和 Auditor 得到责任模块的完整 Skill 文本。Skill 放在独立 XML 上下文，不与用户输入或客户资料混合。

### 6. 子模块与返工保护

`ModuleSubmission` 必须包含固定 `submodule_narratives`，`ClaimRecord` 必须标注合法 submodule_id。审计 blocking issue 必须指出 submodule_id。返工后程序比较前后提交：目标子模块允许变化，其余子模块正文、Claim 和来源集合必须完全一致，否则拒绝结果。

### 7. 编辑、图片、表格和 DOCX

Chief Editor 提交完整五模块和固定子模块内容、Claim 保护集合、引用锚点、结构化表格以及照片放置。程序将 `photo-manifest.json` 与 EvidenceItem.photo_refs 解析为 ReportPhoto，将表格来源和 Claim 绑定为 ReportTable。渲染前验证所有资产和来源；渲染后验证 Word/WPS 可打开、批准正文未丢失、引用和图片 token 已物化。

### 8. 受控能力改进

修改请求可生成未发布 SkillCandidate，记录原稿、修改稿、原因、责任模块、失败类型和样本。EvaluationResult 记录基线、候选、模型、配置及评分；只有评测通过且用户明确确认时才写入 approved SkillVersion。回退只切换 manifest 指针，不删除历史版本。

## 错误处理

- 缺资、Agent 主动阻塞和达到返工预算返回 `blocked`，并包含模块、子模块和所需资料；基础设施异常返回 `failed`。
- 单文件解析失败保留 manifest 错误和 ParsedArtifact 状态，不阻塞无关文件。
- 局部报告不生成完整 DOCX；完整报告缺少任一已批准模块时不得进入总编。
- 外部网页和本地 Knowledge 都不能被登记为客户现场证据。
- 渲染或交付失败不得发布 success manifest，也不得覆盖上一版成功交付。

## 验收

- 报告子系统测试覆盖 Knowledge 任意子目录、路径逃逸、请求范围、深度要求、四种缺资策略、子模块保护、图片表格接线、Skill 注入、YAML revision budget 和 TaskBoard 状态。
- 完整脚本 Agent 测试验证五模块并行、独立审计、barrier、跨模块返工、Chief Editor 保护、仓库内 DOCX 和项目内交付。
- 运行报告子系统、全仓离线测试、Ruff、`git diff --check` 和包构建；真实外部模型测试单独列明授权和限流条件，不用离线 writer 代替。

## 非目标

- 不新增证据仪表盘、Agent 管理页或 Skill 管理页。
- 不把 Knowledge 内容自动转成客户事实。
- 不在本轮实现 DWG 几何解析或视频内容理解；只提供明确的人工处理状态和后续适配接口。
- 不恢复已删除的确定性专业写作链。
