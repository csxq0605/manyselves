import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";

import { App } from "./app/App";
import { getOrCreateBrowserClientId } from "./app/client-config";
import { AppProviders } from "./app/providers";
import { createAppRouter } from "./app/router";
import { createApiGateway } from "./api/gateway";
import {
  createBrowserSettingsStorage,
  createElectronSettingsStorage,
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

if (window.manyselvesDesktop) {
  const token = await window.manyselvesDesktop.secureToken.get();
  if (token) window.sessionStorage.setItem("manyselves.deploymentToken", token);
}
const browserSettingsStorage = createBrowserSettingsStorage({
  ...(window.manyselvesDesktop ? { defaultServerUrl: "http://127.0.0.1:8000" } : {}),
  localStorage: window.localStorage,
  root: document.documentElement,
  sessionStorage: window.sessionStorage,
});
const settingsStorage = window.manyselvesDesktop
  ? createElectronSettingsStorage(browserSettingsStorage, window.manyselvesDesktop.secureToken)
  : browserSettingsStorage;
const savedConnection = settingsStorage.loadConnection();
settingsStorage.loadPreferences();
const baseUrl = resolveServerUrl({
  environmentUrl: import.meta.env.VITE_API_BASE_URL,
  origin: window.location.origin,
  savedUrl: savedConnection.serverUrl,
});
const fetchImplementation = window.fetch.bind(window);
const getToken = () => window.sessionStorage.getItem("manyselves.deploymentToken");
const getLeaseToken = () => window.sessionStorage.getItem("manyselves.controlLeaseToken");
const gateway = createApiGateway({
  baseUrl,
  clientId: getOrCreateBrowserClientId(window.localStorage),
  fetch: fetchImplementation,
  getLeaseToken,
  getToken,
});
const platform = window.manyselvesDesktop
  ? new ElectronPlatformBridge(window.manyselvesDesktop)
  : new BrowserPlatformBridge(createDomBrowserPlatformDriver());
const router = createAppRouter(
  <App
    eventSource={{ baseUrl, fetch: fetchImplementation, getToken }}
    gateway={gateway}
    platform={platform}
    settingsStorage={settingsStorage}
  />,
);

createRoot(rootElement).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
);
