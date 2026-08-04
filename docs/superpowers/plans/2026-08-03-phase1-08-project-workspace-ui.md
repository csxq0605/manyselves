# Phase 1 Project Workspace UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the engineering-console shell with the approved light project navigation, project home, six fixed entries, browser-local upload flow, and permission-aware file pages.

**Architecture:** React Router owns page location; `AppLayout` owns only navigation chrome; domain pages use existing project, file, conversation, preview, reporting, and Runtime APIs. A small project metadata store adds display name and description without renaming existing project directories.

**Tech Stack:** React 19, React Router, TanStack Query, Zustand, Vitest, Playwright, FastAPI, filesystem-backed application services.

## Global Constraints

- Left navigation contains only New conversation, Global knowledge, and Projects.
- Every expanded project shows Input, Knowledge, Output templates, Outputs, Runtime, and Logs; none may be omitted.
- Templates and Outputs are independent; Outputs are generated final results.
- All upload buttons select files from the browser computer, never from the server.
- Fixed folders cannot be created, renamed, deleted, or downloaded.
- Existing project directories and Agent/Loop/Reporting architecture remain compatible.
- Remove the permanent Agent sidebar and every server-file/reference/current-selection composer control.

---

### Task 1: Stable project display metadata

**Files:**
- Create: `manyselves/application/project_metadata.py`
- Modify: `manyselves/application/project_registry.py`
- Modify: `manyselves/webapi/schemas/projects.py`
- Modify: `manyselves/webapi/routes/projects.py`
- Test: `tests/application/test_project_metadata.py`
- Modify: `tests/webapi/test_projects_and_files.py`

**Interfaces:**
- Produces: `ProjectMetadata(display_name: str, description: str)`.
- Produces: `ProjectMetadataStore.read(project_root: Path) -> ProjectMetadata` and `write(project_root: Path, metadata: ProjectMetadata, revision: str | None) -> str`.
- Produces: `ProjectResponse{id, displayName, description, revision, active}`, `ProjectCreateRequest{projectId, displayName, description}`, and `ProjectUpdateRequest{displayName, description, revision}`.
- Existing project ID rename endpoint is removed from the ordinary pencil flow; project IDs remain stable.

- [x] **Step 1: Write failing metadata compatibility and route tests**

```python
def test_missing_metadata_uses_project_id(tmp_path: Path) -> None:
    project = tmp_path / "energy"
    ensure_project_structure(project)
    assert ProjectMetadataStore().read(project) == ProjectMetadata("energy", "")

@pytest.mark.asyncio
async def test_patch_updates_display_metadata_without_renaming_directory(authed_client):
    response = await authed_client.patch(
        "/api/v1/projects/project-1",
        headers={"X-Control-Lease-Token": authed_client.lease_token},
        json={"displayName": "能源管理", "description": "企业能耗分析"},
    )
    assert response.json()["id"] == "project-1"
    assert response.json()["displayName"] == "能源管理"
```

- [x] **Step 2: Run focused tests and verify failure**

Run: `uv run pytest tests/application/test_project_metadata.py tests/webapi/test_projects_and_files.py -q -k "metadata or display"`

Expected: FAIL because the metadata store and response fields do not exist.

- [x] **Step 3: Implement project metadata with atomic revision writes**

Store JSON at `<project>/.manyselves/project.json`, reject symlinked metadata paths, use the directory name fallback, validate non-empty display names up to 120 characters and descriptions up to 1000 characters, and write via sibling temporary file plus `Path.replace()`. Return a SHA-256 revision and reject stale revisions with the existing conflict envelope style.

- [x] **Step 4: Run focused and project API tests**

Run: `uv run pytest tests/application/test_project_metadata.py tests/webapi/test_projects_and_files.py -q`

Expected: PASS, including old projects without metadata.

- [x] **Step 5: Commit project metadata**

```bash
git add manyselves/application/project_metadata.py manyselves/application/project_registry.py manyselves/webapi/schemas/projects.py manyselves/webapi/routes/projects.py tests/application/test_project_metadata.py tests/webapi/test_projects_and_files.py
git commit -m "feat: add stable project display metadata"
```

### Task 2: Route-based light application shell and complete project tree

**Files:**
- Create: `frontend/src/app/routes.tsx`
- Create: `frontend/src/features/shell/AppLayout.tsx`
- Create: `frontend/src/features/shell/Sidebar.tsx`
- Create: `frontend/src/features/shell/light-shell.css`
- Create: `frontend/src/features/projects/ProjectHomePage.tsx`
- Create: `frontend/src/features/projects/ProjectHomePage.test.tsx`
- Create: `frontend/src/features/shell/Sidebar.test.tsx`
- Modify: `frontend/src/app/App.tsx`
- Modify: `frontend/src/main.tsx`
- Delete: `frontend/src/features/shell/AppShell.tsx`
- Delete: `frontend/src/features/shell/app-shell.css`
- Delete: `frontend/src/features/agents/AgentSidebar.tsx`
- Delete: `frontend/src/features/agents/agent-sidebar.css`
- Modify: `frontend/src/features/shell/AppShell.test.tsx`

