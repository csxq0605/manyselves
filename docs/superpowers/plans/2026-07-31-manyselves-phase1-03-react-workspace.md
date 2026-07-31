# Manyselves Phase 1 React Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared React application shell with reliable server synchronization, project/file management, multi-tab editing, and all current file preview capabilities.

**Architecture:** Generate API models from the locked OpenAPI artifact, keep authoritative snapshots in TanStack Query, keep ephemeral workspace UI state in Zustand, and update snapshots from recoverable SSE events. Files are always server-relative; unsaved drafts are local and revision-aware.

**Tech Stack:** React 19, TypeScript strict mode, Vite, React Router, TanStack Query, Zustand, Monaco Editor, Vitest, React Testing Library, MSW, Playwright.

## Global Constraints

- `frontend-contract/openapi.json` is the sole HTTP type source.
- Components do not call `fetch` directly; all requests go through `ApiGateway`.
- Components do not reference Electron globals; native operations go through `PlatformBridge`.
- Server content and revision are authoritative; local drafts never overwrite a stale revision silently.
- Preview renderers sanitize active content and apply explicit size/row limits.
- Keyboard-only navigation, visible focus, labels, and reduced-motion behavior are required.

---

### Task 1: Scaffold React, Tooling, Generated Client, and Platform Boundary

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/eslint.config.js`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/platform/types.ts`
- Create: `frontend/src/platform/browser-platform.ts`
- Create: `frontend/src/api/generated/`
- Create: `frontend/src/api/gateway.ts`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/platform/browser-platform.test.ts`

**Interfaces:**
- Consumes: `frontend-contract/openapi.json`.
- Produces: `ApiGateway`, `PlatformBridge`, generated DTOs, `npm run generate:api`, `lint`, `test`, `build`, and `verify` scripts.

- [ ] **Step 1: Create package scripts and write boundary tests**

```ts
it("browser selection returns uploaded File objects without local paths", async () => {
  const platform = new BrowserPlatformBridge(fileInputDriver([new File(["x"], "a.txt")]));
  const selected = await platform.selectFiles();
  expect(selected).toEqual([{ name: "a.txt", size: 1, file: expect.any(File) }]);
  expect(selected[0]).not.toHaveProperty("path");
});

it("gateway sends bearer and idempotency headers", async () => {
  const gateway = createApiGateway({ baseUrl: "https://server", token: "secret", fetch: fetchMock });
  await gateway.sendMessage("main", { content: "hello", source: "user" }, "cmd-id");
  expect(fetchMock).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({
    headers: expect.objectContaining({ Authorization: "Bearer secret", "Idempotency-Key": "cmd-id" }),
  }));
});
```

- [ ] **Step 2: Install locked dependencies and confirm tests fail before implementation**

Run from `frontend/`:

```powershell
npm install react react-dom react-router-dom @tanstack/react-query zustand
npm install -D typescript vite @vitejs/plugin-react eslint vitest jsdom @testing-library/react @testing-library/user-event msw openapi-typescript
npm run test -- --run
```

Expected: boundary tests FAIL because implementations are missing; `package-lock.json` records exact versions.

- [ ] **Step 3: Implement the exact platform contract and API generation**

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

`generate:api` runs `openapi-typescript ../frontend-contract/openapi.json -o src/api/generated/schema.ts`. `ApiGateway` accepts `baseUrl`, token provider, fetch implementation, and client ID; map every non-2xx response into a typed `ApiError` containing code, message, retryable, details, requestId, and HTTP status. SSE uses a fetch-stream parser rather than native `EventSource`, so the client can send the bearer token and `Last-Event-ID` header without putting credentials in the URL.

- [ ] **Step 4: Run frontend foundation checks**

Run:

```powershell
npm --prefix frontend run generate:api
npm --prefix frontend run lint
npm --prefix frontend run test -- --run
npm --prefix frontend run build
```

Expected: PASS with no TypeScript errors and no handwritten duplicate HTTP DTOs.

- [ ] **Step 5: Commit the frontend foundation**

```powershell
git add frontend
git diff --cached --check
git commit -m "feat: scaffold typed react client"
```

### Task 2: Add Bootstrap, Connection State, SSE Recovery, and Application Shell

**Files:**
- Create: `frontend/src/app/App.tsx`
- Create: `frontend/src/app/router.tsx`
- Create: `frontend/src/app/providers.tsx`
- Create: `frontend/src/api/event-stream.ts`
- Create: `frontend/src/store/connection-store.ts`
- Create: `frontend/src/store/workspace-store.ts`
- Create: `frontend/src/features/shell/AppShell.tsx`
- Create: `frontend/src/features/shell/ConnectionBanner.tsx`
- Create: `frontend/src/api/event-stream.test.ts`
- Create: `frontend/src/features/shell/AppShell.test.tsx`

**Interfaces:**
- Consumes: `ApiGateway.bootstrap()`, `/api/v1/events`, `EventEnvelope`.
- Produces: application providers, reconnecting `EventStream`, global connection states, responsive three-pane shell.

- [ ] **Step 1: Write reconnect and resync tests**

```ts
it("reconnects with the last event id and refreshes on resync", async () => {
  server.send(event({ eventId: "evt-9", sequence: 9, type: "agent.status.changed" }));
  server.disconnect();
  server.reconnect();
  expect(server.lastRequestHeader("Last-Event-ID")).toBe("evt-9");
  server.send(event({ eventId: "evt-10", sequence: 10, type: "stream.resync_required" }));
  await waitFor(() => expect(gateway.bootstrap).toHaveBeenCalledTimes(2));
});

