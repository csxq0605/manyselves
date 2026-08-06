# Provider Configuration Identity and Settings Design

## Goal

Make model-provider configuration unambiguous and recoverable by separating a preset's identity from its API protocol, saving complete connection settings atomically, and making the active runtime configuration explicit.

## Scope

This change covers provider connection settings only:

- provider configuration identity and preset selection;
- protocol, endpoint, credential, model, activation, and optional request headers;
- connection testing of an unsaved form;
- atomic persistence and runtime activation;
- safe handling of legacy duplicate configurations.

It does not place generation controls such as temperature or maximum output tokens on the provider connection form. Those remain model-behavior settings.

## Terminology

| Term | Meaning |
| --- | --- |
| `presetId` | Stable, unique identifier for a provider preset, such as `xiaomi-mimo-token-plan-cn`. It is never an API protocol name. |
| `protocol` | Request format understood by the configured endpoint. The first supported values are `anthropic` for Anthropic Messages and `openai` for OpenAI-compatible Chat Completions. |
| `providerConfigId` | UUID-like identity of one saved provider configuration. Multiple configurations may share a protocol. |
| active configuration | The sole saved configuration selected for runtime use. |

## Data Model

Extend each saved `ApiConfig` with an optional `preset_id` field. Existing fields retain their current meanings:

| Saved field | Required | Purpose |
| --- | --- | --- |
| `id` | yes | Unique `providerConfigId`; generated once and never inferred from protocol or preset. |
| `name` | yes | User-visible configuration name. |
| `preset_id` | no | The preset that last populated this configuration; absent for fully custom configurations. |
| `provider` | yes | Runtime protocol: `anthropic` or `openai`. |
| `api_base` | yes | Complete API base URL required by the selected protocol. |
| `api_key` | creation only | Secret credential. Omitted on an update means keep the existing credential. It is never returned by the API. |
| `default_model` | yes | Model identifier sent to the provider. |
| `extra_headers` | no | Object mapping header names to values for compatible gateways. |
| `enabled` | yes | Whether the configuration may be used. |

`providers.active` remains the only active-configuration pointer. When it changes, `agents.defaults.provider` and `agents.defaults.model` are synchronized to the active configuration's protocol and model. Runtime provider selection continues to use the active configuration ID.

## Presets

Preset responses add an `id` field. A preset has one protocol and can prefill a configuration name, endpoint, default model, and optional headers.

The UI must use `preset.id` as the select value and React key. It must never use `preset.provider` as a configuration identifier. This prevents all Anthropic-compatible presets from collapsing into one `anthropic` record.

Selecting a preset pre-populates the connection form but does not persist anything. Users may edit the prefilled name, endpoint, model, and optional headers before testing or saving. Edited fields do not convert the selection into a different identity; the saved record retains the selected `presetId` unless the user explicitly chooses the Custom option.

## UI

The model settings page presents one connection form with these fields:

1. **Preset**: optional selector with a Custom option. It fills the form only.
2. **Configuration name**: required, initialized from the preset name.
3. **Protocol**: required selector with `Anthropic Messages` and `OpenAI-compatible`. A preset supplies its protocol; Custom requires an explicit choice.
4. **API Base URL**: required HTTPS URL, except explicit localhost HTTP endpoints in Custom mode.
5. **API Key**: required for a new configuration. Existing saved credentials are masked; an empty update retains the stored secret.
6. **Default model**: required non-empty model identifier.
7. **Set as active configuration**: checked by default for every successful save from this page.
8. **Advanced settings**: collapsible `Additional headers (JSON object)` and `Enabled` controls.

The form displays the current active configuration name, protocol, model, and endpoint summary. Saving must show the canonical configuration returned by the server, including whether it is active; local assumptions must not override the server response.

## API Contract

Replace the preset-application-only save path with one resource-oriented upsert endpoint:

`PUT /api/v1/settings/provider-configurations/{provider_config_id}`