**Interfaces:**
- Consumes: authenticated app state from Plan 07.
- Produces: routes listed in the approved design and `ProjectSection = "inputs" | "knowledge" | "templates" | "outputs" | "runtime" | "logs"`.
- Produces: `Sidebar` with `onCreateProject`, project metadata edit, and ellipsis delete callbacks.

- [x] **Step 1: Write failing shell and routing tests**

```tsx
it("renders all six fixed entries for an expanded project", () => {
  renderSidebar("/projects/project-1/outputs");
  for (const name of ["输入", "知识库", "输出模板", "输出", "运行态", "日志"]) {
    expect(screen.getByRole("link", { name })).toBeVisible();
  }
});

it("shows conversations on the project home instead of in the directory tree", () => {
  renderProjectHome();
  expect(screen.getByRole("heading", { name: "最近对话" })).toBeVisible();
  expect(screen.getByRole("button", { name: "新对话" })).toBeVisible();
});
```

- [x] **Step 2: Run focused tests and verify the old shell fails the contract**

Run: `npm test -- --run src/features/shell src/features/projects/ProjectHomePage.test.tsx`

Working directory: `frontend`

Expected: FAIL because the router, light shell, and complete tree do not exist.

- [x] **Step 3: Implement routes and the light shell**

Define the route set exactly as the approved spec. `AppLayout` renders `Sidebar` and `<Outlet />`; it must not construct editor, preview, reporting, settings, conversation, or Agent stores. Use a light neutral palette, visible focus rings, a 238px desktop sidebar, responsive stacking/collapse below 700px, and no dark-only settings surfaces. The project row exposes visible pencil and ellipsis buttons. The Outputs entry must be distinct from Output templates.

- [x] **Step 4: Run shell tests, accessibility assertions, build, and lint**

Run: `npm test -- --run src/features/shell src/features/projects/ProjectHomePage.test.tsx`

Run: `npm run build`

Run: `npm run lint`

Working directory: `frontend`

Expected: PASS with no unused old AppShell imports.

- [x] **Step 5: Commit the light routed shell**

```bash
git add frontend/src/app frontend/src/features/shell frontend/src/features/projects frontend/src/features/agents frontend/src/main.tsx
git commit -m "feat: add light project navigation shell"
```

### Task 3: Capability-based project directory pages

**Files:**
- Create: `frontend/src/features/files/project-sections.ts`
- Create: `frontend/src/features/files/ProjectDirectoryPage.tsx`
- Create: `frontend/src/features/files/ProjectDirectoryPage.test.tsx`
- Create: `frontend/src/features/files/FileList.tsx`
- Create: `frontend/src/features/files/FileRowMenu.tsx`
- Create: `frontend/src/features/files/UploadConflictDialog.tsx`
- Modify: `frontend/src/features/files/file-api.ts`
- Modify: `frontend/src/features/files/UploadQueue.tsx`
- Modify: `frontend/src/features/editor/editable-files.ts`
- Modify: `frontend/src/features/preview/PreviewWorkspace.tsx`
- Modify: `frontend/src/features/projects/ProjectWorkspace.tsx`
- Modify: `frontend/src/features/projects/ProjectWorkspace.test.tsx`
- Modify: `manyselves/webapi/schemas/files.py`
- Modify: `manyselves/webapi/routes/files.py`
- Modify: `manyselves/application/workspace_files.py`
- Modify: `tests/application/test_workspace_files.py`
- Modify: `tests/webapi/test_projects_and_files.py`

**Interfaces:**
- Produces: `SECTION_CAPABILITIES` mapping each project section to its physical root and allowed actions.
- Produces: `WorkspaceFiles.upload(relative_path: str, chunks: AsyncIterator[bytes], *, conflict: Literal["reject", "replace", "keep-both"] = "reject", base_revision: str | None = None) -> FileEntry`.
- Produces: server conflict code `FILE_ALREADY_EXISTS` and response details containing only the relative logical path.

- [ ] **Step 1: Write failing permission and upload-conflict tests**

```ts
expect(SECTION_CAPABILITIES.templates.root).toBe("Templates");
expect(SECTION_CAPABILITIES.outputs).toMatchObject({ root: "Outputs", upload: false, edit: false, download: true });

it("never offers upload or edit on outputs", async () => {
  renderDirectory("outputs");
  expect(screen.queryByRole("button", { name: "上传文件" })).not.toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "下载" })).toBeVisible();
});
```

