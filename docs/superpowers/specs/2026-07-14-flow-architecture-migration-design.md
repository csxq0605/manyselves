# 配电报告流程架构迁移设计

## 目标

在全新的 `autoreport-power-distribution` 仓库中实现一套可独立安装、运行和测试的本地配电报告多 Agent 流程骨架。

AutoReport 与 Nexgent 都是只读参考仓库：

- 从 AutoReport 借鉴本地 PyQt 项目工作区、文件查看器、主 Agent 对话、MessageBus、TaskBoard 和 Agent 运行循环的职责划分。
- 从 Nexgent 借鉴 Markdown/frontmatter Agent 定义，以及 `phase`、`parallel`、`pipeline` 的声明式编排表达。
- 新项目不把两个参考仓库作为运行时依赖，不导入它们的包，也不要求它们位于固定路径。

本阶段只打通流程架构，不实现 2.1-2.5 五个模块的完整专业判断，不输出正式 DOCX。

## 产品边界

GUI 只暴露三个用户能力：

1. 创建、打开和组织本地项目文件。
2. 在现有文件查看器中打开输入与输出文件。
3. 通过主 Agent 对话发起报告任务、补充说明、确认缺资和请求返工。

证据账本、覆盖矩阵、TaskBoard、Agent 时间线和运行配置均为后台状态，不增加独立管理页面。主 Agent 通过普通对话消息汇报进度、缺口和结果文件。

## 总体架构

```text
PyQt Project Workspace
  ├── Project File Tree
  ├── File Viewer
  └── Main Agent Chat
           │
           ▼
ReportRequest Parser
           │
           ▼
Declarative Workflow Runtime
  ├── ProjectManifest / Intake
  ├── ParsedArtifact
  ├── EvidenceItem Ledger
  ├── CoverageMatrix
  ├── Report Planner
  ├── Module Worker Slot
  ├── Evidence Auditor
  ├── Revision Router
  └── Project Delivery
           │
           ▼
Project Files + Main Agent Messages
```

GUI、应用服务、领域模型、工作流运行时和基础设施适配器保持单向依赖。领域层不依赖 PyQt、具体 LLM SDK、Excel/PDF 库或两个参考仓库。

## 任务一：运行骨架与 Nexgent 式编排

任务一必须同时完成 AutoReport 运行职责迁移和 Nexgent 编排思想迁移，不能先写死流程、以后再配置化。

### 配置载体

每个 Agent 使用 Markdown 文件定义：

```markdown
---
id: evidence-normalizer
role: evidence
reads:
  - parsed_artifacts
writes:
  - evidence_items
tools:
  - normalize_evidence
---

将解析结果规范化为可追溯证据。不得从方法论或 FAQ 生成客户事实。
```

工作流使用 YAML 定义阶段、依赖和执行模式：

```yaml
id: phase-a-flow
phases:
  - id: intake
    mode: pipeline
    agents: [manifest-builder, artifact-parser, evidence-normalizer]
  - id: coverage
    mode: pipeline
    agents: [coverage-evaluator, report-planner]
  - id: module
    mode: parallel
    agents: [module-worker]
  - id: quality
    mode: pipeline
    agents: [evidence-auditor, revision-router, project-delivery]
```

配置加载器必须验证重复 ID、未知 Agent、非法依赖、循环依赖、未声明读写载体和不支持的执行模式。无效配置不得进入运行状态。

### 运行时职责

- `MessageBus` 发布用户消息、Agent 状态、任务更新、产物生成和错误事件。
- `TaskBoard` 保存任务的来源、目标、依赖、状态、修订轮次和阻塞原因。
- `WorkflowRunner` 根据配置生成执行图；`pipeline` 串行，`parallel` 并发，并在依赖完成后推进。
- `AgentRunner` 通过端口调用 Agent，不了解 PyQt 控件或具体 LLM SDK。
- `RunStore` 将运行快照、任务和事件保存到项目目录，以便关闭后恢复。
- 任一阶段失败必须产生明确失败事件并阻止依赖阶段运行，不得显示伪成功。

## 领域载体

首阶段定义并贯穿以下类型：

- `ReportRequest`：任务意图、目标模块、本轮执行要求、临时约束和缺资处理选择。
- `ProjectManifest`：文件 ID、相对路径、哈希、格式、用途、解析状态和错误。
- `ParsedArtifact`：解析器输出与来源定位。
- `EvidenceItem`：事实、对象、值、单位、时间、来源定位、可信度和待核实状态。
- `CoverageMatrix`：固定子模块的 `ready`、`pending`、`blocked` 状态与缺口。
- `ModuleTask`：模块范围、证据 ID、知识单元 ID、依赖和审校要求。
- `ModuleDraft`：结构化论断、证据引用、建议和待核实项。
- `ReviewIssue`：责任模块、问题类型、严重性、阻塞状态和修订要求。
- `OutputArtifact`：写入项目目录的草稿、审校记录和运行摘要。

