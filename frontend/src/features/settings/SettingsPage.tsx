import { useEffect, useState } from "react";

import { ClientPreferences } from "./ClientPreferences";
import { ModelSettings, type ModelSettingsInput } from "./ModelSettings";
import { PresetSettings } from "./PresetSettings";
import { ProviderSettings } from "./ProviderSettings";
import type {
  AgentDebugResponse,
  AgentListResponse,
  PresetListResponse,
  ProviderConnectionTestResponse,
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

interface RestartRequest {
  readonly action: () => Promise<boolean>;
  readonly description: string;
  readonly resolve: (confirmed: boolean) => void;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "设置操作失败";
}

export function SettingsPage({ api, storage }: SettingsPageProps) {
  const [loaded, setLoaded] = useState<LoadedSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [restartRequest, setRestartRequest] = useState<RestartRequest | null>(null);
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

  async function run(action: () => Promise<void>): Promise<boolean> {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      return true;
    } catch (actionError) {
      setError(errorMessage(actionError));
      return false;
    } finally {
      setBusy(false);
    }
  }

  function requestRestart(
    description: string,
    action: () => Promise<boolean>,
  ): Promise<boolean> {
    return new Promise((resolve) => {
      setRestartRequest({ action, description, resolve });
    });
  }

  function cancelRestart(): void {
    restartRequest?.resolve(false);
    setRestartRequest(null);
  }

  async function confirmRestart(): Promise<void> {
    const request = restartRequest;
    if (!request) return;
    setRestartRequest(null);
    request.resolve(await request.action());
  }

  function replaceSettings(settings: SettingsResponse): void {
    setLoaded((current) => current ? { ...current, settings } : current);
  }

  async function createProvider(input: ProviderSettingsCreate): Promise<boolean> {
    return run(async () => {
      replaceSettings(await api.createProvider(input));
      setNotice("提供商已创建");
    });
  }

  async function updateProvider(providerId: string, input: ProviderSettingsUpdate): Promise<boolean> {
    return run(async () => {
      replaceSettings(await api.updateProvider(providerId, input));
      setNotice("提供商设置已保存");
    });
  }

  async function removeProvider(providerId: string): Promise<boolean> {
    return run(async () => {
      replaceSettings(await api.removeProvider(providerId));
      setNotice("提供商已删除");
    });
  }

  async function saveModel(input: ModelSettingsInput): Promise<boolean> {
    if (!loaded) return false;
    const currentProvider = loaded.settings.providers.find((item) => item.id === input.providerId);
    if (!currentProvider) {
      setError("所选模型提供商已不存在");
      return false;
    }
    const changed = (
      (currentProvider.apiBase ?? null) !== input.apiBase
      || (currentProvider.defaultModel ?? "") !== input.model
      || loaded.settings.defaults.model !== input.model
      || loaded.settings.defaults.provider !== input.provider
      || !currentProvider.active
      || Boolean(input.apiKey)
    );
    if (!changed) {
      setError(null);
      setNotice("模型设置没有变化");
      return true;
    }
    return requestRestart("保存模型设置", () => run(async () => {
      replaceSettings(await api.updateDefaults({
        activeProviderId: input.providerId,
        apiBase: input.apiBase,
        ...(input.apiKey ? { apiKey: input.apiKey } : {}),
        model: input.model,
        provider: input.provider,
      }));
      setNotice("模型设置已保存");
    }));
  }

  async function testConnection(providerId: string): Promise<ProviderConnectionTestResponse> {
    setBusy(true);
    setError(null);
    try {
      return await api.testProviderConnection(providerId);
    } catch (testError) {
      setError(errorMessage(testError));
      return { message: "连接失败", model: null, ok: false, providerId };
    } finally {
      setBusy(false);
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
          <p>管理服务器上 Agent 使用的模型连接。API Key 始终以安全方式处理。</p>
        </div>
      </header>

      {error ? <p className="settings-feedback settings-feedback--error" role="alert">{error}</p> : null}
      {notice ? <p className="settings-feedback settings-feedback--success" role="status">{notice}</p> : null}

      <ModelSettings
        key={loaded.settings.providers.map((provider) => [
          provider.id,
          provider.apiBase,
          provider.defaultModel,
          provider.configured,
          provider.credentialSource,
          provider.active,
        ].join(":")).join("|") + loaded.settings.defaults.model}
        busy={busy}
        onSave={saveModel}
        onTest={testConnection}
        settings={loaded.settings}
      />

      <details className="settings-advanced">
        <summary>
          <span>高级设置</span>
          <small>提供商管理、预设、校验、调试与本机显示</small>
        </summary>
        <div className="settings-advanced__content">
          <ProviderSettings
            busy={busy}
            onCreate={createProvider}
            onRemove={removeProvider}
            onUpdate={updateProvider}
            providers={loaded.settings.providers}
            requestRestart={requestRestart}
          />
          <PresetSettings
            presets={loaded.presets}
            onSync={async () => {
              await run(async () => {
                const result = await api.syncPresets();
                const presets = await api.listPresets();
                setLoaded((current) => current ? { ...current, presets: presets.presets } : current);
                setNotice(`已同步 ${result.downloaded} 个预设`);
              });
            }}
          />
          <section className="settings-card" aria-labelledby="validation-title">
            <div className="settings-card__heading">
              <div><h3 id="validation-title">配置校验</h3><p>检查当前服务端配置是否可供 Runtime 使用。</p></div>
              <button className="settings-button settings-button--quiet" onClick={() => void run(async () => {
                const validation = await api.validateSettings();
                setLoaded((current) => current ? { ...current, validation } : current);
                setNotice(validation.valid ? "配置有效" : "配置仍需处理");
              })} type="button">重新校验</button>
            </div>
            {loaded.validation.errors.length === 0
              ? <p className="settings-validation-ok">配置有效</p>
              : <ul>{loaded.validation.errors.map((item) => <li key={item}>{item}</li>)}</ul>}
          </section>
          <section className="settings-card" aria-labelledby="debug-settings-title">
            <div className="settings-card__heading">
              <div><h3 id="debug-settings-title">Agent 调试模式</h3><p>仅在排查运行问题时开启。</p></div>
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
      </details>

      {restartRequest ? (
        <div className="settings-dialog-backdrop" role="presentation">
          <section
            aria-describedby="restart-dialog-description"
            aria-labelledby="restart-dialog-title"
            aria-modal="true"
            className="settings-dialog"
            role="dialog"
          >
            <div className="settings-dialog__icon" aria-hidden="true">↻</div>
            <div>
              <h2 id="restart-dialog-title">确认重启 Agent</h2>
              <p id="restart-dialog-description">
                将{restartRequest.description}。应用配置时会重启 Agent，正在运行的任务可能会中断。
              </p>
            </div>
            <div className="settings-actions settings-dialog__actions">
              <button className="settings-button settings-button--secondary" onClick={cancelRestart} type="button">
                取消
              </button>
              <button className="settings-button settings-button--primary" onClick={() => void confirmRestart()} type="button">
                确认并应用
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </main>
  );
}
