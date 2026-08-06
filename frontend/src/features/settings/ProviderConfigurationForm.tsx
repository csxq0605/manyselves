import { useMemo, useState } from "react";

import type {
  PresetListResponse,
  ProviderConfigurationUpsert,
  ProviderConnectionTestRequest,
  SettingsApi,
  SettingsResponse,
} from "./settings-api";

const CUSTOM_PRESET_ID = "__custom__";

interface ProviderDraft {
  apiBase: string;
  apiKey: string;
  configurationId: string;
  defaultModel: string;
  enabled: boolean;
  extraHeaders: string;
  makeActive: boolean;
  name: string;
  presetId: string | null;
  protocol: string;
}

export interface ProviderConfigurationFormProps {
  readonly api: Pick<SettingsApi, "testProviderConfiguration" | "testProviderConnection" | "upsertProviderConfiguration"> &
    Partial<Pick<SettingsApi, "removeProvider">>;
  readonly busy?: boolean;
  readonly onSaved: (settings: SettingsResponse) => Promise<void> | void;
  readonly presets: PresetListResponse["presets"];
  readonly settings: SettingsResponse;
}

function isMimoPreset(preset: PresetListResponse["presets"][number]): boolean {
  return preset.name.toLowerCase().includes("mimo");
}

function sortedPresets(presets: PresetListResponse["presets"]) {
  return [...presets].sort((left, right) => {
    if (isMimoPreset(left)) return -1;
    if (isMimoPreset(right)) return 1;
    return left.name.localeCompare(right.name);
  });
}

function activeProvider(settings: SettingsResponse) {
  return settings.providers.find((provider) => provider.active);
}

function presetForId(presets: PresetListResponse["presets"], presetId: string | null) {
  return presets.find((preset) => preset.id === presetId);
}

function providerForPreset(settings: SettingsResponse, presetId: string | null) {
  if (presetId === null) return activeProvider(settings)?.presetId === null ? activeProvider(settings) : undefined;
  return settings.providers.find((provider) => provider.presetId === presetId);
}

function initialPresetId(settings: SettingsResponse, presets: PresetListResponse["presets"]): string {
  const active = activeProvider(settings);
  if (active?.presetId && presetForId(presets, active.presetId)) return active.presetId;
  if (active) return CUSTOM_PRESET_ID;
  return presets[0]?.id ?? CUSTOM_PRESET_ID;
}

function makeDraft(
  settings: SettingsResponse,
  presets: PresetListResponse["presets"],
  selectedPresetId: string,
): ProviderDraft {
  const presetId = selectedPresetId === CUSTOM_PRESET_ID ? null : selectedPresetId;
  const preset = presetForId(presets, presetId);
  const active = activeProvider(settings);
  const existing = presetId === null && active && (!active.presetId || !presetForId(presets, active.presetId))
    ? active
    : providerForPreset(settings, presetId);
  const defaultConfigurationId = preset ? `preset-${preset.id}` : `custom-${Date.now()}`;

  return {
    apiBase: existing?.apiBase ?? preset?.baseUrl ?? "",
    apiKey: "",
    configurationId: existing?.id ?? defaultConfigurationId,
    defaultModel: existing?.defaultModel ?? preset?.defaultModel ?? settings.defaults.model,
    enabled: existing?.enabled ?? true,
    extraHeaders: "",
    makeActive: existing?.active ?? true,
    name: existing?.name ?? preset?.name ?? "自定义提供商",
    presetId,
    protocol: existing?.provider ?? preset?.provider ?? settings.defaults.provider,
  };
}

function draftMatchesConfiguredProvider(draft: ProviderDraft, provider: SettingsResponse["providers"][number]) {
  return draft.apiKey.trim() === ""
    && draft.apiBase.trim() === (provider.apiBase ?? "")
    && draft.defaultModel.trim() === (provider.defaultModel ?? "")
    && draft.enabled === provider.enabled
    && draft.extraHeaders.trim() === ""
    && draft.name.trim() === provider.name
    && draft.presetId === provider.presetId
    && draft.protocol.trim() === provider.provider;
}

function parseExtraHeaders(value: string): { headers?: Record<string, string>; error?: string } {
  if (!value.trim()) return {};
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      return { error: "附加请求头必须是 JSON 对象" };
    }
    const entries = Object.entries(parsed);
    if (entries.some(([key, headerValue]) => !key || typeof headerValue !== "string")) {
      return { error: "附加请求头的键和值都必须是字符串" };
    }
    return { headers: Object.fromEntries(entries) };
  } catch {
    return { error: "附加请求头不是有效的 JSON" };
  }
}

