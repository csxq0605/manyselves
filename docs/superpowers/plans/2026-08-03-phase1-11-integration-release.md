# Phase 1 Light Web Integration and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the approved authentication, project UI, global knowledge, and model settings changes into a regression-tested Podman-deployable Phase 1 release.

**Architecture:** This plan adds no new product subsystem. It closes API generation, end-to-end flows, compatibility, security, image build, Podman smoke, documentation, and release evidence after Plans 07-10 have each passed independently.

**Tech Stack:** pytest, Ruff, Vitest, ESLint, TypeScript, Vite, Playwright, OpenAPI, Docker/Podman images, Nginx, FastAPI/Gunicorn/Uvicorn.

## Global Constraints

- Execute only after Plans 07, 08, 09, and 10 are complete and individually reviewed.
- Preserve API 9000, Nginx 9090, IP access at `192.168.8.28:9090`, and Podman Compose support.
- Do not add Redis, MySQL, Milvus, MinIO, LLM Wiki, multiple workers, RBAC, or a second Runtime.
- Existing untracked image/source archives must not be staged, deleted, or overwritten.
- A completion claim requires fresh full-suite, E2E, image-build, and deployment evidence.

---

### Task 1: API contract and frontend integration closure

**Files:**
- Modify: `scripts/export_openapi.py`
- Modify: `frontend-contract/openapi.json`
- Modify: `frontend/src/api/generated/schema.ts`
- Modify: `frontend/src/app/App.tsx`
- Modify: `frontend/src/app/routes.tsx`
- Modify: `frontend/src/app/providers.tsx`
- Modify: `frontend/src/app/client-config.ts`
- Modify: `frontend/src/app/client-config.test.ts`
- Modify: `tests/webapi/test_openapi_contract.py`

**Interfaces:**
- Consumes: all APIs and routes from Plans 07-10.
- Produces: one generated OpenAPI document and one matching TypeScript schema with no handwritten duplicate DTOs.
- Produces: application boot order `AuthGate -> Bootstrap/SSE -> Router pages`.

- [x] **Step 1: Add contract assertions for required and forbidden semantics**

```python
def test_phase1_redesign_contract(app_schema: dict) -> None:
    paths = app_schema["paths"]
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/auth/session" in paths
    assert "/api/v1/global-knowledge/files" in paths
    assert "SessionCookie" in app_schema["components"]["securitySchemes"]
    assert "DeploymentBearer" not in app_schema["components"]["securitySchemes"]
```

- [x] **Step 2: Export and regenerate contracts**

Run: `uv run python scripts/export_openapi.py`

Run: `npm run generate:api`

Working directory for npm command: `frontend`

- [x] **Step 3: Run API-generation and application boot tests**

Run: `uv run pytest tests/webapi/test_openapi_contract.py -q`

Run: `npm run check:api`

Run: `npm test -- --run src/app src/api`

Working directory for npm commands: `frontend`

Expected: PASS; unauthenticated application tests must not bootstrap Runtime or start SSE.

- [x] **Step 4: Commit contract integration**

```bash
git add scripts/export_openapi.py frontend-contract/openapi.json frontend/src/api/generated/schema.ts frontend/src/app tests/webapi/test_openapi_contract.py
git commit -m "chore: integrate light web API contracts"
```

### Task 2: Complete browser end-to-end user journeys

**Files:**
- Create: `frontend/e2e/auth.spec.ts`
- Create: `frontend/e2e/project-navigation.spec.ts`
- Create: `frontend/e2e/global-knowledge.spec.ts`
- Modify: `frontend/e2e/fixtures/server.ts`
- Modify: `frontend/e2e/conversations.spec.ts`
- Modify: `frontend/e2e/workspace.spec.ts`
- Modify: `frontend/e2e/reporting.spec.ts`
- Modify: `frontend/e2e/settings.spec.ts`
- Modify: `frontend/e2e/failure-recovery.spec.ts`
- Modify: `frontend/e2e/recovery.spec.ts`

**Interfaces:**
- Consumes: production React routes and mocked FastAPI contracts.
- Produces: E2E coverage for login, navigation, browser upload, project conversation, Runtime, outputs, global knowledge, settings, and failure recovery.

- [x] **Step 1: Add the login-to-output golden journey**

