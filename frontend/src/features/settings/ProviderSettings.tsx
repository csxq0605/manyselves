import { useState } from "react";

import type {
  ProviderSettingsCreate,
  ProviderSettingsUpdate,
  SettingsResponse,
} from "./settings-api";

export interface ProviderSettingsProps {
  readonly busy: boolean;
  readonly onCreate: (input: ProviderSettingsCreate) => Promise<boolean>;
  readonly onRemove: (providerId: string) => Promise<boolean>;
  readonly onUpdate: (providerId: string, input: ProviderSettingsUpdate) => Promise<boolean>;
  readonly providers: SettingsResponse["providers"];
  readonly requestRestart: (description: string, action: () => Promise<boolean>) => Promise<boolean>;
}

const providerTypes = ["openai", "anthropic", "deepseek", "google", "openrouter", "groq", "custom"] as const;

export function ProviderSettings({
  busy,
  onCreate,
  onRemove,
  onUpdate,
  providers,
  requestRestart,
}: ProviderSettingsProps) {
  const [name, setName] = useState("");
  const [provider, setProvider] = useState<(typeof providerTypes)[number]>("openai");

  return (
    <section className="settings-card settings-card--wide" aria-labelledby="provider-settings-title">
      <div className="settings-card__heading">
        <div>
          <h3 id="provider-settings-title">提供商管理</h3>
          <p>创建、启停或删除低频使用的提供商配置。</p>
        </div>
      </div>
      {providers.length === 0 ? <p className="settings-note">当前没有提供商。</p> : (
        <ul className="provider-list">
          {providers.map((item) => (
            <li key={item.id}>
              <span className="provider-list__identity">
                <strong>{item.name}</strong>
                <small>{item.provider}{item.active ? " · 当前默认" : ""}</small>
              </span>
              <span className="provider-list__state">
                {item.credentialSource === "environment" ? "环境密钥" : item.configured ? "已配置" : "无密钥"}
              </span>
              <div className="settings-actions">
                <button
                  className="settings-button settings-button--quiet"
                  disabled={busy}
                  onClick={() => void requestRestart(
                    `${item.enabled ? "停用" : "启用"}“${item.name}”提供商`,
                    () => onUpdate(item.id, { enabled: !item.enabled }),
                  )}
                  type="button"
                >{item.enabled ? "停用" : "启用"}</button>
                <button
                  className="settings-button settings-button--danger"
                  disabled={busy}
                  onClick={() => void requestRestart(
                    `删除“${item.name}”提供商`,
                    () => onRemove(item.id),
                  )}
                  type="button"
                >删除</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="provider-create">
        <h4>新建提供商</h4>
        <label>
          显示名称
          <input value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          类型
          <select value={provider} onChange={(event) => setProvider(event.target.value as typeof provider)}>
            {providerTypes.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <button
          className="settings-button settings-button--secondary"
          disabled={busy || !name.trim()}
          onClick={() => void requestRestart(
            `创建“${name.trim()}”提供商`,
            () => onCreate({ enabled: true, name: name.trim(), provider }),
          ).then((created) => {
            if (created) setName("");
          })}
          type="button"
        >创建</button>
      </div>
    </section>
  );
}
