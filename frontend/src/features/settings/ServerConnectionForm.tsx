import { useState } from "react";

import type { ServerConnection } from "./settings-storage";

export interface ServerConnectionFormProps {
  readonly initialValue: ServerConnection;
  readonly onSave: (value: ServerConnection) => void;
  readonly tokenScope?: "secure-device" | "session";
}

export function ServerConnectionForm({ initialValue, onSave, tokenScope = "session" }: ServerConnectionFormProps) {
  const [serverUrl, setServerUrl] = useState(initialValue.serverUrl);
  const [token, setToken] = useState("");

  return (
    <section className="settings-card" aria-labelledby="server-connection-title">
      <div className="settings-card__heading">
        <div>
          <p className="settings-kicker">CLIENT CONNECTION</p>
          <h2 id="server-connection-title">服务器连接</h2>
        </div>
        <span className="settings-scope">{tokenScope === "secure-device" ? "设备加密令牌" : "会话令牌"}</span>
      </div>
      <p className="settings-note">{tokenScope === "secure-device"
        ? "地址保留在本机；访问令牌由操作系统安全存储加密，无法加密时仅保留在内存。"
        : "地址会保留在此浏览器；访问令牌只保留到当前浏览器会话结束。"}</p>
      <label>
        服务器地址
        <input value={serverUrl} onChange={(event) => setServerUrl(event.target.value)} />
      </label>
      <label>
        访问令牌
        <input
          autoComplete="off"
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
        />
      </label>
      {initialValue.token ? <p className="settings-secret-status">访问令牌已配置</p> : null}
      <div className="settings-actions">
        <button onClick={() => {
          onSave({ serverUrl, token: token || initialValue.token });
          setToken("");
        }} type="button">保存并重新连接</button>
        <button onClick={() => {
          setToken("");
          onSave({ serverUrl, token: "" });
        }} type="button">清除访问令牌</button>
      </div>
    </section>
  );
}
