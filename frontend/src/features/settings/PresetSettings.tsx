import type { PresetListResponse } from "./settings-api";

export interface PresetSettingsProps {
  readonly onSync: () => Promise<void>;
  readonly presets: PresetListResponse["presets"];
}

export function PresetSettings({ onSync, presets }: PresetSettingsProps) {
  return (
    <section className="settings-card settings-card--wide" aria-labelledby="preset-settings-title">
      <div className="settings-card__heading">
        <div>
          <h3 id="preset-settings-title">提供商预设</h3>
          <p>查看或同步内置的模型服务配置。</p>
        </div>
        <button className="settings-button settings-button--quiet" onClick={() => void onSync()} type="button">同步预设</button>
      </div>
      {presets.length === 0 ? <p className="settings-note">当前没有可用预设。</p> : (
        <ul className="preset-list">
          {presets.map((preset) => (
            <li key={`${preset.provider}:${preset.name}`}>
              <strong>{preset.name}</strong>
              <span>{preset.category} · {preset.defaultModel}</span>
              <small>{preset.description}</small>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
