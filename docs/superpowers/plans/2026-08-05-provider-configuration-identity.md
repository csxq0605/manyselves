# Provider Configuration Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ambiguous provider-preset saving with uniquely identified, protocol-explicit, atomically activated provider configurations.

**Architecture:** Provider preset identity is distinct from protocol (`anthropic` or `openai`) and from a saved configuration ID. The backend owns atomic validation, persistence, active-provider selection, defaults synchronization, and runtime replacement. React edits and tests one complete connection form using generated OpenAPI types; presets only populate form defaults.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, existing `SettingsService` transactions, YAML configuration, React 19, TypeScript, Vitest, `openapi-typescript`.

## Global Constraints

- Do not use protocol names as preset IDs or saved configuration IDs.
- The save form contains configuration name, protocol, API base URL, API key, default model, active selection, enabled state, and optional JSON headers.
- API keys are write-only: never persist an environment-provided secret, include a secret in a response, or log a secret.
- An empty API key updates an existing YAML-owned configuration without replacing its secret; it is invalid for a new configuration.
- Saving with `makeActive: true` atomically updates `providers.active`, `agents.defaults.provider`, and `agents.defaults.model` before replacing the runtime.
- Connection testing is ephemeral: it neither persists configuration nor changes active state nor restarts the runtime.
- Existing configurations are not automatically deleted or guessed into presets.
- Preserve unrelated working-tree changes. Do not create commits unless the user explicitly asks.

---

### Task 1: Give presets and configurations independent identities

**Files:**
- Modify: `manyselves/config/schema.py`
- Modify: `manyselves/config/presets.py`
- Modify: `manyselves/webapi/schemas/settings.py`
- Modify: `manyselves/webapi/routes/settings.py`
- Modify: `tests/test_presets.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`
- Modify: `tests/webapi/test_openapi_contract.py`

**Interfaces:**
- Produces `ProviderPreset.id: str`, a stable ID such as `xiaomi-mimo-token-plan-cn`.
- Produces `ApiConfig.preset_id: str | None`, serialized as `presetId` in masked provider settings.
- Keeps `ApiConfig.provider` as the runtime protocol, never as a UI preset identity.

- [ ] **Step 1: Write failing unique-identity tests**

```python
def test_anthropic_compatible_presets_have_distinct_ids() -> None:
    presets = load_presets()
    compatible = [preset for preset in presets if preset.provider == "anthropic"]

    assert len(compatible) >= 2
    assert len({preset.id for preset in compatible}) == len(compatible)


@pytest.mark.asyncio
async def test_settings_returns_preset_id_without_returning_secret(resources) -> None:
    client, host, _, secret = resources
    host.config_manager.config.providers.configurations[0].preset_id = "xiaomi-mimo-token-plan-cn"

    response = await client.get("/api/v1/settings")

    assert response.json()["providers"][0]["presetId"] == "xiaomi-mimo-token-plan-cn"
    assert secret not in response.text
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_presets.py tests/webapi/test_conversations_agents_reporting.py -q -k "preset_id or preset_id_without or compatible_presets"`

Expected: FAIL because presets expose only `provider` and `ApiConfig` has no `preset_id` field.

- [ ] **Step 3: Add stable IDs without changing protocol behavior**

Add `id` to `ProviderPreset` and `preset_id` to `ApiConfig`. Built-in presets declare explicit IDs. The cc-switch parser derives a deterministic slug from the source preset name, with a protocol prefix only when needed to avoid collisions. The settings response includes nullable `presetId`; existing YAML remains valid because the field defaults to `None`.

- [ ] **Step 4: Run focused backend and contract tests**

Run: `uv run pytest tests/test_presets.py tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py -q -k "preset or settings"`

Expected: PASS, including the assertion that secrets remain absent.

- [ ] **Step 5: Review the task diff**

Run: `git diff --check -- manyselves/config/schema.py manyselves/config/presets.py manyselves/webapi/schemas/settings.py manyselves/webapi/routes/settings.py tests/test_presets.py tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py`

Expected: no whitespace errors. Do not commit unless the user explicitly requests it.

### Task 2: Add atomic full-configuration save and ephemeral testing APIs

**Files:**
- Modify: `manyselves/application/settings_service.py`
- Modify: `manyselves/webapi/schemas/settings.py`
- Modify: `manyselves/webapi/routes/settings.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`
- Modify: `tests/webapi/test_openapi_contract.py`

