<div align="center">

![Manyselves](assets/screenshots/title.png)

### One runtime. Many selves.

**A local workspace for document-defined agent teams.**

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue.svg)](https://www.python.org/)
[![Built with PyQt6](https://img.shields.io/badge/built%20with-PyQt6-green.svg)](https://www.riverbankcomputing.com/software/pyqt/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

English | [中文](README_zh.md)

</div>

## Philosophy

**Agent team boundaries belong in documents, not in another bespoke app.**

Swap identity, skill, and handoff definitions and the same runtime becomes a
different team. The runtime owns only the durable generic parts—conversation,
files, routing, checkpoints, providers, and artifacts. Orchestration and role
semantics live in Capability file definitions.

> **One runtime. Many selves.**

---

## Design principles

1. **File-defined**  
   Capabilities, Agents, Tasks, Tools, Contracts, Workflows, and Recovery are
   declared in Markdown / YAML / Schema. Flow lives in files, not private
   `if/for` orchestration.

2. **Stateless Kernel + one Compiler/Runtime**  
   A single compile-and-execute semantics:

   ```text
   Plan + State + Event  →  New State + Effects
   ```

   The Kernel does not know providers, report chapters, or domain fields.
   Capabilities must not copy a second state machine.

3. **Capability-owned domain runtime**  
   Domain models, validation, prompts, deterministic tools, rendering, and
   delivery belong to the Capability; the generic layer only schedules them.

4. **Recoverable and observable**  
   The same Run can WAITING / resume; conversations, tool results, events, and
   artifacts stay traceable.

5. **Users talk to Main only**  
   Specialists, auditors, reviewers, and editors collaborate on one timeline.

Full spec: [`docs/PROJECT_POSITIONING.md`](docs/PROJECT_POSITIONING.md) and
[`AGENTS.md`](AGENTS.md).

---

## System shape

![Manyselves architecture](assets/diagrams/architecture.svg)

```text
Markdown / YAML / JSON Schema / Python Tool references
                         │
                         ▼
              Definition Loader & Registry
                         │
                         ▼
                  Workflow Compiler
                         │
                         ▼
             Resolved Plan + Stateless Kernel
                         │
                         ▼
           Generic Action / Executor Runtime
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
        Agents          Tools       Interactions
          │              │              │
          └──────────────┼──────────────┘
                         ▼
              State / Recovery / Events
                         │
                         ▼
        FastAPI / Capability Frontend / Outputs
```

---

## What is in this repository

| Path | Contents |
|---|---|
| `manyselves/kernel/` | Stateless Kernel: definitions, contracts, pure transitions, ports |
| `manyselves/runtime/` | Generic Agent/Tool/Conversation/Interaction/Recovery/Event execution |
| `manyselves/capabilities/` | Capability packages (`distribution_reporting`) |
| `manyselves/application/` | Generic Capability/Workflow/Run lifecycle |
| `manyselves/webapi/` | Generic FastAPI |
| `frontend/` | Capability-specific React product frontend driven by State/Event/Output projections |
| `manyselves/templates/` | Agent identity and report skill documents |
| `docs/` | Positioning, architecture, deployment, internal status ([index](docs/README.md)) |

### Bundled Capability: power-distribution reporting

The completed bundled reporting product: evidence intake → five module lanes →
responsibility audit → Cross/Chief → Final → Word/index delivery, plus targeted
revision of an existing complete report (impact analysis
`impact_mode=auto|confirm`).

It is an example of what the runtime can host, not the product boundary.

---

## Runtime capabilities

- **One Main surface** — users talk to Main only
- **Local project workspace** — inputs, knowledge, state, and outputs on disk
- **Document context** — preview, `@` references, Markdown rendering
- **Reliable execution** — streaming, checkpoints, same-run recovery
- **Multiple LLM providers** — Anthropic, OpenAI, DeepSeek, and compatible APIs
- **Extensible delivery** — documents, review records, ledgers, state snapshots

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

Local web:

```bash
.venv\Scripts\python.exe run_web.py --host 127.0.0.1 --port 9092 --data-dir <workspace>
```

Canonical app config: repository-root `manyselves.config.yaml`.

Day-to-day operation (six report operations, revision, defaults): see the
[Operation Manual](docs/OPERATION_MANUAL.md).

---

## Docs

| Purpose | Document |
|---|---|
| Operation manual | [docs/OPERATION_MANUAL.md](docs/OPERATION_MANUAL.md) |
| Product & architecture positioning | [docs/PROJECT_POSITIONING.md](docs/PROJECT_POSITIONING.md) |
| Architecture convergence plan | [docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md](docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md) |
| Architecture rules (dev / agents) | [AGENTS.md](AGENTS.md) |
| Docs index | [docs/README.md](docs/README.md) |

---

## License

MIT. See [LICENSE](LICENSE).
