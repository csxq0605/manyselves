# Manyselves Phase 1 Electron and Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the shared React application as a secure Electron desktop client and deploy the server/browser distribution through Linux Docker Compose.

**Architecture:** Electron loads the local React production build and exposes only typed platform operations through preload IPC. The Linux distribution uses a single-worker API container, an Nginx web/reverse-proxy container, a host-mounted authoritative data directory, health checks, and recoverable backup tooling.

**Tech Stack:** Electron, TypeScript, Vitest, Playwright Electron, Docker, Docker Compose, Gunicorn, uvicorn-worker, Nginx.

## Global Constraints

- Electron never imports or runs Python Agent code.
- `nodeIntegration=false`, `contextIsolation=true`, `sandbox=true`; no remote content receives Node or preload privileges.
- IPC channels and payloads are allowlisted and validated.
- Production API has exactly one Gunicorn worker and one Compose replica.
- Project data persists only in the server data volume; container layers are disposable.
- Provider keys and deployment token are never baked into images.

---

### Task 1: Create the Hardened Electron Shell and Preload Bridge

**Files:**
- Create: `desktop/package.json`
- Create: `desktop/package-lock.json`
- Create: `desktop/tsconfig.json`
- Create: `desktop/src/main.ts`
- Create: `desktop/src/preload.ts`
- Create: `desktop/src/ipc-contract.ts`
- Create: `desktop/src/security.ts`
- Create: `desktop/src/token-store.ts`
- Create: `desktop/src/preload.test.ts`
- Create: `frontend/src/platform/electron-platform.ts`
- Create: `frontend/src/platform/electron-platform.test.ts`

**Interfaces:**
- Consumes: locked `PlatformBridge` from the React plan.
- Produces: `window.manyselvesDesktop` and `ElectronPlatformBridge` implementing exactly that interface.

- [ ] **Step 1: Write security preference and bridge-shape tests**

```ts
it("creates every application window with hardened preferences", () => {
  const options = createWindowOptions();
  expect(options.webPreferences).toMatchObject({
    nodeIntegration: false,
    contextIsolation: true,
    sandbox: true,
  });
  expect(options.webPreferences?.webSecurity).not.toBe(false);
});

it("preload exposes only the approved bridge methods", () => {
  expect(Object.keys(buildPreloadApi()).sort()).toEqual([
    "notify", "openDownloadedFile", "saveDownload", "selectDirectory", "selectFiles", "secureToken",
  ]);
});
```

- [ ] **Step 2: Install locked Electron tooling and confirm tests fail**

Run from `desktop/`:

```powershell
npm install electron zod
npm install -D typescript vitest eslint electron-builder
npm run test -- --run
```

Expected: tests FAIL before shell implementation; lockfile records exact resolved versions.

- [ ] **Step 3: Implement hardened loading and typed IPC**

The main process loads `frontend/dist/index.html` in production and the configured localhost Vite URL only in development. Deny new-window requests and unexpected navigation; permit `https:` external links only after explicit user action. Validate every IPC input with Zod and validate sender frame URL before performing native operations. Expose no generic shell, filesystem, process, or IPC invoke method.

`secureToken.get/set/delete` uses Electron `safeStorage` to encrypt the token and writes only the encrypted blob beneath Electron `userData` with user-only permissions. When `safeStorage.isEncryptionAvailable()` is false, refuse persistent storage and keep the token in process memory rather than writing plaintext.

- [ ] **Step 4: Run unit tests and package smoke**

Run:

```powershell
npm --prefix frontend run build
npm --prefix desktop run lint
npm --prefix desktop run test -- --run
npm --prefix desktop run package
```

Expected: PASS; packaged application opens local React resources and contains no development URL.

- [ ] **Step 5: Commit the secure shell**

```powershell
git add desktop frontend/src/platform
git diff --cached --check
git commit -m "feat: add secure electron client shell"
```

### Task 2: Implement Native Import, Download, Open, Notifications, and Shortcuts

**Files:**
- Create: `desktop/src/handlers/files.ts`
- Create: `desktop/src/handlers/downloads.ts`
- Create: `desktop/src/handlers/notifications.ts`
- Create: `desktop/src/handlers/shortcuts.ts`
- Create: `desktop/src/handlers/files.test.ts`
- Create: `desktop/e2e/platform.spec.ts`
- Modify: `frontend/src/features/files/UploadQueue.tsx`
- Modify: `frontend/src/features/settings/ServerConnectionForm.tsx`

**Interfaces:**
- Consumes: preload IPC contract, `PlatformBridge`, HTTP upload/download gateway.
- Produces: explicit local selection/import, bounded upload streams, save/open, notifications, desktop shortcuts, secure server credentials.

