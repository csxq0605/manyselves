# Manyselves 表格填报与无状态内核交接 TODO

> 文档状态：当前实施入口，可直接交接
> 审计日期：2026-08-12
> 分支：`docs/handoff-todo-20260812`
> 基线：`main@3babc0ca079fe15337b09f0ec4911bf638c39ae5`
> 基线标签：`success/reporting-framework-20260801`
> 表格规则：`/Users/zzymima0000/Downloads/表格数据填充规则_V2.0_完整版.md`
> 规则文件 SHA-256：`6c90ecdd6e286ad39e5ac27e9f985fca07cca9bb36c897645e7b32c5849f6125`
> （文件正文标题为 V2.1，大小 37,338 bytes）

本文件是两项工作的统一交接清单。已有的
`docs/table-filling-stage-handoff-plan.md` 和
`docs/stateless-kernel-handoff-plan.md` 保留为历史调研材料；如果其中的现状判断、
顺序或接口与本文件冲突，以本文件和接手时的实时代码审计为准。

## 0. 必须遵守的边界

这两项工作是两个独立项目：

1. 表格填报是在现有报告业务中加入 `Cross → Table Fill → Chief`；
2. 无状态化是把身份、功能、工具授权、交互、编排和运行状态从领域代码迁出。

表格填报不等待无状态内核。无状态化也不得改变表格标题、列、内容要求、目标位置
或填报责任。最终只是在无状态化迁移时，把已经验收的表格阶段当作一个完整业务
stage 迁入 capability bundle。

实施纪律：

- [ ] 开工前记录 `git worktree list`、分支、HEAD、`git status --short`；
- [ ] 使用独立分支/worktree，不清理、不重置其他工作树；
- [ ] 每个工作包单独提交，提交信息对应本文件的 TODO 编号；
- [ ] 离线测试、真实 Provider 运行和完整 DOCX 交付分别报告；
- [ ] 失败 run 停在原 run 上取证并等待指令，不自动创建新 run；
- [ ] 不以“类/Schema 已存在”代替端到端行为验收。

共享热点文件包括：

- `manyselves/core/reporting/workflow.py`；
- `manyselves/core/reporting/agent_runner.py`；
- `manyselves/core/reporting/agentic_models.py`；
- `manyselves/core/reporting/input_contracts.py`；
- `manyselves/core/reporting/submission_contracts.py`；
- `manyselves/core/tools/reporting_collaboration_tools.py`。

两组开发者不得同时改这些文件。推荐顺序是：表格 T0/T1 与无状态 K0/K1 可以
并行；表格 T2～T6 先合并并冻结行为；然后无状态化再迁移完整流程。

---

## 第一部分：固定表格填报阶段

### 1.1 当前代码事实

截至基线 `3babc0c`：

- 完整报告与修订报告仍是 Cross 完成后直接进入 Chief：
  `ReportWorkflowRunner.run()`、`run_revision()`、`_cross_review()`、
  `_chief_edit()`；
- 没有 `table-filler` 身份，没有 `TableFillInput/Submission/Completion`；
- `EditedReportSubmission.tables` 仍由 Chief 提交；
- `TableSubmission` 只有标题、表头、字符串行、Source/Claim，不能表达稳定表 ID、
  目标最小子模块、插入锚点、行级证据或表内图片；
- `compose_canonical_markdown()` 把结构化表统一放在主体章节之后，而不是放进目标
  最小子模块；
- 当前图片能通过 Evidence 绑定到最小子模块，但 renderer 生成的是独立图证汇总表，
  不是业务表的“现场图片/附件”单元格；
- taxonomy 支持多层 section，但当前只有 `2.3.1`，没有示例中的 `2.3.1.1`；
- 表格规则文件存在 19/21 张、正文表号/附录表号和动态 `表15~N` 等冲突，不能用
  显示表号作为机器主键。

### 1.2 目标调用链

```text
五模块及模块审查完成
  → Cross 跨模块审查、owner 回改和原 Cross reviewer 闭环
  → Table Fill Agent 根据最终子模块正文填固定表格
  → 运行时确定性校验表格
  → Chief 只读消费表格包并综合正文
  → Final Audit 审查表文一致性
  → Markdown/DOCX 按固定叶节点和锚点渲染
  → Delivery/Version/Receipt 固化
```

