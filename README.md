<div align="center">

![Manyselves](assets/screenshots/title.png)

### One runtime. Many selves.

**A recoverable local runtime for file-defined agent teams, with a bundled power-distribution safety reporting capability.**

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue.svg)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-%E2%89%A5%2022-green.svg)](https://nodejs.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

English | [中文](README_zh.md)

</div>

## Philosophy

**How an agent team divides its work belongs in files, not in another bespoke app.**

Swap the identity, skill, task, and workflow definitions and the same runtime
becomes a different team. The runtime owns only the generic, durable parts:
conversations, files, scheduling, checkpoints, providers, and artifacts. Who
does what lives in a Capability's definition files.

> **One runtime. Many selves.**

---

## Design principles

1. **File-defined**  
   Agents, tasks, tools, contracts, workflows, and recovery policies are
   declared in Markdown / YAML / JSON Schema. Flow lives in files, not in
   private `if/for` orchestration.

2. **One stateless kernel, one compile-and-execute semantics**

   ```text
   Plan + State + Event  →  New State + Effects
   ```

   The kernel does not know providers, report chapters, or domain fields, and
   Capabilities do not build a second state machine.

3. **Domain code belongs to the Capability**  
   Domain models, deterministic rules, prompts, tools, rendering, and delivery
   live in the Capability; the generic layer only schedules them.

4. **Structure for code, judgment for the model**  
   What the input structure determines (grouping, citations, checks, assembly)
   is done by code. Professional judgment is left to the model; code checks
   only its citations and structure.

5. **Recoverable and traceable**  
   A run can wait and resume after an interruption. Conversations, tool
   results, events, and artifacts are kept on disk.

6. **Users talk to Main only**  
   Authors, reviewers, and the chief editor work on one timeline behind Main.

Full positioning: [`docs/PROJECT_POSITIONING.md`](docs/PROJECT_POSITIONING.md)
and [`AGENTS.md`](AGENTS.md).

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

## Bundled Capability: power-distribution safety report

`distribution_reporting` turns the assessment tables, site photos, knowledge
base, and report template in a project's `Inputs/` into a power-distribution
safety assessment report (Word plus an evidence and source index). It shows
what the runtime can host; it is not the runtime's boundary.

### How a full report runs

| # | Stage | Done by |
|---|---|---|
| 1 | Read the tables into checkpoint records and an evidence index; chapter 2 section numbers and titles come only from the assessment summary table | code |
| 2 | Read photos; photos only corroborate the tables, and the table text wins for the same reading | model (parallel) |
| 3 | Judgment ledger: judge each checkpoint by group, every row citing its records; evidence gaps are classified by code | model + code |
| 4 | Cross-module relations: authors propose them, code routes and checks them by structure | model + code |
| 5 | Review: module review (including same-kind rows) and cross-module review in parallel; revise only on blocking findings, with code re-checks between rounds | model + code |
| 6 | Settle the ledger, build the storyline and gap entries, attach photos to judgment rows | code |
| 7 | Writing: chapter 2 sections written once from the ledger; the chief editor writes chapters 3 and 1 | model (parallel) |
| 8 | Joint review: one per writer, carrying only its sections, rows, cited records, and the chapter 1/3 entries on those rows; a judgment-row change is carried by code to every place that cites it | model + code |
| 9 | Reconcile, assemble, render the DOCX and the evidence and source index | code |

Chapter 4 (special issues) depends only on whether `Inputs/` holds the
matching special files. Where evidence is missing the report says so.

### Report operations Main can run

| You say | Operation |
|---|---|
| Write a new report from the current inputs | `full_report` |
| Change a section or module, or revise for updated inputs | `revise_report`: the next version under the same run id; unaffected parts are reused |
| Re-distill the writing Skills from the template | `extract_skills`, then `publish_skills` after confirmation |
| Turn existing Markdown into Word | `render_existing` |
| Continue a stopped or failed run | `resume_run` |

See the [Operation Manual](docs/OPERATION_MANUAL.md) (Chinese) for phrasing and
parameters.

---

## What is in this repository

| Path | Contents |
|---|---|
| `manyselves/kernel/` | Stateless kernel: definitions, contracts, pure transitions, ports |
| `manyselves/runtime/` | Generic agent/tool/conversation/interaction/recovery/event execution; providers |
| `manyselves/definitions/` | Standard definition library shared by Capabilities |
| `manyselves/capabilities/` | Capabilities: `distribution_reporting`, plus `parameter_adjustment` and `checklist_review` that exercise the generic path |
| `manyselves/application/` | Generic Capability/Workflow/Run lifecycle |
| `manyselves/webapi/` | Generic FastAPI |
| `manyselves/templates/` | Agent identity and report Skill documents |
| `frontend/` | React product frontend for the reporting Capability |
| `frontend-contract/` | OpenAPI contract used by the frontend |
| `desktop/` | Electron shell |
| `deploy/`, `deployment/`, `scripts/` | Compose deployment, offline deployment, start and maintenance scripts |
| `tests/` | Kernel, runtime, Capability, API, and deployment tests |
| `docs/` | Operation, running, configuration, positioning ([index](docs/README.md)) |

---

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 22+, and
an API key for at least one supported provider.

```bash
git clone https://github.com/csxq0605/manyselves.git
cd manyselves
uv sync

cp manyselves.config.example.yaml manyselves.config.yaml
cp .env.example .env              # set the admin password; never commit .env

cd frontend && npm ci && npm run build -- --base / && cd ..
uv run python run_web.py --host 127.0.0.1 --port 9090 --data-dir <workspace>
```

Open <http://127.0.0.1:9090/>, sign in with the admin account from `.env`, set
a provider and key in model settings, and tell Main what you want.

On Linux/macOS `./start.sh` also works (it builds the frontend when needed).
Docker, multi-account, and service deployment: [Running](docs/RUNNING.md);
offline packaging: [Offline deployment](docs/OFFLINE_PACKAGING_REMOTE_DEPLOYMENT.md).

---

## Docs

| Purpose | Document |
|---|---|
| Operation manual | [docs/OPERATION_MANUAL.md](docs/OPERATION_MANUAL.md) |
| Run locally | [docs/RUN_LOCAL.md](docs/RUN_LOCAL.md) |
| Running and deployment (Docker, multi-account, services) | [docs/RUNNING.md](docs/RUNNING.md) |
| Offline packaging and remote deployment | [docs/OFFLINE_PACKAGING_REMOTE_DEPLOYMENT.md](docs/OFFLINE_PACKAGING_REMOTE_DEPLOYMENT.md) |
| Configuration | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Product and architecture positioning | [docs/PROJECT_POSITIONING.md](docs/PROJECT_POSITIONING.md) |
| Repository rules (dev / agents) | [AGENTS.md](AGENTS.md) |
| Docs index | [docs/README.md](docs/README.md) |

---

## License

MIT. See [LICENSE](LICENSE).
