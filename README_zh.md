<div align="center">

![Manyselves](assets/screenshots/title.png)

### One runtime. Many selves.

**由文档定义 Agent 团队的本地工作空间**

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | 中文

</div>

## Manyselves 是什么？

Manyselves 是一套本地 Agent 团队运行时。Agent 的身份、边界、技能和交接由文档定义；换一套定义，就能变成另一支团队。

运行时提供稳定的通用部分：Main 对话、项目文件树、文档预览、任务路由、工具、检查点、Provider 接入和本地产物。仓库内置一支配电报告团队，作为可运行示例，而不是产品边界。

![Manyselves 如何成为一支团队](assets/screenshots/workflow.png)

**日常怎么用：** 见 [操作手册](docs/OPERATION_MANUAL.md)（启动、六种报告操作、修订与影响分析、默认参数）。

---

## 快速启动

需要：Python 3.12+、[uv](https://docs.astral.sh/uv/)、至少一个 LLM Provider 的 API Key。

### 桌面端

```bash
git clone <repository-url> manyselves
cd manyselves
uv sync
uv run manyselves
```

### 本地 Web（演示/验收）

```bash
.venv\Scripts\python.exe run_web.py --host 127.0.0.1 --port 9092 --data-dir <包含项目的工作目录>
```

浏览器打开 <http://127.0.0.1:9092/>，登录后选择项目，在 Main 对话里说明目标。

更多本地运行细节见 [RUN_LOCAL.md](docs/RUN_LOCAL.md)。

---

## 你只需要和 Main 说话

| 你说 | 系统做 |
|---|---|
| 用当前项目资料生成完整报告 | `full_report` |
| 只写某几个模块 | `module_report` |
| 汇总已有五份模块报告 | `aggregate_existing` |
| Markdown 转 Word | `render_existing` |
| 改已有完整报告 | `revise_report` |

修订时可以：

- 直接点名小节（如 2.3.1）
- 或让系统先做**影响分析**，确认后再改（`impact_mode=confirm` / `auto`）

详见 [操作手册 · 修订](docs/OPERATION_MANUAL.md#3-修订已有报告revise_report)。

---

## 运行时能力

- **一个 Main 入口** — 专家、审计、复核通过同一时间线回报
- **本地项目工作区** — 输入、知识、状态、输出都在项目目录里
- **文档上下文** — 预览、`@` 引用、Markdown 渲染
- **可恢复执行** — 流式响应、检查点、同 Run 恢复
- **多 Provider** — Anthropic、OpenAI、DeepSeek 及兼容 API

---

## 配置

根目录 `manyselves.config.yaml` 为应用配置；项目状态与交付物在所选项目目录内。

```yaml
agents:
  defaults:
    model: "anthropic/claude-sonnet-4.5"
    temperature: 0.1
    max_tool_iterations: 200
```

Provider、部署与账户说明见 [docs/](docs/README.md)。

---

## 文档导航

| 你想看 | 打开 |
|---|---|
| 操作手册 | [docs/OPERATION_MANUAL.md](docs/OPERATION_MANUAL.md) |
| 文档总索引 | [docs/README.md](docs/README.md) |
| 架构约定（开发/Agent） | [AGENTS.md](AGENTS.md) |
| 本地 Web 细节 | [docs/RUN_LOCAL.md](docs/RUN_LOCAL.md) |

---

## License

MIT，见 [LICENSE](LICENSE)。
