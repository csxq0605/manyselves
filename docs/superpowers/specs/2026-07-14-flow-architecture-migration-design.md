# 基于 AutoReport 的配电报告改造设计

## 已确认方向

`autoreport-power-distribution` 是独立 Git 仓库，但代码底座必须是 AutoReport 的完整源码快照，而不是重新实现一个相似应用。AutoReport 与 Nexgent 仍作为同级参考仓库存在；新仓库运行时不通过相对路径依赖它们。

- 直接保留 AutoReport 的 PyQt 桌面应用、项目创建/打开、文件树、文件预览、主 Agent 对话、模型配置、会话、MessageBus、TaskBoard、工具系统和 Agent loop。
- 在 AutoReport 原有运行时内替换物理实验业务，不另建一套 `pds_report` GUI 或并行运行时。
- Nexgent 只提供 YAML frontmatter Agent 定义，以及 `phase`、`pipeline`、`parallel`、失败隔离和恢复等编排思想；不接入 Nexgent 包或子 Agent 运行时。
- `work/` 只保存用户资料，迁移和运行均不得向其中写文件。

## 迁移边界

第一步将 AutoReport 当前源码树复制到新仓库根目录，不复制其 `.git`、虚拟环境、缓存或构建产物。新仓库自己的 Git 历史和远程保持不变，当前独立原型保存在历史分支中。

迁移完成后，根目录应直接包含 `autoreport/`、`tests/`、`pyproject.toml`、`README.md` 等 AutoReport 文件；不得出现嵌套的 `AutoReport/` 或 `Nexgent/` 目录。

## 产品架构

```text
AutoReport PyQt Workspace
  ├── Project File Tree
  ├── Existing File Preview
  └── Main Agent Chat
           │
           ▼
Existing MessageBus + LoopManager + TaskBoard + ToolRegistry
           │
           ▼
Power Distribution Workflow
  ├── ReportRequest
  ├── ProjectManifest / ParsedArtifact
  ├── EvidenceItem / CoverageMatrix
  ├── Planner / Module Worker
  ├── Evidence Auditor / Revision Router
  └── OutputArtifact
           │
           ▼
Current Project Files + Conversation Messages
```

GUI 继续只有项目文件、文件查看和主 Agent 对话这三个用户入口。证据、覆盖率、任务和审校状态保存在项目内并通过对话汇报，不新增仪表盘。

## Phase A 纵向样例

首个可运行流程只覆盖一个配电模块，但必须贯穿 AutoReport 的真实运行链路：

1. 主 Agent 将用户消息解析为 `ReportRequest`，把“深度思考”等本轮要求与报告范围分开。
2. 扫描当前 AutoReport 项目，解析三类工作簿并形成 `ProjectManifest`、`ParsedArtifact`、`EvidenceItem`。
3. 生成固定 2.1–2.5 taxonomy 的 `CoverageMatrix`；缺资必须标记 `pending` 或 `blocked`，不能猜测。
4. Planner 只为一个目标模块创建 `ModuleTask`。
5. 一个 Worker 生成 `ModuleDraft`，Evidence Auditor 输出批准或 `ReviewIssue`。
6. Revision Router 只返工责任模块并限制轮次。
7. 结果、审校记录和运行摘要写回当前项目，由现有文件树刷新展示；主 Agent 返回文件路径或缺资说明。

## 编排定义

Agent 提示与契约采用 Markdown/YAML frontmatter，工作流采用项目内 YAML 资源描述阶段与模式。配置加载器只负责声明和验证，执行仍由 AutoReport 的 LoopManager、AgentLoop、MessageBus、TaskBoard 和工具注册表完成。

支持模式限定为：

- `pipeline`：同一阶段顺序执行，前一步输出成为后一步输入。
- `parallel`：互不冲突的模块任务并发执行；Phase A 只有一个 Worker，但接口预留列表形态。
- `phase`：用于明确 intake、coverage、module、quality 的边界和失败传播。

配置必须拒绝重复 ID、未知 Agent、未知载体、非法模式、循环依赖和冲突写入。

## 项目内状态

保留 AutoReport 的项目目录能力，同时把配电流程状态放入当前客户项目，例如：

```text
project/
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

所有路径都由当前项目根目录派生。源码、测试和运行不得把项目资料写到 `work/`、用户主目录或系统临时目录。

## 测试与验收

1. 先运行迁入后的 AutoReport 原始测试，建立底座基线。
2. 每个新增行为先写失败测试，再做最小实现。
3. GUI 回归测试保证项目文件树、预览、对话和配置仍可用。
4. 编排测试覆盖 frontmatter、pipeline、parallel、依赖失败和项目内持久化。
5. 集成测试从主 Agent 请求走到一个模块产物，并验证缺资不会伪成功。
6. 新仓库可在移走同级 AutoReport/Nexgent 后独立安装、测试和启动。

## 非目标

- 不在本阶段完成五个模块的全部专业规则和正式 DOCX 模板。
- 不接入 Nexgent 运行时，不复制其 TUI、权限系统或插件系统。
- 不重做 AutoReport 已有 GUI，不另起第二套应用框架。
