# Phase 1 Simple Model Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fragmented provider control plane with one simple model form while retaining `manyselves.config.yaml`, environment overrides, transactional persistence, and advanced provider capabilities.

**Architecture:** Existing `ConfigManager` and `SettingsService` remain authoritative. The API adds explicit credential-source metadata and a connection-test command; React shows one active-provider form and places all low-frequency provider management under a collapsed advanced section.

**Tech Stack:** Pydantic Settings, YAML, existing ConfigManager/SettingsService, FastAPI, React 19, TanStack Query, Vitest.

## Global Constraints

- Main form contains Provider, optional API URL, API Key, default model, Test connection, and Save.
- API keys are masked and never returned.
- Environment-provided API keys are read-only in the UI and have higher precedence than YAML.
- Web edits and manual YAML edits use one configuration model; do not add a settings database or raw YAML editor.
- Remove Access Token/server-connection configuration from settings.
- Keep provider presets and low-frequency fields only under collapsed Advanced settings.

---

### Task 1: Credential-source metadata and environment-safe persistence

**Files:**
- Modify: `manyselves/config/schema.py`
- Modify: `manyselves/config/manager.py`
- Modify: `manyselves/application/settings_service.py`
- Modify: `manyselves/webapi/schemas/settings.py`
- Modify: `manyselves/webapi/routes/settings.py`
- Create: `tests/config/test_manager.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`
- Modify: `tests/webapi/test_openapi_contract.py`

**Interfaces:**
- Produces: `CredentialSource = Literal["none", "yaml", "environment"]`.
- Produces: provider response fields `configured: bool` and `credentialSource: CredentialSource`.
- Produces: environment-owned API key updates rejected with `CREDENTIAL_MANAGED_BY_ENVIRONMENT`.

- [x] **Step 1: Write failing precedence and non-persistence tests**

```python
def test_environment_key_is_reported_but_not_written_to_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret")
    manager = ConfigManager(Settings(config_path=tmp_path / "manyselves.config.yaml"))
    provider = manager.get_provider_state("openai")
    assert provider.credential_source == "environment"
    manager.save_config()
    assert "env-secret" not in (tmp_path / "manyselves.config.yaml").read_text()

@pytest.mark.asyncio
async def test_api_rejects_replacing_environment_owned_key(authed_client):
    response = await authed_client.patch(
        "/api/v1/settings/providers/openai",
        json={"apiKey": "replacement"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CREDENTIAL_MANAGED_BY_ENVIRONMENT"
```

- [x] **Step 2: Run focused tests and verify failure**

Run: `uv run pytest tests/config/test_manager.py tests/webapi/test_conversations_agents_reporting.py -q -k "credential or environment"`

Expected: FAIL because source ownership is not preserved explicitly and environment secrets can enter the in-memory object saved to YAML.

- [x] **Step 3: Separate effective secrets from persisted YAML values**

Track provider credential source during settings load. `ConfigManager.save_config()` must serialize the configured YAML value, never the environment-injected effective value. Settings responses return only `configured` and source. `SettingsService` keeps the existing capture/mutate/validate/save/restart/rollback transaction and returns a stable conflict if an environment-owned key is patched or cleared.

- [x] **Step 4: Run settings and contract tests**

Run: `uv run pytest tests/config/test_manager.py tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py -q -k "settings or provider or credential or openapi"`

Expected: PASS with no API key in JSON, repr, OpenAPI examples, or YAML when sourced from environment.

- [x] **Step 5: Commit credential source handling**

```bash
git add manyselves/config/schema.py manyselves/config/manager.py manyselves/application/settings_service.py manyselves/webapi/schemas/settings.py manyselves/webapi/routes/settings.py tests/config/test_manager.py tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py frontend-contract/openapi.json
git commit -m "fix: preserve model credential ownership"
```

### Task 2: Connection test API with no persistence side effect

**Files:**
- Modify: `manyselves/application/settings_service.py`
- Modify: `manyselves/webapi/schemas/settings.py`
- Modify: `manyselves/webapi/routes/settings.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`

**Interfaces:**
- Produces: `POST /api/v1/settings/providers/{provider_id}/test`.
- Produces: `ProviderConnectionTestResponse{ok, providerId, model, message}` with secret-safe messages.
- Does not save config, replace Runtime, or require a control lease.

- [x] **Step 1: Write failing success and secret-safe failure tests**

```python
@pytest.mark.asyncio
async def test_provider_connection_test_does_not_persist_or_restart(authed_client, manager, host):
    before = manager.config_path.read_bytes()
    response = await authed_client.post("/api/v1/settings/providers/openai/test")
    assert response.json()["ok"] is True
    assert manager.config_path.read_bytes() == before
    assert host.replace_count == 0

@pytest.mark.asyncio
async def test_provider_test_failure_never_returns_key(authed_client):
    response = await authed_client.post("/api/v1/settings/providers/broken/test")
    assert "provider-secret" not in response.text
```

- [x] **Step 2: Run the focused tests and verify the missing route**

Run: `uv run pytest tests/webapi/test_conversations_agents_reporting.py -q -k "provider_connection_test or provider_test"`

Expected: FAIL with 404.

- [x] **Step 3: Implement a bounded provider probe**

Resolve the existing effective provider config, create the provider through the same registry/factory used by Runtime, execute the cheapest supported model-list or minimal completion probe with the existing timeout policy, and map exceptions to a generic safe message. Do not write YAML and do not replace the live LoopManager.

- [x] **Step 4: Run provider settings tests**