```python
@pytest.mark.asyncio
async def test_keep_both_generates_a_server_side_name(files: WorkspaceFiles) -> None:
    async def chunks(value: bytes):
        yield value

    await files.upload("Inputs/data.txt", chunks(b"one"), conflict="reject")
    result = await files.upload("Inputs/data.txt", chunks(b"two"), conflict="keep-both")
    assert result.path == "Inputs/data (1).txt"
```

- [ ] **Step 2: Run focused frontend and backend tests and verify failure**

Run: `npm test -- --run src/features/files/ProjectDirectoryPage.test.tsx src/features/projects/ProjectWorkspace.test.tsx`

Working directory: `frontend`

Run: `uv run pytest tests/application/test_workspace_files.py tests/webapi/test_projects_and_files.py -q -k "conflict or keep_both or output"`

Expected: FAIL because capability mapping and conflict modes do not exist.

- [ ] **Step 3: Implement server conflict modes and section allow-lists**

Server routes must derive the authorized root from the page/API operation, not trust a client-provided absolute directory. Sanitize names, stream to a `.manyselves-tmp-*` sibling, enforce existing limits, fsync/replace on success, and remove temporary files on disconnect. `keep-both` chooses `name (1).ext`, then increments deterministically. Outputs reject upload/write/replace regardless of client input.

- [ ] **Step 4: Implement the shared directory page**

Use one `ProjectDirectoryPage` for Input, Knowledge, Templates, and Outputs. Text/code rows offer edit; PDF, DOCX, spreadsheets, and images offer preview; Outputs offer preview/download/delete only. Existing subdirectories render as expandable navigation but expose no create/rename/delete menu. Outputs display `Reports`, `Modules`, and `Reviews` without flattening or hiding them.

- [ ] **Step 5: Run file tests and build**

Run: `uv run pytest tests/application/test_workspace_files.py tests/webapi/test_projects_and_files.py -q`

Run: `npm test -- --run src/features/files src/features/projects/ProjectWorkspace.test.tsx src/features/preview`

Run: `npm run build`

Working directory for npm commands: `frontend`

Expected: PASS.

- [ ] **Step 6: Commit directory capabilities and upload conflicts**

```bash
git add manyselves/application/workspace_files.py manyselves/webapi/routes/files.py manyselves/webapi/schemas/files.py tests/application/test_workspace_files.py tests/webapi/test_projects_and_files.py frontend/src/features/files frontend/src/features/editor/editable-files.ts frontend/src/features/preview frontend/src/features/projects
git commit -m "feat: add capability-based project file pages"
```

### Task 4: Project-bound conversations and browser-computer attachments

**Files:**
- Modify: `frontend/src/features/chat/MessageComposer.tsx`
- Modify: `frontend/src/features/chat/MessageComposer.test.tsx`
- Delete: `frontend/src/features/chat/FileReferencePicker.tsx`
- Delete: `frontend/src/features/chat/FileReferencePicker.test.tsx`
- Modify: `frontend/src/features/conversations/ConversationWorkspace.tsx`
- Modify: `frontend/src/features/conversations/ConversationWorkspace.test.tsx`
- Modify: `frontend/src/features/conversations/ConversationList.tsx`
- Modify: `frontend/src/features/conversations/conversation-api.ts`
- Modify: `manyselves/webapi/schemas/conversations.py`
- Modify: `manyselves/webapi/routes/conversations.py`
- Modify: `manyselves/application/conversation_service.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`
- Modify: `frontend/e2e/conversations.spec.ts`

**Interfaces:**
- Produces: every conversation response and mutation carries/validates `projectId`.
- Produces: composer attachments `{path, name, size}` created only after browser-file upload to `Inputs/`.
- Produces: top-level new-conversation flow that selects/creates a project before creating a session.

- [ ] **Step 1: Write failing project-binding and composer tests**

```tsx
it("plus opens the browser file picker and uploads to project Inputs", async () => {
  renderComposer({ projectId: "energy" });
  await user.click(screen.getByRole("button", { name: "上传本地文件" }));
  await user.upload(screen.getByLabelText("选择本地文件"), [new File(["x"], "brief.txt")]);
  expect(api.upload).toHaveBeenCalledWith("energy", "Inputs", expect.any(File), "reject");
});

it("does not render server-reference controls", () => {
  renderComposer({ projectId: "energy" });
  expect(screen.queryByText("引用服务器文件")).not.toBeInTheDocument();
  expect(screen.queryByText("引用当前选区")).not.toBeInTheDocument();
});
```

