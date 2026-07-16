# AutoReport 配电安全专家多智能体系统设计

日期：2026-07-15  
状态：已与用户逐节确认并完成规格审阅
目标分支：`feature/v2-phase-a-migration`

## 1. 背景与目标

当前仓库虽然新增了配电报告模型、模板和工作流文件，但活跃运行链仍保留物理实验报告的固定 Agent 类型，新增模块正文又主要由确定性 Python Worker 拼接。结果是系统表面上完成了领域迁移，实际没有形成由配电专业 Agent 主动查证、分析、协作和写作的多智能体系统。

本次改造的目标是：以 AutoReport 现有 PyQt GUI、AgentLoop、MessageBus、TaskBoard、Provider 抽象和项目文件能力为底座，参考 Nexgent 的独立 Agent Loop、Markdown + YAML frontmatter 身份、`phase / parallel / pipeline` 工作流表达和隔离通信方式，建立配电安全专家报告多智能体系统。

系统必须产生自然、完整、丰富、多样且具有专业分析的报告，同时保持关键判断可溯源。可审计结构保存在后台，不能反过来把正文强制写成重复的“事实—证据—风险—建议”模板。

## 2. 设计依据

### 2.1 业务计划

`/Users/zzymima0000/Documents/Codex/work/local-multi-agent-report-plan.html` 是目标架构和实施边界的业务基线：

- AutoReport 是唯一代码底座，保留 PyQt GUI；
- 不接入 Nexgent 运行时，只借鉴其身份、Loop、编排和资源治理方式；
- 用户只与 Main Agent 对话；
- 固定 2.1–2.5 报告目录，模块 Agent 对完整专业论证负责；
- 项目证据、专业参考和写作能力必须分层；
- 支持并行模块写作、证据审查、跨模块审查、总编、局部返工和 DOCX 交付。

### 2.2 Claude 提示词原则

身份设计遵循 Claude 官方 Prompting Best Practices：

- 角色、任务、上下文、工具和完成标准清晰直接；
- 解释目标和动机，优先告诉模型要做什么；
- 复杂上下文使用明确、稳定的分层；
- 示例应相关、多样，避免模型学习单一表面模式；
- 复杂推理优先提供总体目标与判断原则，不手写僵硬推理步骤；
- Agent 自主选择工具，工具结果返回后继续反思和行动；
- 多智能体通过独立上下文、任务、结果和受控消息协作。

参考：<https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices>

### 2.3 Nexgent 参考范围

借鉴而不复制以下机制：

- 单 Agent 模型—工具循环；
- 子 Agent 独立 Session、工具范围和资源预算；
- Markdown + YAML frontmatter 身份定义；
- 外层 `phase / parallel / pipeline` Workflow；
- 任务输入、阶段结果、共享成果和消息通道式协作。

参考源码：

- <https://github.com/csxq0605/Nexgent/blob/main/nexgent/nexgent/agent.py>
- <https://github.com/csxq0605/Nexgent/blob/main/nexgent/nexgent/subagent.py>
- <https://github.com/csxq0605/Nexgent/blob/main/nexgent/nexgent/workflow.py>
- <https://github.com/csxq0605/Nexgent/blob/main/nexgent/nexgent/agents.py>
- <https://github.com/csxq0605/Nexgent/blob/main/nexgent/examples/workflow_code_review.py>

## 3. 知识与事实边界

### 3.1 项目事实

客户现场事实只能来自本项目上传资料或人工确认，统一形成 `EvidenceItem`。项目事实包括检测值、设备、位置、时间、工作表、单元格、页码、图片和制度文件执行记录。

项目事实使用 `E-*` 标识。模型知识、本地参考和网络资料均不能替代客户现场证据。

### 3.2 导入选择与项目 `Knowledge/`

交接资料中的参考文档源目录是一次性导入来源。导入时只选择计划作为参考资料的内容复制到项目 `Knowledge/`；这一选择不成为运行时目录协议。