Run: `uv run pytest tests/webapi/test_conversations_agents_reporting.py -q -k "provider or settings"`

Expected: PASS.

- [x] **Step 5: Commit the connection test endpoint**

```bash
git add manyselves/application/settings_service.py manyselves/webapi/schemas/settings.py manyselves/webapi/routes/settings.py tests/webapi/test_conversations_agents_reporting.py frontend-contract/openapi.json
git commit -m "feat: add model provider connection test"
```

### Task 3: Simplified React model settings page

**Files:**
- Rewrite: `frontend/src/features/settings/SettingsPage.tsx`
- Rewrite: `frontend/src/features/settings/settings.css`
- Modify: `frontend/src/features/settings/settings-api.ts`
- Modify: `frontend/src/features/settings/settings-api.test.ts`
- Modify: `frontend/src/features/settings/SettingsPage.test.tsx`
- Modify: `frontend/src/features/settings/ModelSettings.tsx`
- Modify: `frontend/src/features/settings/ProviderSettings.tsx`
- Modify: `frontend/src/features/settings/PresetSettings.tsx`
- Modify: `frontend/src/features/settings/ClientPreferences.tsx`
- Modify: `frontend/src/app/routes.tsx`
- Modify: `frontend/e2e/settings.spec.ts`

**Interfaces:**
- Consumes: credential-source and connection-test APIs from Tasks 1-2.
- Produces: `/settings/models` main form and a native collapsed `<details>` advanced section.
- Produces: no ServerConnection form, runtime control-plane heading, or dark-only settings theme.

- [x] **Step 1: Write failing simple-form tests**

```tsx
it("renders only the approved primary model fields", async () => {
  renderSettings();
  expect(await screen.findByLabelText("模型提供商")).toBeVisible();
  expect(screen.getByLabelText("API 地址（可选）")).toBeVisible();
  expect(screen.getByLabelText("API Key")).toBeVisible();
  expect(screen.getByLabelText("默认模型")).toBeVisible();
  expect(screen.getByRole("button", { name: "测试连接" })).toBeVisible();
  expect(screen.getByRole("button", { name: "保存" })).toBeVisible();
  expect(screen.queryByText("RUNTIME CONTROL PLANE")).not.toBeInTheDocument();
});

it("locks an environment-managed key without displaying it", async () => {
  renderSettings({ credentialSource: "environment", configured: true });
  expect(await screen.findByLabelText("API Key")).toBeDisabled();
  expect(screen.getByText("由服务器环境管理")).toBeVisible();
  expect(screen.queryByDisplayValue(/secret/i)).not.toBeInTheDocument();
});
```

- [x] **Step 2: Run focused tests and verify current settings fail**

Run: `npm test -- --run src/features/settings`

Working directory: `frontend`

Expected: FAIL because provider/preset/connection panels are all top-level and credential source is absent.

- [x] **Step 3: Implement the simple form and advanced disclosure**

Use one selected active provider. Save API key only when the user typed a new non-empty value; never hydrate a secret. Connection test displays inline success/error and does not trigger a Runtime restart. Saving model/base URL/key continues through current settings mutations and existing restart confirmation only when backend reports restart-required. Place provider creation/removal, presets, client density, timeouts, and other low-frequency controls under one closed-by-default `<details>` section.

- [x] **Step 4: Regenerate schema and run settings tests/E2E**

Run: `npm run generate:api`

Run: `npm run check:api`

Run: `npm test -- --run src/features/settings`

Run: `npm run e2e -- settings.spec.ts`

Run: `npm run build`

Working directory: `frontend`

Expected: PASS.

- [x] **Step 5: Commit simplified settings**

```bash
git add frontend/src/features/settings frontend/src/app/routes.tsx frontend/src/api/generated/schema.ts frontend/e2e/settings.spec.ts
git commit -m "feat: simplify model settings experience"
```

### Task 4: Configuration documentation and restart behavior verification

**Files:**
- Modify: `deploy/env.example`
- Modify: `docs/deployment/linux-compose.md`
- Modify: `README_zh.md`
- Modify: `tests/release/test_failure_recovery.py`

**Interfaces:**
- Documents: YAML-editable fields, environment-owned credential behavior, and restart requirement.
- Verifies: failed persistence leaves old config/live Runtime coherent or Runtime explicitly unavailable under existing consistency rules.

- [x] **Step 1: Add a release characterization for environment-owned credentials**

```python
def test_environment_provider_key_survives_ui_model_update_without_yaml_leak(runtime):
    runtime.update_default_model("gpt-5")
    assert runtime.active_provider.api_key == "env-secret"
    assert "env-secret" not in runtime.config_path.read_text()
```

- [x] **Step 2: Run release failure tests**

Run: `uv run pytest tests/release/test_failure_recovery.py -q -k "provider or config or environment"`

Expected: PASS after Tasks 1-3; if it fails, fix the settings transaction rather than weakening the assertion.

- [x] **Step 3: Document the two supported administration paths**

Document the simple UI path and direct `manyselves.config.yaml` path. State that environment keys override YAML, appear read-only in UI, and require an API container restart when changed. Do not instruct users to edit raw YAML in the browser.

- [x] **Step 4: Run documentation/deployment checks**

Run: `uv run pytest tests/release/test_failure_recovery.py tests/release/test_deployment_verifier.py -q`

Expected: PASS.

- [x] **Step 5: Commit settings documentation**

```bash
git add deploy/env.example docs/deployment/linux-compose.md README_zh.md tests/release/test_failure_recovery.py
git commit -m "docs: explain model configuration ownership"
```
