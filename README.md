# 本地配电报告多 Agent 工作区

这是一个从零实现的本地 PyQt 配电报告流程应用。当前版本首先打通流程架构：用户在项目工作区中组织资料、查看文件并与主 Agent 对话，后台按声明式工作流依次完成资料清单、解析、证据规范化、覆盖判断、报告规划、模块草稿、证据审校、局部返工路由和项目交付。

## 参考边界

- AutoReport 仅作为职责设计参考：项目文件工作区、文件查看器、主 Agent 对话、MessageBus、TaskBoard 和 Agent 运行循环。
- Nexgent 仅作为编排设计参考：Markdown/frontmatter Agent 定义与 YAML `phase`、`pipeline`、`parallel` 表达。
- 本仓库不导入两个参考项目的包，不访问其源码路径，也不要求它们存在才能安装、测试或运行。

## 当前流程

```text
PyQt 项目文件树 / 文件查看器 / 主 Agent 对话
                    │
                    ▼
              ReportRequest
                    │
                    ▼
ProjectManifest → ParsedArtifact → EvidenceItem → CoverageMatrix
                                                    │
                                                    ▼
                                             Report Planner
                                                    │
                                                    ▼
Module Worker → Evidence Auditor → Revision Router → Project Delivery
```

运行时同时具备：

- 类型化领域载体，禁止用一份无结构字符串贯穿流程。
- MessageBus 事件历史和 TaskBoard 状态机。
- `pipeline` 串行状态传递与 `parallel` 并行执行。
- 未声明载体写入、并行写冲突、Agent 异常和依赖失败传播。
- 所有项目状态与输出落在当前客户项目目录。
- 知识文件不会被当成客户事实写入证据账本。

## 本地安装

要求 Python 3.12 或更高版本。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## 启动 GUI

```bash
.venv/bin/pds-report --project /absolute/path/to/customer-project
```

窗口只包含项目文件区、文件查看器和主 Agent 对话。可以从文件区新建或打开另一个客户项目。

## Headless 验证

```bash
.venv/bin/pds-report \
  --headless \
  --project .test-projects/smoke \
  --message '写作配电报告，要求深度思考，先做2.4'
```

命令返回 JSON，包括运行状态、主 Agent 消息和生成文件路径。

## 客户项目目录

```text
customer-project/
├── Inputs/                 # 客户输入资料
├── Knowledge/              # 方法论与知识资料，不作为客户事实
├── Work/
│   ├── manifest.json
│   ├── evidence.jsonl
│   ├── coverage.json
│   └── runs/               # 可恢复运行快照、任务和事件
└── Outputs/
    ├── Modules/            # 结构化模块草稿
    ├── Reviews/            # 证据审校记录
    └── Reports/            # 本轮运行摘要；正式 DOCX 属于后续阶段
```

所有路径都从客户项目根目录派生。应用不会将项目运行数据写入源码仓库、用户主目录或系统临时目录。

## Agent 配置

Agent 位于 `src/pds_report/resources/agents/`，使用 Markdown/frontmatter：

```markdown
---
id: evidence-normalizer
role: evidence
reads: [parsed_artifacts]
writes: [evidence_items]
tools: [normalize_evidence]
---

将解析结果规范化为客户证据。不得从方法论或 FAQ 生成客户事实。
```

加载器会校验重复 ID、未知载体以及缺失的读写合同。

## 工作流配置

工作流位于 `src/pds_report/resources/workflows/`：

```yaml
id: phase-a-flow
phases:
  - id: intake
    mode: pipeline
    agents: [manifest-builder, artifact-parser, evidence-normalizer]
  - id: module
    mode: parallel
    agents: [module-worker]
```

加载器会在运行前拒绝未知 Agent、非法模式、未知依赖、重复阶段和循环依赖。

## 验证

```bash
.venv/bin/ruff check src tests
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

测试临时目录固定在仓库内 `.test-projects/`。构建 wheel 时可把构建临时目录也固定到仓库内：

```bash
mkdir -p .build-tmp
TMPDIR="$PWD/.build-tmp" .venv/bin/python -m hatchling build -t wheel
```

## 当前范围

当前交付是可运行的流程架构和单模块纵向骨架。它尚不包含 2.1-2.5 的完整专业知识、正式 DOCX Renderer、跨模块总编与长期 Skill 发布；这些能力应在现有领域载体和配置化运行时上继续实现，不改变 GUI 边界。