- [ ] **Step 1: Write traversal, cancellation, and user-gesture tests**

```ts
it("directory import returns relative entries and excludes symbolic links", async () => {
  const result = await enumerateImportDirectory(fixtureDirectory());
  expect(result.map((entry) => entry.relativePath)).toEqual(["a.txt", "nested/b.csv"]);
  expect(result.some((entry) => entry.kind === "symlink")).toBe(false);
});

it("open downloaded file rejects paths not issued by a completed download", async () => {
  await expect(openDownloadedFile("C:/Windows/System32/cmd.exe", emptyDownloadRegistry()))
    .rejects.toMatchObject({ code: "UNTRUSTED_DOWNLOAD_PATH" });
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `npm --prefix desktop run test -- --run`
Expected: FAIL because handlers are missing.

- [ ] **Step 3: Implement native-equivalent behavior**

Directory enumeration requires a user dialog, returns only regular files with normalized `/` relative paths, skips symlinks, and displays skipped/failed entries before upload. Downloads write only after a save dialog, use a partial file during transfer, atomically rename on success, and delete the partial file on cancel/failure. Opening a file is allowed only for a path recorded from a successful download in the current app profile.

Shortcuts cover open project switcher, quick file search, save, close tab, send message, and interrupt; they dispatch React actions rather than bypassing API/lease checks. Notifications contain project/run labels but never message bodies, tool arguments, secrets, or local paths.

- [ ] **Step 4: Run desktop E2E**

Run:

```powershell
npm --prefix desktop run test -- --run
npm --prefix desktop run e2e -- platform.spec.ts
```

Expected: PASS for file selection cancel, directory import, upload failure, download cancel, safe open, token persistence fallback, shortcuts, and notification redaction.

- [ ] **Step 5: Commit native client behavior**

```powershell
git add desktop frontend/src/features/files/UploadQueue.tsx frontend/src/features/settings/ServerConnectionForm.tsx
git diff --cached --check
git commit -m "feat: add electron native workspace bridge"
```

### Task 3: Build the Single-Worker API Container

**Files:**
- Create: `deploy/api/Dockerfile`
- Create: `deploy/api/entrypoint.sh`
- Create: `deploy/api/healthcheck.py`
- Create: `.dockerignore`
- Create: `tests/deploy/test_api_image_contract.py`

**Interfaces:**
- Consumes: packaged Python application and `manyselves.webapi.main:create_app`.
- Produces: non-root API image listening on port 8000 with one worker and health endpoint.

- [ ] **Step 1: Write image-contract tests**

```python
def test_entrypoint_enforces_one_worker():
    script = Path("deploy/api/entrypoint.sh").read_text("utf-8")
    assert "--workers 1" in script
    assert "uvicorn_worker.UvicornWorker" in script


def test_dockerfile_uses_non_root_user():
    dockerfile = Path("deploy/api/Dockerfile").read_text("utf-8")
    assert "USER manyselves" in dockerfile
    assert "HEALTHCHECK" in dockerfile
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/deploy/test_api_image_contract.py -q`
Expected: FAIL because deploy assets do not exist.

- [ ] **Step 3: Implement reproducible API image**

Use a Python 3.12 slim builder/runtime multi-stage image, install from `uv.lock`, copy only required package/assets, create an unprivileged `manyselves` user, and declare `/data/manyselves` as the data root. Entrypoint uses:

```sh
exec gunicorn manyselves.webapi.main:app \
  --workers 1 --worker-class uvicorn_worker.UvicornWorker \
  --bind 0.0.0.0:8000 --access-logfile - --error-logfile -
```

Do not permit a worker-count environment override in Phase 1.

- [ ] **Step 4: Build and smoke the image**

Run:

```powershell
uv run pytest tests/deploy/test_api_image_contract.py -q
docker build -f deploy/api/Dockerfile -t manyselves-api:phase1 .
docker run --rm manyselves-api:phase1 --help
```

Expected: tests and build PASS; container command resolves Gunicorn and does not run as root.

- [ ] **Step 5: Commit the API image**

```powershell
git add deploy/api .dockerignore tests/deploy/test_api_image_contract.py
git diff --cached --check
git commit -m "build: package single runtime api container"
```

### Task 4: Build Nginx Web Image and Compose Topology

**Files:**
- Create: `deploy/web/Dockerfile`
- Create: `deploy/nginx/default.conf`
- Create: `deploy/compose.yaml`
- Create: `deploy/env.example`
- Create: `tests/deploy/test_compose_contract.py`

**Interfaces:**
- Consumes: `frontend/dist`, API image.
- Produces: browser static server, TLS-ready reverse proxy, SSE-safe proxy, persistent data volume topology.

- [ ] **Step 1: Write topology and SSE proxy tests**

```python
def test_compose_has_one_api_replica_and_persistent_data():
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text("utf-8"))
    api = compose["services"]["api"]
    assert api.get("deploy", {}).get("replicas", 1) == 1
    assert any("/data/manyselves" in volume for volume in api["volumes"])


