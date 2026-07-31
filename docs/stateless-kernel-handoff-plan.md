# Manyselves 内核无状态化实施与交接计划

> 计划编号：STATELESS-KERNEL-01
> 状态：待实施，可直接交接
> 基线仓库：`/Users/zzymima0000/Documents/Codex/manyselves`
> 基线提交：`fdf168c9ee93f24826211f06bae6dc1d619f2188`
> 本计划不实现配电报告表格填报；表格阶段作为一个领域流程节点在后续迁移。

## 1. 目标

把 Manyselves 从“部分身份文件化、但功能/交互/编排/状态仍由 Python 领域逻辑
决定”，迁移为：

```text
不可变 Capability Definition Bundle
        +
不可变 Run Input Bundle
        +
只追加 Run Event Log / 不可变 Artifacts
        ↓
通用 Kernel 执行与恢复
        ↓
可重建的进度、对话、状态和交付投影
```

任务迁移时，应能带走一个版本化能力包及输入引用，在另一实例启动同一任务，不再
依赖安装目录中隐含的 Agent 名称、配电章节、固定 workflow 分支或进程内状态。

“无状态”不表示没有运行状态，而表示：

- 进程内对象不是状态真相；
- 状态由持久事件和不可变 artifacts 重建；
- identity、tool grant、interaction、workflow、schema、taxonomy、路径策略和
  renderer 选择来自已锁定的定义包；
- 内核不认识 `2.1`、S4-4、Cross、Chief 或配电报告章节。

## 2. 范围和非目标

### 2.1 本计划包含

- Capability bundle 的 schema、加载、校验、版本和 digest lock；
- 身份、Prompt、Skill、工具授权、carrier/schema 定义外置；
- Agent-to-Agent、review、用户决策和升级交互定义外置；
- operation 和 workflow 有限状态图外置；
- 通用 task/stage/attempt/artifact/event/projection 状态；
- 本地持久仓储、恢复、lease/fencing 和幂等；
- 将现有配电报告能力迁成首个 bundle；
- 用第二个最小能力包证明“新增任务不改 kernel”；
- 旧 run、旧 checkpoint 和旧入口的兼容迁移/拒绝规则。

### 2.2 本计划不包含

- 重新设计配电报告的业务规则；
- 实现或修改表格填报业务；
- 借无状态化改变当前审查责任或串行语义；
- 在 parity 之前引入新的并行编排；
- 在首轮同时替换为云数据库、对象存储和分布式队列；
- GUI 重做、浏览器三块界面或多租户产品化；
- 把 YAML 变成可执行 Python/shell 的插件机制。

首轮先提供本地端口实现，证明运行和恢复行为等价；远端存储和 headless 部署是后续
在相同端口上的实现替换。

## 3. 当前代码事实

| 当前事实 | 代码位置 | 迁移目标 |
| --- | --- | --- |
| Agent frontmatter 已可描述角色、tools、reads、writes、模型和预算 | `manyselves/core/reporting/config.py::AgentDefinition`、`manyselves/templates/reporting/agents/` | 保留并纳入 bundle lock |
| Main loop 固定加载 `main-agent` 并注册为 `main` | `manyselves/core/loops/manager.py::_create_loops` | 由 bundle entrypoint 声明 |
| 工具注册对 `agent_id == "main"` 分支 | `LoopManager._create_tools_for_agent` | ToolFactory + 编译后的 ToolGrant |
| Prompt loader 固定目录和配电 fallback | `manyselves/core/prompts/loader.py` | 定义仓库加载；缺失 fail closed |
| ReportingService 对 operation 写死分支 | `manyselves/core/reporting/service.py::_execute_locked` | workflow entrypoint 映射 |
| 模块和 taxonomy 是 Python 常量 | `manyselves/core/reporting/models.py::REPORT_MODULE_IDS`、`taxonomy.py::REPORT_TAXONOMY` | bundle schema/data |
| 专家、审查、Cross、Chief、Final 顺序写在 workflow | `manyselves/core/reporting/workflow.py` | 声明式 workflow + 注册 executor |
| 输入 adapter 路由由 Python registry 决定 | `manyselves/core/reporting/intake/adapters.py::IntakeAdapterRegistry` | bundle 选择已注册 adapter |
| 活跃 runner 保存在进程内 dict | `manyselves/core/reporting/service.py::_active_agent_runners` | durable task/attempt + lease |
| 大型可变 `state: dict` 与 `workflow-state.json` 并存 | `workflow.py::FullReportCheckpoint` 和各阶段 state key | 通用事件和 reducer 投影 |
| artifact/CAS、typed submission、Agent conversation 已有基础 | `core/artifacts/`、`submission_contracts.py`、`agent_runner.py` | 复用，不另造平行事实源 |