```ts
test("login, upload local input, run, and download output", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("账号").fill("admin");
  await page.getByLabel("密码").fill("yuanxi@2026");
  await page.getByRole("button", { name: "登录" }).click();
  await page.getByRole("link", { name: "能源管理" }).click();
  await page.getByRole("button", { name: "新对话" }).click();
  await page.getByLabel("选择本地文件").setInputFiles("e2e/fixtures/input.md");
  await page.getByLabel("给 Agent 发送消息").fill("生成报告");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByRole("link", { name: "输出" })).toBeVisible();
});
```

The fixture must verify multipart bytes came from the Playwright client, the logical destination is `Inputs/input.md`, no local absolute path appears in the request body/UI, and final output is surfaced only after a completed event.

- [x] **Step 2: Add navigation and permission-matrix journeys**

Assert all six fixed entries, separate Templates/Outputs routes, no folder mutation menu, no output upload/edit, no server-file picker, and no `.manyselves` text. Add global knowledge project-priority and environment-managed model-key states.

- [x] **Step 3: Run the complete E2E suite**

Run: `npm run e2e`

Working directory: `frontend`

Expected: every Playwright test PASS with no console errors, blank page, or uncaught request rejection.

- [x] **Step 4: Commit end-to-end coverage**

```bash
git add frontend/e2e
git commit -m "test: cover light web user journeys"
```

### Task 3: Compatibility, security, and full regression gates

**Files:**
- Modify: `tests/release/test_feature_matrix.py`
- Modify: `tests/release/test_security_boundaries.py`
- Modify: `tests/release/test_concurrent_clients.py`
- Modify: `tests/release/test_failure_recovery.py`
- Create: `tests/release/test_phase1_light_web_compatibility.py`
- Modify: `tests/release/fixtures/workspace_v1/README.md`

**Interfaces:**
- Verifies: old workspace compatibility, one Runtime, shared-session clients, path and secret safety, rollback, and exact feature matrix.

- [x] **Step 1: Add compatibility and scope-gate tests**

```python
def test_workspace_v1_opens_without_directory_migration(workspace_v1: Path, app) -> None:
    before = sorted(path.relative_to(workspace_v1) for path in workspace_v1.rglob("*"))
    app.open_project(workspace_v1.name)
    after = sorted(path.relative_to(workspace_v1) for path in workspace_v1.rglob("*"))
    assert set(before).issubset(after)
    assert not (workspace_v1.parent / "projects").exists()

def test_hidden_internal_paths_never_enter_public_file_tree(client) -> None:
    payload = client.get("/api/v1/projects/default/files/tree").json()
    assert all(".manyselves" not in item["path"] for item in payload["entries"])
```

Add assertions that two clients cannot cause two Runtime instances or concurrent different-project runs, and that session/API/model/global-knowledge secrets never appear in errors, logs, SSE, OpenAPI examples, or file lists.

- [ ] **Step 2: Run Python lint and full Python tests**

Run: `uv run ruff check manyselves tests scripts deploy`

Run: `uv run pytest -q`

Expected: Ruff PASS and the complete Python suite PASS. Existing intentional skips must be reviewed; no new unexpected skip or xfail is accepted.

- [ ] **Step 3: Run the complete frontend verification pipeline**

Run: `npm run verify`

Working directory: `frontend`

Expected: API check, ESLint, all Vitest tests, TypeScript/Vite build, and all Playwright tests PASS.

- [x] **Step 4: Review changes against the approved spec**

Run: `git diff --check`

Run: `rg -n "deploymentToken|Access Token|引用服务器文件|artifact-gateway\.key|RUNTIME CONTROL PLANE" frontend/src deploy docs/deployment`

Expected: `git diff --check` returns no output. The search returns no active UI/deployment references; any historical design text is evaluated separately and not changed mechanically.

- [x] **Step 5: Commit compatibility and security gates**

```bash
git add tests/release
git commit -m "test: lock light web compatibility and security"
```

### Task 4: Build images and validate Podman Compose deployment

**Files:**
- Modify: `deploy/api/Dockerfile`
- Modify: `deploy/web/Dockerfile`
- Modify: `deploy/compose.yaml`
- Modify: `deploy/nginx/default.conf`
- Modify: `deploy/api/healthcheck.py`
- Modify: `deploy/api/init_config.py`
- Create: `deploy/smoke.env.example`
- Modify: `docs/deployment/linux-compose.md`
- Modify: `scripts/verify_deployment.py`
- Modify: `tests/release/test_deployment_verifier.py`