表格 Agent 负责专业提炼和填单元格；运行时负责固定结构、位置、引用、计算复核、
持久化和恢复。Cross 不填表，Chief 不改表，renderer 不判断业务内容。

### T0：冻结规则覆盖矩阵

负责人：业务规则/报告结构开发者。此工作不改运行流程。

- [ ] 将规则 MD 的通用规则、逐表定义、执行流程、质量清单和附录逐项对齐；
- [ ] 为每张表建立稳定 `definition_id`，显示表号只保留为 `source_aliases`；
- [ ] 每张表明确 `target_section_id` 和精确 `placement`；
- [ ] 每张表固定标题、列 ID、显示列名、顺序、数据类型和必填规则；
- [ ] 固化触发条件、空表 suppression、正文 notice、空值、排序和计算规则；
- [ ] 固化每列允许使用的子模块正文、Evidence、Knowledge 和 Photo 来源；
- [ ] 明确动态表实例 ID 和稳定排序；
- [ ] 核对示例 `2.3.1.1` 是否是正式叶节点；如果是，先更新 taxonomy 和结构测试；
- [ ] 产出 `source-rule-audit.md`，逐条记录规则文件内部冲突及采用结果；
- [ ] 未得到业务确认的冲突不得由开发者猜测后写死。

建议文件：

```text
manyselves/templates/reporting/tables/catalog.yaml
manyselves/templates/reporting/tables/source-rule-audit.md
tests/reporting/tables/test_catalog.py
```

退出条件：所有规则表都能通过稳定 ID 定位；缺目标、锚点、列、触发或空表规则时
catalog 加载失败；相同内容产生相同摘要。

### T1：建立独立身份和 typed 合同

负责人：Agent contract 开发者。

- [ ] 新增 `manyselves/templates/reporting/agents/table-filler.md`；
- [ ] 新增 carrier：`table_fill_input`、`table_fill_submission`、
  `table_fill_validation`、`table_fill_completion`；
- [ ] `TableFillInput` 绑定 run、Cross completion、最终模块、catalog 摘要、Evidence、
  Source ledger 和 photo manifest；
- [ ] `TableFillSubmission` 只允许提交 `definition_id + disposition + rows/notices`；
- [ ] 固定 headers、目标 section 和 placement 由运行时注入，不允许 Agent 提交；
- [ ] 单元格使用 column ID，不允许新增、删减或重命名列；
- [ ] 每一项目事实行绑定当前 run 的 `E-*`，图片单元格绑定有效 `P-*`；
- [ ] 禁止空字符串，按表级规则使用“—/待补充/未测量/未收到/N/A”等值；
- [ ] Agent 只能使用读、检索、图片检查、安全计算和 `submit_result`，不能直接改
  Markdown/DOCX；
- [ ] 将合同接入 `INPUT_CONTRACT_TYPES`、submission schema 和 `SubmitResultTool`；
- [ ] 为错误列、额外列、未知表、无效 Evidence/Photo、漏表、空值编写拒绝测试。

不要删除现有 ArtifactGateway、typed submission 和输入合同校验；在其上增加本阶段
合同即可。

退出条件：一个隔离 table-filler Agent 能根据固定输入提交合法 typed result；任何
结构、scope 或当前 run 引用错误在进入 workflow 下一阶段前被拒绝。

### T2：插入 Cross → Table Fill → Chief

负责人：report workflow 开发者。

- [ ] 在 full report 路径的 Cross 后、Chief 前调用 `_table_fill()`；
- [ ] 在 revision 路径的 Cross 后、Chief 前执行同一阶段；
- [ ] 首版修订可重填全部适用表，先保证一致性，再做增量优化；
- [ ] 新增 `table_fill_input_ref`、`table_fill_submission_ref`、
  `table_fill_validation_ref`、`table_fill_completion_ref`；
- [ ] completion 记录绑定 catalog、五模块、Cross、Evidence、Source 和照片摘要；
- [ ] completion 未持久化成功时不得进入 Chief；
- [ ] resume 仅复用当前 run 且全部语义摘要一致的结果；
- [ ] 模块、Cross、catalog、Evidence、Source 或照片变化后拒绝 stale completion；
- [ ] 为 table-fill 增加 cost boundary、进度通知和失败/等待状态；
- [ ] 更新 `_write_handoff_contracts()`，在 `cross-review` 与 `synthesis` 之间明确列出
  `table-fill`；