这些事实说明：当前“身份文档独立”已经有基础，但运行时身份选择、工具授权、交互、
编排和恢复仍是任务特定代码。

## 4. 目标边界

### 4.1 Kernel 只认识

- Capability、Definition、DefinitionLock；
- Run、Workflow、Stage、Task、Attempt；
- Identity、Session；
- Artifact、ArtifactRef、SchemaRef；
- Tool、ToolGrant、SecretRef；
- Interaction、Message、Correlation；
- Event、Projection；
- Lease、FencingToken；
- Provider、Renderer、Publisher；
- Validation、Completion、Delivery。

以下内容不得出现在 generic kernel 的常量、条件分支或 import 中：

- `REPORT_TAXONOMY`、`2.1`～`2.5`；
- `S2-1`、`S4-4`、`S4-6`；
- `cross-module-reviewer`、`chief-editor` 等领域身份；
- 配电章节和表格名称；
- `Outputs/Reports` 等领域固定路径。

### 4.2 Capability bundle 负责

- 功能入口和 operation；
- identities、Prompt、Skill；
- identity 允许的工具和 carrier；
- typed input/output schema；
- taxonomy、业务配置和模板；
- workflow graph、stage executor 名称和 dataflow；
- interaction/review/decision 规则；
- adapter、validator、renderer、publisher 的注册名和配置；
- artifact logical name 与 retention/publish policy；
- 兼容迁移声明。

bundle 只能引用 kernel 已注册的安全 executor/adapter/validator/renderer。定义文件
不得包含任意 Python、shell、动态 import 或 secret 值。

## 5. Definition Bundle

建议布局：

```text
manyselves/capabilities/power_distribution/
├── capability.yaml
├── identities/
├── skills/
├── tools.yaml
├── carriers.yaml
├── interactions.yaml
├── workflows/
│   ├── full-report.yaml
│   ├── module-report.yaml
│   ├── aggregate-existing.yaml
│   ├── render-existing.yaml
│   └── distill-template-skill.yaml
├── schemas/
├── taxonomies/
├── intake/
├── rendering/
├── assets/
└── migrations/
```

`capability.yaml` 至少包含：

```yaml
bundle_id: manyselves.power-distribution
bundle_version: 1.0.0
schema_version: 1
entrypoints:
  full_report: workflows/full-report.yaml
definitions:
  identities: identities/
  tools: tools.yaml
  carriers: carriers.yaml
  interactions: interactions.yaml
compatibility:
  kernel_api: ">=1,<2"
```

加载过程必须：

1. 路径 confinement，禁止越出 bundle root；
2. schema 严格校验，拒绝未知字段和重复 ID；
3. 检查所有引用、schema、executor 和 tool name；
4. 检查 identity、interaction、workflow 的引用闭包；
5. 对规范化后的所有定义和资产计算 SHA-256；
6. 生成不可变 `definition-lock.json`；
7. run 创建后不因磁盘文件变化而热切换定义。

## 6. Run 输入、状态和端口

### 6.1 `RunInputBundle`

包含：

- run/request ID、capability lock；
- entrypoint 和规范化参数；
- 输入文件的 ArtifactRef、媒体类型、大小和 SHA-256；
- provider execution profile 引用；
- secret 名称引用，不含 secret 值；
- parent run/version；
- 创建者、项目/租户 scope 和创建时间。

### 6.2 `RunEventLog`

至少定义：

```text
RunCreated
DefinitionLocked
InputAttached
StageReady
TaskDispatched
AttemptStarted
MessagePersisted
ToolCallPersisted
TypedResultAccepted
ValidationCompleted
FindingCreated
DecisionRequested
StageCompleted
RunWaitingUser
ArtifactPublished
RunCompleted
RunFailed
```

事件必须有 run、event sequence、stage/task/attempt、fencing token、artifact refs、
causation/correlation、时间和 payload schema version。

`workflow-state.json` 在兼容期可以继续存在，但只能是包含 `event_cursor` 和
`projection_hash` 的可重建投影，不能与 event log 分别接受业务写入。

### 6.3 通用状态

不再增加 `cross_review_completion_ref`、`chief_candidate_ref` 等领域字段。目标
状态使用通用结构：

