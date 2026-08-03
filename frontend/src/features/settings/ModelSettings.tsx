import { useState } from "react";

import type { SettingsResponse } from "./settings-api";

export interface ModelSettingsProps {
  readonly onSaveModel: (model: string) => Promise<void>;
  readonly onSelectProvider: (providerId: string, provider: string) => void;
  readonly settings: SettingsResponse;
}

export function ModelSettings({ onSaveModel, onSelectProvider, settings }: ModelSettingsProps) {
  const [model, setModel] = useState(settings.defaults.model);
  const activeProvider = settings.providers.find((provider) => provider.active) ?? settings.providers[0];
  const [providerId, setProviderId] = useState(activeProvider?.id ?? "");

  return (
    <section className="settings-card" aria-labelledby="model-settings-title">
      <div className="settings-card__heading">
        <div>
          <p className="settings-kicker">RUNTIME DEFAULTS</p>
          <h2 id="model-settings-title">模型与默认 Provider</h2>
        </div>
        <span className="settings-scope">服务端</span>
      </div>
      <label>
        默认模型
        <input value={model} onChange={(event) => setModel(event.target.value)} />
      </label>
      <button disabled={!model.trim()} onClick={() => void onSaveModel(model.trim())} type="button">
        保存模型
      </button>
      <label>
        默认 Provider
        <select value={providerId} onChange={(event) => setProviderId(event.target.value)}>
          {settings.providers.map((provider) => (
            <option key={provider.id} value={provider.id}>{provider.name}</option>
          ))}
        </select>
      </label>
      <button
        disabled={!providerId}
        onClick={() => {
          const provider = settings.providers.find((item) => item.id === providerId);
          if (provider) onSelectProvider(provider.id, provider.provider);
        }}
        type="button"
      >切换默认 Provider</button>
    </section>
  );
}