it("shows offline state without discarding local drafts", async () => {
  renderApp({ initialDraft: "unsaved", streamState: "offline" });
  expect(screen.getByRole("status")).toHaveTextContent("连接已中断");
  expect(screen.getByRole("textbox")).toHaveValue("unsaved");
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `npm --prefix frontend run test -- --run src/api/event-stream.test.ts src/features/shell/AppShell.test.tsx`
Expected: FAIL because shell and stream do not exist.

- [ ] **Step 3: Implement state ownership and recovery**

Connection states are exactly `connecting | online | reconnecting | resyncing | offline | unauthorized`. Bootstrap populates TanStack Query caches. SSE invalidates or patches named resource caches by event type; unknown event types are logged once and ignored. Exponential reconnect delays are 1, 2, 5, 10, then 15 seconds with jitter; successful connection resets the delay.

The shell provides project/file navigation, editor workspace, Agent panel, conversation area, connection banner, control-owner indicator, and keyboard landmarks without implementing feature content yet.

- [ ] **Step 4: Run unit, accessibility smoke, and build checks**

Run:

```powershell
npm --prefix frontend run test -- --run
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: PASS; initial load, 401, offline, reconnect, and resync states have explicit UI.

- [ ] **Step 5: Commit the resilient shell**

```powershell
git add frontend/src
git diff --cached --check
git commit -m "feat: add resilient react application shell"
```

### Task 3: Add Project Lifecycle, File Tree, Import, Download, and Mutations

**Files:**
- Create: `frontend/src/features/projects/project-api.ts`
- Create: `frontend/src/features/projects/ProjectSwitcher.tsx`
- Create: `frontend/src/features/projects/CreateProjectDialog.tsx`
- Create: `frontend/src/features/files/file-api.ts`
- Create: `frontend/src/features/files/FileTree.tsx`
- Create: `frontend/src/features/files/FileActions.tsx`
- Create: `frontend/src/features/files/UploadQueue.tsx`
- Create: `frontend/src/features/files/file-tree-store.ts`
- Create: `frontend/src/features/projects/ProjectSwitcher.test.tsx`
- Create: `frontend/src/features/files/FileTree.test.tsx`

**Interfaces:**
- Consumes: project/file endpoints and `PlatformBridge.selectFiles/selectDirectory/saveDownload`.
- Produces: project create/list/activate UI, accessible file tree, explicit import, upload progress, atomic mutations, download.

- [ ] **Step 1: Write project activation and file mutation tests**

```ts
it("does not switch UI project until activation succeeds", async () => {
  gateway.activateProject.mockRejectedValue(apiError("RUNTIME_BUSY"));
  render(<ProjectSwitcher current="p1" projects={[project("p1"), project("p2")]} />);
  await userEvent.selectOptions(screen.getByLabelText("项目"), "p2");
  expect(screen.getByLabelText("项目")).toHaveValue("p1");
  expect(screen.getByRole("alert")).toHaveTextContent("当前任务运行中");
});

it("imports selected files into the chosen server directory", async () => {
  platform.selectFiles.mockResolvedValue([localFile("a.csv", "x")]);
  render(<FileActions directory="Inputs" />);
  await userEvent.click(screen.getByRole("button", { name: "导入文件" }));
  expect(gateway.uploadFile).toHaveBeenCalledWith("Inputs", expect.objectContaining({ name: "a.csv" }));
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `npm --prefix frontend run test -- --run src/features/projects src/features/files`
Expected: FAIL because components are missing.

- [ ] **Step 3: Implement the complete file-tree interaction set**

Use WAI-ARIA tree/treeitem semantics, keyboard arrows, Enter to open, F2 to rename, Delete with confirmation, and context actions for create file, create directory, upload, download, refresh, rename, and delete. Persist only expanded relative paths per project. Upload shows queued/uploading/completed/failed states and retry; cancelled or failed uploads never appear as completed server files.

Project activation requires the control lease, confirms when drafts are dirty, and refetches bootstrap after success. Local folder selection displays an import summary and destination before upload; never claim ongoing synchronization.

- [ ] **Step 4: Run unit and mocked integration tests**

Run:

```powershell
npm --prefix frontend run test -- --run src/features/projects src/features/files
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: PASS for create, activate, busy rejection, tree navigation, mutations, upload retry, and download.

- [ ] **Step 5: Commit project and file management**

```powershell
git add frontend/src/features/projects frontend/src/features/files
git diff --cached --check
git commit -m "feat: add server backed project workspace"
```

### Task 4: Add Multi-Tab Text Editor, Draft Recovery, and Revision Conflict UI

**Files:**
- Create: `frontend/src/features/editor/editor-store.ts`
- Create: `frontend/src/features/editor/EditorTabs.tsx`
- Create: `frontend/src/features/editor/TextEditor.tsx`
- Create: `frontend/src/features/editor/SaveConflictDialog.tsx`
- Create: `frontend/src/features/editor/draft-storage.ts`
- Create: `frontend/src/features/editor/selection-context.ts`
- Create: `frontend/src/features/editor/EditorTabs.test.tsx`
- Create: `frontend/src/features/editor/SaveConflictDialog.test.tsx`

**Interfaces:**
- Consumes: file content/read/write endpoints, Monaco, file-changed events.
- Produces: tabs, language modes, dirty drafts, save, conflict resolution, selection attachment, tab restoration.

- [ ] **Step 1: Write dirty-tab and conflict tests**

```ts
it("keeps a draft when server revision changes", async () => {
  renderEditor(serverFile({ content: "old", revision: "r1" }));
  await userEvent.type(screen.getByRole("textbox"), " local");
  emitFileChanged({ path: "Inputs/a.md", revision: "r2" });
  expect(screen.getByText("服务器文件已更新")).toBeVisible();
  expect(getDraft("Inputs/a.md")).toContain("local");
});

it("sends selection metadata without inventing other tabs", async () => {
  const context = buildSelectionContext({ path: "Inputs/a.py", startLine: 2, endLine: 4 });
  expect(context).toEqual({ type: "selection", file: "Inputs/a.py", startLine: 2, endLine: 4 });
});
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
npm --prefix frontend install @monaco-editor/react idb
npm --prefix frontend run test -- --run src/features/editor
```

Expected: FAIL because editor modules do not exist.

- [ ] **Step 3: Implement exact editor behavior**

Support editable extensions `.py`, `.json`, `.yaml`, `.yml`, `.md`, `.csv`, and configured text extensions. Store `path`, `serverRevision`, `serverContent`, `draftContent`, `dirty`, cursor, and view state per tab. Save sends `baseRevision`; a 409 opens a three-action dialog: reload server, keep draft without saving, or explicitly overwrite after fetching the latest revision and confirming again.

Persist bounded drafts in IndexedDB keyed by server URL, project ID, and path. On successful save remove the draft. On close, project switch, logout, or page unload, dirty tabs remain recoverable and the user receives a clear confirmation.

- [ ] **Step 4: Run editor tests and production build**

Run:

```powershell
npm --prefix frontend run test -- --run src/features/editor
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: PASS for tab restore, close confirmation, save, 409 conflict, external change, draft recovery, and selection context.

- [ ] **Step 5: Commit the editor workspace**

```powershell
git add frontend/src/features/editor
git diff --cached --check
git commit -m "feat: add revision aware workspace editor"
```

### Task 5: Add Complete Safe Preview Registry and Python Run Action

**Files:**
- Create: `frontend/src/features/preview/PreviewPane.tsx`
- Create: `frontend/src/features/preview/preview-registry.ts`
- Create: `frontend/src/features/preview/TextPreview.tsx`
- Create: `frontend/src/features/preview/MarkdownPreview.tsx`
- Create: `frontend/src/features/preview/PdfPreview.tsx`
- Create: `frontend/src/features/preview/ImagePreview.tsx`
- Create: `frontend/src/features/preview/SpreadsheetPreview.tsx`
- Create: `frontend/src/features/preview/DocxPreview.tsx`
- Create: `frontend/src/features/preview/PreviewPane.test.tsx`
- Create: `frontend/e2e/workspace.spec.ts`

**Interfaces:**
- Consumes: file preview/download endpoints and Python-run command endpoint.
- Produces: complete preview dispatch, zoom/navigation, bounded tables, safe HTML, trusted-server Python run action.

- [ ] **Step 1: Write a renderer matrix and safety tests**

```ts
it.each([
  ["a.pdf", "pdf"], ["a.png", "image"], ["a.svg", "image"], ["a.xlsx", "spreadsheet"],
  ["a.csv", "spreadsheet"], ["a.docx", "docx"], ["a.md", "markdown"], ["a.py", "text"],
])("selects %s renderer", (path, renderer) => {
  expect(selectPreviewRenderer(path)).toBe(renderer);
});

it("sanitizes active markdown and svg content", () => {
  render(<PreviewPane file={maliciousPreview()} />);
  expect(document.querySelector("script")).toBeNull();
  expect(document.querySelector("[onerror]")).toBeNull();
});
```

- [ ] **Step 2: Run preview tests and verify failure**

Run:

```powershell
npm --prefix frontend install react-markdown remark-gfm dompurify pdfjs-dist
npm --prefix frontend run test -- --run src/features/preview
```

Expected: FAIL because preview registry is missing.

- [ ] **Step 3: Implement preview equivalence**

PDF supports page navigation, fit width, zoom, and range requests. Images support zoom, fit, and safe SVG rendering. Spreadsheet and CSV previews expose sheet selection, rows/columns, cell text, loading and truncation notices. DOCX displays paragraphs, tables, and extracted images in document order. Markdown permits a documented safe subset. Unsupported/binary files display metadata and download rather than decoding arbitrary bytes.

The Python run action requires control lease, confirms that execution occurs on the trusted server, streams stdout/stderr as operation events, supports interrupt, and never claims OS-level sandboxing that the current core does not provide.

- [ ] **Step 4: Run workspace E2E and Gate C workspace subset**

Run:

```powershell
npm --prefix frontend run test -- --run
npm --prefix frontend run build
npm --prefix frontend run e2e -- workspace.spec.ts
```

Expected: PASS for all renderer fixtures, file edit/save/conflict, import/download, project switch, Python run/interrupt, refresh, and draft restore.

- [ ] **Step 5: Commit complete workspace behavior**

```powershell
git add frontend/src/features/preview frontend/e2e/workspace.spec.ts docs/phase1/feature-parity.csv
git diff --cached --check
git commit -m "feat: complete browser workspace parity"
```