```yaml
stages:
  cross_review:
    status: completed
    outputs:
      completion: artifact:...
tasks:
  table_fill:
    current_attempt: attempt-2
artifacts:
  approved_modules: artifact:...
```

字段名来自已锁定 workflow 的逻辑 output slot，kernel 只验证 schema/ref/hash。

### 6.4 端口

先提供本地实现：

```text
DefinitionRepository
RunRepository
EventStore
ProjectionStore
ArtifactStore
ConversationStore
TaskQueue
LeaseStore
ProviderPort
ToolRuntime
RendererPort
PublisherPort
SecretResolver
Clock
IdGenerator
```

本地端口可以使用现有目录、CAS、JSONL 和 SQLite；业务 stage 不得直接拼接路径、
获取 `fcntl` 锁或持有进程级全局 runner。

## 7. 声明式编排和交互

### 7.1 Workflow

workflow 是有限状态机和 typed dataflow，不是脚本：

```yaml
id: full-report
stages:
  - id: prepare
    executor: deterministic_pipeline
    input: report_request
    output: preparation_bundle
  - id: module_cycle
    executor: foreach
    items_from: taxonomy.modules
    workflow: module-review-cycle
  - id: cross_review
    executor: agent_task
    identity: cross_reviewer
    requires: [module_cycle.completed]
  - id: table_fill
    executor: agent_task
    identity: table_filler
    requires: [cross_review.completed]
  - id: chief_edit
    executor: agent_task
    identity: chief_editor
    requires: [table_fill.completed]
```

首轮 executor 白名单建议：

- `deterministic_task`；
- `agent_task`；
- `sequence`；
- `foreach`；
- `barrier`；
- `review_cycle`；
- `decision`；
- `render`；
- `publish`。

先保持现有串行语义。并行、重试和超时只有在定义和内核均显式支持且测试完成后
才能启用。

### 7.2 Interactions

`interactions.yaml` 声明：

- 哪个 identity 可以向哪个 identity 发送哪种 typed message；
- request/response schema；
- correlation、timeout、重试和升级目标；
- reviewer ownership 与 same-reviewer recheck；
- Finding → RevisionResponse → ResolutionVerdict；
- user decision point；
- 哪些消息可投影到文件、进度和对话界面。

工具最终授权取以下集合的交集：

```text
kernel 可用工具
∩ capability tools.yaml
∩ identity allowlist
∩ task envelope grant
∩ 当前 Artifact/Secret scope
```

任何一层缺失都 fail closed。

## 8. 工作包与提交顺序

### K0：冻结基线和领域泄漏清单

工作：

- 为当前五个 operation 建立 characterization tests；
- 记录身份、工具、carrier、interaction、taxonomy、stage、state key、路径、
  adapter、renderer 和 publisher；
- 增加静态扫描，列出预定 generic kernel 中的领域 import/字符串；
- 记录当前 full/revision/resume/delivery 调用链和 artifact graph；
- 不改变运行行为。

完成标准：

- 有一份可追踪到代码符号的定义清单；
- 当前行为有测试和 fixture 基线；
- 当前 dirty worktree 被记录，用户变更未被覆盖。

建议提交：`test(kernel): characterize current capability runtime`

### K1：DefinitionRepository、bundle schema 和 lock

建议新增：

```text
manyselves/core/kernel/definitions.py
manyselves/core/kernel/locks.py
manyselves/core/kernel/contracts.py
manyselves/core/kernel/repositories.py
tests/kernel/test_definition_bundle.py
```

先让 loader 可以从现有 `manyselves/templates/reporting` 构建兼容 bundle view，
再迁移目录；避免一开始同时移动文件和改变加载逻辑。

完成标准：

- 引用缺失、schema 错误、未知 executor/tool、目录逃逸均在 run 前失败；
- 任一文件变化都会改变 lock；
- run 持久化 lock，resume 校验 lock；
- 旧加载入口通过 compatibility adapter 保持行为。

建议提交：`feat(kernel): add immutable capability definitions`

### K2：外置身份、carrier 和工具授权

工作：

- `LoopManager` 接收编译后的 identity，不自行选择 `main-agent`；
- ToolFactory 按注册名创建工具；
- ToolGrant 编译器替代 `agent_id == "main"`；
- Prompt/Skill 从 DefinitionRepository 加载；
- 删除任务特定 fallback，缺失定义 fail closed；
- secret 只通过 `SecretResolver` 按名字和 scope 获取。

完成标准：