进入运行时后，`Knowledge/` 是唯一参考边界。其下任意名称、任意层级中的支持文件都可被 Agent 主动搜索、登记为 `R-*` 并引用；程序不得根据导入前的目录名、编号、文件名或标题做白名单、黑名单或名称匹配。

`Knowledge/` 的定位类似联网搜索，而不是身份、system prompt、隐藏规则包或每轮固定注入的模块上下文。

Agent 可以根据当前问题搜索 `Knowledge/`，用它：

- 扩展专业知识框架；
- 查找评估方法、阈值、术语、风险机制和标准线索；
- 检查是否遗漏重要关联；
- 评价报告是否覆盖必要的专业维度；
- 校准或补充模型已有知识。

Agent 必须判断检索结果是否适用于当前项目，不得直接复制知识库中的项目结论或固定句式。Knowledge 来源进入 `SourceLedger`，使用 `R-*` 标识，不进入 `EvidenceItem`。

### 3.3 未导入资料与模块 Skill

未被导入选择的本地提示词、旧案例或客户化材料应留在项目 `Knowledge/` 之外。它们不进入运行时，仅因为没有被复制进项目参考边界，而不是因为运行时识别某个目录名称并实施特殊禁用。

需要保留的通用质量规则必须离线抽象、匿名化、评测后写入仓库打包的模块 Skill；模块 Skill 由专门加载器按模块注入 Specialist 和 Auditor，不混入 `Knowledge/` 引用库。

因此边界是：

- `Knowledge/` 内任何内容都可以作为参考来源和引用；
- `Knowledge/` 外的交接资料不会被 ReferenceLibrary 扫描；
- 模块 Skill 是打包、版本化、按任务注入的能力，不通过名称匹配从 Knowledge 中挑选；
- 客户名、位置、柜号、数值、图片和原始示例不得从旧提示词资料复制进通用 Skill。

### 3.4 网络参考

模块 Agent 可按需调用联网搜索与网页读取工具。联网用于补充更新的标准、机构资料、制造商文档、行业研究、事故机理和技术解释。

网络来源使用 `W-*` 标识，记录机构、标题、发布日期、URL、访问日期和适用范围。网络资料不能弥补客户现场事实缺失。

## 4. 总体架构：双层 Loop

### 4.1 单 Agent Loop

所有需要模型理解、判断、研究或写作的角色都运行独立 `AgentLoop`：

1. 使用稳定身份文件、`TaskEnvelope` 和允许工具创建独立 Session；
2. 模型理解任务并自主选择下一步；
3. 若模型调用工具，系统执行安全可并行或必须顺序的工具；
4. 工具结果写回同一 Session；
5. 模型根据结果继续搜索、读取、计算、沟通、分析或写作；
6. Agent 通过 `submit_result` 提交类型化成果，或通过 `report_blocked` 报告阻塞；
7. 达到最大步骤、时间、Token 或修订轮次时，保留已有成果和未解决问题，不伪装成功。

是否搜索 `Knowledge/`、是否联网、是否向其他 Agent 提问、是否直接开始写作以及何时结束，由 Agent 在自己的 Loop 内决定。外层程序不得把这些选择实现成固定研究状态机。

如果模型自然结束但没有提交工作流成果，Loop 只提醒一次整理类型化结果；仍未提交则保存原始输出并标记 `incomplete`。

### 4.2 Report Workflow Loop

外层 Workflow 只负责任务阶段、依赖、并行、Pipeline、汇合、预算、恢复和局部返工，不承担专业推理。

```text
Phase 1  Main Agent：用户消息 → ReportRequest
Phase 2  Manifest → Parser → Evidence Normalizer → Coverage Evaluator
Phase 3  Report Planner → 五个 ModuleTask
Phase 4  五条模块 Pipeline 并行：Module Expert → Evidence Auditor → 局部修订
Phase 5  全部模块通过后：Cross-module Reviewer
Phase 6  Chief Editor → ReportState + CitationPlan
Phase 7  DOCX Renderer → 版式校验 → 项目文件与对话交付
```

