import { useEffect, useState } from "react";

import { ClientPreferences } from "./ClientPreferences";
import { CollapsibleSection } from "./CollapsibleSection";
import { ProviderConfigurationForm } from "./ProviderConfigurationForm";
import type {
  PresetListResponse,
  SettingsApi,
  SettingsResponse,
  SettingsValidationResponse,
} from "./settings-api";
import type { SettingsStorage } from "./settings-storage";
import "./settings.css";

const appVersion = "V0.0.1";

export interface SettingsPageProps {
  readonly api: SettingsApi;
  readonly storage: SettingsStorage;
}

interface LoadedSettings {
  readonly presets: PresetListResponse["presets"];
  readonly settings: SettingsResponse;
  readonly validation: SettingsValidationResponse;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "设置操作失败";
}

export function SettingsPage({ api, storage }: SettingsPageProps) {
  const [loaded, setLoaded] = useState<LoadedSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const busy = false;
  const [preferences] = useState(() => storage.loadPreferences());

  useEffect(() => {
    let active = true;
    void Promise.all([
      api.getSettings(),
      api.listPresets(),
      api.validateSettings(),
    ]).then(([settings, presetResult, validation]) => {
      if (active) {
        setLoaded({
          presets: presetResult.presets,
          settings,
          validation,
        });
      }
    }).catch((loadError: unknown) => {
      if (active) setError(errorMessage(loadError));
    });
    return () => { active = false; };
  }, [api]);

  async function handleSavedProviderConfiguration(settings: SettingsResponse): Promise<void> {
    setError(null);
    setNotice(null);
    try {
      const validation = await api.validateSettings();
      setLoaded((current) => current ? { ...current, settings, validation } : null);
      setNotice("模型配置已保存，立即生效");
    } catch (actionError: unknown) {
      setError(errorMessage(actionError));
      throw actionError;
    }
  }

  if (error && !loaded) {
    return (
      <section className="settings-workspace settings-workspace--error">
        <h1>设置无法加载</h1>
        <p role="alert">{error}</p>
        <p>请检查服务器状态后重试，或重新登录。</p>
      </section>
    );
  }

  if (!loaded) return <p className="settings-loading" role="status">正在读取模型设置…</p>;

  return (
    <main className="settings-workspace" aria-busy={busy}>
      <header className="settings-hero">
        <div>
          <p className="settings-eyebrow">设置</p>
          <h1>模型设置</h1>
          <p>选择预设提供商并配置 API Key 即可使用。</p>
        </div>
        <span className="settings-version">版本 {appVersion}</span>
      </header>

      {error ? <p className="settings-feedback settings-feedback--error" role="alert">{error}</p> : null}
      {notice ? <p className="settings-feedback settings-feedback--success" role="status">{notice}</p> : null}

      <ProviderConfigurationForm
        api={api}
        busy={busy}
        presets={loaded.presets}
        settings={loaded.settings}
        onSaved={handleSavedProviderConfiguration}
      />

      <CollapsibleSection title="高级设置">
        <ClientPreferences
          initialValue={preferences}
          onSave={(value) => storage.savePreferences(value)}
        />

        <section className="settings-card" aria-labelledby="validation-title">
          <h3 id="validation-title">配置校验</h3>
          {loaded.validation.valid ? (
            <p className="settings-status settings-status--ready">✅ 所有配置有效</p>
          ) : (
            <ul className="validation-errors">
              {loaded.validation.errors.map((error, index) => (
                <li key={index}>{error}</li>
              ))}
            </ul>
          )}
        </section>
      </CollapsibleSection>
    </main>
  );
}
