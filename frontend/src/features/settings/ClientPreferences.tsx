import { useState } from "react";

import type { ClientPreferences as ClientPreferencesValue } from "./settings-storage";

export interface ClientPreferencesProps {
  readonly initialValue: ClientPreferencesValue;
  readonly onSave: (value: ClientPreferencesValue) => void;
}

export function ClientPreferences({ initialValue, onSave }: ClientPreferencesProps) {
  const [value, setValue] = useState(initialValue);

  return (
    <section className="settings-card" aria-labelledby="client-preferences-title">
      <div className="settings-card__heading">
        <div>
          <h3 id="client-preferences-title">客户端偏好</h3>
          <p>只影响当前浏览器的显示方式。</p>
        </div>
        <span className="settings-scope">仅本机</span>
      </div>
      <div className="settings-fields settings-fields--two">
        <label>
          主题
          <select value={value.theme} onChange={(event) => setValue({
            ...value,
            theme: event.target.value as ClientPreferencesValue["theme"],
          })}>
            <option value="system">跟随系统</option>
            <option value="light">浅色</option>
            <option value="dark">深色</option>
          </select>
        </label>
        <label>
          界面密度
          <select value={value.density} onChange={(event) => setValue({
            ...value,
            density: event.target.value as ClientPreferencesValue["density"],
          })}>
            <option value="comfortable">舒适</option>
            <option value="compact">紧凑</option>
          </select>
        </label>
        <label>
          字体大小
          <input
            max={22}
            min={12}
            type="number"
            value={value.fontSize}
            onChange={(event) => setValue({ ...value, fontSize: Number(event.target.value) })}
          />
        </label>
        <label>
          文件默认视图
          <select value={value.previewDefault} onChange={(event) => setValue({
            ...value,
            previewDefault: event.target.value as ClientPreferencesValue["previewDefault"],
          })}>
            <option value="auto">自动判断</option>
            <option value="preview">优先预览</option>
            <option value="source">优先源码</option>
          </select>
        </label>
      </div>
      <label className="settings-check">
        <input
          checked={value.notifications}
          type="checkbox"
          onChange={(event) => setValue({ ...value, notifications: event.target.checked })}
        />
        允许任务完成通知
      </label>
      <button className="settings-button settings-button--secondary" onClick={() => onSave(value)} type="button">保存客户端偏好</button>
    </section>
  );
}