- [ ] 不因 table-fill 失败自动创建新 run。

退出条件：full、revision、resume 都严格执行 Cross → Table Fill → Chief，且无法
绕过 completion gate。

### T3：把表格所有权从 Chief 移给运行时

负责人：Chief/asset contract 开发者。

- [ ] `ChiefEditorInput` 增加只读的 `table_fill_completion_ref` 或表格包投影；
- [ ] 从 Chief Prompt 中删除生成/选择表格的要求；
- [ ] `EditedReportSubmission.tables` 先进入兼容期只允许空值，再从模型输出删除；
- [ ] `ReportAssetAssembler` 从 table completion 构造 `ReportTable`；
- [ ] Chief 的 semantic context hash 必须包含 table completion 和 catalog 摘要；
- [ ] Chief completion 的 artifact refs/hash 必须包含 table-fill artifacts；
- [ ] Chief 重写过渡内容时，表格定义、行、证据、图片和 placement 摘要不得变化；
- [ ] aggregate/revision 兼容路径明确是否接受已有表格包，禁止退回 Chief 自由造表。

退出条件：Chief 即使不返回任何 table 字段，报告仍能装配完整表格；Chief 试图修改
表格会被合同或确定性校验拒绝。

### T4：叶节点锚点和表内图片渲染

负责人：Markdown/DOCX renderer 开发者。

- [ ] 为最小子模块建立不依赖模糊关键词的稳定 block/anchor；
- [ ] 在 target section 的 `before_current_state` 等固定锚点插入表格；
- [ ] 同一小节多表按 catalog order 稳定排序；
- [ ] suppressed 表不渲染标题/空表，只在指定正文 block 插入 notice；
- [ ] Markdown 审计投影保留表名、列、行、Evidence 和图片占位语义；
- [ ] DOCX 将 `photo_id` 图片插入指定业务表单元格；
- [ ] 图片等比缩放、限制版心、提供 caption/替代文字；
- [ ] 已进入业务表的图片不再重复进入“原表图证汇总”；
- [ ] 图片损坏/缺失按照表级规则 fallback；规则要求必备时阻止交付；
- [ ] 覆盖跨页表、长文本、多图、无图和连续表格；
- [ ] 表格在 Markdown 与 DOCX 中都只出现一次。

退出条件：表格精确出现在目标最小子模块的固定锚点，表内图片可见，Markdown 与
DOCX 的表名、列、行、图片和 notice 一致。

### T5：确定性质量门、Final Audit 和交付

负责人：validation/delivery 开发者。

- [ ] 校验 catalog ID/version/digest；
- [ ] 校验表格集合、target、anchor、列名/顺序、行宽、空值和排序；
- [ ] 重算派生数值并核对单位、小数位和阈值；
- [ ] 校验表格数值/位置/风险与已批准子模块正文一致；
- [ ] 校验行级 Evidence/Claim/Photo 均属于当前 run；
- [ ] 校验空表 suppression 与 notice 一一对应；
- [ ] FinalReviewInput 提供只读表格包和确定性校验报告；
- [ ] Final auditor 只创建问题/结论，不直接修改表格；
- [ ] delivery manifest、receipt 和 report version 写入 catalog/table completion 摘要；
- [ ] 错误信息定位到 `definition_id / row / column / E-* / P-*`。

退出条件：任何表文矛盾、漏表、位置错误、公式错误或不可追溯图片都不能交付。

### T6：测试和真实运行

- [ ] 每张固定表至少有一个正向 fixture；
- [ ] 覆盖有数据、全部正常、缺数据、部分空值、公式缺参、图片缺失；
- [ ] 覆盖 `<50%` 有效列的“数据不完整”规则；
- [ ] 覆盖严重度/位置、超标率、改善行动优先级排序；
- [ ] 覆盖多张表同叶节点、动态表和深层叶节点；
- [ ] 覆盖 full、revision、resume、stale completion 和中途 crash；
- [ ] 覆盖 Chief 不能修改表格；
- [ ] 跑 reporting 定向测试、完整非 integration 测试、compileall、diff-check；
- [ ] 使用真实 Inputs 跑一次完整 Provider 流程；
- [ ] 目视检查新鲜 DOCX 的表格位置、分页和图片；
- [ ] 确认当前 run `completed`，五模块、Cross、Table Fill、Chief、Final、DOCX、
  receipt 全部属于同一 run。

