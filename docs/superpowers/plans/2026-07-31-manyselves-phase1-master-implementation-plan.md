# Manyselves Phase 1 Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a feature-complete React Web and Electron client backed by a single-runtime FastAPI service, deployable with Linux Docker Compose without changing Manyselves core behavior or persisted formats.

**Architecture:** Preserve `manyselves/core/**` and `manyselves/templates/**`; extract application startup into a shared `RuntimeHost`, expose operations through `RuntimeFacade`, and add versioned REST/SSE adapters. The React application is shared by browser and Electron through `PlatformBridge`, while the server workspace remains authoritative and PyQt remains available as the regression baseline.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, Uvicorn, Gunicorn with `uvicorn-worker`, React 19, TypeScript, Vite, TanStack Query, Zustand, Monaco Editor, Electron, Vitest, Playwright, Docker Compose, Nginx.

## Global Constraints

- Do not change business behavior under `manyselves/core/**` or `manyselves/templates/**`.
- Preserve existing project directories, conversation JSONL, checkpoints, report stores, and generated artifact formats.
- Production API process count is exactly one; Gunicorn worker count is exactly one.
- Server workspace is authoritative; local folder selection is import, never bidirectional synchronization.
- All runtime-mutating commands require a valid control lease and pass through one serialization lock.
- All file paths are project-relative and revision-protected; path escape must fail before filesystem access.
- PyQt remains runnable and all existing tests remain green until the full parity matrix is accepted.
- Do not add MySQL, Redis, Milvus, MinIO, LLM Wiki, multi-tenant RBAC, or project-worker orchestration in Phase 1.
- API prefix is `/api/v1`; REST is authoritative and SSE is a recoverable notification stream.
- Provider credentials stay server-side; Electron uses `contextIsolation`, sandbox, and a minimal preload bridge.

---

## Plan Set and Required Order

| Order | Plan | Independently testable outcome |
|---:|---|---|
| 1 | [Runtime Foundation](2026-07-31-manyselves-phase1-01-runtime-foundation.md) | PyQt and tests use a shared, behavior-equivalent runtime composition root |
| 2 | [FastAPI and SSE](2026-07-31-manyselves-phase1-02-fastapi-sse.md) | Headless REST/SSE service controls one Runtime and passes contract tests |
| 3 | [React Workspace](2026-07-31-manyselves-phase1-03-react-workspace.md) | Browser can manage projects/files and provide complete editing/preview behavior |
| 4 | [React Agent and Reporting Parity](2026-07-31-manyselves-phase1-04-react-agent-reporting.md) | Browser covers conversations, Agent runtime, tools, settings, and Reporting |
| 5 | [Electron and Deployment](2026-07-31-manyselves-phase1-05-electron-deployment.md) | Secure desktop shell and Linux Compose distribution are installable |
| 6 | [Verification and Release](2026-07-31-manyselves-phase1-06-verification-release.md) | Feature matrix, recovery, security, compatibility, and release gates all pass |

Do not start a later plan before the preceding plan's final gate is green. A reviewer may approve plans independently, but implementation order remains fixed because later plans consume interfaces produced earlier.

## Locked Cross-Plan Interfaces

The first plan owns these Python interfaces; later plans consume them without renaming:

- `RuntimeHost.create(config_manager: ConfigManager | None = None) -> RuntimeHost`
- `RuntimeHost.start(workspace: Path) -> None`
- `RuntimeHost.stop() -> None`
- `RuntimeHost.is_ready -> bool`
- `RuntimeHost.workspace -> Path | None`
- `RuntimeFacade.snapshot() -> RuntimeSnapshot`
- `RuntimeFacade.send_user_message(command: SendMessageCommand) -> AcceptedCommand`
- `RuntimeFacade.send_file_context(command: SendFileContextCommand) -> AcceptedCommand`
- `RuntimeFacade.interrupt(command: InterruptCommand) -> AcceptedCommand`
- `RuntimeFacade.rollback(command: RollbackCommand) -> RollbackResult`

The second plan owns the event envelope consumed by React and Electron:

```ts
export interface EventEnvelope<T = unknown> {
  schemaVersion: 1;
  eventId: string;
  sequence: number;
  type: string;
  timestamp: string;
  projectId: string | null;
  sessionId: string | null;
  agentId: string | null;
  runId: string | null;
  messageId: string | null;
  payload: T;
}
```

The third plan owns the platform boundary consumed by the Electron plan:

```ts
export interface PlatformBridge {
  kind: "browser" | "electron";
  selectFiles(): Promise<LocalFileRef[]>;
  selectDirectory(): Promise<LocalDirectoryRef | null>;
  saveDownload(input: SaveDownloadInput): Promise<void>;
  openDownloadedFile(path: string): Promise<void>;
  notify(input: NotificationInput): Promise<void>;
}
```

## Integration Gates

### Gate A — Core isolation

Run:

```powershell
uv run pytest tests/application tests/test_app_stderr_filter.py tests/test_bus.py tests/test_manager.py -q
uv run python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: all tests pass and the freeze checker reports no protected-path changes.

### Gate B — Headless server

Run:

```powershell
uv run pytest tests/webapi -q
uv run ruff check manyselves/application manyselves/webapi tests/application tests/webapi
```

Expected: API, SSE replay, lease, path security, conflict, and lifecycle tests pass.

### Gate C — Browser parity

Run:

```powershell
npm --prefix frontend run lint
npm --prefix frontend run test
npm --prefix frontend run build
npm --prefix frontend run e2e
```

Expected: browser project, file, editor, conversation, Agent, Reporting, settings, reconnect, and refresh flows pass.

### Gate D — Desktop and deployment

Run:

```powershell
npm --prefix desktop run lint
npm --prefix desktop run test
npm --prefix desktop run package
docker compose -f deploy/compose.yaml config
```

Expected: secure Electron bridge tests pass, packages build, and Compose validates.

### Gate E — Release

Run:

```powershell
uv run pytest -q
npm --prefix frontend run verify
npm --prefix desktop run verify
uv run python scripts/verify_phase1_matrix.py docs/phase1/feature-parity.csv
```

Expected: every command passes and every parity row is `accepted` with test evidence.

## Commit Policy

- Each task ends in one focused commit after its named tests pass.
- Never mix core behavior changes with adapter, UI, or deployment changes.
- Use prefixes `test:`, `refactor:`, `feat:`, `fix:`, `build:`, and `docs:`.
- Run `git diff --check` before every commit.
- Preserve unrelated user changes; stop if a planned file already contains overlapping uncommitted edits.

## Final Definition of Done

The phase is complete only when all six child plans and Gates A–E pass, a clean Linux host installation succeeds, backup restoration succeeds, existing workspaces open without migration, and the feature parity matrix is accepted at 100%. PyQt remains available after this milestone; removal is a separate future decision.
