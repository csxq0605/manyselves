<div align="center">

![Manyselves](assets/screenshots/title.png)

### One runtime. Many selves.

**由文档定义 Agent 团队的本地工作空间**

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue.svg)](https://www.python.org/)
[![Built with PyQt6](https://img.shields.io/badge/built%20with-PyQt6-green.svg)](https://www.riverbankcomputing.com/software/pyqt/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | 中文

</div>

> **实验分支说明：**当前 checkout 含仅属于 `cost-control-experiments` 的三波协作
> 和模块并行写作，`main` 不含这些改动；模块责任审查以及 Cross/Chief/Final 路径
> 仍然串行。已实现与待实现边界见
> [成本控制实验交接说明](docs/experimental-cost-control-handoff.md)。

服务端账户级 worker、项目写入、会话传输和网页 API Key 隔离及 MiMo 模型配置见
[服务端账户隔离与 MiMo 配置](docs/server-account-isolation-and-mimo.md)。

## Manyselves 是什么？

Manyselves 是一套本地桌面 Agent 团队运行时。Agent 的身份、边界、技能和
交接关系由文档定义；保留同一个工作空间，只需更换这些定义，就能把它变成
另一支团队。

运行时负责稳定的通用部分：Main Agent 对话、项目文件树、文档预览、任务路由、
工具、检查点、Provider 接入和本地产物。当前仓库内置了一支可用于生产的配电
报告团队；它是 Manyselves 能承载的一种能力，而不是产品边界。

![Manyselves 如何成为一支团队](assets/screenshots/workflow.png)

## 定义团队，而不是重做应用

Manyselves 团队由三层组成：

1. **身份文档**定义每个 Agent 的职责、限制、输入、输出和交接；
2. **Skill 文档**沉淀可复用的领域方法和质量标准；
3. **运行时工具与工作流**把这些定义连接到文件、状态、复核和交付物。

当前定义位于 `manyselves/templates/agents/` 和
`manyselves/templates/reporting/`。哪些变化只需改文档，哪些需要扩展 Python，
见[定义一支团队](docs/team-definition.md)。

需要了解运行基础设施、完整功能、日常操作和产品化服务边界时，见
[项目说明与产品化服务评审稿](docs/project-guide-zh.md)。
当前串行成本控制的范围与兼容边界见
[成本控制主线合入说明](docs/cost-control-main-integration.md)；后续并行工作只按
[并行 Agent 编排与部署调研计划](docs/parallel-agent-orchestration-research-plan.md)
先做研究和准入验证。

## 通用运行时能力

- **一个稳定的 Main 入口** — 用户只和 Main 对话，任务级专家、审计、复核与
  编辑角色通过同一时间线回报进度；
- **本地项目工作空间** — 在一个窗口查看输入、知识、工作状态与输出；
- **文档上下文** — 预览文件、使用 `@` 引用、附加选中行并渲染 Markdown；
- **可靠执行** — 流式响应、任务路由、类型化状态、检查点、回滚和可恢复决策；
- **多 LLM Provider** — 支持 Anthropic、OpenAI、DeepSeek 及兼容 API；
- **可扩展交付** — 工具与工作流可以生成文档、复核记录、台账和状态快照。

## 快速开始

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和至少一个 Provider API Key。

```bash
git clone <repository-url> manyselves
cd manyselves
uv sync
uv run manyselves
```

![Manyselves 启动页](assets/screenshots/start-window.png)

没有 `uv` 时，在仓库内部创建虚拟环境：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/manyselves
```

也可通过环境变量预配置 Provider：

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DEEPSEEK_API_KEY="sk-..."
uv run manyselves
```

## 浏览器、Electron 与服务器部署

第一阶段同时保留 PyQt 桌面入口，并提供 React 浏览器客户端、Electron
客户端和单 Runtime FastAPI 服务。Linux 服务器安装、启动、备份恢复、升级与回滚请见
[Compose 部署手册](docs/deployment/linux-compose.md)；明确的能力边界请见
[第一阶段已知限制](docs/phase1/known-limitations.md)。

```bash
cp deploy/env.example deploy/.env
# 设置管理员账号、可选的 Provider Key 和绝对数据目录。
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build --wait
set -a
. deploy/.env
set +a
uv run python scripts/verify_deployment.py \
  --url http://192.168.8.28:9090 \
  --username "$MANYSELVES_ADMIN_USERNAME" \
  --password-env MANYSELVES_ADMIN_PASSWORD
```

## 配置与本地状态

规范配置文件固定为仓库根目录的 `manyselves.config.yaml`，从任何目录启动
Manyselves 都使用同一默认文件；规范 Python 包名为 `manyselves`。显式传入的
配置路径仍优先。

模型配置有两个受支持入口：日常操作使用浏览器的“设置 → 模型设置”；服务器
管理员也可以直接维护 `manyselves.config.yaml`。二者使用同一个配置模型，项目
不会额外引入设置数据库，也不会在浏览器中提供原始 YAML 编辑器。模型提供商、
API 地址、启停状态、默认模型及 Agent 默认参数可以写入 YAML。

Provider API Key 可以在模型设置页首次填写，也可以通过服务器环境变量提供。
环境变量密钥优先级高于 YAML，页面仅显示“由服务器环境管理”，不会回显或允许
覆盖。修改 `deploy/.env` 中的密钥后，需要使用 Compose 重建 API 容器；仅刷新
浏览器不会生效。未填写 Provider Key 仍可启动和管理项目，但真正调用 Agent 模型
时会失败。

应用偏好写入仓库根目录 `.manyselves/`；项目状态和交付物只写入所选项目目录。

![Manyselves API 配置](assets/screenshots/configuration-window.png)

```yaml
agents:
  defaults:
    model: "anthropic/claude-sonnet-4.5"
    temperature: 0.1
    max_tool_iterations: 200
```

## 内置能力：配电报告

仓库内置团队负责资料接入、五模块三波协作、责任审计、跨模块复核、Chief
Editor 整合、Skill 治理和模板化 DOCX 交付。

客户事实放在 `Inputs/`；项目标准与解释资料放在 `Knowledge/`。当前适配三张
核心工作簿、WPS `DISPIMG` 图片、XLSX/XLSM、DOCX、Markdown、文本、图片和
PDF；DWG 与视频会明确保留为 `manual_required`，不会静默丢失。

客户事实 `E-*`、参考来源 `R-*`、Claim、Coverage、缺资决策、Skill 版本、
复核发现和报告版本均可审计。知识库与网络资料可以辅助解释，但不能变成客户
现场事实。

完整报告中的 2.1–2.5 五个固定模块先完成两波有界协作，再并行写作；责任审计、
定向返修和原审查者复核目前仍按模块固定顺序执行。五个模块全部闭环后，才进入
跨模块复核与完整文档整合。缺少证据时默认以明确不确定性标记继续 `draft`；
`ask`、`block` 和 `skip` 仍可显式选择，持久化决策可跨进程恢复。
交付后修订会从基线恢复，只重跑责任模块，再复核完整报告并发布不可变子版本。

典型项目结构：

```text
项目/
├── Inputs/                         # 客户事实
├── Knowledge/                      # 项目参考资料
├── Templates/                      # 可选 report_template.docx
├── Work/
│   ├── evidence.jsonl
│   ├── coverage.json
│   ├── report-state.json
│   ├── runs/<run-id>/              # 状态、模块、复核、台账和决策
│   └── report-versions/<id>/        # 不可变版本快照
└── Outputs/
    ├── Modules/                     # 2.1–2.5 模块稿
    ├── Reviews/                     # 责任审计和跨模块复核
    └── Reports/                     # 完整 DOCX 与渲染日志
```

报告修订与 Skill 演进互相独立。只有用户明确要求能力演进时才执行：
`FeedbackRecord → SkillCandidate → EvaluationResult → 确认 → SkillVersion`。
后续运行按 packaged、product、project 的顺序解析 Skill；每个报告版本都会固化
实际使用的 ID、版本、scope、哈希和模板 provenance。

证据决策、验收边界、修订和产物的完整约定见
[配电报告能力契约](docs/capabilities/power-distribution.md)。

## 可选文档解析

Manyselves 可以调用可选的文档转换命令行工具，把 PDF、图片、DOCX、PPTX 和
XLSX 转成 Markdown。安装并认证兼容的转换工具后，应用会在启动时自动检测；
本地工作空间的核心使用不依赖该集成。

## 架构

```text
manyselves/                         # 规范 Python 包
├── app.py                          # CLI 与桌面启动
├── branding.py                     # 对外品牌契约
├── config/                         # 仓库本地 YAML 配置
├── core/
│   ├── loops/                      # Agent 运行时与 MessageBus
│   ├── providers/                  # LLM Provider 抽象
│   ├── reporting/                  # 当前内置配电报告能力
│   └── tools/                      # 工作区和工作流工具
├── gui/                            # PyQt6 桌面界面
├── resources/                      # Manyselves 应用图标
└── templates/
    ├── agents/                     # Main 身份与公共策略
    └── reporting/                  # 内置团队身份与 Skills
```

## 开发

```bash
QT_QPA_PLATFORM=offscreen uv run pytest -q
uv run ruff check manyselves tests scripts
uv run python scripts/build_brand_assets.py
```

命名、图标、配色和兼容规则见[品牌系统](docs/brand.md)。

## 产品身份

Manyselves 作为独立产品维护，拥有自己的名称、软件包、资产、文档、配置和
发行产物。

## 许可证

MIT License — Copyright (c) 2026 Manyselves contributors。