```python
@pytest.mark.asyncio
async def test_conversation_project_mismatch_is_rejected(authed_client):
    response = await authed_client.post(
        "/api/v1/conversations/session-1/messages",
        json={"projectId": "other", "content": "run"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONVERSATION_PROJECT_MISMATCH"
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `npm test -- --run src/features/chat/MessageComposer.test.tsx src/features/conversations`

Working directory: `frontend`

Run: `uv run pytest tests/webapi/test_conversations_agents_reporting.py -q -k "conversation and project"`

Expected: FAIL because server references still exist and conversation project identity is not explicit.

- [ ] **Step 3: Implement project-bound conversation contracts**

Persist the project ID in session metadata and validate it before mutation. Existing session files remain compatible by binding legacy sessions to their containing project workspace. Top-level new conversation requires a project choice; project-home creation uses the current route project. Keep project activation through existing `RuntimeFacade` transaction and return existing Runtime busy errors rather than introducing parallel runtimes.

- [ ] **Step 4: Implement browser attachment flow**

Use an actual hidden `<input type="file" multiple>` and drag/drop surface. Upload selected `File` objects through multipart to current `Inputs/`; only successful server responses become chips. Removing a chip does not call delete. Sending includes the logical uploaded references as focused inputs and never includes a local absolute path.

- [ ] **Step 5: Run conversation tests and E2E**

Run: `uv run pytest tests/webapi/test_conversations_agents_reporting.py -q`

Run: `npm test -- --run src/features/chat src/features/conversations`

Run: `npm run e2e -- conversations.spec.ts`

Working directory for npm commands: `frontend`

Expected: PASS.

- [ ] **Step 6: Commit project conversations and local upload composer**

```bash
git add manyselves/application/conversation_service.py manyselves/webapi/schemas/conversations.py manyselves/webapi/routes/conversations.py tests/webapi/test_conversations_agents_reporting.py frontend/src/features/chat frontend/src/features/conversations frontend/e2e/conversations.spec.ts
git commit -m "feat: bind browser conversations to projects"
```

### Task 5: Outputs, Runtime, and Logs pages

**Files:**
- Create: `frontend/src/features/runtime/RuntimePage.tsx`
- Create: `frontend/src/features/runtime/RuntimePage.test.tsx`
- Create: `frontend/src/features/logs/LogsPage.tsx`
- Create: `frontend/src/features/logs/LogsPage.test.tsx`
- Create: `frontend/src/features/logs/log-api.ts`
- Modify: `frontend/src/features/reporting/OutputArtifacts.tsx`
- Modify: `frontend/src/features/reporting/ReportingWorkspace.tsx`
- Modify: `manyselves/webapi/routes/events.py`
- Modify: `manyselves/webapi/events/models.py`
- Test: `tests/webapi/test_sse.py`
- Modify: `frontend/e2e/reporting.spec.ts`
- Modify: `frontend/e2e/recovery.spec.ts`

**Interfaces:**
- Consumes: existing bootstrap Runtime snapshot, SSE event envelope, and Reporting API.
- Produces: read-only Runtime page and read-only/filterable log projection; no raw `Work/` or `.manyselves` browser.
- Produces: Outputs page actions preview/download/delete only.

- [ ] **Step 1: Write failing read-only page tests**

```tsx
it("shows runtime status without filesystem controls", () => {
  render(<RuntimePage snapshot={snapshot} />);
  expect(screen.getByRole("heading", { name: "运行态" })).toBeVisible();
  expect(screen.queryByRole("button", { name: /上传|新建目录|重命名/ })).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `npm test -- --run src/features/runtime src/features/logs src/features/reporting`

Working directory: `frontend`

Expected: FAIL because the routed pages do not exist.

- [ ] **Step 3: Implement controlled projections**

Runtime page renders current task, Agent status, queue, tool calls, progress, and errors from the existing sanitized snapshot. Logs page renders sanitized project/runtime events with timestamp, level/type, and message, plus a download action for the same sanitized projection. Do not add a generic server log-file path API and do not expose raw debug values rejected by the existing sanitizer.

- [ ] **Step 4: Run runtime, SSE, reporting, and E2E tests**

Run: `uv run pytest tests/webapi/test_sse.py tests/webapi/test_event_sanitizer.py tests/webapi/test_conversations_agents_reporting.py -q`

Run: `npm test -- --run src/features/runtime src/features/logs src/features/reporting`

Run: `npm run e2e -- reporting.spec.ts recovery.spec.ts`

Working directory for npm commands: `frontend`

Expected: PASS.

- [ ] **Step 5: Commit read-only operations pages**

```bash
git add frontend/src/features/runtime frontend/src/features/logs frontend/src/features/reporting frontend/e2e manyselves/webapi/routes/events.py manyselves/webapi/events/models.py tests/webapi/test_sse.py
git commit -m "feat: add project runtime logs and outputs views"
```