建议验证命令：

```bash
.venv/bin/pytest -q tests/reporting
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q -m 'not integration'
.venv/bin/python -m compileall -q manyselves tests
git diff --check
```

---

## 第二部分：内核无状态化（按当前实现修订）

### 2.1 已有基座：保留并复用，不要重做

旧计划基线是 `fdf168c`；当前 `3babc0c` 已经增加或加强以下能力：

- `AgentDefinition` 已通过 Markdown frontmatter 描述身份、模型、tools、reads、
  writes、预算和 Prompt，并做严格 schema 校验；
- `compile_agent_access()` 已把 identity 声明与 TaskEnvelope 的 `allowed_tools` 求交，
  并验证声明的任务引用；
- `ArtifactGateway` 已使用 workflow/task/agent/session grant 和带签名 opaque ref；
- `ContentAddressedStore` 已提供 SHA-256 内容寻址和兼容文件视图；
- typed input、typed submission、Finding → RevisionResponse → ResolutionVerdict、
  ReviewCompletionRecord 已经是当前报告合同；
- review completion 已校验 current-run identity、subject refs 和 artifact SHA-256；
- Chief completion 已绑定模块、Cross、输入 envelope、artifact hash 和
  `semantic_context_sha256`，resume 可拒绝 stale replay；
- cost control 已采用“持久 boundary intent → checkpoint → confirm → evaluate”，恢复时
  能处理 boundary crash window；
- AgentLoop context checkpoint、provider-call manifest 和压缩 conversation trace 已有
  持久化基础；
- `_write_handoff_contracts()` 会写当前 run 的 producer/consumer 矩阵。

这些能力是无状态化迁移的输入，不是需要新造的平行系统。尤其不要另建第二套
ArtifactRef、Claim/Source、review lifecycle、completion hash 或 cost ledger。

### 2.2 仍然有状态/任务特定的事实

当前仍未达到“定义文件可迁移任务”：

- `load_packaged_agents()` 固定从安装包的 reporting templates 加载，没有 capability
  manifest、definition lock 或 per-run digest；
- `LoopManager._create_loops()` 固定选择 `main-agent` 并注册成 `main`；
- `LoopManager._create_tools_for_agent()` 仍以 `agent_id == "main"` 注册报告、文档等
  工具；`compile_agent_access()` 约束 reporting task 工具，但不是通用 ToolFactory；
- `PromptLoader` 固定模板目录，并在缺文件时返回配电报告 fallback；
- `ReportingService._execute_locked()` 仍按五种 operation 写 Python 分支；
- `ReportWorkflowRunner` 仍写死模块、Cross、Chief、Final、Render、Delivery 顺序；
- `_write_handoff_contracts()` 只是硬编码流程的运行快照，不是可执行 workflow 定义；
- `WorkflowMessageRouter` 的 session map、research/gap/blocked 列表都在内存，且没有
  外部 interaction policy；
- `REPORT_MODULE_IDS`、`REPORT_TAXONOMY`、S2-1/S4-4/S4-6 mapper、intake parser、
  renderer 和输出逻辑仍是配电领域代码；
- `FullReportCheckpoint` 和 `state: dict` 仍包含 Cross/Chief 等领域字段；
- `ReportingService._active_agent_runners`、`ReportingAgentRunner._sessions` 和 router
  session registry 仍是进程内执行真相；
- 没有通用 EventStore/ProjectionStore、TaskQueue、LeaseStore/FencingToken；
- 没有第二个非配电能力包证明“新增任务不改 kernel”。

### 2.3 修订后的目标

```text
不可变 Capability Definition Lock
  + 不可变 Run Input refs
  + 现有 Artifact/CAS/typed completion
  + append-only Run Events
      ↓
通用 workflow engine / identity runtime / tool grants / interaction router
      ↓
可重建 checkpoint、进度、对话和 delivery projection
```

无状态不是“没有状态”，而是进程内对象不再是状态真相。generic kernel 只认识
Capability、Workflow、Stage、Task、Attempt、Identity、Artifact、ToolGrant、
Interaction、Event、Projection、Lease 和 Delivery，不认识 `2.1`、S4-4、Cross、
Chief、配电章节或固定输出路径。

