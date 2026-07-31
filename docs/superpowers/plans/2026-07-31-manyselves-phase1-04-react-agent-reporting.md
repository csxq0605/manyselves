# Manyselves Phase 1 React Agent and Reporting Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete browser parity for conversations, streaming Agent interaction, dynamic agents, tools, debug, provider settings, and the full Reporting lifecycle.

**Architecture:** REST loads durable conversation/run snapshots and submits idempotent commands; SSE incrementally updates message, Agent, tool, task, checkpoint, and Reporting read models. UI stores only display state and unsent drafts locally.

**Tech Stack:** React, TypeScript, TanStack Query, Zustand, Vitest, React Testing Library, MSW, Playwright.

## Global Constraints

- Never infer completion from a dropped stream; terminal state comes from a terminal event or refreshed snapshot.
- Conversation persistence remains the server's existing ConversationStore.
- Thinking, debug payloads, system notices, errors, tools, and user-visible messages remain distinct UI records.
- Provider secrets are never returned; UI receives configured/masked status only.
- Every existing Reporting state and user-input transition has a named UI state and test.

---

### Task 1: Add Conversation History and Message Composition

**Files:**
- Create: `frontend/src/features/conversations/conversation-api.ts`
- Create: `frontend/src/features/conversations/ConversationList.tsx`
- Create: `frontend/src/features/conversations/ConversationActions.tsx`
- Create: `frontend/src/features/chat/MessageList.tsx`
- Create: `frontend/src/features/chat/MessageComposer.tsx`
- Create: `frontend/src/features/chat/message-store.ts`
- Create: `frontend/src/features/chat/FileReferencePicker.tsx`
- Create: `frontend/src/features/chat/CommandPalette.tsx`
- Create: `frontend/src/features/conversations/ConversationList.test.tsx`
- Create: `frontend/src/features/chat/MessageComposer.test.tsx`

**Interfaces:**
- Consumes: conversation routes, message/file-context commands, control lease, editor selection context.
- Produces: new/switch/rename/delete/clear/history, compose/send, file reference, command palette, edit-resend and rollback entry points.

- [ ] **Step 1: Write conversation and duplicate-send tests**

```ts
it("switches conversation only after server activation", async () => {
  renderConversationList({ active: "s1", sessions: [session("s1"), session("s2")] });
  await userEvent.click(screen.getByRole("button", { name: "s2" }));
  expect(gateway.activateConversation).toHaveBeenCalledWith("s2");
  await screen.findByText("s2 已激活");
});

it("reuses one idempotency key while retrying the same send", async () => {
  gateway.sendMessage.mockRejectedValueOnce(networkError()).mockResolvedValueOnce(accepted("cmd-1"));
  await composeAndRetry("hello");
  expect(gateway.sendMessage.mock.calls[0][2]).toBe(gateway.sendMessage.mock.calls[1][2]);
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `npm --prefix frontend run test -- --run src/features/conversations src/features/chat`
Expected: FAIL because components are missing.

- [ ] **Step 3: Implement complete conversation behavior**

Session activation requires control lease because the Runtime session is global. Rename validates trimmed non-empty names; delete/clear require confirmation; active-session deletion follows server-selected replacement. Composer supports text, server file references, current editor file, current selection metadata, slash commands, send, stop, and per-session unsent drafts. Edit-resend truncates from the chosen message only after server confirmation; rollback renders restored file count and refreshed history.

- [ ] **Step 4: Run chat tests and build**

Run:

```powershell
npm --prefix frontend run test -- --run src/features/conversations src/features/chat
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: PASS for all session operations, send/retry, file context, stop, edit-resend, rollback, and draft preservation.

- [ ] **Step 5: Commit conversations and composer**

```powershell
git add frontend/src/features/conversations frontend/src/features/chat
git diff --cached --check
git commit -m "feat: add complete conversation workflow"
```

### Task 2: Add Streaming Messages, Agent State, Queue, Tools, Tasks, and Debug

**Files:**
- Create: `frontend/src/features/agents/agent-store.ts`
- Create: `frontend/src/features/agents/AgentSidebar.tsx`
- Create: `frontend/src/features/agents/AgentStatus.tsx`
- Create: `frontend/src/features/agents/QueueView.tsx`
- Create: `frontend/src/features/agents/ThinkingView.tsx`
- Create: `frontend/src/features/agents/TaskBoard.tsx`
- Create: `frontend/src/features/tools/ToolCallGroup.tsx`
- Create: `frontend/src/features/debug/DebugPanel.tsx`
- Create: `frontend/src/features/agents/event-reducer.ts`
- Create: `frontend/src/features/agents/event-reducer.test.ts`

**Interfaces:**
- Consumes: runtime snapshot and Agent/tool/task/debug SSE envelopes.
- Produces: deterministic event reducer and complete runtime visualization.

- [ ] **Step 1: Write ordered and duplicate-event reducer tests**

