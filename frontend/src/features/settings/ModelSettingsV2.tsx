import { useState } from "react";

import type {
  ProviderConnectionTestResponse,
  SettingsResponse,
} from "./settings-api";

export interface ModelSettingsInput {
  readonly apiBase: string | null;
  readonly apiKey?: string;
  readonly model: string;
  readonly provider: string;
  readonly providerId: string;
}

export interface ModelSettingsProps {
  readonly busy: boolean;
  readonly onSave: (input: ModelSettingsInput) => Promise<boolean>;
  readonly onTest: (providerId: string) => Promise<ProviderConnectionTestResponse>;
  readonly settings: SettingsResponse;
}

export function ModelSettings({ busy, onSave, onTest, settings }: ModelSettingsProps) {
  const active = settings.providers.find((provider) => provider.active) ?? settings.providers[0];
  const [providerId, setProviderId] = useState(active?.id ?? "");
  const selected = settings.providers.find((provider) => provider.id === providerId) ?? active;
  const [apiBase, setApiBase] = useState(selected?.apiBase ?? "");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState(selected?.defaultModel ?? settings.defaults.model);
  const [probe, setProbe] = useState<ProviderConnectionTestResponse | null>(null);

  if (!selected) {
    return (
      <section className="model-settings model-settings--empty">
        <h2>模型设置</h2>
        <p>尚未配置模型提供商。</p>
      </section>
    );
  }

  function selectProvider(nextId: string): void {
    const next = settings.providers.find((provider) => provider.id === nextId);
    setProviderId(nextId);
    setApiBase(next?.apiBase ?? "");
    setApiKey("");
    setModel(next?.defaultModel ?? settings.defaults.model);
    setProbe(null);
  }

  return (
    <section className="model-settings-v2" aria-labelledby="model-settings-title">
      <div className="model-settings-v2__header">
        <h2 id="model-settings-title">模型配置</h2>
        <span className={`model-settings-v2__status ${selected.configured ? "model-settings-v2__status--ready" : ""}`}>
          {selected.configured ? "已配置" : "待配置"}
        </span>
      </div>

      <div className="model-settings-v2__content">
        {/* 提供商选择 */}
        <div className="model-settings-v2__group">
          <label className="model-settings-v2__label">
            <span className="model-settings-v2__label-text">模型提供商</span>
            <select
              className="model-settings-v2__select"
              value={providerId}
              onChange={(e) => selectProvider(e.target.value)}
            >
              {settings.providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* API Key */}
        <div className="model-settings-v2__group">
          <label className="model-settings-v2__label">
            <span className="model-settings-v2__label-text">API Key</span>
            <input
              className="model-settings-v2__input"
              type="password"
              placeholder={selected.configured ? "已配置，留空表示不修改" : "输入 API Key"}
              value={apiKey}
              onChange={(e) => {
                setApiKey(e.target.value);
                setProbe(null);
              }}
            />
          </label>
        </div>

        {/* API 地址 */}
        <div className="model-settings-v2__group">
          <label className="model-settings-v2__label">
            <span className="model-settings-v2__label-text">API 地址</span>
            <input
              className="model-settings-v2__input"
              type="text"
              placeholder={selected.provider === "openai" ? "https://api.openai.com/v1" : ""}
              value={apiBase}
              onChange={(e) => {
                setApiBase(e.target.value);
                setProbe(null);
              }}
            />
          </label>
        </div>

        {/* 默认模型 */}
        <div className="model-settings-v2__group">
          <label className="model-settings-v2__label">
            <span className="model-settings-v2__label-text">默认模型</span>
            <input
              className="model-settings-v2__input"
              type="text"
              placeholder="例如 mimo-v2.5-pro"
              value={model}
              onChange={(e) => {
                setModel(e.target.value);
                setProbe(null);
              }}
            />
          </label>
        </div>

        {/* 测试结果 */}
        {probe && (
          <div className={`model-settings-v2__probe ${probe.ok ? "model-settings-v2__probe--success" : "model-settings-v2__probe--error"}`}>
            {probe.ok ? `✓ 连接成功 (${probe.model || selected.name})` : `✗ ${probe.message}`}
          </div>
        )}

        {/* 操作按钮 */}
        <div className="model-settings-v2__actions">
          <button
            className="model-settings-v2__button model-settings-v2__button--secondary"
            disabled={busy || !selected.configured}
            onClick={() => {
              setProbe(null);
              void onTest(selected.id).then(setProbe);
            }}
            type="button"
          >
            测试连接
          </button>
          <button
            className="model-settings-v2__button model-settings-v2__button--primary"
            disabled={busy || !model.trim()}
            onClick={() => {
              void onSave({
                apiBase: apiBase.trim() || null,
                ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {}),
                model: model.trim(),
                provider: selected.provider,
                providerId: selected.id,
              }).then((saved) => {
                if (saved) setApiKey("");
              });
            }}
            type="button"
          >
            保存
          </button>
        </div>
      </div>
    </section>
  );
}