### K0：重新冻结当前基线

负责人：无状态化技术负责人。

- [ ] 以 `3babc0c` 而不是 `fdf168c` 建立 characterization baseline；
- [ ] 列出五种 operation、所有 stage、typed input/output、completion 和 artifact graph；
- [ ] 把 `_write_handoff_contracts()` 生成物纳入行为 fixture；
- [ ] 记录 `AgentDefinition`、compiled access、ArtifactGateway/CAS、review/Chief recovery、
  cost boundary 和 conversation/provider manifests 的现有测试；
- [ ] 扫描 Main、配电 identity、taxonomy、mapper、路径和 operation 的领域泄漏；
- [ ] 建立“已有基座/部分能力/未实现”矩阵，禁止重复建设；
- [ ] 如果表格 T2 已合入，characterization 必须包含 Table Fill stage。

退出条件：可以用实际代码符号和测试解释当前 run 如何创建、推进、恢复和交付。

### K1：Capability manifest 和 Definition Lock

负责人：definition/runtime contract 开发者。

- [ ] 新增严格的 `CapabilityManifest`、`DefinitionRepository` 和
  `DefinitionLock` schema；
- [ ] 首版 manifest 先引用现有 agents/skills/taxonomy/handlers/templates，不急于移动
  文件；
- [ ] 对所有定义和静态资产规范化后计算 SHA-256；
- [ ] run 创建时持久化 bundle ID/version/schema/kernel API/digest；
- [ ] resume/revision 必须使用相同 lock，或执行显式 migration；
- [ ] 校验路径 confinement、重复 ID、缺引用、未知 tool/executor/schema；
- [ ] definition 缺失时 fail closed，删除配电 Prompt fallback；
- [ ] 给没有 lock 的旧 run 提供明确 legacy adapter，不静默猜测版本。

建议最小目录：

```text
manyselves/core/kernel/definitions.py
manyselves/core/kernel/locks.py
manyselves/core/kernel/contracts.py
manyselves/capabilities/power_distribution/capability.yaml
tests/kernel/test_definition_bundle.py
```

退出条件：同一 run 的定义可证明、不可热漂移；任一定义变化都会改变 lock，旧 run
不能静默用新定义恢复。

### K2：身份入口和通用 ToolFactory

负责人：identity/tool runtime 开发者。

- [ ] `LoopManager` 接收编译后的 entry identity，不再自行选择 `main-agent`；
- [ ] 建立按注册名创建工具的 ToolFactory；
- [ ] 用 capability tool declaration + identity allowlist + envelope grant + artifact/secret
  scope 的交集授权；
- [ ] 复用并下沉当前 `compile_agent_access()`，不要旁路它；
- [ ] 移除 `agent_id == "main"` 的领域工具分支；
- [ ] Prompt/Skill 由 DefinitionRepository 加载；
- [ ] Secret 只通过 `SecretResolver` 的名称和 scope 引用，值不进入 definitions/events；
- [ ] 工具、carrier、artifact 和 secret 越权全部 fail closed；
- [ ] 迁移后 Main 和 reporting agents 的实际工具集合与基线等价。

退出条件：新增/删除 identity 或收窄工具无需修改 LoopManager。

### K3：把硬编码 handoff 快照升级成可执行 workflow

负责人：workflow engine 开发者。

- [ ] 定义有限状态 workflow schema：stage、requires、input/output slots、executor、
  retry/decision/completion；
- [ ] 只允许注册过的安全 executor，不允许 YAML 内联 Python/shell/import；
- [ ] 将 `_write_handoff_contracts()` 改为“编译后 workflow 的运行投影”，不再维护第二
  份硬编码阶段表；
- [ ] `ReportingService` 只选择 capability + entrypoint，不再按 operation 写业务分支；
- [ ] 首先用 coarse legacy executor 包住现有行为，再逐 stage 拆分，避免一次性重写；
- [ ] 按 `render_existing → aggregate_existing → module_report →
  distill_template_skill → full_report/revision` 迁移；
- [ ] workflow compiler 拒绝循环、缺依赖、类型不匹配、不可达 stage 和缺 completion；
- [ ] 先保持现有串行顺序，不顺便引入并行；
- [ ] 表格阶段若已验收，完整迁移 Cross → Table Fill → Chief，禁止绕过。