```ts
it("combines deltas and finalizes exactly once", () => {
  const events = [started("m1"), delta("m1", "hel"), delta("m1", "lo"), completed("m1", "hello")];
  const state = events.reduce(reduceAgentEvent, emptyAgentState());
  expect(state.messages["m1"].content).toBe("hello");
  expect(state.messages["m1"].status).toBe("completed");
  expect(reduceAgentEvent(state, completed("m1", "hello"))).toEqual(state);
});

it("keeps tool failures separate from agent message failures", () => {
  const state = reduceAgentEvent(emptyAgentState(), toolFailed("t1", "permission denied"));
  expect(state.tools["t1"].status).toBe("failed");
  expect(state.runtimeError).toBeNull();
});
```

- [ ] **Step 2: Run reducer tests and verify failure**

Run: `npm --prefix frontend run test -- --run src/features/agents src/features/tools src/features/debug`
Expected: FAIL because reducer and views do not exist.

- [ ] **Step 3: Implement explicit event-state transitions**

Index messages by message ID, tools by tool-call ID, agents by normalized agent ID, and tasks by task ID. Ignore event IDs already applied. When a sequence gap is detected, mark state stale and request snapshot refresh. Render main and dynamic Agent identity, running/idle/error/waiting status, queue depth/items, Thinking separately from public answer, grouped tool arguments/results/errors, task snapshots, checkpoints, system notices, API debug events, usage metadata, and interrupt state.

Large payloads start collapsed and remain keyboard accessible. Secrets matching configured redaction patterns are masked before rendering debug/tool data.

- [ ] **Step 4: Run event and component tests**

Run:

```powershell
npm --prefix frontend run test -- --run src/features/agents src/features/tools src/features/debug
npm --prefix frontend run lint
```

Expected: PASS for ordered, duplicate, missing, reconnect, tool failure, dynamic Agent, task update, error, waiting, and interrupt cases.

- [ ] **Step 5: Commit runtime visualization**

```powershell
git add frontend/src/features/agents frontend/src/features/tools frontend/src/features/debug
git diff --cached --check
git commit -m "feat: render complete agent runtime state"
```

### Task 3: Add Full Reporting Workspace

**Files:**
- Create: `frontend/src/features/reporting/reporting-api.ts`
- Create: `frontend/src/features/reporting/reporting-store.ts`
- Create: `frontend/src/features/reporting/ReportingWorkspace.tsx`
- Create: `frontend/src/features/reporting/RunList.tsx`
- Create: `frontend/src/features/reporting/RunProgress.tsx`
- Create: `frontend/src/features/reporting/PlanView.tsx`
- Create: `frontend/src/features/reporting/EvidenceView.tsx`
- Create: `frontend/src/features/reporting/WaitingInputForm.tsx`
- Create: `frontend/src/features/reporting/RevisionView.tsx`
- Create: `frontend/src/features/reporting/OutputArtifacts.tsx`
- Create: `frontend/src/features/reporting/reporting-store.test.ts`
- Create: `frontend/e2e/reporting.spec.ts`

**Interfaces:**
- Consumes: Reporting REST resources and `report.*`, `checkpoint.*`, dynamic-agent, evidence, revision, and output events.
- Produces: start/resume/inspect/supplement/revise/wait/continue/download/verify Reporting workflows.

- [ ] **Step 1: Write lifecycle state-machine tests**

```ts
it.each([
  ["planning", "正在规划"], ["running", "生成中"], ["waiting_user", "等待补充信息"],
  ["revising", "修订中"], ["completed", "已完成"], ["failed", "失败"],
])("renders reporting state %s", (state, label) => {
  render(<RunProgress run={reportRun({ state })} />);
  expect(screen.getByText(label)).toBeVisible();
});

it("submits waiting input once and disables duplicate submission", async () => {
  render(<WaitingInputForm request={waitingRequest("w1")} />);
  await userEvent.type(screen.getByLabelText("补充信息"), "details");
  await userEvent.click(screen.getByRole("button", { name: "继续" }));
  expect(gateway.submitReportingInput).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "继续" })).toBeDisabled();
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `npm --prefix frontend run test -- --run src/features/reporting`
Expected: FAIL because Reporting UI does not exist.

- [ ] **Step 3: Implement all persisted Reporting views and commands**

Run list supports existing, interrupted, resumable, completed, and failed runs. Workspace renders plan/modules, specialist/dynamic Agents, coverage, evidence/source ledger, decisions, waiting-input requests, supplements, checkpoints, version/revision history, validation status, and output artifacts. Commands include create, resume, cancel, provide input, request revision, retry allowed failed step, verify output, and download.

After refresh or reconnect, load the run snapshot before applying later SSE sequences. Output is not marked complete until server verification state is successful; generated-but-invalid artifacts remain visibly failed.

- [ ] **Step 4: Run Reporting unit and E2E tests**

Run:

```powershell
npm --prefix frontend run test -- --run src/features/reporting
npm --prefix frontend run e2e -- reporting.spec.ts
```

Expected: PASS for happy path, waiting input, supplement, checkpoint resume, revision, dynamic Agent, verification failure, download, disconnect, and page refresh.

- [ ] **Step 5: Commit Reporting parity**

```powershell
git add frontend/src/features/reporting frontend/e2e/reporting.spec.ts
git diff --cached --check
git commit -m "feat: add complete reporting workspace"
```

### Task 4: Add Provider, Model, Preset, Debug, and Client Settings

**Files:**
- Create: `frontend/src/features/settings/SettingsPage.tsx`
- Create: `frontend/src/features/settings/ServerConnectionForm.tsx`
- Create: `frontend/src/features/settings/ProviderSettings.tsx`
- Create: `frontend/src/features/settings/ModelSettings.tsx`
- Create: `frontend/src/features/settings/PresetSettings.tsx`
- Create: `frontend/src/features/settings/ClientPreferences.tsx`
- Create: `frontend/src/features/settings/SettingsPage.test.tsx`

**Interfaces:**
- Consumes: settings/provider/model/preset/debug endpoints.
- Produces: masked server settings management and local UI preferences.

- [ ] **Step 1: Write secret-masking and restart tests**

```ts
it("never renders or stores provider secret values", async () => {
  gateway.getSettings.mockResolvedValue({ providers: [{ id: "openai", configured: true }] });
  render(<ProviderSettings />);
  expect(screen.queryByDisplayValue(/sk-/)).toBeNull();
  expect(localStorageValue()).not.toMatch(/sk-/);
});