**Interfaces:**
- Produces: `localhost/manyselves-api:phase1` and `localhost/manyselves-web:phase1`.
- Produces: Web at host port 9090 and internal API at 9000.
- Produces: login-authenticated deployment verification.

- [ ] **Step 1: Build clean frontend and API images**

Run: `podman build -f deploy/api/Dockerfile -t localhost/manyselves-api:phase1 .`

Run: `podman build -f deploy/web/Dockerfile -t localhost/manyselves-web:phase1 .`

Expected: both builds finish successfully and the Web image contains the newly generated Vite bundle.

- [x] **Step 2: Render and validate Compose configuration**

Run: `podman-compose -f deploy/compose.yaml --env-file deploy/smoke.env.example config`

Expected: smoke Web publishes `19090:9090`, API listens on 9000 inside the network, one API container/worker is configured, and no Access Token/Redis/MySQL/Milvus/MinIO service exists. The committed smoke env uses `MANYSELVES_HTTP_BIND=127.0.0.1`, `MANYSELVES_HTTP_PORT=19090`, `MANYSELVES_DATA_DIR=./.smoke-data`, and allowed origin `http://127.0.0.1:19090`.

- [ ] **Step 3: Start an isolated smoke stack and inspect health**

Run: `podman-compose -p manyselves-phase1-smoke -f deploy/compose.yaml --env-file deploy/smoke.env.example up -d --no-build`

Run: `podman ps --filter name=manyselves-phase1-smoke`

Expected: API is healthy and Web is running on loopback port 19090 without replacing the standard stack.

- [x] **Step 4: Run the deployment verifier through Nginx**

Run: `uv run python scripts/verify_deployment.py --base-url http://127.0.0.1:19090 --username admin --password yuanxi@2026`

Expected: login, session, health, bootstrap, upload, conversation, SSE/output check, and logout PASS without an Authorization header.

- [ ] **Step 5: Stop only the isolated smoke stack**

Run: `podman-compose -p manyselves-phase1-smoke -f deploy/compose.yaml --env-file deploy/smoke.env.example down`

Expected: only containers/networks with project name `manyselves-phase1-smoke` are removed; persistent user data and unrelated containers remain untouched.

- [x] **Step 6: Commit deployable image and verification changes**

```bash
git add deploy docs/deployment/linux-compose.md scripts/verify_deployment.py tests/release/test_deployment_verifier.py
git commit -m "release: validate phase1 light web deployment"
```

### Task 5: Final evidence, documentation, and handoff

**Files:**
- Modify: `README_zh.md`
- Modify: `README.md`
- Modify: `docs/deployment/linux-compose.md`
- Create: `docs/release/2026-08-03-phase1-light-web-verification.md`

**Interfaces:**
- Produces: one evidence document containing exact commit, commands, results, image IDs, bundle name, and known Phase 1 limits.

- [ ] **Step 1: Record fresh verification evidence**

The evidence file must contain the exact output summaries for:

```text
uv run ruff check manyselves tests scripts deploy
uv run pytest -q
npm run verify
podman image inspect localhost/manyselves-api:phase1
podman image inspect localhost/manyselves-web:phase1
scripts/verify_deployment.py through http://127.0.0.1:9090
```

Record counts and immutable image IDs; do not copy results from an earlier cycle.

- [ ] **Step 2: Update operator and user documentation**

Document: URL/login homepage, default credentials, local-browser upload meaning, six project entries, Templates/Outputs distinction, global/project knowledge priority, simple model settings/YAML ownership, backup scope, HTTP LAN warning, and the one-Runtime/no-concurrent-project limitation.

- [ ] **Step 3: Run final status and diff checks**

Run: `git diff --check`

Run: `git status --short`

Expected: no whitespace errors; only intended tracked changes and the user's pre-existing untracked archives remain.

- [ ] **Step 4: Commit final documentation and evidence**

```bash
git add README.md README_zh.md docs/deployment/linux-compose.md docs/release/2026-08-03-phase1-light-web-verification.md
git commit -m "docs: hand off phase1 light web release"
```

- [ ] **Step 5: Perform the completion gate**

Before claiming completion, invoke `superpowers:verification-before-completion`, rerun any stale critical command, compare every approved spec section to an implemented task, and report remaining Phase 1 limitations explicitly. Do not merge, push, retag, or replace a running server deployment unless the user separately requests that external state change.