首阶段 Worker 可以是确定性的最小实现，但必须消费和产出这些正式载体，不能用无结构字符串绕过边界。

## 数据流

1. 用户在主 Agent 对话中发起任务。
2. 请求解析器将任务与“深度思考”等本轮要求拆分为 `ReportRequest`。
3. Intake 扫描项目文件并生成 `ProjectManifest`。
4. Parser 生成带来源定位的 `ParsedArtifact`。
5. Normalizer 生成 `EvidenceItem`，客户事实只能来自项目资料或明确人工补录。
6. Coverage 根据固定 taxonomy 生成 `CoverageMatrix`。
7. Planner 生成 `ModuleTask`，不得改变固定目录。
8. Worker 插槽生成 `ModuleDraft`。
9. Auditor 进行确定性结构审查，并输出批准结果或 `ReviewIssue`。
10. Revision Router 只重派责任模块并限制轮次。
11. Delivery 将草稿、审校记录和运行摘要写入项目文件树。
12. Main Agent 用对话消息返回结果文件、缺口或失败原因。

## 目录设计

```text
src/pds_report/
  app/                 # 用例编排与应用服务
  domain/              # 领域模型、状态和规则
  workflow/            # 配置加载、执行图、MessageBus、TaskBoard
  agents/              # Agent 端口、运行器和内置确定性实现
  infrastructure/      # 文件、持久化、解析器与 LLM 适配器
  gui/                 # PyQt 窗口和控件
  resources/
    agents/            # Markdown/frontmatter Agent 配置
    workflows/         # YAML 工作流配置
tests/
  unit/
  integration/
  gui/
```

每个源文件只承担单一职责。配置资源随包发布；用户项目中的运行数据不写回源码目录。

## 项目目录约定

```text
customer-project/
  Inputs/
  Knowledge/
  Work/
    manifest.json
    evidence.jsonl
    coverage.json
    runs/
  Outputs/
    Modules/
    Reviews/
    Reports/
```

所有持久化路径都由项目根目录派生。测试、运行和解析不得在系统临时目录或用户主目录写项目数据。

## 错误与恢复

- 配置错误：启动前失败，显示具体文件、字段和原因。
- 请求校验错误：主 Agent 可修正一次；仍有业务歧义才询问用户。
- 文件解析错误：保留 manifest 条目和错误，不丢弃其他已成功资料。
- 证据不足：生成 `pending` 或 `blocked`，不得自动猜测。
- Agent 或工具错误：任务进入失败状态，记录可恢复快照并阻止依赖任务。
- 审校不通过：仅重派责任模块；达到最大轮次后升级到主 Agent 对话。
- 应用重启：从项目内运行快照恢复，不依赖进程内单例状态。

## 测试策略

- 单元测试覆盖领域模型、配置校验、执行图、MessageBus、TaskBoard 和状态转换。
- 集成测试使用确定性 Agent 与内存 LLM 端口，验证完整流程和失败传播。
- GUI 测试验证创建/打开项目、发送消息、文件树刷新和输出文件打开。
- 契约测试保证 Agent 配置声明的载体与 Python 类型一致。
- 所有功能遵循先写失败测试、确认失败原因、实现最小代码、再运行全量测试的顺序。

## 首阶段验收

1. 新仓库可独立安装和启动，不引用 `../AutoReport` 或 `../Nexgent`。
2. PyQt 界面具备项目文件树、文件查看器和主 Agent 对话三个区域。
3. 一条对话请求能够驱动配置化流程从 `ReportRequest` 运行到项目输出文件。
4. `pipeline`、`parallel`、依赖阻塞和失败传播均有自动化测试。
5. 运行状态、证据、覆盖结果、模块草稿和审校记录全部写入当前客户项目。
6. 缺资、配置错误和 Agent 失败通过主 Agent 对话明确反馈。
7. 两个参考仓库可以被移走而不影响新项目测试和运行。

## 非目标

- 五个专业模块的完整知识、提示词和判断规则。
- 正式 DOCX 排版和最终交付模板。
- 独立证据管理、覆盖率仪表盘、运行参数或 Agent 时间线页面。
- 自动发布 SkillCandidate 或长期规则。
- 将 Nexgent 作为运行时依赖或嵌入其服务。