- 增删 identity 或收窄工具无需修改 `LoopManager`；
- 工具越权、carrier 越权和 secret 越权都有拒绝测试；
- 现有 Main/Reporting identities 的工具集合与基线一致。

建议提交：`refactor(kernel): compile identities and tool grants`

### K3：外置 interaction

工作：

- 增加 typed interaction definition 和 compiler；
- `query_peer/reply_peer` 只执行编译后的路由；
- review ownership、recheck、decision/escalation 由规则声明；
- correlation、timeout 和重复响应进入通用事件；
- UI 只读取允许投影的业务消息。

完成标准：

- 非法 sender/receiver/message schema 被拒绝；
- original reviewer ownership 和 Cross 闭环行为与基线一致；
- interaction 文件变化进入 definition lock；
- Agent 名称不再出现在通用 router 的条件分支。

建议提交：`refactor(kernel): drive interactions from definitions`

### K4：Workflow compiler 和通用执行器

建议新增：

```text
manyselves/core/kernel/workflow_schema.py
manyselves/core/kernel/compiler.py
manyselves/core/kernel/engine.py
manyselves/core/kernel/executors/
```

迁移顺序：

1. `render_existing`；
2. `aggregate_existing`；
3. `module_report`；
4. `distill_template_skill`；
5. `full_report` 和 revision/resume。

每迁移一个 entrypoint，就删除 `ReportingService._execute_locked` 对应领域分支，
但保留 compatibility route 到同一个 compiled workflow，直到全部迁完。

完成标准：

- workflow 编译检查循环、缺引用、类型不匹配和不可达 stage；
- 同一 fixture 的 stage 顺序、typed inputs/outputs 和完成条件与基线一致；
- `ReportingService` 最终只选择 capability/entrypoint 并调用 kernel；
- 表格项目若已合入，完整迁移 Cross → Table Fill → Chief，不得绕过。

建议提交：按 entrypoint 分五个小提交，最后
`refactor(reporting): execute compiled capability workflow`

### K5：事件化运行状态和恢复

工作：

- append-only EventStore；
- single-writer reducer 和可校验 projection；
- Task/Attempt/Lease/FencingToken；
- 所有 typed result、validation、decision 和 stage completion 先成为事件；
- runner/controller 进程对象降级为缓存和当前 lease；
- 旧 `workflow-state.json` 只读迁移或投影兼容；
- 进行 crash、late result、重复 terminal、过期 lease 故障注入。

完成标准：

- 在每个主要 stage 杀进程后，仅凭 RunInput、EventLog 和 artifacts 恢复；
- 重复事件幂等，sequence 缺口和 hash 错误被拒绝；
- 旧 attempt 和旧 lease 不能覆盖当前结果；
- waiting_user、failed、cancelled、completed 均可重建；
- 进程内 dict 丢失不导致 run 丢失或跳阶段。

建议提交：拆分 store、reducer、lease、migration 四个提交。

### K6：配电能力包迁移

工作：

- 将 agents、skills、taxonomy、operations、interactions、workflows、schemas、
  intake mapping、rendering config 和模板纳入 power-distribution bundle；
- 每个领域 handler 通过已注册 executor/adapter/validator/renderer 暴露；
- 领域代码可以存在于 capability implementation，但 generic kernel 不 import 它；
- package build/test 确认 bundle assets 被包含；
- 旧入口映射到 bundle ID + entrypoint。

完成标准：

- 安装后的包能发现并校验 power-distribution bundle；
- 复制 bundle 和输入引用即可在另一工作目录执行；
- kernel 静态扫描无配电名称和领域 import；
- full/revision/resume/render/delivery 与迁移前合同等价。

建议提交：按 identities、taxonomy/intake、workflow、rendering/publish 分批迁移。

### K7：第二能力包和迁移证明

新增一个小而完整、没有配电概念的测试能力包，例如“文档摘要 + 审查 + Markdown
交付”，只使用已有通用 executor。

完成标准：

- 不修改 kernel 代码即可发现、锁定和执行第二能力包；
- identity/tool/interaction/workflow/state 均由文件决定；
- kill/resume 和 delivery receipt 通过；
- 由此证明“任务迁移”而不仅是“把配电常量移到 YAML”。

建议提交：`test(kernel): prove second capability portability`

## 9. 兼容、迁移和回滚

### 9.1 旧 run

- 每个新 run 必须保存 bundle ID/version/digest；
- 没有 definition lock 的旧 run 由显式 legacy adapter 读取；
- 旧 checkpoint 只迁移一次并记录 migration event；
- 无法无损映射时 fail closed，要求用旧版本完成，不得猜测 stage；
- resume/revision 不允许静默升级 bundle；必须显式 migration。

