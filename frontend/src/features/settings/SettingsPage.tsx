import { useEffect, useState } from "react";

import { ClientPreferences } from "./ClientPreferences";
import { ModelSettings } from "./ModelSettings";
import { PresetSettings } from "./PresetSettings";
import { ProviderSettings, type RestartRequest } from "./ProviderSettings";
import type {
  AgentDebugResponse,
  AgentListResponse,
  PresetListResponse,
  ProviderSettingsCreate,
  ProviderSettingsUpdate,
  SettingsApi,
  SettingsResponse,
  SettingsValidationResponse,
} from "./settings-api";
import type { SettingsStorage } from "./settings-storage";
import "./settings.css";

export interface SettingsPageProps {
  readonly api: SettingsApi;
  readonly storage: SettingsStorage;
}

interface LoadedSettings {
  readonly agents: AgentListResponse["agents"];
  readonly debug: Readonly<Record<string, AgentDebugResponse>>;
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
  const [pendingRestart, setPendingRestart] = useState<RestartRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [preferences] = useState(() => storage.loadPreferences());

  useEffect(() => {
    let active = true;
    void Promise.all([
      api.getSettings(),
      api.listPresets(),
      api.validateSettings(),
      api.listAgents(),
    ]).then(async ([settings, presetResult, validation, agentResult]) => {
      const debugEntries = await Promise.all(agentResult.agents.map(async (agent) => [
        agent.id,
        await api.getAgentDebug(agent.id),
      ] as const));
      if (active) {
        setLoaded({
          agents: agentResult.agents,
          debug: Object.fromEntries(debugEntries),
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

  async function run(action: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (actionError) {
      setError(errorMessage(actionError));
    } finally {
      setBusy(false);
    }
  }

  function replaceSettings(settings: SettingsResponse): void {
    setLoaded((current) => current ? { ...current, settings } : current);
  }

  async function createProvider(input: ProviderSettingsCreate): Promise<void> {
    const settings = await api.createProvider(input);
    replaceSettings(settings);
    setNotice("Provider 已创建");
  }

  async function updateProvider(providerId: string, input: ProviderSettingsUpdate): Promise<void> {
    const settings = await api.updateProvider(providerId, input);
    replaceSettings(settings);
    setNotice("Provider 设置已应用");
  }

  async function removeProvider(providerId: string): Promise<void> {
    const settings = await api.removeProvider(providerId);
    replaceSettings(settings);
    setNotice("Provider 已删除");
  }

  if (error && !loaded) {
    return (
      <section className="settings-workspace settings-workspace--error">
        <h1>设置无法加载</h1>
        <p role="alert">{error}</p>
        <p>请检查服务状态后重试，或重新登录。</p>
      </section>
    );
  }
  if (!loaded) {
    return <p className="settings-loading" role="status">正在读取安全设置…</p>;
  }

  return (
    <div className="settings-workspace" aria-busy={busy}>
      <header className="settings-hero">
        <div>
          <p className="settings-kicker">RUNTIME CONTROL PLANE</p>
          <h1>连接、模型与运行策略</h1>
          <p>服务端配置会影响整个 Agent Runtime；显示偏好只影响当前浏览器。</p>
        </div>
        <div className={`settings-health ${loaded.validation.valid ? "settings-health--valid" : ""}`}>
          <span aria-hidden="true" />
          <strong>{loaded.validation.valid ? "设置有效" : "需要处理"}</strong>
          <small>{loaded.validation.availableProviders.length} 个可用 Provider</small>
        </div>
      </header>

      {error ? <p className="settings-feedback settings-feedback--error" role="alert">{error}</p> : null}
      {notice ? <p className="settings-feedback" role="status">{notice}</p> : null}

      <div className="settings-grid">
        <ProviderSettings
          key={loaded.settings.providers.map((provider) => [
            provider.id,
            provider.apiBase,
            provider.defaultModel,
            provider.enabled,
            provider.configured,
          ].join(":" )).join("|")}
          onCreate={createProvider}
          onRemove={removeProvider}
          onUpdate={updateProvider}
          providers={loaded.settings.providers}
          requestRestart={setPendingRestart}
        />
        <ModelSettings
          key={`${loaded.settings.defaults.provider}:${loaded.settings.defaults.model}:${loaded.settings.providers.find((provider) => provider.active)?.id ?? ""}`}
          settings={loaded.settings}
          onSaveModel={async (model) => run(async () => {
            const settings = await api.updateDefaults({ model });
            replaceSettings(settings);
            setNotice("默认模型已保存，无需重启 Agent");
          })}
          onSelectProvider={(providerId, provider) => setPendingRestart({
            description: "切换默认 Provider",
            run: async () => {
              const settings = await api.updateDefaults({ activeProviderId: providerId, provider });
              replaceSettings(settings);
              setNotice("默认 Provider 已切换");
            },
          })}
        />
        <PresetSettings
          presets={loaded.presets}
          onSync={() => run(async () => {
            const result = await api.syncPresets();
            const presets = await api.listPresets();
            setLoaded((current) => current ? { ...current, presets: presets.presets } : current);
            setNotice(`已同步 ${result.downloaded} 个预设`);
          })}
        />
        <section className="settings-card" aria-labelledby="validation-title">
          <div className="settings-card__heading">
            <div>
              <p className="settings-kicker">SERVER CHECK</p>
              <h2 id="validation-title">配置校验</h2>
            </div>
            <button onClick={() => void run(async () => {
              const validation = await api.validateSettings();
              setLoaded((current) => current ? { ...current, validation } : current);
              setNotice(validation.valid ? "设置有效" : "设置仍需处理");
            })} type="button">重新校验</button>
          </div>
          {loaded.validation.errors.length === 0 ? (
            <p className="settings-validation-ok">设置有效</p>
          ) : (
            <ul>{loaded.validation.errors.map((item) => <li key={item}>{item}</li>)}</ul>
          )}
        </section>
        <section className="settings-card" aria-labelledby="debug-settings-title">
          <div className="settings-card__heading">
            <div>
              <p className="settings-kicker">AGENT TELEMETRY</p>
              <h2 id="debug-settings-title">Agent 调试模式</h2>
            </div>
            <span className="settings-scope">服务端</span>
          </div>
          <div className="debug-toggle-list">
            {loaded.agents.map((agent) => (
              <label className="settings-check" key={agent.id}>
                <input
                  aria-label={`${agent.id} 调试模式`}
                  checked={loaded.debug[agent.id]?.enabled ?? false}
                  type="checkbox"
                  onChange={(event) => {
                    const enabled = event.target.checked;
                    void run(async () => {
                      const debug = await api.updateAgentDebug(agent.id, enabled);
                      setLoaded((current) => current ? {
                        ...current,
                        debug: { ...current.debug, [agent.id]: debug },
                      } : current);
                      setNotice(`${agent.id} 调试模式已${enabled ? "开启" : "关闭"}`);
                    });
                  }}
                />
                <span><strong>{agent.id}</strong><small>{agent.status}</small></span>
              </label>
            ))}
          </div>
        </section>
        <ClientPreferences
          initialValue={preferences}
          onSave={(value) => {
            storage.savePreferences(value);
            setNotice("客户端偏好已保存");
          }}
        />
      </div>

      {pendingRestart ? (
        <div className="restart-backdrop">
          <div aria-labelledby="restart-dialog-title" aria-modal="true" className="restart-dialog" role="dialog">
            <p className="settings-kicker">RUNTIME RESTART</p>
            <h2 id="restart-dialog-title">确认重启 Agent</h2>
            <p>{pendingRestart.description}将重新启动 Agent，当前正在执行的任务可能被中断。</p>
            <div className="settings-actions">
              <button onClick={() => setPendingRestart(null)} type="button">取消</button>
              <button onClick={() => {
                const action = pendingRestart.run;
                setPendingRestart(null);
                void run(action);
              }} type="button">确认并应用</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