退出条件：五种 operation 由外部 entrypoint 驱动，同一 fixture 的 stage、typed
input/output、completion、receipt 与基线等价。

### K4：交互规则外置并持久化 correlation

负责人：interaction/review runtime 开发者。

- [ ] 新增 `interactions.yaml` 和严格 schema；
- [ ] 声明 sender/recipient、message kind、request/response schema、correlation、
  timeout、retry 和 escalation；
- [ ] 声明 Finding → RevisionResponse → ResolutionVerdict 和 original-reviewer ownership；
- [ ] 声明用户 decision point 和允许投影到 UI 的业务消息；
- [ ] `WorkflowMessageRouter` 只执行编译后的规则，不按领域 agent 名判断；
- [ ] session registration、query correlation、reply 和 timeout 进入持久事件/artifact；
- [ ] 重启后可恢复未完成 interaction，不能依赖 `_sessions` 内存 map；
- [ ] 非法 sender/recipient/message schema 和重复 reply 被拒绝；
- [ ] 保持当前 Cross/模块审查责任不变。

退出条件：修改合法交互拓扑只改 definition，不改通用 router。

### K5：事件化状态、projection 和进程恢复

负责人：state/recovery 开发者。

- [ ] 新增 append-only EventStore，事件包含 run/sequence/stage/task/attempt、
  causation/correlation、artifact refs、schema version；
- [ ] 把现有 review/Chief completion hash 和 cost boundary 作为事件引用复用；
- [ ] single-writer reducer 生成 workflow/progress/conversation/delivery projection；
- [ ] `workflow-state.json` 兼容期只能由 reducer 生成，并携带 event cursor/hash；
- [ ] 禁止 workflow 直接成为 event 与 snapshot 的双写者；
- [ ] 建立 Task/Attempt/Lease/FencingToken；
- [ ] `_active_agent_runners`、runner `_sessions` 和 router map 降级为缓存/当前 lease；
- [ ] stale/late attempt、旧 fencing token、重复 terminal、事件乱序均被拒绝；
- [ ] 对 Provider 已返回但 completion 未写、completion 已写但 projection 未刷新的窗口
  做故障注入；
- [ ] waiting_user、blocked、failed、cancelled、completed 均能从 events + artifacts 重建；
- [ ] 保留 current-run ref/hash/semantic hash 校验，不因 event sourcing 删除它们。

退出条件：杀进程并清空进程内 runner 后，只凭 definition lock、RunInput、events 和
artifacts 能在原 run 恢复，且不会重复执行已完成 stage。

### K6：把配电领域迁入 capability bundle

负责人：power-distribution capability 开发者。

- [ ] 迁移 identities、skills、tools、carriers、interactions 和 workflow definitions；
- [ ] 迁移 taxonomy、table catalog、intake mapping、mapper 选择、render/publish config；
- [ ] 领域实现可作为注册 handler 存在，但 generic kernel 不 import 配电模块；
- [ ] logical ArtifactRef 不依赖原机器绝对路径；
- [ ] package build 包含全部 capability assets；
- [ ] 旧 CLI/GUI/Main 入口兼容映射到 bundle ID + entrypoint；
- [ ] 静态扫描确保 kernel 没有 `REPORT_TAXONOMY`、S4-4、Cross/Chief、章节名和
  `Outputs/Reports` 等领域常量；
- [ ] full/revision/resume/render/delivery parity 全部通过。

退出条件：复制 capability bundle 和 RunInput refs，可在另一工作目录启动同一任务。

### K7：第二能力包证明通用性

负责人：独立验证开发者，避免由配电迁移实现者自行放宽验收。

- [ ] 新增一个不含配电概念的最小能力包；
- [ ] 只使用已有 identity/tool/interaction/workflow/event/renderer 机制；
- [ ] 不修改 generic kernel 代码完成发现、锁定、执行、审查、恢复和交付；
- [ ] 进行 kill/resume、工具越权和 definition drift 测试；
- [ ] 生成 matching delivery receipt；
- [ ] 记录新增能力包所需改动；如果仍需改 kernel 中的领域分支，则验收失败。

退出条件：证明实现的是“任务迁移内核”，不是“把配电常量搬到 YAML”。