**Interfaces:**
- Produces `PUT /api/v1/settings/provider-configurations/{provider_config_id}`.
- Produces `POST /api/v1/settings/provider-configurations/test`.
- Consumes a complete `ProviderConfigurationUpsert` body with `presetId`, `name`, `protocol`, `apiBase`, optional `apiKey`, `defaultModel`, `extraHeaders`, `enabled`, and `makeActive`.
- Produces a masked `SettingsResponse`; ephemeral tests return `ProviderConnectionTestResponse` without a persistent provider ID.

- [ ] **Step 1: Write failing transaction and no-side-effect tests**

```python
@pytest.mark.asyncio
async def test_full_provider_save_activates_and_synchronizes_defaults(resources) -> None:
    client, host, _, _ = resources

    response = await client.put(
        "/api/v1/settings/provider-configurations/mimo-cn",
        json={
            "presetId": "xiaomi-mimo-token-plan-cn",
            "name": "Xiaomi MiMo Token Plan (China)",
            "protocol": "anthropic",
            "apiBase": "https://token-plan-cn.xiaomimimo.com/anthropic",
            "apiKey": "test-secret",
            "defaultModel": "mimo-v2.5-pro",
            "extraHeaders": {},
            "enabled": True,
            "makeActive": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["providers"][-1]["active"] is True
    assert response.json()["defaults"] == {
        "provider": "anthropic", "model": "mimo-v2.5-pro",
        "maxTokens": 8192, "temperature": 0.1,
    }
    assert host.config_manager.config.providers.active == "mimo-cn"


@pytest.mark.asyncio
async def test_ephemeral_provider_test_never_persists_or_activates(resources, monkeypatch) -> None:
    client, host, _, _ = resources
    active_before = host.config_manager.config.providers.active
    save_calls: list[bool] = []
    host.config_manager.save_config = lambda: save_calls.append(True)

    response = await client.post(
        "/api/v1/settings/provider-configurations/test",
        json={"name": "Probe", "protocol": "openai", "apiBase": "https://example.test/v1", "apiKey": "probe", "defaultModel": "probe-model", "enabled": True, "makeActive": False},
    )

    assert response.status_code == 200
    assert save_calls == []
    assert host.config_manager.config.providers.active == active_before
    assert host.replace_calls == []
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/webapi/test_conversations_agents_reporting.py -q -k "full_provider_save or ephemeral_provider_test"`

Expected: FAIL with `404 Not Found` because the resource-oriented endpoints do not yet exist.

- [ ] **Step 3: Implement complete request validation and one mutation**

Define Pydantic request models with `extra="forbid"`. Validate a non-empty name/model, supported protocol, object-shaped `extraHeaders`, and an HTTPS URL except Custom localhost HTTP addresses. On save, locate the configuration strictly by URL ID; create it when absent or update it when present. Preserve an existing YAML key if the update omits `apiKey`; reject an absent key for a new config and any write to an environment-owned key. When `makeActive` is true, set the active ID and synchronized defaults in the same `SettingsService.mutate` callback. Use a temporary `ApiConfig` and the existing provider factory for the test endpoint without invoking `mutate`.

- [ ] **Step 4: Add rollback and protocol coverage**

Extend the focused suite with these assertions:

```python
@pytest.mark.asyncio
async def test_failed_provider_save_restores_active_config_and_defaults(resources, monkeypatch) -> None:
    client, host, _, _ = resources
    before = deepcopy(host.config_manager.config)
    monkeypatch.setattr(host, "replace_config", failing_replace_config)

    response = await client.put("/api/v1/settings/provider-configurations/mimo-cn", json=valid_mimo_body())

    assert response.status_code == 500
    assert host.config_manager.config == before


@pytest.mark.asyncio
async def test_new_provider_requires_key_and_rejects_unknown_protocol(resources) -> None:
    client, _, _, _ = resources
    missing_key = await client.put("/api/v1/settings/provider-configurations/new", json={**valid_mimo_body(), "apiKey": None})
    invalid_protocol = await client.put("/api/v1/settings/provider-configurations/new", json={**valid_mimo_body(), "protocol": "unknown"})

    assert missing_key.status_code == 422
    assert invalid_protocol.status_code == 422
```

- [ ] **Step 5: Run backend tests and review the task diff**

Run: `uv run pytest tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py -q -k "provider or settings"`

Run: `git diff --check -- manyselves/application/settings_service.py manyselves/webapi/schemas/settings.py manyselves/webapi/routes/settings.py tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py`