export function ProviderConfigurationForm({
  api,
  busy = false,
  onSaved,
  presets,
  settings,
}: ProviderConfigurationFormProps) {
  const orderedPresets = useMemo(() => sortedPresets(presets), [presets]);
  const [selectedPresetId, setSelectedPresetId] = useState(() => initialPresetId(settings, orderedPresets));
  const [draft, setDraft] = useState(() => makeDraft(settings, orderedPresets, initialPresetId(settings, orderedPresets)));
  const [actionError, setActionError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ message: string; model: string | null; ok: boolean } | null>(null);

  const selectedPreset = presetForId(orderedPresets, draft.presetId);
  const configuredProvider = settings.providers.find((provider) => provider.id === draft.configurationId);
  const requiresApiKey = !configuredProvider?.configured;
  const controlsDisabled = busy || isSaving || isTesting;
  const legacyProviders = settings.providers.filter((provider) => !provider.active && provider.presetId === null);

  function changeDraft<K extends keyof ProviderDraft>(key: K, value: ProviderDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
    setActionError(null);
    setTestResult(null);
  }

  function selectPreset(nextPresetId: string) {
    setSelectedPresetId(nextPresetId);
    setDraft(makeDraft(settings, orderedPresets, nextPresetId));
    setActionError(null);
    setTestResult(null);
  }

  function requestPayload(includeApiKey: boolean): ProviderConnectionTestRequest | null {
    const headerResult = parseExtraHeaders(draft.extraHeaders);
    if (headerResult.error) {
      setActionError(headerResult.error);
      return null;
    }

    const apiKey = draft.apiKey.trim();
    const payload: ProviderConnectionTestRequest = {
      apiBase: draft.apiBase.trim() || null,
      defaultModel: draft.defaultModel.trim(),
      protocol: draft.protocol.trim(),
    };
    if (headerResult.headers) payload.extraHeaders = headerResult.headers;
    if (!payload.protocol || !payload.defaultModel) {
      setActionError("请填写协议和默认模型");
      return null;
    }
    if (includeApiKey && apiKey) return { ...payload, apiKey };
    return payload;
  }

  async function testConnection() {
    if (configuredProvider?.configured && draftMatchesConfiguredProvider(draft, configuredProvider)) {
      setIsTesting(true);
      setActionError(null);
      setTestResult(null);
      try {
        const result = await api.testProviderConnection(configuredProvider.id);
        setTestResult(result);
      } catch (error) {
        setActionError(error instanceof Error ? error.message : "杩炴帴娴嬭瘯澶辫触");
      } finally {
        setIsTesting(false);
      }
      return;
    }

    const payload = requestPayload(true);
    if (!payload) return;
    if (!("apiKey" in payload) || !payload.apiKey) {
      setActionError("测试连接需要 API Key");
      return;
    }

    setIsTesting(true);
    setActionError(null);
    setTestResult(null);
    try {
      const result = await api.testProviderConfiguration(payload);
      setTestResult(result);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "连接测试失败");
    } finally {
      setIsTesting(false);
    }
  }

  async function saveConfiguration() {
    const basePayload = requestPayload(true);
    if (!basePayload) return;
    const apiKey = draft.apiKey.trim();
    if (requiresApiKey && !apiKey) {
      setActionError("请填写 API Key");
      return;
    }

    const payload: ProviderConfigurationUpsert = {
      ...basePayload,
      enabled: draft.enabled,
      makeActive: draft.makeActive,
      name: draft.name.trim(),
      presetId: draft.presetId,
    };
    if (!payload.name) {
      setActionError("请填写配置名称");
      return;
    }

    setIsSaving(true);
    setActionError(null);
    try {
      const saved = await api.upsertProviderConfiguration(draft.configurationId, payload);
      const nextPresetId = initialPresetId(saved, orderedPresets);
      setSelectedPresetId(nextPresetId);
      setDraft(makeDraft(saved, orderedPresets, nextPresetId));
      await onSaved(saved);
      setTestResult(null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "保存配置失败");
    } finally {
      setIsSaving(false);
    }
  }

  async function removeLegacyProvider(providerId: string) {
    if (!api.removeProvider) return;
    setIsSaving(true);
    setActionError(null);
    try {
      const saved = await api.removeProvider(providerId);
      await onSaved(saved);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "删除配置失败");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="model-settings provider-configuration" aria-labelledby="model-settings-title">
      <div className="model-settings__heading">
        <div>
          <h2 id="model-settings-title">模型配置</h2>
          <p>预设仅填充连接参数；保存时会明确记录协议、地址、模型和活动配置。</p>
        </div>
        {activeProvider(settings) ? <span className="settings-status">当前活动：{activeProvider(settings)?.name}</span> : null}
      </div>

      <div className="provider-configuration__fields">
        <label>
          预设
          <select value={selectedPresetId} onChange={(event) => selectPreset(event.target.value)} disabled={controlsDisabled}>
            {orderedPresets.map((preset) => <option key={preset.id} value={preset.id}>{preset.name}{isMimoPreset(preset) ? "（推荐）" : ""}</option>)}
            <option value={CUSTOM_PRESET_ID}>自定义连接</option>
          </select>
        </label>
        <label>
          配置名称
          <input value={draft.name} onChange={(event) => changeDraft("name", event.target.value)} disabled={controlsDisabled} />
        </label>
        <label>
          协议
          <select value={draft.protocol} onChange={(event) => changeDraft("protocol", event.target.value)} disabled={controlsDisabled}>
            <option value="anthropic">Anthropic Messages</option>
            <option value="openai">OpenAI Compatible</option>
          </select>
        </label>
        <label>
          API 地址
          <input value={draft.apiBase} onChange={(event) => changeDraft("apiBase", event.target.value)} disabled={controlsDisabled} placeholder={selectedPreset?.baseUrl ?? "https://api.example.com/v1"} />
        </label>
        <label>
          API Key {requiresApiKey ? <span className="required">*</span> : null}
          <input aria-label="API Key" type="password" value={draft.apiKey} onChange={(event) => changeDraft("apiKey", event.target.value)} disabled={controlsDisabled || configuredProvider?.credentialSource === "environment"} placeholder={configuredProvider?.credentialSource === "environment" ? "由服务器环境管理" : configuredProvider?.configured ? "********（已保存，留空不修改）" : "输入 API Key"} />
          {configuredProvider?.credentialSource === "environment" ? <small>由服务器环境管理，不能在页面修改。</small> : null}
        </label>
        <label>
          默认模型
          <input value={draft.defaultModel} onChange={(event) => changeDraft("defaultModel", event.target.value)} disabled={controlsDisabled} placeholder={selectedPreset?.defaultModel ?? "例如 gpt-4o"} />
        </label>
      </div>

      <label className="settings-check provider-configuration__active">
        <input checked={draft.makeActive} type="checkbox" onChange={(event) => changeDraft("makeActive", event.target.checked)} disabled={controlsDisabled} />
        <span>设为活动配置</span>
      </label>

      <details className="provider-configuration__advanced">
        <summary>高级连接设置</summary>
        <div className="provider-configuration__advanced-fields">
          <label className="settings-check">
            <input checked={draft.enabled} type="checkbox" onChange={(event) => changeDraft("enabled", event.target.checked)} disabled={controlsDisabled} />
            <span>启用此配置</span>
          </label>
          <label>
            附加请求头（JSON）
            <textarea value={draft.extraHeaders} onChange={(event) => changeDraft("extraHeaders", event.target.value)} disabled={controlsDisabled} placeholder={'{"X-Client": "manyselves"}'} rows={3} />
          </label>
        </div>
      </details>

      {actionError ? <p className="settings-feedback-inline settings-feedback-inline--error" role="alert">{actionError}</p> : null}
      {testResult ? <p className={`settings-feedback-inline ${testResult.ok ? "settings-feedback-inline--success" : "settings-feedback-inline--error"}`} role="status">{testResult.ok ? `连接成功${testResult.model ? ` · ${testResult.model}` : ""}` : testResult.message}</p> : null}

      <div className="settings-actions settings-actions--primary">
        <button className="settings-button settings-button--secondary" disabled={controlsDisabled} onClick={testConnection} type="button">{isTesting ? "测试中…" : "测试连接"}</button>
        <button className="settings-button settings-button--primary" disabled={controlsDisabled} onClick={saveConfiguration} type="button">{isSaving ? "保存中…" : "保存配置"}</button>
      </div>

      {legacyProviders.length > 0 ? (
        <section className="provider-configuration__legacy" aria-labelledby="legacy-configurations-title">
          <h3 id="legacy-configurations-title">自定义或旧配置</h3>
          <p>这些配置不会自动删除；确认不再使用后可手动清理。</p>
          <ul className="provider-list">
            {legacyProviders.map((provider) => (
              <li key={provider.id}>
                <div className="provider-list__identity"><strong>{provider.name}</strong><small>{provider.provider} · {provider.defaultModel ?? "未设置模型"}</small></div>
                <button className="settings-button settings-button--danger" disabled={controlsDisabled || !api.removeProvider} onClick={() => void removeLegacyProvider(provider.id)} type="button">删除</button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}