### 9.2 双写边界

过渡期若同时写 event log 和 `workflow-state.json`：

- event 是唯一写入入口；
- snapshot 只能由 reducer 生成；
- snapshot 必须携带 event cursor/hash；
- 测试禁止 workflow 直接修改 snapshot；
- parity 完成后再删除 legacy write path。

### 9.3 回滚

每个 K 阶段必须能通过配置退回上一入口，但不能让同一 run 在两个 engine 间来回
切换。run 一旦绑定 engine/bundle lock，其恢复必须使用相同版本或显式迁移。

## 10. 验收矩阵

### 定义和安全

- bundle path traversal、重复 ID、未知 schema/executor/tool 均失败；
- tool grant 交集正确，默认拒绝；
- secret 值不进入 definition、input、event、conversation 或 UI projection；
- definition 修改导致 digest 变化，旧 run 拒绝静默恢复。

### 编排和交互

- 五个当前 operation 均由 entrypoint 驱动；
- full report 的模块审查、Cross、表格阶段（若已存在）、Chief、Final、Render、
  Delivery 顺序与完成条件不变；
- review owner、revision/recheck、Main exception 和用户决策合同不变；
- 非法 typed message 和 stale attempt 被拒绝。

### 状态和恢复

- 每个 stage 前后 kill/restart；
- provider 已返回但 completion 未写、completion 已写但 projection 未刷新的 crash window；
- 重复结果、乱序事件、旧 fencing token；
- waiting_user 后补充输入、failed 后精确 run resume；
- 进程内 runner 清空后仍能恢复。

### 可迁移性

- 同一 bundle + RunInputBundle 在另一工作目录执行；
- 第二能力包零 kernel 改动运行；
- 领域文件缺失时明确失败，不回退为配电 Prompt；
- artifact logical refs 不依赖原机器绝对路径。

### 真实交付

迁移完成声明必须同时提供：

- 当前 run `completed`；
- 全部阶段当前 run completion；
- 新鲜可读 DOCX 和 matching receipt；
- definition lock、event tail、projection hash、delivery manifest；
- Provider attempt/usage 和恢复记录；
- 离线测试与真实 Provider 证据分开报告。

建议命令：

```bash
.venv/bin/pytest -q tests/kernel
.venv/bin/pytest -q tests/reporting
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q -m 'not integration'
.venv/bin/python -m compileall -q manyselves tests
git diff --check
```

## 11. 交接方式

接手者按以下顺序阅读：

1. 本计划；
2. `manyselves/core/reporting/config.py` 和现有 Agent 定义；
3. `manyselves/core/loops/manager.py`、`core/prompts/loader.py`；
4. `reporting/service.py`、`workflow.py`、`agent_runner.py`；
5. `input_contracts.py`、`submission_contracts.py`、message router；
6. artifact/CAS、conversation、delivery/version、checkpoint 代码；
7. taxonomy、intake adapters、renderers 和现有测试。

每个工作包交接必须附带：

- 分支/worktree、基线 commit、bundle schema version、kernel API version；
- `git status --short` 和未提交文件归属；
- 本工作包的 ADR/定义清单、commits 和迁移说明；
- 新旧路径 parity 测试和结果；
- definition lock/event/projection 示例；
- crash/recovery 测试结果；
- 未完成的领域泄漏、兼容入口和删除条件；
- 是否跑过真实 Provider、是否完成当前 run DOCX 交付。

当前主工作树在本计划编写时已有多项未提交修改。接手者不得重置或清理这些用户
变更。建议 STATELESS 工作在独立分支/worktree；K0/K1 可与表格项目并行，K4/K6
迁移 `workflow.py` 前必须先选定包含或不包含表格阶段的明确基线。

## 12. 与表格计划的协调

- 两份计划没有架构前置依赖；
- 表格项目负责业务填报行为，并先在当前 runtime 跑通；
- 本项目只在 K4/K6 把当时已经验收的完整业务流程迁入 bundle；
- 若两项目并行，禁止两边同时修改同一 `workflow.py`/合同文件；
- 联合合并顺序建议为：表格 T0～T5 → 冻结行为和测试 → 无状态 K4/K6 迁移；
- 验收结论必须拆开报告，不能用“bundle loader 已实现”代替表格交付，也不能用
  “表格已生成”代替内核无状态化完成。