Expected: all selected tests pass and the diff has no whitespace errors. Do not commit unless the user explicitly requests it.

### Task 3: Regenerate and validate the HTTP type contract

**Files:**
- Modify: `frontend-contract/openapi.json`
- Modify: `frontend/src/api/generated/schema.ts`
- Modify: `tests/webapi/test_openapi_contract.py`

**Interfaces:**
- Consumes FastAPI schemas from Tasks 1-2.
- Produces generated `ProviderPresetResponse.id`, `ProviderConfigurationUpsert`, `ProviderConfigurationTestRequest`, and both provider-configuration endpoints for TypeScript.

- [ ] **Step 1: Write failing OpenAPI assertions**

```python
def test_openapi_exposes_full_provider_configuration_contract(schema: dict) -> None:
    upsert = schema["components"]["schemas"]["ProviderConfigurationUpsert"]
    assert set(["name", "protocol", "apiBase", "defaultModel", "enabled", "makeActive"]) <= set(upsert["properties"])
    assert "/api/v1/settings/provider-configurations/{provider_config_id}" in schema["paths"]
    assert "/api/v1/settings/provider-configurations/test" in schema["paths"]
```

- [ ] **Step 2: Run the assertion and verify RED**

Run: `uv run pytest tests/webapi/test_openapi_contract.py -q -k "full_provider_configuration"`

Expected: FAIL because the generated FastAPI schema lacks the new models and paths.

- [ ] **Step 3: Export OpenAPI and regenerate TypeScript**

Run: `uv run python scripts/export_openapi.py frontend-contract/openapi.json`

Run: `npm run generate:api`

Working directory: `frontend`

Expected: `frontend-contract/openapi.json` and `frontend/src/api/generated/schema.ts` include the models from Task 2 with camel-case wire fields.

- [ ] **Step 4: Check deterministic exports and generated types**

Run: `uv run python scripts/export_openapi.py --check frontend-contract/openapi.json`

Run: `npm run check:api`

Working directory: `frontend`

Expected: both commands pass without modifying files.

- [ ] **Step 5: Review the task diff**

Run: `git diff --check -- frontend-contract/openapi.json frontend/src/api/generated/schema.ts tests/webapi/test_openapi_contract.py`

Expected: no whitespace errors. Do not commit unless the user explicitly requests it.

### Task 4: Replace the ambiguous form with a complete provider connection form

**Files:**
- Create: `frontend/src/features/settings/ProviderConfigurationForm.tsx`
- Create: `frontend/src/features/settings/ProviderConfigurationForm.test.tsx`
- Modify: `frontend/src/features/settings/SettingsPage.tsx`
- Modify: `frontend/src/features/settings/settings-api.ts`
- Modify: `frontend/src/features/settings/settings-api.test.ts`
- Modify: `frontend/src/features/settings/settings.css`
- Modify: `frontend/src/features/settings/EnhancedModelSettings.tsx`
- Modify: `frontend/src/features/settings/SettingsPage.test.tsx`

**Interfaces:**
- Consumes generated upsert/test request and response types from Task 3.
- Produces `upsertProviderConfiguration(id, body)` and `testProviderConfiguration(body)` on `SettingsApi`.
- Produces a form whose preset select value is `preset.id`, whose protocol select is explicit, and whose test operation is non-persistent.

- [ ] **Step 1: Write failing interaction tests**

