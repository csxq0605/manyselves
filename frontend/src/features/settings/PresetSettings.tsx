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
          <p className="settings-kicker">PROVIDER CATALOG</p>
          <h2 id="preset-settings-title">Provider 预设</h2>
        </div>
        <button onClick={() => void onSync()} type="button">同步预设</button>
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
