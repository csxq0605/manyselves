import { useState } from "react";

import type {
  ProviderSettingsCreate,
  ProviderSettingsUpdate,
  SettingsResponse,
} from "./settings-api";

export interface RestartRequest {
  readonly description: string;
  readonly run: () => Promise<void>;
}

export interface ProviderSettingsProps {
  readonly onCreate: (input: ProviderSettingsCreate) => Promise<void>;
  readonly onRemove: (providerId: string) => Promise<void>;
  readonly onUpdate: (providerId: string, input: ProviderSettingsUpdate) => Promise<void>;
  readonly providers: SettingsResponse["providers"];
  readonly requestRestart: (request: RestartRequest) => void;
}

export function ProviderSettings({
  onCreate,
  onRemove,
  onUpdate,
  providers,
  requestRestart,
}: ProviderSettingsProps) {
  const [selectedId, setSelectedId] = useState(providers[0]?.id ?? "");
  const selected = providers.find((provider) => provider.id === selectedId) ?? providers[0];
  const [apiBase, setApiBase] = useState(selected?.apiBase ?? "");
  const [defaultModel, setDefaultModel] = useState(selected?.defaultModel ?? "");
  const [secret, setSecret] = useState("");
  const [newProvider, setNewProvider] = useState({ name: "", provider: "" });

  if (!selected) {
    return (
      <section className="settings-card settings-card--wide">
        <h2>Provider</h2>
        <p className="settings-note">尚未配置 Provider。请创建第一个 Provider。</p>
        <CreateProviderForm value={newProvider} onChange={setNewProvider} onCreate={onCreate} requestRestart={requestRestart} />
      </section>
    );
  }

  return (
    <section className="settings-card settings-card--wide" aria-labelledby="provider-settings-title">
      <div className="settings-card__heading">
        <div>
          <p className="settings-kicker">SECRET-SAFE CONFIGURATION</p>
          <h2 id="provider-settings-title">Provider 管理</h2>
        </div>
        <span className="settings-scope">服务端</span>
      </div>
      <div className="provider-strip" role="list" aria-label="Provider 列表">
        {providers.map((provider) => (
          <button
            aria-pressed={provider.id === selected.id}
            key={provider.id}
            onClick={() => {
              setSelectedId(provider.id);
              setApiBase(provider.apiBase ?? "");
              setDefaultModel(provider.defaultModel ?? "");
            }}
            role="listitem"
            type="button"
          >
            <strong>{provider.name}</strong>
            <span>{provider.configured ? "凭据已配置" : "未配置凭据"}</span>
          </button>
        ))}
      </div>
      <div className="settings-fields settings-fields--two">
        <label>
          API 地址
          <input value={apiBase} onChange={(event) => setApiBase(event.target.value)} />
        </label>
        <label>
          Provider 默认模型
          <input value={defaultModel} onChange={(event) => setDefaultModel(event.target.value)} />
        </label>
      </div>
      <div className="settings-actions">
        <button onClick={() => requestRestart({
          description: `更新 ${selected.name} 的连接配置`,
          run: () => onUpdate(selected.id, {
            apiBase: apiBase.trim() || null,
            defaultModel: defaultModel.trim() || null,
          }),
        })} type="button">保存 Provider 配置</button>
        <button onClick={() => requestRestart({
          description: `${selected.enabled ? "停用" : "启用"} ${selected.name}`,
          run: () => onUpdate(selected.id, { enabled: !selected.enabled }),
        })} type="button">{selected.enabled ? "停用 Provider" : "启用 Provider"}</button>
      </div>
      <div className="credential-row">
        <label>
          新凭据
          <input
            autoComplete="new-password"
            type="password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
          />
        </label>
        <button disabled={!secret} onClick={() => {
          const value = secret;
          requestRestart({
            description: `替换 ${selected.name} 的凭据`,
            run: async () => {
              await onUpdate(selected.id, { apiKey: value });
              setSecret("");
            },
          });
        }} type="button">替换凭据</button>
        <button onClick={() => requestRestart({
          description: `移除 ${selected.name} 的凭据`,
          run: () => onUpdate(selected.id, { apiKey: null }),
        })} type="button">移除凭据</button>
      </div>
      <button className="settings-danger" onClick={() => requestRestart({
        description: `删除 ${selected.name}`,
        run: () => onRemove(selected.id),
      })} type="button">删除 Provider</button>
      <CreateProviderForm value={newProvider} onChange={setNewProvider} onCreate={onCreate} requestRestart={requestRestart} />
    </section>
  );
}

interface CreateProviderFormProps {
  readonly onChange: (value: { name: string; provider: string }) => void;
  readonly onCreate: (input: ProviderSettingsCreate) => Promise<void>;
  readonly requestRestart: (request: RestartRequest) => void;
  readonly value: { name: string; provider: string };
}

function CreateProviderForm({ onChange, onCreate, requestRestart, value }: CreateProviderFormProps) {
  return (
    <div className="provider-create">
      <h3>创建 Provider</h3>
      <label>
        显示名称
        <input value={value.name} onChange={(event) => onChange({ ...value, name: event.target.value })} />
      </label>
      <label>
        Provider 类型
        <input value={value.provider} onChange={(event) => onChange({ ...value, provider: event.target.value })} />
      </label>
      <button disabled={!value.name.trim() || !value.provider.trim()} onClick={() => requestRestart({
        description: `创建 ${value.name.trim()} Provider`,
        run: async () => {
          await onCreate({ enabled: true, name: value.name.trim(), provider: value.provider.trim() });
          onChange({ name: "", provider: "" });
        },
      })} type="button">创建 Provider</button>
    </div>
  );
}