每条模块 Pipeline 独立推进。某一模块完成后可以立即进入自己的审计和修订，不必等待其他模块。Cross-module Reviewer 前设置全局汇合屏障。

### 4.3 Session 隔离与共享状态

Agent Session 只保存自身身份、任务、工具调用、工具结果和与自己相关的消息。不同 Agent 不共享完整聊天历史。

跨 Agent 可见内容必须先保存为项目状态或类型化成果：

- `ProjectManifest`
- `ParsedArtifact`
- `EvidenceItem`
- `CoverageMatrix`
- `ResearchNote`
- `ModuleDraft`
- `ClaimLedger`
- `SourceLedger`
- `ReviewIssue`
- `ReportState`
- `ReportVersion`

Reviewer 和 Chief Editor 读取成果与引用，不继承模块 Agent 的完整会话，从而保持独立判断。

## 5. Agent Registry 与身份格式

身份使用 Markdown + YAML frontmatter。frontmatter 是运行配置，Markdown 正文是稳定角色提示词。

```yaml
---
name: module-2.4-specialist
description: 配电设备与元件风险诊断专家
model: inherit
tools:
  - search_project_evidence
  - search_reference_library
  - web_search
  - open_source
  - inspect_document
  - inspect_image
  - calculate
  - query_peer
  - submit_result
disallowedTools: []
maxTurns: 12
effort: high
memory: task
background: true
---
```

身份正文统一覆盖但不限于：

- `role_and_perspective`
- `mission`
- `default_posture`
- `owned_decisions`
- `tools_and_loop`
- `collaboration`
- `completion_standard`
- `deliverables`

身份文件不能内嵌 `01`、`02`、客户资料或固定报告句式。动态 `TaskEnvelope`、证据入口、共享状态和协作消息由 PromptAssembler 放入独立 XML 上下文区域。

## 6. Agent 身份与责任

### 6.1 Main Agent

项目负责人式身份。唯一面向用户，负责理解真实意图、区分本轮要求与长期能力、启动 Workflow、处理补资和歧义、反馈进度并交付成果。Main Agent 不代替模块专家作专业判断。

### 6.2 Report Planner

咨询项目经理兼技术负责人。先识别报告必须回答的专业问题，再拆分责任、依赖、并行关系、资源预算和完成标准。Planner 固定报告目录，但不规定每个 Agent 的搜索步骤、工具次数或段落模板。

### 6.3 Intake & Parser

资料工程师。关注文件用途、结构、版本和解析异常；自主选择解析器、切换工具或请求人工说明；只生成 `ParsedArtifact`，不评价安全状态。

### 6.4 Evidence Normalizer

技术资料核验员。关注对象、时间、测点、单位、版本、同义字段和跨文件关系；生成 `EvidenceItem`、实体关系、冲突和可信度；不能用常识填补不存在的现场数据。

### 6.5 2.1 配电系统架构专家

从供电拓扑、负荷路径、备用能力、切换关系和系统耦合分析容量余度、结构性风险和单点故障传播。

### 6.6 2.2 环境工况专家

从电能质量和状态诊断视角综合测量条件、时间变化、负荷工况、温升、局放、温湿度、粉尘和进水等因素，避免只按阈值贴标签。

### 6.7 2.3 故障保护专家

沿故障电流路径、动作时序和上下级配合分析保护方案、定值、选择性、漏电和过欠压保护；缺少关键定值时准确限定分析边界。

### 6.8 2.4 设备与元件专家

将铭牌、图片、安装状态、检测数据和运行后果联系起来，分析容量选型、连接、闭锁、接地、防护等级、剩余电流和带病运行，保证设备、位置、图片和问题对应。

### 6.9 2.5 运维管理专家

区分“有制度”“有记录”和“有效执行”，分析组织、人员、SOP/EOP、LOTO、维护、备件、智能化和生命周期管理的闭环。

