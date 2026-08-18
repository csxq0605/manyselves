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
  const environmentManaged = selected?.credentialSource === "environment";

  if (!selected) {
    return (
      <section className="model-settings model-settings--empty">
        <h2>模型设置</h2>
        <p>尚未配置模型提供商。请在高级设置中创建第一个提供商。</p>
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
    <section className="model-settings" aria-labelledby="model-settings-title">
      <div className="model-settings__heading">
        <div>
          <h2 id="model-settings-title">模型配置</h2>
          <p>配置 Agent 默认使用的模型。密钥只会提交到服务器，不会从服务器返回。</p>
        </div>
        <span className={`settings-status${selected.configured ? " settings-status--ready" : ""}`}>
          {selected.configured ? "已配置" : "待配置"}
        </span>
      </div>

      <div className="model-settings__fields">
        <label>
          模型提供商
          <select value={providerId} onChange={(event) => selectProvider(event.target.value)}>
            {settings.providers.map((provider) => (
              <option key={provider.id} value={provider.id}>{provider.name}</option>
            ))}
          </select>
        </label>
        <label>
          API 地址（可选）
          <input
            inputMode="url"
            placeholder="使用提供商默认地址"
            value={apiBase}
            onChange={(event) => { setApiBase(event.target.value); setProbe(null); }}
          />
        </label>
        <label>
          API Key
          <input
            aria-label="API Key"
            aria-describedby="api-key-help"
            autoComplete="new-password"
            disabled={environmentManaged}
            placeholder={selected.configured ? "已配置，留空表示不修改" : "输入 API Key"}
            type="password"
            value={apiKey}
            onChange={(event) => { setApiKey(event.target.value); setProbe(null); }}
          />
          <small id="api-key-help">
            {environmentManaged
              ? <strong>由服务器环境管理</strong>
              : selected.configured
                ? "服务器已有密钥；此处始终保持空白。"
                : "密钥不会显示在页面或接口响应中。"}
          </small>
        </label>
        <label>
          默认模型
          <input
            placeholder="例如 gpt-5"
            value={model}
            onChange={(event) => { setModel(event.target.value); setProbe(null); }}
          />
        </label>
      </div>

      {probe ? (
        <p className={`settings-feedback-inline${probe.ok ? " settings-feedback-inline--success" : " settings-feedback-inline--error"}`} role="status">
          {probe.ok ? `连接成功 · ${probe.model ?? selected.name}` : probe.message}
        </p>
      ) : null}

      <div className="settings-actions settings-actions--primary">
        <button
          className="settings-button settings-button--secondary"
          disabled={busy || !selected.configured}
          onClick={() => {
            setProbe(null);
            void onTest(selected.id).then(setProbe);
          }}
          type="button"
        >测试连接</button>
        <button
          className="settings-button settings-button--primary"
          disabled={busy || !model.trim()}
          onClick={() => {
            void onSave({
              apiBase: apiBase.trim() || null,
              ...(apiKey.trim() && !environmentManaged ? { apiKey: apiKey.trim() } : {}),
              model: model.trim(),
              provider: selected.provider,
              providerId: selected.id,
            }).then((saved) => {
              if (saved) setApiKey("");
            });
          }}
          type="button"
        >保存</button>
      </div>
    </section>
  );
}
