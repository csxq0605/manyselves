import { createApiGateway, type ApiGatewayOptions } from "../api/gateway";
import { useAuthenticatedSession } from "../features/auth/auth-context";
import type { SettingsStorage } from "../features/settings/settings-storage";
import type { PlatformBridge } from "../platform/types";
import { App } from "./App";

export interface AuthenticatedAppProps {
  readonly eventSource: { readonly baseUrl: string; readonly fetch: typeof fetch };
  readonly gatewayOptions: Omit<ApiGatewayOptions, "onUnauthorized">;
  readonly platform: PlatformBridge;
  readonly settingsStorage: SettingsStorage;
}

export function AuthenticatedApp({ eventSource, gatewayOptions, platform, settingsStorage }: AuthenticatedAppProps) {
  const { logout, returnToLogin } = useAuthenticatedSession();
  const gateway = createApiGateway({ ...gatewayOptions, onUnauthorized: returnToLogin });
  return <App eventSource={eventSource} gateway={gateway} onLogout={() => void logout()} onUnauthorized={returnToLogin} platform={platform} settingsStorage={settingsStorage} />;
}