### 6.10 Evidence Auditor

独立技术审计人员。建设性地质疑论断强度是否超过证据，检查事实来源、阈值适用性、推断边界和脚注覆盖。Auditor 只能退回具体问题，不能要求所有模块采用相同句式。

### 6.11 Cross-module Reviewer

总工程师视角。检查设备命名、数字、等级、行动优先级、跨模块叠加效应、矛盾和遗漏。发现问题后向责任模块发送 `PeerQuery` 或 `ReviewIssue`，不直接覆盖专业结论。

### 6.12 Chief Editor

技术咨询报告主笔。面向管理层和电气专业读者整合全文，调整叙事、节奏、详略、过渡和重复内容，保留不同模块的专业表达形态。不得修改已批准的事实、数值、风险等级和来源语义。

### 6.13 确定性组件

以下组件不设置 LLM 人格：

- Coverage Evaluator
- Revision Router
- Claim Ledger / Citation Builder
- DOCX Renderer
- Project Delivery

## 7. 工具体系

### 7.1 研究与证据工具

- `search_project_evidence`
- `open_project_source`
- `search_reference_library`
- `open_reference`
- `web_search`
- `open_web_source`
- `inspect_document`
- `inspect_image`
- `calculate`
- `publish_research_note`

`search_reference_library` 搜索整个项目 `Knowledge/`。`web_search` 和 `open_web_source` 必须保存可引用元数据。研究工具是否调用由 Agent 决定。

### 7.2 协作与完成工具

- `query_peer`
- `reply_peer`
- `report_gap`
- `report_blocked`
- `submit_result`
- `request_revision`

不同身份只获得完成任务所需的工具。工具描述应说明适用条件，不使用“任何情况下必须调用”的过度提示。

## 8. 消息协议

AutoReport 现有 MessageBus 继续作为通信基础。消息至少包含：

- `message_id`
- `workflow_id`
- `task_id`
- `sender`
- `recipient`
- `message_type`
- `priority`
- `requires_reply`
- `artifact_refs`
- `content`
- `timestamp`

主要消息类型：

- `TaskEnvelope`
- `ProgressNote`
- `PeerQuery`
- `PeerReply`
- `ResearchNotePublished`
- `ReviewIssue`
- `RevisionRequest`
- `BlockedNotice`
- `AgentResult`

消息只传递问题、摘要和 Artifact ID；长篇正文、证据和研究资料保存在共享项目状态中。Agent 发现其他模块的 `ResearchNote` 后自主决定是否采用，采用时必须登记来源。

## 9. 自然写作与后台审计

### 9.1 双输出

模块 Agent 同时提交：

1. `ModuleNarrative`：面向读者的自然连续正文；
2. `ClaimLedger`：隐藏的关键判断、证据、参考来源、不确定性和审校状态。

后台结构不决定正文表面顺序。正文不得逐条输出 Claim 字段，也不固定为“现状—结论—风险—建议”。

### 9.2 写作自由

固定的是 2.1–2.5 目录和必要覆盖，不是段落模板。模块可根据问题性质：

- 从系统结构或典型问题切入；
- 比较时间、测点和工况；
- 提出并比较多个解释；
- 进行故障场景推演；
- 对照制度、记录和现场行为；
- 将多个问题组织成系统性风险链；
- 在资料不足时准确说明可判断边界。

不是每个 KU 都必须分别生成结论、风险和建议。相关内容可合并；没有实质风险或行动价值时不强行扩写。

### 9.3 模型内部知识

模型可使用内部配电知识提出假设、理解现象、寻找关联、形成专业解释和建议，并据此判断是否需要搜索 `01` 或网络。

内部知识不能被描述成客户事实。阈值、标准条款、制造商参数、争议性机理或可能变化的信息一旦成为关键判断依据，应补充可引用来源。

### 9.4 Chief Editor 的边界