def test_nginx_disables_sse_buffering():
    config = Path("deploy/nginx/default.conf").read_text("utf-8")
    assert "proxy_buffering off" in config
    assert "proxy_read_timeout 1h" in config
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/deploy/test_compose_contract.py -q`
Expected: FAIL because topology is missing.

- [ ] **Step 3: Implement web/proxy and Compose configuration**

Nginx serves immutable hashed assets with long cache, `index.html` with no-cache, proxies `/api/` to `api:8000`, disables buffering/compression for `/api/v1/events`, forwards request ID and scheme, and applies upload size/time limits matching FastAPI settings. Compose mounts `${MANYSELVES_DATA_DIR}:/data/manyselves`, passes secrets from an ignored `.env` or Docker secrets, adds health-conditioned startup, `stop_grace_period: 60s`, read-only root filesystems where compatible, and restart policies.

- [ ] **Step 4: Validate and smoke Compose**

Run:

```powershell
npm --prefix frontend run build
uv run pytest tests/deploy -q
docker compose -f deploy/compose.yaml --env-file deploy/env.example config
docker compose -f deploy/compose.yaml --env-file deploy/env.example build
```

Expected: PASS; rendered Compose contains one API service, one web service, no database/cache/vector/object-store services, and no literal real secrets.

- [ ] **Step 5: Commit server distribution**

```powershell
git add deploy/web deploy/nginx deploy/compose.yaml deploy/env.example tests/deploy
git diff --cached --check
git commit -m "build: add linux compose deployment"
```

### Task 5: Add Backup, Restore, Operations, Packaging, and Gate D

**Files:**
- Create: `deploy/backup/backup.ps1`
- Create: `deploy/backup/backup.sh`
- Create: `deploy/backup/restore.ps1`
- Create: `deploy/backup/restore.sh`
- Create: `docs/deployment/linux-compose.md`
- Create: `docs/deployment/electron-client.md`
- Create: `tests/deploy/test_backup_restore.py`
- Modify: `desktop/package.json`
- Modify: `docs/phase1/feature-parity.csv`

**Interfaces:**
- Consumes: data-root layout, Compose service names, Electron package scripts.
- Produces: verified data snapshots, guarded restore, operator runbook, signed-build handoff points, Gate D evidence.

- [ ] **Step 1: Write round-trip and refusal tests**

```python
def test_backup_restore_round_trip(tmp_path):
    source = create_fixture_data_root(tmp_path / "source")
    archive = run_backup(source, tmp_path / "backup")
    restored = run_restore(archive, tmp_path / "restored")
    assert directory_hash(restored) == directory_hash(source)


def test_restore_refuses_nonempty_target_without_explicit_force(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    result = invoke_restore(fixture_archive(tmp_path), target)
    assert result.returncode != 0
    assert (target / "keep.txt").read_text("utf-8") == "keep"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/deploy/test_backup_restore.py -q`
Expected: FAIL because scripts are missing.

- [ ] **Step 3: Implement guarded operations and documentation**

Backup requests the API maintenance/checkpoint endpoint, waits for quiescence, records manifest hashes, archives the configured data root, then releases maintenance mode. Restore requires stopped services, verifies manifest hashes, resolves the absolute target under the configured data parent, refuses a non-empty target unless explicit `--force` is supplied, and preserves the previous target as a timestamped sibling before replacement.

Document install, environment values, TLS termination, startup, health, logs, upgrade, rollback, backup, restore, Electron server configuration, trusted-network limitation, one-worker rule, and one-stack-per-concurrent-scenario guidance.

- [ ] **Step 4: Run Gate D**

Run:

```powershell
npm --prefix desktop run lint
npm --prefix desktop run test -- --run
npm --prefix desktop run package
uv run pytest tests/deploy -q
docker compose -f deploy/compose.yaml --env-file deploy/env.example config
```

Expected: PASS. Record Electron/deployment evidence in the parity matrix.

- [ ] **Step 5: Commit operations and distribution evidence**

```powershell
git add deploy/backup docs/deployment desktop/package.json tests/deploy docs/phase1/feature-parity.csv
git diff --cached --check
git commit -m "docs: add phase one operations runbook"
```