it("requires confirmation when provider change restarts agents", async () => {
  render(<ProviderSettings />);
  await userEvent.selectOptions(screen.getByLabelText("Provider"), "anthropic");
  expect(screen.getByRole("dialog")).toHaveTextContent("重新启动 Agent");
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `npm --prefix frontend run test -- --run src/features/settings`
Expected: FAIL because settings UI is missing.

- [ ] **Step 3: Implement server and client settings separation**

Server settings cover configured provider status, replace/remove credential commands, provider, model, preset synchronization, validation result, and per-Agent debug mode. Client settings cover theme, density, font size, preview defaults, notification preference, and server URL/token through a storage abstraction. Browser token storage is session-scoped by default; Electron storage is implemented in the next plan.

- [ ] **Step 4: Run settings tests and full unit suite**

Run:

```powershell
npm --prefix frontend run test -- --run
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: PASS with no secret values in rendered DOM, logs, snapshots, local storage, or test output.

- [ ] **Step 5: Commit settings parity**

```powershell
git add frontend/src/features/settings
git diff --cached --check
git commit -m "feat: add secure runtime settings"
```

### Task 5: Add Full Browser E2E, Refresh/Reconnect Recovery, and Gate C

**Files:**
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/fixtures/server.ts`
- Create: `frontend/e2e/conversations.spec.ts`
- Create: `frontend/e2e/agents.spec.ts`
- Create: `frontend/e2e/recovery.spec.ts`
- Create: `frontend/e2e/settings.spec.ts`
- Modify: `frontend/package.json`
- Modify: `docs/phase1/feature-parity.csv`

**Interfaces:**
- Consumes: completed browser application and deterministic fake-provider API server.
- Produces: `npm run e2e`, `npm run verify`, and browser evidence for every parity row.

- [ ] **Step 1: Write golden user journeys**

```ts
test("conversation survives refresh during streaming", async ({ page, fakeServer }) => {
  await openProjectAndSend(page, "Explain the project");
  await fakeServer.emit(agentDelta("m1", "partial"));
  await page.reload();
  await expect(page.getByText("partial")).toBeVisible();
  await fakeServer.emit(agentCompleted("m1", "complete answer"));
  await expect(page.getByText("complete answer")).toBeVisible();
});

test("evicted SSE cursor triggers snapshot resync", async ({ page, fakeServer }) => {
  await fakeServer.disconnectEvents();
  await fakeServer.evictReplayCursor();
  await fakeServer.reconnectEvents();
  await expect(page.getByRole("status")).toHaveText("已连接");
  expect(fakeServer.bootstrapRequests()).toBeGreaterThan(1);
});
```

- [ ] **Step 2: Run E2E and observe missing journey failures**

Run: `npm --prefix frontend run e2e`
Expected: any incomplete parity or recovery flow fails with trace and screenshot artifacts.

- [ ] **Step 3: Close all browser parity gaps and define verify**

`verify` runs API generation check, TypeScript, ESLint, Vitest, production build, and Playwright. Add explicit E2E coverage for project, file, editor, preview, import, download, Python run, conversations, streaming, tools, tasks, Agent switch, interrupt, rollback, settings, Reporting, 401, lease loss, 409 file conflict, refresh, reconnect, and resync.

- [ ] **Step 4: Run Gate C and record evidence**

Run:

```powershell
npm --prefix frontend run lint
npm --prefix frontend run test -- --run
npm --prefix frontend run build
npm --prefix frontend run e2e
```

Expected: PASS. Update all browser-applicable rows with exact test node evidence and `status=accepted` only where Electron evidence is not required.

- [ ] **Step 5: Commit browser Gate C**

```powershell
git add frontend docs/phase1/feature-parity.csv
git diff --cached --check
git commit -m "test: verify complete browser parity"
```