Chief Editor 可合并背景、调整段落顺序、改变详略、增加过渡、消除模板句和组织主题。已批准 Claim 的事实、数值、风险等级和来源是受保护语义；发现专业冲突时必须退回责任模块或 Reviewer。

## 10. 脚注和证据索引

采用自然正文加关键脚注的方案。

应生成脚注的内容包括：

- 关键现场数值和检测结果；
- 设备、位置、时间和状态判断；
- 高风险结论；
- 标准阈值和法规判断；
- 制造商技术参数；
- 重要因果机制；
- 决定整改优先级的依据。

正文脚注连续编号。文末索引分为：

- 项目证据 `E-*`：文件、工作表/页码/单元格、对象和日期；
- 本地参考 `R-*`：`01` 文件、章节和版本；
- 网络来源 `W-*`：机构、标题、发布日期、URL 和访问日期。

图片和表格进入同一来源体系。Chief Editor 改写后，Citation Builder 必须验证脚注仍绑定正确语义。

## 11. DOCX 渲染

复用：

`/Users/zzymima0000/Documents/Codex/work/配电安全报告工具V2-交接/插件源码目录/core/docx_renderer.py`

改造原则：

- 去除 Dify 包装，增加 AutoReport 工具适配层；
- Renderer 只消费已批准 `ReportState`、表格、图片、脚注计划和模板；
- 限制或关闭内部内容生成兜底，避免覆盖 Agent 已批准分析；
- 继续复用标题、目录、表格、图片、页眉页脚和样式能力；
- 渲染前后验证受保护语义、脚注、图片和表格完整性；
- 渲染失败保留 `ReportState` 和日志，不发布成功状态。

## 12. 异常与恢复

- 单文件解析失败：保留错误记录，其他资料继续处理；
- 工具失败：错误返回原 Session，Agent 自主重试、换工具或报告影响；
- 联网失败：若项目证据和本地参考足够则继续，否则标记缺少外部依据；
- 参考冲突：保留观点和适用条件，由 Agent 或 Reviewer判断；
- 缺少客户事实：请求补资或标记待核实，禁止用网络补齐；
- Agent 超时或预算耗尽：保存 ResearchNote、草稿和未解决问题；
- Auditor 退回：恢复责任 Agent 原 Session，不重跑无关模块；
- 超过最大修订轮次：转 Main Agent 向用户说明争议和所需决定；
- 关键 Claim 无项目证据或脚注缺失：阻止模块进入 Approved；
- DOCX 失败：不产生成功交付消息。

## 13. 迁移范围

### 13.1 保留

- PyQt GUI、项目文件树、预览器和对话入口；
- Provider 抽象；
- AgentLoop 的模型—工具循环基础；
- MessageBus、TaskBoard、检查点和局部恢复；
- 项目目录隔离和文件权限；
- GUI 工具调用、状态和结果反馈。

### 13.2 替换

- 删除活跃运行链中的固定物理 Agent 类型；
- PromptLoader 改为动态身份发现；
- LoopManager 改为按 Workflow 实例化具名 Agent；
- 工具权限改为按身份和成果类型配置；
- 移除固定字符串 Worker 的正式写作职责；
- 将旁路式 ReportingService 改为真正驱动 AgentLoop 的 Workflow。

### 13.3 新增

- Agent Registry 和 PromptAssembler；
- Report Workflow Runner；
- 类型化协作消息；
- 项目证据、`01`、网络、图片、计算和来源工具；
- Claim Ledger、Source Ledger 和 Citation Plan；
- 交接包 DOCX Renderer 适配器。

## 14. 实施切片

### 14.1 第一阶段：真实纵向样板

- 动态身份加载；
- Planner、2.4 专家和 Auditor 的独立 Loop；
- 项目证据、`01` 检索、联网检索和消息工具；
- 自主检索、无需检索直接完成和局部退回三种轨迹；
- 自然模块正文、Claim Ledger、脚注和 DOCX；
- GUI 对话触发及文件树交付。

### 14.2 第二阶段：完整五模块报告