### K8：旧 run 迁移与删除兼容层

- [ ] 定义无 lock 旧 run 的读取范围和支持截止版本；
- [ ] 旧 checkpoint 只迁移一次并记录 migration event；
- [ ] 无法无损映射时 fail closed，要求旧版本完成；
- [ ] 同一 run 不允许在 legacy/new engine 之间反复切换；
- [ ] 每个 compatibility adapter 有使用计数、测试和删除条件；
- [ ] 删除 `PromptLoader` 配电 fallback、operation 分支、直接 checkpoint 写入和进程
  状态真相前，先完成 parity 与恢复测试；
- [ ] 更新架构文档、release/build evidence 和迁移说明。

---

## 3. 合并与所有权建议

| 阶段 | 建议所有者 | 可并行范围 | 禁止并行范围 |
| --- | --- | --- | --- |
| T0 | 表格规则负责人 | 可与 K0/K1 并行 | 不改 workflow |
| T1 | 表格合同负责人 | 可与 K0/K1 分文件并行 | 共享 contract 文件需单写者 |
| T2～T3 | 报告 workflow/Chief 负责人 | 不建议与 K3～K5 并行 | `workflow.py`、Agent contracts |
| T4～T5 | renderer/quality 负责人 | K1 可继续 | render/delivery shared files |
| T6 | 独立验收者 | 无 | 不边验收边放宽 gate |
| K2～K5 | kernel 负责人 | 表格行为冻结后进行 | 不重写表格业务规则 |
| K6 | capability 迁移负责人 | 第二能力包可准备 fixture | 不绕过已验收 Table Fill |
| K7 | 独立通用性验证者 | 可与 K8 文档准备并行 | 不由 kernel 作者放宽验收 |

推荐合并序列：

1. `table/T0-catalog`；
2. `kernel/K0-K1-definition-lock`；
3. `table/T1-contract`；
4. `table/T2-T5-runtime`；
5. `table/T6-validation`，冻结 Table Fill 行为；
6. `kernel/K2-K5-runtime`；
7. `kernel/K6-power-distribution-bundle`；
8. `kernel/K7-K8-portability-migration`。

## 4. 每次交接必须提供的材料

- [ ] 分支、worktree、基线 commit、当前 HEAD 和 `git status --short`；
- [ ] 本轮完成的 TODO 编号和对应 commits；
- [ ] 改动文件及其所有权，是否与其他 worktree 有重叠；
- [ ] 新增/修改的 Schema、定义文件及版本/digest；
- [ ] 表格项目交接时附上与本文件记录 SHA-256 一致的原始规则 MD；
- [ ] 代表性的 input/submission/validation/completion/event/projection artifacts；
- [ ] 定向测试、全量离线测试、compileall、diff-check 的原始结果；
- [ ] 真实 Provider 是否运行、run ID、终态和 usage；
- [ ] 新鲜 Markdown、DOCX、receipt、manifest 路径及 SHA-256；
- [ ] 未完成 TODO、已知失败、兼容入口和删除条件；
- [ ] 回滚方法：只能回滚本分支明确提交，不能重置用户工作树；
- [ ] 若失败，保留原 run 的 state/results/reviews/conversations/submissions/usage/logs。

## 5. 完成定义

表格部分完成必须同时满足：

- 当前 run 中存在 Cross、Table Fill、Chief、Final 的有效 completion；
- 所有适用表都已生成或有合规 suppression notice；
- 列、内容、Evidence、图片和叶节点位置与 catalog 一致；
- 当前 run 为 `completed`；
- 新鲜 DOCX 可读、版式正确，receipt 与产物匹配。

无状态化部分完成必须同时满足：

- identity、tool grant、interaction、workflow、taxonomy/config 和入口都由已锁定
  capability definition 驱动；
- 进程状态丢失后可由 events + artifacts 重建原 run；
- stale/late attempt 和旧 lease 不能写入当前状态；
- 配电能力包在另一工作目录可运行；
- 第二个非配电能力包不修改 kernel 即可运行和恢复；
- 旧 run 兼容/拒绝边界明确；
- 离线测试和至少一次真实 Provider 交付分别有证据。

两项结论必须分别报告。表格已完成不代表无状态化完成；Definition Lock 或 workflow
compiler 已完成也不代表表格已经交付。