The client generates a new `provider_config_id` for a new configuration. Updating uses the selected existing ID. The request body contains the full non-secret connection definition plus an optional replacement key:

```json
{
  "presetId": "xiaomi-mimo-token-plan-cn",
  "name": "Xiaomi MiMo Token Plan (China)",
  "protocol": "anthropic",
  "apiBase": "https://token-plan-cn.xiaomimimo.com/anthropic",
  "apiKey": "only-when-created-or-replaced",
  "defaultModel": "mimo-v2.5-pro",
  "extraHeaders": {},
  "enabled": true,
  "makeActive": true
}
```

The endpoint validates the complete request before changing configuration. It then saves the configuration, updates `providers.active` when `makeActive` is true, synchronizes agent defaults, persists the configuration, and restarts the runtime as a single settings mutation. A validation, persistence, or restart failure restores the prior configuration and runtime state.

The response returns the full masked settings snapshot. It never returns an API key.

Remove the UI dependency on `POST /settings/presets/{provider}/apply`. The endpoint may remain temporarily for external compatibility, but it must be deprecated and must not be called by the React settings page.

## Connection Testing

Add an ephemeral connection-test endpoint that accepts the same connection fields as the save endpoint. It does not write configuration files, mutate active state, or restart the runtime. It validates the chosen protocol, constructs the matching provider, and runs the existing minimal provider probe.

The test button submits the current form, including an entered key. It is disabled until all required connection fields are valid. A passed test does not implicitly save. A failed test identifies validation failures separately from a provider connection failure without including credentials in the response or logs.

## Legacy Data Handling

Existing configurations remain readable. The settings list marks configurations without `preset_id` as custom or legacy according to their fields.

No legacy configuration is automatically deleted or silently rewritten, because existing credentials cannot safely identify an intended preset. The corrected save flow creates or updates the explicitly selected configuration and marks it active. The settings page provides an explicit delete action for inactive duplicate records after the user verifies them.

The current broken `anthropic` entry and duplicate custom MiMo entries are therefore recoverable: save a new correctly identified MiMo configuration, verify it is active and testable, then remove the inactive duplicates explicitly.

## Error Handling

- Invalid URL, missing required fields, invalid header JSON, unsupported protocol, and empty model fail before a connection test or save request.
- A blank API key on an existing configuration preserves its stored key; a blank key on a new configuration is invalid.
- An environment-managed key cannot be replaced through the UI and returns the existing managed-credential error.
- A failed save cannot leave a new inactive configuration, an unsynchronized default model, or a restarted runtime using the previous active configuration without restoring the old persisted state.
- Only a real controller-lease conflict is surfaced to the user. The UI must not conceal settings errors as a lease failure.

## Test Strategy

Backend tests cover:

- two Anthropic-compatible presets create distinct `providerConfigId` records and preserve their different endpoint/model values;
- a save with `makeActive` atomically updates the provider record, `providers.active`, and agent defaults;
- an update without `apiKey` preserves the existing secret;
- an invalid request, persistence failure, or runtime restart failure restores the original configuration and active provider;
- an ephemeral connection test cannot persist or activate a configuration;
- provider and header validation rejects unsupported protocol values and malformed header objects.

Frontend tests cover:

- preset selection uses unique preset IDs even when their protocols match;
- saving submits the complete connection form and displays the returned active configuration;
- Custom mode requires protocol selection;
- a form test sends unsaved fields without changing the loaded settings;
- duplicate legacy configurations remain visible until explicitly deleted.

## Acceptance Criteria

After saving the Xiaomi MiMo Token Plan preset with its API key, the settings response contains one active configuration whose protocol is `anthropic`, endpoint is `https://token-plan-cn.xiaomimimo.com/anthropic`, and default model is `mimo-v2.5-pro`. The defaults snapshot reports the same protocol and model. A subsequent runtime request uses that active configuration. Saving another Anthropic-compatible preset never overwrites the MiMo configuration.
