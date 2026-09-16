<div align="center">

![Manyselves](assets/screenshots/title.png)

### One runtime. Many selves.

**A local workspace for document-defined agent teams.**

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

English | [中文](README_zh.md)

</div>

## What is Manyselves?

Manyselves is a local runtime for teams of AI agents whose identities, boundaries,
skills, and handoffs live in documents. Change the definitions, keep the same
workspace, and you get a different team.

The runtime supplies the durable parts: Main conversation, project file tree,
document preview, task routing, tools, checkpoints, provider integration, and
local artifacts. A power-distribution reporting team ships as a working example.

**How to use it:** see the [Operation Manual](docs/OPERATION_MANUAL.md) (Chinese)
for startup, the six report operations, revision / impact analysis, and defaults.

---

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), and an API key for
at least one supported LLM provider.

```bash
git clone <repository-url> manyselves
cd manyselves
uv sync
uv run manyselves
```

### Local web (demo / acceptance)

```bash
.venv\Scripts\python.exe run_web.py --host 127.0.0.1 --port 9092 --data-dir <workspace-with-projects>
```

Open <http://127.0.0.1:9092/>, sign in, pick a project, and talk to Main.

Details: [docs/RUN_LOCAL.md](docs/RUN_LOCAL.md).

---

## Talk to Main only

| You say | Operation |
|---|---|
| Generate a full report from project inputs | `full_report` |
| Write only selected modules | `module_report` |
| Aggregate five existing module reports | `aggregate_existing` |
| Render existing Markdown as Word | `render_existing` |
| Revise an existing complete report | `revise_report` |

Revision can start from named subsections, or from an **impact analysis** of
changed inputs (`impact_mode=confirm` / `auto`). See the
[operation manual](docs/OPERATION_MANUAL.md).

---

## Runtime capabilities

- **One Main surface** — specialists, auditors, and editors report on one timeline
- **Local project workspace** — inputs, knowledge, state, and outputs stay on disk
- **Document context** — preview, `@` references, Markdown in conversation
- **Resumable execution** — streaming, checkpoints, same-run recovery
- **Multiple LLM providers** — Anthropic, OpenAI, DeepSeek, and compatible APIs

---

## Configuration

Repository-root `manyselves.config.yaml` is the canonical app config. Project
state and deliverables stay inside the selected project.

```yaml
agents:
  defaults:
    model: "anthropic/claude-sonnet-4.5"
    temperature: 0.1
    max_tool_iterations: 200
```

More: [docs/README.md](docs/README.md).

---

## Docs

| Need | Open |
|---|---|
| Operation manual | [docs/OPERATION_MANUAL.md](docs/OPERATION_MANUAL.md) |
| Docs index | [docs/README.md](docs/README.md) |
| Architecture rules (dev / agents) | [AGENTS.md](AGENTS.md) |
| Local web details | [docs/RUN_LOCAL.md](docs/RUN_LOCAL.md) |

---

## License

MIT. See [LICENSE](LICENSE).