- 五模块并行 Pipeline；
- PeerQuery 和共享 ResearchNote；
- Cross-module Reviewer；
- Chief Editor；
- 全文脚注、证据索引、表格与图片；
- 对话驱动的责任 Agent 局部修改。

### 14.3 第三阶段：受控能力改进

- 专家修改转匿名评测样本和候选能力；
- `02` 只服务离线设计和评测；
- 评测通过并经用户确认后才发布身份或能力更新；
- 身份、模型、参考和报告版本可复现。

## 15. 测试策略

### 15.1 身份和上下文隔离

- frontmatter 正确发现所有身份；
- `01` 不出现在 system prompt；
- `02` 不被运行时 Loader 读取；
- Agent 只获得允许工具；
- 不同 Agent Session 相互隔离。

### 15.2 单 Agent Loop

使用可控模型响应验证：

- 不研究直接完成；
- 主动搜索 `01` 后完成；
- 联网搜索并打开来源后完成；
- 向其他 Agent 提问并根据回复继续；
- 工具失败后换工具；
- 无 `submit_result` 时只恢复一次并正确标记 incomplete。

测试验证选择权和 Loop 语义，不规定 Agent 必须走某条搜索路径。

### 15.3 多智能体 Workflow

- 模块真正并发；
- 单模块完成即可进入审计；
- ReviewIssue 只恢复责任 Agent；
- Cross-module Reviewer 前存在汇合屏障；
- 失败、超时或预算耗尽不丢失其他模块成果；
- Workflow 可从持久化状态恢复。

### 15.4 证据与引用

- 项目事实只引用 `E-*`；
- `01`、网络分别进入 `R-*`、`W-*`；
- 高风险、定量和标准判断具有脚注；
- 脚注定位到文件、页码、工作表、单元格或 URL；
- 网络资料不能转为客户 EvidenceItem；
- 总编改写后脚注仍绑定正确语义。

### 15.5 DOCX

- 使用交接包模板和渲染核心；
- 标题、目录、脚注、表格和图片正常；
- 图片与现场问题对应；
- Word/WPS 可打开；
- 渲染前后受保护内容一致；
- 失败时不发布成功状态。

### 15.6 报告质量评测

使用从 `02` 离线匿名化的正例、反例和边界样本，评价：

- 是否分析而非复述；
- 是否合理使用模型内部知识；
- 是否在需要时主动搜索 `01` 或网络；
- 是否考虑替代解释和跨模块影响；
- 五模块是否具有不同专业论证形态；
- 是否存在模板句和重复骨架；
- 是否完整、丰富、自然；
- 建议是否有对象、条件、动作和目的；
- 关键判断是否可溯源；
- 未知信息是否诚实表达。

不以固定字数、逗号数量、句式或复现示例措辞作为质量标准。

## 16. 完成标准

只有同时满足以下条件，迁移才完成：

- 活跃链不再启动旧物理实验 Agent；
- 原 GUI 对话可以启动报告；
- 每个专业 Agent 运行真实、独立的工具 Loop；
- `01` 是自主检索参考，`02` 不进入运行时；
- Agent 可自主决定直接写作、补充本地参考、联网或结束；
- 五模块并行协作，支持定向通信和局部返工；
- 正文不由固定字符串 Worker 生成；
- 报告具有主动分析、自然表达和模块专业差异；
- 关键判断通过脚注和证据索引可追溯；
- DOCX 沿用交接包渲染能力；
- 自动测试、匿名样本评测和人工 DOCX 检查全部通过。

## 17. 非目标

- 不接入 Nexgent 运行时；
- 不新增独立报告管理后台；
- 不把 `01` 变成身份或强制提示词；
- 不把 `02` 导入任何运行时知识库；
- 不要求模型暴露私有思维链；
- 不用结构化 Claim 字段控制正文表面句式；
- 不让 Renderer 重新进行专业分析；
- 不自动把一次用户修改发布为长期能力。