```tsx
it("keeps two Anthropic-compatible presets distinct and saves the selected preset identity", async () => {
  const api = createSettingsApiMock({ presets: [mimoPreset, claudePreset] });
  render(<ProviderConfigurationForm api={api} settings={legacySettings} presets={[mimoPreset, claudePreset]} />);

  await user.selectOptions(screen.getByLabelText("预设"), "xiaomi-mimo-token-plan-cn");
  await user.type(screen.getByLabelText("API Key"), "new-key");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(api.upsertProviderConfiguration).toHaveBeenCalledWith(
    expect.any(String),
    expect.objectContaining({ presetId: "xiaomi-mimo-token-plan-cn", protocol: "anthropic", makeActive: true }),
  );
});

it("tests unsaved values without refreshing settings", async () => {
  const api = createSettingsApiMock();
  render(<ProviderConfigurationForm api={api} settings={legacySettings} presets={[mimoPreset]} />);

  await user.type(screen.getByLabelText("API Key"), "probe-key");
  await user.click(screen.getByRole("button", { name: "测试连接" }));

  expect(api.testProviderConfiguration).toHaveBeenCalledWith(expect.objectContaining({ apiKey: "probe-key" }));
  expect(api.getSettings).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: Run the form tests and verify RED**

Run: `npm test -- --run src/features/settings/ProviderConfigurationForm.test.tsx src/features/settings/settings-api.test.ts`

Working directory: `frontend`

Expected: FAIL because the existing form identifies presets by protocol and has no full-form upsert or ephemeral test API.

- [ ] **Step 3: Implement explicit fields and safe form state**

Create `ProviderConfigurationForm` with required labels for Preset, Configuration name, Protocol, API Base URL, API Key, Default model, and Set as active configuration. Add a closed Advanced section for Enabled and Additional headers JSON. Selecting a preset fills protocol/base/model/name from that preset; selecting Custom clears `presetId` and requires protocol choice. Empty key values are sent only for a new configuration; existing configuration edits omit the key. Do not populate the key input from server state.

- [ ] **Step 4: Implement save, test, and legacy cleanup behavior**

`testProviderConfiguration` sends the current unsaved form and only displays its safe result. `upsertProviderConfiguration` sends the complete form and then reloads settings from the masked server response. Display a current-active summary from returned data. Inactive rows with no `presetId` remain visible as Custom or Legacy and retain the existing explicit delete action; do not automatically delete the current duplicate MiMo records. Remove the settings page's runtime dependency on `applyPreset`.

- [ ] **Step 5: Run focused frontend tests and review the diff**

Run: `npm test -- --run src/features/settings/ProviderConfigurationForm.test.tsx src/features/settings/SettingsPage.test.tsx src/features/settings/settings-api.test.ts`

Run: `npm run build`

Working directory: `frontend`

Run: `git diff --check -- frontend/src/features/settings frontend/src/api/generated/schema.ts`

Expected: all tests and the production build pass with no whitespace errors. Do not commit unless the user explicitly requests it.

### Task 5: Validate recovery, document migration, and run the regression set

**Files:**
- Modify: `docs/CONFIG.md`
- Modify: `docs/PROVIDERS.md`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`
- Modify: `frontend/src/features/settings/ProviderConfigurationForm.test.tsx`

**Interfaces:**
- Documents protocol selection, write-only key behavior, connection testing, active configuration selection, and manual legacy cleanup.
- Verifies the reported Xiaomi MiMo configuration saves as the active Anthropic-compatible endpoint and cannot overwrite another Anthropic-compatible configuration.

- [ ] **Step 1: Add the reported MiMo regression test**

```python
@pytest.mark.asyncio
async def test_mimo_save_does_not_overwrite_another_anthropic_configuration(resources) -> None:
    client, host, _, _ = resources
    await save_claude_configuration(client)
    response = await client.put("/api/v1/settings/provider-configurations/mimo-cn", json=valid_mimo_body())

    providers = {item["id"]: item for item in response.json()["providers"]}
    assert providers["mimo-cn"]["apiBase"] == "https://token-plan-cn.xiaomimimo.com/anthropic"
    assert providers["mimo-cn"]["defaultModel"] == "mimo-v2.5-pro"
    assert providers["claude-official"]["defaultModel"] == "claude-sonnet-4-20250514"
    assert providers["mimo-cn"]["active"] is True
```

- [ ] **Step 2: Run the regression test and verify RED before the final fix if needed**

Run: `uv run pytest tests/webapi/test_conversations_agents_reporting.py -q -k "mimo_save_does_not_overwrite"`

Expected: FAIL before Tasks 1-2 are complete; PASS after the atomic save implementation.

- [ ] **Step 3: Document the supported migration path**

Document these exact user steps: save the intended preset as a new active configuration, run an ephemeral connection test, confirm the Active summary, then manually delete only verified inactive duplicate configurations. State that protocol is not a provider identity and that API keys are never readable after save.

- [ ] **Step 4: Run complete targeted verification**

Run: `uv run pytest tests/test_presets.py tests/webapi/test_conversations_agents_reporting.py tests/webapi/test_openapi_contract.py -q -k "preset or provider or settings or mimo"`

Run: `npm test -- --run src/features/settings`

Run: `npm run check:api`

Run: `npm run build`

Working directory for the last three commands: `frontend`

Expected: all commands pass. Any unrelated pre-existing failure is recorded separately and is not changed by this task.

- [ ] **Step 5: Review the final change set**

Run: `git diff --check`

Run: `git status --short`

Expected: no whitespace errors; only the planned files plus pre-existing user changes appear. Do not commit unless the user explicitly requests it.
