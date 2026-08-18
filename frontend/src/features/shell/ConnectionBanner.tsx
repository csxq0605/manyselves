import { useConnectionStore } from "../../app/store-context";
import type { ConnectionState } from "../../store/connection-store";

const connectionLabels: Record<ConnectionState, string> = {
  connecting: "正在连接服务器",
  offline: "连接已中断，本地草稿仍会保留",
  online: "服务器已连接",
  reconnecting: "正在重新连接服务器",
  resyncing: "正在同步服务器状态",
  unauthorized: "访问令牌无效，请重新连接",
};

export function ConnectionBanner() {
  const state = useConnectionStore((store) => store.state);
  return (
    <div className={`connection-banner connection-banner--${state}`} role="status">
      <span className="connection-banner__signal" aria-hidden="true" />
      <span>{connectionLabels[state]}</span>
    </div>
  );
}
