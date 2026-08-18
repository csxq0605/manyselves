import { useState } from "react";

import type { PresetListResponse, SettingsResponse } from "./settings-api";

export interface SimplifiedModelSettingsProps {
  readonly busy: boolean;
  readonly presets: PresetListResponse["presets"];
  readonly settings: SettingsResponse;
  readonly onSave: (providerId: string, apiKey: string) => Promise<boolean>;
}

export function SimplifiedModelSettings({ busy, presets, settings, onSave }: SimplifiedModelSettingsProps) {
  // MiMo 预设置顶
  const sortedPresets = [...presets].sort((a, b) => {
    if (a.name.toLowerCase().includes("mimo")) return -1;
    if (b.name.toLowerCase().includes("mimo")) return 1;
    return a.name.localeCompare(b.name);
  });

  const activeProvider = settings.providers.find((p) => p.active);
  const [selectedPresetId, setSelectedPresetId] = useState(activeProvider?.id ?? presets[0]?.provider ?? "");
  const [apiKey, setApiKey] = useState("");

  const selectedPreset = presets.find((p) => p.provider === selectedPresetId) ?? presets[0];

  return (
    <section className="model-settings" aria-labelledby="model-settings-title">
      <div className="model-settings__heading">
        <div>
          <h2 id="model-settings-title">模型配置</h2>
          <p>选择预设提供商并配置 API Key 即可使用。MiMo 为默认推荐。</p>
        </div>
      </div>

      <div className="model-settings__fields">
        <label>
          选择提供商
          <select
            value={selectedPresetId}
            onChange={(event) => setSelectedPresetId(event.target.value)}
            disabled={busy}
          >
            {sortedPresets.map((preset) => (
              <option key={preset.provider} value={preset.provider}>
                {preset.name}
                {preset.name.toLowerCase().includes("mimo") ? " (推荐)" : ""}
              </option>
            ))}
          </select>
        </label>

        {selectedPreset && (
          <div className="preset-info">
            <p><strong>{selectedPreset.name}</strong></p>
            <p>{selectedPreset.description}</p>
            <p><small>默认模型: {selectedPreset.defaultModel}</small></p>
          </div>
        )}

        <label>
          API Key
          <input
            type="password"
            placeholder="输入你的 API Key"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            disabled={busy}
          />
        </label>

        <button
          className="settings-button settings-button--primary"
          disabled={busy || !apiKey.trim()}
          onClick={() => void onSave(selectedPresetId, apiKey.trim())}
          type="button"
        >
          {busy ? "保存中..." : "保存配置"}
        </button>
      </div>
    </section>
  );
}