import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { AuthenticatedApp } from "./app/AuthenticatedApp";
import { getOrCreateBrowserClientId } from "./app/client-config";
import { AppProviders } from "./app/providers";
import { AuthGate } from "./features/auth/AuthGate";
import { createAuthApi } from "./features/auth/auth-api";
import {
  createBrowserSettingsStorage,
  resolveServerUrl,
} from "./features/settings/settings-storage";
import {
  BrowserPlatformBridge,
  createDomBrowserPlatformDriver,
} from "./platform/browser-platform";
import { ElectronPlatformBridge } from "./platform/electron-platform";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Manyselves root element is missing");
}

const browserSettingsStorage = createBrowserSettingsStorage({
  ...(window.manyselvesDesktop ? { defaultServerUrl: "http://192.168.8.28:9090" } : {}),
  localStorage: window.localStorage,
  root: document.documentElement,
});
const settingsStorage = browserSettingsStorage;
const savedConnection = settingsStorage.loadConnection();
settingsStorage.loadPreferences();
const baseUrl = resolveServerUrl({
  environmentUrl: import.meta.env.VITE_API_BASE_URL,
  origin: window.location.origin,
  savedUrl: savedConnection.serverUrl,
});
const fetchImplementation = window.fetch.bind(window);
const getLeaseToken = () => window.sessionStorage.getItem("manyselves.controlLeaseToken");
const setLeaseToken = (token: string | null) => {
  if (token) window.sessionStorage.setItem("manyselves.controlLeaseToken", token);
  else window.sessionStorage.removeItem("manyselves.controlLeaseToken");
};
const gatewayOptions = {
  baseUrl,
  clientId: getOrCreateBrowserClientId(window.localStorage),
  fetch: fetchImplementation,
  getLeaseToken,
  setLeaseToken,
};
const authApi = createAuthApi({ baseUrl, fetch: fetchImplementation });
const platform = window.manyselvesDesktop
  ? new ElectronPlatformBridge(window.manyselvesDesktop)
  : new BrowserPlatformBridge(createDomBrowserPlatformDriver());
createRoot(rootElement).render(
  <StrictMode>
    <AppProviders>
      <BrowserRouter><AuthGate api={authApi}><AuthenticatedApp eventSource={{ baseUrl, fetch: fetchImplementation }} gatewayOptions={gatewayOptions} platform={platform} settingsStorage={settingsStorage} /></AuthGate></BrowserRouter>
    </AppProviders>
  </StrictMode>,
);
