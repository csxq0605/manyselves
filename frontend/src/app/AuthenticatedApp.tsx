import type { ApiGateway } from "../api/gateway";
import { useAuthenticatedSession } from "../features/auth/auth-context";
import type { SettingsStorage } from "../features/settings/settings-storage";
import type { PlatformBridge } from "../platform/types";
import { App } from "./App";

export interface AuthenticatedAppProps {
  readonly eventSource: { readonly baseUrl: string; readonly fetch: typeof fetch };
  readonly gateway: ApiGateway;
  readonly platform: PlatformBridge;
  readonly settingsStorage: SettingsStorage;
}

export function AuthenticatedApp({ eventSource, gateway, platform, settingsStorage }: AuthenticatedAppProps) {
  const { returnToLogin } = useAuthenticatedSession();
  return <App eventSource={eventSource} gateway={gateway} onUnauthorized={returnToLogin} platform={platform} settingsStorage={settingsStorage} />;
}
